# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``SessionSearchService``: frozen-snapshot search and exact history reads (§5, C3–C6).

One logical query = one ``query_snapshots`` row (identity of what may be seen:
generation, ``upper_commit``, Journal high-water, exclusions, authority read set)
plus one ``search_queries`` row that scans the frozen chunk range page by page.
While scanning, every page is ``SCANNING`` with **empty items** and a cursor; only
when the whole frozen range has been scanned are the per-channel top-K heaps
fused (weighted RRF, stable five-tuple ties) into the final ranking, which
``RESULTS`` pages then append in fixed order.  Cursor tokens are random 256-bit
values; the partition stores only their hash and the immutable page they
materialised, so a re-sent token returns the same page and never advances.

Scoring channels (C3 step 6): ``exact`` (record id / ``seq N`` hits), ``words``
(unique NFKC-casefolded literal tokens), ``trigram`` (unique query trigrams found
as substrings, query ≥ 3 characters) and ``vector`` (f32 dot product ≥ 0.35 when
the query has a real embedding).  No mutable whole-library BM25 is used.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from simple_harness.contracts import thaw_json
from simple_harness.execution.sqlite.base_agent import history

from .codec import check
from .errors import ArpError
from .partition import Partition, unpack_vector
from .pins import Pin
from .retrieval_status import status_for
from .rules import check_cursor, rrf, utf8_slice
from .strict import canonical, digest

VECTOR_FLOOR = 0.35
MAX_QUERY_WORDS = 16
RESULT_PAGE_ITEMS = 32
MAX_RESULT_PAGES = 16
MAX_SCAN_PAGES = 256
MAX_MERGED_BYTES = 16384
ALGORITHM_ID = "arp-rrf-snapshot-v1"
_WORD = re.compile(r"[\w]+", re.UNICODE)
_SEQ = re.compile(r"(?:\bseq\s*|#)(\d+)\b", re.IGNORECASE)
PROVENANCE_OF_KIND = {
    "instructions": "USER_INPUT",
    "user_input": "USER_INPUT",
    "assistant": "AGENT_CLAIM",
    "tool_result": "TOOL_RESULT",
    "feedback": "VERIFIER_FEEDBACK",
}


# ---- access ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionAccess:
    """Trusted access facts for one Session (never serialised to the model)."""

    session_ref: Pin
    agent_ref: Pin
    turn_ref: Pin | None
    root_incarnation: str
    purpose: str
    caller_ref: Pin
    authority_refs: tuple[Pin, ...]
    authority_readset_hash: str
    control_generation: int
    expires_at_ms: int

    @property
    def session_id(self) -> str:
        return self.session_ref.id

    @property
    def agent_id(self) -> str:
        return self.agent_ref.id

    @property
    def owner_scope_hash(self) -> str:
        return digest({"caller": self.caller_ref.to_json(), "session": self.session_ref.id})


# ---- query text normalisation ---------------------------------------------------


def normalize_words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    seen: list[str] = []
    for token in _WORD.findall(folded):
        if token not in seen:
            seen.append(token)
    return seen


def trigrams(text: str) -> set[str]:
    """Query trigrams; a query shorter than three characters is matched as one bounded substring."""

    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = "".join(folded.split())
    if len(folded) >= 3:
        return {folded[i : i + 3] for i in range(len(folded) - 2)}
    return {folded} if folded else set()


def exact_targets(query: str) -> tuple[set[int], set[str]]:
    seqs = {int(m) for m in _SEQ.findall(query)}
    ids = {t for t in query.split() if ":" in t and len(t) >= 8}
    return seqs, ids


@dataclass(frozen=True, slots=True)
class QueryPlan:
    text: str
    words: tuple[str, ...]
    grams: frozenset[str]
    seqs: frozenset[int]
    record_ids: frozenset[str]
    vector: tuple[float, ...] | None

    @classmethod
    def build(cls, text: str, vector: Sequence[float] | None) -> "QueryPlan":
        seqs, ids = exact_targets(text)
        return cls(
            text,
            tuple(normalize_words(text)[:MAX_QUERY_WORDS]),
            frozenset(trigrams(text)),
            frozenset(seqs),
            frozenset(ids),
            None if vector is None else tuple(float(v) for v in vector),
        )


def score_row(plan: QueryPlan, row: Mapping[str, Any], *, hybrid: bool) -> dict[str, float]:
    """Per-channel scores for one frozen chunk row; absent channels are omitted."""

    scores: dict[str, float] = {}
    text = str(row["text_view"])
    if plan.seqs or plan.record_ids:
        hits = sum(1 for s in plan.seqs if int(row["seq_from"]) <= s <= int(row["seq_to"]))
        hits += 1 if str(row["record_id"]) in plan.record_ids else 0
        if hits:
            scores["exact"] = float(hits)
    if plan.words:
        tokens = set(normalize_words(text))
        hit = sum(1 for w in plan.words if w in tokens)
        if hit:
            scores["words"] = float(hit)
    if plan.grams:
        folded = "".join(unicodedata.normalize("NFKC", text).casefold().split())
        hit = sum(1 for g in plan.grams if g in folded)
        if hit:
            scores["trigram"] = float(hit)
    if hybrid and plan.vector is not None and row.get("vector") is not None:
        vector = unpack_vector(row["vector"], int(row["dim"]))
        if len(vector) == len(plan.vector):
            dot = sum(a * b for a, b in zip(vector, plan.vector))
            if dot >= VECTOR_FLOOR:
                scores["vector"] = float(dot)
    return scores


