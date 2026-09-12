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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..artifacts.versioning import next_versions
from ..contracts import (
    TERMINAL_ATTEMPT,
    TERMINAL_TASK,
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
    ResultOutcome,
    Task,
    TaskStatus,
    ids,
)
from ..contracts.models import (
    STEP2_IMPLEMENTED_LAYERS,
    default_change_policy,
    jsonable,
    sha256_hex,
)
from ..governance.budgets import AccountSnapshot, BudgetError, BudgetLedger, UsageFact
from ..governance.domains import CODE_DOMAIN, DomainProfileV1, check_against_domain, resolve_domain
from ..governance.policies import DeploymentPolicy
from ..graph.changes import (
    ChangeLimits,
    GraphChangeRejected,
    TaskGraphChange,
    node_budget,
    validate_change,
)
from ..graph.task_graph import GraphRejected, TaskBudgetFloor, TaskGraphProposal, validate_graph
from ..memory.claims import grade_claim
from ..memory.summaries import refresh_summaries
from ..memory.verified_knowledge import KnowledgeIndex, KnowledgeRecord
from ..observability.lineage import lineage
from ..planning.manager import conflict_task, inherit_limits, synthesis_task, terminal_task
from ..scheduling.allocator import OPEN_ATTEMPT_STATES
from ..scheduling.backpressure import (
    STATE_KEY,
    BackpressureLimits,
    BackpressureState,
    Observation,
    Transition,
    evaluate,
)
from ..storage.store import DispatchIntent, Store, StoredResult, StoreError
from ..verification.conflicts import Contradiction, find_contradiction
from .action_commits import ActionCommitsMixin
from .human_commits import HumanCommitsMixin
from .policy_commits import PolicyCommitsMixin
from .state_machine import next_attempt, next_claim, next_mission, next_task

SUBMITTED_STATES = frozenset({AttemptStatus.SUBMITTED, AttemptStatus.VERIFYING})

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
    untrusted_sources: tuple[str, ...] = ()  # step 4 (D4-12): path prefixes of external content
    synthesis: Mapping[str, Any] | None = None  # step 4 (D4-8): fixed synthesis Task template
    conflict_reserve_tokens: int = 0  # step 4 (D4-20): tokens set aside for Conflict Tasks
    domain: str = CODE_DOMAIN  # P3.3 (D1): the domain profile this Mission freezes

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
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
        if self.untrusted_sources:
            data["untrusted_sources"] = list(self.untrusted_sources)
        if self.synthesis is not None:
            data["synthesis"] = dict(self.synthesis)
        if self.conflict_reserve_tokens:
            data["conflict_reserve_tokens"] = self.conflict_reserve_tokens
        if self.domain != CODE_DOMAIN:
            # A07: the default must not change ``spec_hash`` — a Host that re-sends the same
            # request after upgrading would otherwise get a MissionConflict
            data["domain"] = self.domain
        return data


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
    tool_calls: int = 0  # step 6 (D6-8): the Attempt's tool-call cap, reserved up front


def mission_account(mission_id: str) -> str:
    return f"budget:{mission_id}"


GLOBAL_ACCOUNT = "budget:global"  # step 6 (D6-1): §18.2 "Global Budget" above every Mission


def task_account(task_id: str) -> str:
    return f"budget:{task_id}"


