# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501  (long event / receipt literals)

"""Commit Service: the single logical writer of formal orchestration state (§15, §17.5).

Every public method is one ``Store.transaction()`` that applies a checked
proposal, appends the corresponding Events (§16.2) and, where a proposal carries a
base version, records a commit receipt so a replay returns the *same* receipt
instead of applying twice (§17.4).  Agents never call this; only the
Orchestrator, the API and the recovery path do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    Artifact,
    Attempt,
    AttemptStatus,
    Budget,
    Claim,
    ClaimStatus,
    ContractError,
    Event,
    Mission,
    MissionStatus,
    MissionStopReason,
    ResultEnvelope,
    Task,
    TaskStatus,
    ids,
)
from ..contracts.models import STEP2_IMPLEMENTED_LAYERS, sha256_hex
from ..governance.budgets import BudgetExhausted, BudgetLedger, UsageFact
from ..storage.store import DispatchIntent, Store, StoredResult, StoreError
from .state_machine import next_attempt, next_claim, next_mission, next_task

ACTOR_SYSTEM = "system"
ORCHESTRATOR_ID = "orchestrator"


class CommitRejected(StoreError):
    """The proposal violates a contract, a budget or the state machine; nothing was written."""


class MissionConflict(CommitRejected):
    """Same (tenant, idempotency_key) with a different specification."""


@dataclass(frozen=True, slots=True)
class MissionSpec:
    """Input of ``create_mission`` (§5: 任务章程)."""

    goal: str
    success_criteria: tuple[str, ...]
    tenant_id: str
    idempotency_key: str
    stop_conditions: tuple[str, ...] = ("verification_passed", "budget_exhausted")
    allowed_tools: tuple[str, ...] = ()
    risk_level: str = "sandbox"
    budget: Budget = field(default_factory=Budget)
    task_kind: str = "code"
    workspace_seed: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "tenant_id": self.tenant_id,
            "idempotency_key": self.idempotency_key,
            "stop_conditions": list(self.stop_conditions),
            "allowed_tools": list(self.allowed_tools),
            "risk_level": self.risk_level,
            "budget": self.budget.to_json(),
            "task_kind": self.task_kind,
            "workspace_seed": dict(self.workspace_seed),
        }


@dataclass(frozen=True, slots=True)
class TaskProposal:
    """What the Planner proposes (§6.3 / §15); never applied without a Commit."""

    goal: str
    rationale: str
    success_criteria: tuple[str, ...]
    verification_policy: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    budget: Budget
    priority: float = 1.0
    root_goal: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "rationale": self.rationale,
            "success_criteria": list(self.success_criteria),
            "verification_policy": list(self.verification_policy),
            "allowed_tools": list(self.allowed_tools),
            "budget": self.budget.to_json(),
            "priority": self.priority,
            "root_goal": self.root_goal,
        }

    @classmethod
    def from_json(cls, value: object) -> TaskProposal:
        if not isinstance(value, Mapping):
            raise ContractError("task proposal must be an object")
        allowed = {
            "goal",
            "rationale",
            "success_criteria",
            "verification_policy",
            "allowed_tools",
            "budget",
            "priority",
            "root_goal",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ContractError(f"task proposal has unknown fields: {sorted(unknown)}")
        missing = {"goal", "rationale", "success_criteria", "verification_policy"} - set(value)
        if missing:
            raise ContractError(f"task proposal is missing fields: {sorted(missing)}")
        return cls(
            goal=str(value["goal"]),
            rationale=str(value["rationale"]),
            success_criteria=tuple(value["success_criteria"]),
            verification_policy=tuple(value["verification_policy"]),
            allowed_tools=tuple(value.get("allowed_tools", ())),
            budget=Budget.from_json(value.get("budget", {})),
            priority=float(value.get("priority", 1.0)),
            root_goal=str(value.get("root_goal", "")),
        )


@dataclass(frozen=True, slots=True)
class Reservation:
    tokens: int
    cost_micros: int


def mission_account(mission_id: str) -> str:
    return f"budget:{mission_id}"


def task_account(task_id: str) -> str:
    return f"budget:{task_id}"


class CommitService:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._ledger = BudgetLedger(store)

    @property
    def store(self) -> Store:
        return self._store

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    # -------------------------------------------------------------- events
    def _emit(
        self,
        event_type: str,
        mission_id: str,
        *,
        key: str,
        task_id: str | None = None,
        attempt_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        actor_type: str = ACTOR_SYSTEM,
        actor_id: str = ORCHESTRATOR_ID,
    ) -> Event:
        idempotency_key = f"{event_type}:{key}"
        return self._store.append_event(
            Event(
                id=ids.event_id(idempotency_key),
                type=event_type,
                trace_id=ids.trace_id(mission_id),
                mission_id=mission_id,
                task_id=task_id,
                attempt_id=attempt_id,
                actor_type=actor_type,
                actor_id=actor_id,
                payload=dict(payload or {}),
                idempotency_key=idempotency_key,
                created_at=self._store.now,
            )
        )

    # ------------------------------------------------------------- missions
    def create_mission(self, spec: MissionSpec) -> tuple[Mission, bool]:
        """Idempotent on (tenant_id, idempotency_key); a different spec is a conflict."""

        spec_hash = sha256_hex(spec.to_json())
        with self._store.transaction():
            found = self._store.find_mission(spec.tenant_id, spec.idempotency_key)
            if found is not None:
                mission, stored_hash = found
                if stored_hash != spec_hash:
                    raise MissionConflict(
                        f"mission {mission.id} already exists with a different specification"
                    )
                return mission, False
            mission_id = ids.mission_id(spec.tenant_id, spec.idempotency_key)
            mission = Mission(
                id=mission_id,
                goal=spec.goal,
                success_criteria=spec.success_criteria,
                stop_conditions=spec.stop_conditions,
                allowed_tools=spec.allowed_tools,
                risk_level=spec.risk_level,
                budget=spec.budget,
                tenant_id=spec.tenant_id,
                status=MissionStatus.CREATED,
                created_at=self._store.now,
                version=1,
                idempotency_key=spec.idempotency_key,
                final_report={
                    "task_kind": spec.task_kind,
                    "workspace_seed": dict(spec.workspace_seed),
                },
            )
            self._store.insert_mission(mission, spec_hash=spec_hash)
            self._ledger.open_account(
                account_id=mission_account(mission_id),
                scope="mission",
                parent_id=None,
                mission_id=mission_id,
                limits=spec.budget,
            )
            self._emit(
                "MissionCreated",
                mission_id,
                key=mission_id,
                payload={
                    "goal": spec.goal,
                    "budget": spec.budget.to_json(),
                    "spec_hash": spec_hash,
                },
                actor_type="user",
                actor_id=spec.tenant_id,
            )
            return mission, True

    def begin_planning(self, mission_id: str) -> Mission:
        with self._store.transaction():
            mission = self._require_mission(mission_id)
            if mission.status is MissionStatus.PLANNING:
                return mission
            updated = next_mission(mission, MissionStatus.PLANNING)
            self._store.update_mission(updated, expected_version=mission.version)
            self._emit("MissionPlanning", mission_id, key=mission_id, payload={})
            return updated

    def create_service_intent(
        self,
        *,
        kind: str,
        subject_id: str,
        mission_id: str,
        account_id: str,
        creation_key: str,
        input_id: str,
        input_hash: str,
        config: Mapping[str, Any],
        reservation: Reservation,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> DispatchIntent:
        """Reserve + intent for a Planner / Critic call (D22); idempotent per ``subject_id``."""

        with self._store.transaction():
            existing = self._store.get_intent_for_subject(subject_id)
            if existing is not None:
                return existing
            self._ledger.reserve(
                account_id=account_id,
                subject_id=subject_id,
                tokens=reservation.tokens,
                cost_micros=reservation.cost_micros,
                counts_attempt=False,
            )
            intent = DispatchIntent(
                intent_id=ids.intent_id(kind, subject_id),
                kind=kind,
                subject_id=subject_id,
                mission_id=mission_id,
                state="PENDING",
                version=1,
                creation_key=creation_key,
                input_id=input_id,
                input_hash=input_hash,
                config=dict(config),
                expected_turn_id=None,
                agent_id=None,
                receipt=None,
                lease_owner=None,
                lease_expires_at=None,
                replays=0,
                created_at=self._store.now,
            )
            self._store.insert_intent(intent)
            self._emit(
                "BudgetReserved",
                mission_id,
                key=subject_id,
                task_id=task_id,
                attempt_id=attempt_id,
                payload={
                    "subject_id": subject_id,
                    "kind": kind,
                    "tokens": reservation.tokens,
                    "cost_micros": reservation.cost_micros,
                },
            )
            return intent

    def commit_task_proposal(
        self,
        mission_id: str,
        proposal: TaskProposal,
        *,
        base_version: int,
        source: Mapping[str, Any],
    ) -> tuple[Task, Mapping[str, Any]]:
        """Apply the Planner's single-Task proposal (step 2) after the §24 step-3 checks."""

        proposal_json = proposal.to_json()
        commit = ids.commit_id(
            {"kind": "task_proposal", "mission_id": mission_id, **proposal_json}, base_version
        )
        with self._store.transaction():
            receipt = self._store.get_receipt(commit)
            if receipt is not None:
                task = self._store.get_task(str(receipt["task_id"]))
                assert task is not None
                return task, receipt
            mission = self._require_mission(mission_id)
            if mission.version != base_version:
                raise CommitRejected(
                    f"proposal is based on mission version {base_version}, current is {mission.version}"
                )
            if mission.status is not MissionStatus.PLANNING:
                raise CommitRejected(f"mission {mission_id} is {mission.status}, not PLANNING")
            if self._store.list_tasks(mission_id):
                raise CommitRejected("step 2 accepts exactly one Task per Mission")
            self._check_task_proposal(mission, proposal)
            task_id = ids.task_id(mission_id, 1)
            task = Task(
                id=task_id,
                mission_id=mission_id,
                parent_task_ids=(),
                dependency_ids=(),
                goal=proposal.goal,
                rationale=proposal.rationale,
                success_criteria=proposal.success_criteria,
                verification_policy=proposal.verification_policy,
                allowed_tools=proposal.allowed_tools,
                budget=proposal.budget,
                priority=proposal.priority,
                status=TaskStatus.READY,  # no dependencies: satisfied from the start (§25.1)
                version=1,
                root_goal=proposal.root_goal or mission.goal,
                created_at=self._store.now,
            )
            self._store.insert_task(task, ordinal=1)
            self._ledger.open_account(
                account_id=task_account(task_id),
                scope="task",
                parent_id=mission_account(mission_id),
                mission_id=mission_id,
                limits=proposal.budget,
            )
            activated = next_mission(mission, MissionStatus.ACTIVE)
            self._store.update_mission(activated, expected_version=mission.version)
            receipt = {
                "commit_id": commit,
                "task_id": task_id,
                "mission_version": activated.version,
                "proposal_hash": sha256_hex(proposal_json),
                "source": dict(source),
            }
            self._store.insert_receipt(
                commit_id=commit,
                kind="task_proposal",
                subject_id=task_id,
                base_version=base_version,
                proposal_hash=receipt["proposal_hash"],
                receipt=receipt,
            )
            self._emit(
                "TaskCommitted",
                mission_id,
                key=task_id,
                task_id=task_id,
                payload={"commit_id": commit, "proposal": proposal_json, "source": dict(source)},
            )
            self._emit("MissionActivated", mission_id, key=mission_id, payload={"task_id": task_id})
            return task, receipt

    def _check_task_proposal(self, mission: Mission, proposal: TaskProposal) -> None:
        """§24 step 3: relation to the root goal, tools, success criteria, budget legality."""

        if not proposal.success_criteria:
            raise CommitRejected("task proposal has no success criteria")
        if not proposal.rationale.strip():
            raise CommitRejected("task proposal cannot explain its relation to the Mission (§19.5)")
        extra_tools = set(proposal.allowed_tools) - set(mission.allowed_tools)
        if extra_tools:
            raise CommitRejected(
                f"task proposal asks for tools outside the Mission: {sorted(extra_tools)}"
            )
        if not proposal.budget.fits_within(mission.budget):
            raise CommitRejected("task budget exceeds the Mission budget (§18.2)")
        unsupported = set(proposal.verification_policy) - STEP2_IMPLEMENTED_LAYERS
        if unsupported:
            raise CommitRejected(
                f"verification layers not deployed in this build: {sorted(unsupported)}"
            )

    def fail_planning(
        self,
        mission_id: str,
        *,
        reason: str,
        detail: Mapping[str, Any],
        stop_reason: MissionStopReason = MissionStopReason.PLANNING_FAILED,
    ) -> Mission:
        with self._store.transaction():
            mission = self._require_mission(mission_id)
            if mission.status is MissionStatus.FAILED:
                return mission
            updated = next_mission(
                mission,
                MissionStatus.FAILED,
                stop_reason=str(stop_reason),
                final_report={
                    **dict(mission.final_report or {}),
                    "planning_failure": {"reason": reason, **dict(detail)},
                },
            )
            self._store.update_mission(updated, expected_version=mission.version)
            self._emit(
                "MissionFailed",
                mission_id,
                key=mission_id,
                payload={
                    "stop_reason": updated.stop_reason,
                    "reason": reason,
                    "detail": dict(detail),
                },
            )
            return updated

    def cancel_mission(self, mission_id: str) -> Mission:
        with self._store.transaction():
            mission = self._require_mission(mission_id)
            if mission.status is MissionStatus.CANCELLED:
                return mission
            updated = next_mission(
                mission, MissionStatus.CANCELLED, stop_reason=str(MissionStopReason.CANCELLED)
            )
            self._store.update_mission(updated, expected_version=mission.version)
            for task in self._store.list_tasks(mission_id):
                if task.status is TaskStatus.VERIFYING:
                    active = next_task(task, TaskStatus.ACTIVE)
                    self._store.update_task(active, expected_version=task.version)
                    self._emit(
                        "TaskVerificationAbandoned",
                        mission_id,
                        key=task.id,
                        task_id=task.id,
                        payload={},
                    )
                    task = active
                if task.status in {TaskStatus.READY, TaskStatus.ACTIVE}:
                    self._store.update_task(
                        next_task(task, TaskStatus.CANCELLED), expected_version=task.version
                    )
                    self._emit(
                        "TaskCancelled", mission_id, key=task.id, task_id=task.id, payload={}
                    )
                    for attempt in self._store.list_attempts(task.id):
                        if attempt.status in {
                            AttemptStatus.PENDING,
                            AttemptStatus.CLAIMED,
                            AttemptStatus.RUNNING,
                            AttemptStatus.SUBMITTED,
                            AttemptStatus.VERIFYING,
                        }:
                            self._store.update_attempt(
                                next_attempt(attempt, AttemptStatus.CANCELLED),
                                expected_version=attempt.version,
                            )
                            self._emit(
                                "AttemptCancelled",
                                mission_id,
                                key=attempt.id,
                                task_id=task.id,
                                attempt_id=attempt.id,
                                payload={},
                            )
                        stored = self._store.find_result_for_attempt(attempt.id)
                        if stored is not None and stored.verification_state in {
                            "PENDING",
                            "RUNNING",
                        }:
                            self._store.set_result_verification(
                                stored.envelope.id, state="REJECTED", verdict=None
                            )
            # every open dispatch intent of this Mission is closed; reservations are released
            for intent in self._store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            ):
                if intent.mission_id != mission_id:
                    continue
                self._settle_intent(intent, "FAILED")
                reservation = self._ledger.reservation(intent.subject_id)
                if reservation is not None and reservation["state"] != "SETTLED":
                    task_id = (
                        intent.subject_id.split(":attempt-")[0]
                        if ":attempt-" in intent.subject_id
                        else None
                    )
                    self._settle_subject(intent.subject_id, mission_id, task_id=task_id)
            self._emit("MissionCancelled", mission_id, key=mission_id, payload={})
            return updated

    def settle_intent(self, intent_id: str, state: str) -> DispatchIntent:
        """Close a dispatch intent (SETTLED / FAILED) through the single writer (D2)."""

        with self._store.transaction():
            intent = self._require_intent(intent_id)
            return self._settle_intent(intent, state)

    def _settle_intent(self, intent: DispatchIntent, state: str) -> DispatchIntent:
        if intent.state == state:
            return intent
        updated = DispatchIntent(
            **{**intent.to_json(), "state": state, "version": intent.version + 1}
        )
        self._store.update_intent(updated, expected_version=intent.version)
        self._emit(
            "IntentSettled",
            intent.mission_id,
            key=f"{intent.subject_id}:{state}",
            attempt_id=intent.subject_id if intent.kind == "attempt" else None,
            payload={"intent_id": intent.intent_id, "kind": intent.kind, "state": state},
        )
        return updated

    # ------------------------------------------------------------- attempts
    def create_attempt(
        self,
        task_id: str,
        *,
        role: str,
        model: str,
        prompt_version: str,
        context_version: str,
        reservation: Reservation,
        intent_config: Mapping[str, Any],
        input_hash: str,
        retry_of: str | None = None,
        feedback: Sequence[str] = (),
    ) -> tuple[Attempt, DispatchIntent]:
        """Atomic Reserve + Attempt(PENDING) + dispatch intent (ORCH-BUILD §4.3 step 1).

        Refuses (nothing written) when the Task is not READY/ACTIVE or the budget
        does not fit; the caller turns ``BudgetExhausted`` into a Mission stop.
        """

        with self._store.transaction():
            task = self._require_task(task_id)
            if task.status not in {TaskStatus.READY, TaskStatus.ACTIVE}:
                raise CommitRejected(f"task {task_id} is {task.status}; no new Attempt")
            existing = self._store.list_attempts(task_id)
            open_attempts = [
                a
                for a in existing
                if a.status
                in {
                    AttemptStatus.PENDING,
                    AttemptStatus.CLAIMED,
                    AttemptStatus.RUNNING,
                    AttemptStatus.SUBMITTED,
                    AttemptStatus.VERIFYING,
                }
            ]
            if open_attempts:
                raise CommitRejected(
                    f"task {task_id} already has an open Attempt {open_attempts[0].id}"
                )
            ordinal = len(existing) + 1
            attempt_id = ids.attempt_id(task_id, ordinal)
            try:
                self._ledger.reserve(
                    account_id=task_account(task_id),
                    subject_id=attempt_id,
                    tokens=reservation.tokens,
                    cost_micros=reservation.cost_micros,
                    counts_attempt=True,
                )
            except BudgetExhausted as error:
                raise error
            attempt = Attempt(
                id=attempt_id,
                task_id=task_id,
                mission_id=task.mission_id,
                role=role,
                model=model,
                prompt_version=prompt_version,
                context_version=context_version,
                budget_reserved=Budget(
                    max_tokens=reservation.tokens, max_cost_micros=reservation.cost_micros
                ),
                lease_owner=None,
                lease_expires_at=None,
                status=AttemptStatus.PENDING,
                retry_of=retry_of,
                created_at=self._store.now,
                version=1,
                ordinal=ordinal,
                creation_key=attempt_id,
                input_id="attempt-input",
                task_version=task.version,
                input_hash=input_hash,
                feedback=tuple(feedback),
            )
            self._store.insert_attempt(attempt)
            intent = DispatchIntent(
                intent_id=ids.intent_id("attempt", attempt_id),
                kind="attempt",
                subject_id=attempt_id,
                mission_id=task.mission_id,
                state="PENDING",
                version=1,
                creation_key=attempt.creation_key,
                input_id=attempt.input_id,
                input_hash=input_hash,
                config=dict(intent_config),
                expected_turn_id=None,
                agent_id=None,
                receipt=None,
                lease_owner=None,
                lease_expires_at=None,
                replays=0,
                created_at=self._store.now,
            )
            self._store.insert_intent(intent)
            if task.status is TaskStatus.READY:
                self._store.update_task(
                    next_task(task, TaskStatus.ACTIVE, attempt_count=task.attempt_count + 1),
                    expected_version=task.version,
                )
            else:
                self._store.update_task(
                    next_task(task, attempt_count=task.attempt_count + 1),
                    expected_version=task.version,
                )
            self._emit(
                "AttemptCreated",
                task.mission_id,
                key=attempt_id,
                task_id=task_id,
                attempt_id=attempt_id,
                payload={
                    "role": role,
                    "model": model,
                    "retry_of": retry_of,
                    "ordinal": ordinal,
                    "feedback": list(feedback),
                },
            )
            self._emit(
                "BudgetReserved",
                task.mission_id,
                key=attempt_id,
                task_id=task_id,
                attempt_id=attempt_id,
                payload={
                    "subject_id": attempt_id,
                    "tokens": reservation.tokens,
                    "cost_micros": reservation.cost_micros,
                },
            )
            return attempt, intent

    def claim_intent(
        self, intent_id: str, *, owner: str, lease_seconds: float
    ) -> DispatchIntent | None:
        """CAS PENDING → CLAIMED (§17.1: only one executor per Attempt)."""

        with self._store.transaction():
            intent = self._store.get_intent(intent_id)
            if intent is None:
                return None
            now = self._store.now
            if (
                intent.state in {"CLAIMED", "AGENT_CREATED"}
                and intent.lease_expires_at is not None
                and intent.lease_expires_at > now
                and intent.lease_owner != owner
            ):
                return None  # someone else holds a live lease
            if intent.state not in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
                return None
            claimed = DispatchIntent(
                **{
                    **intent.to_json(),
                    "state": "CLAIMED" if intent.state == "PENDING" else intent.state,
                    "version": intent.version + 1,
                    "lease_owner": owner,
                    "lease_expires_at": now + lease_seconds,
                    "replays": intent.replays + (1 if intent.state != "PENDING" else 0),
                }
            )
            self._store.update_intent(claimed, expected_version=intent.version)
            if intent.kind == "attempt":
                attempt = self._require_attempt(intent.subject_id)
                if attempt.status is AttemptStatus.PENDING:
                    self._store.update_attempt(
                        next_attempt(
                            attempt,
                            AttemptStatus.CLAIMED,
                            lease_owner=owner,
                            lease_expires_at=now + lease_seconds,
                        ),
                        expected_version=attempt.version,
                    )
                    self._emit(
                        "AttemptClaimed",
                        attempt.mission_id,
                        key=attempt.id,
                        task_id=attempt.task_id,
                        attempt_id=attempt.id,
                        payload={"owner": owner},
                    )
            return claimed

    def record_agent_created(
        self, intent_id: str, *, agent_id: str, expected_turn_id: str
    ) -> DispatchIntent:
        with self._store.transaction():
            intent = self._require_intent(intent_id)
            if intent.state in {"AGENT_CREATED", "SUBMITTED", "SETTLED"}:
                if intent.agent_id != agent_id:
                    raise CommitRejected(
                        f"intent {intent_id} is bound to agent {intent.agent_id}, not {agent_id}"
                    )
                return intent
            updated = DispatchIntent(
                **{
                    **intent.to_json(),
                    "state": "AGENT_CREATED",
                    "version": intent.version + 1,
                    "agent_id": agent_id,
                    "expected_turn_id": expected_turn_id,
                }
            )
            self._store.update_intent(updated, expected_version=intent.version)
            if intent.kind == "attempt":
                attempt = self._require_attempt(intent.subject_id)
                self._store.update_attempt(
                    next_attempt(attempt, agent_id=agent_id, turn_id=expected_turn_id),
                    expected_version=attempt.version,
                )
            self._emit(
                "AgentCreated",
                intent.mission_id,
                key=intent.subject_id,
                attempt_id=intent.subject_id if intent.kind == "attempt" else None,
                payload={
                    "agent_id": agent_id,
                    "expected_turn_id": expected_turn_id,
                    "kind": intent.kind,
                },
            )
            return updated

    def record_submitted(self, intent_id: str, *, receipt: Mapping[str, Any]) -> DispatchIntent:
        """Save the real SDK receipt; the Attempt becomes RUNNING (§25.2 start)."""

        with self._store.transaction():
            intent = self._require_intent(intent_id)
            if intent.state in {"SUBMITTED", "SETTLED"}:
                return intent
            if intent.state != "AGENT_CREATED":
                raise CommitRejected(
                    f"intent {intent_id} is {intent.state}; cannot record a submission"
                )
            if receipt.get("turn_id") != intent.expected_turn_id:
                raise CommitRejected("SDK receipt turn_id differs from the expected turn identity")
            updated = DispatchIntent(
                **{
                    **intent.to_json(),
                    "state": "SUBMITTED",
                    "version": intent.version + 1,
                    "receipt": dict(receipt),
                }
            )
            self._store.update_intent(updated, expected_version=intent.version)
            if intent.kind == "attempt":
                attempt = self._require_attempt(intent.subject_id)
                self._store.update_attempt(
                    next_attempt(attempt, AttemptStatus.RUNNING), expected_version=attempt.version
                )
                self._emit(
                    "AttemptStarted",
                    intent.mission_id,
                    key=attempt.id,
                    task_id=attempt.task_id,
                    attempt_id=attempt.id,
                    payload={"receipt": dict(receipt)},
                )
            else:
                self._emit(
                    "InputSubmitted",
                    intent.mission_id,
                    key=intent.subject_id,
                    payload={"receipt": dict(receipt), "kind": intent.kind},
                )
            return updated

    def renew_lease(
        self, attempt_id: str, *, owner: str, lease_seconds: float, liveness: Mapping[str, Any]
    ) -> Attempt:
        """HeartbeatReceived (§16.2): renew only on evidence the executor is alive (D6')."""

        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            if attempt.lease_owner not in (None, owner):
                # §17.6: a lapsed lease may be taken over; a live one may not.
                if (
                    attempt.lease_expires_at is not None
                    and attempt.lease_expires_at > self._store.now
                ):
                    raise CommitRejected(f"attempt {attempt_id} is leased to {attempt.lease_owner}")
            expires = self._store.now + lease_seconds
            progress = liveness.get("progress")
            marker = None if progress is None else int(progress)
            progress_at = attempt.progress_at
            if marker != attempt.progress_marker or progress_at is None:
                progress_at = self._store.now
            updated = next_attempt(
                attempt,
                lease_owner=owner,
                lease_expires_at=expires,
                progress_marker=marker,
                progress_at=progress_at,
            )
            self._store.update_attempt(updated, expected_version=attempt.version)
            self._emit(
                "HeartbeatReceived",
                attempt.mission_id,
                key=f"{attempt_id}:{int(expires * 1000)}",
                task_id=attempt.task_id,
                attempt_id=attempt_id,
                payload={"owner": owner, "lease_expires_at": expires, "liveness": dict(liveness)},
            )
            return updated

    def mark_attempt_lost(self, attempt_id: str, *, reason: str) -> Attempt:
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            if attempt.status is AttemptStatus.LOST:
                return attempt
            updated = next_attempt(attempt, AttemptStatus.LOST, failure={"reason": reason})
            self._store.update_attempt(updated, expected_version=attempt.version)
            if not self._ledger.has_unknown_usage(attempt.id):
                self._settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)
            self._emit(
                "AttemptLost",
                attempt.mission_id,
                key=attempt.id,
                task_id=attempt.task_id,
                attempt_id=attempt.id,
                payload={"reason": reason},
            )
            return updated

    def mark_attempt_timed_out(
        self, attempt_id: str, *, reason: str, detail: Mapping[str, Any]
    ) -> Attempt:
        """D6' stall: alive executor with no blocker and no progress within stall_seconds."""

        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            if attempt.status is AttemptStatus.TIMED_OUT:
                return attempt
            updated = next_attempt(
                attempt, AttemptStatus.TIMED_OUT, failure={"reason": reason, **dict(detail)}
            )
            self._store.update_attempt(updated, expected_version=attempt.version)
            if not self._ledger.has_unknown_usage(attempt.id):
                self._settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)
            self._emit(
                "AttemptTimedOut",
                attempt.mission_id,
                key=attempt.id,
                task_id=attempt.task_id,
                attempt_id=attempt.id,
                payload={"reason": reason, **dict(detail)},
            )
            return updated

    # -------------------------------------------------------------- results
    def import_usage(self, subject_id: str, mission_id: str, facts: Sequence[UsageFact]) -> int:
        with self._store.transaction():
            return self._ledger.import_usage(
                subject_id=subject_id, mission_id=mission_id, facts=facts
            )

    def settle_subject(
        self, subject_id: str, mission_id: str, *, task_id: str | None = None
    ) -> Mapping[str, Any]:
        with self._store.transaction():
            return self._settle_subject(subject_id, mission_id, task_id=task_id)

    def _settle_subject(
        self, subject_id: str, mission_id: str, *, task_id: str | None
    ) -> Mapping[str, Any]:
        settled = self._ledger.settle(subject_id=subject_id)
        self._emit(
            "BudgetReleased",
            mission_id,
            key=subject_id,
            task_id=task_id,
            payload={
                "subject_id": subject_id,
                "settled_tokens": settled["settled_tokens"],
                "settled_cost_micros": settled["settled_cost_micros"],
                "released_tokens": int(settled["reserved_tokens"])
                - int(settled["settled_tokens"] or 0),
                "unpriced": bool(settled["unpriced"]),
            },
        )
        return settled

    def record_result(
        self,
        attempt_id: str,
        *,
        envelope: ResultEnvelope,
        turn_id: str,
        artifacts: Sequence[Artifact],
        usage_refs: Sequence[str],
    ) -> StoredResult:
        """ResultSubmitted (§16.2): Attempt RUNNING → SUBMITTED, Task ACTIVE → VERIFYING.

        Idempotent on ``(attempt_id, turn_id)``: a duplicate delivery returns the
        stored result and appends nothing (§17.4).
        """

        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            existing = self._store.find_result_for_attempt(attempt_id)
            if existing is not None and existing.turn_id == turn_id:
                return existing
            if attempt.status is not AttemptStatus.RUNNING:
                raise CommitRejected(
                    f"attempt {attempt_id} is {attempt.status}; cannot accept a result"
                )
            if envelope.attempt_id != attempt_id or envelope.task_id != attempt.task_id:
                raise CommitRejected("result identity does not match the Attempt")
            if attempt.turn_id != turn_id:
                raise CommitRejected("result turn differs from the Attempt's bound turn")
            stored = StoredResult(
                envelope=envelope,
                turn_id=turn_id,
                verification_state="PENDING",
                verdict=None,
                received_at=self._store.now,
                artifacts=tuple(artifact.id for artifact in artifacts),
                usage_refs=tuple(usage_refs),
            )
            for artifact in artifacts:
                self._store.upsert_artifact(artifact)
            self._store.insert_result(stored)
            self._store.fault("mid_commit", "attempt")
            for index, proposal in enumerate(envelope.claims, start=1):
                self._store.upsert_claim(
                    Claim(
                        id=ids.claim_id(envelope.id, index),
                        content=proposal.content,
                        type=proposal.type,
                        status=ClaimStatus.PROPOSED,
                        source_task=attempt.task_id,
                        source_attempt=attempt_id,
                        evidence=envelope.evidence,
                        dependencies=envelope.used_knowledge,
                        verifier_results=(),
                        confidence_metadata={"self_reported_confidence": proposal.confidence},
                        supersedes=None,
                        mission_id=attempt.mission_id,
                        result_id=envelope.id,
                    )
                )
            self._store.update_attempt(
                next_attempt(attempt, AttemptStatus.SUBMITTED, result_id=envelope.id),
                expected_version=attempt.version,
            )
            task = self._require_task(attempt.task_id)
            self._store.update_task(
                next_task(task, TaskStatus.VERIFYING), expected_version=task.version
            )
            self._emit(
                "ResultSubmitted",
                attempt.mission_id,
                key=envelope.id,
                task_id=attempt.task_id,
                attempt_id=attempt_id,
                payload={
                    "result_id": envelope.id,
                    "outcome": str(envelope.outcome),
                    "artifacts": list(stored.artifacts),
                    "claims": len(envelope.claims),
                },
                actor_type="agent",
                actor_id=attempt.agent_id or attempt_id,
            )
            return stored

    def reject_result(
        self, attempt_id: str, *, turn_id: str, reason: str, detail: Mapping[str, Any]
    ) -> Attempt:
        """An invalid / forged submission: ResultRejected, Attempt → RETRY_WAIT, Task stays ACTIVE (S2-07)."""

        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            if attempt.status is AttemptStatus.RETRY_WAIT:
                return attempt
            if attempt.status is not AttemptStatus.RUNNING:
                raise CommitRejected(f"attempt {attempt_id} is {attempt.status}; nothing to reject")
            updated = next_attempt(
                attempt, AttemptStatus.RETRY_WAIT, failure={"reason": reason, **dict(detail)}
            )
            self._store.update_attempt(updated, expected_version=attempt.version)
            self._emit(
                "ResultRejected",
                attempt.mission_id,
                key=f"{attempt_id}:{turn_id}",
                task_id=attempt.task_id,
                attempt_id=attempt_id,
                payload={"reason": reason, "detail": dict(detail), "turn_id": turn_id},
            )
            return updated

    def start_verification(self, result_id: str) -> StoredResult:
        with self._store.transaction():
            stored = self._require_result(result_id)
            attempt = self._require_attempt(stored.envelope.attempt_id)
            if stored.verification_state == "PENDING":
                self._store.set_result_verification(result_id, state="RUNNING", verdict=None)
                self._store.update_attempt(
                    next_attempt(attempt, AttemptStatus.VERIFYING), expected_version=attempt.version
                )
                for claim in self._store.list_claims(result_id):
                    self._store.upsert_claim(next_claim(claim, ClaimStatus.UNDER_REVIEW))
                self._emit(
                    "VerificationStarted",
                    attempt.mission_id,
                    key=result_id,
                    task_id=attempt.task_id,
                    attempt_id=attempt.id,
                    payload={"result_id": result_id},
                )
            return self._require_result(result_id)

    def record_verification_layer(
        self, result_id: str, *, layer: str, status: str, detail: Mapping[str, Any]
    ) -> None:
        with self._store.transaction():
            stored = self._require_result(result_id)
            self._store.upsert_verification(
                result_id=result_id,
                attempt_id=stored.envelope.attempt_id,
                layer=layer,
                status=status,
                detail=detail,
            )
            self._emit(
                "VerificationLayerRecorded",
                stored.envelope.mission_id,
                key=f"{result_id}:{layer}",
                task_id=stored.envelope.task_id,
                attempt_id=stored.envelope.attempt_id,
                payload={"layer": layer, "status": status, "summary": detail.get("summary")},
            )

    def accept_result(
        self, result_id: str, *, verifier_results: Sequence[Mapping[str, Any]]
    ) -> Task:
        """PASS (§24 step 11): claims → VERIFIED, Task → COMPLETED, Mission → COMPLETED."""

        with self._store.transaction():
            stored = self._require_result(result_id)
            if stored.verification_state == "DONE" and stored.verdict == "PASS":
                return self._require_task(stored.envelope.task_id)
            attempt = self._require_attempt(stored.envelope.attempt_id)
            task = self._require_task(stored.envelope.task_id)
            mission = self._require_mission(stored.envelope.mission_id)
            self._store.set_result_verification(result_id, state="DONE", verdict="PASS")
            for claim in self._store.list_claims(result_id):
                self._store.upsert_claim(
                    next_claim(
                        claim,
                        ClaimStatus.VERIFIED,
                        verifier_results=tuple(dict(item) for item in verifier_results),
                    )
                )
            self._store.update_attempt(
                next_attempt(attempt, AttemptStatus.COMPLETED), expected_version=attempt.version
            )
            completed = next_task(
                task,
                TaskStatus.COMPLETED,
                accepted_result_id=result_id,
                accepted_artifacts=stored.artifacts,
            )
            self._store.update_task(completed, expected_version=task.version)
            self._settle_subject(attempt.id, mission.id, task_id=task.id)
            self._emit(
                "VerificationPassed",
                mission.id,
                key=result_id,
                task_id=task.id,
                attempt_id=attempt.id,
                payload={
                    "result_id": result_id,
                    "layers": [dict(item) for item in verifier_results],
                },
            )
            self._emit(
                "TaskCompleted",
                mission.id,
                key=task.id,
                task_id=task.id,
                payload={"result_id": result_id, "artifacts": list(stored.artifacts)},
            )
            return completed

    def judge_mission(
        self, mission_id: str, *, judgments: Sequence[Mapping[str, Any]], summary: str
    ) -> Mission:
        """Mission-level success judgment, independent of the Task PASS (D21, ORCH §12.4).

        ``judgments`` carries one entry per ``Mission.success_criteria`` item with
        ``met: bool``; all met → COMPLETED, otherwise FAILED(mission_criteria_unmet)
        while the Task stays COMPLETED.
        """

        with self._store.transaction():
            mission = self._require_mission(mission_id)
            if mission.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}:
                return mission
            tasks = self._store.list_tasks(mission_id)
            if not tasks or any(task.status is not TaskStatus.COMPLETED for task in tasks):
                raise CommitRejected("mission judgment requires every Task to be COMPLETED")
            criteria = list(mission.success_criteria)
            if [item.get("criterion") for item in judgments] != criteria:
                raise CommitRejected("judgments must cover the Mission success criteria in order")
            met = all(bool(item.get("met")) for item in judgments)
            task = tasks[0]
            report = {
                **dict(mission.final_report or {}),
                "accepted_result_id": task.accepted_result_id,
                "accepted_artifacts": list(task.accepted_artifacts),
                "summary": summary,
                "attempts": task.attempt_count,
                "success_criteria": [dict(item) for item in judgments],
            }
            self._emit(
                "MissionSuccessJudged",
                mission_id,
                key=f"{mission_id}:{mission.version}",
                payload={"met": met, "judgments": [dict(item) for item in judgments]},
            )
            if met:
                done = next_mission(
                    mission,
                    MissionStatus.COMPLETED,
                    stop_reason=str(MissionStopReason.VERIFICATION_PASSED),
                    final_report=report,
                )
                self._store.update_mission(done, expected_version=mission.version)
                self._emit(
                    "MissionCompleted",
                    mission_id,
                    key=mission_id,
                    payload={"stop_reason": done.stop_reason, "final_report": report},
                )
                return done
            failed = next_mission(
                mission,
                MissionStatus.FAILED,
                stop_reason="mission_criteria_unmet",
                final_report=report,
            )
            self._store.update_mission(failed, expected_version=mission.version)
            self._emit(
                "MissionFailed",
                mission_id,
                key=mission_id,
                payload={"stop_reason": failed.stop_reason, "final_report": report},
            )
            return failed

    def fail_result(self, result_id: str, *, failures: Sequence[Mapping[str, Any]]) -> Task:
        """FAIL: claims → REJECTED, Attempt → RETRY_WAIT, Task VERIFYING → ACTIVE (retry decision is separate)."""

        with self._store.transaction():
            stored = self._require_result(result_id)
            if stored.verification_state == "DONE" and stored.verdict == "FAIL":
                return self._require_task(stored.envelope.task_id)
            attempt = self._require_attempt(stored.envelope.attempt_id)
            task = self._require_task(stored.envelope.task_id)
            self._store.set_result_verification(result_id, state="DONE", verdict="FAIL")
            for claim in self._store.list_claims(result_id):
                self._store.upsert_claim(
                    next_claim(
                        claim,
                        ClaimStatus.REJECTED,
                        verifier_results=tuple(dict(item) for item in failures),
                    )
                )
            self._store.update_attempt(
                next_attempt(
                    attempt,
                    AttemptStatus.RETRY_WAIT,
                    failure={
                        "reason": "verification_failed",
                        "failures": [dict(item) for item in failures],
                    },
                ),
                expected_version=attempt.version,
            )
            active = next_task(task, TaskStatus.ACTIVE)
            self._store.update_task(active, expected_version=task.version)
            self._settle_subject(attempt.id, attempt.mission_id, task_id=task.id)
            self._emit(
                "VerificationFailed",
                attempt.mission_id,
                key=result_id,
                task_id=task.id,
                attempt_id=attempt.id,
                payload={"result_id": result_id, "failures": [dict(item) for item in failures]},
            )
            return active

    def stop_task(
        self, task_id: str, *, stop_reason: MissionStopReason, detail: Mapping[str, Any]
    ) -> Task:
        """Stop condition reached (§25.1 ACTIVE → FAILED) and the Mission with it."""

        with self._store.transaction():
            task = self._require_task(task_id)
            if task.status is TaskStatus.FAILED:
                return task
            mission = self._require_mission(task.mission_id)
            failed = next_task(task, TaskStatus.FAILED, failure_reason=str(stop_reason))
            self._store.update_task(failed, expected_version=task.version)
            report = {
                **dict(mission.final_report or {}),
                "stop_reason": str(stop_reason),
                "detail": dict(detail),
                "attempts": task.attempt_count,
                "completed_parts": self._completed_parts(task_id),
            }
            done = next_mission(
                mission, MissionStatus.FAILED, stop_reason=str(stop_reason), final_report=report
            )
            self._store.update_mission(done, expected_version=mission.version)
            self._emit(
                "TaskFailed",
                mission.id,
                key=task_id,
                task_id=task_id,
                payload={"stop_reason": str(stop_reason), "detail": dict(detail)},
            )
            self._emit(
                "MissionFailed",
                mission.id,
                key=mission.id,
                payload={"stop_reason": str(stop_reason), "final_report": report},
            )
            return failed

    def _completed_parts(self, task_id: str) -> list[dict[str, Any]]:
        parts = []
        for attempt in self._store.list_attempts(task_id):
            stored = self._store.find_result_for_attempt(attempt.id)
            parts.append(
                {
                    "attempt_id": attempt.id,
                    "status": str(attempt.status),
                    "result_id": None if stored is None else stored.envelope.id,
                    "verdict": None if stored is None else stored.verdict,
                    "artifacts": [] if stored is None else list(stored.artifacts),
                }
            )
        return parts

    # ------------------------------------------------------------ lookups
    def _require_mission(self, mission_id: str) -> Mission:
        mission = self._store.get_mission(mission_id)
        if mission is None:
            raise CommitRejected(f"unknown mission {mission_id}")
        return mission

    def _require_task(self, task_id: str) -> Task:
        task = self._store.get_task(task_id)
        if task is None:
            raise CommitRejected(f"unknown task {task_id}")
        return task

    def _require_attempt(self, attempt_id: str) -> Attempt:
        attempt = self._store.get_attempt(attempt_id)
        if attempt is None:
            raise CommitRejected(f"unknown attempt {attempt_id}")
        return attempt

    def _require_intent(self, intent_id: str) -> DispatchIntent:
        intent = self._store.get_intent(intent_id)
        if intent is None:
            raise CommitRejected(f"unknown dispatch intent {intent_id}")
        return intent

    def _require_result(self, result_id: str) -> StoredResult:
        stored = self._store.get_result(result_id)
        if stored is None:
            raise CommitRejected(f"unknown result {result_id}")
        return stored


__all__ = (
    "CommitRejected",
    "CommitService",
    "MissionConflict",
    "MissionSpec",
    "Reservation",
    "TaskProposal",
    "mission_account",
    "task_account",
)