def stable_key(row: Mapping[str, Any]) -> tuple:
    return (str(row["record_id"]), str(row["source_hash"]), int(row["utf8_start"]), int(row["utf8_end"]), str(row["chunk_id"]))


# ---- service -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrozenQuery:
    query_id: str
    snapshot: Mapping[str, Any]
    body: Mapping[str, Any]


class SessionSearchService:
    def __init__(
        self,
        partition: Partition,
        *,
        clock_ms: Callable[[], int],
        charge: Callable[[str], int],
        cursor_ttl_ms: int,
    ) -> None:
        self._p = partition
        self._clock_ms = clock_ms
        self._charge = charge
        self._ttl = int(cursor_ttl_ms)

    # ---- snapshot (C1) ------------------------------------------------------------

    def freeze_snapshot(
        self,
        access: SessionAccess,
        *,
        index_generation: int,
        journal_highwater: int,
        expected_group_set_hash: str,
        expected_groups: int,
        source_snapshot_hash: str,
        embedding_fingerprint: str,
        chunker_fingerprint: str,
        view_policy_hash: str,
        identity: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Create (or replay) the ``IndexSnapshot`` for one logical query identity."""

        upper = self._p.upper_commit(index_generation)
        snapshot_id = "snap-" + digest({"identity": dict(identity), "generation": index_generation, "upper": upper})[:32]
        existing = self._p.connection.execute(
            "SELECT body_json FROM query_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if existing is not None:
            return check("IndexSnapshot", _load(existing[0]))
        counts = self._p.coverage(index_generation, upper, journal_highwater)
        body = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "session_id": access.session_id,
            "control_generation": access.control_generation,
            "index_generation": index_generation,
            "upper_commit": upper,
            "journal_highwater": journal_highwater,
            "expected_group_set_hash": expected_group_set_hash,
            "indexed_group_set_hash": self._p.indexed_group_set_hash(index_generation, upper, journal_highwater),
            "embedding_fingerprint": embedding_fingerprint,
            "chunker_fingerprint": chunker_fingerprint,
            "view_policy_hash": view_policy_hash,
            "authority_readset_hash": access.authority_readset_hash,
            "source_snapshot_hash": source_snapshot_hash,
            "created_at_ms": self._clock_ms(),
        }
        value = check("IndexSnapshot", body)
        with self._p.transaction() as connection:
            connection.execute(
                "INSERT INTO query_snapshots(snapshot_id,index_generation,upper_commit,journal_highwater,source_snapshot_hash,body_json,created_at_ms)"
                " VALUES (?,?,?,?,?,?,?)",
                (snapshot_id, index_generation, upper, journal_highwater, source_snapshot_hash, _dump({**value, "expected_groups": expected_groups, **counts}), value["created_at_ms"]),
            )
        return value

    def snapshot_extras(self, snapshot_id: str) -> Mapping[str, Any]:
        row = self._p.connection.execute("SELECT body_json FROM query_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise ArpError("CURSOR_STALE", "unknown snapshot")
        return _load(row[0])

    # ---- start / resume (C4) -----------------------------------------------------

    def start_frozen(
        self,
        access: SessionAccess,
        request: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        *,
        query_vector: Sequence[float] | None,
        query_vector_ref: Pin | None,
        mode: str,
        query_id: str,
        purpose: str = "CONTEXT_RECALL",
    ) -> tuple[Mapping[str, Any], str | None]:
        """Create the frozen query (SCANNING) and produce its first page."""

        if mode not in ("HYBRID", "LEXICAL_ONLY", "EXACT_ONLY"):
            raise ArpError("ENUM", field_path="mode")
        limits = request["limits"]
        exclusions = sorted(set(request["mandatory_group_ids"]) | set(request["protected_group_ids"]))
        body = {
            "query_text": request["query_text"],
            "query_hash": request["query_hash"],
            "exclusions": exclusions,
            "journal_highwater": int(request["journal_highwater"]),
            "mode": mode,
            "page_rows": int(limits["page_rows"]),
            "channel_top_k": int(limits["channel_top_k"]),
            "global_candidates": int(limits["global_candidates"]),
            "policy_ref": request["policy_ref"],
            "control_generation": int(request["control_generation"]),
            "snapshot_id": snapshot["snapshot_id"],
            "purpose": purpose,
            "recall_key": request.get("recall_key"),
        }
        options_hash = digest({k: body[k] for k in ("exclusions", "journal_highwater", "mode", "page_rows", "channel_top_k", "global_candidates")})
        existing = self._p.connection.execute("SELECT 1 FROM search_queries WHERE query_id=?", (query_id,)).fetchone()
        if existing is None:
            snapshot_chunks = self._count_visible(snapshot, body)
            body["snapshot_chunks"] = snapshot_chunks
            state = {"heaps": {}, "scanned": 0, "groups": [], "pages": 0}
            now = self._clock_ms()
            with self._p.transaction() as connection:
                connection.execute(
                    "INSERT INTO search_queries(query_id,snapshot_id,owner_scope_hash,purpose,query_hash,options_hash,query_body_json,phase,"
                    "after_rowid,heap_json,final_results_json,query_vector_ref_json,elapsed_ms,expires_at_ms,control_generation,authority_hash,row_version)"
                    " VALUES (?,?,?,?,?,?,?,'SCANNING',0,?,NULL,?,0,?,?,?,1)",
                    (
                        query_id, snapshot["snapshot_id"], access.owner_scope_hash, purpose, body["query_hash"], options_hash,
                        _dump(body), _dump(state),
                        None if query_vector is None else _dump({"ref": None if query_vector_ref is None else query_vector_ref.to_json(), "vector": [float(v) for v in query_vector]}),
                        now + self._ttl, access.control_generation, access.authority_readset_hash,
                    ),
                )
                token = self._new_cursor_locked(
                    connection, access, query_id=query_id, snapshot=snapshot, purpose=purpose,
                    position={"phase": "SCANNING", "ordinal": 1}, token=self._first_token(query_id, access, purpose),
                )
        else:
            # The internal recall query re-derives its first cursor so a coordinator that
            # crashed between creating the query and checkpointing can continue it.
            token = self._first_token(query_id, access, purpose)
        if token is None:
            raise ArpError("CURSOR_UNKNOWN", "query already started; resume with its cursor")
        return self.resume_frozen(access, token)

    @staticmethod
    def _first_token(query_id: str, access: SessionAccess, purpose: str) -> str | None:
        if purpose != "CONTEXT_RECALL":
            return None  # model/host-facing cursors stay random and unrecoverable by design
        return hashlib.sha256(f"first-cursor:{query_id}:{access.owner_scope_hash}:{access.control_generation}".encode("utf-8")).hexdigest()

    def resume_frozen(self, access: SessionAccess, cursor: str) -> tuple[Mapping[str, Any], str | None]:
        """Materialise the page a PENDING cursor points at, or replay a materialised one."""

        token_hash = hashlib.sha256(cursor.encode("utf-8")).hexdigest()
        row = self._p.connection.execute(
            "SELECT query_id,purpose,snapshot_ref_json,owner_scope_hash,control_generation,authority_hash,expires_at_ms,state,position_json,page_json,next_token_hash"
            " FROM cursor_pages WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ArpError("CURSOR_UNKNOWN")
        query_id = str(row[0])
        query = self._query(query_id)
        snapshot = self.snapshot_extras(query["snapshot_id"])
        check_cursor(
            {
                "owner": str(row[3]), "session": snapshot["session_id"], "control_generation": int(row[4]), "purpose": str(row[1]),
                "query_hash": query["query_hash"], "request_hash": query["options_hash"], "expires_at_ms": int(row[6]),
                "index_generation": int(snapshot["index_generation"]), "authority_hash": str(row[5]), "root_incarnation": access.root_incarnation,
            },
            {
                "owner": access.owner_scope_hash, "session": access.session_id, "control_generation": access.control_generation, "purpose": str(row[1]),
                "query_hash": query["query_hash"], "request_hash": query["options_hash"], "now_ms": self._clock_ms(),
                "index_generation": int(snapshot["index_generation"]), "authority_hash": access.authority_readset_hash, "root_incarnation": access.root_incarnation,
            },
        )
        if str(row[7]) == "MATERIALIZED":
            page = _load(row[9])
            return page, self._token_for(page)
        position = _load(row[8])
        started = self._clock_ms()
        if position["phase"] == "SCANNING":
            page, next_position = self._scan_page(access, query, snapshot, position)
        else:
            page, next_position = self._results_page(access, query, snapshot, position)
        with self._p.transaction() as connection:
            next_token = None
            if next_position is not None:
                next_token = self._new_cursor_locked(connection, access, query_id=query_id, snapshot=snapshot, purpose=str(row[1]), position=next_position)
            page = dict(page)
            page["next_cursor"] = next_token
            page["has_more"] = next_token is not None
            page["receipt"] = {**page["receipt"], "has_more": next_token is not None}
            page = _with_body_bytes(page)
            value = check("ContextSearchPage" if str(row[1]) == "CONTEXT_RECALL" else "SearchPage", page)
            page_hash = digest(value)
            updated = connection.execute(
                "UPDATE cursor_pages SET state='MATERIALIZED', page_json=?, page_hash=?, next_token_hash=? WHERE token_hash=? AND state='PENDING'",
                (_dump(value), page_hash, None if next_token is None else hashlib.sha256(next_token.encode("utf-8")).hexdigest(), token_hash),
            ).rowcount
            if updated != 1:
                raise ArpError("CURSOR_STALE", "cursor materialised concurrently")
            elapsed = max(0, self._clock_ms() - started)
            self._advance_query_locked(connection, query, page, elapsed)
        return value, next_token

    # ---- internals ------------------------------------------------------------------

    def _query(self, query_id: str) -> dict[str, Any]:
        row = self._p.connection.execute(
            "SELECT query_id,snapshot_id,owner_scope_hash,purpose,query_hash,options_hash,query_body_json,phase,after_rowid,heap_json,final_results_json,"
            "query_vector_ref_json,elapsed_ms,expires_at_ms,control_generation,authority_hash,row_version FROM search_queries WHERE query_id=?",
            (query_id,),
        ).fetchone()
        if row is None:
            raise ArpError("CURSOR_UNKNOWN", "unknown query")
        return {
            "query_id": str(row[0]), "snapshot_id": str(row[1]), "owner_scope_hash": str(row[2]), "purpose": str(row[3]),
            "query_hash": str(row[4]), "options_hash": str(row[5]), "body": _load(row[6]), "phase": str(row[7]),
            "after_rowid": int(row[8]), "state": _load(row[9]), "final": None if row[10] is None else _load(row[10]),
            "vector": None if row[11] is None else _load(row[11]), "elapsed_ms": int(row[12]), "expires_at_ms": int(row[13]),
            "control_generation": int(row[14]), "authority_hash": str(row[15]), "row_version": int(row[16]),
        }

    def _visible_sql(self, body: Mapping[str, Any]) -> tuple[str, list[Any]]:
        params: list[Any] = [int(body["journal_highwater"])]
        clause = " AND s.seq_to<=?"
        exclusions = list(body["exclusions"])
        if exclusions:
            clause += " AND c.group_id NOT IN (%s)" % ",".join("?" * len(exclusions))
            params.extend(exclusions)
        return clause, params

    def _count_visible(self, snapshot: Mapping[str, Any], body: Mapping[str, Any]) -> int:
        clause, params = self._visible_sql(body)
        return int(
            self._p.connection.execute(
                "SELECT COUNT(*) FROM session_chunks c JOIN group_sources s ON s.group_id=c.group_id AND s.source_hash=c.source_hash"
                " WHERE c.index_generation=? AND c.commit_seq<=?" + clause,
                [int(snapshot["index_generation"]), int(snapshot["upper_commit"]), *params],
            ).fetchone()[0]
        )

    def _rows(self, snapshot: Mapping[str, Any], body: Mapping[str, Any], after_rowid: int, limit: int) -> list[dict[str, Any]]:
        clause, params = self._visible_sql(body)
        rows = self._p.connection.execute(
            "SELECT c.rowid,c.chunk_id,c.record_id,c.group_id,c.utf8_start,c.utf8_end,c.source_hash,c.view_hash,c.text_view,c.provenance,c.validity_epoch,"
            " s.seq_from,s.seq_to,v.vector_le_f32,v.dim FROM session_chunks c"
            " JOIN group_sources s ON s.group_id=c.group_id AND s.source_hash=c.source_hash"
            " LEFT JOIN session_vectors v ON v.chunk_id=c.chunk_id AND v.index_generation=c.index_generation AND v.commit_seq<=?"
            " WHERE c.index_generation=? AND c.commit_seq<=? AND c.rowid>?" + clause + " ORDER BY c.rowid LIMIT ?",
            [int(snapshot["upper_commit"]), int(snapshot["index_generation"]), int(snapshot["upper_commit"]), int(after_rowid), *params, int(limit)],
        ).fetchall()
        keys = ("rowid", "chunk_id", "record_id", "group_id", "utf8_start", "utf8_end", "source_hash", "view_hash", "text_view", "provenance", "validity_epoch", "seq_from", "seq_to", "vector", "dim")
        return [dict(zip(keys, r)) for r in rows]

    def _coverage(self, query: Mapping[str, Any], snapshot: Mapping[str, Any], *, phase: str, scanned: int) -> dict[str, Any]:
        body = query["body"]
        mode = body["mode"]
        expected = int(snapshot["expected_groups"])
        indexed = int(snapshot["indexed_groups"])
        vector_ready = int(snapshot["vector_ready_groups"])
        complete = indexed == expected and (mode != "HYBRID" or vector_ready == expected)
        return {
            "phase": phase,
            "rank_scope": "NONE" if phase == "SCANNING" else "FROZEN_INDEX_CHANNEL_TOPK",
            "index_coverage": "COMPLETE" if complete else "PARTIAL",
            "mode": mode,
            "scanned_chunks": scanned,
            "snapshot_chunks": int(body["snapshot_chunks"]),
            "expected_groups": expected,
            "indexed_groups": indexed,
            "vector_ready_groups": vector_ready,
            "ranking_final": phase == "RESULTS",
            "algorithm_id": ALGORITHM_ID,
        }

    def _receipt(self, access: SessionAccess, query: Mapping[str, Any], snapshot: Mapping[str, Any], *, coverage: Mapping[str, Any], searched: int, elapsed: int, total: int | None, offset: int, returned: int) -> dict[str, Any]:
        body = query["body"]
        vector = query["vector"]
        degradations = []
        if body["mode"] == "LEXICAL_ONLY":
            degradations.append("embedding_unavailable")
        if coverage["index_coverage"] == "PARTIAL":
            degradations.append("INDEX_LAG")
        has_more = True if coverage["phase"] == "SCANNING" else (offset + returned < (total or 0))
        return {
            "schema_version": 2,
            "session_id": access.session_id,
            "generation": access.control_generation,
            "query_hash": body["query_hash"],
            "journal_highwater": int(body["journal_highwater"]),
            "index_snapshot_id": snapshot["snapshot_id"],
            "embedding_ref": None if vector is None or vector.get("ref") is None else vector["ref"],
            "searched_closed_groups": searched if coverage["phase"] == "SCANNING" else int(coverage["indexed_groups"]),
            "indexed_closed_groups": int(coverage["indexed_groups"]),
            "status": status_for(coverage, total),
            "degradations": degradations,
            "excluded_group_ids": list(body["exclusions"]),
            "elapsed_ms": elapsed,
            "policy_ref": body["policy_ref"],
            "index_generation": int(snapshot["index_generation"]),
            "index_upper_commit": int(snapshot["upper_commit"]),
            "authority_readset_hash": access.authority_readset_hash,
            "coverage": dict(coverage),
            "source_snapshot_hash": snapshot["source_snapshot_hash"],
            "returned_count": returned,
            "query_result_count": total,
            "page_offset": offset,
            "has_more": has_more,
        }

    def _scan_page(self, access: SessionAccess, query: dict[str, Any], snapshot: Mapping[str, Any], position: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        body = query["body"]
        state = query["state"]
        if int(position["ordinal"]) > MAX_SCAN_PAGES:
            raise ArpError("INDEX_SCAN_LIMIT")
        plan = QueryPlan.build(body["query_text"], None if query["vector"] is None else query["vector"]["vector"])
        hybrid = body["mode"] == "HYBRID"
        rows = self._rows(snapshot, body, query["after_rowid"], int(body["page_rows"]))
        heaps: dict[str, list[list[Any]]] = {k: list(v) for k, v in state["heaps"].items()}
        groups = set(state["groups"])
        for row in rows:
            groups.add(str(row["group_id"]))
            key = stable_key(row)
            for channel, score in score_row(plan, row, hybrid=hybrid).items():
                heaps.setdefault(channel, []).append([row["chunk_id"], score, list(key)])
        top_k = int(body["channel_top_k"])
        for channel in heaps:
            heaps[channel] = sorted(heaps[channel], key=lambda x: (-x[1], tuple(x[2])))[:top_k]
        scanned = int(state["scanned"]) + len(rows)
        done = scanned >= int(body["snapshot_chunks"]) or not rows
        query["state"] = {"heaps": heaps, "scanned": scanned, "groups": sorted(groups), "pages": int(state["pages"]) + 1}
        query["after_rowid"] = rows[-1]["rowid"] if rows else query["after_rowid"]
        if done:
            # The page that completes the scan fixes the ranking (persisted with the
            # query) but is still a SCANNING page: empty items, progress, next cursor.
            # RESULTS pages only ever append the fixed ranking (§5.2).
            query["final"] = self._finalize(query, snapshot)
            query["phase"] = "RESULTS"
            scanned = min(scanned, int(body["snapshot_chunks"]))
        coverage = self._coverage(query, snapshot, phase="SCANNING", scanned=scanned)
        receipt = self._receipt(access, query, snapshot, coverage=coverage, searched=len(groups), elapsed=query["elapsed_ms"], total=None, offset=0, returned=0)
        page = {
            "schema_version": 2, "receipt": receipt, "items": [], "has_more": True, "next_cursor": None,
            "cursor_purpose": query["purpose"], "page_semantics": "PROGRESS", "body_bytes": 0,
        }
        if done:
            return page, {"phase": "RESULTS", "offset": 0}
        return page, {"phase": "SCANNING", "ordinal": int(position["ordinal"]) + 1}

    def _finalize(self, query: dict[str, Any], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        body = query["body"]
        heaps = query["state"]["heaps"]
        keys: dict[str, tuple] = {}
        channels: dict[str, list[tuple[str, float]]] = {}
        for channel, items in heaps.items():
            channels[channel] = [(str(i[0]), float(i[1])) for i in items]
            for i in items:
                keys[str(i[0])] = tuple(i[2])
        fused = rrf(channels, int(body["global_candidates"]), stable_keys=keys, channel_limit=int(body["channel_top_k"])) if channels else []
        # Resolve every fused chunk to its frozen row (same generation / upper_commit).
        results: list[dict[str, Any]] = []
        for rank, (chunk_id, score) in enumerate(fused, 1):
            row = self._chunk(int(snapshot["index_generation"]), chunk_id)
            results.append({**row, "score": score, "rank": rank})
        merged = _merge_spans(results)
        final: list[dict[str, Any]] = []
        for rank, item in enumerate(merged, 1):
            text = str(item["text_view"])
            final.append(
                {
                    "chunk_id": item["chunk_id"],
                    "group_id": item["group_id"],
                    "record_id": item["record_id"],
                    "utf8_start": int(item["utf8_start"]),
                    "utf8_end": int(item["utf8_end"]),
                    "source_hash": item["source_hash"],
                    "view_hash": item["view_hash"],
                    "provenance": item["provenance"],
                    "validity_epoch": int(item["validity_epoch"]),
                    "text_view": text,
                    "score": float(item["score"]),
                    "rank": rank,
                    "budget_charge": self._charge(text),
                    "seq_from": int(item["seq_from"]),
                    "seq_to": int(item["seq_to"]),
                    "members": list(item.get("members", [item["chunk_id"]])),
                }
            )
        return final

    def _chunk(self, index_generation: int, chunk_id: str) -> dict[str, Any]:
        row = self._p.connection.execute(
            "SELECT c.chunk_id,c.record_id,c.group_id,c.utf8_start,c.utf8_end,c.source_hash,c.view_hash,c.text_view,c.provenance,c.validity_epoch,s.seq_from,s.seq_to"
            " FROM session_chunks c JOIN group_sources s ON s.group_id=c.group_id AND s.source_hash=c.source_hash WHERE c.index_generation=? AND c.chunk_id=?",
            (index_generation, chunk_id),
        ).fetchone()
        if row is None:
            raise ArpError("SOURCE_UNAVAILABLE", "fused chunk vanished from the frozen generation")
        keys = ("chunk_id", "record_id", "group_id", "utf8_start", "utf8_end", "source_hash", "view_hash", "text_view", "provenance", "validity_epoch", "seq_from", "seq_to")
        return dict(zip(keys, row))

    def chunk_text(self, index_generation: int, chunk_id: str) -> str:
        return str(self._chunk(index_generation, chunk_id)["text_view"])

    def final_item(self, query_id: str, chunk_id: str) -> Mapping[str, Any]:
        final = self._query(query_id)["final"] or []
        for item in final:
            if item["chunk_id"] == chunk_id:
                return item
        raise ArpError("SOURCE_UNAVAILABLE", "chunk not in the final ranking")

    def _results_page(self, access: SessionAccess, query: dict[str, Any], snapshot: Mapping[str, Any], position: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if query["phase"] != "RESULTS" or query["final"] is None:
            raise ArpError("STATE_COMBINATION_INVALID", "results requested before the ranking is final")
        coverage = self._coverage(query, snapshot, phase="RESULTS", scanned=int(query["body"]["snapshot_chunks"]))
        return self._results_from(access, query, snapshot, coverage, offset=int(position["offset"]))

    def _results_from(self, access: SessionAccess, query: dict[str, Any], snapshot: Mapping[str, Any], coverage: dict[str, Any], *, offset: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
        final = query["final"] or []
        total = len(final)
        if offset > total or (total > 0 and offset == total):
            raise ArpError("CURSOR_UNKNOWN", "no results page at this offset")
        page_items = final[offset : offset + RESULT_PAGE_ITEMS]
        items = [
            {
                "chunk_id": item["chunk_id"],
                "source_group_ids": [item["group_id"]],
                "source_record_refs": [Pin("journal_record", item["record_id"], 0, item["source_hash"]).to_json()],
                "view_ref": Pin("artifact", f"chunk:{item['chunk_id']}", int(snapshot["index_generation"]), item["view_hash"]).to_json(),
                "budget_charge": int(item["budget_charge"]),
                "rank_ordinal": int(item["rank"]),
                "provenance": item["provenance"],
                "source_epoch": int(item["validity_epoch"]),
            }
            for item in page_items
        ]
        has_more = offset + len(items) < total
        receipt = self._receipt(access, query, snapshot, coverage=coverage, searched=int(coverage["indexed_groups"]), elapsed=query["elapsed_ms"], total=total, offset=offset, returned=len(items))
        page = {
            "schema_version": 2, "receipt": receipt, "items": items, "has_more": has_more, "next_cursor": None,
            "cursor_purpose": query["purpose"], "page_semantics": "APPEND_FINAL", "body_bytes": 0,
        }
        next_position = {"phase": "RESULTS", "offset": offset + len(items)} if has_more else None
        if next_position is not None and (offset + len(items)) // RESULT_PAGE_ITEMS >= MAX_RESULT_PAGES:
            raise ArpError("INDEX_SCAN_LIMIT", "result pages exceed the hard budget")
        return page, next_position

    def _advance_query_locked(self, connection: sqlite3.Connection, query: dict[str, Any], page: Mapping[str, Any], elapsed: int) -> None:
        phase = "RESULTS" if query["final"] is not None else "SCANNING"
        row = connection.execute("SELECT row_version, phase FROM search_queries WHERE query_id=?", (query["query_id"],)).fetchone()
        current = int(row[0])
        if str(row[1]) == "RESULTS":
            # The ranking is fixed; RESULTS pages only append it and never touch the query row.
            return
        updated = connection.execute(
            "UPDATE search_queries SET phase=?, after_rowid=?, heap_json=?, final_results_json=?, elapsed_ms=elapsed_ms+?, row_version=row_version+1"
            " WHERE query_id=? AND row_version=?",
            (
                phase, int(query["after_rowid"]), _dump(query["state"]),
                None if query["final"] is None else _dump(query["final"]), int(elapsed), query["query_id"], current,
            ),
        ).rowcount
        if updated != 1:
            raise ArpError("CURSOR_STALE", "query advanced concurrently")

    def _new_cursor_locked(self, connection: sqlite3.Connection, access: SessionAccess, *, query_id: str, snapshot: Mapping[str, Any], purpose: str, position: Mapping[str, Any], token: str | None = None) -> str:
        token = token or secrets.token_bytes(32).hex()
        connection.execute(
            "INSERT INTO cursor_pages(token_hash,query_id,purpose,snapshot_ref_json,owner_scope_hash,control_generation,authority_hash,expires_at_ms,state,position_json,page_json,page_hash,next_token_hash)"
            " VALUES (?,?,?,?,?,?,?,?,'PENDING',?,NULL,NULL,NULL)",
            (
                hashlib.sha256(token.encode("utf-8")).hexdigest(), query_id, purpose,
                _dump(Pin("index_snapshot", snapshot["snapshot_id"], int(snapshot["index_generation"]), digest({"snapshot": snapshot["snapshot_id"], "upper": snapshot["upper_commit"]})).to_json()),
                access.owner_scope_hash, access.control_generation, access.authority_readset_hash, self._clock_ms() + self._ttl, _dump(position),
            ),
        )
        return token

    def _token_for(self, page: Mapping[str, Any]) -> str | None:
        return page.get("next_cursor")

    def pages_for_query(self, query_id: str) -> tuple[Mapping[str, Any], ...]:
        """Materialised pages of one query in cursor-chain order (first cursor first)."""

        rows = self._p.connection.execute(
            "SELECT token_hash,next_token_hash,page_json FROM cursor_pages WHERE query_id=? AND state='MATERIALIZED'", (query_id,)
        ).fetchall()
        by_hash = {str(r[0]): (None if r[1] is None else str(r[1]), _load(r[2])) for r in rows}
        targets = {n for n, _ in by_hash.values() if n is not None}
        heads = [h for h in by_hash if h not in targets]
        if len(heads) != 1:
            return () if not rows else tuple(p for _, p in by_hash.values())
        ordered: list[Mapping[str, Any]] = []
        cursor: str | None = heads[0]
        while cursor is not None and cursor in by_hash:
            nxt, page = by_hash[cursor]
            ordered.append(page)
            cursor = nxt
        return tuple(ordered)

    def page_pin(self, page: Mapping[str, Any], ordinal: int) -> Pin:
        return Pin("artifact", f"{page['receipt']['index_snapshot_id']}:page:{ordinal}", ordinal, digest(page))

    # ---- history read (C6) --------------------------------------------------------

    def read_history(
        self,
        access: SessionAccess,
        exec_connection: sqlite3.Connection,
        request: Mapping[str, Any],
        *,
        purpose: str,
        journal_highwater: int,
        source_snapshot_hash: str,
    ) -> Mapping[str, Any]:
        """Exact Journal read in seq order with UTF-8 boundary slicing (C6)."""

        value = check("HistoryReadRequest", dict(request))
        if purpose not in ("MODEL_READ", "MANAGEMENT_READ"):
            raise ArpError("ENUM", field_path="purpose")
        if value["seq_to"] > journal_highwater:
            raise ArpError("HISTORY_BYTE_RANGE_INVALID", "seq_to beyond the journal highwater")
        max_bytes = int(value["max_bytes"])
        position = {"seq": int(value["seq_from"]), "offset": 0}
        if value["cursor"] is not None:
            token_hash = hashlib.sha256(str(value["cursor"]).encode("utf-8")).hexdigest()
            row = self._p.connection.execute(
                "SELECT purpose,owner_scope_hash,control_generation,authority_hash,expires_at_ms,state,position_json,page_json FROM cursor_pages WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                raise ArpError("CURSOR_UNKNOWN")
            if str(row[0]) != purpose or str(row[1]) != access.owner_scope_hash:
                raise ArpError("CURSOR_SCOPE_MISMATCH")
            if int(row[2]) != access.control_generation or str(row[3]) != access.authority_readset_hash:
                raise ArpError("CURSOR_STALE")
            if self._clock_ms() >= int(row[4]):
                raise ArpError("CURSOR_EXPIRED")
            if str(row[5]) == "MATERIALIZED":
                return _load(row[7])
            position = _load(row[6])
        records = history.read_records(exec_connection, access.agent_id, from_seq=position["seq"], to_seq=int(value["seq_to"]))
        items: list[dict[str, Any]] = []
        hidden: list[dict[str, int]] = []
        texts: dict[int, str] = {}
        offset = int(position["offset"])
        next_position: dict[str, int] | None = None

        def slice_item(record: Any, text: str, start: int, max_bytes_here: int) -> tuple[dict[str, Any], bool]:
            raw = text.encode("utf-8")
            if not raw:
                piece, end, complete = "", 0, True
            else:
                piece, end, complete = utf8_slice(text, start, max(4, max_bytes_here))
            return (
                {
                    "record_ref": Pin("journal_record", record.record_id, record.seq, record.content_hash).to_json(),
                    "seq": record.seq,
                    "utf8_start": start,
                    "utf8_end": end,
                    "total_utf8_bytes": len(raw),
                    "text": piece,
                    "slice_hash": hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                    "record_complete": start == 0 and end == len(raw),
                    "provenance": PROVENANCE_OF_KIND[record.kind],
                },
                complete,
            )

        # Greedy fill with a generous budget; the exact canonical size is enforced below.
        used = 0
        by_seq: dict[int, Any] = {}
        for record in records:
            if record.visibility != "context":
                hidden.append({"start": record.seq, "end": record.seq})
                continue
            text = _text_of(record)
            texts[record.seq] = text
            by_seq[record.seq] = record
            while True:
                if len(items) >= 64 or used >= max_bytes:
                    next_position = {"seq": record.seq, "offset": offset}
                    break
                item, complete = slice_item(record, text, offset, max_bytes - used)
                items.append(item)
                used += len(item["text"].encode("utf-8"))
                if complete:
                    offset = 0
                    break
                offset = item["utf8_end"]
            if next_position is not None:
                break

        def assemble(page_items: list[dict[str, Any]], more: dict[str, int] | None) -> dict[str, Any]:
            covered = [i["seq"] for i in page_items]
            return _with_body_bytes(
                {
                    "schema_version": 1,
                    "session_ref": access.session_ref.to_json(),
                    "source_snapshot_hash": source_snapshot_hash,
                    "journal_highwater": journal_highwater,
                    "requested_seq_from": int(value["seq_from"]),
                    "requested_seq_to": int(value["seq_to"]),
                    "covered_seq_from": min(covered) if covered else None,
                    "covered_seq_to": max(covered) if covered else None,
                    "items": page_items,
                    "has_more": more is not None,
                    "next_cursor": "0" * 64 if more is not None else None,  # placeholder of the real token's size
                    "cursor_purpose": purpose,
                    "hidden_seq_ranges": hidden,
                    "authority_readset_hash": access.authority_readset_hash,
                    "body_bytes": 0,
                }
            )

        # Enforce max_bytes on the exact canonical body: drop whole trailing slices first,
        # then shrink the single remaining slice on code point boundaries.
        while True:
            page = assemble(items, next_position)
            if page["body_bytes"] <= max_bytes:
                break
            if len(items) > 1:
                last = items.pop()
                next_position = {"seq": int(last["seq"]), "offset": int(last["utf8_start"])}
                continue
            if not items:
                raise ArpError("ITEM_TOO_LARGE", "max_bytes cannot hold the page metadata")
            only = items[0]
            length = int(only["utf8_end"]) - int(only["utf8_start"])
            if length <= 4:
                raise ArpError("ITEM_TOO_LARGE", "max_bytes cannot hold one code point with its metadata")
            shrunk, complete = slice_item(by_seq[int(only["seq"])], texts[int(only["seq"])], int(only["utf8_start"]), length // 2)
            items[0] = shrunk
            next_position = None if complete else {"seq": int(only["seq"]), "offset": int(shrunk["utf8_end"])}
        if not items and records and next_position is not None:
            raise ArpError("ITEM_TOO_LARGE", "max_bytes cannot hold one code point with its metadata")
        page["next_cursor"] = None
        with self._p.transaction() as connection:
            if next_position is not None:
                token = secrets.token_bytes(32).hex()
                connection.execute(
                    "INSERT INTO cursor_pages(token_hash,query_id,purpose,snapshot_ref_json,owner_scope_hash,control_generation,authority_hash,expires_at_ms,state,position_json,page_json,page_hash,next_token_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,'PENDING',?,NULL,NULL,NULL)",
                    (
                        hashlib.sha256(token.encode("utf-8")).hexdigest(), f"history:{access.session_id}:{value['seq_from']}:{value['seq_to']}", purpose,
                        _dump({"kind": "history", "highwater": journal_highwater}), access.owner_scope_hash, access.control_generation,
                        access.authority_readset_hash, self._clock_ms() + self._ttl, _dump(next_position),
                    ),
                )
                page["next_cursor"] = token
            page = _with_body_bytes(page)
            if page["body_bytes"] > max_bytes:
                raise ArpError("ITEM_TOO_LARGE", "history page exceeds max_bytes")
            result = check("HistoryReadPage", page)
            if value["cursor"] is not None:
                connection.execute(
                    "UPDATE cursor_pages SET state='MATERIALIZED', page_json=?, page_hash=?, next_token_hash=? WHERE token_hash=? AND state='PENDING'",
                    (
                        _dump(result), digest(result),
                        None if page["next_cursor"] is None else hashlib.sha256(page["next_cursor"].encode("utf-8")).hexdigest(),
                        hashlib.sha256(str(value["cursor"]).encode("utf-8")).hexdigest(),
                    ),
                )
        return result


# ---- helpers ------------------------------------------------------------------------


def _merge_spans(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """C3 step 9: merge overlapping / adjacent spans of one record with equal provenance."""

    by_rank = sorted(results, key=lambda r: r["rank"])
    kept: list[dict[str, Any]] = []
    for item in by_rank:
        merged = False
        for existing in kept:
            if (
                existing["record_id"] == item["record_id"]
                and existing["source_hash"] == item["source_hash"]
                and existing["provenance"] == item["provenance"]
                and not (item["utf8_end"] < existing["utf8_start"] or item["utf8_start"] > existing["utf8_end"])
            ):
                start = min(existing["utf8_start"], item["utf8_start"])
                end = max(existing["utf8_end"], item["utf8_end"])
                if end - start > MAX_MERGED_BYTES:
                    continue
                # Rebuild the merged view from the two views' byte ranges (same record text).
                a = existing["text_view"].encode("utf-8")
                b = item["text_view"].encode("utf-8")
                if item["utf8_start"] <= existing["utf8_start"]:
                    head, tail, head_start, tail_start = b, a, item["utf8_start"], existing["utf8_start"]
                else:
                    head, tail, head_start, tail_start = a, b, existing["utf8_start"], item["utf8_start"]
                overlap = head_start + len(head) - tail_start
                combined = head + tail[max(0, overlap):]
                try:
                    text = combined.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                existing.update(utf8_start=start, utf8_end=end, text_view=text, score=max(existing["score"], item["score"]))
                existing.setdefault("members", [existing["chunk_id"]]).append(item["chunk_id"])
                merged = True
                break
        if not merged:
            kept.append(dict(item))
    return kept


def _with_body_bytes(page: dict[str, Any]) -> dict[str, Any]:
    size = 0
    for _ in range(4):
        page = {**page, "body_bytes": size}
        measured = len(canonical(page))
        if measured == size:
            return page
        size = measured
    return {**page, "body_bytes": size}


def _text_of(record: Any) -> str:
    message = thaw_json(record.message_json)
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _dump(value: Any) -> str:
    return canonical(value).decode("utf-8")


def _load(text: str) -> Any:
    import json

    return json.loads(text)


__all__ = (
    "ALGORITHM_ID",
    "PROVENANCE_OF_KIND",
    "QueryPlan",
    "RESULT_PAGE_ITEMS",
    "SessionAccess",
    "SessionSearchService",
    "VECTOR_FLOOR",
    "exact_targets",
    "normalize_words",
    "score_row",
    "stable_key",
    "trigrams",
)
