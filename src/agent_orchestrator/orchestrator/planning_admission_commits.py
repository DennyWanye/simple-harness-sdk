# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Commit-time H1-H producer rechecks.

The planner response is assembled from several producer snapshots.  Admission is
therefore not a write authority by itself: immediately before the existing
PlanRevision transaction writes, this mixin rereads every producer on the same
Store transaction and refuses a stale or incomplete snapshot.  Legacy callers do
not use this entry point.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, NoReturn, TYPE_CHECKING

from ..contracts.models import sha256_hex
from simple_harness.contracts import canonical_json
from ..planning.htn.grounding import derive_id
from ..storage.htn_store import HtnStore, PlanCommitReceipt
from ..storage.planning_decision_store import PlanningDecisionStore
from ..graph.execution_contracts import PreviewBindingV1
from ..governance.planning_authorization import (
    PlanningAuthorizationSnapshot,
    planning_policy_for_mission,
    StorePlanningAuthorityReader,
    build_planning_authorization,
    check_planning_authorization,
)
from ..governance.planning_authorization import (
    SourceUnavailable as AuthoritySourceUnavailable,
)
from ..planning.plan_preview import _source_snapshot_payload
from ..runtime.planning_operations import (
    OperationSnapshot,
    RuntimeWorkSnapshot,
    StoreOperationReader,
    build_operation_snapshot,
    operation_gate,
    read_running_work,
)
from ..runtime.planning_operations import (
    SourceUnavailable as OperationSourceUnavailable,
)
from ..storage.planning_admission_store import PlanningAdmissionStore
from .plan_commits import CommitPlanCommand, PlanCommitRejected, PlanPrincipal


if TYPE_CHECKING:
    from .taskgraph_plan_commit import TaskGraphPlanCommitParticipant
    from .taskgraph_preview import FrozenTaskGraphCandidate


@dataclass(frozen=True, slots=True)
class PlanningCommitAdmission:
    """The immutable producer values captured by H1-H preview/admission."""

    request_id: str
    decision_hash: str
    decision_key: str
    authority: PlanningAuthorizationSnapshot
    operations: OperationSnapshot
    runtime_work: RuntimeWorkSnapshot
    preview_request_id: str
    preview_decision_hash: str
    preview_compilation_hash: str
    preview_read_set_hash: str
    taskgraph_candidate: FrozenTaskGraphCandidate | None = None


@dataclass(frozen=True, slots=True)
class CheckedPlanningCommit:
    """Actual successful precommit reads, carried only inside the original transaction."""
    command_id: str
    intent_hash: str
    authority_hash: str
    operations_hash: str
    runtime_work_hash: str


def _write_taskgraph_applied(store: Any, command: CommitPlanCommand, receipt: PlanCommitReceipt,
                             preview: PreviewBindingV1, admission: PlanningCommitAdmission,
                             checked: CheckedPlanningCommit) -> str:
    if not store.connection.in_transaction:
        _raise("TASKGRAPH_APPLIED_TRANSACTION_REQUIRED", "APPLIED joins the original plan write")
    if (checked.command_id != command.command_id or checked.intent_hash != command.intent_hash()
            or receipt.command_id != command.command_id or receipt.intent_hash != checked.intent_hash
            or receipt.mission_id != command.mission_id or receipt.new_plan_revision != preview.base_revision + 1
            or receipt.base_plan_revision != preview.base_revision
            or HtnStore(store).get_commit_receipt(command.command_id) != receipt):
        _raise("TASKGRAPH_APPLIED_RECEIPT_MISMATCH", "the actual original PlanCommit receipt is required")
    decisions = PlanningDecisionStore(store)
    request = decisions.get_planning_request(preview.request_id)
    decision = decisions.get_planning_decision(preview.decision_id)
    if (request is None or request.mission_id != command.mission_id
            or request.base_plan_revision != preview.base_revision
            or decision is None or decision["request_id"] != preview.request_id
            or decision["canonical_hash"] != preview.decision_hash or str(decision["status"]) != "COMPILED"
            or admission.request_id != preview.request_id or admission.decision_hash != preview.decision_hash
            or sha256_hex(command.delta.to_json()) != preview.delta_hash
            or sha256_hex(command.read_set.to_json()) != preview.read_set_hash):
        _raise("TASKGRAPH_APPLIED_DECISION_MISMATCH", "APPLIED must bind the compiled original decision")
    decision_bytes = decision["canonical_json"]
    if (not isinstance(decision_bytes, str)
            or canonical_json(json.loads(decision_bytes)) != decision_bytes
            or hashlib.sha256(decision_bytes.encode()).hexdigest() != preview.decision_hash):
        _raise("TASKGRAPH_APPLIED_DECISION_CORRUPT", "stored decision bytes differ from the admitted identity")
    active = store.connection.execute(
        "SELECT revision,snapshot_hash FROM plan_revisions WHERE mission_id=? AND state='ACTIVE'",
        (command.mission_id,)).fetchall()
    if len(active) != 1 or tuple(active[0]) != (receipt.new_plan_revision, receipt.output_identity["snapshot_hash"]):
        _raise("TASKGRAPH_APPLIED_PLAN_MISMATCH", "actual ACTIVE revision differs from commit result")
    identity = derive_id("tg-h1-applied", command.command_id)
    detail: dict[str, Any] = {"version": 1, "command_id": command.command_id,
        "commit_receipt_hash": sha256_hex(receipt.to_json()), "preview_hash": preview.canonical_hash(),
        "source_reads": [item.to_json() for item in preview.source_reads],
        "runtime_work_hash": checked.runtime_work_hash, "read_set_hash": preview.read_set_hash,
        "compilation_hash": admission.preview_compilation_hash}
    PlanningAdmissionStore(store).put_admission_check({
        "check_id": identity, "request_id": preview.request_id, "decision_id": preview.decision_id,
        "phase": "APPLIED", "snapshot_hash": preview.candidate_hash, "decision_hash": preview.decision_hash,
        "authority_hash": checked.authority_hash, "operations_hash": checked.operations_hash,
        "delta_hash": preview.delta_hash, "check_schema": "taskgraph-h1-applied/v1",
        "detail_json": canonical_json(detail),
        "checked_at_ms": int(store.now * 1000)})
    return identity


