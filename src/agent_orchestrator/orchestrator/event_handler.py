# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""The Orchestrator control loop (§4, §7.5, §24) for a static Task DAG (step 3).

Observe → Plan → Allocate → Execute → Verify → Commit → Repeat, as a
deterministic *workflow shell* around the model calls (theory 08-8): every
action below is an idempotent Commit, so ``run()`` can be interrupted at any
instruction and restarted (``recover()`` first) without a second execution, a
second delivery or a second charge.  Fault points (``self._fault(...)``) mark the
cross-database crash instants of the recovery matrix (plan D14', D3-6').

Step 3 adds: the Planner proposes a whole graph, the Frontier / Allocator decide
which READY Tasks get an Attempt under the concurrency bound, a downstream
Attempt starts from its ancestors' accepted artifacts (frozen as inputs and
protected), accepting a result also supersedes the sibling candidates and
unblocks the dependents in the same transaction, a stop cascades to every open
Task, and the Mission is judged on the integrated tree of every Task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState

from ..artifacts.store import ArtifactStoreError, backfill, read_nofollow, read_verified
from ..artifacts.versioning import (
    ArtifactConflict,
    UpstreamInput,
    ancestors,
    merge_accepted,
    next_versions,
)
from ..artifacts.workspace import WorkspaceError
from ..context.context_builder import (
    CONTEXT_BUILDER_VERSION,
    ContextRejected,
    build_critic_package,
    build_manager_package,
    build_planner_package,
    build_worker_package,
)
from ..context.retrieval import (
    KnowledgeContext,
    RetrievalUnavailable,
    candidate_claims,
    disputed_claims,
    knowledge_view,
    rank_knowledge,
)
from ..contracts import (
    TERMINAL_ATTEMPT,
    TERMINAL_MISSION,
    TERMINAL_TASK,
    Artifact,
    Attempt,
    AttemptStatus,
    ClaimStatus,
    ContractError,
    Mission,
    MissionStatus,
    MissionStopReason,
    ResultEnvelope,
    ResultOutcome,
    Task,
    TaskStatus,
    ids,
)
from ..contracts.models import jsonable, sha256_hex
from ..contracts.state_machines import IllegalTransition
from ..governance.budgets import BudgetError, BudgetExhausted
from ..governance.policies import action_decision, deployed_layers, effective_tools
from ..governance.promotion import diff_params, interpreter_versions, resolve_params
from ..graph.changes import ChangeLimits, TaskGraphChange
from ..graph.task_graph import TaskBudgetFloor
from ..memory.summaries import build_summaries
from ..memory.verified_knowledge import KnowledgeIndex
from ..planning.manager import terminal_task
from ..planning.planner import parse_task_graph_proposal
from ..runtime.actions import ActionExecutor, publication_overlaps_storage
from ..runtime.agent_worker import AgentBridge, Liveness, user_message_json
from ..runtime.assembly import (
    AssembledOrchestratorRuntime,
    OrchestratorConfig,
    assemble_orchestrator_runtime,
)
from ..runtime.model_router import (
    DEFAULT_PROFILE,
    ModelRouter,
    RoutingDecision,
    RoutingRules,
    RoutingUnavailable,
    RuntimeProfile,
    classify_turn_error,
)
from ..runtime.output_blocks import BlockError, extract_block, outside_text
from ..runtime.role_templates import (
    CRITIC,
    GRAPH_CHANGE_PROPOSAL_TAG,
    MANAGER,
    PLANNER,
    RESULT_ENVELOPE_TAG,
    role_for_task,
    template_for_domain,
)
from ..runtime.sandbox import resolve_executor
from ..runtime.tool_gateway import CRITIC_TOOLS, WORKER_TOOLS, WorkspaceBinding, run_pytest
from ..scheduling.allocator import OPEN_ATTEMPT_STATES, allocate
from ..scheduling.backpressure import BackpressureState, Observation
from ..storage.store import DispatchIntent, InjectedCrash, Store, StoreBusy
from ..verification.critics import CriticVerdict, parse_critic_verdict
from ..verification.deterministic_checks import LayerResult
from ..verification.human_review import (
    NEEDS_HUMAN,
    arbitration_request_id,
    judgment_conflict,
    reusable_layers,
    review_request_id,
)
from ..verification.verifier_router import VERIFIER_VERSION, VerifierRouter
from .action_commits import (
    ACTION_PREFIX,
    HANDOFF_READY_STATES,
    ActionCommitError,
    CandidateRejected,
    check_candidate,
    is_action_path,
    judgment_key,
    parse_action_criterion,
)
from .commit_service import (
    GLOBAL_ACCOUNT,
    CommitRejected,
    CommitService,
    MissionSpec,
    Reservation,
    mission_account,
    task_account,
)

logger = logging.getLogger("agent_orchestrator")

FAULT_POINTS = (
    "after_agent_created",
    "after_submit",
    "after_turn_committed",
    "after_result_submitted",
    "after_layer_pass",
    "mid_commit",
    "after_accept_before_supersede",  # step 3 (inside the accept transaction → rolls back)
    "after_task_completed",  # step 3 (accept committed, release / next cycle not yet run)
    "retrieval_unavailable",  # step 4 (S4-07): the knowledge index cannot be read
    "before_graph_change",  # step 5: a Manager's proposal parsed, not yet committed
)
MAX_CRITIC_ATTEMPTS = 2


RECONCILE_EVERY_CYCLES = 50  # D7-5': UNKNOWN actions are asked about again while a run goes on
# step 9 (plan D9-3'): the whitelisted items a deployment configuration also names — a
# difference from the ACTIVE version is recorded as drift (the version still governs)
CONFIG_DERIVED = frozenset(
    {
        "candidates_per_task",
        "exploration_slots",
        "mission_concurrency",
        "manager_after_failures",
        "no_progress_limit",
        "max_manager_rounds",
        "aging_window_seconds",
        "routing",
    }
)


def _provider_kind(
    provider: Any, profiles: Mapping[str, RuntimeProfile], default_profile: str
) -> str:
    """Plan D9-3' (review P1-5 / P2-6): fixtures, a real model, or unknown — judged from
    the provider classes of every profile; "fixtures" only when all of them are."""

    def classify(candidate: Any, declared: str | None = None) -> str:
        module = type(candidate).__module__
        if module == "fixtures_provider" or module.startswith("agent_orchestrator.testing"):
            return "fixtures"
        if module.startswith("simple_harness.providers") or declared == "env":
            return "real"
        return "unknown"

    if provider is not None:
        return classify(provider)
    kinds = {classify(p.provider, str(p.provider_kind)) for p in profiles.values()}
    if kinds == {"fixtures"}:
        return "fixtures"
    return "real" if "real" in kinds else "unknown"


