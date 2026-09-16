# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501  (SQL statement literals)

"""``HtnStore``: the durable side of the full-target semantic network.

Everything here is *storage*, not judgement.  The store writes and reads the P1.1
contract objects as canonical JSON next to the columns the orchestrator queries or
guards, exactly like :class:`~agent_orchestrator.storage.store.Store` does for the
legacy entities.  What it does enforce is identity and atomicity:

* one ACTIVE plan revision per Mission (:meth:`HtnStore.activate_plan_revision`),
* one official review record per package, one adopted resolution per obligation,
* one ``request_hash`` per ``OperationId`` (AER §14.3 payload conflict),
* an idempotent plan commit receipt (:meth:`HtnStore.record_commit_receipt`).

Whether a plan *should* be committed — coverage, cycles, budget — is decided in
P2.3a's ``plan_commits``; this module would happily store a plan that fails those
rules, and says so on purpose so no caller mistakes persistence for admission.

The store owns no connection: it composes an existing ``Store`` and runs inside
that store's ``transaction()``, so a write here rolls back together with whatever
the Commit Service wrote beside it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.evidence_state import ObservationRecord, ValidityWitness
from ..contracts.htn import (
    BoundInput,
    ChildBinding,
    DataRequirement,
    MethodContract,
    MethodInstanceDraft,
    MethodRegistration,
    MethodRegistryStatus,
    OccurrenceSpec,
    OrderConstraint,
    SemanticReadSet,
    TaskSemanticBindingV1,
)
from ..contracts.resolution import (
    Acceptance,
    DeliveryReceipt,
    GoalResolution,
    OperationEnvelope,
    RequirementsRevision,
    ReviewPackage,
    ReviewPurpose,
    ReviewRecord,
    account_for_purpose,
)
from ..contracts.semantic_base import TypedRef, content_hash_of, enum_of, identifier, index
from ..knowledge.validity import witness_subject
from .store import Store, StoreConflict

#: A plan revision is PREPARED until it is adopted, ACTIVE while it is the plan
#: being scheduled, RETIRED once another revision replaced it (TG §7.1).
PLAN_REVISION_STATES = ("PREPARED", "ACTIVE", "RETIRED")
#: A method instance is DRAFT until a plan revision adopts it, RETIRED when the
#: plan stops using it; the row is never deleted (§6.4 keeps the history).
METHOD_INSTANCE_STATES = ("DRAFT", "ADOPTED", "RETIRED")
DIRTY_STATES = ("PENDING", "RECHECKING", "CLEARED")
#: What one accept-side command produced (§25.1 decision 4 keeps the two apart all
#: the way into the receipt).  Mirrors the CHECK on ``acceptance_commit_receipts``.
ACCEPTANCE_RECEIPT_KINDS = ("acceptance", "goal_resolution")


@dataclass(frozen=True, slots=True)
class AcceptanceCommitReceipt:
    """§17.4: what one ``accept_review`` / ``commit_goal_resolution`` command did.

    Not a :class:`PlanCommitReceipt`: that table is plan-shaped and its CHECK
    requires a new revision above the base, while an acceptance produces no plan
    revision at all.  ``kind`` stays on the receipt because accepting a contribution
    and resolving a goal are two actions and a replay must be told which it was.
    """

    mission_id: str
    command_id: str
    kind: str
    subject_id: str
    intent_hash: str
    read_set_hash: str
    event_id: str
    output_identity: Mapping[str, Any] = field(default_factory=dict)
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "command_id": self.command_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "intent_hash": self.intent_hash,
            "read_set_hash": self.read_set_hash,
            "event_id": self.event_id,
            "output_identity": dict(self.output_identity),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class StoredMethod:
    """A method definition together with the registry record that admitted it."""

    contract: MethodContract
    registration: MethodRegistration


@dataclass(frozen=True, slots=True)
class StoredPlanRevision:
    """One Mission plan revision and its lifecycle state."""

    mission_id: str
    revision: int
    state: str
    base_revision: int | None
    snapshot_hash: str
    delta_id: str | None
    read_set: SemanticReadSet


@dataclass(frozen=True, slots=True)
class StoredReviewRecord:
    """A review judgement plus whether it is *the* official one for its package."""

    record: ReviewRecord
    official: bool


@dataclass(frozen=True, slots=True)
class StoredJustificationSet:
    """One support set of a subject, with its members and their polarity."""

    set_id: str
    mission_id: str
    subject_kind: str
    subject_id: str
    member_revision: int
    member_digest: str
    rule_ref: str | None
    members: tuple[tuple[TypedRef, bool], ...]
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PlanCommitReceipt:
    """What one plan commit command produced: the intent, what it read, what it made."""

    command_id: str
    mission_id: str
    delta_id: str
    base_plan_revision: int
    new_plan_revision: int
    intent_hash: str
    read_set_hash: str
    read_set: SemanticReadSet
    output_identity: dict[str, Any]
    detail: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "mission_id": self.mission_id,
            "delta_id": self.delta_id,
            "base_plan_revision": self.base_plan_revision,
            "new_plan_revision": self.new_plan_revision,
            "intent_hash": self.intent_hash,
            "read_set_hash": self.read_set_hash,
            "read_set": self.read_set.to_json(),
            "output_identity": dict(self.output_identity),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class DirtyEntry:
    """One subject whose validity must be rechecked before it may be used again."""

    mission_id: str
    subject_kind: str
    subject_id: str
    epoch: int
    scope_id: str
    reason: str
    state: str


class HtnStore:
    """Insert / get / list for the full-target tables, in contract objects."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ================================================================== task semantics
    def put_task_semantics(self, mission_id: str, binding: TaskSemanticBindingV1) -> str:
        """Store one semantic binding.  ``(task_id, contract_revision)`` is its identity."""

        if not isinstance(binding, TaskSemanticBindingV1):
            raise StoreConflict("put_task_semantics expects a TaskSemanticBindingV1")
        mission = identifier(mission_id, "mission_id")
        digest = binding.content_hash()
        self._insert(
            "INSERT INTO task_semantics(task_id,binding_revision,mission_id,obligation_id,form,"
            "semantic_scope,goal_signature_id,operator_ref,adopted_method_instance_id,"
            "input_binding_revision,dispatch_generation,content_hash,binding_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(binding.task_id),
                int(binding.contract_revision),
                mission,
                str(binding.obligation_id),
                str(binding.form),
                binding.semantic_scope,
                binding.goal_signature.signature_id,
                None
                if binding.operator_ref is None
                else canonical_json(binding.operator_ref.to_json()),
                (
                    None
                    if binding.adopted_method_instance_id is None
                    else str(binding.adopted_method_instance_id)
                ),
                int(binding.input_binding_revision),
                int(binding.dispatch_generation),
                digest,
                canonical_json(binding.to_json()),
                self._store.now,
            ),
            f"task semantics {binding.task_id!s}@{binding.contract_revision} already stored",
        )
        return digest

    def get_task_semantics(self, task_id: str, binding_revision: int) -> TaskSemanticBindingV1:
        row = self._one(
            "SELECT binding_json FROM task_semantics WHERE task_id = ? AND binding_revision = ?",
            (identifier(task_id, "task_id"), index(binding_revision, "binding_revision")),
            f"no semantic binding for task {task_id}@{binding_revision}",
        )
        return TaskSemanticBindingV1.from_json(json.loads(row[0]))

    def latest_task_semantics(self, task_id: str) -> TaskSemanticBindingV1 | None:
        row = self._store.connection.execute(
            "SELECT binding_json FROM task_semantics WHERE task_id = ?"
            " ORDER BY binding_revision DESC LIMIT 1",
            (identifier(task_id, "task_id"),),
        ).fetchone()
        return None if row is None else TaskSemanticBindingV1.from_json(json.loads(row[0]))

    def task_semantics_of(self, mission_id: str, task_id: str) -> TaskSemanticBindingV1 | None:
        """The newest binding of one task *within one Mission*.

        This is the accessor a commit path wants: it keeps the Mission in the
        predicate, so nothing outside ``storage`` has to spell the table name in
        SQL of its own to read a task's meaning.
        """

        row = self._store.connection.execute(
            "SELECT binding_json FROM task_semantics WHERE mission_id = ? AND task_id = ?"
            " ORDER BY binding_revision DESC LIMIT 1",
            (identifier(mission_id, "mission_id"), identifier(task_id, "task_id")),
        ).fetchone()
        return None if row is None else TaskSemanticBindingV1.from_json(json.loads(row[0]))

    def list_task_semantics(
        self, mission_id: str, *, form: str | None = None
    ) -> tuple[TaskSemanticBindingV1, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if form is not None:
            clauses.append("form = ?")
            values.append(str(form))
        rows = self._store.connection.execute(
            f"SELECT binding_json FROM task_semantics WHERE {' AND '.join(clauses)}"
            " ORDER BY task_id, binding_revision",
            tuple(values),
        ).fetchall()
        return tuple(TaskSemanticBindingV1.from_json(json.loads(row[0])) for row in rows)

    # ================================================================== method registry
    def register_method(
        self, contract: MethodContract, registration: MethodRegistration
    ) -> StoredMethod:
        """Store an immutable method definition with its registry record (§6.3, §7.3).

        Re-registering the same ``(method_id, version)`` with the same bytes returns
        what is stored; different bytes are a conflict, because a method definition
        is immutable and a second meaning would silently rewrite past decompositions.
        """

        if not isinstance(contract, MethodContract):
            raise StoreConflict("register_method expects a MethodContract")
        if not isinstance(registration, MethodRegistration):
            raise StoreConflict("register_method expects a MethodRegistration")
        reference = contract.method_ref()
        if (
            registration.method_ref.method_id != reference.method_id
            or registration.method_ref.version != reference.version
            or registration.method_ref.content_hash != reference.content_hash
        ):
            raise StoreConflict(
                "the registration does not describe this method definition "
                f"({registration.method_ref.to_json()} vs {reference.to_json()})"
            )
        stored = self._method_row(reference.method_id, reference.version)
        if stored is not None:
            if stored["content_hash"] != reference.content_hash:
                raise StoreConflict(
                    f"method {reference.method_id}@{reference.version} is already stored "
                    "with different content; a changed definition needs a new version"
                )
            return self._stored_method(stored)
        now = self._store.now
        self._insert(
            "INSERT INTO method_contracts(method_id,method_version,content_hash,registry_status,"
            "author,trial_scope_mission,registration_json,contract_json,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                reference.method_id,
                reference.version,
                reference.content_hash,
                str(registration.status),
                str(registration.author),
                registration.trial_scope_mission,
                canonical_json(registration.to_json()),
                canonical_json(contract.to_json()),
                now,
                now,
            ),
            f"method {reference.method_id}@{reference.version} already stored",
        )
        return StoredMethod(contract=contract, registration=registration)

    def set_method_registration(self, registration: MethodRegistration) -> StoredMethod:
        """Promote / suspend a stored method.  The definition itself never changes."""

        if not isinstance(registration, MethodRegistration):
            raise StoreConflict("set_method_registration expects a MethodRegistration")
        reference = registration.method_ref
        stored = self._method_row(reference.method_id, reference.version)
        if stored is None:
            raise StoreConflict(f"method {reference.method_id}@{reference.version} is not stored")
        if stored["content_hash"] != reference.content_hash:
            raise StoreConflict(
                f"method {reference.method_id}@{reference.version} has a different content hash"
            )
        with self._store.transaction() as connection:
            try:
                connection.execute(
                    "UPDATE method_contracts SET registry_status = ?, author = ?,"
                    " trial_scope_mission = ?, registration_json = ?, updated_at = ?"
                    " WHERE method_id = ? AND method_version = ?",
                    (
                        str(registration.status),
                        str(registration.author),
                        registration.trial_scope_mission,
                        canonical_json(registration.to_json()),
                        self._store.now,
                        reference.method_id,
                        reference.version,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(f"registry status rejected: {error}") from error
        return self.get_method(reference.method_id, reference.version)

    def get_method(self, method_id: str, method_version: int) -> StoredMethod:
        row = self._method_row(
            identifier(method_id, "method_id"), index(method_version, "method_version", minimum=1)
        )
        if row is None:
            raise StoreConflict(f"method {method_id}@{method_version} is not stored")
        return self._stored_method(row)

    def list_methods(
        self, *, status: MethodRegistryStatus | None = None
    ) -> tuple[StoredMethod, ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            clauses.append("registry_status = ?")
            values.append(str(enum_of(MethodRegistryStatus, status, "status")))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._store.connection.execute(
            f"SELECT * FROM method_contracts{where} ORDER BY method_id, method_version",
            tuple(values),
        ).fetchall()
        return tuple(self._stored_method(row) for row in rows)

    # ================================================================== method instances
    def insert_method_instance(
        self, mission_id: str, draft: MethodInstanceDraft, *, state: str = "DRAFT"
    ) -> MethodInstanceDraft:
        """Store one grounding and, in the same transaction, its child occurrences."""

        if not isinstance(draft, MethodInstanceDraft):
            raise StoreConflict("insert_method_instance expects a MethodInstanceDraft")
        mission = identifier(mission_id, "mission_id")
        lifecycle = self._state(state, METHOD_INSTANCE_STATES, "method instance state")
        now = self._store.now
        with self._store.transaction() as connection:
            self._execute(
                connection,
                "INSERT INTO method_instances(mission_id,instance_id,goal_task_id,"
                "goal_occurrence_id,obligation_id,method_id,method_version,method_content_hash,"
                "plan_revision,state,parameters_digest,draft_json,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mission,
                    str(draft.instance_id),
                    str(draft.goal_id),
                    str(draft.effective_goal_occurrence_id),
                    str(draft.obligation_id),
                    draft.method_ref.method_id,
                    draft.method_ref.version,
                    draft.method_ref.content_hash,
                    int(draft.plan_revision),
                    lifecycle,
                    draft.parameters_digest(),
                    canonical_json(draft.to_json()),
                    now,
                    now,
                ),
                f"method instance {draft.instance_id!s} already stored in {mission}",
            )
            for binding in draft.child_bindings:
                self._insert_child_occurrence(connection, mission, binding, now)
        return draft

    def get_method_instance(self, mission_id: str, instance_id: str) -> MethodInstanceDraft:
        row = self._one(
            "SELECT draft_json FROM method_instances WHERE mission_id = ? AND instance_id = ?",
            (identifier(mission_id, "mission_id"), identifier(instance_id, "instance_id")),
            f"no method instance {instance_id} in {mission_id}",
        )
        return MethodInstanceDraft.from_json(json.loads(row[0]))

    def method_instance_state(self, mission_id: str, instance_id: str) -> str:
        row = self._one(
            "SELECT state FROM method_instances WHERE mission_id = ? AND instance_id = ?",
            (identifier(mission_id, "mission_id"), identifier(instance_id, "instance_id")),
            f"no method instance {instance_id} in {mission_id}",
        )
        return str(row[0])

    def set_method_instance_state(self, mission_id: str, instance_id: str, state: str) -> str:
        lifecycle = self._state(state, METHOD_INSTANCE_STATES, "method instance state")
        mission = identifier(mission_id, "mission_id")
        instance = identifier(instance_id, "instance_id")
        self.method_instance_state(mission, instance)
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE method_instances SET state = ?, updated_at = ?"
                " WHERE mission_id = ? AND instance_id = ?",
                (lifecycle, self._store.now, mission, instance),
            )
        return lifecycle

    def list_method_instances(
        self, mission_id: str, *, plan_revision: int | None = None, state: str | None = None
    ) -> tuple[MethodInstanceDraft, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if plan_revision is not None:
            clauses.append("plan_revision = ?")
            values.append(index(plan_revision, "plan_revision"))
        if state is not None:
            clauses.append("state = ?")
            values.append(self._state(state, METHOD_INSTANCE_STATES, "method instance state"))
        rows = self._store.connection.execute(
            f"SELECT draft_json FROM method_instances WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at, instance_id",
            tuple(values),
        ).fetchall()
        return tuple(MethodInstanceDraft.from_json(json.loads(row[0])) for row in rows)

    def list_child_occurrences(self, mission_id: str, instance_id: str) -> tuple[ChildBinding, ...]:
        rows = self._store.connection.execute(
            "SELECT binding_json FROM method_child_occurrences WHERE mission_id = ?"
            " AND instance_id = ? ORDER BY slot_key",
            (identifier(mission_id, "mission_id"), identifier(instance_id, "instance_id")),
        ).fetchall()
        return tuple(ChildBinding.from_json(json.loads(row[0])) for row in rows)

    def occurrence_consumers(
        self, mission_id: str, goal_occurrence_id: str
    ) -> tuple[ChildBinding, ...]:
        """Every slot that adopted this goal occurrence — the shared-goal reverse index."""

        rows = self._store.connection.execute(
            "SELECT binding_json FROM method_child_occurrences WHERE mission_id = ?"
            " AND goal_occurrence_id = ? ORDER BY instance_id, slot_key",
            (
                identifier(mission_id, "mission_id"),
                identifier(goal_occurrence_id, "goal_occurrence_id"),
            ),
        ).fetchall()
        return tuple(ChildBinding.from_json(json.loads(row[0])) for row in rows)

    # ================================================================== plan revisions
    def insert_plan_revision(
        self,
        mission_id: str,
        revision: int,
        *,
        snapshot_hash: str,
        read_set: SemanticReadSet,
        state: str = "PREPARED",
        base_revision: int | None = None,
        delta_id: str | None = None,
    ) -> StoredPlanRevision:
        if not isinstance(read_set, SemanticReadSet):
            raise StoreConflict("insert_plan_revision expects a SemanticReadSet")
        mission = identifier(mission_id, "mission_id")
        number = index(revision, "revision")
        lifecycle = self._state(state, PLAN_REVISION_STATES, "plan revision state")
        base = None if base_revision is None else index(base_revision, "base_revision")
        now = self._store.now
        self._insert(
            "INSERT INTO plan_revisions(mission_id,revision,state,base_revision,snapshot_hash,"
            "delta_id,read_set_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                mission,
                number,
                lifecycle,
                base,
                snapshot_hash,
                delta_id,
                canonical_json(read_set.to_json()),
                now,
                now,
            ),
            f"plan revision {mission}@{number} already stored",
        )
        return StoredPlanRevision(
            mission_id=mission,
            revision=number,
            state=lifecycle,
            base_revision=base,
            snapshot_hash=snapshot_hash,
            delta_id=delta_id,
            read_set=read_set,
        )

    def get_plan_revision(self, mission_id: str, revision: int) -> StoredPlanRevision:
        row = self._one(
            "SELECT * FROM plan_revisions WHERE mission_id = ? AND revision = ?",
            (identifier(mission_id, "mission_id"), index(revision, "revision")),
            f"no plan revision {revision} in {mission_id}",
        )
        return self._plan_revision(row)

    def list_plan_revisions(self, mission_id: str) -> tuple[StoredPlanRevision, ...]:
        rows = self._store.connection.execute(
            "SELECT * FROM plan_revisions WHERE mission_id = ? ORDER BY revision",
            (identifier(mission_id, "mission_id"),),
        ).fetchall()
        return tuple(self._plan_revision(row) for row in rows)

    def active_plan_revision(self, mission_id: str) -> StoredPlanRevision | None:
        row = self._store.connection.execute(
            "SELECT * FROM plan_revisions WHERE mission_id = ? AND state = 'ACTIVE'",
            (identifier(mission_id, "mission_id"),),
        ).fetchone()
        return None if row is None else self._plan_revision(row)

    def activate_plan_revision(self, mission_id: str, revision: int) -> StoredPlanRevision:
        """Adopt one revision.  The previous ACTIVE is retired in the same transaction.

        A Mission has exactly one plan under execution control (§20 rule 5); the
        partial unique index enforces it, and this method is the only way to move
        between revisions without tripping it.
        """

        mission = identifier(mission_id, "mission_id")
        number = index(revision, "revision")
        current = self.get_plan_revision(mission, number)
        if current.state == "RETIRED":
            raise StoreConflict(f"plan revision {mission}@{number} is retired")
        now = self._store.now
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE plan_revisions SET state = 'RETIRED', updated_at = ?"
                " WHERE mission_id = ? AND state = 'ACTIVE' AND revision <> ?",
                (now, mission, number),
            )
            self._execute(
                connection,
                "UPDATE plan_revisions SET state = 'ACTIVE', updated_at = ?"
                " WHERE mission_id = ? AND revision = ?",
                (now, mission, number),
                f"plan revision {mission}@{number} could not be activated",
            )
        return self.get_plan_revision(mission, number)

    def insert_plan_membership(
        self,
        mission_id: str,
        revision: int,
        occurrence: OccurrenceSpec,
        *,
        instance_id: str | None = None,
        adopted: bool = True,
    ) -> OccurrenceSpec:
        if not isinstance(occurrence, OccurrenceSpec):
            raise StoreConflict("insert_plan_membership expects an OccurrenceSpec")
        mission = identifier(mission_id, "mission_id")
        number = index(revision, "revision")
        self._insert(
            "INSERT INTO plan_memberships(mission_id,revision,occurrence_id,task_id,obligation_id,"
            "instance_id,form,requiredness,adopted,member_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                mission,
                number,
                str(occurrence.occurrence_id),
                str(occurrence.task_id),
                str(occurrence.obligation_id),
                None if instance_id is None else identifier(instance_id, "instance_id"),
                str(occurrence.form),
                str(occurrence.requiredness),
                1 if adopted else 0,
                canonical_json(occurrence.to_json()),
                self._store.now,
            ),
            f"occurrence {occurrence.occurrence_id!s} already a member of {mission}@{number}",
        )
        return occurrence

    def list_plan_memberships(self, mission_id: str, revision: int) -> tuple[OccurrenceSpec, ...]:
        rows = self._store.connection.execute(
            "SELECT member_json FROM plan_memberships WHERE mission_id = ? AND revision = ?"
            " ORDER BY occurrence_id",
            (identifier(mission_id, "mission_id"), index(revision, "revision")),
        ).fetchall()
        return tuple(OccurrenceSpec.from_json(json.loads(row[0])) for row in rows)

    # ================================================================== order and data
    def insert_order_constraint(
        self, mission_id: str, plan_revision: int, constraint: OrderConstraint
    ) -> OrderConstraint:
        if not isinstance(constraint, OrderConstraint):
            raise StoreConflict("insert_order_constraint expects an OrderConstraint")
        mission = identifier(mission_id, "mission_id")
        number = index(plan_revision, "plan_revision")
        self._insert(
            "INSERT INTO order_constraints(mission_id,plan_revision,before_occurrence,"
            "after_occurrence,release_condition,constraint_json,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                mission,
                number,
                str(constraint.before),
                str(constraint.after),
                str(constraint.release_condition),
                canonical_json(constraint.to_json()),
                self._store.now,
            ),
            f"order {constraint.before!s} → {constraint.after!s} already stored"
            f" in {mission}@{number}",
        )
        return constraint

    def list_order_constraints(
        self,
        mission_id: str,
        plan_revision: int,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> tuple[OrderConstraint, ...]:
        clauses = ["mission_id = ?", "plan_revision = ?"]
        values: list[Any] = [
            identifier(mission_id, "mission_id"),
            index(plan_revision, "plan_revision"),
        ]
        if before is not None:
            clauses.append("before_occurrence = ?")
            values.append(identifier(before, "before"))
        if after is not None:
            clauses.append("after_occurrence = ?")
            values.append(identifier(after, "after"))
        rows = self._store.connection.execute(
            f"SELECT constraint_json FROM order_constraints WHERE {' AND '.join(clauses)}"
            " ORDER BY before_occurrence, after_occurrence",
            tuple(values),
        ).fetchall()
        return tuple(OrderConstraint.from_json(json.loads(row[0])) for row in rows)

    def insert_data_requirement(
        self, mission_id: str, plan_revision: int, requirement: DataRequirement
    ) -> DataRequirement:
        if not isinstance(requirement, DataRequirement):
            raise StoreConflict("insert_data_requirement expects a DataRequirement")
        mission = identifier(mission_id, "mission_id")
        number = index(plan_revision, "plan_revision")
        self._insert(
            "INSERT INTO data_requirements(mission_id,plan_revision,requirement_id,"
            "producer_occurrence,output_port,consumer_occurrence,input_port,"
            "source_revision_policy,requirement_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mission,
                number,
                requirement.requirement_id,
                str(requirement.producer_occurrence),
                requirement.output_port,
                str(requirement.consumer_occurrence),
                requirement.input_port,
                str(requirement.source_revision_policy),
                canonical_json(requirement.to_json()),
                self._store.now,
            ),
            f"data requirement {requirement.requirement_id} already stored in {mission}@{number}",
        )
        return requirement

    def list_data_requirements(
        self,
        mission_id: str,
        plan_revision: int,
        *,
        consumer_occurrence: str | None = None,
        producer_occurrence: str | None = None,
    ) -> tuple[DataRequirement, ...]:
        clauses = ["mission_id = ?", "plan_revision = ?"]
        values: list[Any] = [
            identifier(mission_id, "mission_id"),
            index(plan_revision, "plan_revision"),
        ]
        if consumer_occurrence is not None:
            clauses.append("consumer_occurrence = ?")
            values.append(identifier(consumer_occurrence, "consumer_occurrence"))
        if producer_occurrence is not None:
            clauses.append("producer_occurrence = ?")
            values.append(identifier(producer_occurrence, "producer_occurrence"))
        rows = self._store.connection.execute(
            f"SELECT requirement_json FROM data_requirements WHERE {' AND '.join(clauses)}"
            " ORDER BY requirement_id",
            tuple(values),
        ).fetchall()
        return tuple(DataRequirement.from_json(json.loads(row[0])) for row in rows)

    def insert_bound_input(
        self,
        mission_id: str,
        plan_revision: int,
        bound: BoundInput,
        *,
        input_binding_revision: int = 0,
    ) -> BoundInput:
        if not isinstance(bound, BoundInput):
            raise StoreConflict("insert_bound_input expects a BoundInput")
        mission = identifier(mission_id, "mission_id")
        number = index(plan_revision, "plan_revision")
        binding_revision = index(input_binding_revision, "input_binding_revision")
        self._insert(
            "INSERT INTO bound_inputs(mission_id,plan_revision,requirement_id,"
            "input_binding_revision,producer_result_id,acceptance_id,artifact_id,content_hash,"
            "source_revision,binding_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                mission,
                number,
                bound.requirement_id,
                binding_revision,
                bound.producer_result_id,
                bound.acceptance_id,
                bound.artifact_id,
                bound.content_hash,
                bound.source_revision,
                canonical_json(bound.to_json()),
                self._store.now,
            ),
            f"bound input {bound.requirement_id}@{binding_revision} already stored"
            f" in {mission}@{number}",
        )
        return bound

    def list_bound_inputs(
        self, mission_id: str, plan_revision: int, *, requirement_id: str | None = None
    ) -> tuple[BoundInput, ...]:
        clauses = ["mission_id = ?", "plan_revision = ?"]
        values: list[Any] = [
            identifier(mission_id, "mission_id"),
            index(plan_revision, "plan_revision"),
        ]
        if requirement_id is not None:
            clauses.append("requirement_id = ?")
            values.append(identifier(requirement_id, "requirement_id"))
        rows = self._store.connection.execute(
            f"SELECT binding_json FROM bound_inputs WHERE {' AND '.join(clauses)}"
            " ORDER BY requirement_id, input_binding_revision",
            tuple(values),
        ).fetchall()
        return tuple(BoundInput.from_json(json.loads(row[0])) for row in rows)

    def insert_input_manifest(
        self,
        mission_id: str,
        task_id: str,
        manifest: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
        request_id: str | None = None,
        input_binding_revision: int = 0,
    ) -> str:
        """Freeze one input set and bind this task to it.  Identity is the content hash.

        The content row is immutable and Mission-free; *using* it is a binding row.
        Two tasks — in one Mission or in two — that resolve to byte-identical inputs
        therefore share one immutable manifest and get one binding each, instead of
        one of them being refused for reusing the other's bytes.

        The document is kept as opaque canonical JSON: the resolved-manifest type
        lives in ``artifacts/input_bindings`` (P2.2b) and this table must not become
        a second authority on how an input set is built.
        """

        mission = identifier(mission_id, "mission_id")
        task = identifier(task_id, "task_id")
        if not isinstance(manifest, Mapping):
            raise StoreConflict("insert_input_manifest expects a mapping")
        document = dict(manifest)
        digest = content_hash_of(document)
        revision = index(input_binding_revision, "input_binding_revision")
        now = self._store.now
        with self._store.transaction() as connection:
            self._execute(
                connection,
                "INSERT INTO input_manifests(manifest_hash,origin_mission_id,manifest_json,"
                "created_at) VALUES (?,?,?,?) ON CONFLICT(manifest_hash) DO NOTHING",
                (digest, mission, canonical_json(document), now),
                f"input manifest {digest} could not be stored",
            )
            self._execute(
                connection,
                "INSERT INTO input_manifest_bindings(mission_id,task_id,manifest_hash,attempt_id,"
                "request_id,input_binding_revision,created_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(mission_id,task_id,manifest_hash) DO NOTHING",
                (mission, task, digest, attempt_id, request_id, revision, now),
                f"task {task} could not be bound to input manifest {digest}",
            )
        return digest

    def get_input_manifest(self, manifest_hash: str) -> dict[str, Any]:
        row = self._one(
            "SELECT manifest_json FROM input_manifests WHERE manifest_hash = ?",
            (identifier(manifest_hash, "manifest_hash"),),
            f"no input manifest {manifest_hash}",
        )
        loaded: dict[str, Any] = json.loads(row[0])
        return loaded

    def list_manifest_bindings(self, manifest_hash: str) -> tuple[dict[str, Any], ...]:
        """Every (Mission, task) that froze this exact input set."""

        rows = self._store.connection.execute(
            "SELECT mission_id, task_id, attempt_id, request_id, input_binding_revision"
            " FROM input_manifest_bindings WHERE manifest_hash = ? ORDER BY mission_id, task_id",
            (identifier(manifest_hash, "manifest_hash"),),
        ).fetchall()
        return tuple(
            {
                "mission_id": row[0],
                "task_id": row[1],
                "attempt_id": row[2],
                "request_id": row[3],
                "input_binding_revision": row[4],
            }
            for row in rows
        )

    # ================================================================== requirements
    def insert_requirements_revision(self, revision: RequirementsRevision) -> str:
        if not isinstance(revision, RequirementsRevision):
            raise StoreConflict("insert_requirements_revision expects a RequirementsRevision")
        document = revision.to_json()
        digest = content_hash_of(document)
        self._insert(
            "INSERT INTO requirements_revisions(mission_id,revision,revision_id,content_hash,"
            "authority_subject,revision_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                revision.mission_id,
                revision.revision,
                str(revision.revision_id),
                digest,
                revision.authority_subject,
                canonical_json(document),
                self._store.now,
            ),
            f"requirements revision {revision.mission_id}@{revision.revision} already stored",
        )
        return digest

    def get_requirements_revision(self, mission_id: str, revision: int) -> RequirementsRevision:
        row = self._one(
            "SELECT revision_json FROM requirements_revisions WHERE mission_id = ? AND revision = ?",
            (identifier(mission_id, "mission_id"), index(revision, "revision")),
            f"no requirements revision {revision} in {mission_id}",
        )
        return RequirementsRevision.from_json(json.loads(row[0]))

    def latest_requirements_revision(self, mission_id: str) -> RequirementsRevision | None:
        row = self._store.connection.execute(
            "SELECT revision_json FROM requirements_revisions WHERE mission_id = ?"
            " ORDER BY revision DESC LIMIT 1",
            (identifier(mission_id, "mission_id"),),
        ).fetchone()
        return None if row is None else RequirementsRevision.from_json(json.loads(row[0]))

    # ================================================================== review
    def insert_review_package(self, package: ReviewPackage) -> str:
        if not isinstance(package, ReviewPackage):
            raise StoreConflict("insert_review_package expects a ReviewPackage")
        document = package.to_json()
        digest = content_hash_of(document)
        binding = package.binding
        self._insert(
            "INSERT INTO review_packages(package_id,mission_id,purpose,review_account,"
            "obligation_id,subject_kind,subject_id,requirements_revision,input_manifest_hash,"
            "method_instance_id,package_hash,package_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(package.package_id),
                binding.mission_id,
                str(package.purpose),
                str(account_for_purpose(package.purpose)),
                binding.obligation_id,
                str(binding.subject_ref.kind),
                binding.subject_ref.id,
                binding.requirements_revision,
                binding.input_manifest_hash,
                (None if package.method_instance_id is None else str(package.method_instance_id)),
                digest,
                canonical_json(document),
                self._store.now,
            ),
            f"review package {package.package_id!s} already stored",
        )
        return digest

    def get_review_package(self, package_id: str) -> ReviewPackage:
        row = self._one(
            "SELECT package_json FROM review_packages WHERE package_id = ?",
            (identifier(package_id, "package_id"),),
            f"no review package {package_id}",
        )
        return ReviewPackage.from_json(json.loads(row[0]))

    def list_review_packages(
        self, mission_id: str, *, purpose: ReviewPurpose | None = None
    ) -> tuple[ReviewPackage, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if purpose is not None:
            clauses.append("purpose = ?")
            values.append(str(enum_of(ReviewPurpose, purpose, "purpose")))
        rows = self._store.connection.execute(
            f"SELECT package_json FROM review_packages WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at, package_id",
            tuple(values),
        ).fetchall()
        return tuple(ReviewPackage.from_json(json.loads(row[0])) for row in rows)

    def insert_review_record(self, record: ReviewRecord, *, official: bool = False) -> str:
        """Store one judgement and its per-criterion evaluations in one transaction.

        At most one record per package may be ``official`` — AER §18.2's "同 review
        command 只一份正式接纳".  A second one is a conflict, not an overwrite.
        """

        if not isinstance(record, ReviewRecord):
            raise StoreConflict("insert_review_record expects a ReviewRecord")
        stored_package = self.get_review_package(record.package_id)
        if stored_package.purpose is not record.purpose:
            raise StoreConflict(
                f"review record {record.record_id!s} does not carry its package's purpose"
            )
        document = record.to_json()
        digest = content_hash_of(document)
        mission = record.binding.mission_id
        now = self._store.now
        with self._store.transaction() as connection:
            self._execute(
                connection,
                "INSERT INTO review_records(record_id,package_id,mission_id,purpose,"
                "reviewer_agent_id,reviewer_turn_id,verdict,evidence_manifest_hash,official,"
                "record_hash,record_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(record.record_id),
                    str(record.package_id),
                    mission,
                    str(record.purpose),
                    record.reviewer_agent_id,
                    record.reviewer_turn_id,
                    str(record.verdict),
                    record.evidence_manifest_hash,
                    1 if official else 0,
                    digest,
                    canonical_json(document),
                    now,
                ),
                f"review record {record.record_id!s} conflicts with what is stored"
                f" for package {record.package_id!s}",
            )
            for outcome in record.criteria:
                receipt = content_hash_of(
                    {
                        "package_id": str(record.package_id),
                        "binding": record.binding.to_json(),
                        "outcome": outcome.to_json(),
                    }
                )
                self._execute(
                    connection,
                    "INSERT INTO criterion_evaluations(review_id,criterion_id,mission_id,"
                    "package_id,verdict,check_execution,check_receipt_hash,outcome_json,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(record.record_id),
                        outcome.criterion_id,
                        mission,
                        str(record.package_id),
                        str(outcome.verdict),
                        str(outcome.check_execution),
                        receipt,
                        canonical_json(outcome.to_json()),
                        now,
                    ),
                    f"criterion {outcome.criterion_id} appears twice in review"
                    f" {record.record_id!s}",
                )
        return digest

    def get_review_record(self, record_id: str) -> StoredReviewRecord:
        row = self._one(
            "SELECT record_json, official FROM review_records WHERE record_id = ?",
            (identifier(record_id, "record_id"),),
            f"no review record {record_id}",
        )
        return StoredReviewRecord(
            record=ReviewRecord.from_json(json.loads(row[0])), official=bool(row[1])
        )

    def list_review_records(self, package_id: str) -> tuple[StoredReviewRecord, ...]:
        rows = self._store.connection.execute(
            "SELECT record_json, official FROM review_records WHERE package_id = ?"
            " ORDER BY created_at, record_id",
            (identifier(package_id, "package_id"),),
        ).fetchall()
        return tuple(
            StoredReviewRecord(
                record=ReviewRecord.from_json(json.loads(row[0])), official=bool(row[1])
            )
            for row in rows
        )

    def official_review_record(self, package_id: str) -> ReviewRecord | None:
        row = self._store.connection.execute(
            "SELECT record_json FROM review_records WHERE package_id = ? AND official = 1",
            (identifier(package_id, "package_id"),),
        ).fetchone()
        return None if row is None else ReviewRecord.from_json(json.loads(row[0]))

    def list_criterion_evaluations(self, review_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._store.connection.execute(
            "SELECT criterion_id, verdict, check_execution, check_receipt_hash, outcome_json"
            " FROM criterion_evaluations WHERE review_id = ? ORDER BY criterion_id",
            (identifier(review_id, "review_id"),),
        ).fetchall()
        return tuple(
            {
                "criterion_id": row[0],
                "verdict": row[1],
                "check_execution": row[2],
                "check_receipt_hash": row[3],
                "outcome": json.loads(row[4]),
            }
            for row in rows
        )

    # ================================================================== acceptance
    def insert_acceptance(self, acceptance: Acceptance) -> str:
        """Store one acceptance.  Re-submitting the same content returns its hash."""

        if not isinstance(acceptance, Acceptance):
            raise StoreConflict("insert_acceptance expects an Acceptance")
        document = acceptance.to_json()
        digest = content_hash_of(document)
        existing = self._store.connection.execute(
            "SELECT content_hash FROM acceptances WHERE acceptance_id = ?",
            (str(acceptance.acceptance_id),),
        ).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise StoreConflict(
                    f"acceptance {acceptance.acceptance_id!s} is already stored with"
                    " different content"
                )
            return digest
        self._insert(
            "INSERT INTO acceptances(acceptance_id,mission_id,task_id,obligation_id,"
            "review_record_id,requirements_revision,contract_revision,input_manifest_hash,"
            "validity,accepted_at_ms,content_hash,acceptance_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(acceptance.acceptance_id),
                acceptance.mission_id,
                str(acceptance.task_id),
                str(acceptance.obligation_id),
                str(acceptance.review_record_id),
                acceptance.requirements_revision,
                acceptance.contract_revision,
                acceptance.input_manifest_hash,
                str(acceptance.validity),
                acceptance.accepted_at_ms,
                digest,
                canonical_json(document),
                self._store.now,
            ),
            f"acceptance {acceptance.acceptance_id!s} could not be stored",
        )
        return digest

    def get_acceptance(self, acceptance_id: str) -> Acceptance:
        row = self._one(
            "SELECT acceptance_json FROM acceptances WHERE acceptance_id = ?",
            (identifier(acceptance_id, "acceptance_id"),),
            f"no acceptance {acceptance_id}",
        )
        return Acceptance.from_json(json.loads(row[0]))

    def list_acceptances(
        self, mission_id: str, *, obligation_id: str | None = None
    ) -> tuple[Acceptance, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if obligation_id is not None:
            clauses.append("obligation_id = ?")
            values.append(identifier(obligation_id, "obligation_id"))
        rows = self._store.connection.execute(
            f"SELECT acceptance_json FROM acceptances WHERE {' AND '.join(clauses)}"
            " ORDER BY accepted_at_ms, acceptance_id",
            tuple(values),
        ).fetchall()
        return tuple(Acceptance.from_json(json.loads(row[0])) for row in rows)

    def insert_goal_resolution(self, resolution: GoalResolution, *, adopted: bool = False) -> str:
        if not isinstance(resolution, GoalResolution):
            raise StoreConflict("insert_goal_resolution expects a GoalResolution")
        document = resolution.to_json()
        digest = content_hash_of(document)
        now = self._store.now
        self._insert(
            "INSERT INTO goal_resolutions(resolution_id,mission_id,obligation_id,goal_task_id,"
            "requirements_version,contract_revision,method_instance_id,review_receipt_id,verdict,"
            "validity,adopted,content_hash,resolution_json,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(resolution.resolution_id),
                resolution.mission_id,
                resolution.obligation_id,
                resolution.goal_task_id,
                resolution.requirements_version,
                resolution.contract_revision,
                resolution.method_instance_id,
                resolution.review_receipt_id,
                str(resolution.verdict),
                str(resolution.validity),
                1 if adopted else 0,
                digest,
                canonical_json(document),
                now,
                now,
            ),
            f"goal resolution {resolution.resolution_id!s} conflicts with what is stored"
            f" for obligation {resolution.obligation_id}",
        )
        return digest

    def adopt_goal_resolution(self, mission_id: str, resolution_id: str) -> GoalResolution:
        """Make one resolution the currently adopted one for its obligation."""

        mission = identifier(mission_id, "mission_id")
        target = identifier(resolution_id, "resolution_id")
        row = self._one(
            "SELECT obligation_id FROM goal_resolutions WHERE mission_id = ? AND resolution_id = ?",
            (mission, target),
            f"no goal resolution {resolution_id} in {mission_id}",
        )
        now = self._store.now
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE goal_resolutions SET adopted = 0, updated_at = ? WHERE mission_id = ?"
                " AND obligation_id = ? AND resolution_id <> ?",
                (now, mission, row[0], target),
            )
            self._execute(
                connection,
                "UPDATE goal_resolutions SET adopted = 1, updated_at = ?"
                " WHERE mission_id = ? AND resolution_id = ?",
                (now, mission, target),
                f"goal resolution {resolution_id} could not be adopted",
            )
        return self.get_goal_resolution(target)

    def get_goal_resolution(self, resolution_id: str) -> GoalResolution:
        row = self._one(
            "SELECT resolution_json FROM goal_resolutions WHERE resolution_id = ?",
            (identifier(resolution_id, "resolution_id"),),
            f"no goal resolution {resolution_id}",
        )
        return GoalResolution.from_json(json.loads(row[0]))

    def adopted_goal_resolution(self, mission_id: str, obligation_id: str) -> GoalResolution | None:
        row = self._store.connection.execute(
            "SELECT resolution_json FROM goal_resolutions WHERE mission_id = ?"
            " AND obligation_id = ? AND adopted = 1",
            (identifier(mission_id, "mission_id"), identifier(obligation_id, "obligation_id")),
        ).fetchone()
        return None if row is None else GoalResolution.from_json(json.loads(row[0]))

    def list_goal_resolutions(
        self, mission_id: str, *, obligation_id: str | None = None
    ) -> tuple[GoalResolution, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if obligation_id is not None:
            clauses.append("obligation_id = ?")
            values.append(identifier(obligation_id, "obligation_id"))
        rows = self._store.connection.execute(
            f"SELECT resolution_json FROM goal_resolutions WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at, resolution_id",
            tuple(values),
        ).fetchall()
        return tuple(GoalResolution.from_json(json.loads(row[0])) for row in rows)

    # ================================================================== evidence
    def insert_validity_witness(
        self, mission_id: str, witness: ValidityWitness, *, subject: str
    ) -> ValidityWitness:
        """Store one licence, filed under the subject its issuer names (migration 18).

        ``subject`` is declared by the caller and re-checked here against
        :func:`~..knowledge.validity.witness_subject`, which recomputes it from the
        witness alone.  The declaration is what AER §8.1 asks for — a licence says
        which support it was taken over — and the recomputation is what keeps the row
        auditable (§16.1): a subject the witness does not name would let two different
        licences be filed under one key again, so it is refused rather than stored.
        """

        if not isinstance(witness, ValidityWitness):
            raise StoreConflict("insert_validity_witness expects a ValidityWitness")
        expected = witness_subject(witness)
        if str(subject) != expected:
            raise StoreConflict(
                f"validity witness {witness.witness_id} was offered under subject {subject!r}"
                f" but names {expected!r}; a licence is filed under the subject it says it"
                " was taken over, never under one supplied beside it"
            )
        mission = identifier(mission_id, "mission_id")
        self._insert(
            "INSERT INTO validity_witnesses(witness_id,mission_id,consumer_kind,consumer_id,"
            "purpose,subject_digest,scope_id,scope_epoch,support_revision,truth,freshness,"
            "availability,decision,as_of_ms,not_after_ms,witness_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                witness.witness_id,
                mission,
                str(witness.consumer_ref.kind),
                witness.consumer_ref.id,
                str(witness.purpose),
                expected,
                witness.scope_id,
                witness.scope_epoch,
                witness.support_revision,
                str(witness.truth),
                str(witness.freshness),
                str(witness.availability),
                str(witness.decision),
                witness.as_of_ms,
                witness.not_after_ms,
                canonical_json(witness.to_json()),
                self._store.now,
            ),
            f"validity witness {witness.witness_id} conflicts with one already stored"
            f" for {witness.consumer_ref.id}/{witness.purpose!s}",
        )
        return witness

    def get_validity_witness(self, witness_id: str) -> ValidityWitness:
        row = self._one(
            "SELECT witness_json FROM validity_witnesses WHERE witness_id = ?",
            (identifier(witness_id, "witness_id"),),
            f"no validity witness {witness_id}",
        )
        return ValidityWitness.from_json(json.loads(row[0]))

    def list_validity_witnesses(
        self, mission_id: str, *, scope_id: str | None = None, subject: str | None = None
    ) -> tuple[ValidityWitness, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if scope_id is not None:
            clauses.append("scope_id = ?")
            values.append(identifier(scope_id, "scope_id"))
        if subject is not None:
            clauses.append("subject_digest = ?")
            values.append(str(subject))
        rows = self._store.connection.execute(
            f"SELECT witness_json FROM validity_witnesses WHERE {' AND '.join(clauses)}"
            " ORDER BY as_of_ms, witness_id",
            tuple(values),
        ).fetchall()
        return tuple(ValidityWitness.from_json(json.loads(row[0])) for row in rows)

    def insert_observation(
        self, mission_id: str, observation: ObservationRecord, *, scope_id: str = "mission"
    ) -> ObservationRecord:
        if not isinstance(observation, ObservationRecord):
            raise StoreConflict("insert_observation expects an ObservationRecord")
        mission = identifier(mission_id, "mission_id")
        self._insert(
            "INSERT INTO observations(observation_id,mission_id,proposition_key,polarity,scope_id,"
            "source_kind,source_id,coverage,observer_id,observed_at_ms,recorded_at_ms,"
            "query_watermark_ms,valid_until_ms,observation_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                observation.observation_id,
                mission,
                observation.proposition_key,
                1 if observation.polarity else 0,
                identifier(scope_id, "scope_id"),
                str(observation.source_ref.kind),
                observation.source_ref.id,
                str(observation.coverage),
                observation.observer_id,
                observation.observed_at_ms,
                observation.recorded_at_ms,
                observation.query_watermark_ms,
                observation.valid_until_ms,
                canonical_json(observation.to_json()),
                self._store.now,
            ),
            f"observation {observation.observation_id} already stored",
        )
        return observation

    def get_observation(self, observation_id: str) -> ObservationRecord:
        row = self._one(
            "SELECT observation_json FROM observations WHERE observation_id = ?",
            (identifier(observation_id, "observation_id"),),
            f"no observation {observation_id}",
        )
        return ObservationRecord.from_json(json.loads(row[0]))

    def list_observations(
        self, mission_id: str, *, proposition_key: str | None = None
    ) -> tuple[ObservationRecord, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if proposition_key is not None:
            clauses.append("proposition_key = ?")
            values.append(str(proposition_key))
        rows = self._store.connection.execute(
            f"SELECT observation_json FROM observations WHERE {' AND '.join(clauses)}"
            " ORDER BY observed_at_ms, observation_id",
            tuple(values),
        ).fetchall()
        return tuple(ObservationRecord.from_json(json.loads(row[0])) for row in rows)

    def insert_justification_set(
        self,
        mission_id: str,
        set_id: str,
        *,
        subject_kind: str,
        subject_id: str,
        members: Sequence[tuple[TypedRef, bool]],
        member_revision: int = 0,
        rule_ref: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> StoredJustificationSet:
        """Store one support set and its members, with the reverse index (AER §10.1)."""

        mission = identifier(mission_id, "mission_id")
        identity = identifier(set_id, "set_id")
        kind = identifier(subject_kind, "subject_kind")
        subject = identifier(subject_id, "subject_id")
        revision = index(member_revision, "member_revision")
        entries = tuple(members)
        if not entries:
            raise StoreConflict("a justification set needs at least one member (AER §9.1)")
        for reference, polarity in entries:
            if not isinstance(reference, TypedRef):
                raise StoreConflict("justification members must be TypedRefs")
            if not isinstance(polarity, bool):
                raise StoreConflict("a justification member carries an explicit polarity")
        # A support set is a set: its digest and its read order must not depend on
        # the order the caller happened to list the members in.
        entries = tuple(sorted(entries, key=lambda item: (str(item[0].kind), item[0].id)))
        digest = content_hash_of(
            [[reference.to_json(), polarity] for reference, polarity in entries]
        )
        payload = dict(detail or {})
        now = self._store.now
        with self._store.transaction() as connection:
            self._execute(
                connection,
                "INSERT INTO justification_sets(set_id,mission_id,subject_kind,subject_id,"
                "member_revision,member_digest,rule_ref,detail_json,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    mission,
                    kind,
                    subject,
                    revision,
                    digest,
                    rule_ref,
                    canonical_json(payload),
                    now,
                ),
                f"justification set {identity} conflicts with one already stored for"
                f" {kind}/{subject}",
            )
            for reference, polarity in entries:
                self._execute(
                    connection,
                    "INSERT INTO support_members(set_id,member_kind,member_id,mission_id,polarity,"
                    "member_revision,member_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        str(reference.kind),
                        reference.id,
                        mission,
                        1 if polarity else 0,
                        reference.revision,
                        canonical_json(reference.to_json()),
                        now,
                    ),
                    f"member {reference.kind!s}/{reference.id} appears twice in {identity}",
                )
        return StoredJustificationSet(
            set_id=identity,
            mission_id=mission,
            subject_kind=kind,
            subject_id=subject,
            member_revision=revision,
            member_digest=digest,
            rule_ref=rule_ref,
            members=entries,
            detail=payload,
        )

    def get_justification_set(self, set_id: str) -> StoredJustificationSet:
        row = self._one(
            "SELECT * FROM justification_sets WHERE set_id = ?",
            (identifier(set_id, "set_id"),),
            f"no justification set {set_id}",
        )
        return self._justification_set(row)

    def list_justification_sets(
        self, mission_id: str, subject_kind: str, subject_id: str
    ) -> tuple[StoredJustificationSet, ...]:
        rows = self._store.connection.execute(
            "SELECT * FROM justification_sets WHERE mission_id = ? AND subject_kind = ?"
            " AND subject_id = ? ORDER BY member_revision, set_id",
            (
                identifier(mission_id, "mission_id"),
                identifier(subject_kind, "subject_kind"),
                identifier(subject_id, "subject_id"),
            ),
        ).fetchall()
        return tuple(self._justification_set(row) for row in rows)

    def consumers_of(
        self, mission_id: str, member_kind: str, member_id: str
    ) -> tuple[StoredJustificationSet, ...]:
        """The reverse support index: every set that rests on this member."""

        rows = self._store.connection.execute(
            "SELECT justification_sets.* FROM support_members JOIN justification_sets"
            " ON justification_sets.set_id = support_members.set_id"
            " WHERE support_members.mission_id = ? AND support_members.member_kind = ?"
            " AND support_members.member_id = ? ORDER BY justification_sets.set_id",
            (
                identifier(mission_id, "mission_id"),
                identifier(member_kind, "member_kind"),
                identifier(member_id, "member_id"),
            ),
        ).fetchall()
        return tuple(self._justification_set(row) for row in rows)

    def bump_epoch(self, mission_id: str, scope_id: str, *, bumped_by: str) -> int:
        """Raise a scope's validity epoch.  Readers behind it must fail closed."""

        mission = identifier(mission_id, "mission_id")
        scope = identifier(scope_id, "scope_id")
        actor = identifier(bumped_by, "bumped_by")
        with self._store.transaction() as connection:
            row = connection.execute(
                "SELECT epoch FROM validity_epochs WHERE mission_id = ? AND scope_id = ?",
                (mission, scope),
            ).fetchone()
            epoch = 0 if row is None else int(row[0]) + 1
            connection.execute(
                "INSERT INTO validity_epochs(mission_id,scope_id,epoch,bumped_by,updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(mission_id,scope_id) DO UPDATE SET"
                " epoch = excluded.epoch, bumped_by = excluded.bumped_by,"
                " updated_at = excluded.updated_at",
                (mission, scope, epoch, actor, self._store.now),
            )
        return epoch

    def epoch(self, mission_id: str, scope_id: str) -> int:
        row = self._store.connection.execute(
            "SELECT epoch FROM validity_epochs WHERE mission_id = ? AND scope_id = ?",
            (identifier(mission_id, "mission_id"), identifier(scope_id, "scope_id")),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def mark_dirty(
        self,
        mission_id: str,
        *,
        subject_kind: str,
        subject_id: str,
        scope_id: str,
        epoch: int,
        reason: str,
        state: str = "PENDING",
    ) -> DirtyEntry:
        mission = identifier(mission_id, "mission_id")
        entry = DirtyEntry(
            mission_id=mission,
            subject_kind=identifier(subject_kind, "subject_kind"),
            subject_id=identifier(subject_id, "subject_id"),
            epoch=index(epoch, "epoch"),
            scope_id=identifier(scope_id, "scope_id"),
            reason=str(reason),
            state=self._state(state, DIRTY_STATES, "dirty state"),
        )
        now = self._store.now
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO validity_dirty(mission_id,subject_kind,subject_id,epoch,scope_id,"
                "reason,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(mission_id,subject_kind,subject_id,epoch) DO UPDATE SET"
                " state = excluded.state, reason = excluded.reason,"
                " updated_at = excluded.updated_at",
                (
                    entry.mission_id,
                    entry.subject_kind,
                    entry.subject_id,
                    entry.epoch,
                    entry.scope_id,
                    entry.reason,
                    entry.state,
                    now,
                    now,
                ),
            )
        return entry

    def list_dirty(self, mission_id: str, *, state: str = "PENDING") -> tuple[DirtyEntry, ...]:
        rows = self._store.connection.execute(
            "SELECT mission_id,subject_kind,subject_id,epoch,scope_id,reason,state"
            " FROM validity_dirty WHERE mission_id = ? AND state = ?"
            " ORDER BY created_at, subject_id",
            (
                identifier(mission_id, "mission_id"),
                self._state(state, DIRTY_STATES, "dirty state"),
            ),
        ).fetchall()
        return tuple(
            DirtyEntry(
                mission_id=str(row[0]),
                subject_kind=str(row[1]),
                subject_id=str(row[2]),
                epoch=int(row[3]),
                scope_id=str(row[4]),
                reason=str(row[5]),
                state=str(row[6]),
            )
            for row in rows
        )

    # ================================================================== operations
    def bind_operation(
        self, envelope: OperationEnvelope, *, principal_id: str, scope_id: str | None = None
    ) -> OperationEnvelope:
        """Freeze one operation's semantics against a principal and scope (AER §12.2).

        ``OperationId`` carries exactly one ``request_hash``: a second envelope with
        the same id and a different request is ``OPERATION_PAYLOAD_CONFLICT`` and is
        refused by the foreign key, not merely by a check this method could forget.
        """

        if not isinstance(envelope, OperationEnvelope):
            raise StoreConflict("bind_operation expects an OperationEnvelope")
        principal = identifier(principal_id, "principal_id")
        scope = identifier(scope_id, "scope_id") if scope_id is not None else envelope.scope_id
        document = envelope.to_json()
        digest = content_hash_of(document)
        now = self._store.now
        with self._store.transaction() as connection:
            stored = connection.execute(
                "SELECT request_hash, envelope_hash FROM operation_identities WHERE operation_id = ?",
                (str(envelope.operation_id),),
            ).fetchone()
            if stored is None:
                self._execute(
                    connection,
                    "INSERT INTO operation_identities(operation_id,mission_id,request_hash,"
                    "envelope_hash,envelope_json,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        str(envelope.operation_id),
                        envelope.mission_id,
                        envelope.request_hash,
                        digest,
                        canonical_json(document),
                        now,
                    ),
                    f"operation {envelope.operation_id!s} identity could not be stored",
                )
            elif stored["request_hash"] != envelope.request_hash:
                raise StoreConflict(
                    f"OPERATION_PAYLOAD_CONFLICT: operation {envelope.operation_id!s} is bound to "
                    f"request {stored['request_hash']}, not {envelope.request_hash}"
                )
            self._execute(
                connection,
                "INSERT INTO operation_bindings(principal_id,scope_id,operation_occurrence_id,"
                "operation_id,request_hash,mission_id,obligation_id,connector_id,connector_version,"
                "operation_kind,envelope_hash,envelope_json,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    principal,
                    scope,
                    str(envelope.operation_occurrence_id),
                    str(envelope.operation_id),
                    envelope.request_hash,
                    envelope.mission_id,
                    envelope.obligation_id,
                    envelope.connector_id,
                    envelope.connector_version,
                    str(envelope.operation_kind),
                    digest,
                    canonical_json(document),
                    now,
                ),
                f"operation occurrence {envelope.operation_occurrence_id!s} is already bound",
            )
        return envelope

    def get_operation_binding(
        self, principal_id: str, scope_id: str, operation_occurrence_id: str
    ) -> OperationEnvelope:
        row = self._one(
            "SELECT envelope_json FROM operation_bindings WHERE principal_id = ?"
            " AND scope_id = ? AND operation_occurrence_id = ?",
            (
                identifier(principal_id, "principal_id"),
                identifier(scope_id, "scope_id"),
                identifier(operation_occurrence_id, "operation_occurrence_id"),
            ),
            f"no operation binding for {operation_occurrence_id}",
        )
        return OperationEnvelope.from_json(json.loads(row[0]))

    def list_operation_bindings(
        self, mission_id: str, *, operation_id: str | None = None
    ) -> tuple[OperationEnvelope, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if operation_id is not None:
            clauses.append("operation_id = ?")
            values.append(identifier(operation_id, "operation_id"))
        rows = self._store.connection.execute(
            f"SELECT envelope_json FROM operation_bindings WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at, operation_occurrence_id",
            tuple(values),
        ).fetchall()
        return tuple(OperationEnvelope.from_json(json.loads(row[0])) for row in rows)

    # ================================================================== read sets
    def record_read_set(self, mission_id: str, proposal_id: str, read_set: SemanticReadSet) -> str:
        """Index one proposal's semantic read-set, item by item (ADR-13).

        The whole read-set is kept in a ``read_set`` row so it round-trips exactly;
        the other rows are the per-subject index a revalidation reads.
        """

        if not isinstance(read_set, SemanticReadSet):
            raise StoreConflict("record_read_set expects a SemanticReadSet")
        mission = identifier(mission_id, "mission_id")
        proposal = identifier(proposal_id, "proposal_id")
        document = read_set.to_json()
        digest = content_hash_of(document)
        rows: list[tuple[str, str, int, str | None, str]] = [
            (
                "read_set",
                proposal,
                read_set.requirements_revision,
                digest,
                canonical_json(document),
            ),
            (
                "requirements",
                mission,
                read_set.requirements_revision,
                None,
                canonical_json({"requirements_revision": read_set.requirements_revision}),
            ),
            (
                "manager_epoch",
                mission,
                read_set.manager_epoch,
                None,
                canonical_json({"manager_epoch": read_set.manager_epoch}),
            ),
            (
                "budget_grant_revision",
                mission,
                read_set.budget_grant_revision,
                None,
                canonical_json({"budget_grant_revision": read_set.budget_grant_revision}),
            ),
        ]
        for group in (
            read_set.goal_revisions,
            read_set.method_revisions,
            read_set.observation_revisions,
            read_set.acceptance_revisions,
        ):
            for item in group:
                rows.append(
                    (
                        str(item.kind),
                        item.id,
                        item.semantic_revision,
                        item.content_hash,
                        canonical_json(item.to_json()),
                    )
                )
        for support in read_set.support_sets:
            rows.append(
                (
                    "support_set",
                    support.support_set_id,
                    support.revision,
                    support.member_digest,
                    canonical_json(support.to_json()),
                )
            )
        for scope in read_set.scope_epochs:
            rows.append(
                (
                    "scope_epoch",
                    scope.scope_id,
                    scope.validity_epoch,
                    None,
                    canonical_json(scope.to_json()),
                )
            )
        for absence in read_set.absences:
            payload = absence.to_json()
            rows.append(
                (
                    "absence",
                    content_hash_of(payload),
                    absence.range_revision,
                    None,
                    canonical_json(payload),
                )
            )
        now = self._store.now
        with self._store.transaction() as connection:
            for subject_type, subject_id, revision, content_hash, item_json in rows:
                self._execute(
                    connection,
                    "INSERT INTO plan_read_sets(mission_id,proposal_id,subject_type,subject_id,"
                    "semantic_revision,content_hash,item_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        mission,
                        proposal,
                        subject_type,
                        subject_id,
                        revision,
                        content_hash,
                        item_json,
                        now,
                    ),
                    f"read-set item {subject_type}/{subject_id} appears twice in {proposal}",
                )
        return digest

    def get_read_set(self, mission_id: str, proposal_id: str) -> SemanticReadSet:
        row = self._one(
            "SELECT item_json FROM plan_read_sets WHERE mission_id = ? AND proposal_id = ?"
            " AND subject_type = 'read_set'",
            (identifier(mission_id, "mission_id"), identifier(proposal_id, "proposal_id")),
            f"no read-set recorded for proposal {proposal_id}",
        )
        return SemanticReadSet.from_json(json.loads(row[0]))

    def list_read_set_items(self, mission_id: str, proposal_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._store.connection.execute(
            "SELECT subject_type,subject_id,semantic_revision,content_hash,item_json"
            " FROM plan_read_sets WHERE mission_id = ? AND proposal_id = ?"
            " AND subject_type <> 'read_set' ORDER BY subject_type, subject_id",
            (identifier(mission_id, "mission_id"), identifier(proposal_id, "proposal_id")),
        ).fetchall()
        return tuple(
            {
                "subject_type": row[0],
                "subject_id": row[1],
                "semantic_revision": row[2],
                "content_hash": row[3],
                "item": json.loads(row[4]),
            }
            for row in rows
        )

    def read_set_consumers(
        self, mission_id: str, subject_type: str, subject_id: str
    ) -> tuple[str, ...]:
        """Which proposals read this subject — the reverse of :meth:`record_read_set`."""

        rows = self._store.connection.execute(
            "SELECT proposal_id FROM plan_read_sets WHERE mission_id = ? AND subject_type = ?"
            " AND subject_id = ? ORDER BY proposal_id",
            (
                identifier(mission_id, "mission_id"),
                identifier(subject_type, "subject_type"),
                identifier(subject_id, "subject_id"),
            ),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    # ================================================================== commit receipts
    def record_commit_receipt(
        self,
        mission_id: str,
        *,
        command_id: str,
        delta_id: str,
        base_plan_revision: int,
        new_plan_revision: int,
        intent_hash: str,
        read_set: SemanticReadSet,
        output_identity: Mapping[str, Any],
        detail: Mapping[str, Any] | None = None,
    ) -> PlanCommitReceipt:
        """Record what one commit command did.  Replaying the command is idempotent.

        The same ``command_id`` with the same intent and read-set returns the stored
        receipt (§17.4: a replayed command yields the same receipt, not a second
        commit).  A different intent under the same id is a conflict.
        """

        if not isinstance(read_set, SemanticReadSet):
            raise StoreConflict("record_commit_receipt expects a SemanticReadSet")
        mission = identifier(mission_id, "mission_id")
        command = identifier(command_id, "command_id")
        read_document = read_set.to_json()
        read_hash = content_hash_of(read_document)
        receipt = PlanCommitReceipt(
            command_id=command,
            mission_id=mission,
            delta_id=identifier(delta_id, "delta_id"),
            base_plan_revision=index(base_plan_revision, "base_plan_revision"),
            new_plan_revision=index(new_plan_revision, "new_plan_revision"),
            intent_hash=str(intent_hash),
            read_set_hash=read_hash,
            read_set=read_set,
            output_identity=dict(output_identity),
            detail=dict(detail or {}),
        )
        stored = self._store.connection.execute(
            "SELECT * FROM plan_commit_receipts WHERE command_id = ?", (command,)
        ).fetchone()
        if stored is not None:
            if stored["intent_hash"] != receipt.intent_hash:
                raise StoreConflict(
                    f"command {command} was already applied with intent {stored['intent_hash']},"
                    f" not {receipt.intent_hash}"
                )
            if stored["read_set_hash"] != read_hash:
                raise StoreConflict(
                    f"command {command} was already applied against a different read-set"
                )
            return self._commit_receipt(stored)
        self._insert(
            "INSERT INTO plan_commit_receipts(command_id,mission_id,delta_id,base_plan_revision,"
            "new_plan_revision,intent_hash,read_set_hash,read_set_json,output_identity_json,"
            "receipt_json,applied_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                command,
                mission,
                receipt.delta_id,
                receipt.base_plan_revision,
                receipt.new_plan_revision,
                receipt.intent_hash,
                read_hash,
                canonical_json(read_document),
                canonical_json(dict(receipt.output_identity)),
                canonical_json(dict(receipt.detail)),
                self._store.now,
            ),
            f"commit receipt for {mission}@{receipt.new_plan_revision} conflicts with a stored one",
        )
        return receipt

    def get_commit_receipt(self, command_id: str) -> PlanCommitReceipt:
        row = self._one(
            "SELECT * FROM plan_commit_receipts WHERE command_id = ?",
            (identifier(command_id, "command_id"),),
            f"no commit receipt for command {command_id}",
        )
        return self._commit_receipt(row)

    def list_commit_receipts(self, mission_id: str) -> tuple[PlanCommitReceipt, ...]:
        rows = self._store.connection.execute(
            "SELECT * FROM plan_commit_receipts WHERE mission_id = ? ORDER BY new_plan_revision",
            (identifier(mission_id, "mission_id"),),
        ).fetchall()
        return tuple(self._commit_receipt(row) for row in rows)

    # ============================================ migration 17: the accept-side receipts
    def record_acceptance_receipt(
        self,
        mission_id: str,
        *,
        command_id: str,
        kind: str,
        subject_id: str,
        intent_hash: str,
        read_set_hash: str,
        event_id: str,
        output_identity: Mapping[str, Any],
        detail: Mapping[str, Any] | None = None,
    ) -> AcceptanceCommitReceipt:
        """§17.4 for the accept side: one command, one commit, one receipt.

        The unique key is ``(mission_id, command_id)``, so a second delivery of the
        same command with the same intent returns the stored receipt and a *different*
        intent under the same id is a conflict rather than a silent second write.
        Migration 17 exists precisely so this is a keyed read: P2.3c part 1 had to
        project the receipt out of the Mission's event log and page through it.
        """

        mission = identifier(mission_id, "mission_id")
        command = identifier(command_id, "command_id")
        receipt = AcceptanceCommitReceipt(
            mission_id=mission,
            command_id=command,
            kind=self._state(kind, ACCEPTANCE_RECEIPT_KINDS, "receipt kind"),
            subject_id=identifier(subject_id, "subject_id"),
            intent_hash=str(intent_hash),
            read_set_hash=str(read_set_hash),
            event_id=str(event_id),
            output_identity=dict(output_identity),
            detail=dict(detail or {}),
        )
        stored = self._store.connection.execute(
            "SELECT * FROM acceptance_commit_receipts WHERE mission_id = ? AND command_id = ?",
            (mission, command),
        ).fetchone()
        if stored is not None:
            if stored["intent_hash"] != receipt.intent_hash:
                raise StoreConflict(
                    f"command {command} was already applied with intent {stored['intent_hash']},"
                    f" not {receipt.intent_hash}"
                )
            return self._acceptance_receipt(stored)
        self._insert(
            "INSERT INTO acceptance_commit_receipts(mission_id,command_id,kind,subject_id,"
            "intent_hash,read_set_hash,event_id,output_identity_json,detail_json,applied_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mission,
                command,
                receipt.kind,
                receipt.subject_id,
                receipt.intent_hash,
                receipt.read_set_hash,
                receipt.event_id,
                canonical_json(dict(receipt.output_identity)),
                canonical_json(dict(receipt.detail)),
                self._store.now,
            ),
            f"acceptance receipt for {mission}/{command} conflicts with a stored one",
        )
        return receipt

    def find_acceptance_receipt(
        self, mission_id: str, command_id: str
    ) -> AcceptanceCommitReceipt | None:
        """The receipt of one accept-side command, or None.  One indexed read."""

        row = self._store.connection.execute(
            "SELECT * FROM acceptance_commit_receipts WHERE mission_id = ? AND command_id = ?",
            (identifier(mission_id, "mission_id"), identifier(command_id, "command_id")),
        ).fetchone()
        return None if row is None else self._acceptance_receipt(row)

    def list_acceptance_receipts(
        self, mission_id: str, *, kind: str | None = None
    ) -> tuple[AcceptanceCommitReceipt, ...]:
        sql = "SELECT * FROM acceptance_commit_receipts WHERE mission_id = ?"
        values: tuple[Any, ...] = (identifier(mission_id, "mission_id"),)
        if kind is not None:
            sql += " AND kind = ?"
            values += (self._state(kind, ACCEPTANCE_RECEIPT_KINDS, "receipt kind"),)
        rows = self._store.connection.execute(sql + " ORDER BY applied_at, command_id", values)
        return tuple(self._acceptance_receipt(row) for row in rows)

    def record_delivery_receipt(
        self, mission_id: str, receipt: DeliveryReceipt, *, command_id: str, intent_hash: str
    ) -> DeliveryReceipt:
        """Record how far one accepted output travelled (AER §6.1).

        These rows are the *only* source a Mission-root resolution may quote.  A
        receipt that arrives on a command and was never recorded here is a claim
        about the world, and "wrongly declared complete = 0" is exactly the invariant
        that a claim may not stand in for a record.
        """

        if not isinstance(receipt, DeliveryReceipt):
            raise StoreConflict("record_delivery_receipt expects a DeliveryReceipt")
        mission = identifier(mission_id, "mission_id")
        if receipt.mission_id != mission:
            raise StoreConflict(
                f"delivery receipt {receipt.receipt_id} belongs to mission "
                f"{receipt.mission_id!r}, not {mission!r}"
            )
        command = identifier(command_id, "command_id")
        document = receipt.to_json()
        stored = self._store.connection.execute(
            "SELECT * FROM delivery_receipts WHERE mission_id = ? AND command_id = ?",
            (mission, command),
        ).fetchone()
        if stored is not None:
            if stored["intent_hash"] != str(intent_hash):
                raise StoreConflict(
                    f"command {command} already recorded a delivery receipt with intent "
                    f"{stored['intent_hash']}, not {intent_hash}"
                )
            return DeliveryReceipt.from_json(json.loads(stored["receipt_json"]))
        self._insert(
            "INSERT INTO delivery_receipts(mission_id,command_id,receipt_id,acceptance_id,stage,"
            "observed_at_ms,operation_id,intent_hash,receipt_json,recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mission,
                command,
                receipt.receipt_id,
                str(receipt.acceptance_id),
                str(receipt.stage),
                int(receipt.observed_at_ms),
                None if receipt.operation_id is None else str(receipt.operation_id),
                str(intent_hash),
                canonical_json(document),
                self._store.now,
            ),
            f"delivery receipt {receipt.receipt_id} conflicts with a stored one",
        )
        return receipt

    def find_delivery_receipt(self, mission_id: str, receipt_id: str) -> DeliveryReceipt | None:
        row = self._store.connection.execute(
            "SELECT receipt_json FROM delivery_receipts WHERE mission_id = ? AND receipt_id = ?",
            (identifier(mission_id, "mission_id"), str(receipt_id)),
        ).fetchone()
        return None if row is None else DeliveryReceipt.from_json(json.loads(row["receipt_json"]))

    def list_delivery_receipts(
        self, mission_id: str, *, acceptance_id: str | None = None
    ) -> tuple[DeliveryReceipt, ...]:
        sql = "SELECT receipt_json FROM delivery_receipts WHERE mission_id = ?"
        values: tuple[Any, ...] = (identifier(mission_id, "mission_id"),)
        if acceptance_id is not None:
            sql += " AND acceptance_id = ?"
            values += (str(acceptance_id),)
        rows = self._store.connection.execute(sql + " ORDER BY receipt_id", values)
        return tuple(DeliveryReceipt.from_json(json.loads(row["receipt_json"])) for row in rows)

    def insert_acceptance_output(
        self,
        mission_id: str,
        *,
        acceptance_id: str,
        output_port: str,
        artifact_id: str,
        producer_occurrence: str,
        producer_task_ref: str,
        producer_result_id: str,
        support_revision: int,
        content_hash: str,
        source_revision: str,
        document: Mapping[str, Any],
    ) -> str:
        """Record "this Acceptance accepted that artifact at this output port".

        The index P2.3b needed and the schema did not hold.  Re-recording the same
        ``(acceptance, port, artifact)`` with the same content is a no-op — a crash
        between the result and the acceptance must not be able to double-write it —
        and with *different* content it is a conflict, because two answers to "which
        bytes were accepted at this port" is not something a later read can settle.
        """

        mission = identifier(mission_id, "mission_id")
        acceptance = identifier(acceptance_id, "acceptance_id")
        port = identifier(output_port, "output_port")
        artifact = identifier(artifact_id, "artifact_id")
        payload = canonical_json(dict(document))
        stored = self._store.connection.execute(
            "SELECT output_json FROM acceptance_outputs"
            " WHERE acceptance_id = ? AND output_port = ? AND artifact_id = ?",
            (acceptance, port, artifact),
        ).fetchone()
        if stored is not None:
            if stored["output_json"] != payload:
                raise StoreConflict(
                    f"acceptance {acceptance} already records {artifact} at port {port!r} with"
                    " different content"
                )
            return content_hash_of(dict(document))
        self._insert(
            "INSERT INTO acceptance_outputs(mission_id,acceptance_id,output_port,artifact_id,"
            "producer_occurrence,producer_task_ref,producer_result_id,support_revision,"
            "content_hash,source_revision,output_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mission,
                acceptance,
                port,
                artifact,
                identifier(producer_occurrence, "producer_occurrence"),
                identifier(producer_task_ref, "producer_task_ref"),
                str(producer_result_id),
                index(support_revision, "support_revision"),
                str(content_hash),
                str(source_revision),
                payload,
                self._store.now,
            ),
            f"acceptance output {acceptance}.{port} could not be stored",
        )
        return content_hash_of(dict(document))

    #: Which ``acceptances.validity`` values still make an indexed output usable.
    #: The same set the accept side spells as ``USABLE_ACCEPTANCE_VALIDITY``; it lives
    #: here as SQL because the filter is part of the *read*, not of a caller's loop.
    USABLE_ACCEPTANCE_VALIDITY: tuple[str, ...] = ("CURRENT",)

    def list_acceptance_outputs(
        self, mission_id: str, *, producer_occurrence: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """The recorded accepted outputs of one Mission, in a deterministic order.

        **Joined to ``acceptances`` and filtered on validity** (P2.3c part 2c, review
        F3).  ``acceptance_outputs`` has no validity column of its own — an accepted
        output is a fact about an ``Acceptance``, and duplicating the Acceptance's
        state here would be a second answer that can disagree with the first.  So the
        row is offered only while the Acceptance that recorded it is still CURRENT: a
        superseded or revoked Acceptance is history, and letting the resolver bind a
        consumer to its output would feed downstream work on the strength of an
        acceptance nobody holds any more.  ``root_contributions`` already reads the
        ``acceptances`` rows for exactly this reason; this is the same rule on the
        other lane.

        An output whose Acceptance row is missing altogether is likewise not offered:
        the index entry is a claim about an Acceptance, and an Acceptance the library
        does not hold cannot support it.
        """

        placeholders = ",".join("?" for _ in self.USABLE_ACCEPTANCE_VALIDITY)
        sql = (
            "SELECT o.output_json AS output_json FROM acceptance_outputs AS o"
            " JOIN acceptances AS a ON a.acceptance_id = o.acceptance_id"
            f" WHERE o.mission_id = ? AND a.validity IN ({placeholders})"
        )
        values: tuple[Any, ...] = (
            identifier(mission_id, "mission_id"),
            *self.USABLE_ACCEPTANCE_VALIDITY,
        )
        if producer_occurrence is not None:
            sql += " AND o.producer_occurrence = ?"
            values += (str(producer_occurrence),)
        rows = self._store.connection.execute(
            sql
            + " ORDER BY o.producer_occurrence, o.output_port, o.source_revision, o.artifact_id",
            values,
        )
        return tuple(dict(json.loads(row["output_json"])) for row in rows)

    @staticmethod
    def _acceptance_receipt(row: sqlite3.Row) -> AcceptanceCommitReceipt:
        return AcceptanceCommitReceipt(
            mission_id=row["mission_id"],
            command_id=row["command_id"],
            kind=row["kind"],
            subject_id=row["subject_id"],
            intent_hash=row["intent_hash"],
            read_set_hash=row["read_set_hash"],
            event_id=row["event_id"],
            output_identity=dict(json.loads(row["output_identity_json"])),
            detail=dict(json.loads(row["detail_json"])),
        )

    # ================================================================== internals
    def _insert_child_occurrence(
        self, connection: sqlite3.Connection, mission_id: str, binding: ChildBinding, now: float
    ) -> None:
        goal_occurrence = (
            binding.occurrence_id
            if binding.goal_occurrence_id is None
            else binding.goal_occurrence_id
        )
        self._execute(
            connection,
            "INSERT INTO method_child_occurrences(instance_id,slot_key,mission_id,occurrence_id,"
            "obligation_id,goal_occurrence_id,requiredness,reuse_policy,binding_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(binding.instance_id),
                binding.slot_key,
                mission_id,
                str(binding.occurrence_id),
                str(binding.obligation_id),
                str(goal_occurrence),
                str(binding.requiredness),
                str(binding.reuse_policy),
                canonical_json(binding.to_json()),
                now,
            ),
            f"slot {binding.slot_key} of instance {binding.instance_id!s} is already bound",
        )

    def _method_row(self, method_id: str, method_version: int) -> sqlite3.Row | None:
        return self._store.connection.execute(
            "SELECT * FROM method_contracts WHERE method_id = ? AND method_version = ?",
            (method_id, method_version),
        ).fetchone()

    @staticmethod
    def _stored_method(row: sqlite3.Row) -> StoredMethod:
        """Both halves come back through their contract codec.

        Re-deriving the registration from the indexed columns would skip the
        admission rules (§7.3: a model may not submit past DRAFT, TRIAL_ADMITTED is
        mission-scoped), which is exactly what a hand-edited row would exploit.
        """

        return StoredMethod(
            contract=MethodContract.from_json(json.loads(row["contract_json"])),
            registration=MethodRegistration.from_json(json.loads(row["registration_json"])),
        )

    @staticmethod
    def _plan_revision(row: sqlite3.Row) -> StoredPlanRevision:
        return StoredPlanRevision(
            mission_id=str(row["mission_id"]),
            revision=int(row["revision"]),
            state=str(row["state"]),
            base_revision=None if row["base_revision"] is None else int(row["base_revision"]),
            snapshot_hash=str(row["snapshot_hash"]),
            delta_id=row["delta_id"],
            read_set=SemanticReadSet.from_json(json.loads(row["read_set_json"])),
        )

    def _justification_set(self, row: sqlite3.Row) -> StoredJustificationSet:
        members = self._store.connection.execute(
            "SELECT member_json, polarity FROM support_members WHERE set_id = ?"
            " ORDER BY member_kind, member_id",
            (row["set_id"],),
        ).fetchall()
        return StoredJustificationSet(
            set_id=str(row["set_id"]),
            mission_id=str(row["mission_id"]),
            subject_kind=str(row["subject_kind"]),
            subject_id=str(row["subject_id"]),
            member_revision=int(row["member_revision"]),
            member_digest=str(row["member_digest"]),
            rule_ref=row["rule_ref"],
            members=tuple(
                (TypedRef.from_json(json.loads(item[0])), bool(item[1])) for item in members
            ),
            detail=json.loads(row["detail_json"]),
        )

    @staticmethod
    def _commit_receipt(row: sqlite3.Row) -> PlanCommitReceipt:
        return PlanCommitReceipt(
            command_id=str(row["command_id"]),
            mission_id=str(row["mission_id"]),
            delta_id=str(row["delta_id"]),
            base_plan_revision=int(row["base_plan_revision"]),
            new_plan_revision=int(row["new_plan_revision"]),
            intent_hash=str(row["intent_hash"]),
            read_set_hash=str(row["read_set_hash"]),
            read_set=SemanticReadSet.from_json(json.loads(row["read_set_json"])),
            output_identity=json.loads(row["output_identity_json"]),
            detail=json.loads(row["receipt_json"]),
        )

    @staticmethod
    def _state(value: str, allowed: tuple[str, ...], name: str) -> str:
        text = str(value)
        if text not in allowed:
            raise StoreConflict(f"{name} must be one of {allowed}, not {text!r}")
        return text

    def _one(self, sql: str, values: tuple[Any, ...], missing: str) -> sqlite3.Row:
        row = self._store.connection.execute(sql, values).fetchone()
        if row is None:
            raise StoreConflict(missing)
        return row

    def _insert(self, sql: str, values: tuple[Any, ...], conflict: str) -> None:
        with self._store.transaction() as connection:
            self._execute(connection, sql, values, conflict)

    @staticmethod
    def _execute(
        connection: sqlite3.Connection, sql: str, values: tuple[Any, ...], conflict: str
    ) -> None:
        try:
            connection.execute(sql, values)
        except sqlite3.IntegrityError as error:
            raise StoreConflict(f"{conflict}: {error}") from error


__all__ = (
    "ACCEPTANCE_RECEIPT_KINDS",
    "DIRTY_STATES",
    "METHOD_INSTANCE_STATES",
    "PLAN_REVISION_STATES",
    "AcceptanceCommitReceipt",
    "DirtyEntry",
    "HtnStore",
    "PlanCommitReceipt",
    "StoredJustificationSet",
    "StoredMethod",
    "StoredPlanRevision",
    "StoredReviewRecord",
)