def _authority_identity(snapshot: PlanningAuthorizationSnapshot) -> dict[str, Any]:
    value = snapshot.to_json()
    value.pop("checked_at_ms", None)
    return value


def _raise(reason: str, detail: str) -> NoReturn:
    raise PlanCommitRejected(reason, detail)


def _check_planning_commit(
    *,
    store: Any,
    command: CommitPlanCommand,
    principal: PlanPrincipal,
    admission: PlanningCommitAdmission,
    taskgraph: TaskGraphPlanCommitParticipant | None = None,
) -> CheckedPlanningCommit:
    """Run all H1-H producer checks inside ``commit_plan_revision``'s transaction."""

    if admission.preview_request_id != admission.request_id:
        _raise("PREVIEW_IDENTITY_STALE", "preview request identity differs from admission")
    if admission.preview_decision_hash != admission.decision_hash:
        _raise("PREVIEW_IDENTITY_STALE", "preview decision identity differs from admission")
    expected_compilation_hash = sha256_hex(
        {
            "delta": command.delta.to_json(),
            "network": _source_snapshot_payload(command.network),
        }
    )
    if expected_compilation_hash != admission.preview_compilation_hash:
        _raise("PREVIEW_IDENTITY_STALE", "preview compilation identity changed")
    if sha256_hex(command.read_set.to_json()) != admission.preview_read_set_hash:
        _raise("PREVIEW_IDENTITY_STALE", "preview read-set identity changed")

    now_ms = int(store.now * 1000)
    try:
        authority = build_planning_authorization(
            admission.request_id,
            read=StorePlanningAuthorityReader(PlanningAdmissionStore(store), store),
            caller=principal,
            policy=planning_policy_for_mission(store, command.mission_id),
            now_ms=now_ms,
        )
    except Exception as error:  # pragma: no cover - producer wrapper is defensive
        _raise("SOURCE_UNAVAILABLE", f"planning authority read failed: {type(error).__name__}")
    if isinstance(authority, AuthoritySourceUnavailable):
        _raise(
            authority.reason_code
            if authority.reason_code in {"REQUEST_BINDING_STALE", "AUTHORIZATION_REQUIRED"}
            else "SOURCE_UNAVAILABLE",
            f"{authority.source}: {authority.detail}",
        )
    if not isinstance(authority, PlanningAuthorizationSnapshot):
        _raise("SOURCE_UNAVAILABLE", "planning authority snapshot is unreadable")
    if not isinstance(admission.authority, PlanningAuthorizationSnapshot):
        _raise("SOURCE_UNAVAILABLE", "preview authority snapshot is unreadable")
    if _authority_identity(authority) != _authority_identity(admission.authority):
        _raise("REQUEST_BINDING_STALE", "planning grant changed since preview")
    refusal = check_planning_authorization(
        authority, decision_key=admission.decision_key, now_ms=now_ms
    )
    if refusal is not None:
        _raise(refusal, "planning grant is inactive or expired at commit")

    try:
        operations = build_operation_snapshot(
            command.mission_id,
            reader=StoreOperationReader(store),
            now_ms=now_ms,
        )
    except OperationSourceUnavailable as error:
        _raise(
            "OPERATION_SNAPSHOT_STALE"
            if error.reason == "operation_mapping_incomplete"
            else "SOURCE_UNAVAILABLE",
            str(error),
        )
    if operations.read_digest != admission.operations.read_digest:
        _raise("OPERATION_SNAPSHOT_STALE", "operation bindings/links/actions changed since preview")
    if taskgraph is None:
        try:
            operation_gate(operations)
        except OperationSourceUnavailable as error:
            _raise("OPERATION_UNRESOLVED", str(error))
    else:
        from .taskgraph_plan_commit import TaskGraphPlanCommitParticipant
        from ..graph.planning_scope import planning_convergence_scope
        from ..runtime.taskgraph_operation_sources import read_operation_producers
        if (not isinstance(taskgraph, TaskGraphPlanCommitParticipant) or taskgraph.store is not store
                or admission.taskgraph_candidate is None
                or taskgraph.document != admission.taskgraph_candidate.document
                or taskgraph.preview != admission.taskgraph_candidate.preview):
            _raise("SOURCE_UNAVAILABLE", "TaskGraph original commit participant mismatch")
        if taskgraph.preview.base_revision == 0:
            if not operations.complete_empty:
                _raise("OPERATION_UNRESOLVED", "initial TaskGraph seed has no Operation producer network")
        else:
            before = taskgraph.history.read_revision(command.mission_id, taskgraph.preview.base_revision).record.document
            scope = planning_convergence_scope(before, taskgraph.document, operations,
                                               read_operation_producers(store, operations))
            if scope.unresolved_operations:
                _raise("OPERATION_UNRESOLVED", "affected Operations require original reconciliation")
        try:
            operation_gate(operations)
        except OperationSourceUnavailable as error:
            _raise("OPERATION_UNRESOLVED", str(error))

    try:
        runtime = read_running_work(
            command.mission_id,
            admission.runtime_work.retiring_instance_ids,
            reader=StoreOperationReader(
                store,
                ignore_intent_ids=(
                    str((command.source or {}).get("intent_id"))
                    if (command.source or {}).get("intent_id") is not None
                    else "",
                ),
            ),
        )
    except OperationSourceUnavailable as error:
        _raise("SOURCE_UNAVAILABLE", str(error))
    if runtime.snapshot_digest != admission.runtime_work.snapshot_digest:
        _raise("RUNTIME_WORK_STALE", "running work changed since preview")
    if runtime.requires_convergence and taskgraph is None:
        _raise("RUNNING_WORK_UNRESOLVED", "running work requires convergence before commit")
    return CheckedPlanningCommit(command_id=command.command_id, intent_hash=command.intent_hash(),
        authority_hash=sha256_hex(_authority_identity(authority)), operations_hash=operations.read_digest,
        runtime_work_hash=runtime.snapshot_digest)