class Orchestrator:
    def __init__(
        self,
        config: OrchestratorConfig,
        provider=None,  # type: ignore[no-untyped-def]
        *,
        owner: str | None = None,
        poll_interval: float = 0.05,
        critic_wait_seconds: float = 120.0,
        profiles: Mapping[str, RuntimeProfile] | None = None,
        routing: RoutingRules | None = None,
        connectors: Mapping[str, Any] | None = None,
        provider_kind: str | None = None,
        policy_pin: Mapping[str, Any] | None = None,
    ) -> None:
        # D3-10': ``owner`` is this instance's identity for orchestration leases *and* for
        # the SDK runtime (``owner_id``); the SDK ``owner_scope`` is one constant for all.
        self._owner = owner or f"orchestrator-{os.getpid()}"
        self._config = replace(config, owner_id=self._owner)
        self._provider = provider
        # D6-4' / D6-5': one provider == the single ``default`` profile (every earlier
        # step's path); several profiles == several execution pools routed by rules
        if profiles is None:
            if provider is None:
                raise ValueError("Orchestrator needs a provider or runtime profiles")
            profiles = {
                DEFAULT_PROFILE: RuntimeProfile(
                    DEFAULT_PROFILE, provider, config.model, price_table=config.price_table
                )
            }
        self._profiles: dict[str, RuntimeProfile] = dict(profiles)
        default_profile = (
            routing.default
            if routing is not None
            else (
                DEFAULT_PROFILE if DEFAULT_PROFILE in self._profiles else next(iter(self._profiles))
            )
        )
        self._model_router = ModelRouter(
            self._profiles,
            routing if routing is not None else RoutingRules(default=default_profile),
        )
        self._default_profile = default_profile
        self._deferred: dict[str, float] = {}  # task_id → first time it waited for a profile
        # review P0-1: a Planner whose pool is cooling down waits too: mission_id → (since, ordinal)
        self._deferred_planning: dict[str, tuple[float, int]] = {}
        self._poll = poll_interval
        self._critic_wait = critic_wait_seconds
        self._store: Store | None = None
        self._commit: CommitService | None = None
        self._assembled: AssembledOrchestratorRuntime | None = None
        self._bridge: AgentBridge | None = None
        # P3.2 D2: the executor model-written code runs through (None when execution is
        # off); a sandboxed deployment without a probed seatbelt executor fails right here
        self._executor = resolve_executor(config.deployment_policy, config.sandbox_executor)
        self._router = VerifierRouter(
            test_timeout=config.test_timeout_seconds,
            local_code_execution=config.deployment_policy.local_code_execution,
            executor=self._executor,
        )
        # host support 0.9.8: the verification layers this deployment can run
        self._deployed = deployed_layers(config.deployment_policy)
        self._critic_verdicts: dict[str, CriticVerdict] = {}
        self._client_ids: dict[str, str | None] = {}
        self._released: set[str] = set()
        self.progress_log: list[str] = []
        self.cancel_receipts: list[dict[str, Any]] = []
        self._rotation = 0  # D6-1: round-robin start across active Missions
        self._verifying: dict[str, asyncio.Task[bool]] = {}  # D6-9': bounded verification set
        self._verification_error: BaseException | None = None  # a crash inside a verification task
        self._pressure = BackpressureState()  # D6-2: the current backpressure signal
        self._connectors: dict[str, Any] = dict(
            connectors or {}
        )  # D7-6: only the executor calls them
        self._actions: ActionExecutor | None = None
        self._routing = routing  # step 8 (D8-5'): part of the policy snapshot
        # step 9 (plan D9-3' / D9-4'): the provider kind recorded with each binding, the
        # evaluation pin (evaluation libraries only), per-version parameter and router caches
        self._provider_kind = provider_kind or _provider_kind(
            provider, self._profiles, default_profile
        )
        self._policy_pin = None if policy_pin is None else dict(policy_pin)
        self._policies: dict[str, dict[str, Any]] = {}
        self._routers: dict[str, ModelRouter] = {}
        self._route_drops: dict[str, dict[str, str]] = {}
        self._route_noted: set[str] = set()

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> Orchestrator:
        self._store = Store.open(self._config.orchestrator_db)
        self._task_floor = self._budget_floor_rule()  # P3.1 fix F-ORCH-1
        self._commit = CommitService(
            self._store,
            conflict_tasks=self._config.knowledge_sharing,
            global_budget=self._config.global_budget,
            deployed_layers=self._deployed,
            task_floor=self._task_floor,
            candidates_for=self._candidates_for,
        )
        self._open_policy_library()  # step 9 (plan D9-3'): role, seed, drift
        self._assembled = assemble_orchestrator_runtime(
            self._config, profiles=self._profiles, default_profile=self._default_profile
        )
        await self._assembled.__aenter__()
        # P3.2 D3: artifacts recorded before 0.10 move into the content-addressed store
        # (or are marked unavailable); execution copies a crash left behind are removed
        workspaces = self._assembled.workspaces
        changes = backfill(self._store.list_all_artifacts(), workspaces.artifact_store)
        if changes:
            self._store.update_artifact_storage(changes)
        workspaces.sweep_exec_copies()
        self.cleanup_workspaces()  # P3.2 D4: finished Missions past their retention
        self._bridge = self._assembled.pool(self._default_profile).bridge
        self._commit.tool_calls_for = self._executed_tool_calls  # D6-8
        self._assembled.gateway.on_rejected = self._audit_tool_rejection  # D6-7
        self._assembled.gateway.on_executed = self._record_tool_call  # review P1-3
        self._assembled.gateway.executed_counter = self.store.count_tool_calls
        self._pressure = self._commit.backpressure_state()
        self._actions = ActionExecutor(  # D7-5: the only caller of connectors
            self._commit,
            self._connectors,
            self._config.deployment_policy,
            owner=self._owner,
            source_storage_roots=(
                self.assembled.workspaces.artifact_store.root,
                self.assembled.workspaces.root,
            ),
        )
        return self

    # ------------------------------------------------------------ policies (step 9)
    def _config_policy(self) -> dict[str, Any]:
        """The deployment configuration read as a resolved policy (plan D9-1')."""

        return resolve_params(self._config, routing=self._model_router.rules)

    def _refuse_library(self, message: str) -> None:
        if self._store is not None:
            self._store.close()
        self._store = None
        self._commit = None
        raise ValueError(message)

    def _open_policy_library(self) -> None:
        """Plan D9-3': an evaluation library only takes pinned Missions; a production
        library gets its seed (the resolved built-in policy) on first use; a
        configuration whose whitelisted values differ from the ACTIVE version is
        recorded as drift, and the ACTIVE version still governs."""

        role = self.commit.library_role()
        if self._policy_pin is not None:
            if role == "production" or (role is None and self.store.list_missions()):
                self._refuse_library(
                    "a policy pin is only for an evaluation library; this is a production library"
                )
            self.commit.set_library_role("evaluation")
            return
        if role == "evaluation":
            self._refuse_library(
                "this is an evaluation library (pinned Missions only); a normal orchestrator does not run it"
            )
        if role is None:
            self.commit.set_library_role("production")
        configured = self._config_policy()
        config_hash = sha256_hex(configured)[:16]
        active = self.commit.seed_policy(
            configured,
            detail={
                "config_hash": config_hash,
                "sources": "OrchestratorConfig + code constants (allocator WEIGHTS, role templates)",
            },
        )
        if active.get("params"):
            differences = [
                d
                for d in diff_params(active["params"], configured)
                if str(d["key"]).split(".")[0] in CONFIG_DERIVED
            ]
            if differences:
                self.commit.record_policy_drift(config_hash=config_hash, differences=differences)

    def policy_version_of(self, mission_id: str) -> str | None:
        binding = self.store.get_mission_policy(mission_id)
        return None if binding is None else str(binding["version_id"])

    def _budget_floor_rule(self) -> TaskBudgetFloor:
        """P3.1 fix F-ORCH-1 (plan review P2-1): the floor's base is what one turn of any
        routable profile may emit — the largest ``default_max_output_tokens`` of the config
        and every profile — unless the deployment names one (0 = no floor)."""

        base = self._config.min_task_tokens
        if base is None:
            outputs = [int(self._config.default_max_output_tokens)]
            outputs += [
                int(profile.default_max_output_tokens)
                for profile in self._profiles.values()
                if profile.default_max_output_tokens
            ]
            base = max(outputs)
        return TaskBudgetFloor(base=int(base), critic=int(self._config.critic_reserve_tokens))

    def _candidates_for(self, mission_id: str) -> int:
        """Candidates per Task from the policy ``mission_id`` is bound to (plan review P1-2)."""

        # step 9 (D9-4'): a whitelisted value is read from the bound policy, never the config
        return max(1, int(self.policy_for(mission_id)["candidates_per_task"]))

    def _budget_floor(self, mission_id: str) -> dict[str, int]:
        """What the Planner / Manager is told a Task must at least hold."""

        candidates = self._candidates_for(mission_id)
        return {
            "min_task_tokens": self._task_floor.floor_for((), candidates),
            "min_task_tokens_with_critic_review": self._task_floor.floor_for(
                ("critic_review",), candidates
            ),
        }

    def policy_for(self, mission_id: str) -> dict[str, Any]:
        """The resolved parameters of the version ``mission_id`` is bound to (plan
        D9-4'); ``legacy`` Missions follow the deployment configuration, as they did."""

        version_id = self.policy_version_of(mission_id)
        if version_id is None:
            raise ContractError(f"mission {mission_id} is bound to no policy version")
        cached = self._policies.get(version_id)
        if cached is None:
            version = self.store.get_policy_version(version_id)
            if version is None:
                raise ContractError(
                    f"mission {mission_id} is bound to {version_id}, which this library does not have"
                )
            params = version.get("params")
            cached = dict(params) if params else self._config_policy()
            self._policies[version_id] = cached
        return cached

    def _template(self, template: Any, mission_id: str) -> Any:
        return template_for_domain(
            template,
            self.commit.domain_for(mission_id),
            self.policy_for(mission_id)["prompt_versions"],
        )

    def _router_for(self, mission_id: str) -> ModelRouter:
        """One router per policy version: the deployment's rules with the version's
        routing items on top; an item naming a profile this deployment lacks falls back
        to the deployment's rule, on record (plan D9-4')."""

        version_id = self.policy_version_of(mission_id) or ""
        router = self._routers.get(version_id)
        if router is not None:
            self._note_route_drops(mission_id, version_id)
            return router
        routing = dict(self.policy_for(mission_id).get("routing") or {})
        base = self._model_router.rules
        by_kind = dict(base.by_task_kind)
        dropped: dict[str, str] = {}
        for kind, target in dict(routing.get("by_task_kind") or {}).items():
            if target in self._profiles:
                by_kind[str(kind)] = str(target)
            else:
                dropped[str(kind)] = str(target)
        self._route_drops[version_id] = dropped
        self._note_route_drops(mission_id, version_id)
        rules = replace(
            base,
            by_task_kind=by_kind,
            escalate_after_failures=int(
                routing.get("escalate_after_failures", base.escalate_after_failures)
            ),
        )
        router = ModelRouter(self._profiles, rules)
        self._routers[version_id] = router
        return router

    def _note_route_drops(self, mission_id: str, version_id: str) -> None:
        dropped = self._route_drops.get(version_id)
        if dropped and mission_id not in self._route_noted:
            self._route_noted.add(mission_id)
            self.commit.record_policy_route_unavailable(
                mission_id, version_id=version_id, dropped=dropped
            )

    def _check_interpreter(self, mission: Mission) -> None:
        """Plan D9-4': a resumed Mission whose bound version was recorded under other
        interpreter versions says so on its timeline (once) — never silently."""

        version_id = self.policy_version_of(mission.id)
        version = None if version_id is None else self.store.get_policy_version(version_id)
        bound = dict((version or {}).get("interpreter_versions") or {})
        if not bound or version_id is None:
            return
        running = interpreter_versions()
        differences = [
            {"key": key, "bound": bound[key], "running": running.get(key)}
            for key in sorted(bound)
            if running.get(key) != bound[key]
        ]
        if differences:
            self.commit.record_interpreter_drift(
                mission.id, version_id=version_id, differences=differences
            )

    def _refuse_policy_ops(self, mission_id: str, task_id: str, operations: Any) -> None:
        """Plan D9-10' (review P2-1): a Manager proposal that smuggles a configuration or
        safety change — any operation outside the closed vocabulary, or policy keys
        carried inside a legal one — is refused on record; the closed vocabulary then
        rejects the proposal exactly as before (unknown keys of a legal operation are
        never read)."""

        from ..governance.promotion import NON_PROMOTABLE, PROMOTABLE
        from ..graph.changes import OPERATIONS

        policy_keys = NON_PROMOTABLE | PROMOTABLE
        items: set[str] = set()
        names: list[str] = []
        for op in operations if isinstance(operations, list) else []:
            if not isinstance(op, Mapping):
                continue
            name = str(op.get("op"))
            if name not in OPERATIONS:
                names.append(name)
                items.add(name)
                if op.get("key"):
                    items.add(str(op["key"]))
                items.update(
                    str(k) for k in op if k not in {"op", "key", "value", "reason", "task_id"}
                )
            else:
                carried = sorted(str(k) for k in op if str(k) in policy_keys)
                if carried:
                    names.append(name)
                    items.update(carried)
        if items:
            self.commit.record_policy_suggestion_refused(
                mission_id,
                source="manager",
                keys=sorted(items),
                detail={"task_id": task_id, "operations": names},
            )

    def policy_snapshot(self) -> dict[str, Any]:
        """Step 8 (plan D8-5'): where this orchestrator's behaviour comes from."""

        from ..governance.policies import policy_snapshot

        return policy_snapshot(
            self._config,
            profiles=self._profiles,
            routing=self._routing,
            connectors=self._connectors,
            provider=self._provider,
        )

    @property
    def actions(self) -> ActionExecutor:
        assert self._actions is not None, "use `async with Orchestrator(...)`"
        return self._actions

    def _audit_tool_rejection(self, run_id: str, record: Mapping[str, Any]) -> None:
        """Every gateway refusal becomes a ``ToolCallRejected`` event on its Mission."""

        attempt_id = str(record.get("attempt_id") or "")
        attempt = self.store.get_attempt(attempt_id) if attempt_id else None
        mission_id: str | None = None
        task_id: str | None = None
        if attempt is not None:
            mission_id, task_id = attempt.mission_id, attempt.task_id
        elif attempt_id:  # a Mission-level judgment view: "<mission>-judge-<owner>"
            candidate = attempt_id.split(":", 1)[0].split("-judge-", 1)[0]
            if self.store.get_mission(candidate) is not None:
                mission_id = candidate
        else:  # review P2-5: an unbound run (e.g. a released zombie turn) — find its intent
            for intent in self.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
            ):
                if intent.agent_id == run_id:
                    mission_id = intent.mission_id
                    attempt = self.store.get_attempt(intent.subject_id)
                    task_id = None if attempt is None else attempt.task_id
                    break
        if mission_id is None:
            return
        self.commit.record_tool_rejected(
            mission_id,
            task_id=task_id,
            attempt_id=None if attempt is None else attempt.id,
            run_id=run_id,
            call_key=f"{run_id}:{record.get('call_id')}",
            record=record,
        )

    def _record_tool_call(self, run_id: str, record: Mapping[str, Any]) -> None:
        """Review P1-3: an executed Worker tool call is a durable fact (once per SDK call id)."""

        if record.get("view") != "work":
            return  # Critic / judgment views are read-only and not charged to the dimension
        attempt = self.store.get_attempt(str(record.get("attempt_id") or ""))
        if attempt is None:
            return
        self.commit.record_tool_call(
            call_key=f"{run_id}:{record.get('call_id')}",
            subject_id=attempt.id,
            mission_id=attempt.mission_id,
            tool=str(record.get("tool")),
        )

    def _read_only_inputs(self, attempt_id: str) -> tuple[str, ...]:
        """D6-6: the upstream inputs this Attempt may read but not rewrite (the Task did
        not declare them as ``outputs``); seed files keep the step-2 tamper detection."""

        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            return ()
        task = self.store.get_task(attempt.task_id)
        mission = self.store.get_mission(attempt.mission_id)
        if task is None or mission is None:
            return ()
        try:
            protected = self._protected_files(mission, task, attempt)
        except ArtifactConflict:
            return ()
        seed = self._protected_seed(mission, task)
        return tuple(sorted(path for path in protected if path not in seed))

    def echoed_models_for(self, mission_id: str) -> dict[str, list[str]]:
        """attempt_id → the model names its provider echoed (read from the Attempt's own
        pool library) — the physical-route evidence ``trace.json`` carries (S6-03/S6-09)."""

        echoes: dict[str, list[str]] = {}
        for task in self.store.list_tasks(mission_id):
            for attempt in self.store.list_attempts(task.id):
                intent = self.store.get_intent_for_subject(attempt.id)
                if intent is None or intent.agent_id is None:
                    continue
                if self.profile_of(intent) not in self.assembled.pools:
                    continue
                models = self.bridge_for(intent).echoed_models(agent_id=intent.agent_id)
                if models:
                    echoes[attempt.id] = sorted(models)
        return echoes

    def _executed_tool_calls(self, subject_id: str) -> int:
        """The gateway's executed-call count for an Attempt (0 for service intents or an
        Attempt that never got an agent) — the fact the tool-call dimension settles on."""

        return self.store.count_tool_calls(subject_id)  # durable (review P1-3)

    async def __aexit__(self, *exc_info: object) -> None:
        for task in list(self._verifying.values()):
            if not task.done():
                task.cancel()
        for task in list(self._verifying.values()):
            with contextlib.suppress(BaseException):
                await task
        self._verifying.clear()
        if self._assembled is not None:
            await self._assembled.__aexit__(*exc_info)
        if self._store is not None:
            self._store.close()

    @property
    def store(self) -> Store:
        assert self._store is not None
        return self._store

    @property
    def commit(self) -> CommitService:
        assert self._commit is not None
        return self._commit

    @property
    def connectors(self) -> Mapping[str, Any]:
        """The connectors this deployment enabled (P3.2 D8: a compensation is proposed
        against the same connector the original action ran on)."""

        return dict(self._connectors)

    @property
    def bridge(self) -> AgentBridge:
        """The default pool's bridge (single-profile callers and tests)."""

        assert self._bridge is not None
        return self._bridge

    @property
    def model_router(self) -> ModelRouter:
        return self._model_router

    def _expected_model(self, intent: DispatchIntent) -> str:
        """The model frozen in the intent (review P0-3): the echo is checked against what
        was routed at dispatch time, never against the current configuration."""

        frozen = intent.config.get("model")
        if frozen:
            return str(frozen)
        profile = self._profiles.get(self.profile_of(intent))
        return profile.model if profile is not None else self._config.model

    def _route_service(self, role: str, mission_id: str) -> RoutingDecision:
        """Planner / Manager / Critic routing (D6-4'): by role, with the profile health
        applied; a cooling-down profile without fallback fails the caller fast."""

        return self._router_for(mission_id).route(
            role=role,
            task_kind=None,
            previous_attempts=(),
            unavailable_until=self.commit.unavailable_until(),
            now=self.store.now,
        )

    def _service_config(self, decision: RoutingDecision) -> dict[str, Any]:
        return {
            "runtime_profile_id": decision.profile_id,
            "model": decision.model,
            "routing": decision.to_json(),
        }

    def _note_turn_health(self, intent: DispatchIntent, result) -> str:  # type: ignore[no-untyped-def]
        """D6-5' / review P1-8: a committed turn closes the profile's failure streak; a
        provider-unavailable failure counts towards its cooldown.  Returns the error class."""

        profile_id = self.profile_of(intent)
        if result.state is AgentTurnState.COMMITTED:
            self.commit.record_profile_success(profile_id)
            return "ok"
        kind = classify_turn_error(result.error)
        if kind == "provider_unavailable":
            until = self.commit.record_profile_failure(
                profile_id,
                error=result.error,
                threshold=self._config.profile_failure_threshold,
                cooldown_seconds=self._config.profile_cooldown_seconds,
                mission_ids=[m.id for m in self._active_missions()],
            )
            if until is not None:
                self._note(f"runtime profile {profile_id!r} unavailable until {until:.3f}")
        return kind

    def profile_of(self, intent: DispatchIntent) -> str:
        return str(intent.config.get("runtime_profile_id") or self._default_profile)

    def bridge_for(self, intent: DispatchIntent) -> AgentBridge:
        """The pool an intent is bound to (D6-5'): an intent bound to a profile this
        process does not run raises — it is never handed to another model."""

        return self.assembled.pool(self.profile_of(intent)).bridge

    def _pool_missing(self, intent: DispatchIntent) -> bool:
        """S6-08: an intent whose profile is not configured here stays untouched (its
        lease lapses like any dead executor's); recorded once per intent."""

        profile_id = self.profile_of(intent)
        if profile_id in self.assembled.pools:
            return False
        key = f"{intent.intent_id}:not_configured"
        if key not in self._released:
            self._released.add(key)
            self.commit.record_profile_unavailable(
                profile_id,
                reason="not_configured",
                until=None,
                mission_ids=[intent.mission_id],
                detail={"intent_id": intent.intent_id, "subject_id": intent.subject_id},
            )
            self._note(
                f"{intent.subject_id}: bound to profile {profile_id!r} which is not configured here"
            )
        return True

    @property
    def assembled(self) -> AssembledOrchestratorRuntime:
        assert self._assembled is not None
        return self._assembled

    @property
    def config(self) -> OrchestratorConfig:
        return self._config

    @property
    def owner(self) -> str:
        return self._owner

    def arm_fault(
        self, point: str, *, kind: str | None = None, skip: int = 0, times: int = 1
    ) -> None:
        """Arm a crash at ``point``; ``kind`` restricts it to plan / attempt / critic intents;
        ``skip`` lets that many hits pass first (crash on the n+1-th); ``times`` repeats."""

        if point not in FAULT_POINTS:
            raise ValueError(f"unknown fault point {point}")
        self.store.arm(point if kind is None else f"{point}:{kind}", skip=skip, times=times)

    def _fault(self, point: str, kind: str | None = None) -> None:
        self.store.fault(point, kind)

    def _note(self, text: str) -> None:
        self.progress_log.append(text)
        logger.info("orchestrator.progress", extra={"detail": text})

    # ------------------------------------------------------------------ api
    async def submit_mission(self, spec: MissionSpec) -> Mission:
        self._check_mission_door(spec)
        mission, _ = self._commit_mission(spec)
        return mission

    def create_mission(self, *, tenant_id: str, request: Mapping[str, Any]) -> tuple[Mission, bool]:
        """Host support 0.9.8 (plan review P1-1): the one door that knows the deployment —
        parse the request, ``validate_spec`` against the deployment's tools, the action
        criteria, local code execution, then the Commit with the provider kind and the
        policy binding :meth:`submit_mission` uses.  Idempotent on ``(tenant_id,
        idempotency_key)``: the same request returns ``(mission, False)``; a *different*
        request under the same key raises ``MissionConflict`` (review round 1 P2-4).  Every
        other refusal is a ``MissionRequestError``; no refusal writes anything."""

        from ..api.missions import MissionRequestError, spec_from_request, validate_spec

        deployment = self._config.deployment_policy
        spec = spec_from_request(tenant_id, request, default_tools=deployment.allowed_tools)
        validate_spec(spec, available_tools=deployment.allowed_tools)
        try:
            self._check_mission_door(spec)
        except ContractError as error:
            raise MissionRequestError(str(error)) from error
        return self._commit_mission(spec)

    def _commit_mission(self, spec: MissionSpec) -> tuple[Mission, bool]:
        return self.commit.create_mission(
            spec,
            provider_kind=self._provider_kind,
            policy_defaults=self._config_policy(),
            policy_pin=self._policy_pin,
        )

    def _check_mission_door(self, spec: MissionSpec) -> None:
        self._check_action_criteria(spec.success_criteria)
        self._check_source_publish_roots(spec)
        if not self._config.deployment_policy.local_code_execution:
            tests = [c for c in spec.success_criteria if c.startswith("pytest:")]
            template = dict(spec.synthesis or {})  # review round 1 P2-5: refused up front
            tests += [
                c for c in template.get("success_criteria", ()) if str(c).startswith("pytest:")
            ]
            if tests or "code_test" in template.get("verification_policy", ()):
                raise ContractError(
                    "pytest criteria and code_test need local code execution, which this "
                    f"deployment has turned off: {tests or ['synthesis: code_test']}"
                )
        if spec.synthesis is not None:
            self._check_synthesis_template(spec)

    def _check_source_publish_roots(self, spec: MissionSpec) -> None:
        """A publisher cannot write the actual source CAS or its workspace mounts.

        Domain roots are logical paths, so compare physical deployment roots here.
        This is a write-boundary check, not a claim about the origin of user text.
        """
        from ..governance.domains import resolve_domain

        if not resolve_domain(spec.domain).source_roots:
            return
        self._ensure_source_storage_disjoint()

    def validate_source_storage(self, mission_id: str) -> None:
        """Recheck physical storage when an existing Mission imports source material."""
        if self.commit.domain_for(mission_id).source_roots:
            self._ensure_source_storage_disjoint()

    def _ensure_source_storage_disjoint(self) -> None:
        protected = (
            self.assembled.workspaces.artifact_store.root.resolve(),
            self.assembled.workspaces.root.resolve(),
        )
        for name, connector in self._connectors.items():
            if (
                name != "file_publish"
                or name not in self._config.deployment_policy.enabled_connectors
            ):
                continue
            root = getattr(connector, "root", None)
            if not isinstance(root, (str, Path)):
                raise ContractError("source_publish_root_unavailable")
            if publication_overlaps_storage(root, protected):
                raise ContractError("source_publish_root_overlap")

    def _check_synthesis_template(self, spec: MissionSpec) -> None:
        """Review round 2 P1-A: a synthesis template is a Task contract written by the
        caller; it meets a Planner Task's gates at the door, not first at graph commit."""

        from ..contracts import Budget

        template = dict(spec.synthesis or {})
        goal, criteria = template.get("goal"), template.get("success_criteria")
        if not isinstance(goal, str) or not goal.strip():
            raise ContractError("synthesis.goal must be a non-blank string")
        if (
            isinstance(criteria, str)
            or not isinstance(criteria, (list, tuple))
            or not criteria
            or not all(isinstance(c, str) and c.strip() for c in criteria)
        ):
            raise ContractError("synthesis.success_criteria must be a list of non-blank strings")
        policy = template.get("verification_policy")
        if policy is not None:
            undeployed = set(policy) - self._deployed
            if undeployed:
                raise ContractError(
                    f"synthesis.verification_policy names undeployed layers: {sorted(undeployed)}"
                )
        if not Budget.from_json(template.get("budget", {})).fits_within(spec.budget):
            raise ContractError("synthesis.budget exceeds the Mission budget (§18.2)")
        tools = template.get("allowed_tools")
        if tools is not None and set(tools) - set(spec.allowed_tools):
            raise ContractError("synthesis.allowed_tools must stay inside the Mission's tools")
        self._check_action_criteria(tuple(criteria))

    def _check_action_criteria(self, criteria: Sequence[str]) -> None:
        """D7-3' / review P2-10: an action criterion must name an enabled connector and an
        operation this deployment would run; otherwise the Mission is refused up front."""

        for criterion in criteria:
            parsed = parse_action_criterion(criterion)  # malformed → ContractError
            if parsed is None:
                continue
            name, operation, _target = parsed
            decision = action_decision(
                self._config.deployment_policy, self._connectors.get(name), operation
            )
            if decision.refused is not None:
                raise ContractError(f"action criterion {criterion!r} refused: {decision.refused}")

    async def recover(self) -> None:
        """§16.4 recovery (D3-6'): rebind the workspaces of in-flight turns, let the
        Commit Service heal each active Mission (frontier recompute, orphan candidates
        closed), re-import the usage of settled-but-unpaid Attempts, wake the SDK
        turns, then let the loop re-drive the remaining intents (review P1-3)."""

        for intent in self.store.list_intents("AGENT_CREATED", "SUBMITTED"):
            if intent.agent_id is None:
                continue
            if intent.kind == "attempt":
                attempt = self.store.get_attempt(intent.subject_id)
                if attempt is not None:
                    self._bind_workspace(attempt)
                    self._bind_agent(intent.agent_id, intent.config)
            elif intent.kind == "critic":
                self._bind_critic(intent.agent_id, intent.config)
        for mission in self._active_missions():
            try:
                report = self.commit.heal_mission(mission.id)
            except StoreBusy as error:  # another instance is healing; the loop retries
                self._note(f"recover {mission.id}: store busy ({error})")
                continue
            if report["unblocked"] or report["closed_attempts"]:
                self._note(f"recover {mission.id}: {report}")
            for attempt_id in report["closed_attempts"]:
                await self._release_attempt(attempt_id, cancel=True)
            self._reimport_unsettled(mission)
            self._check_interpreter(mission)  # step 9 (plan D9-4')
        for pool in self.assembled.pools.values():  # D6-5': each pool recovers only its own library
            await pool.bridge.recover()

    async def run(self, *, max_cycles: int = 10_000, until_idle: bool = True) -> None:
        """Drive the loop until idle.  ``max_cycles`` bounds *progressing* cycles (work
        done), never the waiting: a slow real model turn may keep the loop polling for
        many minutes and must not end the run early (step 4 real-run finding)."""

        await self.recover()
        await self.actions.reconcile()  # D7-5': every run() first asks about UNKNOWN actions
        cycles = 0
        idle_rounds = 0
        while cycles < max_cycles:
            progressed = await self._cycle()
            if progressed:
                cycles += 1
                idle_rounds = 0
                if cycles % RECONCILE_EVERY_CYCLES == 0:
                    await self.actions.reconcile()
                continue
            if not until_idle:
                return
            if self._has_inflight():
                idle_rounds = 0
                await asyncio.sleep(self._poll)
                continue
            idle_rounds += 1
            if idle_rounds >= 2:
                # review P2-6: one more look at hand-offs whose lease lapsed (a crashed owner)
                settled = await self.actions.reconcile()
                if any(a["state"] != "UNKNOWN" for a in settled):
                    idle_rounds = 0
                    continue
                return
            await asyncio.sleep(self._poll)

    # ---------------------------------------------------------------- cycle
    def _active_missions(self) -> list[Mission]:
        return [m for m in self.store.list_missions() if m.status not in TERMINAL_MISSION]

    def _note_verification_done(self, task: asyncio.Task[bool]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and self._verification_error is None:
            self._verification_error = error

    def _raise_if_verification_crashed(self) -> None:
        # look at the tasks themselves: a done-callback is only scheduled *after* the loop
        # resumed us, so a crash that happened during our last yield would otherwise be
        # seen one phase too late (step-3 fault-point tests)
        error = self._verification_error
        for result_id, task in list(self._verifying.items()):
            if task.done() and not task.cancelled() and task.exception() is not None:
                error = error or task.exception()
                del self._verifying[result_id]
        if error is not None:
            self._verification_error = None
            for result_id, task in list(self._verifying.items()):
                if task.done():
                    del self._verifying[result_id]
            raise error

    def _has_inflight(self) -> bool:
        """A submitted turn counts as in flight until it is collected — also for a
        terminal Mission (a superseded / cancelled Attempt's cost and late result are
        still collected); critic turns are collected inline by their runner."""

        if self._actions is not None and self._actions.inflight:
            return True  # D7-5': a hand-off this process is waiting on
        if any(not task.done() for task in self._verifying.values()):
            return True
        self._prune_deferred()  # review P0-2: only live waits keep the loop alive
        if self._deferred or self._deferred_planning:  # D6-5': bounded, not idle
            return True
        return any(
            intent.kind != "critic" and self.profile_of(intent) in self.assembled.pools
            for intent in self.store.list_intents("SUBMITTED")
        )  # review P1-4: a turn bound to a pool this process does not run is not ours to wait for

    async def _cycle(self) -> bool:
        try:
            return await self._cycle_inner()
        except StoreBusy as error:
            # D3-10': another instance holds the write lock; nothing was applied, retry next cycle
            self._note(f"store busy, cycle skipped: {error}")
            await asyncio.sleep(self._poll)
            return False
        except (CommitRejected, IllegalTransition) as error:
            # a Commit refused because the library moved under us (another instance, a
            # cascade): nothing was written; the next cycle re-observes (P1-4)
            self._note(f"commit refused, cycle skipped: {error}")
            await asyncio.sleep(self._poll)
            return False

    async def _cycle_inner(self) -> bool:
        progressed = False
        for mission in self._active_missions():
            if mission.status is MissionStatus.CREATED:
                await self._start_planning(mission)
                progressed = True
        if await self._retry_deferred_planning():
            progressed = True
        active = {mission.id for mission in self._active_missions()}
        for intent in self.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED"):
            if intent.mission_id not in active:
                continue
            if await self._dispatch(intent):
                progressed = True
        for intent in self.store.list_intents("SUBMITTED"):
            if intent.kind == "critic":
                continue  # critics are collected inline by the critic runner
            if intent.mission_id not in active:
                if await self._collect_after_stop(intent):
                    progressed = True
                continue
            if await self._collect(intent):
                progressed = True
        # D6-9': verification runs in a bounded set of tasks (``verifier_workers``); the loop
        # reaps finished ones and starts new ones.  A crash inside a verification is raised at
        # the next phase boundary — nothing else is decided after it (fault-injection tests)
        self._raise_if_verification_crashed()
        for result_id, task in list(self._verifying.items()):
            if task.done():
                del self._verifying[result_id]
                if task.result():
                    progressed = True
        for stored in self.store.list_results_by_verification("PENDING", "RUNNING"):
            if stored.envelope.mission_id not in active or stored.envelope.id in self._verifying:
                continue
            if len(self._verifying) >= self._config.verifier_workers:
                break
            task = asyncio.create_task(self._verify(stored.envelope.id))
            task.add_done_callback(self._note_verification_done)
            self._verifying[stored.envelope.id] = task
            await asyncio.sleep(0)  # let the verification reach its first Commit before deciding
        self._raise_if_verification_crashed()
        self._observe_pressure(active)
        missions = self._active_missions()
        if missions:  # D6-1 fair progress: the allocation order rotates across Missions
            start = self._rotation % len(missions)
            self._rotation += 1
            missions = missions[start:] + missions[:start]
        for mission in missions:
            self._raise_if_verification_crashed()
            if await self._decide(mission):
                progressed = True
        return progressed

    # ------------------------------------------------------------- planning
    def _prune_deferred(self) -> None:
        """Review P0-2: a wait whose Task or Mission has ended is dropped, whoever ended it."""

        for task_id in list(self._deferred):
            task = self.store.get_task(task_id)
            mission = None if task is None else self.store.get_mission(task.mission_id)
            if (
                task is None
                or task.status in TERMINAL_TASK
                or mission is None
                or mission.status is not MissionStatus.ACTIVE
            ):
                self._deferred.pop(task_id, None)
        for mission_id in list(self._deferred_planning):
            mission = self.store.get_mission(mission_id)
            if mission is None or mission.status is not MissionStatus.PLANNING:
                self._deferred_planning.pop(mission_id, None)

    async def _try_planner_intent(self, mission_id: str, *, ordinal: int) -> bool:
        """Create the Planner intent, or — review P0-1 — wait (bounded) while its pool is
        cooling down; a package that would carry a credential stops planning visibly."""

        try:
            await self._create_planner_intent(mission_id, ordinal=ordinal)
        except RoutingUnavailable as unavailable:
            since = self._deferred_planning.get(mission_id, (self.store.now, ordinal))[0]
            self._deferred_planning[mission_id] = (since, ordinal)
            if self.store.now - since >= self._config.profile_wait_seconds:
                self._deferred_planning.pop(mission_id, None)
                self.commit.fail_planning(
                    mission_id,
                    reason="runtime_unavailable",
                    detail={
                        "profile_id": unavailable.profile_id,
                        "waited_seconds": round(self.store.now - since, 3),
                        "profile_wait_seconds": self._config.profile_wait_seconds,
                        "ordinal": ordinal,
                    },
                    stop_reason=MissionStopReason.RUNTIME_UNAVAILABLE,
                )
                self._note(
                    f"mission {mission_id}: planner pool {unavailable.profile_id!r} unavailable → stopped"
                )
            return False
        except ContextRejected as error:
            self._deferred_planning.pop(mission_id, None)
            self.commit.fail_planning(
                mission_id,
                reason="context_rejected",
                detail={"error": str(error)[:300]},
                stop_reason=MissionStopReason.CONTEXT_REJECTED,
            )
            self._note(f"mission {mission_id}: planner package refused ({error})")
            return False
        self._deferred_planning.pop(mission_id, None)
        return True

    async def _retry_deferred_planning(self) -> bool:
        progressed = False
        self._prune_deferred()
        for mission_id, (_since, ordinal) in list(self._deferred_planning.items()):
            if await self._try_planner_intent(mission_id, ordinal=ordinal):
                progressed = True
        return progressed

    async def _start_planning(self, mission: Mission) -> None:
        self.commit.begin_planning(mission.id)
        try:
            await self._try_planner_intent(mission.id, ordinal=1)
        except BudgetExhausted as error:
            # D6-8 / D3-12': a pool that cannot even fund the Planner is exhausted on that
            # dimension; the Mission stops visibly instead of the loop crashing
            detail = {
                "dimension": error.dimension,
                "requested": error.requested,
                "remaining": error.remaining,
                "account": error.account_id,
                "phase": "planning",
                # review P1-1: the Global pool is named as such — no Mission is to blame
                "scope": "global" if error.account_id == GLOBAL_ACCOUNT else "mission",
            }
            self.commit.fail_planning(
                mission.id,
                reason="budget_exhausted",
                detail=detail,
                stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
            )
            self._note(
                f"mission {mission.id} stopped in planning: budget_exhausted ({error.dimension})"
            )

    def _planning_rejections(self, mission_id: str) -> list[dict[str, Any]]:
        """Durable feedback for the next proposal (D3-2'): the recorded rejections."""

        return [
            {"reason": event.payload.get("reason"), "detail": event.payload.get("detail")}
            for event in self.store.list_events(mission_id)
            if event.type in {"TaskGraphRejected", "PlanningRejected"}
        ]

    async def _create_planner_intent(self, mission_id: str, *, ordinal: int) -> DispatchIntent:
        mission = self.store.get_mission(mission_id)
        assert mission is not None
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        source_binding = self._active_source_binding(mission_id)
        package = build_planner_package(
            mission,
            workspace_files=sorted(set(seed) | set(source_binding.get("source_versions", {}))),
            source_versions=source_binding.get("source_versions"),
            attempt_ordinal=ordinal,
            rejected=self._planning_rejections(mission_id) if ordinal > 1 else (),
            deployed_layers=self._deployed,
            budget_floor=self._budget_floor(mission_id),
            domain=self.commit.domain_for(mission_id),
        )
        decision = self._route_service("planner", mission_id)
        template = self._template(PLANNER, mission_id)
        config = AgentConfig(
            name=f"planner-{ordinal}",
            instructions=template.instructions,
            model_profile_ref=decision.profile_id,
            tool_names=(),
            limits=AgentLimits(
                max_model_calls_per_turn=4,
                max_tool_calls_per_turn=1,
                turn_deadline_seconds=self._config.turn_deadline_seconds,
            ),
        )
        message = user_message_json(package.text)
        subject = f"{mission_id}:planner:{ordinal}"
        return self.commit.create_service_intent(
            kind="plan",
            subject_id=subject,
            mission_id=mission_id,
            account_id=mission_account(mission_id),
            creation_key=subject,
            input_id="attempt-input",
            input_hash=sha256_hex(message),
            config={
                "agent_config": config.to_json(),
                "message": message,
                "context_version": package.context_version,
                "prompt_version": template.prompt_version,
                "base_version": mission.version,
                "ordinal": ordinal,
                **source_binding,
                **self._service_config(decision),
            },
            reservation=self._reservation(self._config.planner_reserve_tokens, decision.profile_id),
        )

    # -------------------------------------------------------------- dispatch
    def _reservation(self, tokens: int, profile_id: str | None = None) -> Reservation:
        profile = self._profiles.get(profile_id or self._default_profile)
        table = profile.price_table if profile is not None else self._config.price_table
        if table is None:
            return Reservation(tokens=tokens, cost_micros=0)
        rate = max(table.input_micros_per_million_tokens, table.output_micros_per_million_tokens)
        return Reservation(tokens=tokens, cost_micros=(tokens * rate + 999_999) // 1_000_000)

    async def _dispatch(self, intent: DispatchIntent) -> bool:
        """ORCH §4.3 steps 2–3 with the identity frozen in the intent (D5')."""

        if self._pool_missing(intent):
            return False
        claimed = self.commit.claim_intent(
            intent.intent_id, owner=self._owner, lease_seconds=self._config.lease_seconds
        )
        if claimed is None:
            return False
        config = claimed.config
        if claimed.kind == "attempt":
            attempt = self.store.get_attempt(claimed.subject_id)
            assert attempt is not None
            if attempt.status in TERMINAL_ATTEMPT:  # cancelled / superseded before it ran
                self.commit.settle_intent(claimed.intent_id, "FAILED")
                return True
            try:
                self._bind_workspace(attempt)
            except ArtifactConflict as error:  # an upstream artifact file moved / changed
                self.commit.settle_intent(claimed.intent_id, "FAILED")
                self.commit.mark_attempt_lost(attempt.id, reason="upstream_artifact_missing")
                self.commit.stop_task(
                    attempt.task_id,
                    stop_reason=MissionStopReason.ARTIFACT_CONFLICT,
                    detail={"error": str(error)},
                )
                await self._release_mission(attempt.mission_id)
                self._note(f"attempt {attempt.id}: upstream artifacts unusable → stopped")
                return True
        if claimed.state == "CLAIMED":
            agent_id, _run_id, _ = await self.bridge_for(claimed).create(
                creation_key=claimed.creation_key, config_json=config["agent_config"]
            )
            expected = await self.bridge_for(claimed).expected_turn_id(
                agent_id=agent_id, input_id=claimed.input_id
            )
            self._fault("after_agent_created", claimed.kind)
            claimed = self.commit.record_agent_created(
                claimed.intent_id, agent_id=agent_id, expected_turn_id=expected
            )
            if claimed.kind == "critic":
                self._bind_critic(agent_id, config)
            elif claimed.kind == "attempt":
                self._bind_agent(agent_id, config)
        if claimed.state == "AGENT_CREATED":
            assert claimed.agent_id is not None
            if claimed.kind == "critic":
                self._bind_critic(claimed.agent_id, config)
            elif claimed.kind == "attempt":
                self._bind_agent(claimed.agent_id, config)
            try:
                receipt = await self.bridge_for(claimed).submit(
                    agent_id=claimed.agent_id,
                    input_id=claimed.input_id,
                    message_json=config["message"],
                )
            except Exception as error:  # noqa: BLE001 - the created Agent is gone (P0-3)
                if claimed.kind != "attempt":
                    raise
                self._note(f"{claimed.subject_id}: created executor unreachable ({error})")
                self.commit.mark_attempt_lost(claimed.subject_id, reason="executor_agent_missing")
                self.commit.settle_intent(claimed.intent_id, "FAILED")
                await self._release_attempt(claimed.subject_id, cancel=False)
                return True
            self._fault("after_submit", claimed.kind)
            self.commit.record_submitted(claimed.intent_id, receipt=receipt)
            self._note(f"dispatched {claimed.kind} {claimed.subject_id} → agent {claimed.agent_id}")
        return True

    def _upstream_inputs(self, attempt: Attempt) -> list[UpstreamInput]:
        intent = self.store.get_intent_for_subject(attempt.id)
        raw = [] if intent is None else list(intent.config.get("inputs", []))
        return [UpstreamInput.from_json(item) for item in raw]

    def _active_source_binding(self, mission_id: str) -> dict[str, Any]:
        domain = self.commit.domain_for(mission_id)
        if not domain.source_roots:
            return {}
        return {
            "source_versions": {
                row["path"]: row["version_hash"]
                for row in self.store.list_sources(mission_id, active_only=True)
            },
            "source_roots": list(domain.source_roots),
        }

    def _frozen_source_binding(self, attempt: Attempt) -> dict[str, Any]:
        intent = self.store.get_intent_for_subject(attempt.id)
        if intent is None or "source_versions" not in intent.config:
            return {}  # Legacy intents never acquire today's source registry.
        return {
            "source_versions": dict(intent.config["source_versions"]),
            "source_roots": list(intent.config.get("source_roots", ())),
        }

    def _source_files(self, attempt: Attempt) -> dict[str, bytes]:
        from ..verification.evidence_resolver import EvidenceResolver

        binding = self._frozen_source_binding(attempt)
        versions = binding.get("source_versions", {})
        if not versions:
            return {}
        self.validate_source_storage(attempt.mission_id)
        mission = self.store.get_mission(attempt.mission_id)
        assert mission is not None
        resolver = EvidenceResolver(self.store, self.assembled.workspaces.artifact_store)
        files: dict[str, bytes] = {}
        for path, version in versions.items():
            source = resolver.read_source(
                tenant_id=mission.tenant_id,
                mission_id=mission.id,
                path=path,
                version=version,
                source_roots=binding["source_roots"],
            )
            if source.status != "resolved" or source.data is None:
                raise ArtifactConflict(f"frozen source {path} is {source.status}")
            files[path] = source.data
        return files

    def _bind_workspace(self, attempt: Attempt) -> None:
        mission = self.store.get_mission(attempt.mission_id)
        assert mission is not None
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        previous = None
        if attempt.retry_of is not None:
            previous = self.assembled.workspaces.root / attempt.retry_of
        inputs: dict[str, Path | bytes] = {}
        for item in self._upstream_inputs(attempt):
            artifact = self.store.get_artifact(item.artifact_id)
            try:  # P3.2 D3: the stored bytes, hash re-checked, never through a symlink
                if artifact is None or artifact.content_hash != item.content_hash:
                    raise ArtifactStoreError("missing", item.path)
                read_verified(artifact)
            except ArtifactStoreError as error:
                raise ArtifactConflict(
                    f"upstream artifact {item.artifact_id} ({item.path}) is missing or changed"
                ) from error
            inputs[item.path] = Path(artifact.storage_uri)
        binding = self._frozen_source_binding(attempt)
        source_roots = binding.get("source_roots", ())
        inputs = {
            path: value
            for path, value in inputs.items()
            if not _under_source_root(path, source_roots)
        }
        inputs.update(self._source_files(attempt))
        task = self.store.get_task(attempt.task_id)
        # P3.2 D4 (review round 2 P2-4): a rebind — recover() and every dispatch — is
        # checked against the registered identity, never the directory's content
        base = sha256_hex(
            {
                "seed": {path: sha256_hex_text(content) for path, content in seed.items()},
                "inputs": {item.path: item.content_hash for item in self._upstream_inputs(attempt)},
                "previous": attempt.retry_of,
                **self._frozen_source_binding(attempt),
            }
        )
        detail = {
            "path": attempt.id,
            "seed": sorted(seed),
            "read_only_inputs": sorted(self._read_only_inputs(attempt.id)),
            "writable_outputs": [] if task is None else list(task.outputs),
            "adopted": False,
        }
        record = self.store.get_workspace(attempt.id)
        root_path = self.assembled.workspaces.root / attempt.id
        if root_path.is_symlink():  # code review round 1 P2-10: never adopt a link as a tree
            raise ArtifactConflict(
                f"workspace_identity_mismatch: {attempt.id} is a symlink, not a workspace"
            )
        exists = root_path.exists()
        building = False
        if record is None and exists:  # a tree from before 0.10: adopted as it is
            self.store.register_workspace(
                attempt.id,
                kind="attempt",
                mission_id=attempt.mission_id,
                attempt_id=attempt.id,
                base_snapshot=base,
                state="ACTIVE",
                detail={**detail, "adopted": True},
            )
        elif record is None or record["state"] == "CREATING":
            if record is not None:  # a tree half-made by a crash is rebuilt
                self.assembled.workspaces.remove(attempt.id)
            self.store.register_workspace(
                attempt.id,
                kind="attempt",
                mission_id=attempt.mission_id,
                attempt_id=attempt.id,
                base_snapshot=base,
                state="CREATING",
                detail=detail,
            )
            building = True
        elif record["state"] != "ACTIVE" or record["base_snapshot"] != base:
            raise ArtifactConflict(
                f"workspace_identity_mismatch: {attempt.id} is registered "
                f"{record['state']} with another seed, inputs or repair source"
            )
        try:
            workspace = self.assembled.workspaces.create(
                attempt.id,
                seed=seed,
                previous=previous,
                inputs=inputs,
                replace_input_roots=source_roots,
            )
        except WorkspaceError as error:  # P3.2 D3: e.g. a symlink in the previous tree
            raise ArtifactConflict(str(error)) from error
        if building:
            self.store.set_workspace_state(attempt.id, "ACTIVE")
        if task is not None:
            for path, content in self._protected_files(mission, task, attempt).items():
                if isinstance(content, bytes):
                    if not building:
                        continue  # Preserve ACTIVE-tree tamper evidence across recovery.
                    try:
                        current = read_nofollow(workspace.root / path)
                    except ArtifactStoreError:
                        current = None
                    if current != content:
                        workspace.write_bytes(path, content)
                elif (
                    workspace.read_text(path) != content
                    if (workspace.root / path).is_file()
                    else True
                ):
                    workspace.write_text(path, content)

    def _register_copy(
        self, kind: str, name: str, *, mission_id: str, attempt_id: str, detail: dict[str, Any]
    ) -> None:
        """P3.2 D4: a verification copy or judgment tree, registered each time it is rebuilt
        (its identity is what it was rebuilt from)."""

        self.store.register_workspace(
            name,
            kind=kind,
            mission_id=mission_id,
            attempt_id=attempt_id,
            base_snapshot=sha256_hex(detail),
            state="ACTIVE",
            detail={"path": name, **detail},
        )

    def cleanup_workspaces(self) -> list[str]:
        """P3.2 D4: remove the directories of finished Missions once the retention period
        has passed.  The registry rows (now CLEANED) and the content-addressed Artifact
        bytes stay, so snapshots, artifact reads, replay and reconciliation still work."""

        cutoff = self.store.now - self._config.workspace_retention_seconds
        removed: list[str] = []
        for row in self.store.list_workspaces():
            if row["state"] == "CLEANED" or row["updated_at"] > cutoff:
                continue
            mission = self.store.get_mission(row["mission_id"])
            if mission is None or mission.status not in TERMINAL_MISSION:
                continue
            self.assembled.workspaces.remove(str(row["detail"].get("path", row["workspace_id"])))
            self.store.set_workspace_state(row["workspace_id"], "CLEANED")
            removed.append(row["workspace_id"])
        return removed

    def _input_files(self, attempt: Attempt) -> dict[str, bytes]:
        """P3.2 review round 2 P1-3: an Attempt's upstream inputs, read back from the
        store with their hashes re-checked — the verification copy is rebuilt from them."""

        files: dict[str, bytes] = {}
        for item in self._upstream_inputs(attempt):
            artifact = self.store.get_artifact(item.artifact_id)
            try:
                if artifact is None or artifact.content_hash != item.content_hash:
                    raise ArtifactStoreError("missing", item.path)
                files[item.path] = read_verified(artifact)
            except ArtifactStoreError as error:
                raise WorkspaceError(f"upstream input unreadable: {error}") from error
        files.update(self._source_files(attempt))
        return files

    def _bind_agent(self, agent_id: str, config: Mapping[str, Any]) -> None:
        cap = config.get("max_tool_calls")
        self.assembled.gateway.bind(
            agent_id,
            WorkspaceBinding(
                str(config["attempt_id"]),
                "work",
                True,
                tuple(config.get("allowed_tools", WORKER_TOOLS)),
                tuple(str(p) for p in config.get("untrusted_sources", ())),
                max_tool_calls=None if cap is None else int(cap),
                protected=self._read_only_inputs(str(config["attempt_id"])),
                protected_prefixes=tuple(config.get("source_roots", ())),
                denied_prefixes=self._config.deployment_policy.denied_path_prefixes,
            ),
        )

    def _bind_critic(self, agent_id: str, config: Mapping[str, Any]) -> None:
        self.assembled.gateway.bind(
            agent_id,
            WorkspaceBinding(
                str(config["attempt_id"]),
                "verify",
                False,
                tuple(t for t in CRITIC_TOOLS if t in self._config.deployment_policy.allowed_tools),
                tuple(str(p) for p in config.get("untrusted_sources", ())),
                protected_prefixes=tuple(config.get("source_roots", ())),
                denied_prefixes=self._config.deployment_policy.denied_path_prefixes,
            ),
        )

    # ------------------------------------------------------------ knowledge (step 4)
    def _gather_knowledge(
        self, mission: Mission, task: Task, tasks_by_id: Mapping[str, Task]
    ) -> KnowledgeContext:
        """§10 items 4/5/7 for one Task: ranked Verified Knowledge (read back in full),
        the disputed claims (marked), the candidate / rejected claims for the templates
        that may see them, and the deterministic summaries.  Raises
        ``RetrievalUnavailable`` instead of pretending the Mission has no knowledge."""

        if not self._config.knowledge_sharing:
            return KnowledgeContext.unavailable("knowledge_sharing disabled", status="disabled")
        try:
            self._fault("retrieval_unavailable", "attempt")
        except InjectedCrash as error:
            raise RetrievalUnavailable(str(error)) from error
        try:
            records = self.store.list_knowledge(mission.id)
            claims = self.store.list_mission_claims(mission.id)
            summaries = build_summaries(self.store, mission.id)
        except (StoreBusy, OSError, ValueError) as error:  # index unreadable / not ready
            raise RetrievalUnavailable(str(error)) from error
        ranked = rank_knowledge(
            task, records, tasks_by_id=tasks_by_id, limit=self._config.max_knowledge_items
        )
        by_id = {record.id: record for record in records}
        from ..context.compression import GLOBAL_BRANCH, branch_of

        branch = branch_of(task, tasks_by_id)
        return KnowledgeContext(
            retrieval=ranked,
            verified=tuple(knowledge_view(by_id[item.id], item) for item in ranked.items),
            disputed=tuple(disputed_claims(claims, mission_id=mission.id)),
            candidates=tuple(
                candidate_claims(
                    claims,
                    mission_id=mission.id,
                    statuses=(
                        ClaimStatus.PROPOSED,
                        ClaimStatus.UNDER_REVIEW,
                        ClaimStatus.SUPPORTED,
                    ),
                )
            ),
            rejected=tuple(
                candidate_claims(claims, mission_id=mission.id, statuses=(ClaimStatus.REJECTED,))
            ),
            branch_summary=summaries.get(branch if branch != GLOBAL_BRANCH else GLOBAL_BRANCH),
            global_summary=summaries.get(f"mission:{mission.id}"),
        )

    def _knowledge_or_unavailable(
        self, mission: Mission, task: Task | None
    ) -> KnowledgeContext | None:
        """For the Critic / Verifier layer: never blocks a verification on retrieval."""

        if task is None:
            return None
        tasks_by_id = {t.id: t for t in self.store.list_tasks(mission.id)}
        try:
            return self._gather_knowledge(mission, task, tasks_by_id)
        except RetrievalUnavailable as error:
            return KnowledgeContext.unavailable(str(error))

    # --------------------------------------------------------------- collect
    async def _collect(self, intent: DispatchIntent) -> bool:
        assert intent.agent_id is not None and intent.expected_turn_id is not None
        if self._pool_missing(intent):
            return False
        try:
            result = await self.bridge_for(intent).result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
        except Exception as error:  # noqa: BLE001 - AgentNotFound & co.: not alive (P0-3)
            self._note(f"{intent.subject_id}: executor unreachable ({error})")
            result = None
        if result is None:
            return await self._observe_liveness(intent)
        self._note_turn_health(intent, result)
        self._fault("after_turn_committed", intent.kind)
        if intent.kind == "plan":
            await self._collect_plan(intent, result)
        elif intent.kind == "attempt":
            await self._collect_attempt(intent, result)
        elif intent.kind == "manager":
            await self._collect_manager(intent, result)
        return True

    async def _collect_after_stop(self, intent: DispatchIntent) -> bool:
        """A turn still running for a terminal Mission (D3-6'): import its usage when
        it settles, keep a committed late result as history, settle the reservation
        and the intent; never plan, verify or accept anything for it."""

        assert intent.agent_id is not None and intent.expected_turn_id is not None
        try:
            result = await self.bridge_for(intent).result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
        except Exception:  # noqa: BLE001 - executor gone: nothing more to collect
            result = None
            liveness = Liveness(False, None, False, None, None, False)
        else:
            liveness = (
                Liveness(True, None, False, None, None, True)
                if result is not None
                else await self.bridge_for(intent).liveness(
                    agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                )
            )
        if result is None and liveness.alive:
            await self._release_attempt(intent.subject_id, cancel=True)
            return False
        self._import_usage(intent)
        if intent.kind == "attempt":
            attempt = self.store.get_attempt(intent.subject_id)
            assert attempt is not None
            if result is not None and attempt.status in {
                AttemptStatus.SUPERSEDED,
                AttemptStatus.CANCELLED,
            }:
                self._record_late_result(attempt, result)
            self._settle_if_known(attempt)
            self._settle_intent(intent, "SETTLED" if result is not None else "FAILED")
            await self._release_attempt(attempt.id, cancel=False)
        else:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, intent.mission_id)
            self.assembled.gateway.unbind(intent.agent_id)
        self._note(f"{intent.subject_id}: collected after the Mission stopped")
        return True

    def _record_late_result(self, attempt: Attempt, result) -> None:  # type: ignore[no-untyped-def]
        text = "" if result.public_output is None else str(result.public_output.content)
        summary, late_paths = "", []
        try:
            raw = extract_block(text, RESULT_ENVELOPE_TAG)
            summary = str(raw.get("summary", ""))[:400]
            late_paths = [str(p) for p in raw.get("artifacts", [])]
        except BlockError:
            summary = text[:200] or f"turn {result.state}: {jsonable(result.error or {})}"[:200]
        self.commit.record_late_result(
            attempt.id, turn_id=result.turn_id, summary=summary, artifacts=late_paths
        )
        self._note(f"attempt {attempt.id}: late result recorded as history")

    async def _observe_liveness(self, intent: DispatchIntent) -> bool:
        assert intent.agent_id and intent.expected_turn_id
        liveness: Liveness = await self.bridge_for(intent).liveness(
            agent_id=intent.agent_id, turn_id=intent.expected_turn_id
        )
        if intent.kind == "plan":
            if liveness.exists:
                return False
            self._import_usage(intent)
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, intent.mission_id)
            await self._planning_rejected(
                intent, reason="planner_turn_missing", detail={"agent_id": intent.agent_id}
            )
            return True
        if intent.kind == "manager":
            if liveness.exists:
                return False
            self._import_usage(intent)
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, intent.mission_id)
            await self._manager_unusable(intent, reason="manager_turn_missing")
            return True
        if intent.kind != "attempt":
            return False
        attempt = self.store.get_attempt(intent.subject_id)
        assert attempt is not None
        if attempt.status in TERMINAL_ATTEMPT:  # closed by a cascade while its turn ran
            if liveness.alive:
                await self._release_attempt(attempt.id, cancel=True)
                return False  # collected (cost, late result) once the turn settles
            self._import_usage(intent)
            self._settle_if_known(attempt)
            self._settle_intent(intent, "FAILED")
            await self._release_attempt(attempt.id, cancel=False)
            return True
        now = self.store.now
        if liveness.alive:
            due = (
                attempt.lease_expires_at is None
                or now >= attempt.lease_expires_at - self._config.lease_seconds / 2
            )
            if due:
                try:
                    attempt = self.commit.renew_lease(
                        attempt.id,
                        owner=self._owner,
                        lease_seconds=self._config.lease_seconds,
                        liveness=liveness.to_json(),
                    )
                except CommitRejected:
                    return False  # another live owner; not ours yet (review P1-2)
            running = liveness.state == str(AgentTurnState.RUNNING)
            if running and not liveness.blocked and attempt.progress_at is not None:
                # D3-4': only a *running* turn is timed; queued / semaphore-waiting ones are not
                stalled_for = now - attempt.progress_at
                if stalled_for > self._config.stall_seconds:
                    # D6': alive, no blocker, no provider progress within stall_seconds.
                    self._import_usage(intent)
                    self.commit.mark_attempt_timed_out(
                        attempt.id,
                        reason="executor_stalled",
                        detail={
                            "stalled_seconds": round(stalled_for, 3),
                            "progress_marker": attempt.progress_marker,
                        },
                    )
                    self.commit.settle_intent(intent.intent_id, "FAILED")
                    await self._release_attempt(attempt.id, cancel=True)
                    self._note(
                        f"attempt {attempt.id} TIMED_OUT: no progress for {stalled_for:.1f}s"
                    )
                    return True
            return False
        if not liveness.exists:
            self._import_usage(intent)
            self.commit.mark_attempt_lost(attempt.id, reason="executor_turn_missing")
            self.commit.settle_intent(intent.intent_id, "FAILED")
            await self._release_attempt(attempt.id, cancel=False)
            self._note(f"attempt {attempt.id} LOST: turn missing")
            return True
        return False

    async def _cancel_turn(self, intent: DispatchIntent) -> None:
        """Best-effort cooperative cancel of a superseded SDK turn (never a kernel cancel)."""

        if intent.agent_id is None or intent.expected_turn_id is None:
            return
        try:
            receipt = await self.bridge_for(intent).runtime.cancel_turn(
                intent.agent_id,
                intent.expected_turn_id,
                command_id=f"{intent.subject_id}:cancel",
                wait_timeout=0.0,
            )
            self.cancel_receipts.append(
                {
                    "attempt_id": intent.subject_id,
                    "agent_id": receipt.agent_id,
                    "turn_id": receipt.turn_id,
                    "command_id": receipt.command_id,
                    "state": str(receipt.state),
                }
            )
        except Exception as error:  # noqa: BLE001 - cancellation is advisory here
            self._note(f"cancel_turn for {intent.subject_id} not applied: {error}")

    async def _release_attempt(self, attempt_id: str, *, cancel: bool) -> None:
        """D3-17: a terminal Attempt's executor loses its workspace binding at once (a
        zombie turn can no longer write) and, when asked, its SDK turn is cancelled."""

        intent = self.store.get_intent_for_subject(attempt_id)
        if intent is None or intent.agent_id is None:
            return
        self.assembled.gateway.unbind(intent.agent_id)
        key = f"{attempt_id}:{'cancel' if cancel else 'unbind'}"
        if key in self._released:
            return
        self._released.add(key)
        if cancel and intent.expected_turn_id is not None:
            try:
                liveness = await self.bridge_for(intent).liveness(
                    agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                )
            except Exception:  # noqa: BLE001
                liveness = Liveness(False, None, False, None, None, False)
            if liveness.alive:
                await self._cancel_turn(intent)

    async def _release_mission(self, mission_id: str) -> None:
        """After a stop cascade: unbind and cancel every Attempt the cascade closed."""

        for task in self.store.list_tasks(mission_id):
            for attempt in self.store.list_attempts(task.id):
                if attempt.status in {AttemptStatus.CANCELLED, AttemptStatus.SUPERSEDED}:
                    await self._release_attempt(attempt.id, cancel=True)

    def _settle_intent(self, intent: DispatchIntent, state: str) -> None:
        self.commit.settle_intent(intent.intent_id, state)

    def _import_usage(self, intent: DispatchIntent) -> None:
        assert intent.agent_id is not None
        facts = self.bridge_for(intent).usage_facts(agent_id=intent.agent_id)
        self.commit.import_usage(intent.subject_id, intent.mission_id, facts)

    def _reimport_unsettled(self, mission: Mission) -> None:
        """D3-6': LOST / TIMED_OUT / SUPERSEDED / CANCELLED Attempts whose reservation is
        still open get their SDK usage imported again and settled when it is known."""

        for task in self.store.list_tasks(mission.id):
            for attempt in self.store.list_attempts(task.id):
                if attempt.status not in TERMINAL_ATTEMPT:
                    continue
                with self.store.transaction():
                    reservation = self.commit.ledger.reservation(attempt.id)
                if reservation is None or reservation["state"] == "SETTLED":
                    continue
                intent = self.store.get_intent_for_subject(attempt.id)
                if intent is not None and intent.agent_id is not None:
                    try:
                        self._import_usage(intent)
                    except Exception as error:  # noqa: BLE001 - SDK ledger unreachable
                        self._note(f"attempt {attempt.id}: usage re-import failed ({error})")
                        continue
                self._settle_if_known(attempt)

    def _settle_service_if_known(
        self, subject_id: str, mission_id: str, task_id: str | None = None
    ) -> None:
        with self.store.transaction():
            unknown = self.commit.ledger.has_unknown_usage(subject_id)
        if unknown:
            self._note(f"{subject_id}: unknown provider charge, reservation held")
            return
        self.commit.settle_subject(subject_id, mission_id, task_id=task_id)

    def _settle_if_known(self, attempt: Attempt) -> None:
        """Settle the Attempt's reservation unless an UNKNOWN charge keeps it occupied (ORCH §12.2)."""

        with self.store.transaction():
            unknown = self.commit.ledger.has_unknown_usage(attempt.id)
        if unknown:
            self._note(f"attempt {attempt.id}: unknown provider charge, reservation held")
            self.commit.record_reservation_held(  # L3-3: visible and traceable, never auto-released
                attempt.id, attempt.mission_id, task_id=attempt.task_id, reason="unknown_usage"
            )
            return
        self.commit.settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)

    async def _planning_rejected(
        self, intent: DispatchIntent, *, reason: str, detail: Mapping[str, Any]
    ) -> None:
        mission = self.store.get_mission(intent.mission_id)
        assert mission is not None
        ordinal = int(intent.config.get("ordinal", 1))
        self._note(f"planning attempt {ordinal} rejected: {reason}")
        if reason != "task_graph_rejected":  # graph rejections are already durable events
            self.commit.record_planning_rejected(
                mission.id, ordinal=ordinal, reason=reason, detail=detail
            )
        if ordinal < self._config.max_planning_attempts:
            await self._try_planner_intent(mission.id, ordinal=ordinal + 1)
        else:
            self.commit.fail_planning(
                mission.id, reason=reason, detail={"attempts": ordinal, **dict(detail)}
            )

    async def _collect_plan(self, intent: DispatchIntent, result) -> None:  # type: ignore[no-untyped-def]
        mission = self.store.get_mission(intent.mission_id)
        assert mission is not None
        self._import_usage(intent)
        text = "" if result.public_output is None else str(result.public_output.content)
        echoed = self.bridge_for(intent).echoed_models(agent_id=intent.agent_id or "")
        if echoed and echoed != {self._expected_model(intent)}:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self.commit.fail_planning(
                mission.id,
                reason="model_echo_mismatch",
                detail={"expected": self._expected_model(intent), "echoed": sorted(echoed)},
                stop_reason=MissionStopReason.MODEL_ECHO_MISMATCH,
            )
            self._note(f"planner: model echo mismatch {sorted(echoed)} → mission stopped")
            return
        try:
            if result.state is not AgentTurnState.COMMITTED:
                raise ContractError(f"planner turn failed: {dict(result.error or {})}")
            proposal = parse_task_graph_proposal(text)
        except ContractError as error:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            await self._planning_rejected(
                intent, reason="proposal_unreadable", detail={"error": str(error)}
            )
            return
        try:
            tasks, receipt = self.commit.commit_task_graph(
                mission.id,
                proposal,
                base_version=int(intent.config["base_version"]),
                source={
                    "intent_id": intent.intent_id,
                    "agent_id": intent.agent_id,
                    "turn_id": result.turn_id,
                },
            )
        except CommitRejected as error:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            await self._planning_rejected(
                intent, reason="task_graph_rejected", detail={"error": str(error)}
            )
            return
        self._note(
            f"task graph committed: {[task.id for task in tasks]} (warnings={receipt.get('warnings')})"
        )
        self._settle_intent(intent, "SETTLED")
        self._settle_service_if_known(intent.subject_id, mission.id)

    async def _collect_attempt(self, intent: DispatchIntent, result) -> None:  # type: ignore[no-untyped-def]
        attempt = self.store.get_attempt(intent.subject_id)
        assert attempt is not None
        self._import_usage(intent)
        if attempt.status is not AttemptStatus.RUNNING:
            if attempt.status in {AttemptStatus.SUPERSEDED, AttemptStatus.CANCELLED}:
                # D3-6': a late result on a closed Attempt is history, never a transition
                self._record_late_result(attempt, result)
                self._settle_if_known(attempt)
            self._settle_intent(intent, "SETTLED")
            await self._release_attempt(attempt.id, cancel=False)
            return
        echoed = self.bridge_for(intent).echoed_models(agent_id=intent.agent_id or "")
        if echoed and echoed != {self._expected_model(intent)}:
            # D10': the provider answered as a different model; charges are unknown and
            # the deployment binding is wrong.  Fail fast and visibly, hold the reservation.
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="model_echo_mismatch",
                detail={"expected": self._expected_model(intent), "echoed": sorted(echoed)},
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            self.commit.stop_task(
                attempt.task_id,
                stop_reason=MissionStopReason.MODEL_ECHO_MISMATCH,
                detail={"expected": self._expected_model(intent), "echoed": sorted(echoed)},
            )
            await self._release_mission(attempt.mission_id)
            self._note(f"attempt {attempt.id}: model echo mismatch {sorted(echoed)} → stopped")
            return
        if result.state is AgentTurnState.FAILED:
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="turn_failed",
                detail={
                    "error": jsonable(result.error or {}),
                    "error_kind": classify_turn_error(result.error),  # D6-4' classification
                },
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            self._note(f"attempt {attempt.id}: SDK turn failed → RETRY_WAIT")
            return
        text = "" if result.public_output is None else str(result.public_output.content)
        try:
            envelope, client_result_id = self._parse_envelope(text, attempt, turn_id=result.turn_id)
            self.commit.check_result_evidence(attempt.mission_id, envelope)
        except (ContractError, CommitRejected) as error:
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason=(
                    "result_evidence_kind_not_allowed"
                    if isinstance(error, CommitRejected)
                    else "envelope_invalid"
                ),
                detail={"error": str(error), "output_head": text[:400]},
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            self._note(f"attempt {attempt.id}: envelope invalid → RETRY_WAIT ({error})")
            return
        mission = self.store.get_mission(attempt.mission_id)
        task = self.store.get_task(attempt.task_id)
        assert mission is not None and task is not None
        if envelope.outcome is not ResultOutcome.CANDIDATE:
            # D5-5: execution evidence, not a candidate — history + a management decision
            stored = self.commit.record_outcome_result(
                attempt.id,
                envelope=envelope,
                turn_id=result.turn_id,
                usage_refs=tuple(result.usage_refs),
            )
            self._settle_intent(intent, "SETTLED")
            await self._release_attempt(attempt.id, cancel=False)
            self._note(f"attempt {attempt.id}: outcome {envelope.outcome} → manager decision")
            await self._request_management(
                mission,
                task,
                trigger=f"outcome:{stored.envelope.id}",
                result_id=stored.envelope.id,
                attempt_id=attempt.id,
            )
            return
        workspace = self.assembled.workspaces.get(attempt.id)
        artifacts = workspace.snapshot(
            mission_id=attempt.mission_id,
            task_id=attempt.task_id,
            produced_by=intent.agent_id or attempt.id,
            versions=next_versions(self.store.list_mission_artifacts(attempt.mission_id)),
        )
        known = {artifact.path for artifact in artifacts}
        source_roots = tuple(self._frozen_source_binding(attempt).get("source_roots", ()))
        source_hashes = {
            path: sha256_hex_text(content) for path, content in self._source_files(attempt).items()
        }
        # Check the physical snapshot first: case aliases in a reported path must not
        # hide a changed source behind a generic missing-artifact diagnostic.
        source_rewritten = {
            artifact.path
            for artifact in artifacts
            if _under_source_root(artifact.path, source_roots)
            and artifact.content_hash != source_hashes.get(artifact.path)
        }
        missing = [path for path in envelope.artifacts if path not in known]
        if missing and not source_rewritten:
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="envelope_invalid",
                detail={"error": f"artifacts not in the workspace: {missing}"},
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            self._note(f"attempt {attempt.id}: artifacts missing → RETRY_WAIT")
            return
        # D3-7': the Attempt's artifact set = what it listed ∪ what it changed relative to
        # its initial inputs (seed + upstream); protected paths are never registered as
        # produced work (a rewrite there is tampering, reported by rule_check instead).
        initial = {
            path: sha256_hex_text(content)
            for path, content in dict(
                (mission.final_report or {}).get("workspace_seed", {})
            ).items()
        }
        for item in self._upstream_inputs(attempt):
            initial[item.path] = item.content_hash
        guarded = {
            path: sha256_hex_text(content)
            for path, content in self._protected_files(mission, task, attempt).items()
        }
        listed = set(envelope.artifacts)
        by_path = {artifact.path: artifact for artifact in artifacts}
        rewritten = sorted(
            source_rewritten
            | {
                path
                for path in listed & known
                if path in guarded and by_path[path].content_hash != guarded[path]
            }
        )
        if rewritten:  # P1-6: a protected path is never registered as produced work
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="protected_path_rewritten",
                detail={"paths": rewritten},
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            self._note(f"attempt {attempt.id}: rewrote protected {rewritten} → RETRY_WAIT")
            return
        referenced = [
            artifact
            for artifact in artifacts
            if (
                artifact.path not in guarded
                and (artifact.path in listed or initial.get(artifact.path) != artifact.content_hash)
            )
            or (artifact.path in guarded and artifact.path in listed)  # unchanged, merely cited
        ]
        self.commit.record_result(
            attempt.id,
            envelope=envelope,
            turn_id=result.turn_id,
            artifacts=referenced,
            usage_refs=tuple(result.usage_refs),
        )
        self._fault("after_result_submitted", "attempt")
        self._settle_intent(intent, "SETTLED")
        await self._release_attempt(attempt.id, cancel=False)
        self._client_ids[envelope.id] = client_result_id
        self._note(f"attempt {attempt.id}: result {envelope.id} submitted")

    def _parse_envelope(
        self, text: str, attempt: Attempt, *, turn_id: str
    ) -> tuple[ResultEnvelope, str | None]:
        try:
            raw = extract_block(text, RESULT_ENVELOPE_TAG)
        except BlockError as error:
            raise ContractError(str(error)) from error
        client_ids = {raw.get("id"), raw.get("result_id")} - {None}
        if len(client_ids) > 1:
            raise ContractError("result carries both id and result_id with different values")
        client_result_id = None if not client_ids else str(next(iter(client_ids)))
        raw.pop("id", None)
        raw.pop("result_id", None)
        raw.setdefault("mission_id", attempt.mission_id)
        provisional = ResultEnvelope.from_json({**raw, "id": "result-provisional"})
        if provisional.attempt_id != attempt.id or provisional.task_id != attempt.task_id:
            raise ContractError(
                "result identity does not match the Attempt / Task it was submitted for"
            )
        if provisional.mission_id != attempt.mission_id:
            raise ContractError("result mission_id does not match")
        body = provisional.to_json()
        body.pop("id")
        result_id = ids.result_id(attempt.id, turn_id, sha256_hex(body))
        envelope = ResultEnvelope.from_json({**body, "id": result_id})
        if outside_text(text, RESULT_ENVELOPE_TAG):
            logger.info("orchestrator.envelope_prose", extra={"attempt_id": attempt.id})
        return envelope, client_result_id

    # ---------------------------------------------------------------- verify
    async def _verify(self, result_id: str) -> bool:
        stored = self.store.get_result(result_id)
        assert stored is not None
        attempt = self.store.get_attempt(stored.envelope.attempt_id)
        task = self.store.get_task(stored.envelope.task_id)
        mission = self.store.get_mission(stored.envelope.mission_id)
        assert attempt is not None and task is not None and mission is not None
        if attempt.status in TERMINAL_ATTEMPT:
            return False  # closed by a cascade; its result was rejected as history
        try:  # D3-10': verification is done by the Attempt's lease holder only
            attempt = self.commit.renew_lease(
                attempt.id,
                owner=self._owner,
                lease_seconds=self._config.lease_seconds,
                liveness={"progress": attempt.progress_marker, "phase": "verifying"},
            )
        except CommitRejected:
            return False
        stored = self.commit.start_verification(result_id)
        protected = self._protected_files(mission, task, attempt)
        tampered = self.assembled.workspaces.tampered_protected(attempt.id, protected)
        artifacts = [
            a for a in self.store.list_artifacts(attempt.id) if a.id in set(stored.artifacts)
        ]
        # P3.2 review round 2 P1-3: rebuilt from the recorded bytes, never the live tree —
        # what is verified is what was recorded and what an approval will bind
        copy = self.assembled.workspaces.verification_copy(
            attempt.id,
            protected=protected,
            seed=dict((mission.final_report or {}).get("workspace_seed", {})),
            inputs=self._input_files(attempt),
            artifacts=artifacts,
        )
        self._register_copy(
            "verify",
            f"{attempt.id}-verify",
            mission_id=attempt.mission_id,
            attempt_id=attempt.id,
            detail={"artifacts": sorted(a.id for a in artifacts), "protected": sorted(protected)},
        )

        async def recorder(layer: LayerResult) -> None:
            self._hold_lease(attempt.id)  # P1-3: a lost lease aborts the verification
            detail = {"summary": layer.summary, **dict(layer.detail)}
            if layer.layer == "critic_review":
                # A recovered intent may still contain an older prompt than the
                # domain now selects. Attribute only a completed Critic verdict,
                # including a reused layer, to its durable execution ordinal.
                detail.pop("critic_intent_id", None)
                detail["verifier_version"] = None
                if layer.status in {"PASS", "FAIL", NEEDS_HUMAN}:
                    detail.update(self._critic_provenance(mission.id, attempt.id))
            else:
                detail["verifier_version"] = VERIFIER_VERSION
            self.commit.record_verification_layer(
                result_id,
                layer=layer.layer,
                status=layer.status,
                detail=detail,
            )
            if layer.status == "PASS":
                self._fault("after_layer_pass", "attempt")

        async def run_critic(test_output: str | None) -> CriticVerdict:
            try:
                return await self._run_critic(
                    mission,
                    task,
                    view_id=attempt.id,
                    subject_prefix=f"{attempt.id}:critic",
                    account_id=task_account(task.id),
                    artifacts=artifacts,
                    test_output=test_output,
                    attempt_id=attempt.id,
                )
            except BudgetExhausted as error:
                # a required layer that could not run is an ERROR, never a PASS (ORCH §12.4)
                raise ContractError(f"critic could not be funded: {error}") from error

        action_problems = self._action_problems(mission, task, artifacts, copy)
        human, reuse, escalation_left = self._human_inputs(result_id, task)
        domain = self.commit.domain_for(mission.id)
        assessment_binding = None
        evidence_resolver = None
        if domain.id == "doc-research-v1":
            from ..verification.assessments import assessment_binding_for
            from ..verification.evidence_resolver import EvidenceResolver

            try:
                assessment_binding = assessment_binding_for(
                    self.store,
                    task=task,
                    attempt=attempt,
                    envelope=stored.envelope,
                    artifacts=artifacts,
                )
            except ContractError as error:
                # A frozen contract cannot be reconstructed from today's Task or source
                # registry. Persist a real failure; never fabricate a reusable PASS.
                failure = LayerResult(
                    "rule_check",
                    "FAIL",
                    "document assessment binding invalid",
                    {"reason": "assessment_binding_invalid", "error": str(error)},
                )
                await recorder(failure)
                self.commit.fail_result(result_id, failures=[failure.to_json()], owner=self._owner)
                return True
            evidence_resolver = EvidenceResolver(
                self.store, self.assembled.workspaces.artifact_store
            )
        verdict = await self._router.verify(
            mission=mission,
            task=task,
            envelope=stored.envelope,
            artifacts=artifacts,
            verification_copy=copy,
            client_result_id=self._client_ids.get(result_id),
            run_critic=run_critic,
            recorder=recorder,
            tampered=tampered,
            knowledge=KnowledgeIndex.load(self.store, mission.id),
            require_synthesis_knowledge=self._config.knowledge_sharing,
            action_problems=action_problems,
            human=human,
            reuse=reuse,
            needs_human_allowed=escalation_left,
            domain=domain,
            assessment_binding=assessment_binding,
            evidence_resolver=evidence_resolver,
            ablated=frozenset({"critic_review"})
            if "critic" in self._config.ablations
            else frozenset(),
        )
        if verdict.critic is not None:
            self._critic_verdicts[result_id] = verdict.critic
        if verdict.suspended:  # D7-8': the sixth layer waits for a person
            reason = (
                "needs_human"
                if any(layer.status == NEEDS_HUMAN for layer in verdict.layers)
                else "policy"
            )
            try:
                self.commit.suspend_verification(
                    result_id,
                    owner=self._owner,
                    reason=reason,
                    layers=[layer.to_json() for layer in verdict.layers],
                )
            except (CommitRejected, IllegalTransition, ActionCommitError) as error:
                self._note(f"result {result_id}: suspension dropped ({error})")
                return True
            self._note(f"result {result_id} suspended: waiting for a person ({reason})")
            return True
        try:
            if verdict.passed:
                completed = self.commit.accept_result(
                    result_id,
                    verifier_results=[
                        layer.to_json() for layer in verdict.layers if layer.status == "PASS"
                    ],
                    owner=self._owner,
                    connectors=self._connectors,
                    deployment=self._config.deployment_policy,
                )
            else:
                self.commit.fail_result(result_id, failures=verdict.failures, owner=self._owner)
        except (CommitRejected, IllegalTransition) as error:
            # the Attempt was closed / taken over while we verified (P1-4): the verdict is
            # dropped; the library's state is whatever the other Commit made it
            self._note(f"result {result_id}: verdict dropped ({error})")
            return True
        if verdict.passed:
            self._fault("after_task_completed", "attempt")
            self._note(f"result {result_id} PASS → task {completed.id} COMPLETED")
            for sibling in self.store.list_attempts(task.id):
                if sibling.status is AttemptStatus.SUPERSEDED:
                    await self._release_attempt(sibling.id, cancel=True)
            live = self.store.get_mission(mission.id)
            if (
                stored.envelope.proposed_tasks
                and completed.status is TaskStatus.COMPLETED
                and live is not None
                and live.status is MissionStatus.ACTIVE  # a finished Mission has no plan to amend
            ):
                # S5-01 / D5-5: a Worker's proposed_tasks reach the graph only via the Manager
                await self._request_management(
                    mission,
                    completed,
                    trigger=f"proposed:{result_id}",
                    result_id=result_id,
                    attempt_id=attempt.id,
                )
        else:
            self._note(f"result {result_id} FAIL at {verdict.short_circuited_at}")
            undeployed = [
                layer.layer
                for layer in verdict.layers
                if layer.status == "ERROR" and layer.detail.get("undeployed")
            ]
            if undeployed:
                # D6-9': a required verifier that is not deployed blocks — no retry can make
                # it appear, and a missing layer is never a PASS
                self.commit.stop_task(
                    task.id,
                    stop_reason=MissionStopReason.VERIFIER_UNAVAILABLE,
                    detail={"layers": undeployed, "result_id": result_id},
                )
                await self._release_mission(mission.id)
                self._note(f"task {task.id} stopped: verifier(s) {undeployed} not deployed")
                return True
            failures = self.commit.no_progress_count(task.id)
            after = self.store.get_task(task.id)
            # a Task that can still retry and keeps failing is a stall (§19.2); one that
            # just spent its last attempt is stopped by the next _decide (max_attempts)
            can_retry = (
                after is not None
                and after.status
                not in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED}
                and (
                    after.budget.max_attempts is None
                    or after.attempt_count < after.budget.max_attempts
                )
            )
            manager_after = int(self.policy_for(mission.id)["manager_after_failures"])
            if can_retry and failures >= manager_after:
                # D5-6: repeated verification failures are a stall signal (§19.2)
                await self._request_management(
                    mission,
                    task,
                    trigger=f"failures:{task.id}:{failures}",
                    result_id=result_id,
                    attempt_id=attempt.id,
                )
        return True

    def _critic_provenance(self, mission_id: str, attempt_id: str) -> dict[str, str]:
        """The one parsed Critic verdict's durable intent, not a current template.

        Invalid verdicts settle FAILED; the successful ordinal settles SETTLED
        before returning its verdict. This survives a crash before layer recording
        and also interprets legacy layers that did not record an intent id.
        Missing or ambiguous execution identity is never proof of a prompt version.
        """

        settled = []
        for ordinal in range(1, MAX_CRITIC_ATTEMPTS + 1):
            intent = self.store.get_intent_for_subject(f"{attempt_id}:critic:{ordinal}")
            if intent is not None and intent.state == "SETTLED":
                settled.append(intent)
        if len(settled) != 1:
            return {}
        intent = settled[0]
        version = intent.config.get("prompt_version")
        if (
            intent.kind != "critic"
            or intent.mission_id != mission_id
            or intent.config.get("attempt_id") != attempt_id
            or not isinstance(version, str)
            or not version
        ):
            return {}
        return {"critic_intent_id": intent.intent_id, "verifier_version": version}

    def _human_inputs(
        self, result_id: str, task: Task
    ) -> tuple[dict[str, Any] | None, dict[str, LayerResult] | None, bool]:
        """D7-8': a person's answer to this result's review (if any), the recorded layers
        a resumed verification may reuse, and whether this Task may still escalate."""

        request = self.store.get_approval(review_request_id(result_id))
        human: dict[str, Any] | None = None
        reuse: dict[str, LayerResult] | None = None
        if request is not None and request["state"] in {"GRANTED", "REJECTED"}:
            human = {
                "verdict": "PASS" if request["state"] == "GRANTED" else "FAIL",
                "note": request.get("note", ""),
                "principal": request.get("decided_by"),
                "request_id": request["request_id"],
            }
            stored = self.store.get_result(result_id)
            provenance = (
                self._critic_provenance(task.mission_id, stored.envelope.attempt_id)
                if stored is not None
                and stored.envelope.task_id == task.id
                and stored.envelope.mission_id == task.mission_id
                else {}
            )
            rows = []
            for row in self.store.list_verifications(result_id):
                if row["layer"] == "critic_review":
                    detail = row.get("detail") or {}
                    if (
                        not provenance
                        or detail.get("verifier_version") != provenance["verifier_version"]
                        or (
                            "critic_intent_id" in detail
                            and detail["critic_intent_id"] != provenance["critic_intent_id"]
                        )
                    ):
                        continue
                rows.append(row)
            reuse = reusable_layers(
                rows,
                versions={"critic_review": provenance["verifier_version"]} if provenance else {},
                default_version=VERIFIER_VERSION,
            )
        escalated_before = any(
            r["kind"] == "review"
            and r.get("reason") == "needs_human"
            and r.get("task_id") == task.id
            and r["subject_key"] != result_id
            for r in self.store.list_approvals(task.mission_id)
        )
        return human, reuse, not escalated_before

    def _arbitrate_conflict(self, mission: Mission, task: Task, detail: Mapping[str, Any]) -> bool:
        """D7-8' kind ①: a Conflict Task that used its attempts without settling the
        contradiction goes to a person instead of failing the Mission."""

        conflict_id = str(task.context.get("conflict_id"))
        conflict = self.store.get_conflict(conflict_id) or {}
        sides = list(conflict.get("sides") or task.context.get("sides") or [])
        options = [f"keep:{side['claim_id']}" for side in sides] + ["unresolved"]
        _request, created = self.commit.request_arbitration(
            mission.id,
            subject=conflict_id,
            topic="conflict",
            options=options,
            context={"key": task.context.get("key"), "sides": sides, "attempts": dict(detail)},
            task_id=task.id,
        )
        if created:
            self._note(f"task {task.id}: conflict {conflict_id} goes to a person (arbitration)")
        return created

    def _arbitrated(
        self,
        mission: Mission,
        tasks: Sequence[Task],
        key: str,
        judgments: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]] | None, bool]:
        """D7-8' kind ②: when the independent judge Critic disagrees with the Tasks' own
        Critics on a criterion everything else says is met, a person rules.  Returns the
        judgments to use (``None`` while waiting) and whether a request was just opened."""

        opinions: dict[
            str, list[bool]
        ] = {}  # review P2-7: what each Task Critic said, per criterion
        for task in tasks:
            for layer in self.store.list_verifications(task.accepted_result_id or ""):
                if layer["layer"] != "critic_review" or layer["status"] != "PASS":
                    continue
                for item in (layer.get("detail") or {}).get("mission_criteria", []):
                    opinions.setdefault(str(item.get("criterion")), []).append(
                        bool(item.get("met"))
                    )
        contested = judgment_conflict(judgments, task_opinions=opinions)
        if not contested:
            return [dict(j) for j in judgments], False
        subject = f"{mission.id}:judgment:{key}"
        request = self.store.get_approval(arbitration_request_id(subject))
        if request is None:
            self.commit.request_arbitration(
                mission.id,
                subject=subject,
                topic="judgment",
                options=("met", "unmet"),
                context={"criteria": contested, "judgments": [dict(j) for j in judgments]},
            )
            self._note(f"mission {mission.id}: Verifiers disagree on {contested} → arbitration")
            return None, True
        if request["state"] != "GRANTED":
            return None, False
        ruling = str(request.get("ruling"))
        return [
            {
                **dict(j),
                "met": ruling == "met",
                "judge": "human_arbitration",
                "reason": f"arbitrated by {request.get('decided_by')}: {request.get('basis')}",
                "override_id": request.get("override_id"),
            }
            if j.get("criterion") in contested
            else dict(j)
            for j in judgments
        ], False

    def _action_problems(
        self, mission: Mission, task: Task, artifacts: Sequence[Artifact], copy: Any
    ) -> list[str] | None:
        """D7-2'': a result carrying ``actions/*.json`` is always checked for them — schema,
        deployment policy, the Mission's action scope and the Task's declared outputs —
        whatever the Task's verification policy says.  ``None`` = no candidate at all."""

        from .action_commits import allowed_actions

        paths = [artifact.path for artifact in artifacts if is_action_path(artifact.path)]
        criteria = (
            [c for c in task.success_criteria if c.startswith("action:")]
            if self.commit.domain_for(mission.id).id == "doc-research-v1"
            else []
        )
        if not paths and not criteria:
            return None
        problems: list[str] = []
        checked: set[tuple[str, str, str]] = set()
        for path in paths:
            if path not in task.outputs:
                problems.append(f"action candidate {path} is not a declared output of this Task")
                continue
            try:
                candidate = json.loads(copy.resolve(path).read_text(encoding="utf-8"))
                candidate, _ = check_candidate(
                    candidate,
                    criteria=mission.success_criteria,
                    connectors=self._connectors,
                    deployment=self._config.deployment_policy,
                )
                checked.add((candidate["connector"], candidate["operation"], candidate["target"]))
            except CandidateRejected as error:
                problems.append(f"action candidate {path} rejected ({error.reason}): {error}")
            except Exception as error:  # noqa: BLE001 - unreadable or not JSON
                problems.append(f"action candidate {path} unreadable: {error}")
        for criterion in criteria:
            if not allowed_actions([criterion], self._connectors).intersection(checked):
                problems.append(f"action criterion {criterion!r} has no checked matching candidate")
        return problems

    def _hold_lease(self, attempt_id: str) -> Attempt:
        """Renew this owner's lease during a long verification; ``CommitRejected`` when
        another owner took the Attempt over after a lapse (P1-3)."""

        attempt = self.store.get_attempt(attempt_id)
        assert attempt is not None
        return self.commit.renew_lease(
            attempt.id,
            owner=self._owner,
            lease_seconds=self._config.lease_seconds,
            liveness={"progress": attempt.progress_marker, "phase": "verifying"},
        )

    def _protected_seed(self, mission: Mission, task: Task) -> dict[str, str]:
        """Seed files the Worker may not rewrite: pytest targets and anything under tests/ (P0-1)."""

        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        targets = [
            c.removeprefix("pytest:").strip()
            for c in (*task.success_criteria, *mission.success_criteria)
            if c.startswith("pytest:")
        ]
        protected = {}
        for path, content in seed.items():
            if path.startswith("tests/") or any(
                path == target or (target and path.startswith(target.rstrip("/") + "/"))
                for target in targets
            ):
                protected[path] = content
        return protected

    def _protected_files(
        self, mission: Mission, task: Task, attempt: Attempt
    ) -> dict[str, str | bytes]:
        """Protected seed files plus every upstream input the Task did not declare as
        one of its ``outputs`` (D3-7': a downstream Worker may not silently rewrite what
        its dependencies delivered; rewriting an undeclared path needs a new Task)."""

        protected: dict[str, str | bytes] = dict(self._protected_seed(mission, task))
        declared = set(task.outputs)
        for item in self._upstream_inputs(attempt):
            if item.path in declared:  # the Task declared it will rewrite this path
                continue
            artifact = self.store.get_artifact(item.artifact_id)
            try:  # P3.2 D3: the stored bytes, hash re-checked
                if artifact is None:
                    raise ArtifactStoreError("missing", item.path)
                raw = read_verified(artifact)
            except ArtifactStoreError as error:
                raise ArtifactConflict(
                    f"upstream artifact {item.artifact_id} ({item.path}) is missing"
                ) from error
            try:
                protected[item.path] = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ArtifactConflict(f"upstream artifact {item.path} is not text") from error
        # Source registration never grants a Worker permission to change source bytes,
        # even when it declares that path as an output.
        protected.update(self._source_files(attempt))
        return protected

    # ------------------------------------------------------- management (step 5)
    def _tasks_under_management(self, mission_id: str) -> set[str]:
        return {
            str(intent.config.get("task_id"))
            for intent in self.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            )
            if intent.kind == "manager"
            and intent.mission_id == mission_id
            and intent.config.get("task_id")
        }

    def _manager_rounds(self, mission_id: str) -> int:
        return sum(1 for e in self.store.list_events(mission_id) if e.type == "ManagementRequested")

    def _change_rejections(self, mission_id: str, trigger: str) -> list[dict[str, Any]]:
        return [
            {"reason": e.payload.get("reason"), "detail": e.payload.get("detail")}
            for e in self.store.list_events(mission_id)
            if e.type == "TaskGraphChangeRejected"
            and str(e.payload.get("basis", {}).get("trigger", "")).split(":retry-")[0]
            == trigger.split(":retry-")[0]
        ]

    def _affected_subgraph(self, task: Task, tasks: Sequence[Task]) -> list[dict[str, Any]]:
        by_id = {t.id: t for t in tasks}
        related = {task.id} | set(task.dependency_ids)
        related |= {t.id for t in tasks if task.id in t.dependency_ids}
        for dep in task.dependency_ids:  # siblings under the same dependency
            related |= {t.id for t in tasks if dep in t.dependency_ids}
        view = []
        for tid in sorted(related, key=lambda x: by_id[x].id):
            t = by_id[tid]
            view.append(
                {
                    "task_id": t.id,
                    "kind": t.kind,
                    "goal": t.goal,
                    "status": str(t.status),
                    "dependencies": list(t.dependency_ids),
                    "attempts": t.attempt_count,
                    "paused": t.paused,
                    "role": t.context.get("role", "worker"),
                    "supersedes_task": t.context.get("supersedes_task"),
                }
            )
        return view

    async def _request_management(
        self,
        mission: Mission,
        task: Task,
        *,
        trigger: str,
        result_id: str | None,
        attempt_id: str | None,
    ) -> DispatchIntent | None:
        """One durable, deduplicated management decision per trigger (D5-6 / S5-07)."""

        if not self._config.dynamic_graph:  # D5-15: the layer's kill switch
            self._note(f"dynamic graph disabled: no management for {task.id} ({trigger})")
            return None
        subject = f"{mission.id}:manager:{trigger}"
        existing = self.store.get_intent_for_subject(subject)
        if existing is not None:
            return existing
        rounds = self._manager_rounds(mission.id)
        bound = self.policy_for(mission.id)  # step 9 (plan D9-4'): the Mission's version
        rounds_cap = int(bound["max_manager_rounds"])
        if rounds >= rounds_cap:
            self.commit.stop_task(
                task.id,
                stop_reason=MissionStopReason.MANAGEMENT_EXHAUSTED,
                detail={
                    "rounds": rounds,
                    "max_manager_rounds": rounds_cap,
                    "trigger": trigger,
                },
            )
            await self._release_mission(mission.id)
            self._note(f"task {task.id}: management rounds exhausted ({rounds})")
            return None
        mission = self.store.get_mission(mission.id) or mission
        tasks = self.store.list_tasks(mission.id)
        task = self.store.get_task(task.id) or task
        report = dict(mission.final_report or {})
        stored = self.store.get_result(result_id) if result_id else None
        feedback: list[dict[str, Any]] = []
        for attempt in self.store.list_attempts(task.id):
            failure = attempt.failure or {}
            if failure.get("reason") == "verification_failed":
                feedback.extend(
                    dict(item) for item in failure.get("failures", []) if isinstance(item, Mapping)
                )
        no_progress = self.commit.no_progress_count(task.id)
        with self.store.transaction():
            account = self.commit.ledger.account(mission_account(mission.id))
        trigger_view: dict[str, Any] = {
            "trigger": trigger,
            "result_id": result_id,
            "attempt_id": attempt_id,
            "task_id": task.id,
        }
        if stored is not None:
            trigger_view.update(
                {
                    "outcome": str(stored.envelope.outcome),
                    "summary": stored.envelope.summary,
                    "proposed_tasks": [dict(item) for item in stored.envelope.proposed_tasks],
                    "risks": list(stored.envelope.risks),
                }
            )
        limits = {
            "max_graph_depth": self._config.max_graph_depth,
            "max_proposals_per_agent": self._config.max_proposals_per_agent,
            "max_supersede_chain": self._config.max_supersede_chain,
            "mission_tokens_remaining": account.remaining_tokens(),
            "task_attempts_remaining": None
            if task.budget.max_attempts is None
            else max(0, task.budget.max_attempts - task.attempt_count),
            "no_progress_count": no_progress,
            "no_progress_limit": int(bound["no_progress_limit"]),
            "management_rounds_remaining": rounds_cap - rounds,
        }
        try:
            knowledge = self._gather_knowledge(mission, task, {t.id: t for t in tasks})
        except RetrievalUnavailable as error:
            knowledge = KnowledgeContext.unavailable(str(error))
        try:
            package = build_manager_package(
                mission,
                task,
                trigger=trigger_view,
                verifier_feedback=feedback[-6:],
                subgraph=self._affected_subgraph(task, tasks),
                graph_version=int(report.get("graph_version") or 1),
                limits=limits,
                knowledge=knowledge,
                rejections=self._change_rejections(mission.id, trigger),
                deployed_layers=self._deployed,
                budget_floor=self._budget_floor(mission.id),
                domain=self.commit.domain_for(mission.id),
            )
        except ContextRejected as error:
            self._note(f"task {task.id}: manager package refused ({error}); management postponed")
            return None
        try:
            decision = self._route_service("manager", mission.id)
        except RoutingUnavailable as unavailable:
            # review P0-1: the decision is postponed; the Task keeps its own retries meanwhile
            self._note(
                f"task {task.id}: manager pool {unavailable.profile_id!r} unavailable; management postponed"
            )
            return None
        template = self._template(MANAGER, mission.id)
        config = AgentConfig(
            name=f"manager-{rounds + 1}",
            instructions=template.instructions,
            model_profile_ref=decision.profile_id,
            tool_names=(),
            limits=AgentLimits(
                max_model_calls_per_turn=4,
                max_tool_calls_per_turn=1,
                turn_deadline_seconds=self._config.turn_deadline_seconds,
            ),
        )
        message = user_message_json(package.text)
        intent = self.commit.create_service_intent(
            kind="manager",
            subject_id=subject,
            mission_id=mission.id,
            account_id=mission_account(mission.id),
            creation_key=subject,
            input_id="attempt-input",
            input_hash=sha256_hex(message),
            config={
                "agent_config": config.to_json(),
                "message": message,
                "context_version": package.context_version,
                "prompt_version": template.prompt_version,
                "task_id": task.id,
                "trigger": trigger,
                "result_id": result_id,
                "attempt_id": attempt_id,
                "graph_version": int(report.get("graph_version") or 1),
                "no_progress_count": no_progress,
                **self._service_config(decision),
            },
            reservation=self._reservation(self._config.manager_reserve_tokens, decision.profile_id),
            task_id=task.id,
            attempt_id=attempt_id,
        )
        self.commit.record_management_requested(
            mission.id, task_id=task.id, trigger=trigger, subject=subject, round_number=rounds + 1
        )
        self._note(f"management requested for {task.id} ({trigger})")
        return intent

    async def _manager_unusable(self, intent: DispatchIntent, *, reason: str) -> None:
        """No usable proposal from the Manager: the Task continues its own retry path
        unless it is out of progress (D5-7)."""

        task = self.store.get_task(str(intent.config.get("task_id")))
        mission = self.store.get_mission(intent.mission_id)
        if task is None or mission is None:
            return
        self.commit.record_management_decided(
            mission.id,
            task_id=task.id,
            trigger=str(intent.config.get("trigger")),
            decision="unusable",
            detail={"reason": reason},
        )
        await self._enforce_no_progress(mission, task)

    async def _enforce_no_progress(self, mission: Mission, task: Task) -> bool:
        task = self.store.get_task(task.id) or task
        if task.status in TERMINAL_TASK:
            return False
        count = self.commit.no_progress_count(task.id)
        limit = int(self.policy_for(mission.id)["no_progress_limit"])
        if count >= limit:
            self.commit.stop_task(
                task.id,
                stop_reason=MissionStopReason.NO_PROGRESS,
                detail={
                    "no_progress_count": count,
                    "no_progress_limit": limit,
                },
            )
            await self._release_mission(mission.id)
            self._note(
                f"task {task.id} stopped: no progress after {count} attempts and no change of approach"
            )
            return True
        return False

    async def _collect_manager(self, intent: DispatchIntent, result) -> None:  # type: ignore[no-untyped-def]
        mission = self.store.get_mission(intent.mission_id)
        assert mission is not None
        self._import_usage(intent)
        task_id = str(intent.config.get("task_id"))
        trigger = str(intent.config.get("trigger"))
        text = "" if result.public_output is None else str(result.public_output.content)
        echoed = self.bridge_for(intent).echoed_models(agent_id=intent.agent_id or "")
        if echoed and echoed != {self._expected_model(intent)}:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self.commit.stop_task(
                task_id,
                stop_reason=MissionStopReason.MODEL_ECHO_MISMATCH,
                detail={"expected": self._expected_model(intent), "echoed": sorted(echoed)},
            )
            await self._release_mission(mission.id)
            return
        try:
            if result.state is not AgentTurnState.COMMITTED:
                raise ContractError(f"manager turn failed: {jsonable(result.error or {})}")
            raw = extract_block(text, GRAPH_CHANGE_PROPOSAL_TAG)
            raw = {
                **{
                    k: v
                    for k, v in raw.items()
                    if k in {"base_graph_version", "rationale", "operations"}
                },
                "basis": {  # the system fills the basis; a model may not forge it
                    "trigger": trigger,
                    "result_id": intent.config.get("result_id"),
                    "attempt_id": intent.config.get("attempt_id"),
                    "task_id": task_id,
                },
            }
            self._refuse_policy_ops(mission.id, task_id, raw.get("operations"))  # D9-10'
            change = TaskGraphChange.from_json(raw)
        except (ContractError, BlockError) as error:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self._note(f"manager proposal unusable for {task_id}: {error}")
            await self._manager_unusable(intent, reason=f"proposal_unreadable: {error}")
            return
        # P1-3 (review): the intent is settled only after the decision is durable — a crash
        # before the Commit re-collects the same turn and the receipt makes it idempotent
        self._fault("before_graph_change", "manager")
        task = self.store.get_task(task_id)
        assert task is not None
        limits = ChangeLimits(
            max_graph_depth=self._config.max_graph_depth,
            max_proposals_per_agent=self._config.max_proposals_per_agent,
            max_supersede_chain=self._config.max_supersede_chain,
            admit_new_tasks=not self._pressure.is_raised,  # §18.5 "禁止新任务继续分裂" (D6-3 ③)
        )
        no_progress = int(intent.config.get("no_progress_count", 0))
        if not change.operations or all(op.op == "set_priority" for op in change.operations):
            # priority-only (or empty) proposals are "keep" (S5-02); validation happens inside
            # the Commit transaction, so the test is on the operations themselves
            self._settle_intent(intent, "SETTLED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self.commit.record_management_decided(
                mission.id,
                task_id=task_id,
                trigger=trigger,
                decision="keep",
                detail={"rationale": change.rationale},
            )
            if no_progress >= int(self.policy_for(mission.id)["no_progress_limit"]):
                await self._enforce_no_progress(mission, task)
            elif change.operations:
                try:
                    self.commit.commit_graph_change(
                        mission.id,
                        change,
                        source={"intent_id": intent.intent_id, "agent_id": intent.agent_id},
                        limits=limits,
                    )
                except CommitRejected as error:
                    self._note(f"manager priority change refused: {error}")
            return
        try:
            created, receipt = self.commit.commit_graph_change(
                mission.id,
                change,
                source={
                    "intent_id": intent.intent_id,
                    "agent_id": intent.agent_id,
                    "turn_id": result.turn_id,
                },
                limits=limits,
            )
        except CommitRejected as error:
            self._settle_intent(intent, "SETTLED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self._note(f"manager change rejected for {task_id}: {error}")
            self.commit.record_management_decided(
                mission.id,
                task_id=task_id,
                trigger=trigger,
                decision="rejected",
                detail={"error": str(error)},
            )
            retry = 0 if ":retry-" not in trigger else int(trigger.rsplit("-", 1)[1])
            if retry < 1:  # D5-10 / S5-08: once more, with the rejection in the package
                await self._request_management(
                    mission,
                    task,
                    trigger=f"{trigger}:retry-{retry + 1}",
                    result_id=intent.config.get("result_id"),
                    attempt_id=intent.config.get("attempt_id"),
                )
            else:
                await self._enforce_no_progress(mission, task)
            return
        self._settle_intent(intent, "SETTLED")
        self._settle_service_if_known(intent.subject_id, mission.id)
        self.commit.record_management_decided(
            mission.id,
            task_id=task_id,
            trigger=trigger,
            decision="changed",
            detail={
                "change_id": receipt["change_id"],
                "to_version": receipt["to_version"],
                "new_tasks": [t.id for t in created],
                "superseded": receipt["superseded"],
            },
        )
        for old_id in receipt["superseded"]:
            for attempt in self.store.list_attempts(old_id):
                if attempt.status is AttemptStatus.CANCELLED:
                    await self._release_attempt(attempt.id, cancel=True)
        self._note(
            f"graph changed v{receipt['from_version']}→v{receipt['to_version']} for {task_id}: {[t.id for t in created]} superseded={receipt['superseded']}"
        )

    async def _run_critic(
        self,
        mission: Mission,
        task: Task | None,
        *,
        view_id: str,
        subject_prefix: str,
        account_id: str,
        artifacts: Sequence[Artifact],
        test_output: str | None,
        attempt_id: str | None = None,
    ) -> CriticVerdict:
        copy = self.assembled.workspaces.verification_view(view_id)
        source_attempt = None if attempt_id is None else self.store.get_attempt(attempt_id)
        source_binding = (
            {} if source_attempt is None else self._frozen_source_binding(source_attempt)
        )
        untrusted = [str(p) for p in (mission.final_report or {}).get("untrusted_sources", [])]
        if source_binding:
            untrusted = sorted(set(untrusted) | set(source_binding.get("source_versions", {})))
        try:
            package = build_critic_package(
                mission,
                task,
                attempt_id=view_id,
                artifacts=[
                    {"path": a.path, "content_hash": a.content_hash, "size_bytes": a.size_bytes}
                    for a in artifacts
                ],
                test_output=test_output,
                workspace_files=copy.list_files(),
                knowledge=self._knowledge_or_unavailable(mission, task),
                visibility="critic" if task is not None and task.kind == "conflict" else "verifier",
                domain=self.commit.domain_for(mission.id),
                source_versions=source_binding.get("source_versions"),
            )
        except ContextRejected as error:
            raise ContractError(f"critic package refused: {error}") from error
        task_id = None if task is None else task.id
        last_error: ContractError | None = None
        for ordinal in range(1, MAX_CRITIC_ATTEMPTS + 1):
            subject = f"{subject_prefix}:{ordinal}"
            try:
                decision = self._route_service("critic", mission.id)
            except RoutingUnavailable as unavailable:
                # review P0-1: an unavailable Critic makes the layer an ERROR (never a PASS)
                raise ContractError(
                    f"critic runtime profile {unavailable.profile_id!r} unavailable"
                ) from unavailable
            template = self._template(CRITIC, mission.id)
            config = AgentConfig(
                name=f"critic-{ordinal}",
                instructions=template.instructions,
                model_profile_ref=decision.profile_id,
                tool_names=template.tool_names,
                limits=AgentLimits(
                    max_model_calls_per_turn=12,
                    max_tool_calls_per_turn=24,
                    turn_deadline_seconds=self._config.turn_deadline_seconds,
                ),
            )
            message = user_message_json(package.text)
            intent = self.commit.create_service_intent(
                kind="critic",
                subject_id=subject,
                mission_id=mission.id,
                account_id=account_id,
                creation_key=subject,
                input_id="attempt-input",
                input_hash=sha256_hex(message),
                config={
                    "agent_config": config.to_json(),
                    "message": message,
                    "attempt_id": view_id,
                    "context_version": package.context_version,
                    "prompt_version": template.prompt_version,
                    "untrusted_sources": untrusted,
                    **source_binding,
                    **self._service_config(decision),
                },
                reservation=self._reservation(
                    self._config.critic_reserve_tokens, decision.profile_id
                ),
                task_id=task_id,
                attempt_id=attempt_id,
            )
            deadline = self.store.now + self._critic_wait
            while intent.state in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
                if not await self._dispatch(intent):  # another owner holds the claim (P1-8)
                    if self.store.now >= deadline:
                        raise ContractError("critic intent is claimed elsewhere; wait window over")
                    await asyncio.sleep(self._poll)
                refreshed = self.store.get_intent(intent.intent_id)
                assert refreshed is not None
                intent = refreshed
            assert intent.agent_id and intent.expected_turn_id
            result = None
            while self.store.now < deadline:
                result = await self.bridge_for(intent).result(
                    agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                )
                if result is not None:
                    break
                if attempt_id is not None:
                    self._hold_lease(attempt_id)  # P1-3: keep the lease while the Critic thinks
                await asyncio.sleep(self._poll)
            self._import_usage(intent)
            self.assembled.gateway.unbind(intent.agent_id)
            try:
                if result is None:
                    raise ContractError("critic did not answer within the wait window")
                self._note_turn_health(intent, result)  # review P2-2: Critic turns count too
                echoed = self.bridge_for(intent).echoed_models(agent_id=intent.agent_id)
                if echoed and echoed != {self._expected_model(intent)}:
                    raise ContractError(f"critic model echo mismatch: {sorted(echoed)}")
                if result.state is not AgentTurnState.COMMITTED:
                    raise ContractError(f"critic turn failed: {dict(result.error or {})}")
                text = "" if result.public_output is None else str(result.public_output.content)
                verdict = parse_critic_verdict(text, expected_criteria=mission.success_criteria)
            except ContractError as error:
                last_error = error
                self._settle_intent(intent, "FAILED")
                self._settle_service_if_known(subject, mission.id, task_id)
                continue
            self._settle_intent(intent, "SETTLED")
            self._settle_service_if_known(subject, mission.id, task_id)
            return verdict
        assert last_error is not None
        raise last_error

    # --------------------------------------------------------------- decide
    async def _defer_for_profile(
        self, mission: Mission, task: Task, unavailable: RoutingUnavailable
    ) -> bool:
        """S6-06 (D6-5'): the profile the Task needs is cooling down and has no fallback —
        the Task waits visibly (no Attempt, the loop stays alive) for at most
        ``profile_wait_seconds`` measured on the store clock, then stops explicitly."""

        now = self.store.now
        since = self._deferred.get(task.id)
        if since is None:
            self._deferred[task.id] = now
            self._note(
                f"task {task.id}: waiting for runtime profile {unavailable.profile_id!r} "
                f"(unavailable until {unavailable.until})"
            )
            return False
        if now - since < self._config.profile_wait_seconds:
            return False
        self._deferred.pop(task.id, None)
        self.commit.stop_task(
            task.id,
            stop_reason=MissionStopReason.RUNTIME_UNAVAILABLE,
            detail={
                "profile_id": unavailable.profile_id,
                "waited_seconds": round(now - since, 3),
                "profile_wait_seconds": self._config.profile_wait_seconds,
                "unavailable_until": unavailable.until,
            },
        )
        await self._release_mission(mission.id)
        self._note(
            f"task {task.id} stopped: runtime profile {unavailable.profile_id!r} unavailable "
            f"for {now - since:.1f}s"
        )
        return True

    def _observe_pressure(self, active: set[str]) -> None:
        """D6-2: one watermark evaluation per cycle, before any allocation."""

        running = self.store.count_attempts_by_status(
            str(AttemptStatus.CLAIMED), str(AttemptStatus.RUNNING)
        )
        pending_dispatch = sum(
            1
            for intent in self.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED")
            if intent.kind == "attempt"
            and intent.mission_id in active
            and self.profile_of(intent) in self.assembled.pools  # review P1-4
        )
        pending_verifications = sum(
            1
            for stored in self.store.list_results_by_verification("PENDING", "RUNNING")
            if stored.envelope.mission_id in active
        )
        observation = Observation(
            running_attempts=running,
            pending_dispatch=pending_dispatch,
            pending_verifications=pending_verifications,
            observed_at=self.store.now,
        )
        state, transitions = self.commit.record_backpressure(
            observation, limits=self._config.backpressure_limits(), mission_ids=sorted(active)
        )
        self._pressure = state
        for transition in transitions:
            self._note(
                f"backpressure {transition.to_level.lower()} on {transition.dimension}: "
                f"{transition.observed} (high {transition.high} / low {transition.low})"
            )

    @property
    def pressure(self) -> BackpressureState:
        return self._pressure

    def _tool_call_room(self, mission: Mission, task: Task) -> tuple[int | None, int | None]:
        """(reservable now, not yet spent) on the Task → Mission → Global chain."""

        accounts = [task_account(task.id), mission_account(mission.id)]
        if self._config.global_budget is not None:
            accounts.append(GLOBAL_ACCOUNT)
        reservable: int | None = None
        spent_room: int | None = None
        with self.store.transaction():
            for account_id in accounts:
                try:
                    snap = self.commit.ledger.account(account_id)
                except BudgetError:
                    continue
                limit = snap.limits.max_tool_calls
                if limit is None:
                    continue
                room = limit - snap.reserved_tool_calls - snap.settled_tool_calls
                left = limit - snap.settled_tool_calls
                reservable = room if reservable is None else min(reservable, room)
                spent_room = left if spent_room is None else min(spent_room, left)
        return reservable, spent_room

    def _tool_calls_limited(self, mission: Mission, task: Task) -> bool:
        """Whether any account on the Task's chain caps tool calls (only then is a
        reservation meaningful; an unlimited dimension is never reserved)."""

        if task.budget.max_tool_calls is not None or mission.budget.max_tool_calls is not None:
            return True
        limits = self._config.global_budget
        return limits is not None and limits.max_tool_calls is not None

    async def _runtime_exhausted(self, mission: Mission, tasks: Sequence[Task]) -> bool:
        """D6-8 / §18.1 wall-clock dimension: a Mission (or Task) past ``max_runtime_seconds``
        gets no new allocation; what was spent and reserved stays on the books."""

        now = self.store.now
        limit = mission.budget.max_runtime_seconds
        waited = self.store.human_wait_seconds(
            mission.id, now
        )  # D7-7': a person's time is not run time
        if limit is not None and now - mission.created_at - waited >= limit:
            detail = {
                "dimension": "runtime",
                "elapsed_seconds": round(now - mission.created_at - waited, 3),
                "human_wait_seconds": round(waited, 3),
                "max_runtime_seconds": limit,
                "account": mission_account(mission.id),
            }
            self.commit.fail_mission(
                mission.id, stop_reason=MissionStopReason.BUDGET_EXHAUSTED, detail=detail
            )
            await self._release_mission(mission.id)
            self._note(f"mission {mission.id} stopped: budget_exhausted (runtime)")
            return True
        for task in tasks:
            cap = task.budget.max_runtime_seconds
            if cap is None or task.status in TERMINAL_TASK:
                continue
            attempts = self.store.list_attempts(task.id)
            if not attempts:
                continue
            started = min(a.created_at for a in attempts)
            if now - started >= cap and not any(a.status in OPEN_ATTEMPT_STATES for a in attempts):
                self.commit.stop_task(
                    task.id,
                    stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
                    detail={
                        "dimension": "runtime",
                        "elapsed_seconds": round(now - started, 3),
                        "max_runtime_seconds": cap,
                        "account": task_account(task.id),
                    },
                )
                await self._release_mission(mission.id)
                self._note(f"task {task.id} stopped: budget_exhausted (runtime)")
                return True
        return False

    async def _decide(self, mission: Mission) -> bool:
        tasks = self.store.list_tasks(mission.id)
        if not tasks or mission.status is not MissionStatus.ACTIVE:
            return False
        live = [  # D5-4 / R4: superseded work is history and a paused route is not required
            t
            for t in tasks
            if t.status is not TaskStatus.CANCELLED
            and not (t.paused and t.status in {TaskStatus.READY, TaskStatus.BLOCKED})
        ]
        if live and all(task.status is TaskStatus.COMPLETED for task in live):
            current = self.store.get_mission(mission.id)  # not the cycle's stale snapshot
            if current is None or current.status is not MissionStatus.ACTIVE:
                return False
            if any(c.startswith(ACTION_PREFIX) for c in current.success_criteria):
                return await self._decide_actions(current, live)  # D7-7' two-stage judgment
            return await self._judge(current, live)
        if await self._runtime_exhausted(mission, tasks):  # after the judge (review P2-9)
            return True
        if any(task.status is TaskStatus.FAILED for task in tasks):
            return False  # the stop cascade already ended the Mission
        attempts = [a for task in tasks for a in self.store.list_attempts(task.id)]
        open_conflicts = [
            c["conflict_id"] for c in self.store.list_conflicts(mission.id, state="OPEN")
        ]
        if open_conflicts:  # D4-8': an open conflict gates the synthesis Task (no state change)
            gated = [t for t in tasks if t.kind == "synthesis" and t.status is TaskStatus.READY]
            for task in gated:
                if self.commit.record_synthesis_gated(task.id, conflict_ids=open_conflicts):
                    self._note(f"synthesis task {task.id} gated by open conflicts {open_conflicts}")
            tasks = [t for t in tasks if t not in gated]
        pending = self._tasks_under_management(mission.id)
        if pending:  # D5-6: no new Attempt while the Manager decides about the Task
            tasks = [t for t in tasks if t.id not in pending]
        bound = self.policy_for(mission.id)  # step 9 (plan D9-4'): the Mission's own version
        plan = allocate(
            tasks,
            attempts,
            concurrency_limit=min(int(bound["mission_concurrency"]), self._config.max_concurrency),
            candidates_per_task=int(bound["candidates_per_task"]),
            now=self.store.now,
            aging_window_seconds=float(bound["aging_window_seconds"]),
            mission_max_tokens=mission.budget.max_tokens,
            pressure=self._pressure,  # D6-3: the gate outside the §29.3 formula
            reduced_concurrency_ratio=self._config.reduced_concurrency_ratio,
            exploration_slots=int(bound["exploration_slots"]),
            weights=bound["allocator_weights"],
        )
        progressed = False
        for granted, _candidate in plan.grants:
            current_task = self.store.get_task(granted.id)
            assert current_task is not None
            task = current_task
            if task.status in TERMINAL_TASK:
                continue
            score = plan.scores.get(task.id)
            if await self._next_attempt(
                mission,
                task,
                self.store.list_attempts(task.id),
                allocation=None
                if score is None
                else {
                    **score.to_json(),
                    "policy_version_id": self.policy_version_of(mission.id),
                    "candidates_per_task": int(bound["candidates_per_task"]),
                    "concurrency_limit": plan.concurrency_limit,
                    "eligible": plan.eligible,
                    "slots": plan.slots,
                    "pressure": plan.pressure,  # S9-08: the gate the grant was made under
                },
            ):
                progressed = True
            current = self.store.get_mission(mission.id)
            if current is None or current.status in TERMINAL_MISSION:
                break
        return progressed

    def _artifacts_by_task(self, tasks: Sequence[Task]) -> dict[str, list[Artifact]]:
        by_task: dict[str, list[Artifact]] = {}
        for task in tasks:
            found = []
            for artifact_id in task.accepted_artifacts:
                artifact = self.store.get_artifact(artifact_id)
                if artifact is not None:
                    found.append(artifact)
            by_task[task.id] = found
        return by_task

    async def _next_attempt(
        self,
        mission: Mission,
        task: Task,
        attempts: Sequence[Attempt],
        *,
        allocation: Mapping[str, Any] | None = None,
    ) -> bool:
        # a repair follows the last *failed* Attempt; a parallel candidate follows nobody
        previous = next(
            (a for a in reversed(attempts) if a.status in TERMINAL_ATTEMPT and a.failure), None
        )
        feedback: list[str] = []
        verifier_feedback: list[Mapping[str, Any]] = []
        if previous is not None and previous.failure is not None:
            failure = previous.failure
            reason = str(failure.get("reason"))
            if reason == "verification_failed":
                for item in failure.get("failures", []):
                    if isinstance(item, Mapping):
                        feedback.append(f"{item.get('layer')}: {item.get('summary')}")
                        verifier_feedback.append(dict(item))
            else:
                feedback.append(f"{reason}: {failure.get('error', '')}")
        for event in self.store.list_events(mission.id):  # D7-9': a person's notes, as data
            if event.type == "HumanCommentAdded" and event.payload.get("target_id") in {
                task.id,
                mission.id,
            }:
                feedback.append(
                    f"human note from {event.payload.get('principal_id')} "
                    f"(information, not a permission change): {event.payload.get('text')}"
                )
        # D3-7': the Attempt starts from every ancestor's accepted artifacts
        all_tasks = {t.id: t for t in self.store.list_tasks(mission.id)}
        upstream_tasks = ancestors(task.id, all_tasks)
        try:
            inputs = merge_accepted(
                upstream_tasks, self._artifacts_by_task(upstream_tasks), tasks_by_id=all_tasks
            )
        except ArtifactConflict as error:
            self.commit.stop_task(
                task.id,
                stop_reason=MissionStopReason.ARTIFACT_CONFLICT,
                detail={"error": str(error)},
            )
            await self._release_mission(mission.id)
            self._note(f"task {task.id} stopped: artifact conflict ({error})")
            return True
        bound = self.policy_for(mission.id)  # step 9 (plan D9-4'): the Mission's own version
        role = self._template(role_for_task(task), mission.id)  # D5-9: approach
        untrusted = [str(p) for p in (mission.final_report or {}).get("untrusted_sources", [])]
        try:
            knowledge = self._gather_knowledge(mission, task, all_tasks)
        except RetrievalUnavailable as error:
            # S4-07 / D4-11': never "no knowledge" — degrade explicitly or block visibly
            count = self.commit.record_retrieval_unavailable(
                task.id, reason=str(error), policy=self._config.on_retrieval_failure
            )
            if self._config.on_retrieval_failure == "degrade":
                knowledge = KnowledgeContext.unavailable(str(error))
                self._note(f"task {task.id}: retrieval unavailable, degraded ({error})")
            else:
                if count >= self._config.max_retrieval_failures:
                    self.commit.stop_task(
                        task.id,
                        stop_reason=MissionStopReason.RETRIEVAL_UNAVAILABLE,
                        detail={"failures": count, "reason": str(error)},
                    )
                    await self._release_mission(mission.id)
                    self._note(f"task {task.id} stopped: retrieval unavailable {count} times")
                else:
                    self._note(f"task {task.id}: retrieval unavailable, blocked ({count})")
                return True
        task_kind = str((mission.final_report or {}).get("task_kind") or "code")
        try:
            decision = self._router_for(mission.id).route(
                role=role.name,
                task_kind=task_kind,
                previous_attempts=attempts,
                unavailable_until=self.commit.unavailable_until(),
                now=self.store.now,
            )
        except RoutingUnavailable as unavailable:
            return await self._defer_for_profile(mission, task, unavailable)
        self._deferred.pop(task.id, None)
        placeholder = Attempt(
            id=ids.attempt_id(task.id, len(attempts) + 1),
            task_id=task.id,
            mission_id=mission.id,
            role=role.name,
            model=decision.model,
            prompt_version=role.prompt_version,
            context_version="pending",
            budget_reserved=task.budget,
            lease_owner=None,
            lease_expires_at=None,
            status=AttemptStatus.PENDING,
            retry_of=None if previous is None else previous.id,
            created_at=self.store.now,
            version=1,
            ordinal=len(attempts) + 1,
            creation_key="pending",
            input_id="attempt-input",
            task_version=task.version,
            feedback=tuple(feedback),
        )
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        source_binding = self._active_source_binding(mission.id)
        source_versions = source_binding.get("source_versions")
        source_paths = set(source_versions or {})
        if source_binding:
            untrusted = sorted(set(untrusted) | source_paths)
        previous_files = sorted({*seed, *(item.path for item in inputs), *source_paths})
        if previous is not None:
            try:
                previous_files = sorted(
                    set(self.assembled.workspaces.get(previous.id).list_files()) | source_paths
                )
            except Exception:  # noqa: BLE001
                pass
        if source_binding:
            previous_files = [
                path
                for path in previous_files
                if path in source_paths
                or not _under_source_root(path, source_binding["source_roots"])
            ]
        try:
            package = build_worker_package(
                mission,
                task,
                placeholder,
                previous_attempts=attempts,
                verifier_feedback=verifier_feedback,
                workspace_files=previous_files,
                dependencies=[
                    {
                        "task_id": dep.id,
                        "goal": dep.goal,
                        "status": str(dep.status),
                        "accepted_artifacts": [
                            item.to_json() for item in inputs if item.task_id == dep.id
                        ],
                    }
                    for dep in upstream_tasks
                    if dep.id in set(task.dependency_ids)
                ],
                knowledge=knowledge,
                untrusted_sources=untrusted,
                role=role.name,
                domain=self.commit.domain_for(mission.id),
                source_versions=source_versions,
            )
        except ContextRejected as error:
            self.commit.stop_task(
                task.id,
                stop_reason=MissionStopReason.CONTEXT_REJECTED,
                detail={"error": str(error)[:300]},
            )
            await self._release_mission(mission.id)
            self._note(f"task {task.id} stopped: worker package refused ({error})")
            return True
        # D6-7: Mission ∩ Task ∩ Role ∩ Deployment, frozen into the intent below
        allowed = effective_tools(
            mission_tools=mission.allowed_tools,
            task_tools=task.allowed_tools,
            role_tools=role.tool_names,
            deployment=self._config.deployment_policy,
        )
        # D6-8: the Attempt's tool-call cap = the deployment's per-turn cap, narrowed by the
        # Task budget's own dimension; it is reserved up front and enforced at the gateway
        tool_cap = self._config.max_tool_calls_per_turn
        if task.budget.max_tool_calls is not None:
            tool_cap = min(tool_cap, task.budget.max_tool_calls)
        if self._tool_calls_limited(mission, task):
            # review P1-2: never reserve more than the chain can still hold; in-flight
            # reservations are not spending — only a spent dimension is exhaustion
            reservable, spent_room = self._tool_call_room(mission, task)
            if spent_room is not None and spent_room > 0 and reservable is not None:
                if reservable <= 0:
                    self._note(f"task {task.id}: tool calls all reserved in flight; waiting")
                    return False
                tool_cap = min(tool_cap, reservable)
        config = AgentConfig(
            name=f"{role.name}-{placeholder.ordinal}",
            instructions=role.instructions,
            model_profile_ref=decision.profile_id,  # the label *is* the pool it runs in
            tool_names=allowed,
            limits=AgentLimits(
                max_model_calls_per_turn=self._config.max_model_calls_per_turn,
                max_tool_calls_per_turn=tool_cap,
                turn_deadline_seconds=min(
                    self._config.turn_deadline_seconds,
                    float(task.budget.max_runtime_seconds or self._config.turn_deadline_seconds),
                ),
            ),
        )
        message = user_message_json(package.text)
        tokens = self._config.attempt_reserve_tokens
        if self._pressure.is_raised:  # §18.5 "缩小每个 Attempt 预算" (D6-3 ④)
            tokens = max(4_000, int(tokens * self._config.reduced_reserve_ratio))
        if task.budget.max_tokens is not None:
            # D3-5': explorative candidates share the Task's token budget evenly
            tokens = min(
                tokens, max(1, task.budget.max_tokens // int(bound["candidates_per_task"]))
            )
            # step 4: a repair reserves what the Task still has rather than failing on a
            # nominal share it no longer can afford (the reservation is a cap, not a spend)
            with self.store.transaction():
                remaining = self.commit.ledger.account(task_account(task.id)).remaining_tokens()
            if remaining is not None:
                critic_share = (
                    self._config.critic_reserve_tokens
                    if "critic_review" in task.verification_policy
                    else 0
                )
                head_room = remaining - critic_share  # keep the Critic's own share free
                if 0 < head_room < tokens:
                    tokens = head_room
        try:
            attempt, _intent = self.commit.create_attempt(
                task.id,
                role=role.name,
                model=decision.model,
                prompt_version=role.prompt_version,
                context_version=package.context_version,
                reservation=replace(
                    self._reservation(tokens, decision.profile_id),
                    tool_calls=tool_cap if self._tool_calls_limited(mission, task) else 0,
                ),
                runtime_profile_id=decision.profile_id,
                routing=decision.to_json(),
                intent_config={
                    "agent_config": config.to_json(),
                    "message": message,
                    "attempt_id": placeholder.id,
                    "allowed_tools": list(allowed),
                    **self._service_config(decision),
                    "max_tool_calls": tool_cap,
                    "context_version": package.context_version,
                    "prompt_version": role.prompt_version,
                    "policy_version_id": self.policy_version_of(mission.id),
                    "task_version": task.version,
                    "role": role.name,
                    "knowledge": knowledge.frozen_ids,  # D4-10: what this Attempt saw
                    "retrieval_version": knowledge.retrieval.version,
                    "retrieval_status": knowledge.retrieval.status,
                    "context_builder_version": CONTEXT_BUILDER_VERSION,
                    "untrusted_sources": untrusted,
                    **source_binding,
                    "allocation": dict(
                        allocation or {}
                    ),  # D5-8': the §29.3 score it was granted on
                },
                input_hash=sha256_hex(message),
                retry_of=placeholder.retry_of,
                feedback=feedback,
                candidates_per_task=int(bound["candidates_per_task"]),
                inputs=[item.to_json() for item in inputs],
                max_open_attempts=min(
                    int(bound["mission_concurrency"]), self._config.max_concurrency
                ),
                max_running_attempts=self._config.max_running_attempts,
            )
        except CommitRejected as error:
            self._note(f"task {task.id}: no new attempt ({error})")
            return False
        except BudgetExhausted as error:
            detail = {
                "dimension": error.dimension,
                "requested": error.requested,
                "remaining": error.remaining,
                "account": error.account_id,
            }
            reason = (
                MissionStopReason.MAX_ATTEMPTS_REACHED
                if error.dimension == "attempts"
                else MissionStopReason.BUDGET_EXHAUSTED
            )
            if error.account_id == GLOBAL_ACCOUNT:
                # review P1-1 / D6-1': the deployment-wide pool ran out — no Task and no
                # Mission is to blame; the Mission stops with the Global scope named
                reason = MissionStopReason.BUDGET_EXHAUSTED
                self.commit.fail_mission(
                    mission.id, stop_reason=reason, detail={**detail, "scope": "global"}
                )
                self._note(
                    f"mission {mission.id} stopped: {reason} ({error.dimension}, global pool)"
                )
            elif error.account_id == mission_account(mission.id):
                # D3-12': the Mission pool itself is exhausted (any dimension) — no Task
                # is to blame and the stop reason is the pool's: budget_exhausted
                reason = MissionStopReason.BUDGET_EXHAUSTED
                self.commit.fail_mission(
                    mission.id, stop_reason=reason, detail={**detail, "scope": "mission"}
                )
                self._note(
                    f"mission {mission.id} stopped: {reason} ({error.dimension}, mission pool)"
                )
            else:
                if task.kind == "conflict" and reason is MissionStopReason.MAX_ATTEMPTS_REACHED:
                    return self._arbitrate_conflict(mission, task, detail)  # D7-8' ①
                self.commit.stop_task(task.id, stop_reason=reason, detail=detail)
                self._note(f"task {task.id} stopped: {reason} ({error.dimension})")
            await self._release_mission(mission.id)
            return True
        self._note(
            f"attempt {attempt.id} created (retry_of={attempt.retry_of}, inputs={len(inputs)})"
        )
        return True

    async def _judge(self, mission: Mission, tasks: Sequence[Task]) -> bool:
        key = judgment_key(tasks)
        cached = self.commit.criteria_judgment(mission.id, key)  # booked only for an arbitration
        evaluated = cached if cached is not None else await self._evaluate_criteria(mission, tasks)
        if evaluated is None:
            return True
        judgments, summary = evaluated
        ruled, created = self._arbitrated(mission, tasks, key, judgments)
        if ruled is None:  # D7-8' ②: a person rules first; the judgment is kept meanwhile
            if cached is None:
                self.commit.record_criteria_judgment(mission.id, key, judgments, summary=summary)
            return created or cached is None
        judged = self.commit.judge_mission(mission.id, judgments=ruled, summary=summary)
        self._note(f"mission {mission.id} judged: {judged.status} ({judged.stop_reason})")
        return True

    async def _evaluate_criteria(
        self, mission: Mission, tasks: Sequence[Task]
    ) -> tuple[list[dict[str, Any]], str] | None:
        """D21 / ORCH §12.4 / D3-9': judge the Mission's own success criteria on the
        *integrated* tree — the seed plus every Task's accepted artifacts applied in
        topological order — independently of the Task PASSes.  ``pytest:`` criteria run
        there, ``file:`` criteria are checked there and free-text criteria go to an
        independent Critic bound to that tree (reused from the single Task's own
        critic_review when the Mission has exactly one Task)."""

        self._reimport_unsettled(mission)
        all_tasks = {t.id: t for t in tasks}
        try:
            merged = merge_accepted(
                list(tasks), self._artifacts_by_task(tasks), tasks_by_id=all_tasks
            )
        except ArtifactConflict as error:
            self.commit.fail_mission(
                mission.id,
                stop_reason=MissionStopReason.ARTIFACT_CONFLICT,
                detail={"error": str(error)},
            )
            self._note(f"mission {mission.id} failed at judgment: {error}")
            return None
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        files: dict[str, Path | bytes] = {}
        artifacts: list[Artifact] = []
        try:
            for item in merged:
                artifact = self.store.get_artifact(item.artifact_id)
                if artifact is None:
                    continue
                files[item.path] = read_verified(artifact)  # P3.2 D3: hash re-checked
                artifacts.append(artifact)
        except ArtifactStoreError as error:
            self.commit.fail_mission(
                mission.id,
                stop_reason=MissionStopReason.ARTIFACT_CONFLICT,
                detail={"error": str(error)},
            )
            self._note(f"mission {mission.id} failed at judgment: {error}")
            return None
        # P0-2: one judgment tree per orchestrator instance — another instance may be
        # running pytest in its own; the judgment Commit itself is idempotent
        view_id = f"{mission.id}-judge-{self._owner}"
        copy = self.assembled.workspaces.integrated_copy(view_id, seed=seed, files=files)
        self._register_copy(
            "judge",
            f"{view_id}-verify",
            mission_id=mission.id,
            attempt_id="",
            detail={"artifacts": sorted(a.id for a in artifacts), "seed": sorted(seed)},
        )
        terminal = terminal_task(list(tasks))
        stored = self.store.get_result(terminal.accepted_result_id or "")
        summary = "" if stored is None else stored.envelope.summary
        test_runs: dict[str, dict[str, Any]] = {}
        for criterion in mission.success_criteria:
            if not criterion.startswith("pytest:"):
                continue
            target = criterion.removeprefix("pytest:").strip() or None
            if not self._config.deployment_policy.local_code_execution:
                # host support 0.9.8: a criterion from before the switch is judged unmet —
                # never run on this machine
                test_runs[criterion] = {
                    "passed": False,
                    "error": "local_code_execution is off in this deployment: pytest criteria are not run on this machine",
                    "stdout": "",
                }
                continue
            try:
                if target is not None:
                    copy.resolve(target)
                test_run = await run_pytest(
                    str(copy.root),
                    path=target,
                    timeout=self._config.test_timeout_seconds,
                    executor=self._executor,
                )
                test_runs[criterion] = {**test_run.to_json(), "passed": test_run.passed}
            except Exception as error:  # noqa: BLE001
                test_runs[criterion] = {"passed": False, "error": str(error), "stdout": ""}
        judge_ablated = "critic" in self._config.ablations  # step 8 (D8-7'): no judge Critic
        needs_critic = not judge_ablated and any(
            not c.startswith(("pytest:", "file:", ACTION_PREFIX)) for c in mission.success_criteria
        )
        critic: CriticVerdict | None = None
        reused_critic = False
        if needs_critic:
            if len(tasks) == 1 and stored is not None:
                critic = self._critic_verdicts.get(stored.envelope.id)
                reused_critic = critic is not None
            if critic is None:
                test_output = "\n".join(str(r.get("stdout", "")) for r in test_runs.values())
                try:
                    critic = await self._run_critic(
                        mission,
                        None,
                        view_id=view_id,
                        subject_prefix=f"{mission.id}:judge",
                        account_id=mission_account(mission.id),
                        artifacts=artifacts,
                        test_output=test_output or None,
                    )
                except (ContractError, BudgetExhausted) as error:
                    self._note(f"mission {mission.id}: independent judge unavailable ({error})")
        judgments: list[dict[str, Any]] = []
        for criterion in mission.success_criteria:
            if criterion.startswith("pytest:"):
                outcome = test_runs.get(criterion, {})
                judgments.append(
                    {
                        "criterion": criterion,
                        "met": bool(outcome.get("passed")),
                        "judge": "code_test",
                        "reason": (outcome.get("stdout") or outcome.get("error") or "")[-300:],
                    }
                )
            elif criterion.startswith("file:"):
                relative = criterion.removeprefix("file:")
                try:
                    met = copy.resolve(relative).is_file()
                except Exception:  # noqa: BLE001
                    met = False
                judgments.append(
                    {
                        "criterion": criterion,
                        "met": met,
                        "judge": "rule_check",
                        "reason": "file exists" if met else "file missing",
                    }
                )
            elif criterion.startswith(ACTION_PREFIX):
                continue  # D7-7': judged from the action ledger by the caller
            else:
                found: Mapping[str, Any] | None = None
                if critic is not None:
                    found = next(
                        (c for c in critic.mission_criteria if c.get("criterion") == criterion),
                        None,
                    )
                judgments.append(
                    {
                        "criterion": criterion,
                        "met": bool(found and found.get("met")),
                        "judge": "critic_review",
                        "source": "ablated"  # step 8: this run removed the judge Critic
                        if judge_ablated
                        else "unavailable"  # review P1-2: no judge ran — not a Verifier
                        if critic is None
                        else ("task_critic" if reused_critic else "independent"),
                        "reason": "judge ablated in this run"
                        if judge_ablated
                        else "no independent judge ran"
                        if found is None
                        else found.get("reason"),
                    }
                )
        return judgments, summary

    async def _decide_actions(self, mission: Mission, tasks: Sequence[Task]) -> bool:
        """D7-7' / D7-5': judgment in two stages.  ① The non-action criteria are judged once
        per integrated tree and put on the books.  ② Only then are the actions looked at: an
        approved (or L0/L1) action is handed off as the *last* step of the judgment, a
        rejected / revoked / expired one fails the Mission, and one that waits for a person
        leaves the Mission ACTIVE without progress, so ``run()`` goes idle."""

        progressed = False
        key = judgment_key(tasks)
        cached = self.commit.criteria_judgment(mission.id, key)
        if cached is None:
            evaluated = await self._evaluate_criteria(mission, tasks)
            if evaluated is None:
                return True
            self.commit.record_criteria_judgment(
                mission.id, key, evaluated[0], summary=evaluated[1]
            )
            cached = evaluated
            progressed = True
        plain, summary = cached
        ruled, created = self._arbitrated(mission, tasks, key, plain)
        if ruled is None:
            return progressed or created  # a person rules on a Verifier conflict first
        plain = ruled
        if not all(bool(item.get("met")) for item in plain):
            self._judge_with_actions(mission, plain, summary, unmet="another criterion is unmet")
            return True
        self.commit.expire_approvals(mission.id)
        actions = {
            criterion: self.commit.action_for_criterion(mission.id, criterion, self._connectors)
            for criterion in mission.success_criteria
            if criterion.startswith(ACTION_PREFIX)
        }
        for criterion, action in actions.items():
            state = None if action is None else str(action["state"])
            if action is not None and state in {"REJECTED", "REVOKED", "EXPIRED"}:
                self.commit.fail_mission(
                    mission.id,
                    stop_reason=MissionStopReason.APPROVAL_REJECTED,
                    detail={
                        "kind": str(state).lower(),
                        "criterion": criterion,
                        "action_key": action["action_key"],
                    },
                )
                await self._release_mission(mission.id)
                return True
            if action is not None and state == "FAILED":
                self.commit.fail_mission(
                    mission.id,
                    stop_reason=MissionStopReason.ACTION_FAILED,
                    detail={
                        "criterion": criterion,
                        "action_key": action["action_key"],
                        "error": action.get("error"),
                    },
                )
                await self._release_mission(mission.id)
                return True
            if action is None or state == "CANCELLED":
                self._judge_with_actions(
                    mission, plain, summary, unmet="no candidate reached the action ledger"
                )
                return True
        if any(
            a is not None and a["state"] not in HANDOFF_READY_STATES | {"SUCCEEDED"}
            for a in actions.values()
        ):  # review P2-1: nothing is handed off while another action of the Mission waits
            return progressed
        for criterion, action in actions.items():
            assert action is not None
            if action["state"] not in HANDOFF_READY_STATES:
                continue
            key_ = str(action["action_key"])
            done = await self.actions.hand_off(key_)
            after = self.store.get_action(key_) if done is None else done
            if after is not None and after["state"] in HANDOFF_READY_STATES:
                # the deployment will not run it (cap, budget, switched off): it cannot happen
                self.commit.fail_mission(
                    mission.id,
                    stop_reason=MissionStopReason.ACTION_FAILED,
                    detail={
                        "criterion": criterion,
                        "action_key": key_,
                        "reason": "handoff_refused:" + self.actions.last_refusal.get(key_, ""),
                    },
                )
                await self._release_mission(mission.id)
                return True
            self._note(
                f"mission {mission.id}: action {key_} → {None if after is None else after['state']}"
            )
            progressed = True
        if progressed:
            return True
        if all(a is not None and a["state"] == "SUCCEEDED" for a in actions.values()):
            self._judge_with_actions(mission, plain, summary, unmet=None)
            return True
        return False  # waiting for a person or a reconciliation: no progress, run() goes idle

    def _judge_with_actions(
        self,
        mission: Mission,
        plain: Sequence[Mapping[str, Any]],
        summary: str,
        *,
        unmet: str | None,
    ) -> None:
        by_criterion = {str(item.get("criterion")): dict(item) for item in plain}
        judgments: list[dict[str, Any]] = []
        for criterion in mission.success_criteria:
            if not criterion.startswith(ACTION_PREFIX):
                judgments.append(by_criterion[criterion])
                continue
            action = self.commit.action_for_criterion(mission.id, criterion, self._connectors)
            met = unmet is None and action is not None and action["state"] == "SUCCEEDED"
            receipt = {} if action is None else dict(action.get("receipt") or {})
            judgments.append(
                {
                    "criterion": criterion,
                    "met": met,
                    "judge": "action_ledger",
                    "reason": f"receipt {receipt.get('receipt_hash')}"
                    if met
                    else (unmet or "the action did not succeed"),
                    "action_key": None if action is None else action["action_key"],
                }
            )
        judged = self.commit.judge_mission(mission.id, judgments=judgments, summary=summary)
        self._note(f"mission {mission.id} judged: {judged.status} ({judged.stop_reason})")


def _under_source_root(path: str, roots: Sequence[str]) -> bool:
    return any(
        path.casefold() == root.rstrip("/").casefold()
        or path.casefold().startswith(root.rstrip("/").casefold() + "/")
        for root in roots
    )


def sha256_hex_text(content: str | bytes) -> str:
    import hashlib

    return hashlib.sha256(
        content if isinstance(content, bytes) else content.encode("utf-8")
    ).hexdigest()


__all__ = ("FAULT_POINTS", "InjectedCrash", "Orchestrator")
