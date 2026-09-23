# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""INDEX jobs: closed groups → chunks (+ vectors) → partition batch → central ACK (§6, J2).

``SessionIndexCoordinator.enqueue_closed_groups_locked`` records one INDEX job per
closed indexable group (semantic key = generation / group / source / view /
chunker / embedding) in the caller's execution transaction.  ``process`` runs one
job: it re-reads the exact records named by the payload (their content hashes
must reproduce the group's source hash), chunks them by real token counts, embeds
outside every lock with one call key per job (``job_id/1/input_hash``; a persisted
successful call is reused, never repeated), materialises one immutable batch under
the FileGuard, then ACKs the job centrally (``DONE`` + ``RuntimeJobChanged``) and
publishes the generation pointer on first use (``RuntimeIndexGenerationPublished``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from simple_harness.execution.base_agent import AgentJournalRecord
from simple_harness.execution.sqlite.base_agent import history

from . import embedding_call, store
from .codec import check
from .context.groups import PROVENANCE_OF, GroupSnapshot, GroupView
from .errors import ArpError
from .partition import NO_EMBEDDING_FINGERPRINT, FileGuard, GenerationRow, Partition, open_partition
from .pins import Pin
from .ports import RootIdentity
from .search import _text_of
from .strict import digest

CHUNKER_VERSION = "arp-utf8-token-window:v1"
VIEW_POLICY_ID = {"view": "context-visible-records:v1"}
VIEW_POLICY_HASH = digest(VIEW_POLICY_ID)
MAX_CHUNK_BYTES = 16384
DEFAULT_JOB_DEADLINE_MS = 5 * 60 * 1000
MAX_EMBED_ATTEMPTS = 3
LEASE_MS = 30_000


def chunker_fingerprint(policy: Mapping[str, Any], tokenizer_fingerprint: str) -> str:
    return digest(
        {
            "chunker": CHUNKER_VERSION,
            "chunk_tokens": int(policy["embedding_chunk_tokens"]),
            "overlap_tokens": int(policy["embedding_overlap_tokens"]),
            "tokenizer": tokenizer_fingerprint,
        }
    )


def chunk_text(
    text: str, *, chunk_tokens: int, overlap_tokens: int, count: Callable[[str], int]
) -> list[tuple[int, int, str]]:
    """Deterministic windows ``(utf8_start, utf8_end, text)`` by token count and ≤16KiB.

    Windows advance by ``chunk_tokens - overlap_tokens`` worth of characters; each
    window is shrunk until both the token count and the byte size fit.  Boundaries
    are code points, so byte offsets are always valid UTF-8 boundaries.
    """

    if not text.strip():
        return []
    if chunk_tokens < 1 or overlap_tokens < 0 or overlap_tokens >= chunk_tokens:
        raise ArpError("POLICY_CONFLICT", "chunk/overlap tokens invalid")
    chunks: list[tuple[int, int, str]] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + chunk_tokens * 4)
        while end > start + 1:
            piece = text[start:end]
            if len(piece.encode("utf-8")) <= MAX_CHUNK_BYTES and count(piece) <= chunk_tokens:
                break
            end = start + max(1, int((end - start) * 0.8))
        piece = text[start:end]
        byte_start = len(text[:start].encode("utf-8"))
        byte_end = byte_start + len(piece.encode("utf-8"))
        chunks.append((byte_start, byte_end, piece))
        if end >= length:
            break
        # Overlap: step back by the overlap share of the window, but always advance.
        step = max(1, int((end - start) * (chunk_tokens - overlap_tokens) / chunk_tokens))
        start = start + step
    return chunks


@dataclass(slots=True)
class SessionIndexState:
    partition: Partition
    guard: FileGuard
    generation: GenerationRow


@dataclass(slots=True)
class SessionIndexCoordinator:
    uow: Any
    root: RootIdentity
    policy: Mapping[str, Any]
    tokenizer_fingerprint: str
    count: Callable[[str], int]
    clock_ms: Callable[[], int]
    clock: Callable[[], float]
    owner_id: str
    embedding: Any | None = None
    embedding_resource_ref: Pin | None = None
    fault: Callable[[str], None] | None = None
    # Other job kinds claimed by the same fair pass (RP-D1: PURGE); a kind without a
    # handler is handed back untouched.
    handlers: dict[str, Callable[[store.JobRow], Mapping[str, Any]]] = field(default_factory=dict)
    _states: dict[str, SessionIndexState] = field(default_factory=dict)

    # ---- partition / generation -----------------------------------------------------

    @property
    def embedding_fingerprint(self) -> str:
        return NO_EMBEDDING_FINGERPRINT if self.embedding is None else digest(str(self.embedding.fingerprint))

    @property
    def chunker(self) -> str:
        return chunker_fingerprint(self.policy, self.tokenizer_fingerprint)

    def state_for(self, session: store.SessionRow) -> SessionIndexState:
        state = self._states.get(session.session_id)
        if state is not None:
            return state
        protocol = store.read_protocol(self.uow.database.connection, session.agent_id)
        if protocol is None:
            raise ArpError("RUNTIME_CREATION_MARKER_MISSING", "session protocol row missing")
        guard = FileGuard(self.root, session.session_id)
        with guard.held():
            partition = open_partition(
                self.root.resolve_relative(session.relative_directory),
                session_id=session.session_id,
                agent_id=session.agent_id,
                root=self.root,
                control_generation=session.generation,
                marker_hash=protocol.marker_hash,
                source_from=session.journal_seq_from,
            )
            with partition.transaction() as connection:
                generation = partition.ensure_generation_locked(
                    connection,
                    view_policy_hash=VIEW_POLICY_HASH,
                    chunker_fingerprint=self.chunker,
                    embedding_fingerprint=self.embedding_fingerprint,
                )
        state = SessionIndexState(partition, guard, generation)
        self._states[session.session_id] = state
        return state

    def close(self) -> None:
        for state in self._states.values():
            state.partition.close()
        self._states.clear()

    def release(self, session_id: str) -> None:
        """Drop one Session's open partition (RP-D1: before its directory is renamed)."""

        state = self._states.pop(session_id, None)
        if state is not None:
            state.partition.close()

    # ---- enqueue (same execution transaction as the caller) ---------------------------

    def job_key(self, generation: GenerationRow, group: GroupView) -> str:
        return digest(
            {
                "generation": generation.index_generation,
                "group": group.group_id,
                "source": group.source_hash,
                "view": VIEW_POLICY_HASH,
                "chunker": generation.chunker_fingerprint,
                "embedding": generation.embedding_fingerprint,
            }
        )

    def enqueue_closed_groups_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session: store.SessionRow,
        snapshot: GroupSnapshot,
        generation: GenerationRow,
        authority_ref: Pin,
        turn_ref: Pin,
    ) -> tuple[store.JobRow, ...]:
        if self.policy.get("index_mode") != "ON_CLOSED_GROUP":
            return ()
        jobs: list[store.JobRow] = []
        now_ms = self.clock_ms()
        for group in snapshot.groups:
            if not group.indexable:
                continue
            key = self.job_key(generation, group)
            if store.read_job_by_key(connection, session.session_id, "INDEX", key) is not None:
                continue
            payload = {
                "schema_version": 1,
                "session_ref": session.pin.to_json(),
                "control_generation": session.generation,
                "source_refs": [ref.to_json() for ref in group.record_refs],
                "budget_owner_ref": turn_ref.to_json(),
                "authority_ref": authority_ref.to_json(),
                "deadline_ms": now_ms + DEFAULT_JOB_DEADLINE_MS,
                "index_generation": generation.index_generation,
                "group_ref": Pin("journal_group", group.group_id, group.seq_to, group.source_hash).to_json(),
                "chunker_ref": Pin("policy", "chunker", 1, generation.chunker_fingerprint).to_json(),
                "embedding_deployment_ref": (
                    self.embedding_resource_ref or Pin("deployment", "embedding:none", 0, NO_EMBEDDING_FINGERPRINT)
                ).to_json(),
                "view_policy_ref": Pin("policy", "view", 1, VIEW_POLICY_HASH).to_json(),
                "source_highwater": group.seq_to,
            }
            jobs.append(
                store.put_job_locked(
                    connection,
                    job_id=f"index-{digest({'session': session.session_id, 'key': key})[:32]}",
                    session_id=session.session_id,
                    kind="INDEX",
                    semantic_key=key,
                    payload=payload,
                    generation=session.generation,
                    next_at_ms=now_ms,
                    deadline_ms=now_ms + DEFAULT_JOB_DEADLINE_MS,
                )
            )
        return tuple(jobs)

    # ---- worker -------------------------------------------------------------------------

    def process_due(self, *, limit: int = 8, session_id: str | None = None) -> tuple[Mapping[str, Any], ...]:
        """Claim ≤limit due INDEX jobs (≤2 per Session, optionally one Session only) and process
        them one by one. A failing job is recorded on that job (BLOCKED, or handed back when the
        Session file is busy); it never escapes to the caller's Turn."""

        now_ms = self.clock_ms()
        with self.uow.database.transaction() as connection:
            claimed = store.claim_jobs_locked(
                connection, owner_id=self.owner_id, now_ms=now_ms, lease_ms=LEASE_MS, limit=limit, per_session=2, session_id=session_id
            )
        results = []
        for job in claimed:
            if job.kind != "INDEX":
                handler = self.handlers.get(job.kind)
                if handler is None:
                    # No consumer assembled for this kind: give the lease back untouched.
                    with self.uow.database.transaction() as connection:
                        store.complete_job_locked(connection, job, state="PENDING", owner_id=self.owner_id, next_at_ms=now_ms + LEASE_MS)
                    continue
                results.append(handler(job))
                continue
            results.append(self._process_guarded(job, now_ms))
        return tuple(results)

    def _process_guarded(self, job: store.JobRow, now_ms: int) -> Mapping[str, Any]:
        try:
            return self.process(job)
        except ArpError as error:
            if error.code == "FILE_BUSY":
                with self.uow.database.transaction() as connection:
                    current = store.read_job(connection, job.job_id) or job
                    store.complete_job_locked(connection, current, state="PENDING", owner_id=self.owner_id, next_at_ms=now_ms + LEASE_MS)
                return {"job_id": job.job_id, "state": "PENDING", "reason": "FILE_BUSY"}
            return self._block_job(job, f"{error.code}")
        except Exception as error:  # noqa: BLE001 - a poison job blocks itself, not other Turns
            return self._block_job(job, f"INTERNAL:{type(error).__name__}")

    def _block_job(self, job: store.JobRow, reason: str) -> Mapping[str, Any]:
        session = store.read_session(self.uow.database.connection, job.session_id)
        if session is None:
            with self.uow.database.transaction() as connection:
                current = store.read_job(connection, job.job_id) or job
                store.complete_job_locked(connection, current, state="BLOCKED", owner_id=self.owner_id)
            return {"job_id": job.job_id, "state": "BLOCKED", "reason": reason}
        return self._finish(job, "BLOCKED", reason=reason[:256], session=session)

    def process(self, job: store.JobRow) -> Mapping[str, Any]:
        connection = self.uow.database.connection
        payload = job.payload
        session = store.read_session(connection, job.session_id)
        if session is None:
            raise ArpError("SESSION_NOT_ACTIVE", "job session vanished")
        if session.state != "ACTIVE" or session.generation != int(payload["control_generation"]):
            return self._finish(job, "CANCELLED", reason="SESSION_NOT_ACTIVE", session=session)
        group_ref = Pin.from_json(payload["group_ref"])
        record_ids = [Pin.from_json(r).id for r in payload["source_refs"]]
        records = _records_by_id(connection, session.agent_id, record_ids)
        source_hash = digest([[r.record_id, r.content_hash] for r in records])
        if len(records) != len(record_ids) or source_hash != group_ref.content_hash:
            return self._finish(job, "BLOCKED", reason="GROUP_HASH_CONFLICT", session=session)
        state = self.state_for(session)
        generation = state.generation
        if generation.index_generation != int(payload["index_generation"]):
            return self._finish(job, "BLOCKED", reason="GENERATION_STALE", session=session)
        # Chunks from the visible records only (the view policy); journal_only bodies
        # are never indexed as model-visible text.
        chunks: list[dict[str, Any]] = []
        for record in records:
            if record.visibility != "context":
                continue
            for start, end, piece in chunk_text(
                _text_of(record),
                chunk_tokens=int(self.policy["embedding_chunk_tokens"]),
                overlap_tokens=int(self.policy["embedding_overlap_tokens"]),
                count=self.count,
            ):
                chunk_id = "chunk-" + digest(
                    {"gen": generation.index_generation, "record": record.record_id, "start": start, "end": end, "source": source_hash}
                )[:32]
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "record_id": record.record_id,
                        "utf8_start": start,
                        "utf8_end": end,
                        "text_view": piece,
                        "provenance": PROVENANCE_OF[record.kind],
                        "validity_epoch": 0,
                    }
                )
        view_hash = digest([[r.record_id, r.content_hash] for r in records if r.visibility == "context"])
        vectors: dict[str, Sequence[float]] | None = None
        embedding_ref: Pin | None = None
        invocation_refs: list[Pin] = []
        if self.embedding is not None and chunks and generation.embedding_fingerprint != NO_EMBEDDING_FINGERPRINT:
            texts = [str(c["text_view"]) for c in chunks]
            deployment = self.embedding_resource_ref or Pin("deployment", "embedding:local", 1, self.embedding_fingerprint)
            receipt: Mapping[str, Any] | None = None
            for attempt in range(1, MAX_EMBED_ATTEMPTS + 1):
                # J2: a call whose result was lost is recorded UNKNOWN and never re-issued under
                # its own key; the next attempt gets its own ordinal (and its own intent).
                call_key = f"{job.job_id}/{attempt}/{digest(texts)}"
                receipt = embedding_call.perform_call(
                    self.uow, run_id=session.agent_id, call_key=call_key, purpose="SESSION_INDEX", texts=texts, port=self.embedding,
                    deployment_ref=deployment, clock=self.clock, clock_ms=self.clock_ms, fault=self.fault, fault_name="index.before_embed",
                )
                invocation_refs.append(Pin.from_json(receipt["invocation_ref"]))
                if receipt["status"] != "UNKNOWN":
                    break
            assert receipt is not None
            if receipt["status"] != "SUCCEEDED":
                return self._finish(job, "BLOCKED", reason="EMBEDDING_UNAVAILABLE", session=session, invocations=invocation_refs)
            output = receipt["output"]
            vectors = {str(c["chunk_id"]): output[i] for i, c in enumerate(chunks)}
            embedding_ref = embedding_call.receipt_pin(call_key, receipt)
        self._fault_point("index.before_materialize")
        seq_from = min(r.seq for r in records)
        seq_to = max(r.seq for r in records)
        with state.guard.held():
            if state.partition.is_materialized(generation.index_generation, group_ref.id):
                batch = {"kind": "partition-materialization-v1", "replayed": True, "group_id": group_ref.id}
            else:
                with state.partition.transaction() as pconn:
                    batch = dict(
                        state.partition.materialize_group_locked(
                            pconn,
                            generation=generation,
                            group_id=group_ref.id,
                            source_hash=source_hash,
                            seq_from=seq_from,
                            seq_to=seq_to,
                            closed_receipt_ref=Pin("receipt", f"{group_ref.id}:closed", 0, source_hash),
                            view_hash=view_hash,
                            chunks=chunks,
                            vectors=vectors,
                            embedding_receipt_ref=embedding_ref,
                        )
                    )
        self._fault_point("index.before_ack")
        batch_ref = Pin("receipt", f"batch:{job.job_id}", 0, digest(batch))
        return self._finish(job, "DONE", session=session, invocations=invocation_refs, result_refs=[batch_ref], generation=generation, source_highwater=seq_to)

    def _fault_point(self, name: str) -> None:
        if self.fault is not None:
            self.fault(name)

    def _finish(
        self,
        job: store.JobRow,
        state: str,
        *,
        session: store.SessionRow,
        reason: str | None = None,
        invocations: Sequence[Pin] = (),
        result_refs: Sequence[Pin] = (),
        generation: GenerationRow | None = None,
        source_highwater: int = 0,
    ) -> Mapping[str, Any]:
        with self.uow.database.transaction() as connection:
            value, result_ref = finish_job_locked(
                connection, job, state, session=session, owner_id=self.owner_id, clock=self.clock, clock_ms=self.clock_ms,
                result_kind={"DONE": "INDEX_COMMITTED", "BLOCKED": "BLOCKED", "CANCELLED": "CANCELLED"}[state],
                reason=reason, invocations=invocations, result_refs=result_refs,
            )
            if state == "DONE" and generation is not None:
                self._publish_locked(connection, session, generation, source_highwater, result_ref)
        return value

    def _publish_locked(self, connection: sqlite3.Connection, session: store.SessionRow, generation: GenerationRow, source_highwater: int, receipt: Pin) -> None:
        state = self._states[session.session_id]
        active = store.active_index_publication(connection, session.session_id)
        if active is not None and active.index_generation == generation.index_generation:
            return
        publication = store.publish_index_generation_locked(
            connection,
            session_id=session.session_id,
            index_generation=generation.index_generation,
            partition_id=state.partition.partition_id,
            generation_manifest_hash=generation.generation_manifest_hash or digest(generation.manifest()),
            source_highwater=source_highwater,
            publish_receipt_ref=receipt,
        )
        store.append_runtime_event_locked(
            connection,
            run_id=session.agent_id,
            event_type="RuntimeIndexGenerationPublished",
            body={"session_ref": session.pin.to_json(), "index_ref": publication.pin.to_json(), "source_receipt_ref": receipt.to_json()},
            source_receipt_ref=receipt,
            dedupe_key=f"{session.session_id}:publish:{generation.index_generation}",
            now=self.clock(),
        )


