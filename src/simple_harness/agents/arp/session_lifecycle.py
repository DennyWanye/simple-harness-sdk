# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-D1: Session destroy → DRAINING → complete disposal proof → PURGING → PURGED (§7, R1, J6/J7).

``SessionLifecycleService.destroy`` is the only way a Session gets a destroy identity:
the original Agent is closed and its open Turn cancelled first, then one execution
transaction records the destroy receipt, freezes ``PurgeProgress`` (exact source /
trash relative directories + the creation marker hash), bumps the control generation
and moves the row to DRAINING.  From there every step is *observe → one action*:

* DRAINING: ``capture_disposal`` witnesses the seven collections from the original
  ledgers; only a complete, empty proof seals the row (PURGING / RENAME_PENDING) and
  enqueues the PURGE job.
* PURGING: the two exact directories are inspected under the Session FileGuard and
  ``purge_rules.recovery_action`` names the single allowed step (rename, delete,
  record a recovered rename / absence, finalize) or the typed block that is persisted
  on the row without ever leaving DRAINING/PURGING (no "→ QUARANTINED" revival).
* FILE_BUSY blocks retry under the same destroy identity with backoff; every other
  block waits for the authenticated ``resume_destroy`` re-inspection.

Nothing here accepts a caller-supplied path, rewrites a marker, or fabricates a
receipt for a step that was not observed.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from . import embedding_call, store
from .codec import check
from .creation import MARKER_FILE
from .errors import ArpError
from .indexing import LEASE_MS, VIEW_POLICY_HASH, finish_job_locked
from .partition import NO_EMBEDDING_FINGERPRINT, FileGuard
from .pins import Pin
from .ports import RootIdentity, TRASH_DIR, TrustedCaller
from .purge_rules import can_rebuild, recovery_action, validate_relative_directory
from .rules import complete_disposal
from .strict import digest, parse_strict, plain

if TYPE_CHECKING:  # pragma: no cover
    from .context.recall import ContextRecallCoordinator
    from .indexing import SessionIndexCoordinator

DISPOSAL_KINDS = ("TURNS", "CALLS", "UNKNOWN", "PENDING_IMPORTS", "INDEX_WRITERS", "TEMP_ROOTS", "READ_HANDLES")
OPEN_TURN_PHASES = frozenset({"queued", "running", "result_pending"})
OPEN_EFFECT_STATES = frozenset({"prepared", "handed_off"})
OPEN_JOB_STATES = ("PENDING", "LEASED", "BLOCKED")
FILE_BUSY_BACKOFF_MS = 60_000
PURGE_DEADLINE_MS = 24 * 60 * 60 * 1000
DEFAULT_PERMIT_TTL_MS = 7 * 24 * 60 * 60 * 1000
BLOCK_CODES = {"BLOCK_DUAL_DIRECTORY": "DUAL_DIRECTORY", "BLOCK_MARKER_MISMATCH": "MARKER_MISMATCH", "BLOCK_DELETE_STATE_AMBIGUOUS": "DELETE_STATE_AMBIGUOUS"}
_BUSY_ERRNOS = frozenset({errno.EBUSY, errno.ETXTBSY, errno.EACCES, errno.EPERM})


def _hash(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) == 64 and all(c in "0123456789abcdef" for c in text) else digest(text)


@dataclass(frozen=True, slots=True)
class DisposalCapture:
    """One witnessed ``CompleteSessionDisposal`` plus the reader receipts that back it
    (persisted only when the proof seals the row, so a waiting Session does not grow
    the event log on every pass)."""

    body: Mapping[str, Any]
    receipts: tuple[tuple[str, str, Mapping[str, Any]], ...]
    blocking_refs: tuple[Pin, ...]

    @property
    def all_safe(self) -> bool:
        return bool(self.body["all_safe"])


