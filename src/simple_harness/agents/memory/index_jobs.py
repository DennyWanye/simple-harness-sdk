# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Durable index jobs: Journal record → FTS row (same transaction) and vector (job).

Vectors are computed off the event loop and outside any transaction (BA-v1.0
§6.3 [W03]); a failed embedding leaves the job in ``error`` with a code and the
original record untouched (BA25: never delete the only body).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from simple_harness.execution.base_agent import AgentJournalRecord
from simple_harness.execution.sqlite.uow import SqliteExecutionUnitOfWork
from simple_harness.execution.uow import UnitOfWorkConflict

from .embedding import EmbeddingPort, EmbeddingUnavailable
from .retrieval import record_text


def _text_of_json(message_json: str) -> str:
    import json

    try:
        message = json.loads(message_json)
    except ValueError:
        return ""
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else (json.dumps(content) if content else "")


INDEXABLE_KINDS = ("user_input", "assistant", "tool_result")


class SessionIndexer:
    def __init__(
        self,
        uow: SqliteExecutionUnitOfWork,
        *,
        embedding: EmbeddingPort | None,
        fts_available: bool,
        owner: str,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 60.0,
        backfill_interval: float = 5.0,
    ) -> None:
        self._uow = uow
        self._embedding = embedding
        self._fts = fts_available
        self._owner = owner
        self._clock = clock
        self._lease = lease_seconds
        self._lock = asyncio.Lock()
        self._backfill_interval = backfill_interval
        self._last_backfill = float("-inf")
        self.embedded = 0
        self.failed = 0

    def request_backfill(self) -> None:
        """Force the next ``run_once`` to scan for missing FTS rows / vectors."""

        self._last_backfill = float("-inf")

    def on_records(self, records: tuple[AgentJournalRecord, ...]) -> None:
        """Synchronous, cheap: FTS rows + pending vector jobs for new Journal rows."""

        for record in records:
            if record.kind not in INDEXABLE_KINDS:
                continue
            text = record_text(record)
            if self._fts and text:
                self._uow.index_agent_journal_fts(
                    agent_id=record.agent_id, seq=record.seq, text=text
                )
            if self._embedding is not None and text:
                self._uow.enqueue_agent_index_job(
                    agent_id=record.agent_id,
                    record_seq=record.seq,
                    source_hash=record.content_hash,
                    embedding_fingerprint=self._embedding.fingerprint,
                    now=self._clock(),
                )

    async def run_once(self, *, limit: int = 32) -> int:
        """Claim pending jobs, embed in a thread, store vectors; returns jobs settled."""

        async with self._lock:
            return await self._run_once_locked(limit=limit)

    async def _run_once_locked(self, *, limit: int) -> int:
        now = self._clock()
        backfill_due = now - self._last_backfill >= self._backfill_interval
        if self._fts and backfill_due:
            # FTS backfill (review S4-02): rows written by a build without FTS5, or
            # committed before a crash of the derived write, get their rows now.
            for agent_id, seq, message_json in self._uow.agent_journal_rows_missing_fts(
                limit=limit
            ):
                text = _text_of_json(message_json)
                if text:
                    self._uow.index_agent_journal_fts(agent_id=agent_id, seq=seq, text=text)
        if self._embedding is None:
            if backfill_due:
                self._last_backfill = now
            return 0
        if backfill_due:
            # Backfill (BA26): records that predate this embedding generation get jobs.
            self._last_backfill = now
            for agent_id, seq, content_hash in self._uow.agent_journal_seqs_without_vectors(
                embedding_fingerprint=self._embedding.fingerprint, limit=limit
            ):
                self._uow.enqueue_agent_index_job(
                    agent_id=agent_id,
                    record_seq=seq,
                    source_hash=content_hash,
                    embedding_fingerprint=self._embedding.fingerprint,
                    now=self._clock(),
                )
        jobs = self._uow.claim_agent_index_jobs(
            owner=self._owner,
            embedding_fingerprint=self._embedding.fingerprint,
            now=self._clock(),
            lease_seconds=self._lease,
            limit=limit,
        )
        if not jobs:
            return 0
        texts: list[str] = []
        for job in jobs:
            records = self._uow.read_agent_journal(
                job.agent_id, from_seq=job.record_seq, to_seq=job.record_seq
            )
            texts.append(record_text(records[0]) if records else "")
        try:
            vectors = await asyncio.to_thread(self._embedding.embed, texts)
            if len(vectors) != len(jobs):
                raise EmbeddingUnavailable("embedding_batch_mismatch")
        except Exception as error:  # noqa: BLE001 - every failure is a visible job error
            code = getattr(error, "code", "embedding_unavailable")
            for job in jobs:
                self._settle_quietly(job.job_id, "error", str(code))
            self.failed += len(jobs)
            return len(jobs)
        for job, vector in zip(jobs, vectors):
            if len(vector) != self._embedding.dim:
                # BA26 by data, not by naming: a width that differs from the declared
                # dimension is an error job, never a silent zero-score vector.
                self._settle_quietly(job.job_id, "error", "embedding_dim_mismatch")
                self.failed += 1
                continue
            self._uow.store_agent_vector(
                agent_id=job.agent_id,
                record_seq=job.record_seq,
                source_hash=job.source_hash,
                embedding_fingerprint=job.embedding_fingerprint,
                vector=vector,
                now=self._clock(),
            )
            self._settle_quietly(job.job_id, "done", None)
        self.embedded += len(jobs)
        return len(jobs)

    def _settle_quietly(self, job_id: str, state: str, error_code: str | None) -> None:
        """A lost lease (another drainer re-claimed the job) is benign: the vector
        store is idempotent, so the other owner's settle wins (review S4-06)."""

        try:
            self._uow.settle_agent_index_job(
                job_id=job_id,
                owner=self._owner,
                state=state,
                error_code=error_code,
                now=self._clock(),
            )
        except UnitOfWorkConflict:
            return

    async def drain(self, *, max_rounds: int = 100) -> int:
        total = 0
        for _ in range(max_rounds):
            settled = await self.run_once()
            if settled == 0:
                break
            total += settled
        return total


__all__ = ("INDEXABLE_KINDS", "SessionIndexer")