class CommitService(
    ActionCommitsMixin, HumanCommitsMixin, PolicyCommitsMixin
):  # step 7: the action ledger + approvals half; step 9: the policy registry half
    def __init__(
        self,
        store: Store,
        *,
        conflict_tasks: bool = True,
        global_budget: Budget | None = None,
        deployed_layers: frozenset[str] = STEP2_IMPLEMENTED_LAYERS,
        task_floor: TaskBudgetFloor | None = None,
        candidates_for: Callable[[str], int] | None = None,
    ) -> None:
        self._store = store
        # P3.1 fix F-ORCH-1: the Task budget floor the Graph Manager applies — only the
        # Orchestrator injects one (None = no floor, every earlier construction unchanged);
        # ``candidates_for`` gives a Mission's candidates per Task from its bound policy
        self._task_floor = task_floor
        self._candidates_for = candidates_for
        self._ledger = BudgetLedger(store)
        self._conflict_tasks = conflict_tasks  # D4-19: False = defer every conflict
        self._global_budget = global_budget  # D6-1: None = no deployment-wide cap
        # host support 0.9.8: the verification layers this deployment runs — without local
        # code execution ``code_test`` is not among them; the Graph Manager checks, the
        # system default policies and the conflict path all follow it
        self._deployed_layers = frozenset(deployed_layers)
        # D6-8: the orchestrator installs the gateway's executed-call counter (subject → count)
        # so every settlement path books the tool-call fact without threading it through
        self.tool_calls_for: Callable[[str], int] | None = None

    def _candidates(self, mission_id: str) -> int:
        """Candidates per Task of ``mission_id``'s bound policy (1 when nobody told us)."""

        if self._candidates_for is None:
            return 1
        return max(1, int(self._candidates_for(mission_id)))

    # ----------------------------------------------------------- backpressure
    def backpressure_state(self) -> BackpressureState:
        return BackpressureState.from_json(self._store.get_scheduler_state(STATE_KEY))

    def record_backpressure(
        self,
        observation: Observation,
        *,
        limits: BackpressureLimits,
        mission_ids: Sequence[str],
    ) -> tuple[BackpressureState, list[Transition]]:
        """One evaluation step (D6-2'): the new state and, when a dimension crossed a
        watermark, the ``BackpressureRaised`` / ``BackpressureCleared`` events on every
        active Mission's timeline — state and events in one transaction (review P1-4).
        The state document keeps a bounded log of transitions: that log is the single
        truth for ``scheduler.json`` / ``metrics.json`` (review P1-11)."""

        with self._store.transaction():
            previous = BackpressureState.from_json(self._store.get_scheduler_state(STATE_KEY))
            state, transitions = evaluate(previous, observation, limits)
            raw = self._store.get_scheduler_state(STATE_KEY) or {}
            peaks = {str(k): int(v) for k, v in dict(raw.get("peaks") or {}).items()}
            new_peak = False
            for dimension in ("running_attempts", "pending_dispatch", "pending_verifications"):
                observed = observation.value(dimension)
                if observed > peaks.get(dimension, 0):  # review P2-4: the real peak
                    peaks[dimension] = observed
                    new_peak = True
            if not transitions and not new_peak and previous.observation is not None:
                return state, []
            document = state.to_json()
            document["peaks"] = peaks
            log = list(raw.get("log") or [])
            for transition in transitions:
                log.append({**transition.to_json(), "at": observation.observed_at})
            document["log"] = log[-200:]
            document["limits"] = limits.to_json()
            self._store.put_scheduler_state(STATE_KEY, document)
            for transition in transitions:
                for mission_id in mission_ids:
                    self._emit(
                        transition.event_type,
                        mission_id,
                        key=f"{transition.dimension}:{state.changes}:{mission_id}",
                        payload={**transition.to_json(), "since": state.since},
                    )
            return state, transitions

    # ------------------------------------------------------------ tool audit
    def record_tool_rejected(
        self,
        mission_id: str,
        *,
        task_id: str | None,
        attempt_id: str | None,
        run_id: str,
        call_key: str,
        record: Mapping[str, Any],
    ) -> Event:
        """§21.1 last step / §21.3 "对可疑指令进行隔离和审计": a refused tool call is
        a durable fact on the Mission's timeline (arguments reduced to their keys and
        the path, never file contents)."""

        arguments = dict(record.get("arguments") or {})
        with self._store.transaction():
            return self._emit(
                "ToolCallRejected",
                mission_id,
                key=call_key,  # review P1-3: the SDK call id — stable across a restart
                task_id=task_id,
                attempt_id=attempt_id,
                payload={
                    "tool": record.get("tool"),
                    "reason": record.get("error_code"),
                    "stage": record.get("stage"),
                    "outcome": record.get("outcome"),
                    "argument_keys": sorted(arguments),
                    "path": str(arguments["path"])[:200]  # review P2-5: bounded, never content
                    if isinstance(arguments.get("path"), str)
                    else None,
                    "run_id": run_id,
                },
            )

    def record_tool_call(
        self, *, call_key: str, subject_id: str, mission_id: str, tool: str
    ) -> bool:
        """An executed tool call of an Attempt — the tool-call dimension's fact (review P1-3)."""

        return self._store.record_tool_call(
            call_key=call_key,
            subject_id=subject_id,
            mission_id=mission_id,
            tool=tool,
            outcome="succeeded",
        )

    def record_reservation_held(
        self, subject_id: str, mission_id: str, *, task_id: str | None, reason: str
    ) -> Event:
        """L3-3 (plan §6.2): a reservation an UNKNOWN charge keeps occupied is visible on the
        timeline — once per subject — and listed in ``costs.json``; never auto-released."""

        with self._store.transaction():
            reservation = self._ledger.reservation(subject_id) or {}
            return self._emit(
                "ReservationHeld",
                mission_id,
                key=subject_id,
                task_id=task_id,
                attempt_id=subject_id if task_id else None,
                payload={
                    "subject_id": subject_id,
                    "reason": reason,
                    "reserved_tokens": reservation.get("reserved_tokens"),
                    "reserved_cost_micros": reservation.get("reserved_cost_micros"),
                },
            )

    # --------------------------------------------------------- profile health
    PROFILE_HEALTH_KEY = "profile_health"

    def profile_health(self) -> dict[str, dict[str, Any]]:
        raw = self._store.get_scheduler_state(self.PROFILE_HEALTH_KEY) or {}
        return {str(k): dict(v) for k, v in dict(raw.get("profiles") or {}).items()}

    def unavailable_until(self) -> dict[str, float]:
        """profile → cooldown end, for the router (D6-5')."""

        return {
            pid: float(entry["unavailable_until"])
            for pid, entry in self.profile_health().items()
            if entry.get("unavailable_until") is not None
        }

    def record_profile_failure(
        self,
        profile_id: str,
        *,
        error: Mapping[str, Any] | None,
        threshold: int,
        cooldown_seconds: float,
        mission_ids: Sequence[str],
    ) -> float | None:
        """A provider-unavailable turn failure on ``profile_id``; ``threshold`` of them in
        a row put the profile into a cooldown (``RuntimeProfileUnavailable`` on every
        active Mission's timeline).  Returns the cooldown end when tripped."""

        with self._store.transaction():
            raw = self._store.get_scheduler_state(self.PROFILE_HEALTH_KEY) or {"profiles": {}}
            profiles = dict(raw.get("profiles") or {})
            entry = dict(profiles.get(profile_id) or {"failures": 0, "unavailable_until": None})
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["last_error"] = jsonable(error or {})
            entry["last_failure_at"] = self._store.now
            until: float | None = None
            if entry["failures"] >= max(1, threshold):
                until = self._store.now + float(cooldown_seconds)
                entry["unavailable_until"] = until
                entry["trips"] = int(entry.get("trips", 0)) + 1
                entry["failures"] = 0
            profiles[profile_id] = entry
            self._store.put_scheduler_state(self.PROFILE_HEALTH_KEY, {"profiles": profiles})
            if until is not None:
                for mission_id in mission_ids:
                    self._emit(
                        "RuntimeProfileUnavailable",
                        mission_id,
                        key=f"{profile_id}:{entry['trips']}:{mission_id}",
                        payload={
                            "profile_id": profile_id,
                            "reason": "provider_unavailable",
                            "until": until,
                            "trips": entry["trips"],
                            "last_error": entry["last_error"],
                        },
                    )
            return until

    def record_profile_success(self, profile_id: str) -> None:
        """A committed turn on ``profile_id`` closes its failure streak and any cooldown."""

        with self._store.transaction():
            raw = self._store.get_scheduler_state(self.PROFILE_HEALTH_KEY) or {"profiles": {}}
            profiles = dict(raw.get("profiles") or {})
            entry = dict(profiles.get(profile_id) or {})
            if not entry or (
                entry.get("failures", 0) == 0 and entry.get("unavailable_until") is None
            ):
                return
            entry["failures"] = 0
            entry["unavailable_until"] = None
            entry["recovered_at"] = self._store.now
            profiles[profile_id] = entry
            self._store.put_scheduler_state(self.PROFILE_HEALTH_KEY, {"profiles": profiles})

    def record_profile_unavailable(
        self,
        profile_id: str,
        *,
        reason: str,
        until: float | None,
        mission_ids: Sequence[str],
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        with self._store.transaction():
            for mission_id in mission_ids:
                self._emit(
                    "RuntimeProfileUnavailable",
                    mission_id,
                    key=f"{profile_id}:{reason}:{(detail or {}).get('intent_id', '')}:{mission_id}",
                    payload={
                        "profile_id": profile_id,
                        "reason": reason,
                        "until": until,
                        **dict(detail or {}),
                    },
                )

    def global_account(self) -> AccountSnapshot | None:
        """The deployment-wide account (§18.2 Global Budget), if this deployment set one."""

        if self._global_budget is None:
            return None
        with self._store.transaction():
            try:
                return self._ledger.account(GLOBAL_ACCOUNT)
            except BudgetError:
                return None

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
    def create_mission(
        self,
        spec: MissionSpec,
        *,
        provider_kind: str = "unknown",
        policy_defaults: Mapping[str, Any] | None = None,
        policy_pin: Mapping[str, Any] | None = None,
    ) -> tuple[Mission, bool]:
        """Idempotent on (tenant_id, idempotency_key); a different spec is a conflict."""

        spec_hash = sha256_hex(spec.to_json())
        try:  # P3.3 (D1): an unknown domain is refused before anything is written
            domain = resolve_domain(spec.domain)
        except KeyError as error:
            raise CommitRejected(f"unknown domain profile {spec.domain!r}") from error
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
            # review fix: with a Global Budget the Mission's unnamed dimensions are inherited, and
            # the Mission contract must say so — graph checks compare against it (D6-1')
            effective_budget = (
                inherit_limits(spec.budget, self._global_budget)
                if self._global_budget is not None
                else spec.budget
            )
            mission = Mission(
                id=mission_id,
                goal=spec.goal,
                success_criteria=spec.success_criteria,
                stop_conditions=spec.stop_conditions,
                allowed_tools=spec.allowed_tools,
                risk_level=spec.risk_level,
                budget=effective_budget,
                tenant_id=spec.tenant_id,
                status=MissionStatus.CREATED,
                created_at=self._store.now,
                version=1,
                idempotency_key=spec.idempotency_key,
                final_report={
                    "task_kind": spec.task_kind,
                    "workspace_seed": dict(spec.workspace_seed),
                    "untrusted_sources": list(spec.untrusted_sources),
                    "conflict_reserve_tokens": int(spec.conflict_reserve_tokens),
                    "conflict_reserve_remaining": int(spec.conflict_reserve_tokens),
                    **({} if spec.synthesis is None else {"synthesis": dict(spec.synthesis)}),
                },
            )
            self._store.insert_mission(mission, spec_hash=spec_hash)
            parent: str | None = None
            limits = spec.budget
            if self._global_budget is not None:  # D6-1: Global → Mission (§18.2), never the reverse
                self._ledger.open_account(
                    account_id=GLOBAL_ACCOUNT,
                    scope="global",
                    parent_id=None,
                    mission_id="global",
                    limits=self._global_budget,
                )
                parent = GLOBAL_ACCOUNT
                # a dimension the Mission does not name is inherited from the Global cap
                # (review P0-2: ``fits_within`` treats an unnamed child dimension as unbounded)
                limits = effective_budget
            self._ledger.open_account(  # BudgetError (does not fit the Global) rolls everything back
                account_id=mission_account(mission_id),
                scope="mission",
                parent_id=parent,
                mission_id=mission_id,
                limits=limits,
            )
            # step 9 (plan D9-3'): the policy version this Mission runs under, in the same
            # transaction — a later promotion or configuration never changes it silently
            binding = self.bind_policy(
                mission_id,
                provider_kind=provider_kind,
                default_params=policy_defaults,
                pin=policy_pin,
            )
            self._store.bind_mission_domain(
                mission_id,
                domain_id=domain.id,
                domain_version=domain.version,
                snapshot=domain.to_json(),
            )
            self._emit(
                "MissionCreated",
                mission_id,
                key=mission_id,
                payload={
                    "goal": spec.goal,
                    "budget": spec.budget.to_json(),
                    "spec_hash": spec_hash,
                    "policy_version_id": binding["version_id"],
                    "domain_id": domain.id,
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
                mission_id=mission_id,
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
                ready_at=self._store.now,  # step 5: waiting_age starts here (review P2-4)
                context={"graph_version": 1},
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

    def domain_for(self, mission_id: str) -> DomainProfileV1:
        """The frozen domain profile of this Mission; ``code-v1`` for anything created
        before domain binding existed (plan D1, A07)."""

        binding = self._store.get_mission_domain(mission_id)
        if binding is None:
            return resolve_domain(None)
        try:
            domain = DomainProfileV1.from_json(binding["json"])
            if (domain.id, domain.version) != (binding["domain_id"], binding["domain_version"]):
                raise ValueError("frozen domain identity mismatch")
            return domain
        except (KeyError, TypeError, ValueError) as error:
            raise CommitRejected(f"invalid frozen domain: {error}") from error

    def _check_system_template(self, domain: DomainProfileV1, task: Task) -> None:
        """A Task the *system* writes (a conflict or synthesis template) against the
        Mission's domain.  These two go straight to ``insert_task`` and so bypass both
        graph gates; without this they are the hole the domain cannot see."""

        problems = check_against_domain(
            domain,
            key=f"system template {task.kind}",
            success_criteria=task.success_criteria,
            verification_policy=task.verification_policy,
        )
        if problems:
            raise CommitRejected("domain: " + "; ".join(problems))

    def _check_task_proposal(self, mission: Mission, proposal: TaskProposal) -> None:
        """§24 step 3: relation to the root goal, tools, success criteria, budget legality."""

        if not proposal.success_criteria:
            raise CommitRejected("task proposal has no success criteria")
        # P3.3 (D1) gate 3 of 5: a single Task proposal / the Manager's ``add_task``
        problems = check_against_domain(
            self.domain_for(mission.id),
            key="task proposal",
            success_criteria=proposal.success_criteria,
            verification_policy=proposal.verification_policy,
        )
        if problems:
            raise CommitRejected("domain: " + "; ".join(problems))
        if not proposal.rationale.strip():
            raise CommitRejected("task proposal cannot explain its relation to the Mission (§19.5)")
        extra_tools = set(proposal.allowed_tools) - set(mission.allowed_tools)
        if extra_tools:
            raise CommitRejected(
                f"task proposal asks for tools outside the Mission: {sorted(extra_tools)}"
            )
        if not proposal.budget.fits_within(mission.budget):
            raise CommitRejected("task budget exceeds the Mission budget (§18.2)")
        tests = [c for c in proposal.success_criteria if c.startswith("pytest:")]
        if "code_test" not in self._deployed_layers and tests:  # review round 1 P1-1
            raise CommitRejected(
                "verification_policy_undeployed: pytest criteria need local code execution, "
                f"which this deployment has turned off: {tests}"
            )
        unsupported = set(proposal.verification_policy) - self._deployed_layers
        if unsupported:
            raise CommitRejected(
                f"verification_policy_undeployed: layers not deployed in this build {sorted(unsupported)}"
            )

    def commit_task_graph(
        self,
        mission_id: str,
        proposal: TaskGraphProposal,
        *,
        base_version: int,
        source: Mapping[str, Any],
    ) -> tuple[list[Task], Mapping[str, Any]]:
        """Apply the Planner's whole Task DAG proposal atomically (step 3, D3-2/D3-3).

        The Graph Manager checks (``validate_graph``) run first; a rejection writes
        only a ``TaskGraphRejected`` event and leaves the formal graph untouched.
        Roots start READY, everything else BLOCKED (§25.1); ids follow the
        deterministic topological order so a replay yields the same receipt.
        """

        proposal_json = proposal.to_json()
        commit = ids.commit_id(
            {"kind": "task_graph", "mission_id": mission_id, **proposal_json}, base_version
        )
        try:
            return self._commit_task_graph(
                mission_id,
                proposal,
                commit,
                proposal_json,
                base_version=base_version,
                source=source,
            )
        except GraphRejected as error:
            # the write transaction rolled back; the rejection itself is a durable fact
            self._emit(
                "TaskGraphRejected",
                mission_id,
                key=f"{mission_id}:{base_version}:{sha256_hex(proposal_json)[:12]}:{source.get('intent_id', '')}",
                payload={"reason": error.reason, "detail": error.detail, "source": dict(source)},
            )
            raise CommitRejected(f"task graph rejected ({error.reason}): {error.detail}") from error

    def _commit_task_graph(
        self,
        mission_id: str,
        proposal: TaskGraphProposal,
        commit: str,
        proposal_json: Mapping[str, Any],
        *,
        base_version: int,
        source: Mapping[str, Any],
    ) -> tuple[list[Task], Mapping[str, Any]]:
        with self._store.transaction():
            receipt = self._store.get_receipt(commit)
            if receipt is not None:
                replayed = [self._require_task(task_id) for task_id in receipt["task_ids"]]
                return replayed, receipt
            mission = self._require_mission(mission_id)
            if mission.version != base_version:
                raise CommitRejected(
                    f"proposal is based on mission version {base_version}, current is {mission.version}"
                )
            if mission.status is not MissionStatus.PLANNING:
                raise CommitRejected(f"mission {mission_id} is {mission.status}, not PLANNING")
            if self._store.list_tasks(mission_id):
                raise CommitRejected(
                    "the Mission already has a committed graph (static DAG, step 3)"
                )
            graph = validate_graph(  # GraphRejected handled by the caller
                mission,
                proposal,
                deployed_layers=self._deployed_layers,
                task_floor=self._task_floor,
                candidates=self._candidates(mission.id),
                domain=self.domain_for(mission.id),
            )
            key_to_id = {
                key: ids.task_id(mission_id, ordinal)
                for ordinal, key in enumerate(graph.order, start=1)
            }
            tasks: list[Task] = []
            for ordinal, key in enumerate(graph.order, start=1):
                node = graph.node(key)
                dependencies = tuple(key_to_id[dependency] for dependency in node.dependencies)
                task = Task(
                    id=key_to_id[key],
                    mission_id=mission_id,
                    parent_task_ids=(),
                    dependency_ids=dependencies,
                    goal=node.goal,
                    rationale=node.rationale,
                    success_criteria=node.success_criteria,
                    verification_policy=node.verification_policy,
                    allowed_tools=node.allowed_tools,
                    budget=node.budget,
                    priority=node.priority,
                    status=TaskStatus.READY if not dependencies else TaskStatus.BLOCKED,
                    version=1,
                    root_goal=mission.goal,
                    created_at=self._store.now,
                    outputs=node.outputs,
                    ready_at=self._store.now if not dependencies else None,
                    context={"graph_version": 1},
                )
                self._store.insert_task(task, ordinal=ordinal)
                self._ledger.open_account(
                    account_id=task_account(task.id),
                    scope="task",
                    parent_id=mission_account(mission_id),
                    mission_id=mission_id,
                    limits=node.budget,
                )
                tasks.append(task)
                self._emit(
                    "TaskCommitted",
                    mission_id,
                    key=task.id,
                    task_id=task.id,
                    payload={
                        "commit_id": commit,
                        "key": key,
                        "dependencies": list(dependencies),
                        "proposal": node.to_json(),
                        "source": dict(source),
                    },
                )
            terminal_id = key_to_id[graph.terminal_key]
            template = (mission.final_report or {}).get("synthesis")
            if template:  # D4-8: the fixed synthesis Task depends on every Planner leaf
                synthesis = synthesis_task(
                    mission,
                    task_id=ids.task_id(mission_id, len(tasks) + 1),
                    template=template,
                    leaves=[key_to_id[key] for key in graph.order if key in set(graph.leaves)],
                    now=self._store.now,
                    default_policy=tuple(
                        layer
                        for layer in self.domain_for(mission_id).synthesis_default_policy
                        if layer in self._deployed_layers
                    )
                    or default_change_policy(self._deployed_layers),
                )
                self._check_system_template(  # P3.3 (D1) gate 5 of 5
                    self.domain_for(mission_id), synthesis
                )
                self._store.insert_task(synthesis, ordinal=len(tasks) + 1)
                self._ledger.open_account(
                    account_id=task_account(synthesis.id),
                    scope="task",
                    parent_id=mission_account(mission_id),
                    mission_id=mission_id,
                    limits=synthesis.budget,
                )
                tasks.append(synthesis)
                terminal_id = synthesis.id
                self._emit(
                    "TaskCommitted",
                    mission_id,
                    key=synthesis.id,
                    task_id=synthesis.id,
                    payload={
                        "commit_id": commit,
                        "key": "synthesis",
                        "dependencies": list(synthesis.dependency_ids),
                        "proposal": dict(template),
                        "source": {"template": "synthesis"},
                    },
                )
            activated = next_mission(
                mission,
                MissionStatus.ACTIVE,
                final_report={**dict(mission.final_report or {}), "graph_version": 1},
            )
            self._store.update_mission(activated, expected_version=mission.version)
            receipt = {
                "commit_id": commit,
                "graph_version": 1,
                "task_ids": [task.id for task in tasks],
                "terminal_task_id": terminal_id,
                "mission_version": activated.version,
                "proposal_hash": sha256_hex(proposal_json),
                "source": dict(source),
                "warnings": list(graph.warnings),
            }
            self._store.insert_receipt(
                commit_id=commit,
                kind="task_graph",
                subject_id=mission_id,
                base_version=base_version,
                proposal_hash=receipt["proposal_hash"],
                receipt=receipt,
            )
            self._emit(
                "TaskGraphCommitted",
                mission_id,
                key=f"{mission_id}:graph-1",
                payload={
                    "commit_id": commit,
                    "task_ids": receipt["task_ids"],
                    "terminal_task_id": receipt["terminal_task_id"],
                    "edges": {task.id: list(task.dependency_ids) for task in tasks},
                    "warnings": list(graph.warnings),
                },
            )
            self._emit(
                "MissionActivated",
                mission_id,
                key=mission_id,
                payload={"task_ids": receipt["task_ids"]},
            )
            return tasks, receipt

    # ------------------------------------------------------ graph changes (step 5)
    def commit_graph_change(
        self,
        mission_id: str,
        change: TaskGraphChange,
        *,
        source: Mapping[str, Any],
        limits: ChangeLimits | None = None,
        allow_rebase: bool = True,
    ) -> tuple[list[Task], Mapping[str, Any]]:
        """Apply a Manager's Task DAG change atomically (D5-2 / D5-3 / D5-10).

        The proposal must be based on the current ``graph_version`` (CAS); a stale
        base is rebased automatically when its operations touch none of the Tasks the
        intervening changes affected, otherwise refused.  A repeated delivery of the
        same proposal on the same base returns the same receipt.  Validation failures
        write only ``TaskGraphChangeRejected`` and leave the formal graph untouched.
        """

        limits = limits or ChangeLimits()
        try:
            return self._commit_graph_change(
                mission_id, change, source=source, limits=limits, allow_rebase=allow_rebase
            )
        except GraphChangeRejected as error:
            self._emit(
                "TaskGraphChangeRejected",
                mission_id,
                key=f"{mission_id}:{change.base_graph_version}:{change.proposal_hash[:12]}:{source.get('intent_id', '')}",
                payload={
                    "reason": error.reason,
                    "detail": error.detail,
                    "base_graph_version": change.base_graph_version,
                    "basis": dict(change.basis),
                    "source": dict(source),
                },
            )
            raise CommitRejected(
                f"graph change rejected ({error.reason}): {error.detail}"
            ) from error

    def _commit_graph_change(
        self,
        mission_id: str,
        change: TaskGraphChange,
        *,
        source: Mapping[str, Any],
        limits: ChangeLimits,
        allow_rebase: bool,
    ) -> tuple[list[Task], Mapping[str, Any]]:
        with self._store.transaction():
            mission = self._require_mission(mission_id)
            if mission.status is not MissionStatus.ACTIVE:
                raise GraphChangeRejected(
                    "mission_not_active", f"mission {mission_id} is {mission.status}"
                )
            report = dict(mission.final_report or {})
            current = int(report.get("graph_version") or 1)
            change_id = ids.commit_id(
                {
                    "kind": "graph_change",
                    "mission_id": mission_id,
                    "proposal": change.proposal_hash,
                },
                change.base_graph_version,
            )
            receipt = self._store.get_receipt(change_id)
            if receipt is not None:  # S5-07: the same proposal on the same base, once
                replayed = [self._require_task(task_id) for task_id in receipt["new_task_ids"]]
                return replayed, receipt
            rebased_from: int | None = None
            if change.base_graph_version != current:
                if not allow_rebase or change.base_graph_version > current:
                    raise GraphChangeRejected(
                        "stale_base",
                        f"proposal is based on graph version {change.base_graph_version}, current is {current}",
                    )
                touched: set[str] = set()
                for applied in self._store.list_graph_changes(
                    mission_id, since_version=change.base_graph_version
                ):
                    touched.update(str(t) for t in applied.get("affected_task_ids", []))
                overlap = touched & change.referenced_task_ids()
                if overlap:
                    raise GraphChangeRejected(
                        "stale_base",
                        f"proposal is based on graph version {change.base_graph_version}, current is {current}; "
                        f"it touches tasks changed since: {sorted(overlap)}",
                    )
                rebased_from = change.base_graph_version
            tasks = self._store.list_tasks(mission_id)
            proposals_by_attempt: dict[str, int] = {}
            for task in tasks:
                for parent in (
                    task.context.get("proposed_by_attempt", [])
                    if isinstance(task.context.get("proposed_by_attempt"), list)
                    else []
                ):
                    proposals_by_attempt[str(parent)] = proposals_by_attempt.get(str(parent), 0) + 1
            committed: dict[str, int] = {}
            for task in tasks:
                if task.status is TaskStatus.CANCELLED or task.id in change.referenced_task_ids():
                    account = self._ledger.account(task_account(task.id))
                    committed[task.id] = int(account.settled_tokens) + int(account.reserved_tokens)
            validated = validate_change(
                mission,
                tasks,
                change,
                limits=limits,
                proposals_by_attempt=proposals_by_attempt,
                committed_tokens_by_task=committed,
                deployed_layers=self._deployed_layers,
                task_floor=self._task_floor,
                candidates=self._candidates(mission.id),
                domain=self.domain_for(mission.id),
            )
            by_id = {task.id: task for task in tasks}
            new_version = current + 1
            key_to_id: dict[str, str] = {}
            key_to_ordinal: dict[str, int] = {}
            ordinal = len(tasks)
            created: list[Task] = []
            source_attempt = str(change.basis.get("attempt_id") or "")
            for key in validated.order:  # topological among the new nodes → ordinal ≡ order
                ordinal += 1
                key_to_id[key] = ids.task_id(mission_id, ordinal)
                key_to_ordinal[key] = ordinal
            for key in validated.order:
                node = next(n for n in validated.new_nodes if n.key == key)
                dependencies = tuple(key_to_id.get(d, d) for d in node.dependencies)
                supersedes = next(
                    (old for old, rep in validated.superseded.items() if rep == key), None
                )
                context: dict[str, Any] = {
                    "graph_version": new_version,
                    "change_id": change_id,
                    "role": node.role,
                    "proposed_by_attempt": [source_attempt] if source_attempt else [],
                }
                if supersedes is not None:
                    old = by_id[supersedes]
                    context["supersedes_task"] = supersedes
                    context["supersede_depth"] = int(old.context.get("supersede_depth", 0)) + 1
                deps_done = all(
                    by_id[d].status is TaskStatus.COMPLETED for d in dependencies if d in by_id
                ) and all(d in by_id for d in dependencies)
                task = Task(
                    id=key_to_id[key],
                    mission_id=mission_id,
                    parent_task_ids=tuple(node.parent_task_ids)
                    or ((supersedes,) if supersedes else ()),
                    dependency_ids=dependencies,
                    goal=node.goal,
                    rationale=node.rationale,
                    success_criteria=node.success_criteria,
                    verification_policy=node.verification_policy,
                    allowed_tools=node.allowed_tools or mission.allowed_tools,
                    budget=node_budget(node, validated, mission),
                    priority=node.priority,
                    status=TaskStatus.READY if deps_done else TaskStatus.BLOCKED,
                    version=1,
                    root_goal=mission.goal,
                    created_at=self._store.now,
                    outputs=node.outputs,
                    context=context,
                    ready_at=self._store.now if deps_done else None,
                )
                self._store.insert_task(task, ordinal=key_to_ordinal[key])
                self._ledger.open_account(
                    account_id=task_account(task.id),
                    scope="task",
                    parent_id=mission_account(mission_id),
                    mission_id=mission_id,
                    limits=task.budget,
                )
                created.append(task)
                self._emit(
                    "TaskCommitted",
                    mission_id,
                    key=task.id,
                    task_id=task.id,
                    payload={
                        "commit_id": change_id,
                        "key": key,
                        "dependencies": list(dependencies),
                        "proposal": node.to_json(),
                        "source": {
                            **dict(source),
                            "template": "change",
                            "graph_version": new_version,
                        },
                    },
                )
            # in-place rewrites on BLOCKED tasks (data only, §25.1 untouched); a dependency
            # naming a Task this same proposal supersedes follows the replacement (review P1-1)
            translate = {
                **key_to_id,
                **{old: key_to_id[rep] for old, rep in validated.superseded.items()},
            }
            implicitly_rewired: list[str] = []
            for task_id, deps in validated.retargets.items():
                task = self._require_task(task_id)
                resolved = tuple(translate.get(d, d) for d in deps)
                self._store.update_task(
                    next_task(
                        task,
                        dependency_ids=resolved,
                        context={**dict(task.context), "graph_version": new_version},
                    ),
                    expected_version=task.version,
                )
                self._emit(
                    "TaskDependenciesRewritten",
                    mission_id,
                    key=f"{task_id}:{new_version}",
                    task_id=task_id,
                    payload={
                        "from": list(task.dependency_ids),
                        "to": list(resolved),
                        "graph_version": new_version,
                    },
                )
            # dependents of a superseded task that were not explicitly retargeted follow the replacement
            for old_id, replacement_key in validated.superseded.items():
                new_id = key_to_id[replacement_key]
                for task in self._store.list_tasks(mission_id):
                    if (
                        task.status is TaskStatus.BLOCKED
                        and old_id in task.dependency_ids
                        and task.id not in validated.retargets
                    ):
                        resolved = tuple(new_id if d == old_id else d for d in task.dependency_ids)
                        implicitly_rewired.append(task.id)
                        self._store.update_task(
                            next_task(task, dependency_ids=resolved), expected_version=task.version
                        )
                        self._emit(
                            "TaskDependenciesRewritten",
                            mission_id,
                            key=f"{task.id}:{new_version}",
                            task_id=task.id,
                            payload={
                                "from": list(task.dependency_ids),
                                "to": list(resolved),
                                "graph_version": new_version,
                                "follows_supersede": old_id,
                            },
                        )
            for task_id, priority in validated.priorities.items():
                task = self._require_task(task_id)
                self._store.update_task(
                    next_task(task, priority=priority), expected_version=task.version
                )
            for task_id, reason in validated.pauses.items():
                task = self._require_task(task_id)
                self._store.update_task(
                    next_task(task, paused=True, pause_reason=reason or "paused by manager"),
                    expected_version=task.version,
                )
                self._emit(
                    "TaskPaused",
                    mission_id,
                    key=f"{task_id}:{new_version}",
                    task_id=task_id,
                    payload={"reason": reason},
                )
            for task_id in validated.resumes:
                task = self._require_task(task_id)
                self._store.update_task(
                    next_task(task, paused=False, pause_reason=None), expected_version=task.version
                )
                self._emit(
                    "TaskResumed",
                    mission_id,
                    key=f"{task_id}:{new_version}",
                    task_id=task_id,
                    payload={},
                )
            for task_id, role in validated.roles.items():
                task = self._require_task(task_id)
                self._store.update_task(
                    next_task(task, context={**dict(task.context), "role": role}),
                    expected_version=task.version,
                )
                self._emit(
                    "TaskRoleChanged",
                    mission_id,
                    key=f"{task_id}:{new_version}",
                    task_id=task_id,
                    payload={"role": role},
                )
            # superseded / cancelled executing tasks: ACTIVE→CANCELLED (VERIFYING→ACTIVE first), attempts closed
            for old_id, replacement_key in validated.superseded.items():
                self._cancel_task_entity(
                    old_id, reason="superseded", replaced_by=key_to_id[replacement_key]
                )
            for old_id, reason in validated.cancels.items():
                self._cancel_task_entity(
                    old_id, reason=reason or "cancelled by manager", replaced_by=None
                )
            unblocked = [t.id for t in self._unblock(mission_id, unblocked_by=None)]
            record = {
                "change_id": change_id,
                "mission_id": mission_id,
                "from_version": current,
                "to_version": new_version,
                "proposal_hash": change.proposal_hash,
                "rebased_from": rebased_from,
                "basis": dict(change.basis),
                "rationale": change.rationale,
                "operations": [op.to_json() for op in change.operations],
                "new_task_ids": [t.id for t in created],
                "superseded": {old: key_to_id[rep] for old, rep in validated.superseded.items()},
                "cancelled": list(validated.cancels),
                "affected_task_ids": sorted(  # review P2-1: implicit rewires are affected too
                    {key_to_id.get(t, t) for t in validated.affected_task_ids}
                    | set(implicitly_rewired)
                ),
                "unblocked": unblocked,
                "warnings": list(validated.warnings),
                "depth": validated.depth,
                "source": dict(source),
            }
            self._store.insert_graph_change(record)
            self._store.update_mission(
                next_mission(mission, final_report={**report, "graph_version": new_version}),
                expected_version=mission.version,
            )
            self._store.insert_receipt(
                commit_id=change_id,
                kind="graph_change",
                subject_id=mission_id,
                base_version=change.base_graph_version,
                proposal_hash=change.proposal_hash,
                receipt=record,
            )
            self._emit(
                "TaskGraphChanged",
                mission_id,
                key=change_id,
                payload={k: v for k, v in record.items() if k not in {"operations"}}
                | {"operations": len(change.operations)},
            )
            return created, record

    def _cancel_task_entity(self, task_id: str, *, reason: str, replaced_by: str | None) -> Task:
        """READY/ACTIVE → CANCELLED (VERIFYING → ACTIVE first: two legal edges), open
        Attempts CANCELLED (late results stay history), unsubmitted intents closed."""

        task = self._require_task(task_id)
        if task.status is TaskStatus.VERIFYING:
            task = next_task(task, TaskStatus.ACTIVE)
            self._store.update_task(task, expected_version=task.version - 1)
            stored = self._store.find_result_for_attempt(
                next(
                    (
                        a.id
                        for a in self._store.list_attempts(task_id)
                        if a.status in SUBMITTED_STATES
                    ),
                    "",
                )
            )
            if stored is not None and stored.verification_state in {"PENDING", "RUNNING"}:
                self._store.set_result_verification(
                    stored.envelope.id, state="REJECTED", verdict="superseded"
                )
        cancelled = next_task(
            task,
            TaskStatus.CANCELLED,
            failure_reason=reason,
            context={**dict(task.context), **({"replaced_by": replaced_by} if replaced_by else {})},
        )
        self._store.update_task(cancelled, expected_version=task.version)
        for attempt in self._store.list_attempts(task_id):
            if attempt.status in OPEN_ATTEMPT_STATES:
                self._close_attempt(attempt, AttemptStatus.CANCELLED, reason=f"task_{reason}")
        self._emit(
            "TaskSuperseded" if replaced_by else "TaskCancelled",
            task.mission_id,
            key=task_id,
            task_id=task_id,
            payload={"reason": reason, "replaced_by": replaced_by},
        )
        return cancelled

    def unblock_dependents(self, task_id: str) -> list[Task]:
        """After a Task COMPLETED: every BLOCKED dependent whose dependencies are all
        COMPLETED becomes READY (§25.1 "dependencies satisfied")."""

        with self._store.transaction():
            completed = self._require_task(task_id)
            return self._unblock(completed.mission_id, unblocked_by=task_id)

    def _unblock(self, mission_id: str, *, unblocked_by: str | None) -> list[Task]:
        unblocked: list[Task] = []
        tasks = {task.id: task for task in self._store.list_tasks(mission_id)}
        for task in tasks.values():
            if task.status is not TaskStatus.BLOCKED:
                continue
            if unblocked_by is not None and unblocked_by not in task.dependency_ids:
                continue
            if all(tasks[dep].status is TaskStatus.COMPLETED for dep in task.dependency_ids):
                ready = next_task(task, TaskStatus.READY, ready_at=self._store.now)
                self._store.update_task(ready, expected_version=task.version)
                unblocked.append(ready)
                self._emit(
                    "TaskUnblocked",
                    task.mission_id,
                    key=task.id,
                    task_id=task.id,
                    payload={
                        "dependencies": list(task.dependency_ids),
                        "unblocked_by": unblocked_by or "recover",
                    },
                )
        return unblocked

    def heal_mission(self, mission_id: str) -> dict[str, Any]:
        """§16.4 idempotent self-healing on restart (D3-6'): recompute the frontier,
        close every non-terminal Attempt left under a terminal Task (SUPERSEDED for a
        COMPLETED Task, CANCELLED otherwise) and reject their pending results.  A
        healthy library is left untouched; the report says what changed."""

        with self._store.transaction():
            mission = self._require_mission(mission_id)
            report: dict[str, Any] = {"unblocked": [], "closed_attempts": []}
            if mission.status is not MissionStatus.ACTIVE:
                return report
            report["unblocked"] = [task.id for task in self._unblock(mission_id, unblocked_by=None)]
            for task in self._store.list_tasks(mission_id):
                if task.status not in TERMINAL_TASK:
                    continue
                target = (
                    AttemptStatus.SUPERSEDED
                    if task.status is TaskStatus.COMPLETED
                    else AttemptStatus.CANCELLED
                )
                for attempt in self._store.list_attempts(task.id):
                    if attempt.status in TERMINAL_ATTEMPT:
                        continue
                    self._close_attempt(attempt, target, reason="task_terminal_on_recover")
                    report["closed_attempts"].append(attempt.id)
            return report

    def _close_attempt(self, attempt: Attempt, target: AttemptStatus, *, reason: str) -> Attempt:
        """Attempt → SUPERSEDED / CANCELLED with its result (if any) rejected as history,
        its intent closed and its reservation settled unless an UNKNOWN charge holds it."""

        updated = next_attempt(attempt, target, failure={"reason": reason})
        self._store.update_attempt(updated, expected_version=attempt.version)
        stored = self._store.find_result_for_attempt(attempt.id)
        if stored is not None and stored.verification_state in {"PENDING", "RUNNING", "SUSPENDED"}:
            self._store.set_result_verification(
                stored.envelope.id, state="REJECTED", verdict="superseded"
            )
            if stored.verification_state == "SUSPENDED":  # D7-8': the review goes with it
                self._cancel_review_request(stored.envelope.id, reason=reason)
            for claim in self._store.list_claims(stored.envelope.id):
                if claim.status in {ClaimStatus.PROPOSED, ClaimStatus.UNDER_REVIEW}:
                    target_claim = (
                        ClaimStatus.UNDER_REVIEW
                        if claim.status is ClaimStatus.PROPOSED
                        else claim.status
                    )
                    moved = (
                        claim if target_claim is claim.status else next_claim(claim, target_claim)
                    )
                    self._store.upsert_claim(next_claim(moved, ClaimStatus.REJECTED))
        intent = self._store.get_intent_for_subject(attempt.id)
        turn_in_flight = intent is not None and intent.state == "SUBMITTED"
        if intent is not None and intent.state in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
            self._settle_intent(intent, "FAILED")
        # A SUBMITTED intent stays open: its SDK turn is still running and the loop
        # collects it later (usage imported, late result kept as history, D3-6'); the
        # reservation is settled at that point, never before the turn's cost is known.
        reservation = self._ledger.reservation(attempt.id)
        if (
            not turn_in_flight
            and reservation is not None
            and reservation["state"] != "SETTLED"
            and not self._ledger.has_unknown_usage(attempt.id)
        ):
            self._settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)
        self._emit(
            "AttemptSuperseded" if target is AttemptStatus.SUPERSEDED else "AttemptCancelled",
            attempt.mission_id,
            key=attempt.id,
            task_id=attempt.task_id,
            attempt_id=attempt.id,
            payload={"reason": reason, "from": str(attempt.status)},
        )
        return updated

    # ---------------------------------------------------------- knowledge (step 4)
    def _grade_and_project(
        self,
        mission: Mission,
        task: Task,
        attempt: Attempt,
        stored: StoredResult,
        *,
        verifier_results: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """D4-2/D4-3/D4-4/D4-5 inside the accept transaction: grade every claim of the
        accepted result from the verification that ran, project the VERIFIED ones into
        the Verified Knowledge store with their provenance, record the reuse chain of
        ``used_knowledge`` and apply an explicit, legal supersession."""

        envelope = stored.envelope
        untrusted = [str(p) for p in (mission.final_report or {}).get("untrusted_sources", [])]
        artifact_paths = [
            artifact.path
            for artifact_id in stored.artifacts
            for artifact in [self._store.get_artifact(artifact_id)]
            if artifact is not None
        ]
        layers = [dict(item) for item in verifier_results]
        report: list[dict[str, Any]] = []
        existing_claims = [
            other
            for other in self._store.list_mission_claims(mission.id)
            if other.result_id != envelope.id and self._accepted_result(other.result_id)
        ]
        for claim in self._store.list_claims(envelope.id):
            grade = grade_claim(
                claim.id,
                claim.evidence,
                verifier_results=layers,
                artifact_paths=artifact_paths,
                untrusted_prefixes=untrusted,
            )
            metadata = {
                **dict(claim.confidence_metadata),
                "grade": str(grade.status).lower()
                if grade.status is not ClaimStatus.UNDER_REVIEW
                else "unsupported",
                "basis": dict(grade.basis),
                "evidence_trust": list(grade.evidence_trust),
            }
            supersedes: str | None = None
            proposed = claim.confidence_metadata.get("proposed_supersedes")
            if proposed is not None:
                target = self._store.get_knowledge(str(proposed))
                if grade.status is not ClaimStatus.VERIFIED:
                    metadata["supersedes_rejected"] = (
                        f"only a VERIFIED claim may supersede knowledge (graded {grade.status})"
                    )
                elif (
                    target is None or target.mission_id != mission.id or target.status != "VERIFIED"
                ):
                    metadata["supersedes_rejected"] = (
                        f"{proposed!r} is not VERIFIED knowledge of this Mission"
                    )
                else:
                    supersedes = target.id
            results = (
                *claim.verifier_results,
                *layers,
                dict(grade.basis, layer=grade.basis.get("layer", "grading")),
            )
            target_status = grade.status
            contradiction = None
            if task.kind != "conflict":
                # D4-6': conflict precedes grading — a contested claim is capped at
                # DISPUTED and never projected, whatever its own evidence says
                contradiction = find_contradiction(claim, existing_claims)
                if contradiction is not None:
                    target_status = ClaimStatus.DISPUTED
                    metadata["grade_before_dispute"] = metadata["grade"]
                    metadata["grade"] = "disputed"
                    supersedes = None
            updated = next_claim(
                claim,
                target_status if target_status is not claim.status else None,
                verifier_results=tuple(results),
                confidence_metadata=metadata,
                supersedes=supersedes,
            )
            self._store.upsert_claim(updated)
            report.append({"claim_id": claim.id, "status": str(updated.status), "key": claim.key})
            if contradiction is not None:
                self._dispute(mission, updated, contradiction, stored)
                continue
            if updated.status is ClaimStatus.VERIFIED:
                record = KnowledgeRecord(
                    id=updated.id,
                    mission_id=mission.id,
                    claim_id=updated.id,
                    content=updated.content,
                    type=updated.type,
                    status="VERIFIED",
                    version=1,
                    key=updated.key,
                    stance=updated.stance,
                    proposed_by=updated.proposed_by,
                    source_task=task.id,
                    source_attempt=attempt.id,
                    source_result=envelope.id,
                    evidence=updated.evidence,
                    verifier=dict(grade.basis),
                    dependencies=envelope.used_knowledge,
                    created_at=self._store.now,
                    supersedes=supersedes,
                    evidence_trust=grade.evidence_trust,
                )
                self._store.upsert_knowledge(record)
                self._emit(
                    "KnowledgeCommitted",
                    mission.id,
                    key=record.id,
                    task_id=task.id,
                    attempt_id=attempt.id,
                    payload={
                        "knowledge_id": record.id,
                        "key": record.key,
                        "stance": record.stance,
                        "verifier": dict(record.verifier),
                        "supersedes": supersedes,
                    },
                )
                if supersedes is not None:
                    self._supersede_knowledge(supersedes, by=record.id)
                if task.kind == "conflict" and record.key == task.context.get("key"):
                    self._resolve_conflict(mission, task, record)
        for reference in envelope.used_knowledge:
            used = self._store.get_knowledge(reference)
            if used is None or used.mission_id != mission.id or used.status != "VERIFIED":
                continue
            if task.id not in used.used_by:
                self._store.upsert_knowledge(replace(used, used_by=(*used.used_by, task.id)))
            self._emit(
                "KnowledgeUsed",
                mission.id,
                key=f"{envelope.id}:{used.id}",
                task_id=task.id,
                attempt_id=attempt.id,
                payload={
                    "knowledge_id": used.id,
                    "version": used.version,
                    "result_id": envelope.id,
                    "source_task": used.source_task,
                    "source_attempt": used.source_attempt,
                },
            )
        return report

    def _accepted_result(self, result_id: str) -> bool:
        stored = self._store.get_result(result_id)
        return (
            stored is not None and stored.verification_state == "DONE" and stored.verdict == "PASS"
        )

    def _dispute(
        self, mission: Mission, claim: Claim, contradiction: Contradiction, stored: StoredResult
    ) -> None:
        """§14.4 保留双方 → 标 DISPUTED → 创建 Conflict Task (D4-6' / D4-7' / D4-20)."""

        other = contradiction.other
        if other.status is ClaimStatus.VERIFIED:
            # VERIFIED has no edge to DISPUTED (§25.3): the knowledge stays formal and is
            # marked as contested on both the claim and its projection
            if claim.id not in other.disputed_by:
                self._store.upsert_claim(
                    next_claim(other, disputed_by=(*other.disputed_by, claim.id))
                )
            record = self._store.get_knowledge(other.id)
            if record is not None and claim.id not in record.disputed_by:
                self._store.upsert_knowledge(
                    replace(record, disputed_by=(*record.disputed_by, claim.id))
                )
        elif other.status is not ClaimStatus.DISPUTED:
            self._store.upsert_claim(
                next_claim(other, ClaimStatus.DISPUTED, disputed_by=(*other.disputed_by, claim.id))
            )
        self._emit(
            "ClaimDisputed",
            mission.id,
            key=f"{claim.id}:{other.id}",
            task_id=claim.source_task,
            attempt_id=claim.source_attempt,
            payload={
                "claim_id": claim.id,
                "contradicts": other.id,
                "key": contradiction.key,
                "reason": contradiction.reason,
                "other_status": str(other.status),
            },
        )
        existing = next(
            (
                c
                for c in self._store.list_conflicts(mission.id)
                if c["key"] == contradiction.key and c["state"] in {"OPEN", "DEFERRED"}
            ),
            None,
        )
        if existing is not None:
            if claim.id not in existing["claim_ids"]:
                existing = {**existing, "claim_ids": [*existing["claim_ids"], claim.id]}
                self._store.upsert_conflict(existing)
            self._store.upsert_claim(
                next_claim(self._require_claim(claim.id), conflict_id=str(existing["conflict_id"]))
            )
            return
        self._open_conflict(mission, contradiction, opened_by=stored.envelope.id)

    def _open_conflict(
        self, mission: Mission, contradiction: Contradiction, *, opened_by: str
    ) -> None:
        mission = self._require_mission(mission.id)  # fresh: the reserve is on the record
        report = dict(mission.final_report or {})
        ordinal = len(self._store.list_conflicts(mission.id)) + 1
        conflict_id = f"{mission.id}:conflict-{ordinal}"
        sides = [
            {
                "claim_id": side.id,
                "stance": side.stance,
                "content": side.content,
                "evidence": list(side.evidence),
                "source_task": side.source_task,
                "status": str(self._require_claim(side.id).status),
            }
            for side in (contradiction.other, contradiction.claim)
        ]
        remaining = int(report.get("conflict_reserve_remaining") or 0)
        per_task = int(report.get("conflict_reserve_tokens") or 0)
        record: dict[str, Any] = {
            "conflict_id": conflict_id,
            "mission_id": mission.id,
            "key": contradiction.key,
            "state": "OPEN",
            "task_id": None,
            "claim_ids": [side["claim_id"] for side in sides],
            "sides": sides,
            "opened_by": opened_by,
            "created_at": self._store.now,
            "version": 1,
            "resolution_knowledge_id": None,
        }
        deferred_reason = None
        if not self._conflict_tasks:
            deferred_reason = "knowledge_sharing_disabled"
        elif self.domain_for(mission.id).conflict_template.decides_with not in (
            self._deployed_layers
        ):
            # host support 0.9.8: a Conflict Task settles on whatever its domain says
            # decides a dispute — a probe test in the code domain, a person in the
            # document domain.  If *that* layer is not deployed here, an unexecuted check
            # would stand as evidence, so the dispute stays DISPUTED and the conflict
            # waits.  P3.3 (review round 2 B P0-3): this used to name ``code_test``
            # literally, which deferred every conflict in a deployment that runs no tests.
            deferred_reason = (
                "local_code_execution_disabled"
                if self.domain_for(mission.id).conflict_template.decides_with == "code_test"
                else "arbitration_layer_undeployed"
            )
        elif per_task <= 0:
            deferred_reason = "no_reserve"
        elif remaining <= 0:
            deferred_reason = "reserve_exhausted"
        if deferred_reason is not None:
            record["state"] = "DEFERRED"
            record["deferred_reason"] = deferred_reason
            self._store.upsert_conflict(record)
            for side in sides:
                self._store.upsert_claim(
                    next_claim(self._require_claim(str(side["claim_id"])), conflict_id=conflict_id)
                )
            self._emit(
                "ConflictOpenDeferred",
                mission.id,
                key=conflict_id,
                payload={
                    "conflict_id": conflict_id,
                    "key": contradiction.key,
                    "reason": deferred_reason,
                },
            )
            return
        tokens = min(remaining, per_task)
        tasks = self._store.list_tasks(mission.id)
        domain = self.domain_for(mission.id)
        task = conflict_task(
            mission,
            task_id=ids.task_id(mission.id, len(tasks) + 1),
            key=contradiction.key,
            sides=sides,
            conflict_id=conflict_id,
            tokens=tokens,
            now=self._store.now,
            template=domain.conflict_template,
        )
        # P3.3 (D1) gate 4 of 5: the system's own template goes through the same check as
        # anything a model proposes — a template the domain would refuse is a bug here,
        # not something to discover four slices later as a Task that never completes
        self._check_system_template(domain, task)
        try:  # P0-1: an accept transaction never rolls back on a budget problem (D4-20)
            self._ledger.open_account(
                account_id=task_account(task.id),
                scope="task",
                parent_id=mission_account(mission.id),
                mission_id=mission.id,
                limits=task.budget,
            )
        except BudgetError as error:
            record["state"] = "DEFERRED"
            record["deferred_reason"] = f"budget_unavailable: {error}"
            self._store.upsert_conflict(record)
            for side in sides:
                self._store.upsert_claim(
                    next_claim(self._require_claim(str(side["claim_id"])), conflict_id=conflict_id)
                )
            self._emit(
                "ConflictOpenDeferred",
                mission.id,
                key=conflict_id,
                payload={
                    "conflict_id": conflict_id,
                    "key": contradiction.key,
                    "reason": "budget_unavailable",
                },
            )
            return
        task = replace(  # review P2-4: a system Task carries the graph version it joined at
            task,
            context={
                **dict(task.context),
                "graph_version": int((mission.final_report or {}).get("graph_version") or 1),
            },
        )
        self._store.insert_task(task, ordinal=len(tasks) + 1)
        record["task_id"] = task.id
        self._store.upsert_conflict(record)
        for side in sides:
            self._store.upsert_claim(
                next_claim(self._require_claim(str(side["claim_id"])), conflict_id=conflict_id)
            )
        graph_version = int(report.get("graph_version") or 1) + 1
        self._store.update_mission(
            next_mission(
                mission,
                final_report={
                    **report,
                    "graph_version": graph_version,
                    "conflict_reserve_remaining": remaining - tokens,
                },
            ),
            expected_version=mission.version,
        )
        self._store.insert_graph_change(  # R8: every graph_version has a ledger row
            {
                "change_id": conflict_id,
                "mission_id": mission.id,
                "from_version": graph_version - 1,
                "to_version": graph_version,
                "proposal_hash": sha256_hex({"conflict": conflict_id}),
                "rebased_from": None,
                "basis": {
                    "trigger": "conflict",
                    "conflict_id": conflict_id,
                    "result_id": opened_by,
                },
                "rationale": f"§14.4 conflict on {contradiction.key}: arbitration task opened by the system",
                "operations": [
                    {
                        "op": "add_task",
                        "key": "conflict",
                        "task_id": task.id,
                        "dependencies": list(task.dependency_ids),
                    }
                ],
                "new_task_ids": [task.id],
                "superseded": {},
                "cancelled": [],
                "affected_task_ids": [task.id, *task.dependency_ids],
                "unblocked": [],
                "warnings": [],
                "depth": 0,
                "source": {"template": "conflict"},
            }
        )
        self._emit(
            "ConflictOpened",
            mission.id,
            key=conflict_id,
            task_id=task.id,
            payload={
                "conflict_id": conflict_id,
                "key": contradiction.key,
                "claim_ids": record["claim_ids"],
                "task_id": task.id,
                "reserve_tokens": tokens,
                "graph_version": graph_version,
            },
        )
        self._emit(
            "TaskCommitted",
            mission.id,
            key=task.id,
            task_id=task.id,
            payload={
                "commit_id": conflict_id,
                "key": "conflict",
                "dependencies": list(task.dependency_ids),
                "proposal": {"goal": task.goal, "success_criteria": list(task.success_criteria)},
                "source": {"template": "conflict", "conflict_id": conflict_id},
            },
        )
        # D4-7': committed BLOCKED; the enclosing accept's ``_unblock(unblocked_by=task)``
        # turns it READY in this same transaction once the current Task is COMPLETED (P2-9)

    def _resolve_conflict(self, mission: Mission, task: Task, resolution: KnowledgeRecord) -> None:
        """The arbitration claim became VERIFIED knowledge: the conflict is RESOLVED on
        the strength of its external check — never on a count (D4-7')."""

        conflict_id = str(task.context.get("conflict_id"))
        conflict = self._store.get_conflict(conflict_id)
        if conflict is None or conflict["state"] == "RESOLVED":
            return
        resolved_ids = [str(c) for c in conflict["claim_ids"]]
        superseded: list[str] = []
        confirmed: list[str] = []
        for claim_id in resolved_ids:
            claim = self._store.get_claim(claim_id)
            if claim is None:
                continue
            self._store.upsert_claim(next_claim(claim, resolved_by=resolution.id))
            record = self._store.get_knowledge(claim_id)
            if record is None or record.status != "VERIFIED":
                continue
            if record.stance != resolution.stance:
                self._supersede_knowledge(record.id, by=resolution.id)
                superseded.append(record.id)
            else:
                fresh = self._store.get_knowledge(record.id)
                assert fresh is not None
                self._store.upsert_knowledge(
                    replace(fresh, confirmed_by=(*fresh.confirmed_by, resolution.id))
                )
                confirmed.append(record.id)
        current = self._store.get_knowledge(resolution.id)
        assert current is not None
        self._store.upsert_knowledge(replace(current, resolves=tuple(resolved_ids)))
        self._store.upsert_conflict(
            {
                **conflict,
                "state": "RESOLVED",
                "resolution_knowledge_id": resolution.id,
                "version": int(conflict.get("version", 1)) + 1,
            }
        )
        self._emit(
            "ConflictResolved",
            mission.id,
            key=conflict_id,
            task_id=task.id,
            payload={
                "conflict_id": conflict_id,
                "key": conflict["key"],
                "resolution_knowledge_id": resolution.id,
                "basis": dict(resolution.verifier),
                "resolved_claims": resolved_ids,
                "superseded": superseded,
                "confirmed": confirmed,
            },
        )

    def record_synthesis_gated(self, task_id: str, *, conflict_ids: Sequence[str]) -> bool:
        """D4-8': the synthesis Task waits (no state change) while a conflict is OPEN;
        one event per (task, conflict) — returns True when a new event was written."""

        with self._store.transaction():
            task = self._require_task(task_id)
            new = False
            for conflict_id in conflict_ids:
                before = self._store.count_events(task.mission_id, "SynthesisGated")
                self._emit(
                    "SynthesisGated",
                    task.mission_id,
                    key=f"{task_id}:{conflict_id}",
                    task_id=task_id,
                    payload={"conflict_id": conflict_id},
                )
                new = new or self._store.count_events(task.mission_id, "SynthesisGated") > before
            return new

    def _require_claim(self, claim_id: str) -> Claim:
        claim = self._store.get_claim(claim_id)
        if claim is None:
            raise CommitRejected(f"unknown claim {claim_id}")
        return claim

    def record_retrieval_unavailable(self, task_id: str, *, reason: str, policy: str) -> int:
        """S4-07 / D4-11': one durable event per failed retrieval; returns the count so
        far for this Task (it survives restarts — it is derived from the events)."""

        with self._store.transaction():
            task = self._require_task(task_id)
            previous = sum(
                1
                for event in self._store.list_events(task.mission_id)
                if event.type == "RetrievalUnavailable" and event.task_id == task_id
            )
            count = previous + 1
            self._emit(
                "RetrievalUnavailable",
                task.mission_id,
                key=f"{task_id}:retrieval:{count}",
                task_id=task_id,
                payload={"reason": reason, "policy": policy, "count": count},
            )
            return count

    def _supersede_knowledge(self, knowledge_id: str, *, by: str) -> None:
        """VERIFIED → SUPERSEDED (§25.3, the one legal edge out of VERIFIED) on both the
        claim and its knowledge projection, pointing at the newer version."""

        record = self._store.get_knowledge(knowledge_id)
        if record is None or record.status != "VERIFIED":
            return
        self._store.upsert_knowledge(
            replace(record, status="SUPERSEDED", superseded_by=by)  # P2-13: version = identity
        )
        claim = self._store.get_claim(record.claim_id)
        if claim is not None and claim.status is ClaimStatus.VERIFIED:
            self._store.upsert_claim(next_claim(claim, ClaimStatus.SUPERSEDED, superseded_by=by))
        self._emit(
            "KnowledgeSuperseded",
            record.mission_id,
            key=f"{knowledge_id}:{by}",
            task_id=record.source_task,
            payload={"knowledge_id": knowledge_id, "superseded_by": by, "key": record.key},
        )

    def record_planning_rejected(
        self, mission_id: str, *, ordinal: int, reason: str, detail: Mapping[str, Any]
    ) -> Event:
        """A Planner turn that produced no usable graph (D3-2'): durable feedback for the
        next proposal, no state transition."""

        return self._emit(
            "PlanningRejected",
            mission_id,
            key=f"{mission_id}:planner:{ordinal}",
            payload={"ordinal": ordinal, "reason": reason, "detail": dict(detail)},
        )

    def fail_planning(
        self,
        mission_id: str,
        *,
        reason: str,
        detail: Mapping[str, Any],
        stop_reason: MissionStopReason = MissionStopReason.PLANNING_FAILED,
    ) -> Mission:
        detail = jsonable(detail)
        detail = jsonable(detail)
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
                    # step 6: a non-planning stop reason (the pool ran out) reads like fail_mission
                    **(
                        {}
                        if stop_reason is MissionStopReason.PLANNING_FAILED
                        else {"stop_reason": str(stop_reason), "detail": dict(detail)}
                    ),
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
            self._cascade_stop(mission_id, skip_task=None)
            self._emit("MissionCancelled", mission_id, key=mission_id, payload={})
            return updated

    def fail_mission(
        self, mission_id: str, *, stop_reason: MissionStopReason, detail: Mapping[str, Any]
    ) -> Mission:
        """Mission-level stop that blames no Task (D3-12': the Mission pool itself ran
        out, or an operator condition): Mission → FAILED, open work cancelled."""

        with self._store.transaction():
            mission = self._require_mission(mission_id)
            if mission.status is MissionStatus.FAILED:
                return mission
            report = {
                **dict(mission.final_report or {}),
                "stop_reason": str(stop_reason),
                "detail": dict(detail),
                "tasks": self._task_reports(mission_id),
            }
            failed = next_mission(
                mission, MissionStatus.FAILED, stop_reason=str(stop_reason), final_report=report
            )
            self._store.update_mission(failed, expected_version=mission.version)
            self._cascade_stop(mission_id, skip_task=None)
            self._emit(
                "MissionFailed",
                mission_id,
                key=mission_id,
                payload={"stop_reason": str(stop_reason), "final_report": report},
            )
            return failed

    def _cascade_stop(self, mission_id: str, *, skip_task: str | None) -> list[str]:
        """D3-13': every READY / ACTIVE / VERIFYING Task (except ``skip_task``) is
        cancelled with its open Attempts; BLOCKED Tasks are left as they are (§25.1
        has no BLOCKED→CANCELLED edge — they end with the Mission); every open
        dispatch intent of the Mission is closed and its reservation released."""

        cancelled: list[str] = []
        for task in self._store.list_tasks(mission_id):
            if task.id == skip_task:
                continue
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
                self._emit("TaskCancelled", mission_id, key=task.id, task_id=task.id, payload={})
                for attempt in self._store.list_attempts(task.id):
                    if attempt.status in OPEN_ATTEMPT_STATES:
                        self._close_attempt(
                            attempt, AttemptStatus.CANCELLED, reason="mission_stopped"
                        )
                        cancelled.append(attempt.id)
        # every not-yet-submitted dispatch intent of this Mission is closed and its
        # reservation released; SUBMITTED ones (a turn is running) stay open for the
        # loop to collect — their cost is imported when the turn settles (D3-6')
        for intent in self._store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED"):
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
                if not self._ledger.has_unknown_usage(intent.subject_id):
                    self._settle_subject(intent.subject_id, mission_id, task_id=task_id)
        # D7-4' / D7-5': open actions and requests end with the Mission; handed-off and
        # UNKNOWN actions are left to the reconciliation (reality may already have moved)
        self._cancel_open_actions(mission_id, reason="mission_stopped")
        return cancelled

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
        candidates_per_task: int = 1,
        inputs: Sequence[Mapping[str, Any]] = (),
        max_open_attempts: int | None = None,
        max_running_attempts: int | None = None,
        runtime_profile_id: str = "default",
        routing: Mapping[str, Any] | None = None,
    ) -> tuple[Attempt, DispatchIntent]:
        """Atomic Reserve + Attempt(PENDING) + dispatch intent (ORCH-BUILD §4.3 step 1).

        Refuses (nothing written) when the Task is not READY/ACTIVE/VERIFYING, when it
        already has ``candidates_per_task`` open Attempts (D3-5': the Task status is a
        function of its Attempt set, so a second candidate may join while the first is
        being verified) or when the budget does not fit; the caller turns
        ``BudgetExhausted`` into a stop.  Every candidate counts against ``max_attempts``.
        """

        with self._store.transaction():
            task = self._require_task(task_id)
            if task.status not in {TaskStatus.READY, TaskStatus.ACTIVE, TaskStatus.VERIFYING}:
                raise CommitRejected(f"task {task_id} is {task.status}; no new Attempt")
            existing = self._store.list_attempts(task_id)
            open_attempts = [a for a in existing if a.status in OPEN_ATTEMPT_STATES]
            if len(open_attempts) >= max(1, candidates_per_task):
                raise CommitRejected(
                    f"task {task_id} already has {len(open_attempts)} open Attempt(s) "
                    f"(candidates_per_task={candidates_per_task}): {open_attempts[0].id}"
                )
            if max_open_attempts is not None:  # D3-4: the Mission-wide bound, checked here
                open_in_mission = sum(
                    1
                    for other in self._store.list_tasks(task.mission_id)
                    for a in self._store.list_attempts(other.id)
                    if a.status in OPEN_ATTEMPT_STATES
                )
                if open_in_mission >= max_open_attempts:
                    raise CommitRejected(
                        f"mission {task.mission_id} already has {open_in_mission} open Attempts "
                        f"(max_concurrency={max_open_attempts})"
                    )
            if max_running_attempts is not None:  # D6-1' / review P1-12: the deployment-wide cap
                open_everywhere = self._store.count_attempts_by_status(
                    *(str(s) for s in OPEN_ATTEMPT_STATES)
                )
                if open_everywhere >= max_running_attempts:
                    raise CommitRejected(
                        f"{open_everywhere} Attempts are open across all Missions "
                        f"(max_running_attempts={max_running_attempts})"
                    )
            ordinal = len(existing) + 1
            attempt_id = ids.attempt_id(task_id, ordinal)
            self._ledger.reserve(  # BudgetExhausted propagates; nothing was written
                account_id=task_account(task_id),
                subject_id=attempt_id,
                mission_id=task.mission_id,
                tokens=reservation.tokens,
                cost_micros=reservation.cost_micros,
                counts_attempt=True,
                tool_calls=reservation.tool_calls,
            )
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
                runtime_profile_id=runtime_profile_id,
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
                config={
                    **dict(intent_config),
                    "attempt_id": attempt_id,  # authoritative (P1-7): never the caller's guess
                    "inputs": [dict(item) for item in inputs],
                },
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
                    "inputs": [dict(item) for item in inputs],
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
            if (
                routing
            ):  # step 6 (S6-03): the physical route is on the timeline, frozen before the call
                self._emit(
                    "ModelRouted",
                    task.mission_id,
                    key=attempt_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload={
                        "runtime_profile_id": runtime_profile_id,
                        "model": model,
                        **dict(routing),
                    },
                )
            allocation = intent_config.get("allocation")
            if allocation:  # step 5 (S5-09): the §29.3 decision is on the timeline as well
                self._emit(
                    "AllocationDecided",
                    task.mission_id,
                    key=attempt_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    payload=dict(allocation),
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

        detail = jsonable(detail)
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
        self,
        subject_id: str,
        mission_id: str,
        *,
        task_id: str | None = None,
        tool_calls: int | None = None,
    ) -> Mapping[str, Any]:
        with self._store.transaction():
            return self._settle_subject(
                subject_id, mission_id, task_id=task_id, tool_calls=tool_calls
            )

    def _settle_subject(
        self,
        subject_id: str,
        mission_id: str,
        *,
        task_id: str | None,
        tool_calls: int | None = None,
    ) -> Mapping[str, Any]:
        if tool_calls is None:
            tool_calls = 0 if self.tool_calls_for is None else int(self.tool_calls_for(subject_id))
        settled = self._ledger.settle(subject_id=subject_id, tool_calls=tool_calls)
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
                "settled_tool_calls": int(settled.get("settled_tool_calls") or 0),
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
            # D4-15 (L3-2): the version is assigned here, inside the transaction, along the
            # (mission, path) lineage — two candidates snapshotting concurrently never collide
            lineage = next_versions(self._store.list_mission_artifacts(attempt.mission_id))
            registered: list[Artifact] = []
            for artifact in artifacts:
                if self._store.get_artifact(artifact.id) is None:
                    version = lineage.get(artifact.path, 0) + 1
                    lineage[artifact.path] = version
                    artifact = replace(artifact, version=version)
                self._store.upsert_artifact(artifact)
                registered.append(artifact)
            stored = replace(stored, artifacts=tuple(artifact.id for artifact in registered))
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
                        evidence=proposal.evidence or envelope.evidence,
                        dependencies=envelope.used_knowledge,
                        verifier_results=(),
                        confidence_metadata={
                            "self_reported_confidence": proposal.confidence,
                            "proposed_supersedes": proposal.supersedes,
                        },
                        supersedes=None,
                        mission_id=attempt.mission_id,
                        result_id=envelope.id,
                        key=proposal.key,
                        stance=proposal.stance,
                        proposed_by=attempt.agent_id or "",
                        contradicts=proposal.contradicts,
                    )
                )
            self._store.update_attempt(
                next_attempt(attempt, AttemptStatus.SUBMITTED, result_id=envelope.id),
                expected_version=attempt.version,
            )
            task = self._require_task(attempt.task_id)
            if (
                task.status is TaskStatus.ACTIVE
            ):  # D3-5': another candidate may already be VERIFYING
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

    def record_outcome_result(
        self,
        attempt_id: str,
        *,
        envelope: ResultEnvelope,
        turn_id: str,
        usage_refs: Sequence[str],
    ) -> StoredResult:
        """A non-candidate Result Envelope (§13 blocked / failure / no_progress /
        proposed_subtasks; D5-5): kept as history (never verified), the Attempt ends in
        RETRY_WAIT with the outcome as its failure, the Task stays ACTIVE for the
        Manager's decision (D5-6).  Idempotent on (attempt, turn)."""

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
            if envelope.outcome is ResultOutcome.CANDIDATE:
                raise CommitRejected("a candidate result goes through record_result / verification")
            stored = StoredResult(
                envelope=envelope,
                turn_id=turn_id,
                verification_state="REJECTED",
                verdict=f"outcome:{envelope.outcome}",
                received_at=self._store.now,
                artifacts=(),
                usage_refs=tuple(usage_refs),
            )
            self._store.insert_result(stored)
            for index, proposal in enumerate(envelope.claims, start=1):
                claim = Claim(
                    id=ids.claim_id(envelope.id, index),
                    content=proposal.content,
                    type=proposal.type,
                    status=ClaimStatus.PROPOSED,
                    source_task=attempt.task_id,
                    source_attempt=attempt_id,
                    evidence=proposal.evidence or envelope.evidence,
                    dependencies=envelope.used_knowledge,
                    verifier_results=(),
                    confidence_metadata={
                        "self_reported_confidence": proposal.confidence,
                        "grade": "not_verified_non_candidate",
                    },
                    supersedes=None,
                    mission_id=attempt.mission_id,
                    result_id=envelope.id,
                    key=proposal.key,
                    stance=proposal.stance,
                    proposed_by=attempt.agent_id or "",
                )
                self._store.upsert_claim(claim)
                self._store.upsert_claim(
                    next_claim(next_claim(claim, ClaimStatus.UNDER_REVIEW), ClaimStatus.REJECTED)
                )
            failure = {
                "reason": f"outcome_{envelope.outcome}",
                "summary": envelope.summary,
                "proposed_tasks": [dict(item) for item in envelope.proposed_tasks],
                "risks": list(envelope.risks),
                "result_id": envelope.id,
            }
            self._store.update_attempt(
                next_attempt(
                    attempt, AttemptStatus.RETRY_WAIT, result_id=envelope.id, failure=failure
                ),
                expected_version=attempt.version,
            )
            self._settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)
            self._emit(
                "ResultSubmitted",
                attempt.mission_id,
                key=envelope.id,
                task_id=attempt.task_id,
                attempt_id=attempt_id,
                payload={
                    "result_id": envelope.id,
                    "outcome": str(envelope.outcome),
                    "artifacts": [],
                    "claims": len(envelope.claims),
                    "proposed_tasks": [dict(item) for item in envelope.proposed_tasks],
                },
                actor_type="agent",
                actor_id=attempt.agent_id or attempt_id,
            )
            self._emit(
                "OutcomeRecorded",
                attempt.mission_id,
                key=envelope.id,
                task_id=attempt.task_id,
                attempt_id=attempt_id,
                payload={"outcome": str(envelope.outcome), "summary": envelope.summary[:400]},
            )
            return stored

    def no_progress_count(self, task_id: str) -> int:
        """Attempts of the Task that ended without progress (D5-7): no_progress / failure
        outcomes and failed verifications."""

        count = 0
        for attempt in self._store.list_attempts(task_id):
            reason = str((attempt.failure or {}).get("reason", ""))
            if reason in {"outcome_no_progress", "outcome_failure", "verification_failed"}:
                count += 1
        return count

    def record_management_requested(
        self, mission_id: str, *, task_id: str, trigger: str, subject: str, round_number: int
    ) -> Event:
        return self._emit(
            "ManagementRequested",
            mission_id,
            key=subject,
            task_id=task_id,
            payload={"trigger": trigger, "subject": subject, "round": round_number},
        )

    def record_management_decided(
        self,
        mission_id: str,
        *,
        task_id: str,
        trigger: str,
        decision: str,
        detail: Mapping[str, Any],
    ) -> Event:
        return self._emit(
            "ManagementDecided",
            mission_id,
            key=f"{mission_id}:manager:{trigger}:{decision}",
            task_id=task_id,
            payload={"trigger": trigger, "decision": decision, "detail": jsonable(detail)},
        )

    def record_late_result(
        self, attempt_id: str, *, turn_id: str, summary: str, artifacts: Sequence[str]
    ) -> Attempt:
        """A result that arrived after its Attempt was superseded / cancelled (D3-6'):
        history only — ``ResultRejected(reason=superseded)``, no state transition."""

        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            if attempt.status not in {AttemptStatus.SUPERSEDED, AttemptStatus.CANCELLED}:
                raise CommitRejected(f"attempt {attempt_id} is {attempt.status}; not a late result")
            self._emit(
                "ResultRejected",
                attempt.mission_id,
                key=f"{attempt_id}:{turn_id}",
                task_id=attempt.task_id,
                attempt_id=attempt_id,
                payload={
                    "reason": "superseded",
                    "detail": {"summary": summary, "artifacts": list(artifacts)},
                    "turn_id": turn_id,
                },
            )
            return attempt

    def reject_result(
        self, attempt_id: str, *, turn_id: str, reason: str, detail: Mapping[str, Any]
    ) -> Attempt:
        """An invalid / forged submission: ResultRejected, Attempt → RETRY_WAIT, Task stays ACTIVE (S2-07)."""

        detail = jsonable(detail)
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
                payload={
                    "layer": layer,
                    "status": status,
                    "summary": detail.get("summary"),
                    "verifier_version": detail.get("verifier_version"),
                },
            )

    def accept_result(
        self,
        result_id: str,
        *,
        verifier_results: Sequence[Mapping[str, Any]],
        owner: str | None = None,
        connectors: Mapping[str, Any] | None = None,
        deployment: DeploymentPolicy | None = None,
    ) -> Task:
        """PASS (§24 step 11) in one transaction (D3-6'): claims → VERIFIED, Attempt →
        COMPLETED, Task → COMPLETED (via VERIFYING when a sibling candidate had not
        moved it yet), the losing candidates → SUPERSEDED with their results kept as
        history, and every dependent whose dependencies are now all COMPLETED → READY."""

        with self._store.transaction():
            stored = self._require_result(result_id)
            if stored.verification_state == "DONE" and stored.verdict == "PASS":
                return self._require_task(stored.envelope.task_id)
            attempt = self._require_attempt(stored.envelope.attempt_id)
            self._require_lease(attempt, owner)
            task = self._require_task(stored.envelope.task_id)
            mission = self._require_mission(stored.envelope.mission_id)
            stale = KnowledgeIndex.load(self._store, mission.id).check(
                stored.envelope.used_knowledge
            )
            if stale:  # D4-4': the reference check is repeated inside the Commit (TOCTOU)
                return self.fail_result(
                    result_id,
                    failures=[
                        {
                            "layer": "rule_check",
                            "status": "FAIL",
                            "summary": "used_knowledge_stale: " + "; ".join(stale),
                            "detail": {"problems": stale, "reason": "used_knowledge_stale"},
                        }
                    ],
                    owner=owner,
                )
            open_conflicts = [
                c["conflict_id"] for c in self._store.list_conflicts(mission.id, state="OPEN")
            ]
            if task.kind == "synthesis" and open_conflicts:  # D4-8': guard inside the Commit
                return self.fail_result(
                    result_id,
                    failures=[
                        {
                            "layer": "rule_check",
                            "status": "FAIL",
                            "summary": "synthesis_blocked_by_open_conflict: "
                            + ", ".join(open_conflicts),
                            "detail": {
                                "reason": "synthesis_blocked_by_open_conflict",
                                "conflicts": open_conflicts,
                            },
                        }
                    ],
                    owner=owner,
                )
            candidates, rejection = self._action_candidates(
                stored, task, mission, connectors=connectors, deployment=deployment
            )
            if rejection is not None:  # D7-2'': re-checked on the accepted bytes, in the Commit
                return self.fail_result(result_id, failures=[rejection], owner=owner)
            if task.status is TaskStatus.ACTIVE:
                verifying = next_task(task, TaskStatus.VERIFYING)
                self._store.update_task(verifying, expected_version=task.version)
                task = verifying
            self._store.set_result_verification(result_id, state="DONE", verdict="PASS")
            grading = self._grade_and_project(
                mission, task, attempt, stored, verifier_results=verifier_results
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
            for artifact_id in stored.artifacts:  # P3.1 fix F-ORCH-3: in this transaction
                self._store.update_artifact_verification(artifact_id, "VERIFIED")
            # step 9 (plan D9-10'): an Agent's file that tries to set policy is refused on
            # record in the same transaction; it never reaches the registry
            self.refuse_policy_files(
                stored, mission_id=mission.id, task_id=task.id, result_id=result_id
            )
            for artifact, candidate in candidates:  # D7-2: registered in the accept transaction
                self.propose_action(
                    candidate,
                    mission_id=mission.id,
                    task_id=task.id,
                    result_id=result_id,
                    attempt_id=attempt.id,
                    artifact_id=artifact.id,
                    artifact_hash=artifact.content_hash,
                    connectors=connectors or {},
                    deployment=deployment or DeploymentPolicy(),
                )
            if not self._ledger.has_unknown_usage(attempt.id):  # ORCH §12.2 (P2-12)
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
                    "claims": grading,
                },
            )
            self._store.fault("after_accept_before_supersede", "attempt")
            superseded = []
            for other in self._store.list_attempts(task.id):
                if other.id != attempt.id and other.status in OPEN_ATTEMPT_STATES:
                    self._close_attempt(other, AttemptStatus.SUPERSEDED, reason="sibling_accepted")
                    superseded.append(other.id)
            unblocked = self._unblock(mission.id, unblocked_by=task.id)
            refresh_summaries(self._store, mission.id)  # D4-13: Summaries layer, same transaction
            self._emit(
                "TaskCompleted",
                mission.id,
                key=task.id,
                task_id=task.id,
                payload={
                    "result_id": result_id,
                    "artifacts": list(stored.artifacts),
                    "superseded": superseded,
                    "unblocked": [t.id for t in unblocked],
                },
            )
            return completed

    def judge_mission(
        self, mission_id: str, *, judgments: Sequence[Mapping[str, Any]], summary: str
    ) -> Mission:
        """Mission-level success judgment, independent of the Task PASS (D21, ORCH §12.4).

        Not the "Judge" of 理论 04-7 (which picks a champion candidate; not implemented in
        this build) — this is the check of the Mission's own success criteria.

        ``judgments`` carries one entry per ``Mission.success_criteria`` item with
        ``met: bool``; all met → COMPLETED, otherwise FAILED(mission_criteria_unmet)
        while the Task stays COMPLETED.
        """

        with self._store.transaction():
            mission = self._require_mission(mission_id)
            if mission.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}:
                return mission
            all_tasks = self._store.list_tasks(mission_id)
            tasks = [
                task
                for task in all_tasks
                if task.status is not TaskStatus.CANCELLED  # D5-4: superseded work is history
                and not (
                    task.paused and task.status in {TaskStatus.READY, TaskStatus.BLOCKED}
                )  # R4: a paused route is not required
            ]
            if not tasks or any(task.status is not TaskStatus.COMPLETED for task in tasks):
                raise CommitRejected("mission judgment requires every live Task to be COMPLETED")
            for task in all_tasks:  # a paused READY route ends with the Mission as not needed
                if task.paused and task.status is TaskStatus.READY:
                    self._store.update_task(
                        next_task(task, TaskStatus.CANCELLED, failure_reason="not_needed_paused"),
                        expected_version=task.version,
                    )
                    self._emit(
                        "TaskCancelled",
                        mission_id,
                        key=task.id,
                        task_id=task.id,
                        payload={"reason": "not_needed_paused"},
                    )
            criteria = list(mission.success_criteria)
            if [item.get("criterion") for item in judgments] != criteria:
                raise CommitRejected("judgments must cover the Mission success criteria in order")
            met = all(bool(item.get("met")) for item in judgments)
            terminal = terminal_task(tasks)  # D4-7': synthesis, else the last non-conflict leaf
            report = {
                **dict(mission.final_report or {}),
                "accepted_result_id": terminal.accepted_result_id,
                "accepted_artifacts": list(terminal.accepted_artifacts),
                "terminal_task_id": terminal.id,
                "summary": summary,
                "attempts": sum(task.attempt_count for task in tasks),
                "tasks": self._task_reports(mission_id),
                "success_criteria": [dict(item) for item in judgments],
                "conflicts": self._store.list_conflicts(mission_id),
                "unresolved_conflicts": [
                    c["conflict_id"]
                    for c in self._store.list_conflicts(mission_id)
                    if c["state"] not in {"RESOLVED", "RESOLVED_BY_HUMAN"}
                ],
                "knowledge": [
                    k.id for k in self._store.list_knowledge(mission_id, status="VERIFIED")
                ],
                "lineage": lineage(self._store, mission_id),  # D4-14 / 30-27
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
            self._cancel_open_actions(mission_id, reason="mission_criteria_unmet")
            self._emit(
                "MissionFailed",
                mission_id,
                key=mission_id,
                payload={"stop_reason": failed.stop_reason, "final_report": report},
            )
            return failed

    def fail_result(
        self,
        result_id: str,
        *,
        failures: Sequence[Mapping[str, Any]],
        owner: str | None = None,
    ) -> Task:
        """FAIL: claims → REJECTED, Attempt → RETRY_WAIT, Task VERIFYING → ACTIVE (retry decision is separate)."""

        with self._store.transaction():
            stored = self._require_result(result_id)
            if stored.verification_state == "DONE" and stored.verdict == "FAIL":
                return self._require_task(stored.envelope.task_id)
            attempt = self._require_attempt(stored.envelope.attempt_id)
            self._require_lease(attempt, owner)
            task = self._require_task(stored.envelope.task_id)
            self._store.set_result_verification(result_id, state="DONE", verdict="FAIL")
            for artifact_id in stored.artifacts:  # P3.1 fix F-ORCH-3: judged, and not accepted
                self._store.update_artifact_verification(artifact_id, "REJECTED")
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
            others_submitted = any(
                other.id != attempt.id and other.status in SUBMITTED_STATES
                for other in self._store.list_attempts(task.id)
            )
            active = task
            if task.status is TaskStatus.VERIFYING and not others_submitted:
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
            if task.status in {TaskStatus.VERIFYING, TaskStatus.READY}:
                # §25.1: FAILED is reached from ACTIVE only; a READY Task whose first
                # Attempt could not even be created (budget, artifact conflict) is
                # promoted first, a VERIFYING one is returned to ACTIVE (two legal edges)
                task = next_task(task, TaskStatus.ACTIVE)
                self._store.update_task(task, expected_version=task.version - 1)
            if task.status is not TaskStatus.ACTIVE:
                raise CommitRejected(f"task {task_id} is {task.status}; cannot stop it")
            failed = next_task(task, TaskStatus.FAILED, failure_reason=str(stop_reason))
            self._store.update_task(failed, expected_version=task.version)
            for attempt in self._store.list_attempts(task_id):
                if attempt.status in OPEN_ATTEMPT_STATES:
                    self._close_attempt(attempt, AttemptStatus.CANCELLED, reason="task_stopped")
            if task.kind == "conflict":
                conflict = self._store.get_conflict(str(task.context.get("conflict_id")))
                if conflict is not None and conflict["state"] == "OPEN":
                    self._store.upsert_conflict(
                        {
                            **conflict,
                            "state": "UNRESOLVED",
                            "version": int(conflict.get("version", 1)) + 1,
                        }
                    )
            report = {
                **dict(mission.final_report or {}),
                "stop_reason": str(stop_reason),
                "failed_task_id": task_id,
                "detail": dict(detail),
                "attempts": task.attempt_count,
                "completed_parts": self._completed_parts(task_id),
                "tasks": self._task_reports(mission.id),
            }
            done = next_mission(
                mission, MissionStatus.FAILED, stop_reason=str(stop_reason), final_report=report
            )
            self._store.update_mission(done, expected_version=mission.version)
            self._cascade_stop(mission.id, skip_task=task_id)
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

    def _task_reports(self, mission_id: str) -> list[dict[str, Any]]:
        return [
            {
                "task_id": task.id,
                "status": str(task.status),
                "dependencies": list(task.dependency_ids),
                "accepted_result_id": task.accepted_result_id,
                "accepted_artifacts": list(task.accepted_artifacts),
                "attempts": task.attempt_count,
                "failure_reason": task.failure_reason,
            }
            for task in self._store.list_tasks(mission_id)
        ]

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
    def _require_lease(self, attempt: Attempt, owner: str | None) -> None:
        """A verdict is committed only by the Attempt's current live lease holder (P1-3):
        a stale owner whose lease lapsed and was taken over is refused."""

        if owner is None:
            return
        if attempt.lease_owner != owner:
            raise CommitRejected(
                f"attempt {attempt.id} is leased to {attempt.lease_owner}, not {owner}"
            )
        if attempt.lease_expires_at is not None and attempt.lease_expires_at <= self._store.now:
            raise CommitRejected(f"lease of attempt {attempt.id} held by {owner} has lapsed")

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
