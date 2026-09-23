# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Durable fenced recovery of one already-produced planning decision.

This service never calls a model.  The Orchestrator supplies a synchronous local
resume callback that re-enters the existing pre-admission/preview/guarded-commit
chain with the frozen raw bytes and decision identity.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from simple_harness.contracts import canonical_json

from ..artifacts.store import ArtifactStore, ArtifactStoreError
from ..contracts.models import ContractError
from ..contracts.semantic_base import (
    TypedRef,
    TypedRefKind,
    fields_of,
    hash_hex,
    identifier,
    index,
)
from ..storage.htn_store import HtnStore
from ..storage.planning_admission_store import PlanningAdmissionStore
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.planning_repair_store import (
    PlanningRepairStore,
    StoredPlanningRepairContinuation,
)
from ..storage.store import Store, StoreConflict

POLICY_VERSION = "planning-repair-wait-v1"
DEADLINE_MS = 300_000
RESUME_LIMIT = 64
BACKOFF_MS = (1_000, 2_000, 4_000, 8_000, 16_000, 30_000)


class RepairContinuationError(ContractError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class RepairAuthorityBinding:
    grant_id: str
    grant_revision: int
    grant_hash: str
    policy_hash: str
    scope_id: str
    manager_epoch: int
    scope_epochs: tuple[tuple[str, int], ...]
    requirements_hash: str
    base_network_hash: str

    def to_json(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "grant_revision": self.grant_revision,
            "grant_hash": self.grant_hash,
            "policy_hash": self.policy_hash,
            "scope_id": self.scope_id,
            "manager_epoch": self.manager_epoch,
            "scope_epochs": [
                {"scope_id": scope_id, "epoch": epoch} for scope_id, epoch in self.scope_epochs
            ],
            "requirements_hash": self.requirements_hash,
            "base_network_hash": self.base_network_hash,
        }

    @classmethod
    def from_json(cls, value: object) -> RepairAuthorityBinding:
        data = fields_of(
            value,
            "repair_authority_binding",
            required=(
                "grant_id",
                "grant_revision",
                "grant_hash",
                "policy_hash",
                "scope_id",
                "manager_epoch",
                "scope_epochs",
                "requirements_hash",
                "base_network_hash",
            ),
        )
        epochs: list[tuple[str, int]] = []
        if not isinstance(data["scope_epochs"], list):
            raise ContractError("repair authority scope_epochs must be an array")
        for position, item in enumerate(data["scope_epochs"]):
            entry = fields_of(item, f"scope_epochs[{position}]", required=("scope_id", "epoch"))
            epochs.append(
                (
                    identifier(entry["scope_id"], "scope_id"),
                    index(entry["epoch"], "epoch"),
                )
            )
        if epochs != sorted(epochs) or len({item[0] for item in epochs}) != len(epochs):
            raise ContractError("repair authority scope_epochs must be unique and sorted")
        return cls(
            identifier(data["grant_id"], "grant_id"),
            index(data["grant_revision"], "grant_revision"),
            hash_hex(data["grant_hash"], "grant_hash"),
            hash_hex(data["policy_hash"], "policy_hash"),
            identifier(data["scope_id"], "scope_id"),
            index(data["manager_epoch"], "manager_epoch"),
            tuple(epochs),
            hash_hex(data["requirements_hash"], "requirements_hash"),
            hash_hex(data["base_network_hash"], "base_network_hash"),
        )


@dataclass(frozen=True, slots=True)
class RepairTarget:
    task_id: str
    occurrence_id: str
    contract_revision: int
    contract_hash: str
    input_binding_revision: int
    captured_dispatch_generation: int
    attempt_ids: tuple[str, ...]
    dispatch_intent_ids: tuple[str, ...]
    source_method_instance_ids: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            name: list(value) if isinstance(value, tuple) else value
            for name, value in (
                ("task_id", self.task_id),
                ("occurrence_id", self.occurrence_id),
                ("contract_revision", self.contract_revision),
                ("contract_hash", self.contract_hash),
                ("input_binding_revision", self.input_binding_revision),
                ("captured_dispatch_generation", self.captured_dispatch_generation),
                ("attempt_ids", self.attempt_ids),
                ("dispatch_intent_ids", self.dispatch_intent_ids),
                ("source_method_instance_ids", self.source_method_instance_ids),
            )
        }

    @classmethod
    def from_json(cls, value: object) -> RepairTarget:
        names = (
            "task_id",
            "occurrence_id",
            "contract_revision",
            "contract_hash",
            "input_binding_revision",
            "captured_dispatch_generation",
            "attempt_ids",
            "dispatch_intent_ids",
            "source_method_instance_ids",
        )
        data = fields_of(value, "repair_target", required=names)
        lists: list[tuple[str, ...]] = []
        for name in names[-3:]:
            raw = data[name]
            if not isinstance(raw, list):
                raise ContractError(f"repair target {name} must be an array")
            parsed = tuple(identifier(item, name) for item in raw)
            if parsed != tuple(sorted(set(parsed))):
                raise ContractError(f"repair target {name} must be unique and sorted")
            lists.append(parsed)
        return cls(
            identifier(data["task_id"], "task_id"),
            identifier(data["occurrence_id"], "occurrence_id"),
            index(data["contract_revision"], "contract_revision"),
            hash_hex(data["contract_hash"], "contract_hash"),
            index(data["input_binding_revision"], "input_binding_revision"),
            index(data["captured_dispatch_generation"], "captured_dispatch_generation"),
            *lists,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeferredPlanningRepair:
    continuation_id: str
    mission_id: str
    decision_id: str
    request_id: str
    command_id: str
    raw_artifact_ref: TypedRef
    raw_hash: str
    decision_hash: str
    codec_version: str
    package_hash: str
    prompt_hash: str
    base_plan_revision: int
    requirements_revision: int
    authority_binding: RepairAuthorityBinding
    targets: tuple[RepairTarget, ...]
    preview_hash: str
    authority_hash: str | None
    operations_hash: str | None
    delta_hash: str | None

    def __post_init__(self) -> None:
        for name in ("continuation_id", "mission_id", "decision_id", "request_id", "command_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        if not isinstance(self.raw_artifact_ref, TypedRef):
            raise ContractError("raw_artifact_ref must be a TypedRef")
        for name in ("raw_hash", "decision_hash", "package_hash", "prompt_hash", "preview_hash"):
            object.__setattr__(self, name, hash_hex(getattr(self, name), name))
        object.__setattr__(self, "codec_version", identifier(self.codec_version, "codec_version"))
        object.__setattr__(
            self, "base_plan_revision", index(self.base_plan_revision, "base_plan_revision")
        )
        object.__setattr__(
            self,
            "requirements_revision",
            index(self.requirements_revision, "requirements_revision"),
        )
        if not isinstance(self.authority_binding, RepairAuthorityBinding):
            raise ContractError("authority_binding must be RepairAuthorityBinding")
        object.__setattr__(self, "targets", _validate_targets(self.targets))
        for name in ("authority_hash", "operations_hash", "delta_hash"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, hash_hex(value, name))


class ResumeState(StrEnum):
    WAITING = "WAITING"
    READY = "READY"
    APPLIED = "APPLIED"
    STALE_FENCED = "STALE_FENCED"


@dataclass(frozen=True, slots=True)
class RepairResumeResult:
    state: ResumeState
    reason_code: str | None = None
    preview_hash: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedPlanningRepair:
    continuation: StoredPlanningRepairContinuation
    raw_bytes: bytes
    authority_binding: RepairAuthorityBinding
    targets: tuple[RepairTarget, ...]


ResumeCallback = Callable[[PreparedPlanningRepair], RepairResumeResult]


def _validate_targets(targets: Sequence[RepairTarget]) -> tuple[RepairTarget, ...]:
    values = tuple(targets)
    identities = [(item.task_id, item.occurrence_id) for item in values]
    if not values or identities != sorted(identities) or len(set(identities)) != len(identities):
        raise RepairContinuationError(
            "REPAIR_SOURCE_UNAVAILABLE", "targets must be unique and sorted"
        )
    return values


def _target_dispatch_intents(
    store: Store, mission_id: str, task_id: str, attempt_ids: tuple[str, ...]
) -> tuple[str, ...]:
    subjects = {task_id, *attempt_ids}
    found: list[str] = []
    for row in store.connection.execute(
        "SELECT intent_id, subject_id, config_json FROM dispatch_intents "
        "WHERE mission_id=? ORDER BY intent_id",
        (mission_id,),
    ).fetchall():
        config = json.loads(row["config_json"])
        if (
            row["subject_id"] in subjects
            or config.get("task_id") == task_id
            or config.get("attempt_id") in attempt_ids
        ):
            found.append(str(row["intent_id"]))
    return tuple(found)


def _validate_target_sources(
    store: Store,
    *,
    mission_id: str,
    plan_revision: int,
    targets: tuple[RepairTarget, ...],
) -> None:
    htn = HtnStore(store)
    active = htn.active_plan_revision(mission_id)
    if active is None or int(active.revision) != int(plan_revision):
        raise RepairContinuationError("REQUEST_BINDING_STALE", "base Plan is no longer active")
    members = {
        (str(item.task_id), str(item.occurrence_id)): item
        for item in htn.list_plan_memberships(mission_id, plan_revision)
    }
    for target in targets:
        if (target.task_id, target.occurrence_id) not in members:
            raise RepairContinuationError(
                "REPAIR_SOURCE_UNAVAILABLE", "repair target is absent from the base Plan"
            )
        binding = htn.task_semantics_of(mission_id, target.task_id)
        if (
            binding is None
            or int(binding.contract_revision) != target.contract_revision
            or binding.contract_hash != target.contract_hash
            or int(binding.input_binding_revision) != target.input_binding_revision
            or int(binding.dispatch_generation) != target.captured_dispatch_generation
        ):
            raise RepairContinuationError(
                "REQUEST_BINDING_STALE", "repair target Task binding differs"
            )
        attempts = tuple(sorted(item.id for item in store.list_attempts(target.task_id)))
        if attempts != target.attempt_ids:
            raise RepairContinuationError(
                "REPAIR_SOURCE_UNAVAILABLE", "repair target Attempt set is incomplete"
            )
        intents = _target_dispatch_intents(store, mission_id, target.task_id, target.attempt_ids)
        if intents != target.dispatch_intent_ids:
            raise RepairContinuationError(
                "REPAIR_SOURCE_UNAVAILABLE", "repair target dispatch-intent set is incomplete"
            )
        known_instances = {str(item.instance_id) for item in htn.list_method_instances(mission_id)}
        for instance_id in target.source_method_instance_ids:
            if instance_id not in known_instances:
                raise RepairContinuationError(
                    "REPAIR_SOURCE_UNAVAILABLE", "repair source Method instance is absent"
                )


def capture_repair_targets(
    store: Store,
    *,
    mission_id: str,
    plan_revision: int,
    blocking_attempt_ids: Sequence[str],
    source_method_instance_ids: Sequence[str],
    additional_task_ids: Sequence[str] = (),
) -> tuple[RepairTarget, ...]:
    """Capture the complete Task execution set around real blocking Attempts."""

    htn = HtnStore(store)
    members = htn.list_plan_memberships(mission_id, plan_revision)
    occurrence_by_task: dict[str, list[str]] = {}
    for member in members:
        occurrence_by_task.setdefault(str(member.task_id), []).append(str(member.occurrence_id))
    methods = tuple(sorted(set(str(item) for item in source_method_instance_ids)))
    member_tasks = {str(member.occurrence_id): str(member.task_id) for member in members}
    # Fence every child of the retiring instance, including Tasks which have not
    # started an Attempt yet. A list of currently running Attempts is not the scope.
    tasks: set[str] = set(additional_task_ids)
    pending_methods = list(methods)
    visited: set[str] = set()
    while pending_methods:
        instance_id = pending_methods.pop()
        if instance_id in visited:
            continue
        visited.add(instance_id)
        children = htn.list_child_occurrences(mission_id, instance_id)
        for child in children:
            task_id = member_tasks.get(str(child.occurrence_id))
            if task_id is None:
                raise RepairContinuationError(
                    "REPAIR_SOURCE_UNAVAILABLE", "retired child has no active Task"
                )
            tasks.add(task_id)
            for instance in htn.list_method_instances(mission_id, state="ADOPTED"):
                if str(instance.effective_goal_occurrence_id) == str(child.occurrence_id):
                    pending_methods.append(str(instance.instance_id))
    for attempt_id in blocking_attempt_ids:
        attempt = store.get_attempt(str(attempt_id))
        if attempt is None or attempt.mission_id != mission_id:
            raise RepairContinuationError(
                "REPAIR_SOURCE_UNAVAILABLE", "blocking Attempt is unavailable"
            )
        # Mission-wide execution snapshots may include unrelated sibling work;
        # it remains visible to admission but does not widen this repair's fence.
        if attempt.task_id not in tasks:
            continue
    targets: list[RepairTarget] = []
    for task_id in sorted(tasks):
        occurrences = occurrence_by_task.get(task_id, [])
        binding = htn.task_semantics_of(mission_id, task_id)
        if not occurrences or binding is None:
            raise RepairContinuationError(
                "REPAIR_SOURCE_UNAVAILABLE", "blocking Task has no active occurrence or binding")
        attempts = tuple(sorted(item.id for item in store.list_attempts(task_id)))
        intents = _target_dispatch_intents(store, mission_id, task_id, attempts)
        for occurrence in sorted(occurrences):
            targets.append(RepairTarget(task_id=task_id, occurrence_id=occurrence,
                contract_revision=int(binding.contract_revision), contract_hash=binding.contract_hash,
                input_binding_revision=int(binding.input_binding_revision),
                captured_dispatch_generation=int(binding.dispatch_generation),
                attempt_ids=attempts, dispatch_intent_ids=intents, source_method_instance_ids=methods))
    values = _validate_targets(targets)
    _validate_target_sources(
        store, mission_id=mission_id, plan_revision=plan_revision, targets=values
    )
    return values


def defer_planning_repair(
    store: Store, source: DeferredPlanningRepair, *, now_ms: int
) -> StoredPlanningRepairContinuation:
    """Atomically freeze the original decision and install its stop gate."""

    targets = _validate_targets(source.targets)
    if source.raw_artifact_ref.kind is not TypedRefKind.ARTIFACT:
        raise RepairContinuationError(
            "REPAIR_SOURCE_UNAVAILABLE", "raw source must be CAS artifact"
        )
    decisions = PlanningDecisionStore(store)
    with store.transaction():
        request = decisions.get_planning_request(source.request_id)
        decision = decisions.get_planning_decision(source.decision_id)
        if request is None or decision is None or decision["request_id"] != source.request_id:
            raise RepairContinuationError("REPAIR_SOURCE_UNAVAILABLE", "request/decision is absent")
        if (
            request.mission_id != source.mission_id
            or request.protocol_version != "planning-decision-v1"
            or request.package_hash != source.package_hash
            or request.prompt_hash != source.prompt_hash
            or request.base_plan_revision != source.base_plan_revision
            or request.requirements_revision != source.requirements_revision
            or decision["raw_output_hash"] != source.raw_hash
            or decision.get("canonical_hash") != source.decision_hash
        ):
            raise RepairContinuationError(
                "REQUEST_BINDING_STALE", "frozen request identity differs"
            )
        _validate_target_sources(
            store,
            mission_id=source.mission_id,
            plan_revision=source.base_plan_revision,
            targets=targets,
        )
        created = int(now_ms)
        value = StoredPlanningRepairContinuation(
            source.continuation_id,
            source.mission_id,
            source.decision_id,
            source.request_id,
            source.command_id,
            source.raw_artifact_ref.to_json(),
            source.raw_hash,
            source.decision_hash,
            source.codec_version,
            "planning-decision-v1",
            source.package_hash,
            source.prompt_hash,
            source.base_plan_revision,
            source.requirements_revision,
            source.authority_binding.to_json(),
            tuple(item.to_json() for item in targets),
            source.preview_hash,
            "WAITING",
            1,
            POLICY_VERSION,
            created,
            created + DEADLINE_MS,
            RESUME_LIMIT,
            0,
            created + BACKOFF_MS[0],
            None,
            None,
            None,
            created,
        )
        stored = PlanningRepairStore(store).put(value)
        PlanningAdmissionStore(store).put_admission_check(
            {
                "check_id": f"repair-deferred:{source.continuation_id}",
                "request_id": source.request_id,
                "decision_id": source.decision_id,
                "phase": "DEFERRED",
                "snapshot_hash": source.preview_hash,
                "decision_hash": source.decision_hash,
                "authority_hash": source.authority_hash,
                "operations_hash": source.operations_hash,
                "delta_hash": source.delta_hash,
                "check_schema": POLICY_VERSION,
                "detail_json": canonical_json(
                    {
                        "continuation_id": source.continuation_id,
                        "targets": [item.to_json() for item in targets],
                    }
                ),
                "checked_at_ms": created,
            }
        )
        from .hierarchical_dispatch import append_hierarchical_event

        append_hierarchical_event(
            store,
            "PlanningRepairDeferred",
            source.mission_id,
            key=source.continuation_id,
            payload={
                "continuation_id": source.continuation_id,
                "decision_id": source.decision_id,
                "state": stored.state,
                "target_task_ids": [target.task_id for target in targets],
                "deadline_ms": stored.deadline_ms,
            },
        )
        return stored


def planning_repair_stop_gate(store: Store, mission_id: str, task_id: str) -> bool:
    """True when a durable continuation temporarily revokes new dispatch/handoff."""

    task = str(task_id)
    for continuation in PlanningRepairStore(store).list_active_fences(mission_id):
        targets = tuple(RepairTarget.from_json(item) for item in continuation.targets)
        if any(item.task_id == task for item in targets):
            return True
    return False


def _raw_bytes(cas: ArtifactStore, continuation: StoredPlanningRepairContinuation) -> bytes:
    try:
        ref = TypedRef.from_json(continuation.raw_artifact_ref, "repair.raw_artifact_ref")
        if ref.kind is not TypedRefKind.ARTIFACT or ref.content_hash != continuation.raw_hash:
            raise ContractError("raw artifact identity differs")
        return cas.read(ref.content_hash)
    except (ArtifactStoreError, ContractError, OSError) as error:
        raise RepairContinuationError("REPAIR_SOURCE_UNAVAILABLE", str(error)) from error


def _prepare_resume(
    store: Store, cas: ArtifactStore, continuation: StoredPlanningRepairContinuation
) -> PreparedPlanningRepair:
    try:
        authority = RepairAuthorityBinding.from_json(continuation.authority_binding)
        targets = _validate_targets(
            tuple(RepairTarget.from_json(item) for item in continuation.targets)
        )
    except (ContractError, RepairContinuationError) as error:
        raise RepairContinuationError("REPAIR_SOURCE_UNAVAILABLE", str(error)) from error
    decisions = PlanningDecisionStore(store)
    request = decisions.get_planning_request(continuation.request_id)
    decision = decisions.get_planning_decision(continuation.decision_id)
    if (
        request is None
        or decision is None
        or request.mission_id != continuation.mission_id
        or request.protocol_version != continuation.protocol_version
        or request.package_hash != continuation.package_hash
        or request.prompt_hash != continuation.prompt_hash
        or request.base_plan_revision != continuation.base_plan_revision
        or request.requirements_revision != continuation.requirements_revision
        or decision["request_id"] != continuation.request_id
        or decision["raw_output_hash"] != continuation.raw_hash
        or decision.get("canonical_hash") != continuation.decision_hash
    ):
        raise RepairContinuationError(
            "REPAIR_SOURCE_UNAVAILABLE", "persisted request/decision identity differs"
        )
    return PreparedPlanningRepair(continuation, _raw_bytes(cas, continuation), authority, targets)


def resume_pending_planning_repairs(
    store: Store,
    cas: ArtifactStore,
    *,
    owner_id: str,
    now_ms: int,
    resume: ResumeCallback,
    lease_ms: int = 30_000,
    limit: int = 8,
) -> tuple[StoredPlanningRepairContinuation, ...]:
    """Claim and locally re-evaluate due continuations without asking a model."""

    repaired: list[StoredPlanningRepairContinuation] = []
    records = PlanningRepairStore(store)
    for _ in range(max(0, int(limit))):
        current = records.claim_due(
            owner_id=owner_id, now_ms=now_ms, lease_until_ms=now_ms + max(1, int(lease_ms))
        )
        if current is None:
            break
        count = current.resume_count + 1
        if now_ms >= current.deadline_ms or count > current.resume_limit:
            repaired.append(
                records.transition(
                    current,
                    owner_id=owner_id,
                    new_state="MANUAL_REQUIRED",
                    now_ms=now_ms,
                    next_check_at_ms=current.next_check_at_ms,
                    resume_count=current.resume_count,
                    reason="REPAIR_MANUAL_REQUIRED",
                )
            )
            continue
        try:
            prepared = _prepare_resume(store, cas, current)
        except RepairContinuationError as error:
            repaired.append(
                records.transition(
                    current,
                    owner_id=owner_id,
                    new_state="MANUAL_REQUIRED",
                    now_ms=now_ms,
                    next_check_at_ms=current.next_check_at_ms,
                    resume_count=count,
                    reason=error.code,
                )
            )
            continue
        # The callback and APPLIED transition share the Store transaction.  It must
        # contain only local admission/commit work and must never call a provider.
        with store.transaction():
            result = resume(prepared)
            if not isinstance(result, RepairResumeResult):
                raise StoreConflict("planning repair resume returned no typed result")
            state = str(result.state)
            resume_transitions = {
                "WAITING": {"WAITING", "READY", "APPLIED", "STALE_FENCED"},
                "READY": {"READY", "APPLIED", "STALE_FENCED"},
            }
            if state not in resume_transitions.get(current.state, set()):
                raise StoreConflict("planning repair callback requested an illegal transition")
            if state == "APPLIED":
                decision = PlanningDecisionStore(store).get_planning_decision(current.decision_id)
                if decision is None or decision["status"] != "COMMITTED":
                    raise StoreConflict(
                        "planning repair cannot release its fence before Decision COMMITTED"
                    )
            if result.preview_hash is not None:
                hash_hex(result.preview_hash, "repair_resume.preview_hash")
            delay = BACKOFF_MS[min(count - 1, len(BACKOFF_MS) - 1)]
            repaired.append(
                records.transition(
                    current,
                    owner_id=owner_id,
                    new_state=state,
                    now_ms=now_ms,
                    next_check_at_ms=now_ms + delay,
                    resume_count=count,
                    reason=result.reason_code,
                    preview_hash=result.preview_hash,
                )
            )
    return tuple(repaired)


def claim_due_planning_repair(
    store: Store,
    cas: ArtifactStore,
    *,
    owner_id: str,
    now_ms: int,
    lease_ms: int = 30_000,
) -> PreparedPlanningRepair | None:
    """Claim one due row for an async Orchestrator, without holding a transaction."""

    current = PlanningRepairStore(store).claim_due(
        owner_id=owner_id,
        now_ms=now_ms,
        lease_until_ms=now_ms + max(1, int(lease_ms)),
    )
    if current is None:
        return None
    count = current.resume_count + 1
    if now_ms >= current.deadline_ms or count > current.resume_limit:
        PlanningRepairStore(store).transition(
            current,
            owner_id=owner_id,
            new_state="MANUAL_REQUIRED",
            now_ms=now_ms,
            next_check_at_ms=current.next_check_at_ms,
            resume_count=current.resume_count,
            reason="REPAIR_MANUAL_REQUIRED",
        )
        return None
    try:
        return _prepare_resume(store, cas, current)
    except RepairContinuationError as error:
        PlanningRepairStore(store).transition(
            current,
            owner_id=owner_id,
            new_state="MANUAL_REQUIRED",
            now_ms=now_ms,
            next_check_at_ms=current.next_check_at_ms,
            resume_count=count,
            reason=error.code,
        )
        return None


def settle_claimed_planning_repair(
    store: Store,
    prepared: PreparedPlanningRepair,
    *,
    owner_id: str,
    now_ms: int,
    result: RepairResumeResult,
) -> StoredPlanningRepairContinuation:
    """CAS-settle one async resume after the original collector/commit returned."""

    current = prepared.continuation
    state = str(result.state)
    allowed = {
        "WAITING": {"WAITING", "READY", "APPLIED", "STALE_FENCED"},
        "READY": {"READY", "APPLIED", "STALE_FENCED"},
    }
    if state not in allowed.get(current.state, set()):
        raise StoreConflict("planning repair callback requested an illegal transition")
    if state == "APPLIED":
        decision = PlanningDecisionStore(store).get_planning_decision(current.decision_id)
        if decision is None or decision["status"] != "COMMITTED":
            raise StoreConflict("planning repair Decision is not COMMITTED")
    count = current.resume_count + 1
    delay = BACKOFF_MS[min(count - 1, len(BACKOFF_MS) - 1)]
    return PlanningRepairStore(store).transition(
        current,
        owner_id=owner_id,
        new_state=state,
        now_ms=now_ms,
        next_check_at_ms=now_ms + delay,
        resume_count=count,
        reason=result.reason_code,
        preview_hash=result.preview_hash,
    )


def set_planning_repair_state(
    store: Store,
    continuation_id: str,
    *,
    new_state: str,
    now_ms: int,
    reason: str,
    close_disposition: str | None = None,
) -> StoredPlanningRepairContinuation:
    """Pause, stale-fence, or safely close a continuation by system command."""

    with store.transaction():
        records = PlanningRepairStore(store)
        current = records.get(continuation_id)
        if current is None:
            raise RepairContinuationError("REPAIR_SOURCE_UNAVAILABLE", "continuation is absent")
        allowed = {
            "WAITING": {"PAUSED", "MANUAL_REQUIRED", "STALE_FENCED"},
            "READY": {"PAUSED", "MANUAL_REQUIRED", "STALE_FENCED"},
            "PAUSED": {"WAITING", "READY", "MANUAL_REQUIRED", "STALE_FENCED", "CLOSED"},
            "MANUAL_REQUIRED": {"STALE_FENCED", "CLOSED"},
            "STALE_FENCED": {"CLOSED"},
        }
        if new_state not in allowed.get(current.state, set()):
            raise RepairContinuationError(
                "REPAIR_SOURCE_UNAVAILABLE", "illegal continuation transition"
            )
        if new_state == "CLOSED":
            from ..contracts import TERMINAL_ATTEMPT, TaskStatus
            from ..runtime.planning_operations import StoreOperationReader, read_running_work

            if close_disposition not in {"RESTORE_OLD_DEMAND", "PERMANENTLY_REVOKE"}:
                raise RepairContinuationError(
                    "REPAIR_CLOSE_DISPOSITION_REQUIRED",
                    "closing a fence requires an explicit demand disposition",
                )
            targets = tuple(RepairTarget.from_json(item) for item in current.targets)
            methods = tuple(
                sorted(
                    {method for target in targets for method in target.source_method_instance_ids}
                )
            )
            running = read_running_work(
                current.mission_id, methods, reader=StoreOperationReader(store)
            )
            if running.requires_convergence or any(
                attempt.status not in TERMINAL_ATTEMPT
                for target in targets
                for attempt in store.list_attempts(target.task_id)
            ):
                raise RepairContinuationError(
                    "REPAIR_RUNNING_WORK_UNRESOLVED",
                    "closing cannot discard live or unknown execution",
                )
            if close_disposition == "RESTORE_OLD_DEMAND":
                _validate_target_sources(
                    store,
                    mission_id=current.mission_id,
                    plan_revision=current.base_plan_revision,
                    targets=targets,
                )
            else:
                htn = HtnStore(store)
                active = htn.active_plan_revision(current.mission_id)
                active_tasks = (
                    set()
                    if active is None
                    else {
                        str(member.task_id)
                        for member in htn.list_plan_memberships(current.mission_id, active.revision)
                    }
                )
                if any(
                    target.task_id in active_tasks
                    and (
                        (task := store.get_task(target.task_id)) is None
                        or task.status is not TaskStatus.CANCELLED
                    )
                    for target in targets
                ):
                    raise RepairContinuationError(
                        "REPAIR_DEMAND_STILL_ACTIVE",
                        "permanent revocation needs the original demand to be retired or cancelled",
                    )
        return records.administrative_transition(
            current,
            new_state=new_state,
            now_ms=now_ms,
            next_check_at_ms=(
                now_ms if new_state in {"WAITING", "READY"} else current.next_check_at_ms
            ),
            reason=reason,
        )


__all__ = (
    "DeferredPlanningRepair",
    "PreparedPlanningRepair",
    "RepairAuthorityBinding",
    "RepairContinuationError",
    "RepairResumeResult",
    "RepairTarget",
    "ResumeState",
    "claim_due_planning_repair",
    "capture_repair_targets",
    "defer_planning_repair",
    "planning_repair_stop_gate",
    "resume_pending_planning_repairs",
    "settle_claimed_planning_repair",
    "set_planning_repair_state",
)
