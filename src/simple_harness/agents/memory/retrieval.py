# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Hybrid, Agent-scoped retrieval over the session Journal (BA23–BA29).

Path (BA-v1.0 §7.6): exact seq / identifier → lexical candidates (words + trigram)
→ vector candidates (this Agent's vectors of the current embedding fingerprint)
→ reciprocal-rank fusion → verification (agent_id, hash) → bounded packing by the
caller.  Every query is scoped by ``agent_id`` at the SQL level; there is no API
that takes an arbitrary agent id from the model (BA23).

Degradations are explicit fields of ``SearchResult`` (BA25): ``fts_available``,
``embedding_available``, ``index_partial`` (vectors lag the Journal high-water).
Zero hits are a legitimate answer (BA27): candidates below the score floor are
dropped instead of padding to ``limit``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from simple_harness.contracts import canonical_json, thaw_json
from simple_harness.execution.base_agent import AgentJournalRecord
from simple_harness.execution.sqlite.base_agent.indexes import FTS_TRIGRAM, FTS_WORDS
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork

from .embedding import EmbeddingPort, EmbeddingUnavailable, cosine
from .lexical import identifiers, short_terms, trigram_match, words_match

RRF_K = 60
WEIGHTS = {"exact": 1.0, "words": 0.6, "trigram": 0.5, "vector": 0.7}
VECTOR_FLOOR = 0.35
SCORE_FLOOR = 0.006
HIT_TEXT_LIMIT = 4000


@dataclass(frozen=True, slots=True)
class SearchHit:
    seq: int
    kind: str
    score: float
    sources: tuple[str, ...]
    text: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    agent_id: str
    query: str
    query_hash: str
    hits: tuple[SearchHit, ...]
    fts_available: bool
    embedding_available: bool
    index_partial: bool
    index_generation: str | None
    degradations: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict:
        return {
            "query_hash": self.query_hash,
            "hits": [
                {
                    "seq": hit.seq,
                    "kind": hit.kind,
                    "score": round(hit.score, 6),
                    "sources": list(hit.sources),
                    "text": hit.text,
                }
                for hit in self.hits
            ],
            "fts_available": self.fts_available,
            "embedding_available": self.embedding_available,
            "index_partial": self.index_partial,
            "index_generation": self.index_generation,
            "degradations": list(self.degradations),
        }


def record_text(record: AgentJournalRecord) -> str:
    message = thaw_json(record.message_json)
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    return canonical_json(content) if content is not None else ""


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


class SessionRetriever:
    def __init__(
        self,
        uow: SqliteExecutionUnitOfWork,
        *,
        embedding: EmbeddingPort | None,
        fts_available: bool,
        clock: Callable[[], float],
        search_window: int = 2000,
    ) -> None:
        self._uow = uow
        self._embedding = embedding
        self._fts_available = fts_available
        self._clock = clock
        self._search_window = max(64, int(search_window))
        self._query_cache: dict[str, list[float]] = {}

    @property
    def embedding_fingerprint(self) -> str | None:
        return None if self._embedding is None else self._embedding.fingerprint

    def _query_vector(self, query: str) -> list[float]:
        key = query_hash(query)
        cached = self._query_cache.get(key)
        if cached is not None:
            return cached
        assert self._embedding is not None
        [vector] = self._embedding.embed([query])
        self._remember_query(key, vector)
        return vector

    def _remember_query(self, key: str, vector: list[float]) -> None:
        if len(self._query_cache) >= 256:
            self._query_cache.pop(next(iter(self._query_cache)))
        self._query_cache[key] = vector

    async def prewarm(self, query: str) -> None:
        """Embed a query off the event loop so the sync recall never blocks on the
        model (review S4-04); a no-op without an embedding port."""

        query = query.strip()
        if self._embedding is None or not query:
            return
        key = query_hash(query)
        if key in self._query_cache:
            return
        try:
            [vector] = await asyncio.to_thread(self._embedding.embed, [query])
        except Exception:  # noqa: BLE001 - the sync path will report the degradation
            return
        self._remember_query(key, vector)

    async def search(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 8,
        exclude_seqs: Sequence[int] = (),
        kinds: Sequence[str] = ("user_input", "assistant", "tool_result"),
    ) -> SearchResult:
        """Async facade for tools; the work itself is synchronous (SQLite is single-thread)."""

        return self.search_sync(
            agent_id, query, limit=limit, exclude_seqs=exclude_seqs, kinds=kinds
        )

    def search_sync(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 8,
        exclude_seqs: Sequence[int] = (),
        kinds: Sequence[str] = ("user_input", "assistant", "tool_result"),
    ) -> SearchResult:
        query = query.strip()
        degradations: list[str] = []
        highwater = self._uow.agent_journal_highwater(agent_id)
        # Bounded read (review S4-04): the newest ``search_window`` rows are candidates;
        # older history stays reachable through ``read()`` pagination.
        records = {
            r.seq: r
            for r in self._uow.read_agent_journal(
                agent_id, from_seq=max(1, highwater - self._search_window + 1)
            )
            if r.kind in kinds
        }
        if self._fts_available and records:
            if self._uow.agent_fts_highwater(agent_id) < max(records):
                degradations.append("fts_partial")
        ranked: dict[str, list[int]] = {}
        # 1. exact seq references ("seq 12", "#12")
        exact = [int(m) for m in re.findall(r"(?:seq\s*|#)(\d+)", query) if int(m) in records]
        if exact:
            ranked["exact"] = exact
        # 2. lexical
        if self._fts_available:
            tokens = identifiers(query)
            match = words_match(tokens)
            if match:
                ranked["words"] = [
                    seq
                    for seq, _ in self._uow.search_agent_fts(
                        agent_id=agent_id, table=FTS_WORDS, match=match, limit=limit * 4
                    )
                    if seq in records
                ]
            tri = trigram_match(query)
            if tri:
                ranked["trigram"] = [
                    seq
                    for seq, _ in self._uow.search_agent_fts(
                        agent_id=agent_id, table=FTS_TRIGRAM, match=tri, limit=limit * 4
                    )
                    if seq in records
                ]
            elif not tokens:
                # "short word" path of [W02]: exact substring scan, this Agent only.
                terms = short_terms(query)
                if terms:
                    ranked["exact"] = ranked.get("exact", []) + [
                        seq for seq, r in records.items() if any(t in record_text(r) for t in terms)
                    ]
        else:
            degradations.append("fts_unavailable")
            ranked["exact"] = ranked.get("exact", []) + [
                seq for seq, r in records.items() if query and query in record_text(r)
            ]
        # 3. vectors
        embedding_available = self._embedding is not None
        index_partial = False
        generation = self.embedding_fingerprint
        if self._embedding is not None:
            vectors = self._uow.list_agent_vectors(
                agent_id=agent_id, embedding_fingerprint=self._embedding.fingerprint
            )
            indexed = {v.record_seq for v in vectors}
            if any(seq not in indexed for seq in records):
                index_partial = True
                degradations.append("index_partial")
            errors = self._uow.agent_index_errors(
                agent_id=agent_id, embedding_fingerprint=self._embedding.fingerprint
            )
            degradations.extend(f"index_error:{code}" for code in errors)
            try:
                qvec = self._query_vector(query) if query else []
            except EmbeddingUnavailable as error:
                embedding_available = False
                degradations.append(error.code)
                qvec = []
            except Exception:  # noqa: BLE001 - any embedding failure is a visible degradation
                embedding_available = False
                degradations.append("embedding_unavailable")
                qvec = []
            if qvec:
                usable = [
                    v
                    for v in vectors
                    if v.record_seq in records
                    and v.source_hash == records[v.record_seq].content_hash
                ]
                if any(len(v.vector) != len(qvec) for v in usable):
                    degradations.append("embedding_dim_mismatch")
                    usable = [v for v in usable if len(v.vector) == len(qvec)]
                scored = sorted(
                    ((cosine(qvec, v.vector), v.record_seq) for v in usable), reverse=True
                )
                ranked["vector"] = [seq for sim, seq in scored[: limit * 4] if sim >= VECTOR_FLOOR]
        else:
            degradations.append("embedding_unavailable")
        # 4. fusion
        fused: dict[int, float] = {}
        sources: dict[int, list[str]] = {}
        for source, seqs in ranked.items():
            for rank, seq in enumerate(dict.fromkeys(seqs), start=1):
                if seq in exclude_seqs:
                    continue
                fused[seq] = fused.get(seq, 0.0) + WEIGHTS[source] / (RRF_K + rank)
                sources.setdefault(seq, []).append(source)
        hits = []
        for seq, score in sorted(fused.items(), key=lambda kv: (-kv[1], kv[0])):
            if score < SCORE_FLOOR:
                continue  # BA27: never pad to limit
            record = records[seq]
            hits.append(
                SearchHit(
                    seq=seq,
                    kind=record.kind,
                    score=score,
                    sources=tuple(sources[seq]),
                    text=record_text(record)[:HIT_TEXT_LIMIT],
                    content_hash=record.content_hash,
                )
            )
            if len(hits) >= limit:
                break
        return SearchResult(
            agent_id=agent_id,
            query=query,
            query_hash=query_hash(query),
            hits=tuple(hits),
            fts_available=self._fts_available,
            embedding_available=embedding_available,
            index_partial=index_partial,
            index_generation=generation,
            degradations=tuple(dict.fromkeys(degradations)),
        )

    def read(
        self, agent_id: str, *, from_seq: int, to_seq: int | None, page_size: int = 50
    ) -> tuple[tuple[AgentJournalRecord, ...], int | None]:
        """Sequential pagination for "all history" needs (BA29): never top-k."""

        page_size = max(1, min(int(page_size), 200))
        end = from_seq + page_size - 1 if to_seq is None else min(to_seq, from_seq + page_size - 1)
        records = self._uow.read_agent_journal(agent_id, from_seq=from_seq, to_seq=end)
        highwater = self._uow.agent_journal_highwater(agent_id)
        next_seq = end + 1 if end < highwater and (to_seq is None or end < to_seq) else None
        return records, next_seq


def hits_json(result: SearchResult) -> str:
    return json.dumps(result.to_json(), ensure_ascii=False)


__all__ = (
    "SCORE_FLOOR",
    "SearchHit",
    "SearchResult",
    "SessionRetriever",
    "query_hash",
    "record_text",
)