def finish_job_locked(
    connection: sqlite3.Connection,
    job: store.JobRow,
    state: str,
    *,
    session: store.SessionRow,
    owner_id: str,
    clock: Callable[[], float],
    clock_ms: Callable[[], int],
    result_kind: str,
    reason: str | None = None,
    invocations: Sequence[Pin] = (),
    result_refs: Sequence[Pin] = (),
    next_at_ms: int | None = None,
) -> tuple[Mapping[str, Any], Pin]:
    """Central ACK of one job (J2): JobResult receipt + row transition + ``RuntimeJobChanged``.
    ``PENDING`` hands the lease back (no result) and only records the event."""

    if state == "PENDING":
        current = store.read_job(connection, job.job_id) or job
        after = store.complete_job_locked(connection, current, state="PENDING", owner_id=owner_id, next_at_ms=next_at_ms)
        stub = {"job_id": job.job_id, "state": "PENDING", "reason": reason}
        return stub, Pin("receipt", f"job:{job.job_id}", after.row_version, digest(stub))
    result = {
        "schema_version": 1,
        "job_id": job.job_id,
        "payload_hash": job.payload_hash,
        "result_kind": result_kind,
        "original_invocation_refs": [p.to_json() for p in invocations],
        "result_refs": [p.to_json() for p in result_refs],
        "source_generation": session.generation,
        "observed_at_ms": clock_ms(),
        "reason_code": reason,
    }
    value = check("JobResult", result)
    result_ref = Pin("receipt", f"job:{job.job_id}", job.row_version, digest(value))
    current = store.read_job(connection, job.job_id) or job
    # The JobResult body itself is kept as an original receipt so a BLOCKED reason
    # can be read back, not only its hash.
    store.append_original_receipt_locked(connection, run_id=session.agent_id, kind="job_result", receipt_key=f"{job.job_id}:{job.row_version}", body=value, now=clock())
    after = store.complete_job_locked(connection, current, state=state, owner_id=owner_id, result_receipt_ref=result_ref, next_at_ms=next_at_ms)
    store.append_runtime_event_locked(
        connection,
        run_id=session.agent_id,
        event_type="RuntimeJobChanged",
        body={
            "session_ref": session.pin.to_json(),
            "job_id": job.job_id,
            "row_version": after.row_version,
            "kind": job.kind,
            "state": after.state,
            "source_receipt_ref": result_ref.to_json(),
        },
        source_receipt_ref=result_ref,
        dedupe_key=f"{job.job_id}:{after.row_version}",
        now=clock(),
    )
    return value, result_ref


def _records_by_id(connection: sqlite3.Connection, agent_id: str, record_ids: Sequence[str]) -> tuple[AgentJournalRecord, ...]:
    if not record_ids:
        return ()
    rows = connection.execute(
        "SELECT * FROM base_agent_session_journal_v1 WHERE agent_id=? AND record_id IN (%s) ORDER BY seq" % ",".join("?" * len(record_ids)),
        (agent_id, *record_ids),
    ).fetchall()
    return tuple(history._record(row) for row in rows)


__all__ = (
    "CHUNKER_VERSION",
    "SessionIndexCoordinator",
    "SessionIndexState",
    "VIEW_POLICY_HASH",
    "chunk_text",
    "chunker_fingerprint",
    "finish_job_locked",
)