class SessionLifecycleService:
    def __init__(
        self,
        *,
        runtime: Any,
        root: RootIdentity,
        index: SessionIndexCoordinator,
        recall: ContextRecallCoordinator,
        access_for: Callable[[store.SessionRow, str], Any],
        capture: Callable[[store.SessionRow, int], Any],
        release: Callable[[str], None],
        retention_policy_ref: Pin,
        authority_ref: Pin,
        clock_ms: Callable[[], int],
        clock: Callable[[], float],
        owner_id: str,
        fault: Callable[[str], None] | None = None,
        drain_timeout_s: float = 5.0,
    ) -> None:
        self.runtime = runtime
        self.root = root
        self.index = index
        self.recall = recall
        self.access_for = access_for
        self.capture = capture
        self._release_state = release
        self.retention_policy_ref = retention_policy_ref.require_kind("policy")
        self.authority_ref = authority_ref
        self.clock_ms = clock_ms
        self.clock = clock
        self.owner_id = owner_id
        self.fault = fault
        self.drain_timeout_s = drain_timeout_s
        self._last_blocking: dict[str, tuple[Pin, ...]] = {}

    # ---- helpers -------------------------------------------------------------------------

    @property
    def uow(self) -> Any:
        return self.index.uow

    @property
    def connection(self):  # type: ignore[no-untyped-def]
        return self.uow.database.connection

    def _fault(self, name: str) -> None:
        if self.fault is not None:
            self.fault(name)

    def _release(self, session_id: str) -> None:
        self._release_state(session_id)

    @staticmethod
    def _caller(caller: TrustedCaller | None) -> TrustedCaller:
        if not isinstance(caller, TrustedCaller):
            raise ArpError("AUTHORITY_SOURCE_MISSING", "session lifecycle commands need an authenticated caller")
        return caller

    def _session(self, session_id: str) -> store.SessionRow:
        session = store.read_session(self.connection, session_id)
        if session is None:
            raise ArpError("SESSION_NOT_ACTIVE", "unknown session")
        return session

    @staticmethod
    def _not_destroyable(session: store.SessionRow) -> ArpError:
        if session.state == "DRAINING":
            return ArpError("SESSION_DRAINING", "session is draining under another destroy command")
        if session.state in ("PURGING", "PURGED"):
            return ArpError("SESSION_PURGED", f"session is {session.state}")
        return ArpError("SESSION_NOT_ACTIVE", f"session is {session.state}")

    def _event_locked(self, connection, session: store.SessionRow, after: store.SessionRow, source_receipt_ref: Pin) -> None:  # type: ignore[no-untyped-def]
        store.append_runtime_event_locked(
            connection,
            run_id=after.agent_id,
            event_type="RuntimeSessionStateChanged",
            body={
                "schema_version": 2,
                "session_ref": after.pin.to_json(),
                "row_version": after.row_version,
                "old_state": session.state,
                "new_state": after.state,
                "source_receipt_ref": source_receipt_ref.to_json(),
                "purge_progress_hash": after.purge_progress_hash,
            },
            source_receipt_ref=source_receipt_ref,
            dedupe_key=f"{after.session_id}:state:{after.row_version}",
            now=self.clock(),
        )

    def _cancel_queued_jobs_locked(self, connection, session: store.SessionRow, source: Pin) -> int:  # type: ignore[no-untyped-def]
        """INDEX / BIND_IMPORT work that never started stops here; leased work finishes
        (and cancels itself) under its own owner.  Before a job is cancelled, every
        embedding intent it left without a receipt is settled as UNKNOWN in the call
        ledger (what its re-run would have recorded), so the fact is never hidden."""

        cancelled = 0
        leased = {j.job_id for j in store.list_session_jobs(connection, session.session_id, states=("LEASED",))}
        deployment = self.index.embedding_resource_ref or Pin("deployment", "embedding:none", 0, NO_EMBEDDING_FINGERPRINT)
        for call_key, intent in embedding_call.list_open_intents(connection, session.agent_id):
            if call_key.split("/", 1)[0] in leased:
                continue  # its worker may still be inside the call; witnessed as open below
            embedding_call.record_lost_call_locked(connection, run_id=session.agent_id, call_key=call_key, intent=intent, deployment_ref=deployment, now=self.clock())
        for job in store.list_session_jobs(connection, session.session_id, kinds=("INDEX", "BIND_IMPORT"), states=("PENDING", "BLOCKED")):
            finish_job_locked(
                connection, job, "CANCELLED", session=session, owner_id=self.owner_id, clock=self.clock, clock_ms=self.clock_ms,
                result_kind="CANCELLED", reason="SESSION_NOT_ACTIVE", result_refs=[source],
            )
            cancelled += 1
        return cancelled

    def _stale_recalls(self, session: store.SessionRow) -> int:
        """J7: destroy makes every non-terminal recall of the Session STALE through the
        coordinator's own path (the row records the named code, never a silent drop)."""

        count = 0
        for row in store.list_pending_recalls(self.connection, limit=64, session_id=session.session_id):
            clock = Pin("receipt", f"destroy:{row.recall_key}", 0, digest({"at": self.clock_ms()}))
            try:
                self.recall.resume(row.recall_key, self.access_for(session, row.turn_id), now_ms=self.clock_ms(), clock_receipt_ref=clock)
            except ArpError as error:
                if error.code not in ("RECALL_PREPARE_BLOCKED", "FILE_BUSY"):
                    raise
            count += 1
        return count

    # ---- destroy ---------------------------------------------------------------------------

    async def destroy(self, command: Mapping[str, Any], *, caller: TrustedCaller, command_id: str) -> store.SessionRow:
        """``SessionDelete``: close/cancel the original Agent, then one transaction moves the
        Session to DRAINING with its frozen destroy identity.  A same-command replay returns
        the current row; any other command on a destroyed Session is refused by name."""

        caller = self._caller(caller)
        value = check("SessionDelete", plain(command))
        if value["command_id"] != command_id:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "command id differs from the payload")
        if Pin.from_json(value["retention_policy_ref"]) != self.retention_policy_ref:
            raise ArpError("POLICY_CONFLICT", "retention policy differs from the activated profile")
        session = self._session(str(value["session_id"]))
        command_hash = digest({
            "kind": "destroy", "command": value, "caller": caller.to_json(), "session_id": session.session_id,
            "agent_id": session.agent_id, "root_incarnation": session.root_incarnation,
        })
        if session.destroy_command_id is not None:
            if session.destroy_command_id != command_id:
                raise self._not_destroyable(session)
            if session.destroy_command_hash != command_hash:
                raise ArpError("SOURCE_HASH_CONFLICT", "destroy command id reused with another body")
            return session
        if session.state not in ("ACTIVE", "QUARANTINED"):
            raise self._not_destroyable(session)
        if session.generation != int(value["expected_generation"]):
            raise ArpError("GENERATION_STALE", "expected generation differs")
        protocol = store.read_protocol(self.connection, session.agent_id)
        if protocol is None:
            raise ArpError("RUNTIME_CREATION_MARKER_MISSING", "session protocol row missing")
        # The original Agent stops taking inputs; an open Turn is cancelled cooperatively.
        receipt = await self.runtime.close_agent(session.agent_id, command_id=f"{command_id}:close", drain_timeout=self.drain_timeout_s)
        if receipt.open_turn_id is not None:
            await self.runtime.cancel_turn(session.agent_id, receipt.open_turn_id, command_id=f"{command_id}:cancel", wait_timeout=self.drain_timeout_s)
        self._release(session.session_id)
        trash = validate_relative_directory(f"{TRASH_DIR}/{session.session_id}-{digest({'destroy': command_id})[:16]}")
        source = validate_relative_directory(session.relative_directory)
        now_ms = self.clock_ms()
        guard = FileGuard(self.root, session.session_id)
        with guard.held():
            with self.uow.database.transaction() as connection:
                current = store.read_session(connection, session.session_id)
                if current is None or current.row_version != session.row_version:
                    raise ArpError("GENERATION_STALE", "session row changed concurrently")
                destroy_ref = store.append_original_receipt_locked(
                    connection, run_id=session.agent_id, kind="session_destroy", receipt_key=command_id,
                    body={
                        "command": value, "caller": caller.to_json(), "command_hash": command_hash, "session_id": session.session_id,
                        "agent_id": session.agent_id, "from_state": session.state, "from_generation": session.generation,
                        "to_generation": session.generation + 1, "agent_lifecycle": receipt.state,
                    },
                    now=self.clock(),
                )
                progress = {
                    "schema_version": 1,
                    "session_id": session.session_id,
                    "agent_id": session.agent_id,
                    "root_incarnation": session.root_incarnation,
                    "destroy_command_id": command_id,
                    "destroy_command_hash": command_hash,
                    "destroy_receipt_ref": destroy_ref.to_json(),
                    "control_generation": session.generation + 1,
                    "source_relative_directory": source,
                    "trash_relative_directory": trash,
                    "expected_marker_hash": protocol.marker_hash,
                    "phase": "DRAINING",
                    "rename_receipt_ref": None,
                    "delete_receipt_ref": None,
                    "blocking": None,
                    "inspection_receipt_ref": None,
                }
                after = store.transition_session_locked(
                    connection, current, now_ms=now_ms, state="DRAINING", generation=session.generation + 1,
                    destroy_command=(command_id, command_hash), purge_progress=progress,
                )
                self._event_locked(connection, current, after, destroy_ref)
                self._cancel_queued_jobs_locked(connection, after, destroy_ref)
        self._stale_recalls(after)
        try:
            return self.advance(after.session_id)
        except ArpError as error:
            if error.code != "FILE_BUSY":
                raise
            return after

    # ---- disposal proof ----------------------------------------------------------------------

    def _witness(self, kind: str, session: store.SessionRow, refs: Sequence[Pin], highwater: int, *, now_ms: int) -> tuple[dict[str, Any], tuple[str, str, dict[str, Any]]]:
        destroy = session.destroy_command_id or "none"
        pins = [p.to_json() for p in refs]
        reader = {
            "kind": kind, "session_id": session.session_id, "agent_id": session.agent_id, "destroy_command_id": destroy,
            "generation": session.generation, "refs": pins, "highwater": highwater, "observed_at_ms": now_ms,
        }
        key = f"{kind}:{session.session_id}:{destroy}:{session.row_version}"
        ref = Pin("receipt", f"disposal_reader:{key}", 0, digest(reader))
        witness = {"kind": kind, "complete": True, "count": len(pins), "refs": pins, "set_hash": digest(pins), "highwater": highwater, "reader_receipt_ref": ref.to_json()}
        return witness, ("disposal_reader", key, reader)

    def capture_disposal(self, session: store.SessionRow) -> DisposalCapture:
        """``SessionDisposalReader.capture``: the seven collections read from the original
        ledgers (turns, effects, provider invocations, jobs, skill script uses, recalls).
        Call it with the Session FileGuard held."""

        connection = self.connection
        uow = self.uow
        now_ms = self.clock_ms()
        agent_id = session.agent_id
        turns = uow.list_agent_turns(agent_id)
        open_turns = [Pin("agent_turn", t.turn_id, int(t.seq), _hash(t.input_hash)) for t in turns if t.phase in OPEN_TURN_PHASES]
        effects = uow.list_effects_for_run(agent_id)
        calls = [Pin("invocation", str(e.effect_id), int(e.version), _hash(e.request_hash)) for e in effects if str(e.state) in OPEN_EFFECT_STATES]
        unknown = [Pin("invocation", str(e.effect_id), int(e.version), _hash(e.request_hash)) for e in effects if str(e.state) == "unknown"]
        # A ``claimed`` invocation never entered transport (the kernel's "not started";
        # its reconciliation skips it too) — e.g. a request the composer refused before
        # the handoff.  Only an uncertain handoff is UNKNOWN.
        unknown.extend(
            Pin("invocation", str(p.invocation_id), int(p.version), _hash(p.request_fingerprint))
            for p in uow.list_incomplete_provider_invocations() if str(p.run_id) == agent_id and str(p.state) != "claimed"
        )
        # Embedding intents without a call receipt are calls whose outcome nobody recorded yet.
        unknown.extend(Pin("invocation", key, 0, digest(intent)) for key, intent in embedding_call.list_open_intents(connection, agent_id))
        imports = [Pin("operation", j.job_id, j.row_version, j.payload_hash) for j in store.list_session_jobs(connection, session.session_id, kinds=("BIND_IMPORT",), states=OPEN_JOB_STATES)]
        writers = [Pin("operation", j.job_id, j.row_version, j.payload_hash) for j in store.list_session_jobs(connection, session.session_id, kinds=("INDEX",), states=OPEN_JOB_STATES)]
        temp_roots = self._open_script_uses(session)
        handles = [Pin("operation", r.recall_key, r.row_version, r.request_hash) for r in store.list_pending_recalls(connection, limit=8192, session_id=session.session_id)]
        highwater = int(connection.execute("SELECT COALESCE(MAX(seq),0) FROM base_agent_session_journal_v1 WHERE agent_id=?", (agent_id,)).fetchone()[0])
        collected = {
            "TURNS": (open_turns, max([int(t.seq) for t in turns], default=0)),
            "CALLS": (calls, len(effects)),
            "UNKNOWN": (unknown, len(effects)),
            "PENDING_IMPORTS": (imports, len(store.list_session_jobs(connection, session.session_id, kinds=("BIND_IMPORT",)))),
            "INDEX_WRITERS": (writers, len(store.list_session_jobs(connection, session.session_id, kinds=("INDEX",)))),
            "TEMP_ROOTS": (temp_roots, len(store.read_skill_uses(connection, session.session_id, mode="SCRIPT"))),
            "READ_HANDLES": (handles, highwater),
        }
        witnesses: list[dict[str, Any]] = []
        receipts: list[tuple[str, str, dict[str, Any]]] = []
        blocking: list[Pin] = []
        for kind in DISPOSAL_KINDS:
            refs, water = collected[kind]
            witness, receipt = self._witness(kind, session, refs, water, now_ms=now_ms)
            witnesses.append(witness)
            receipts.append(receipt)
            blocking.extend(refs)
        destroy = session.destroy_command_id or "none"
        guard_key = f"{session.session_id}:{destroy}:{session.row_version}"
        guard_body = {"session_id": session.session_id, "destroy_command_id": destroy, "held_at_ms": now_ms, "lock": f"{session.session_id}.lock"}
        guard_ref = Pin("receipt", f"file_guard:{guard_key}", 0, digest(guard_body))
        receipts.append(("file_guard", guard_key, guard_body))
        readset = {"readers": [r[1] for r in receipts], "journal_highwater": highwater, "generation": session.generation}
        body = {
            "schema_version": 1,
            "session_ref": session.pin.to_json(),
            "agent_ref": Pin("agent", agent_id, 0, digest(agent_id)).to_json(),
            "root_incarnation": session.root_incarnation,
            "generation": session.generation,
            "journal_highwater": highwater,
            "collections": witnesses,
            "file_guard_receipt_ref": guard_ref.to_json(),
            "source_readset_ref": Pin("artifact", f"disposal-readset:{session.session_id}:{destroy}", session.generation, digest(readset)).to_json(),
            "all_safe": complete_disposal(witnesses),
        }
        value = check("CompleteSessionDisposal", body)
        capture = DisposalCapture(value, tuple(receipts), tuple(blocking))
        self._last_blocking[session.session_id] = capture.blocking_refs
        return capture

    def _open_script_uses(self, session: store.SessionRow) -> list[Pin]:
        """SCRIPT uses whose executor never reported a terminal state still own a workspace."""

        connection = self.connection
        uses = store.read_skill_uses(connection, session.session_id, mode="SCRIPT")
        if not uses:
            return []
        rows = connection.execute("SELECT payload_json FROM run_events WHERE run_id=? AND kind='arp.skill_execute.v1'", (session.agent_id,)).fetchall()
        settled: set[str] = set()
        for (raw,) in rows:
            body = store.load_json(raw)["body"]
            if body.get("view", {}).get("status") != "UNKNOWN":
                settled.add(str(body["use_id"]))
        return [Pin("artifact", u["use_id"], 0, digest(u)) for u in uses if u["use_id"] not in settled]

    def _seal_locked(self, connection, session: store.SessionRow, capture: DisposalCapture, *, now_ms: int) -> store.SessionRow:  # type: ignore[no-untyped-def]
        """DRAINING → PURGING / RENAME_PENDING with the disposal proof persisted first."""

        destroy = session.destroy_command_id
        assert destroy is not None
        for kind, key, body in capture.receipts:
            store.append_original_receipt_locked(connection, run_id=session.agent_id, kind=kind, receipt_key=key, body=body, now=self.clock())
        proof = store.append_original_receipt_locked(
            connection, run_id=session.agent_id, kind="session_disposal", receipt_key=f"{session.session_id}:{destroy}", body=dict(capture.body), now=self.clock()
        )
        progress = dict(session.purge_progress or {})
        progress["phase"] = "RENAME_PENDING"
        after = store.transition_session_locked(
            connection, session, now_ms=now_ms, state="PURGING", sealed_highwater=int(capture.body["journal_highwater"]),
            purge_progress=progress, delete_proof_ref=proof,
        )
        self._event_locked(connection, session, after, proof)
        payload = {
            "schema_version": 1,
            "session_ref": after.pin.to_json(),
            "control_generation": after.generation,
            "destroy_receipt_ref": progress["destroy_receipt_ref"],
            "disposal_ref": Pin("artifact", f"disposal:{session.session_id}:{destroy}", after.generation, digest(capture.body)).to_json(),
            "deadline_ms": now_ms + PURGE_DEADLINE_MS,
        }
        store.put_job_locked(
            connection, job_id=f"purge-{digest({'session': session.session_id, 'destroy': destroy})[:32]}", session_id=session.session_id,
            kind="PURGE", semantic_key=destroy, payload=payload, generation=after.generation, next_at_ms=now_ms, deadline_ms=now_ms + PURGE_DEADLINE_MS,
        )
        self._last_blocking.pop(session.session_id, None)
        return after

    # ---- purge steps ------------------------------------------------------------------------

    def _path_state(self, relative: str, expected_marker_hash: str) -> str:
        directory = self.root.resolve_relative(relative)
        if not directory.exists():
            return "ABSENT"
        marker = directory / MARKER_FILE
        try:
            body = check("SessionMarker", parse_strict(marker.read_bytes(), max_bytes=16 * 1024))
        except (OSError, ArpError, ValueError):
            return "MISMATCH"
        return "MATCH" if digest(body) == expected_marker_hash else "MISMATCH"

    def observe(self, session: store.SessionRow, *, now_ms: int) -> dict[str, Any]:
        """``PurgePathObservation`` of the two exact directories (guard held by the caller)."""

        progress = session.purge_progress
        assert progress is not None and session.destroy_command_id is not None
        body = {
            "schema_version": 1,
            "session_id": session.session_id,
            "destroy_command_id": session.destroy_command_id,
            "control_generation": session.generation,
            "expected_marker_hash": str(progress["expected_marker_hash"]),
            "source_state": self._path_state(str(progress["source_relative_directory"]), str(progress["expected_marker_hash"])),
            "trash_state": self._path_state(str(progress["trash_relative_directory"]), str(progress["expected_marker_hash"])),
            "observed_at_ms": now_ms,
        }
        key = f"{session.session_id}:{session.destroy_command_id}:{session.row_version}"
        body["receipt_ref"] = Pin("receipt", f"purge_observation:{key}", 0, digest(body)).to_json()
        return check("PurgePathObservation", body)

    def _observation_locked(self, connection, session: store.SessionRow, observation: Mapping[str, Any]) -> Pin:  # type: ignore[no-untyped-def]
        body = {k: v for k, v in observation.items() if k != "receipt_ref"}
        key = f"{session.session_id}:{session.destroy_command_id}:{session.row_version}"
        return store.append_original_receipt_locked(connection, run_id=session.agent_id, kind="purge_observation", receipt_key=key, body=body, now=self.clock())

    def _progress_locked(self, connection, session: store.SessionRow, progress: Mapping[str, Any], *, source: Pin, now_ms: int, state: str | None = None) -> store.SessionRow:  # type: ignore[no-untyped-def]
        after = store.transition_session_locked(connection, session, now_ms=now_ms, state=state, purge_progress=progress)
        self._event_locked(connection, session, after, source)
        return after

    def _block(self, session: store.SessionRow, observation: Mapping[str, Any], code: str, *, now_ms: int) -> store.SessionRow:
        """Persist a typed block on the row: same state, same phase, same destroy identity."""

        progress = dict(session.purge_progress or {})
        previous = progress.get("blocking")
        first_seen = int(previous["first_seen_at_ms"]) if previous and previous["code"] == code else now_ms
        busy = code == "FILE_BUSY"
        with self.uow.database.transaction() as connection:
            inspection = self._observation_locked(connection, session, observation)
            progress["blocking"] = {
                "code": code,
                "observation_ref": inspection.to_json(),
                "retry_mode": "SAME_DESTROY_BACKOFF" if busy else "MANUAL",
                "retry_at_ms": now_ms + FILE_BUSY_BACKOFF_MS if busy else None,
                "first_seen_at_ms": first_seen,
            }
            progress["inspection_receipt_ref"] = inspection.to_json()
            return self._progress_locked(connection, session, progress, source=inspection, now_ms=now_ms)

    @staticmethod
    def _io_code(error: OSError) -> str:
        return "FILE_BUSY" if isinstance(error, PermissionError) or error.errno in _BUSY_ERRNOS else "IO_ERROR"

    def _rename(self, session: store.SessionRow, observation: Mapping[str, Any], *, recovered: bool, now_ms: int) -> store.SessionRow:
        progress = dict(session.purge_progress or {})
        source = self.root.resolve_relative(str(progress["source_relative_directory"]))
        trash = self.root.resolve_relative(str(progress["trash_relative_directory"]))
        if not recovered:
            try:
                trash.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, trash)
            except OSError as error:
                return self._block(session, observation, self._io_code(error), now_ms=now_ms)
            self._fault("purge.after_rename")
        with self.uow.database.transaction() as connection:
            inspection = self._observation_locked(connection, session, observation)
            rename_ref = store.append_original_receipt_locked(
                connection, run_id=session.agent_id, kind="purge_rename", receipt_key=f"{session.session_id}:{session.destroy_command_id}",
                body={
                    "session_id": session.session_id, "destroy_command_id": session.destroy_command_id, "source": progress["source_relative_directory"],
                    "trash": progress["trash_relative_directory"], "recovered": recovered, "observation_ref": inspection.to_json(),
                },
                now=self.clock(),
            )
            progress.update(phase="RENAMED", rename_receipt_ref=rename_ref.to_json(), blocking=None, inspection_receipt_ref=inspection.to_json())
            return self._progress_locked(connection, session, progress, source=rename_ref, now_ms=now_ms)

    def _delete(self, session: store.SessionRow, observation: Mapping[str, Any], *, absence: bool, now_ms: int) -> store.SessionRow:
        progress = dict(session.purge_progress or {})
        trash = self.root.resolve_relative(str(progress["trash_relative_directory"]))
        if not absence:
            try:
                self._remove_trash(trash)
            except OSError as error:
                return self._block(session, observation, self._io_code(error), now_ms=now_ms)
            self._fault("purge.after_delete")
        with self.uow.database.transaction() as connection:
            inspection = self._observation_locked(connection, session, observation)
            delete_ref = store.append_original_receipt_locked(
                connection, run_id=session.agent_id, kind="purge_delete", receipt_key=f"{session.session_id}:{session.destroy_command_id}",
                body={
                    "session_id": session.session_id, "destroy_command_id": session.destroy_command_id, "trash": progress["trash_relative_directory"],
                    "absence": absence, "rename_receipt_ref": progress["rename_receipt_ref"], "observation_ref": inspection.to_json(),
                },
                now=self.clock(),
            )
            progress.update(phase="DELETE_CONFIRMED", delete_receipt_ref=delete_ref.to_json(), blocking=None, inspection_receipt_ref=inspection.to_json())
            return self._progress_locked(connection, session, progress, source=delete_ref, now_ms=now_ms)

    @staticmethod
    def _remove_trash(trash: Path) -> None:
        """Delete the renamed directory's contents first and its marker last, so a partial
        failure leaves a directory that still identifies itself (retry under the same
        destroy identity) instead of an unidentifiable one (MARKER_MISMATCH)."""

        for entry in sorted(trash.iterdir(), key=lambda e: e.name):
            if entry.name == MARKER_FILE:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        marker = trash / MARKER_FILE
        if marker.exists():
            marker.unlink()
        trash.rmdir()

    def _finalize(self, session: store.SessionRow, observation: Mapping[str, Any], *, now_ms: int) -> store.SessionRow:
        progress = dict(session.purge_progress or {})
        with self.uow.database.transaction() as connection:
            inspection = self._observation_locked(connection, session, observation)
            progress.update(blocking=None, inspection_receipt_ref=inspection.to_json())
            return self._progress_locked(connection, session, progress, source=inspection, now_ms=now_ms, state="PURGED")

    def advance(self, session_id: str, *, force: bool = False) -> store.SessionRow:
        """One observe → one action step for a destroyed Session (the PURGE consumer's body).

        ``force`` is the authenticated resume: a MANUAL block is re-inspected; otherwise
        only FILE_BUSY blocks whose backoff elapsed are retried.  ``FILE_BUSY`` from the
        guard itself escapes to the caller (retry next pass)."""

        session = self._session(session_id)
        if session.destroy_command_id is None or session.purge_progress is None:
            raise ArpError("PURGE_STATE_INVALID", "session has no destroy identity")
        if session.state == "PURGED":
            return session
        now_ms = self.clock_ms()
        block = session.purge_progress.get("blocking")
        if block is not None and not force:
            if block["retry_mode"] != "SAME_DESTROY_BACKOFF" or now_ms < int(block["retry_at_ms"]):
                return session
        self._release(session_id)
        guard = FileGuard(self.root, session_id)
        with guard.held():
            if session.state == "DRAINING":
                with self.uow.database.transaction() as connection:
                    self._cancel_queued_jobs_locked(connection, session, Pin.from_json(session.purge_progress["destroy_receipt_ref"]))
                session = self._session(session_id)
                capture = self.capture_disposal(session)
                if not capture.all_safe:
                    return session
                with self.uow.database.transaction() as connection:
                    return self._seal_locked(connection, session, capture, now_ms=now_ms)
            observation = self.observe(session, now_ms=now_ms)
            action = recovery_action(session.purge_progress, observation)
            if action == "RENAME_EXACT":
                return self._rename(session, observation, recovered=False, now_ms=now_ms)
            if action == "RECORD_RECOVERED_RENAME":
                return self._rename(session, observation, recovered=True, now_ms=now_ms)
            if action == "DELETE_EXACT":
                return self._delete(session, observation, absence=False, now_ms=now_ms)
            if action == "RECORD_DELETION_ABSENCE":
                return self._delete(session, observation, absence=True, now_ms=now_ms)
            if action == "FINALIZE_SAME_DESTROY":
                return self._finalize(session, observation, now_ms=now_ms)
            if action in BLOCK_CODES:
                return self._block(session, observation, BLOCK_CODES[action], now_ms=now_ms)
            raise ArpError("PURGE_STATE_INVALID", f"no action for {action}")

    # ---- resume (R1 authenticated re-inspection) ------------------------------------------------

    def resume_destroy(self, command: Mapping[str, Any], *, caller: TrustedCaller, command_id: str) -> store.SessionRow:
        """``SessionDestroyResume``: re-inspect the same destroy identity and continue its phase.
        The original destroy id/hash, directories and marker never change; a repeated resume
        command returns the current row without a second inspection."""

        caller = self._caller(caller)
        value = check("SessionDestroyResume", plain(command))
        if value["command_id"] != command_id:
            raise ArpError("SESSION_IDENTITY_MISMATCH", "command id differs from the payload")
        session = self._session(str(value["session_id"]))
        if (
            session.agent_id != value["agent_id"]
            or session.destroy_command_id != value["destroy_command_id"]
            or session.destroy_command_hash != value["destroy_command_hash"]
        ):
            raise ArpError("PURGE_IDENTITY_MISMATCH", "resume names another destroy identity")
        if store.read_original_receipt(self.connection, kind="session_destroy_resume", receipt_key=command_id) is not None:
            return session
        if session.generation != int(value["expected_generation"]) or session.row_version != int(value["expected_row_version"]):
            raise ArpError("GENERATION_STALE", "session row differs from the resume command")
        with self.uow.database.transaction() as connection:
            store.append_original_receipt_locked(
                connection, run_id=session.agent_id, kind="session_destroy_resume", receipt_key=command_id,
                body={"command": value, "caller": caller.to_json(), "state": session.state, "phase": (session.purge_progress or {}).get("phase")},
                now=self.clock(),
            )
        after = self.advance(session.session_id, force=True)
        self._requeue_purge_job(after)
        return after

    def _requeue_purge_job(self, session: store.SessionRow) -> None:
        if session.destroy_command_id is None or session.state != "PURGING":
            return
        block = (session.purge_progress or {}).get("blocking")
        with self.uow.database.transaction() as connection:
            job = store.read_job_by_key(connection, session.session_id, "PURGE", session.destroy_command_id)
            if job is None or job.state not in ("BLOCKED", "PENDING"):
                return
            next_at = self.clock_ms() if block is None else (int(block["retry_at_ms"]) if block["retry_at_ms"] is not None else None)
            if next_at is None:
                return
            store.complete_job_locked(connection, job, state="PENDING", owner_id=self.owner_id, next_at_ms=next_at)

    # ---- tick integration --------------------------------------------------------------------

    def drive_draining(self, *, limit: int = 8) -> int:
        """Every DRAINING Session gets one proof attempt per pass (no job row yet: the PURGE
        job is created by the seal itself)."""

        driven = 0
        for session in store.list_sessions_in_state(self.connection, ["DRAINING"], limit=limit):
            try:
                self.advance(session.session_id)
            except ArpError as error:
                if error.code != "FILE_BUSY":
                    raise
                continue
            driven += 1
        return driven

    def process_purge_job(self, job: store.JobRow) -> Mapping[str, Any]:
        """PURGE consumer: one step, then the central ACK (DONE / BLOCKED / handed back)."""

        session = store.read_session(self.connection, job.session_id)
        if session is None:
            raise ArpError("SESSION_NOT_ACTIVE", "purge job session vanished")
        now_ms = self.clock_ms()
        if session.destroy_command_id != job.semantic_key or session.generation != int(job.payload["control_generation"]):
            return self._ack(job, session, "CANCELLED", reason="PURGE_IDENTITY_MISMATCH")
        try:
            after = self.advance(session.session_id)
        except ArpError as error:
            if error.code == "FILE_BUSY":
                return self._ack(job, session, "PENDING", reason="FILE_BUSY", next_at_ms=now_ms + LEASE_MS)
            return self._ack(job, session, "BLOCKED", reason=error.code)
        if after.state == "PURGED":
            delete_ref = Pin.from_json((after.purge_progress or {})["delete_receipt_ref"])
            return self._ack(job, after, "DONE", result_refs=[delete_ref])
        block = (after.purge_progress or {}).get("blocking")
        if block is None:
            return self._ack(job, after, "PENDING", reason=str((after.purge_progress or {}).get("phase")), next_at_ms=now_ms)
        if block["retry_mode"] == "SAME_DESTROY_BACKOFF":
            return self._ack(job, after, "PENDING", reason="PURGE_FILE_BUSY", next_at_ms=int(block["retry_at_ms"]))
        return self._ack(job, after, "BLOCKED", reason=f"PURGE_{block['code']}")

    def _ack(self, job: store.JobRow, session: store.SessionRow, state: str, *, reason: str | None = None, result_refs: Sequence[Pin] = (), next_at_ms: int | None = None) -> Mapping[str, Any]:
        with self.uow.database.transaction() as connection:
            value, _ = finish_job_locked(
                connection, job, state, session=session, owner_id=self.owner_id, clock=self.clock, clock_ms=self.clock_ms,
                result_kind={"DONE": "PURGED", "BLOCKED": "BLOCKED", "CANCELLED": "CANCELLED", "PENDING": "BLOCKED"}[state],
                reason=reason, result_refs=result_refs, next_at_ms=next_at_ms,
            )
        return value

    # ---- retention permits ---------------------------------------------------------------------

    def permit_session_erasure(self, session_id: str, *, caller: TrustedCaller, command_id: str, ttl_ms: int = DEFAULT_PERMIT_TTL_MS) -> Mapping[str, Any]:
        """Sign the retention permit that lets the PURGED Session's central row be deleted by
        the retention service; anything not PURGED is refused (``RETENTION_BLOCKED``)."""

        caller = self._caller(caller)
        session = self._session(session_id)
        if session.state != "PURGED":
            raise ArpError("RETENTION_BLOCKED", f"session is {session.state}, not PURGED")
        if type(ttl_ms) is not int or ttl_ms <= 0:
            raise ArpError("NUMBER_LIMIT", field_path="ttl_ms")
        with self.uow.database.transaction() as connection:
            receipt = store.append_original_receipt_locked(
                connection, run_id=session.agent_id, kind="retention_permit", receipt_key=command_id,
                body={"session_id": session_id, "caller": caller.to_json(), "table_name": "arp_agent_sessions", "ttl_ms": ttl_ms},
                now=self.clock(),
            )
            return store.put_retention_permit_locked(
                connection, table_name="arp_agent_sessions", row_key=[session_id], body_hash=session.create_command_hash,
                expires_at_ms=self.clock_ms() + ttl_ms, source_receipt_ref=receipt,
            )

    # ---- quarantine / rebuild ------------------------------------------------------------------

    def quarantine(self, session_id: str, *, caller: TrustedCaller, command_id: str, reason: str) -> store.SessionRow:
        """ACTIVE → QUARANTINED (index isolation without a destroy identity)."""

        caller = self._caller(caller)
        session = self._session(session_id)
        if store.read_original_receipt(self.connection, kind="session_quarantine", receipt_key=command_id) is not None:
            return session
        if session.state != "ACTIVE":
            raise self._not_destroyable(session)
        self._release(session_id)
        now_ms = self.clock_ms()
        with self.uow.database.transaction() as connection:
            receipt = store.append_original_receipt_locked(
                connection, run_id=session.agent_id, kind="session_quarantine", receipt_key=command_id,
                body={"session_id": session_id, "caller": caller.to_json(), "reason": str(reason)[:1024], "from_generation": session.generation},
                now=self.clock(),
            )
            after = store.transition_session_locked(connection, session, now_ms=now_ms, state="QUARANTINED", generation=session.generation + 1)
            self._event_locked(connection, session, after, receipt)
            self._cancel_queued_jobs_locked(connection, after, receipt)
        self._stale_recalls(after)
        return after

    def rebuild(self, command: Mapping[str, Any], *, caller: TrustedCaller, command_id: str) -> store.SessionRow:
        """``IndexRebuildCommand``: same-root rebuild of a QUARANTINED Session into a new index
        generation.  Any destroy identity is refused by name before anything else changes."""

        caller = self._caller(caller)
        value = check("IndexRebuildCommand", plain(command))
        session = self._session(str(value["session_id"]))
        if store.read_original_receipt(self.connection, kind="session_rebuild", receipt_key=command_id) is not None:
            return session
        if session.destroy_command_id is not None or session.purge_progress is not None:
            raise self._not_destroyable(session)
        if not can_rebuild({"state": session.state, "destroy_command_id": session.destroy_command_id, "purge_progress": session.purge_progress}):
            raise ArpError("STATE_COMBINATION_INVALID", f"rebuild needs a QUARANTINED session, not {session.state}")
        if session.generation != int(value["expected_generation"]):
            raise ArpError("GENERATION_STALE", "expected generation differs")
        index = self.index
        embedding_ref = index.embedding_resource_ref or Pin("deployment", "embedding:none", 0, NO_EMBEDDING_FINGERPRINT)
        if (
            Pin.from_json(value["chunker_ref"]).content_hash != index.chunker
            or Pin.from_json(value["view_policy_ref"]).content_hash != VIEW_POLICY_HASH
            or Pin.from_json(value["embedding_deployment_ref"]) != embedding_ref
        ):
            raise ArpError("POLICY_CONFLICT", "rebuild names resources other than the deployment's")
        self._release(session.session_id)
        state = index.state_for(session)
        active = store.active_index_publication(self.connection, session.session_id)
        current_generation = active.index_generation if active is not None else state.generation.index_generation
        if current_generation != int(value["old_index_generation"]):
            raise ArpError("GENERATION_STALE", "old index generation differs from the published one")
        with state.guard.held():
            with state.partition.transaction() as pconn:
                pconn.execute("UPDATE index_generations SET state='RETIRED' WHERE index_generation=? AND state!='RETIRED'", (int(value["old_index_generation"]),))
                fresh = state.partition.ensure_generation_locked(
                    pconn, view_policy_hash=VIEW_POLICY_HASH, chunker_fingerprint=index.chunker, embedding_fingerprint=index.embedding_fingerprint
                )
            state.generation = fresh
            now_ms = self.clock_ms()
            with self.uow.database.transaction() as connection:
                receipt = store.append_original_receipt_locked(
                    connection, run_id=session.agent_id, kind="session_rebuild", receipt_key=command_id,
                    body={"command": value, "caller": caller.to_json(), "new_index_generation": fresh.index_generation},
                    now=self.clock(),
                )
                after = store.transition_session_locked(connection, session, now_ms=now_ms, state="ACTIVE")
                self._event_locked(connection, session, after, receipt)
                highwater = int(connection.execute("SELECT COALESCE(MAX(seq),0) FROM base_agent_session_journal_v1 WHERE agent_id=?", (session.agent_id,)).fetchone()[0])
                if highwater > 0:
                    snapshot = self.capture(after, highwater)
                    index.enqueue_closed_groups_locked(
                        connection, session=after, snapshot=snapshot, generation=fresh, authority_ref=self.authority_ref,
                        turn_ref=Pin("agent_turn", f"rebuild:{command_id}", 0, digest(command_id)),
                    )
        return after

    # ---- SessionView projection --------------------------------------------------------------

    def view(self, session_id: str) -> Mapping[str, Any]:
        session = self._session(session_id)
        connection = self.connection
        adoption = store.latest_adoption(connection, session_id)
        if adoption is None:
            raise ArpError("STATE_COMBINATION_INVALID", "session without a policy adoption")
        protocol = store.read_protocol(connection, session.agent_id)
        if protocol is None:
            raise ArpError("RUNTIME_CREATION_MARKER_MISSING", "session protocol row missing")
        active = store.active_index_publication(connection, session_id)
        pending = len(store.list_session_jobs(connection, session_id, kinds=("INDEX",), states=("PENDING", "LEASED")))
        progress = session.purge_progress
        last: Pin = protocol.creation_receipt_ref
        blocking: list[Pin] = []
        if progress is not None:
            for key in ("destroy_receipt_ref", "inspection_receipt_ref", "rename_receipt_ref", "delete_receipt_ref"):
                if progress.get(key) is not None:
                    last = Pin.from_json(progress[key])
            if progress.get("blocking") is not None:
                blocking.append(Pin.from_json(progress["blocking"]["observation_ref"]))
            elif session.state == "DRAINING":
                blocking.extend(self._last_blocking.get(session_id, ()))
        body = {
            "schema_version": 2,
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "root_id": self.root.root_id,
            "generation": session.generation,
            "state": session.state,
            "journal_seq_from": session.journal_seq_from,
            "sealed_highwater": session.sealed_highwater,
            "pending_index_groups": pending,
            "embedding_available": self.index.embedding is not None,
            "deletable": session.state in ("ACTIVE", "QUARANTINED"),
            "blocking_refs": [p.to_json() for p in blocking[:128]],
            "row_version": session.row_version,
            "adoption_revision": adoption.adoption_revision,
            "effective_policy_ref": adoption.policy_ref.to_json(),
            "active_index_generation": None if active is None else active.index_generation,
            "last_lifecycle_receipt_ref": last.to_json(),
            "purge_progress": None if progress is None else dict(progress),
        }
        return check("SessionView", body)


__all__ = (
    "DEFAULT_PERMIT_TTL_MS",
    "DISPOSAL_KINDS",
    "DisposalCapture",
    "FILE_BUSY_BACKOFF_MS",
    "PURGE_DEADLINE_MS",
    "SessionLifecycleService",
)