class PlanningAdmissionCommitsMixin:
    """New-protocol commit entry; inherited by ``CommitService`` only."""

    def commit_planning_revision(
        self,
        command: CommitPlanCommand,
        principal: PlanPrincipal,
        *,
        admission: PlanningCommitAdmission,
        taskgraph: TaskGraphPlanCommitParticipant | None = None,
    ) -> Any:
        if not isinstance(admission, PlanningCommitAdmission):
            raise PlanCommitRejected("SOURCE_UNAVAILABLE", "missing H1-H commit admission")

        checked: CheckedPlanningCommit | None = None

        def guard() -> None:
            nonlocal checked
            checked = _check_planning_commit(
                store=self._store,  # type: ignore[attr-defined]
                command=command,
                principal=principal,
                admission=admission,
                taskgraph=taskgraph,
            )

        def write_applied(store: Any, written_command: CommitPlanCommand,
                          receipt: PlanCommitReceipt, preview: PreviewBindingV1) -> str:
            if store is not self._store or written_command != command or checked is None:  # type: ignore[attr-defined]
                _raise("TASKGRAPH_H1_COMMIT_CHECK_REQUIRED", "no successful original precommit check exists")
            return _write_taskgraph_applied(store, command, receipt, preview, admission, checked)

        with self._store.transaction():  # type: ignore[attr-defined]
            from .taskgraph_dispatch import taskgraph_enabled
            replay = self._store.connection.execute(  # type: ignore[attr-defined]
                "SELECT 1 FROM plan_commit_receipts WHERE command_id=?", (command.command_id,)).fetchone()
            if taskgraph is None and replay is None and taskgraph_enabled(self._store, command.mission_id):  # type: ignore[attr-defined]
                factory = getattr(self, "_taskgraph_participant_factory", None)
                if not callable(factory):
                    _raise("TASKGRAPH_COMMIT_PARTICIPANT_REQUIRED", "fixed TaskGraph admission assembly is missing")
                taskgraph = factory(command, principal, admission)
            if taskgraph is not None and replay is None:
                from .taskgraph_plan_commit import TaskGraphPlanCommitParticipant
                if not isinstance(taskgraph, TaskGraphPlanCommitParticipant):
                    _raise("TASKGRAPH_COMMIT_PARTICIPANT_REQUIRED", "fixed participant has the wrong type")
                taskgraph.bind_h1_commit(write_applied)
            return self.commit_plan_revision(  # type: ignore[attr-defined]
                command, principal, precommit_guard=guard, taskgraph=taskgraph
            )


__all__ = ("PlanningAdmissionCommitsMixin", "PlanningCommitAdmission")
