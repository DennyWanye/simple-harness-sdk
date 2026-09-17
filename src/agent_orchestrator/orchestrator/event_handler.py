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
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState
from simple_harness.execution.provider_admission import ProviderAdmissionDenied

from .accounting_recovery import import_late_accounting
from .fragment_commits import SelectionFragmentExpired

if TYPE_CHECKING:
    from ..runtime.provider_budget_guard import ProviderBudgetGuard

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
    assert_no_secrets,
    build_critic_package,
    build_manager_package,
    build_planner_package,
    build_worker_package,
)
from ..context.manager_fragments import (
    consumer_fragment_context,
    manager_fragment_origin,
    manager_validated_fragment,
    validate_manager_fragment_choice,
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
    FragmentValidationDecisionV1,
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
from ..contracts.resolution import DeliveryStage, ReviewAccount
from ..contracts.state_machines import IllegalTransition
from ..governance.budgets import BudgetError, BudgetExhausted
from ..governance.domains import (
    requires_document_critic_proof,
    requires_mission_source_binding,
    supports_document_assessments,
)
from ..governance.permissions import Principal
from ..governance.policies import action_decision, deployed_layers, effective_tools
from ..governance.promotion import diff_params, interpreter_versions, resolve_params
from ..graph.changes import ChangeLimits, GraphChangeRejected, TaskGraphChange
from ..graph.eligibility import EligiblePrimitiveTask
from ..graph.projection_validation import GraphIntegrityError
from ..graph.task_graph import TaskBudgetFloor
from ..memory.summaries import build_summaries
from ..memory.verified_knowledge import KnowledgeIndex
from ..planning.fragments import _task_contract
from ..planning.manager import terminal_task
from ..planning.planner import (
    NO_APPLICABLE_METHOD,
    PROPOSAL_WRONG_BLOCK,
    NoApplicableMethodDeclared,
    parse_task_graph_proposal,
)
from ..runtime.actions import ActionExecutor, publication_overlaps_storage
from ..runtime.agent_worker import AgentBridge, Liveness, user_message_json
from ..runtime.assembly import (
    AssembledOrchestratorRuntime,
    OrchestratorConfig,
    assemble_orchestrator_runtime,
)
from ..runtime.first_request_budget import (
    FirstRequestBudget,
    FirstRequestBudgetUnknown,
    ProviderInputCap,
    actual_output_ceiling,
    first_request_budget,
    frozen_provider_input_cap,
)
from ..runtime.model_router import (
    DEFAULT_PROFILE,
    ModelRouter,
    RoutingDecision,
    RoutingRules,
    RoutingUnavailable,
    RuntimeProfile,
    _error_codes,
    classify_turn_error,
)
from ..runtime.output_blocks import (
    BlockError,
    PortClaim,
    extract_block,
    outside_text,
    parse_port_claims,
    repair_hint,
)
from ..runtime.role_templates import (
    CRITIC,
    FRAGMENT_VALIDATION_DECISION_TAG,
    GRAPH_CHANGE_PROPOSAL_TAG,
    MANAGER,
    PLAN_REVISION_PROPOSAL_TAG,
    PLANNER,
    PLANNER_HIERARCHICAL,
    RESULT_ENVELOPE_TAG,
    role_for_task,
    template_for_domain,
)
from ..runtime.sandbox import resolve_executor
from ..runtime.tool_gateway import CRITIC_TOOLS, WORKER_TOOLS, WorkspaceBinding, run_pytest
from ..scheduling.allocator import (
    OPEN_ATTEMPT_STATES,
    AllocationPlan,
    AllocationPlanV2,
    allocate,
    allocate_v2,
)
from ..scheduling.backpressure import BackpressureState, Observation
from ..storage.store import (
    DispatchIntent,
    InjectedCrash,
    Store,
    StoreBusy,
    StoreConflict,
    StoreError,
)
from ..verification.assessments import task_contract_revision
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
    REFINEMENT_REQUESTED,
    SERVICE_INTENT_REHANDED_OFF,
    CommitRejected,
    CommitService,
    MissionSpec,
    Reservation,
    mission_account,
    task_account,
)
from .hierarchical_dispatch import (
    MISSION_STALLED,
    SYNTHESIS_ROUND_RECORDED,
    DispatchAdmissions,
    HierarchicalDispatch,
    append_hierarchical_event,
    is_hierarchical,
    record_assembly_missing,
)
from .occurrence_tasks import (
    MAX_READ_ONLY_REWRITE_REJECTIONS,
    MAX_READ_ONLY_REWRITE_REPAIRS,
    read_only_rewrites,
)
from .plan_commits import PlanPrincipal
from .resolution_commits import eligible_root_receipts

logger = logging.getLogger("agent_orchestrator")

#: P2.3d / defect D5-A.  The ``PlanningRejected`` reason a root-review repair round
#: carries.  It is a planning rejection rather than a new event type on purpose: the
#: rejection ledger is what ``_planning_rejections`` hands to the next proposal, so
#: recording it here is what makes the reviewer's findings reach the Planner at all,
#: and the per-revision bound is counted off the same rows.
# P2.3j: the reason code now lives beside its reader (``rejected_refinements``); the
# name is kept here so nothing that imported it from this module moves.
from .hierarchical_dispatch import (  # noqa: E402
    READ_ONLY_REWRITE_REPAIR_REASON,
    ROOT_REVIEW_REPAIR_REASON,
)

#: Deterministic 4xx provider refusals.  Runtime already settles
#: ``ProviderAuthenticationError`` / ``ProviderPaymentRequiredError`` as FAILED
#: (``_DEFINITE_PROVIDER_FAILURES``); this set is what the orchestrator reads off
#: a FAILED turn so it does not climb the planning ladder or RETRY_WAIT.
DEFINITE_AUTH_CODES = frozenset(
    {"provider_authentication_failed", "provider_payment_required"}
)

#: P2.3d / defect D2c.  The ``PlanningRejected`` reason for a proposal that *was*
#: readable and was refused on its content — a method that is not grounded here, an
#: operation the plan cannot carry.  ``proposal_unreadable`` stays what its name says:
#: the typed block could not be parsed at all (``__cause__`` is a ``BlockError``).
PROPOSAL_NOT_GROUNDED = "proposal_not_grounded"

#: P2.3g.  How many times one MethodSynthesizer round may be asked on the same anchor.
#: The first real round (Grok, H-L3-C1) answered with a complete method in a shape the
#: codec does not accept and was concluded ``UNREADABLE`` on the spot; the second ask
#: carries the codec's problems as ``schema_feedback`` — the same bounded repair the
#: root reviewer gets (``MAX_ROOT_REVIEW_ASKS``) and the Task Critic gets through
#: ``critic_schema_retry_feedback``.  A reply that was *read* and refused by the
#: admission protocol is a conclusion — unless (P2.3i) every problem on it is a
#: correctable slip of reference or shape (``CORRECTABLE_REJECTIONS`` in
#: ``planning.htn.synthesis``): the first real round on v2 (Grok, H-L3-C1-r0) was
#: refused for one undeclared input port the package had spelled out, and that too
#: is asked once more with the protocol's own lines attached.  The bound is the same.
MAX_SYNTHESIS_ASKS = 2

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
    "after_fragment_commit",  # P34: graph/receipt durable, Manager intent not settled
    "after_validated_fragment_graph_commit",  # P34: C retarget durable, Manager intent not settled
)
MAX_CRITIC_ATTEMPTS = 2
SYSTEM_CRITIC_MODEL_CALLS = 12


RECONCILE_EVERY_CYCLES = 50  # D7-5': UNKNOWN actions are asked about again while a run goes on

#: How many times one Mission's stall confirmation may say "the world moved, go round
#: again" within a single ``run()`` (P2.3c part 3a, third-round review P1-A).  The
#: confirmation cycle has side effects — it re-issues licences and records
#: observations — so a deployment whose observers answer something new each time would
#: move the fingerprint for ever.  Past this bound the run returns with the Mission
#: still ACTIVE and its stall recorded, which is an answer to the caller rather than a
#: verdict about the Mission.
MAX_STALL_CARRY_ONS = 2
#: P2.3f: how long a hierarchical Mission's service turn (Planner, MethodSynthesizer,
#: root reviewer, Critic) may sit on a Provider hand-off whose outcome is *unknown*
#: before the loop acts — the smaller of ``stall_seconds`` and this ceiling.  The
#: runtime is right not to settle such an invocation (the request may have reached
#: the model), and it is equally right that a Mission does not spend its whole
#: deadline on one question nobody can answer: the loop hands the same request off
#: once more, and if that is unknown too it ends the round through the role's own
#: failure door.
MAX_SERVICE_BLOCKER_SECONDS = 300.0
#: One re-hand-off per subject.  A second executor asking the same question is a
#: retry; a third is a loop that spends the Mission account on a Provider that is
#: down, which is what the deadline exists to end.
MAX_SERVICE_REHANDOFFS = 1
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


class _AcceptedSiblingSupersededVerification(Exception):
    """Only the recorder's rejected lease may signal this obsolete verifier."""


class _CriticAdmissionFailure(ContractError):
    """An SDK admission failure, not a malformed completed Critic verdict."""

    def __init__(self, error):
        super().__init__("critic provider admission denied")
        self.error = jsonable(error)


class Orchestrator:
    def __init__(
        self,
        config: OrchestratorConfig,
        provider=None,  # type: ignore[no-untyped-def]
        *,
        owner: str | None = None,
        poll_interval: float = 0.05,
        critic_wait_seconds: float | None = None,
        profiles: Mapping[str, RuntimeProfile] | None = None,
        routing: RoutingRules | None = None,
        connectors: Mapping[str, Any] | None = None,
        provider_kind: str | None = None,
        policy_pin: Mapping[str, Any] | None = None,
        provider_token_estimator=None,
        provider_token_estimators: Mapping[str, Any] | None = None,
    ) -> None:
        # D3-10': ``owner`` is this instance's identity for orchestration leases *and* for
        # the SDK runtime (``owner_id``); the SDK ``owner_scope`` is one constant for all.
        self._owner = owner or f"orchestrator-{os.getpid()}"
        self._config = replace(config, owner_id=self._owner)
        # P2.3b: the hierarchical assembly (§14 / §18.2 "only assemble and call").
        # ``None`` until a deployment installs one, and consulted *only* for a Mission
        # whose ``orchestration_semantics_version`` is hierarchical — so every legacy
        # branch below is reached by exactly the code it was reached by before.
        self._hierarchical: HierarchicalDispatch | None = None
        self._provider = provider
        self._provider_token_estimator = provider_token_estimator
        self._provider_admission: ProviderBudgetGuard | None = None
        self._provider_token_estimators = provider_token_estimators
        self._provider_admissions: dict[str, ProviderBudgetGuard | None] | None = None
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
        if provider_token_estimators is not None and (
            provider_token_estimator is not None
            or set(provider_token_estimators) != set(self._profiles)
        ):
            raise ValueError("per-pool token estimators must explicitly cover every profile")
        if provider_token_estimators is not None:
            from ..runtime.legacy_provider_slots import profile_has_frozen_admission

            for key, estimator in provider_token_estimators.items():
                if (estimator is None and self._profiles[key].context_policy is not None
                        and profile_has_frozen_admission(config, key) is not False):
                    raise ValueError("a new context pool requires its own token estimator")
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
        #: P2.3d / defect D5-B: the plan revision whose unrefined compounds this
        #: process has already put to the Planner, per Mission.  One round per
        #: revision: a successful refinement moves the revision on, and one that
        #: fails leaves it where it was, so the same question is never asked twice.
        self._refinement_rounds: dict[str, int] = {}
        #: P2.3f: when this process first saw a service turn blocked on an unknown
        #: Provider outcome, per ``intent_id:replays``.  In memory on purpose: the
        #: bound is a *wait*, and a restarted process starting the wait again costs at
        #: most one more window; the re-hand-off itself is durable (the event).
        self._service_blocked_since: dict[str, float] = {}
        self._poll = poll_interval
        self._critic_wait = (
            config.turn_deadline_seconds if critic_wait_seconds is None else critic_wait_seconds
        )
        if not 0 < self._critic_wait <= config.turn_deadline_seconds:
            raise ValueError("Critic wait must be positive and within the SDK turn deadline")
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
        #: result id → the output-port claims that arrived with that envelope
        #: (P2.3c part 2d, decision 4).  ``ResultEnvelope`` is a frozen contract with
        #: ``additionalProperties`` refused, so the claims are parsed out of the block
        #: and carried beside it for the rest of this cycle — the same shape
        #: ``_critic_verdicts`` above uses, and for the same reason.  The durable
        #: guard against a lost claim is ``accept_review``'s ``OUTPUT_PORT_UNCLAIMED``,
        #: which refuses rather than indexing a port nobody named.
        self._port_claims: dict[str, tuple[PortClaim, ...]] = {}
        #: mission id → the fingerprint of the stall just recorded for it, handed to
        #: ``_confirm_and_stop_stalled`` so the confirmation compares *this* stall
        #: against what one more cycle produces (P2.3c part 2d, decision 2).
        self._stalled_at: dict[str, str] = {}
        #: mission id → how many times this ``run()`` has already carried on because
        #: the stall confirmation moved the world (third-round review P1-A).
        self._stall_carry_ons: dict[str, int] = {}
        self._client_ids: dict[str, str | None] = {}
        self._released: set[str] = set()
        self.progress_log: list[str] = []
        self.cancel_receipts: list[dict[str, Any]] = []
        self._rotation = 0  # D6-1: round-robin start across active Missions
        self._verifying: dict[str, asyncio.Task[bool]] = {}  # D6-9': bounded verification set
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
        self._routers: dict[tuple[str, str | None, str | None], ModelRouter] = {}
        self._route_drops: dict[str, dict[str, str]] = {}
        self._route_noted: set[str] = set()

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> Orchestrator:
        self._store = Store.open(self._config.orchestrator_db)
        try:
            self._task_floor = self._budget_floor_rule()  # P3.1 fix F-ORCH-1
            self._commit = CommitService(
                self._store,
                conflict_tasks=self._config.knowledge_sharing,
                global_budget=self._config.global_budget,
                deployed_layers=self._deployed,
                task_floor=self._task_floor,
                candidates_for=self._candidates_for,
                system_tail_factory=self._mission_system_tail_plan,
                mission_profile_validator=self._validate_mission_profile,
                task_floor_for=self._task_floor_for_mission,
            )
            if self._provider_token_estimator is not None:
                from ..runtime.provider_budget_guard import ProviderBudgetGuard

                self._provider_admission = ProviderBudgetGuard(
                    self._commit, owner=self._owner, estimator=self._provider_token_estimator,
                    max_slots=self._config.max_concurrent_model_calls,
                    profile_slots=(
                        {key: min(profile.max_concurrent_model_calls or self._config.max_concurrent_model_calls,
                                  self._config.max_concurrent_model_calls)
                         for key, profile in self._profiles.items()}
                        if any(profile.max_concurrent_model_calls is not None
                               for profile in self._profiles.values()) else None
                    ),
                    price_tables={key: (profile.price_table.estimator()
                                        if profile.price_table is not None else None)
                                  for key, profile in self._profiles.items()},
                )
            if self._provider_token_estimators is not None:
                from ..runtime.provider_budget_guard import ProviderBudgetGuard

                slots = ({key: min(profile.max_concurrent_model_calls or self._config.max_concurrent_model_calls,
                                   self._config.max_concurrent_model_calls)
                          for key, profile in self._profiles.items()}
                         if any(p.max_concurrent_model_calls is not None for p in self._profiles.values()) else None)
                self._provider_admissions = {
                    key: None if self._provider_token_estimators[key] is None else ProviderBudgetGuard(
                        self._commit, owner=self._owner, estimator=self._provider_token_estimators[key],
                        max_slots=self._config.max_concurrent_model_calls, profile_slots=slots,
                        price_tables={key: (profile.price_table.estimator()
                                            if profile.price_table is not None else None)},
                    ) for key, profile in self._profiles.items()
                }
                self._provider_admission = self._provider_admissions[self._default_profile]
            local_admissions = {}
            if self._provider_admissions is None and self.store.has_table("legacy_provider_slots_v1"):
                # Once activated, a later deployment cannot silently return an
                # old pool to process-local slots by omitting the estimator map.
                self._provider_admissions = {
                    key: self._provider_admission for key in self._profiles
                }
            if self._provider_admissions is not None and any(
                admission is None for admission in self._provider_admissions.values()
            ):
                from ..runtime.legacy_provider_slots import LegacyProviderSlots, create_slot_table

                create_slot_table(self.store)
                local_admissions = {
                    key: LegacyProviderSlots(
                        self.store, profile_id=key,
                        max_slots=self._config.max_concurrent_model_calls,
                        profile_slots=min(profile.max_concurrent_model_calls
                                          or self._config.max_concurrent_model_calls,
                                          self._config.max_concurrent_model_calls),
                        fence=self._provider_handoff_fence,
                    ) for key, profile in self._profiles.items()
                    if self._provider_admissions[key] is None
                }
            self._open_policy_library()  # step 9 (plan D9-3'): role, seed, drift
            self._assembled = assemble_orchestrator_runtime(
                self._config, profiles=self._profiles, default_profile=self._default_profile,
                provider_admission=(self._provider_admission if self._provider_admissions is None else None),
                provider_admissions=self._provider_admissions,
                local_provider_admissions=local_admissions,
                provider_handoff_fence=self._provider_handoff_fence,
            )
            self._assembled.gateway.executed_lookup = self._executed_tool_proof
            # Before ANY pool may resume, import every old physical handoff.
            # This does not change the external admission identity of old intents.
            for key, admission in local_admissions.items():
                admission.recover(self._assembled.pool(key).runtime.uow)
            # SDK startup itself reconciles grants. Consume already durable late
            # receipts before it can raise on an actual overrun; no runtime is started
            # by this accounting-only pass. Tool counts also come from durable rows.
            self._commit.tool_calls_for = self._executed_tool_calls
            import_late_accounting(self)
            # Reconcile orphan execution identities before any runtime can resume tools.
            workspaces = self._assembled.workspaces
            await asyncio.to_thread(workspaces.sweep_exec_copies)
            # SDK __aenter__ can reconcile an UNKNOWN tool and immediately run
            # its successor. Install Host bindings and durable accounting first.
            self._assembled.gateway.on_rejected = self._audit_tool_rejection
            self._assembled.gateway.on_executed = self._record_tool_call
            from ..context.knowledge_tools import read_knowledge_tool

            self._assembled.gateway.knowledge_reader = (
                lambda mission_id, tool, args: read_knowledge_tool(
                    self.store, mission_id, tool, args,
                    sync_currentness=self.commit.sync_host_knowledge,
                )
            )
            self._assembled.gateway.executed_counter = self.store.count_tool_calls
            changes = backfill(self._store.list_all_artifacts(), workspaces.artifact_store)
            if changes:
                self._store.update_artifact_storage(changes)
            self._bind_startup_tools()
            try:
                await self._assembled.__aenter__()
            except ProviderAdmissionDenied as error:
                if error.detail.get("reason_code") == "bound_overrun":
                    # A receipt may appear during startup reconciliation, after the
                    # first scan. Pay its original actual cost even when SDK startup
                    # has failed; do not pretend that failed runtime has started.
                    import_late_accounting(self)
                raise
            # P3.2 D3: artifacts recorded before 0.10 move into the content-addressed store
            # (or are marked unavailable); execution copies a crash left behind are removed
            workspaces = self._assembled.workspaces
            self.cleanup_workspaces()  # P3.2 D4: finished Missions past their retention
            self._bridge = self._assembled.pool(self._default_profile).bridge
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
        except BaseException as error:
            # Enter failures do not trigger async-with's exit. Preserve the startup
            # error (including cleanup reports) even if resource shutdown also fails.
            try:
                await self.__aexit__(type(error), error, error.__traceback__)
            except BaseException as cleanup_error:
                error.add_note(f"Startup resource cleanup failed: {type(cleanup_error).__name__}")
            raise

    @contextlib.contextmanager
    def _provider_handoff_fence(self, agent_id: str, turn_id: str):
        """Fence legacy local-slot handoff with the same Store as public cancel.

        No estimator is required for lifecycle authority. Submitted service turns
        remain collectible after cancellation, but cannot start another request.
        """
        with self.store.transaction():
            rows = self.store.connection.execute(
                "SELECT intent_id FROM dispatch_intents WHERE agent_id=? AND expected_turn_id=?",
                (agent_id, turn_id),
            ).fetchall()
            if len(rows) != 1:
                raise ProviderAdmissionDenied(public_message="Provider has no unique dispatch intent.")
            intent = self.store.get_intent(rows[0][0])
            mission = None if intent is None else self.store.get_mission(intent.mission_id)
            if (intent is None or mission is None or mission.status in TERMINAL_MISSION
                    or intent.state not in {"AGENT_CREATED", "SUBMITTED"}):
                raise ProviderAdmissionDenied(public_message="Provider subject Mission or intent stopped.")
            yield

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

    def _selected_profile(self, mission_id: str) -> str | None:
        mission = self.store.get_mission(mission_id)
        if mission is None:
            return None
        selected = (mission.final_report or {}).get("runtime_profile_id")
        return selected if isinstance(selected, str) and selected else None

    def _task_floor_for_mission(self, mission_id: str) -> TaskBudgetFloor:
        selected = self._selected_profile(mission_id)
        if selected is None:
            return self._task_floor
        profile = self._profiles.get(selected)
        if profile is None:
            raise ContractError(f"Mission runtime profile {selected!r} is not configured")
        policy = profile.context_policy
        if policy is None:
            return self._task_floor
        first_tokens = policy.input_budget() + actual_output_ceiling(
            profile_default_max_output_tokens=profile.default_max_output_tokens,
            profile_max_output_tokens_ceiling=profile.max_output_tokens_ceiling,
            config_default_max_output_tokens=self._config.default_max_output_tokens,
            config_max_output_tokens_ceiling=self._config.max_output_tokens_ceiling,
        )
        return TaskBudgetFloor(base=first_tokens, critic=first_tokens)

    def _candidates_for(self, mission_id: str) -> int:
        """Candidates per Task from the policy ``mission_id`` is bound to (plan review P1-2)."""

        # step 9 (D9-4'): a whitelisted value is read from the bound policy, never the config
        return max(1, int(self.policy_for(mission_id)["candidates_per_task"]))

    def _budget_floor(self, mission_id: str) -> dict[str, int]:
        """What the Planner / Manager is told a Task must at least hold."""

        candidates = self._candidates_for(mission_id)
        floor = self._task_floor_for_mission(mission_id)
        return {
            "min_task_tokens": floor.floor_for((), candidates),
            "min_task_tokens_with_critic_review": floor.floor_for(
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

    def _validate_mission_profile(self, selected: str, params: Mapping[str, Any]) -> None:
        """Creation-transaction guard: a selected pool cannot mask a policy route."""
        from ..api.missions import MissionRequestError

        if selected not in self._profiles:
            raise MissionRequestError(f"runtime profile {selected!r} is not configured")
        base = self._model_router.rules
        policy_routing = dict(params.get("routing") or {})
        routes = {
            **{f"role:{key}": value for key, value in base.by_role.items()},
            **{f"task_kind:{key}": value for key, value in base.by_task_kind.items()},
            **{f"task_kind:{key}": value for key, value in
               dict(policy_routing.get("by_task_kind") or {}).items()},
        }
        conflict = next((name for name, target in routes.items() if target != selected), None)
        if conflict is not None:
            raise MissionRequestError(
                f"runtime profile {selected!r} conflicts with policy route {conflict}"
            )
        for name, overrides in (("escalate", base.escalate), ("fallback", base.fallback)):
            if selected in overrides and overrides[selected] != selected:
                raise MissionRequestError(
                    f"runtime profile {selected!r} conflicts with {name} route"
                )

    def _template(self, template: Any, mission_id: str) -> Any:
        return template_for_domain(
            template,
            self.commit.domain_for(mission_id),
            self.policy_for(mission_id)["prompt_versions"],
        )

    def _frozen_default_route(self, mission_id: str) -> str | None:
        """Recover an existing Mission's default from its original dispatch facts.

        Policy params freeze by-task-kind routing, not the deployment default.
        Only an explicit default decision proves that default; a role override,
        escalation or fallback must never be mistaken for it. No intent is edited.
        """
        defaults = set()
        for (encoded,) in self.store.connection.execute(
            "SELECT config_json FROM dispatch_intents WHERE mission_id=?", (mission_id,),
        ):
            frozen = json.loads(encoded)
            decision = frozen.get("routing") or {}
            if decision.get("reason") != "default":
                continue
            target = frozen.get("runtime_profile_id") or DEFAULT_PROFILE
            if (decision.get("profile_id") != target
                    or (frozen.get("agent_config") or {}).get("model_profile_ref") != target):
                raise ContractError("frozen default routing identities differ")
            defaults.add(target)
        if len(defaults) > 1:
            raise ContractError("Mission has conflicting frozen default routes")
        return next(iter(defaults)) if defaults else None

    def _router_for(self, mission_id: str) -> ModelRouter:
        """One router per policy version: the deployment's rules with the version's
        routing items on top; an item naming a profile this deployment lacks falls back
        to the deployment's rule, on record (plan D9-4')."""

        version_id = self.policy_version_of(mission_id) or ""
        selected = self._selected_profile(mission_id)
        frozen_default = None if selected is not None else self._frozen_default_route(mission_id)
        cache_key = (version_id, selected, frozen_default)
        router = self._routers.get(cache_key)
        if router is not None:
            if selected is None:
                self._note_route_drops(mission_id, version_id)
            return router
        if selected is not None:
            # All roles, including Planner/Critic/system tasks, use the Mission's
            # persisted choice. No fallback or escalation may leave that pool.
            from ..api.missions import MissionRequestError

            try:
                self._validate_mission_profile(selected, self.policy_for(mission_id))
            except MissionRequestError as error:
                raise ContractError(f"bound Mission runtime route unavailable: {error}") from error
            router = ModelRouter(self._profiles, RoutingRules(default=selected))
            self._routers[cache_key] = router
            return router
        routing = dict(self.policy_for(mission_id).get("routing") or {})
        base = self._model_router.rules
        if frozen_default is not None:
            if frozen_default not in self._profiles:
                raise ContractError(f"frozen default profile {frozen_default!r} is not configured")
            base = replace(base, default=frozen_default)
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
        self._routers[cache_key] = router
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

    def _executed_tool_proof(self, call_key: str) -> Mapping[str, Any] | None:
        record = self.store.get_tool_call(call_key)
        if record is None:
            return None
        attempt = self.store.get_attempt(record["subject_id"])
        if attempt is None or attempt.mission_id != record["mission_id"]:
            return None
        return {**record, "agent_id": attempt.agent_id}

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
        try:
            if self._assembled is not None:
                await self._assembled.__aexit__(*exc_info)
        finally:
            self._assembled = None
            store, self._store = self._store, None
            if store is not None:
                store.close()

    @property
    def store(self) -> Store:
        assert self._store is not None
        return self._store

    @property
    def commit(self) -> CommitService:
        assert self._commit is not None
        return self._commit

    @property
    def hierarchical(self) -> HierarchicalDispatch | None:
        """The hierarchical assembly, or None when this deployment has not installed one."""

        return self._hierarchical

    def install_hierarchical(self, planning: Any = None, **kwargs: Any) -> HierarchicalDispatch:
        """Install the new mode's assembly (P2.3b).

        Installing it changes nothing for a legacy Mission: every branch that consults
        it asks ``is_hierarchical(mission)`` first, which reads the Mission's own
        ``orchestration_semantics_version`` and defaults to ``legacy`` (§18.5 rule 1).
        """

        self._hierarchical = HierarchicalDispatch(
            self.store, self.commit, planning=planning, **kwargs
        )
        return self._hierarchical

    def _new_mode(self, mission: Mission) -> HierarchicalDispatch | None:
        """The assembly for this Mission, or None — the one place the mode is decided."""

        if self._hierarchical is None or not is_hierarchical(mission):
            return None
        return self._hierarchical

    def _assembly_missing(self, mission: Mission, *, at: str) -> bool:
        """Fail-closed: a hierarchical Mission with no assembly is not scheduled at all.

        Review finding F1.  ``_new_mode`` answers None for two different situations —
        "this Mission is legacy" and "this deployment never installed the assembly" —
        and every caller used to treat both as "use the legacy path".  For a legacy
        Mission that is right.  For a hierarchical one it is the silent half-mode
        §18.5 rule 1 forbids: the plan was committed under the new rules (occurrence
        rows, semantic bindings, DATA edges) and would then be dispatched under the
        old ones, on the ``TaskStatus.READY`` string, with the readiness gate, the
        TG §8.3 re-check and the root ``GoalResolution`` trigger all skipped.

        So the two situations are separated here: this returns True only for the
        second, records :data:`~.hierarchical_dispatch.ASSEMBLY_MISSING` once for the
        Mission, and its callers do nothing rather than falling back.  There is no
        auto-assembly to prefer over it: ``build_planning_world`` needs the
        deployment's own facts (which domains, which worktree, which layers are
        deployed, which observers) and an Orchestrator that guessed them would be
        inventing the declarations the plan is admitted against.
        """

        if self._hierarchical is not None or not is_hierarchical(mission):
            return False
        record_assembly_missing(self.store, mission, at=at)
        self._note(
            f"mission {mission.id} runs under hierarchical semantics and this deployment "
            f"installed no assembly; nothing is dispatched or judged at {at} "
            "(§18.5 rule 1 — call install_hierarchical())"
        )
        return True

    async def _plan_integrity_stop(self, mission: Mission, error: GraphIntegrityError) -> None:
        """Stop *this* Mission for a damaged plan and leave the run alone (§24.1 dec. 11).

        ``GraphIntegrityError`` is a ``RuntimeError``, and ``_cycle`` only forgives
        ``StoreBusy`` / ``CommitRejected`` / ``IllegalTransition`` — so an unguarded
        one would end ``run()`` and take every *other* Mission in this process down
        with it.  One Mission's corruption is one Mission's stop: the diagnosis is
        recorded, the Mission fails with its open work cascaded, and the loop carries
        on with the rest.
        """

        if self._hierarchical is not None:
            self._hierarchical.record_integrity_failure(mission.id, error)
        detail = {
            "code": getattr(error, "code", "projection_not_orderable"),
            "subjects": sorted(str(item) for item in error.remaining),
            "cycle": [str(item) for item in error.cycle],
            "diagnose": error.diagnose()[:600],
        }
        current = self.store.get_mission(mission.id)
        status = mission.status if current is None else current.status
        if status is MissionStatus.PLANNING:
            self.commit.fail_planning(mission.id, reason="plan_integrity", detail=detail)
        elif status is MissionStatus.ACTIVE:
            self.commit.fail_mission(
                mission.id, stop_reason=MissionStopReason.PLANNING_FAILED, detail=detail
            )
        else:  # already terminal, or not yet planning: the record is the whole answer
            self._note(f"mission {mission.id}: plan integrity failure while {status!s}")
            return
        await self._release_mission(mission.id)
        self._note(f"mission {mission.id} stopped: plan integrity ({detail['code']})")

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

    def _admission_for(self, profile_id: str) -> ProviderBudgetGuard | None:
        if self._provider_admissions is not None:
            return self._provider_admissions[profile_id]
        return self._provider_admission

    def _release_unknown_grants(self, intent: DispatchIntent) -> None:
        """Drop HELD grants for this intent so a re-hand-off is not refused by them.

        P2.3l / N5.  Hierarchical only in effect: a legacy Mission never reaches the
        re-hand-off / give-up path that calls this.  No-op when the deployment has
        no ``ProviderBudgetGuard``.
        """

        guard = self._admission_for(self.profile_of(intent))
        if guard is None:
            return
        guard.release_held_grants(
            intent_id=intent.intent_id, reason="provider_outcome_unknown"
        )

    def _service_config(self, decision: RoutingDecision) -> dict[str, Any]:
        config: dict[str, Any] = {
            "runtime_profile_id": decision.profile_id,
            "model": decision.model,
            "routing": decision.to_json(),
        }
        snapshot = self._profiles[decision.profile_id].context_snapshot()
        if snapshot is not None:
            config["runtime_context"] = snapshot
        admission = self._admission_for(decision.profile_id)
        if admission is not None:
            config["provider_admission_fingerprint"] = admission.fingerprint
        return config

    def _first_critic_budget(self, decision: RoutingDecision) -> FirstRequestBudget | FirstRequestBudgetUnknown:
        profile = self._profiles[decision.profile_id]
        guard = self._admission_for(decision.profile_id)
        cap = frozen_provider_input_cap(
            profile_id=decision.profile_id, model=decision.model,
            runtime_context=profile.context_snapshot(),
            estimator_fingerprint=(None if guard is None else guard.estimator.fingerprint),
        )
        return first_request_budget(
            provider_input_cap=cap,
            output_ceiling=actual_output_ceiling(
                profile_default_max_output_tokens=profile.default_max_output_tokens,
                profile_max_output_tokens_ceiling=profile.max_output_tokens_ceiling,
                config_default_max_output_tokens=self._config.default_max_output_tokens,
                config_max_output_tokens_ceiling=self._config.max_output_tokens_ceiling,
            ),
            guard_input_cap_protocol=(None if guard is None else getattr(guard, "input_cap_protocol", None)),
        )

    def _context_profile_for(self, config: Mapping[str, Any]) -> RuntimeProfile:
        profile_id = str(config.get("runtime_profile_id") or DEFAULT_PROFILE)
        profile = self.assembled.pool(profile_id).profile
        if config.get("runtime_context") != profile.context_snapshot():
            raise ContractError("context identity differs from the frozen dispatch intent")
        admission = self._admission_for(profile_id)
        if config.get("provider_admission_fingerprint") != (
            None if admission is None else admission.fingerprint
        ):
            raise ContractError("provider admission identity differs from the frozen dispatch intent")
        return profile

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
        return str(intent.config.get("runtime_profile_id") or DEFAULT_PROFILE)

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

    def create_mission_with_sources(
        self,
        *,
        tenant_id: str,
        request: Mapping[str, Any],
        sources: Sequence[Mapping[str, Any]],
        principal: Principal,
    ) -> dict[str, Any]:
        """Apply the deployment door before the single atomic Mission/source commit."""
        from ..api.missions import MissionRequestError, spec_from_request, validate_spec

        deployment = self._config.deployment_policy
        spec = spec_from_request(tenant_id, request, default_tools=deployment.allowed_tools)
        validate_spec(spec, available_tools=deployment.allowed_tools)
        try:
            self._check_mission_door(spec)
        except ContractError as error:
            raise MissionRequestError(str(error)) from error
        return self.commit.create_mission_with_sources(
            spec,
            sources=sources,
            principal=principal,
            provider_kind=self._provider_kind,
            policy_defaults=self._config_policy(),
            policy_pin=self._policy_pin,
        )

    def _commit_mission(self, spec: MissionSpec) -> tuple[Mission, bool]:
        return self.commit.create_mission(
            spec,
            provider_kind=self._provider_kind,
            policy_defaults=self._config_policy(),
            policy_pin=self._policy_pin,
        )

    def _check_mission_door(self, spec: MissionSpec) -> None:
        dojo_tools = set(self._config.agentdojo_tool_schemas)
        if spec.domain == "agentdojo-v1":
            if self._config.agentdojo_invoke is None:
                raise ContractError("AgentDojo Mission requires a bound episode environment")
            if dojo_tools != set(self._config.deployment_policy.agentdojo_tools):
                raise ContractError("AgentDojo schemas must match deployed tool names")
            if self._config.deployment_policy.local_code_execution:
                raise ContractError("AgentDojo must disable local code execution")
            if set(spec.allowed_tools) - dojo_tools - {
                "workspace_read_file", "workspace_write_file", "workspace_list",
                "knowledge_list", "knowledge_read",
            }:
                raise ContractError("AgentDojo Mission names an unavailable tool")
        elif set(spec.allowed_tools) & dojo_tools:
            raise ContractError("AgentDojo tools require the AgentDojo domain")
        are_tools = set(self._config.are_tool_schemas)
        if spec.domain == "are-v1":
            if self._config.are_invoke is None:
                raise ContractError("ARE Mission requires a bound episode environment")
            if are_tools != set(self._config.deployment_policy.are_tools):
                raise ContractError("ARE schemas must match deployed tool names")
            if self._config.deployment_policy.local_code_execution:
                raise ContractError("ARE must disable local code execution")
            if set(spec.allowed_tools) - are_tools - {
                "workspace_read_file", "workspace_write_file", "workspace_list",
                "knowledge_list", "knowledge_read",
            }:
                raise ContractError("ARE Mission names an unavailable tool")
        elif set(spec.allowed_tools) & are_tools:
            raise ContractError("ARE tools require the ARE domain")
        if spec.domain == "appworld-v1" and self._config.appworld_execute is None:
            raise ContractError("AppWorld Mission requires a bound episode environment")
        if spec.runtime_profile_id is not None:
            from ..api.missions import MissionRequestError

            if (not isinstance(spec.runtime_profile_id, str)
                    or not spec.runtime_profile_id.strip()
                    or spec.runtime_profile_id not in self._profiles):
                raise MissionRequestError(
                    f"runtime profile {spec.runtime_profile_id!r} is not configured"
                )
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

    def _bind_startup_tools(self) -> None:
        """Reconstruct frozen tool authority before SDK automatic recovery starts."""
        for intent in self.store.list_intents("AGENT_CREATED", "SUBMITTED"):
            if intent.agent_id is None or self._pool_missing(intent):
                continue
            if intent.kind == "attempt":
                attempt = self.store.get_attempt(intent.subject_id)
                if attempt is not None and attempt.status not in TERMINAL_ATTEMPT:
                    self._bind_workspace(attempt)
                    self._bind_agent(intent.agent_id, intent.config)
            elif intent.kind == "critic" and not self._critic_subject_stopped(intent):
                if intent.state == "AGENT_CREATED":
                    # A created Agent is not necessarily a submitted SDK turn.
                    # With no durable turn there is nothing startup can resume;
                    # recover() retains its cancel + Mission FAILED semantics for
                    # invalid source authority. A crash after submit still leaves
                    # a durable turn even if Host has not recorded SUBMITTED.
                    uow = self.bridge_for(intent).runtime.uow
                    turn = uow.read_agent_turn_by_input(intent.agent_id, intent.input_id)
                    if turn is None and intent.expected_turn_id is not None:
                        turn = uow.read_agent_turn(intent.expected_turn_id)
                    if turn is None:
                        if any(
                            row.phase not in {AgentTurnState.COMMITTED, AgentTurnState.FAILED}
                            for row in uow.list_agent_turns(intent.agent_id)
                        ):
                            raise ContractError("Critic SDK turn identity differs from frozen intent")
                        continue
                    if (
                        turn.agent_id != intent.agent_id
                        or turn.input_id != intent.input_id
                        or turn.turn_id != intent.expected_turn_id
                    ):
                        raise ContractError("Critic SDK turn differs from frozen intent")
                # A submitted SDK turn can resume during __aenter__: validate
                # before granting it tools, never rebind an invalid source tree.
                self._validate_mission_judge_intent(intent)
                self._bind_critic(intent.agent_id, intent.config)

    async def recover(self) -> None:
        """§16.4 recovery (D3-6'): rebind the workspaces of in-flight turns, let the
        Commit Service heal each active Mission (frontier recompute, orphan candidates
        closed), re-import the usage of settled-but-unpaid Attempts, wake the SDK
        turns, then let the loop re-drive the remaining intents (review P1-3)."""

        for intent in self.store.list_intents("AGENT_CREATED", "SUBMITTED"):
            if intent.agent_id is None:
                continue
            if self._pool_missing(intent):
                # The frozen turn belongs to another runtime pool. Leave its
                # workspace and SDK turn untouched for that pool to recover.
                continue
            if intent.kind == "attempt":
                attempt = self.store.get_attempt(intent.subject_id)
                if attempt is not None:
                    self._bind_workspace(attempt)
                    self._bind_agent(intent.agent_id, intent.config)
            elif intent.kind == "critic":
                if self._critic_subject_stopped(intent):
                    self.assembled.gateway.unbind(intent.agent_id)
                    await self._cancel_turn(intent)
                    continue
                try:
                    self._validate_mission_judge_intent(intent)
                except ContractError as error:
                    self.assembled.gateway.unbind(intent.agent_id)
                    await self._cancel_turn(intent)
                    self.commit.fail_mission(
                        intent.mission_id,
                        stop_reason=MissionStopReason.VERIFIER_UNAVAILABLE,
                        detail={"source_binding_error": str(error)},
                    )
                    # Fail closed before bridge.recover could wake this invalid turn.
                    raise
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
            try:
                await pool.bridge.recover()
            except ProviderAdmissionDenied as error:
                if error.detail.get("reason_code") != "bound_overrun":
                    raise
                # SDK/guard have committed the actual overrun. Import its original
                # cost before leaving recovery; new admission remains fail-closed.
                self._note(f"recovered actual provider overrun: {error}")
        import_late_accounting(self)

    async def run(self, *, max_cycles: int = 10_000, until_idle: bool = True) -> None:
        """Drive the loop until idle.  ``max_cycles`` bounds *progressing* cycles (work
        done), never the waiting: a slow real model turn may keep the loop polling for
        many minutes and must not end the run early (step 4 real-run finding)."""

        await self.recover()
        await self.actions.reconcile()  # D7-5': every run() first asks about UNKNOWN actions
        cycles = 0
        idle_rounds = 0
        # P1-A's carry-on budget is per ``run()``: a fresh execution cycle is allowed
        # to give a Mission the same benefit of the doubt the last one did.
        self._stall_carry_ons.clear()
        while cycles < max_cycles:
            progressed = await self._cycle()
            if progressed:
                cycles += 1
                idle_rounds = 0
                if cycles % RECONCILE_EVERY_CYCLES == 0:
                    await self.actions.reconcile()
                # P2.3e: a progressing cycle does not sleep, and the in-process bridge
                # answers without suspending — so a run of progressing cycles used to
                # starve the runtime's own turn tasks (H-L3-C1: two turns submitted at
                # t+0 whose preflight ran at t+28.5 s, the instant this loop finally
                # returned).  One yield per cycle costs nothing and keeps the loop's
                # progress from being the only thing that is allowed to happen.
                await asyncio.sleep(0)
                continue
            if not until_idle:
                # P2.3c part 2d, P2-17: a caller that drives the loop one step at a
                # time gets the same *record* an idle ``run(until_idle=True)`` gets.
                # Not the same *decision*: ``_confirm_and_stop_stalled`` spends a
                # whole confirming cycle and then fails the Mission, and a stepping
                # caller has not asked this loop to decide anything — it asked it to
                # take one step and hand control back.  The record is free; the
                # verdict is not the stepper's to make (§9.1: the repetition is what
                # licenses the stop, and one step is not a repetition).
                await self._record_hierarchical_stall()
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
                await self._record_hierarchical_stall()
                if await self._confirm_and_stop_stalled():
                    # Third-round review P1-A.  The confirmation cycle moved the world,
                    # and the work it unblocked is this loop's to dispatch — returning
                    # here would hand the caller an "idle" run with a ready occurrence
                    # and no Attempt.  Bounded by ``MAX_STALL_CARRY_ONS`` inside the
                    # confirmation itself, so a world that keeps changing under the
                    # gates ends the run rather than spinning in it.
                    idle_rounds = 0
                    continue
                return
            await asyncio.sleep(self._poll)
        # P2.3c part 2d, P2-17: ``max_cycles`` progressing cycles were spent and the
        # loop is leaving with work still on the plan.  The same record as the idle
        # path, for the same reason: a Mission left ACTIVE with nothing written down
        # is the one ending the smoke test refuses.  No stop here either — running
        # out of cycles is the caller's bound, not a statement about the Mission.
        await self._record_hierarchical_stall()

    async def _record_hierarchical_stall(self) -> None:
        """A hierarchical Mission that idles with work left over says so, once.

        P2.3c part 2c, found by the real-model smoke.  :meth:`run` returns when the
        loop is genuinely idle — nothing dispatched this cycle, nothing in flight,
        nothing deferred, no hand-off left to settle — and until now that left a
        hierarchical Mission sitting at ``ACTIVE`` with occurrences every gate had
        withheld and **nothing written down**: an operator saw a Mission that had
        simply stopped moving and had to re-derive which gate was holding what.

        The record is deliberately not a verdict, and part 2d keeps it that way: the
        Mission keeps its status and its rows here.  The *decision* is
        :meth:`_confirm_and_stop_stalled`, which runs immediately after this and only
        stops a Mission whose world has not moved across one more complete cycle —
        §9.1's "repeated no progress", where the repetition is what licenses the stop
        and a single idle cycle is not.

        A legacy Mission is none of its business (``_new_mode`` answers None), and a
        Mission with an admissible occurrence is not stalled — it is between cycles.
        """

        for mission in self._active_missions():
            if mission.status is not MissionStatus.ACTIVE:
                continue
            new_mode = self._new_mode(mission)
            if new_mode is None:
                continue
            try:
                admissions = new_mode.admissions(mission.id)
            except (GraphIntegrityError, ContractError, StoreError) as error:
                # An unreadable plan is already reported by the integrity path; it is
                # not this method's finding and must not become a second verdict.
                self._note(f"mission {mission.id}: stall check could not read the plan ({error})")
                continue
            rows = self.store.list_tasks(mission.id)
            if any(task.status is TaskStatus.ACTIVE for task in rows):
                # A row still running is not a stalled plan; the loop is waiting on it.
                continue
            if all(task.status in TERMINAL_TASK for task in rows):
                # Nothing is left to dispatch because nothing is left: that Mission is
                # finished or being judged, and this is not the method that says so.
                continue
            blocking = [item.to_json() for item in admissions.refusals]
            # An occurrence that every readiness gate admitted and that still did not
            # run is *also* part of the answer — the refusal then came from the
            # allocator (budget, concurrency, attempt policy), not from the plan — so
            # it is named here instead of being silently dropped from the record.
            admitted = sorted(admissions.readiness)
            if not blocking and not admitted:
                continue
            append_hierarchical_event(
                self.store,
                MISSION_STALLED,
                mission.id,
                # Keyed by the plan revision and the reasons, so one stall is recorded
                # once however many times the loop is re-entered, and a *different*
                # stall — another revision, or another gate — is a new record.
                key=f"{mission.id}:{admissions.plan_revision}:"
                + sha256_hex_text(
                    "|".join(sorted(item["reason"] for item in blocking) + admitted)
                )[:16],
                payload={
                    "code": "hierarchical_no_dispatchable_work",
                    "plan_revision": int(admissions.plan_revision),
                    # Every refusal, not a sample: the point is that nobody has to
                    # re-derive which gate is holding which occurrence.
                    "withheld": blocking[:32],
                    "withheld_count": len(blocking),
                    "admitted_not_dispatched": admitted[:32],
                    "unfinished": sorted(
                        task.id for task in rows if task.status not in TERMINAL_TASK
                    )[:32],
                    # §9.1: "the count is not reset by a rename".  The fingerprint is
                    # what makes two stalls comparable *by identity* rather than by
                    # coincidence, and it is what the confirmation cycle re-computes.
                    "fingerprint": self._stall_fingerprint(mission, admissions, rows),
                },
            )
            self._note(
                f"mission {mission.id}: the loop went idle with work left over "
                f"({len(blocking)} withheld, {len(admitted)} admitted and not dispatched)"
            )
            self._stalled_at[mission.id] = self._stall_fingerprint(mission, admissions, rows)

    def _stall_fingerprint(self, mission: Mission, admissions: Any, rows: Sequence[Any]) -> str:
        """Everything that would have to change for this stall to be a different one.

        §9.1 requires that a repeated-no-progress count is **not** reset by a rename,
        so the identity is the *situation*: which revision, which occurrences were
        refused and for exactly which reasons and detail codes, which were admitted
        and not dispatched, which rows are still open, which scope epochs are in
        force, how much evidence has been recorded, and which duties currently hold an
        admitted demand.  Those last three are the world-facing ones: an observation
        recorded, an epoch bumped or a demand admitted from outside is precisely the
        thing that makes the very same plan runnable again, and each of them moves
        this digest.
        """

        from simple_harness.contracts import canonical_json

        from ..contracts.htn import ObligationId
        from ..storage.htn_store import HtnStore
        from ..storage.obligation_store import ObligationStore

        new_mode = self._new_mode(mission)
        semantics = HtnStore(self.store)
        duties = ObligationStore(self.store)
        epochs: Mapping[str, int] = {}
        demands: list[str] = []
        if new_mode is not None:
            try:
                epochs = new_mode.scope_epochs(mission.id)
            except (ContractError, StoreError, GraphIntegrityError):
                epochs = {}
            for duty in sorted(duties.obligation_ids(mission.id)):
                account = duties.account(mission.id, ObligationId(duty))
                if account.has_admitted_demand:
                    demands.append(duty)
        situation: dict[str, Any] = {
            "plan_revision": int(admissions.plan_revision),
            "withheld": sorted(
                str(canonical_json(item.to_json())) for item in admissions.refusals
            ),
            "admitted_not_dispatched": sorted(admissions.readiness),
            "unfinished": sorted(task.id for task in rows if task.status not in TERMINAL_TASK),
            "scope_epochs": {str(key): int(value) for key, value in sorted(epochs.items())},
            "support_revision": len(semantics.list_observations(mission.id)),
            "admitted_demands": demands,
        }
        return sha256_hex_text(canonical_json(situation))

    async def _confirm_and_stop_stalled(self) -> bool:
        """Look once more, and if the world has not moved, end this execution cycle.

        P2.3c part 2d, decision 2.  §15 makes a Mission a **bounded** cycle: "somebody
        could admit a demand later" belongs to the next cycle or to the Commitment
        above it, not to this one, and a run that never ends cannot enter the paired
        evaluation §21.5 asks for.  But §9.1 licenses stopping on *repeated* no
        progress, not on the first idle turn — and part 2c's smoke showed why: one
        evidence round or one witness re-issue moved the world twice.

        So exactly **one** more complete cycle runs — re-issue both licence lanes,
        one evidence round, advance the compound phases, re-read the admissions — and
        the fingerprint is taken again.  Moved: nothing happens and the loop is free to
        carry on.  Identical: :meth:`CommitService.fail_mission` ends the Mission with
        ``NO_DISPATCHABLE_WORK`` and a §6.4-shaped report — the structure that *was*
        expanded and the duties that are still outstanding, never a claim that the goal
        is impossible (§7.4).

        One cycle, hard-coded.  A ``while`` here would turn an idle loop into a busy
        one, which is the failure this method exists to end.

        Returns whether the loop should **carry on** — third-round review P1-A.
        The confirmation cycle is a cycle *with side effects*: it re-issues both
        licence lanes, records observations and advances compound phases, and the
        decision memo's own wording for a moved fingerprint is "do nothing, **let the
        loop carry on**".  :meth:`run` used to return unconditionally afterwards, so a
        Mission the confirmation had just unblocked — one admitted demand is enough —
        was handed back to the caller as "idle" with a ``READY_CANDIDATE`` occurrence
        and not one Attempt created.  That is exactly the case the memo protected when
        it rejected the alternative design.

        So the answer is "the world moved, go round again" — and it is bounded by
        :data:`MAX_STALL_CARRY_ONS` per Mission per :meth:`run`.  The bound is not
        decoration: a confirmation cycle *records observations*, so a deployment whose
        observers answer something new every time would move the fingerprint for ever
        and the carry-on would be the busy loop this whole path exists to end.  Past
        the bound the run simply returns — the Mission stays ACTIVE with its stall
        recorded, which is the caller's answer and not a verdict about the Mission.

        A ``False`` therefore means "nothing here needs another cycle": either a
        Mission was stopped, or there was no stalled hierarchical Mission at all.
        """

        from ..contracts.htn import ObligationId
        from ..contracts.obligations import ObligationLifecycle
        from ..storage.obligation_store import ObligationStore

        carry_on = False
        for mission in self._active_missions():
            if mission.status is not MissionStatus.ACTIVE:
                continue
            before = self._stalled_at.pop(mission.id, None)
            if before is None:
                continue
            new_mode = self._new_mode(mission)
            if new_mode is None:
                continue
            try:
                now_ms = int(self.store.now * 1000)
                network = new_mode.network(mission.id)
                new_mode.issue_input_witnesses(mission.id, network, now_ms=now_ms)
                new_mode.issue_start_witnesses(mission.id, network, now_ms=now_ms)
                self._gather_evidence(mission)
                new_mode.advance_compound_phases(mission.id)
                admissions = new_mode.admissions(mission.id)
            except (GraphIntegrityError, ContractError, StoreError) as error:
                # An unreadable plan is the integrity path's finding, not this one's.
                self._note(f"mission {mission.id}: stall confirmation could not read ({error})")
                continue
            rows = self.store.list_tasks(mission.id)
            after = self._stall_fingerprint(mission, admissions, rows)
            if after != before:
                spent = self._stall_carry_ons.get(mission.id, 0)
                if spent < MAX_STALL_CARRY_ONS:
                    self._stall_carry_ons[mission.id] = spent + 1
                    carry_on = True
                    self._note(
                        f"mission {mission.id}: the confirmation cycle moved the world; "
                        "the stall is not confirmed and the loop carries on"
                    )
                else:
                    self._note(
                        f"mission {mission.id}: the confirmation cycle moved the world for the "
                        f"{spent + 1}th time; this execution cycle ends with the Mission active "
                        "and its stall recorded"
                    )
                continue
            duties = ObligationStore(self.store)
            outstanding: list[dict[str, Any]] = []
            for duty in sorted(duties.obligation_ids(mission.id)):
                account = duties.account(mission.id, ObligationId(duty))
                if account.lifecycle is ObligationLifecycle.UNSATISFIED:
                    outstanding.append(
                        {
                            "obligation_id": duty,
                            "has_admitted_demand": bool(account.has_admitted_demand),
                            "remaining_fuel": int(account.remaining_fuel),
                        }
                    )
            self.commit.fail_mission(
                mission.id,
                stop_reason=MissionStopReason.NO_DISPATCHABLE_WORK,
                detail={
                    "plan_revision": int(admissions.plan_revision),
                    # §6.4: the report names the structure that was expanded and the
                    # duties still outstanding.  Every refusal, not a sample — an
                    # operator must not have to re-derive which gate held what.
                    "withheld": [item.to_json() for item in admissions.refusals],
                    "admitted_not_dispatched": sorted(admissions.readiness),
                    "outstanding_obligations": outstanding,
                    "fingerprint": after,
                    "confirmed_after_one_more_cycle": True,
                    # P2.3j: a Mission that idles *because* its root review rejected the
                    # plan and every repair route is spent says so here, rather than
                    # leaving "no dispatchable work" to be read as a scheduling problem.
                    **self._root_review_stop_detail(mission, new_mode),
                    **self._read_only_rewrite_stop_detail(mission, new_mode),
                },
            )
            self._note(
                f"mission {mission.id}: no dispatchable work, confirmed by one more cycle; "
                "this execution cycle ends"
            )
        return carry_on

    # ---------------------------------------------------------------- cycle
    def _active_missions(self) -> list[Mission]:
        return [m for m in self.store.list_missions() if m.status not in TERMINAL_MISSION]

    def _raise_if_verification_crashed(self) -> None:
        # The task table is the sole completion owner. Do not discard successful
        # siblings or report an error again from a delayed done callback.
        for result_id, task in list(self._verifying.items()):
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
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
            (
                (intent.kind != "critic" and intent.state == "SUBMITTED")
                or (intent.kind == "critic" and self._critic_subject_stopped(intent))
            )
            and self.profile_of(intent) in self.assembled.pools
            for intent in self.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED")
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
        progressed = import_late_accounting(self)
        # P2.3c part 2c: before anything is planned, look at what is still unknown.
        # It runs *first* because the Planner package is built out of the evidence
        # snapshot: gathering after the intent was created would show the model the
        # world as it was one round ago.
        for mission in self._active_missions():
            if self._gather_evidence(mission):
                progressed = True
            if await self._request_method_synthesis(mission):
                progressed = True
        for mission in self._active_missions():
            if mission.status is MissionStatus.CREATED:
                await self._start_planning(mission)
                progressed = True
            elif await self._refine_open_compounds(mission):
                # P2.3d / defect D5-B: a Mission used to be planned exactly once.  A
                # Planner that proposed a *nested* compound left it at
                # ``CompoundPhaseChanged{planning_ready, NEEDS_REFINEMENT}`` and nothing
                # ever asked for a method for it, so its primitives stayed in
                # ``WAITING_ORDER`` and the Mission stopped with
                # ``hierarchical_no_dispatchable_work`` — a plan deeper than one level
                # could be proposed and could never run.
                progressed = True
        if await self._retry_deferred_planning():
            progressed = True
        active = {mission.id for mission in self._active_missions()}
        for intent in self.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED"):
            if intent.kind == "critic" and self._critic_subject_stopped(intent):
                if await self._collect_stopped_critic(intent):
                    progressed = True
                continue
            if intent.mission_id not in active:
                continue
            if await self._dispatch(intent):
                progressed = True
        for intent in self.store.list_intents("SUBMITTED"):
            if intent.kind == "critic":
                if self._critic_subject_stopped(intent) and await self._collect_after_stop(intent):
                    progressed = True
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
            if (stored.envelope.mission_id not in active or stored.envelope.id in self._verifying
                    or self.commit.candidate_is_waiting(stored.envelope.id)):
                continue
            if len(self._verifying) >= self._config.verifier_workers:
                break
            task = asyncio.create_task(self._verify(stored.envelope.id))
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

    def _gather_evidence(self, mission: Mission) -> bool:
        """One read-only evidence round for a hierarchical Mission (P2.3c part 2c).

        ``True`` only when something was actually **recorded**.  An observer that is
        down returns nothing and the round is not progress — reporting it as progress
        would turn an outage into a loop that never goes idle and never gives the
        operator the quiet the ``OBSERVER_UNAVAILABLE`` record is supposed to stand out
        against.

        A deployment with no ``PlanningWorld`` is skipped rather than raised at: the
        planning path already refuses visibly (``require_planning_world``), and a
        Mission that cannot plan does not also need the loop to die here.
        """

        new_mode = self._new_mode(mission)
        if new_mode is None or new_mode.planning is None:
            return False
        try:
            result = new_mode.run_evidence_round(mission.id, now_ms=int(self.store.now * 1000))
        except GraphIntegrityError:
            # The plan is damaged; ``_decide`` is where that is diagnosed and stopped.
            return False
        recorded = tuple(result.recorded)
        if recorded:
            self._note(
                f"mission {mission.id} evidence round recorded {len(recorded)} observation(s)"
            )
        return bool(recorded)

    async def _request_method_synthesis(self, mission: Mission) -> bool:
        """Ask for a method when an open goal has none that could ever apply.

        P2.3c part 2c, the other half of part 2b's §8 item 5: the synthesiser's intent
        and its reply were both wired and nothing decided *when* to ask.  The judgment
        is :meth:`HierarchicalDispatch.goals_needing_method`, which is deliberately
        narrow — a ``NEEDS_EVIDENCE`` goal is answered by looking, not by inventing a
        method — and the bound is the intent's own creation key: ``ordinal=1`` for a
        given goal means exactly one synthesis round per goal per Mission ever, which
        is what keeps a goal nobody can serve from spending a model call each cycle.
        """

        new_mode = self._new_mode(mission)
        if new_mode is None or new_mode.planning is None:
            return False
        if mission.status not in {MissionStatus.PLANNING, MissionStatus.ACTIVE}:
            return False
        try:
            goals = new_mode.goals_needing_method(mission.id)
        except (GraphIntegrityError, ContractError):
            return False
        # P2.3j: a goal whose adopted method the root review rejected is asked about in
        # its *own* round — ``plan_revision + 1`` — with the reviewer's findings, once per
        # rejected revision; the pre-plan round (1) keeps its key and its bound.
        rejected = {
            item.goal_id: item for item in new_mode.rejected_refinements(mission.id)
        }
        progressed = False
        for goal_task_id in goals:
            rejection = rejected.get(str(goal_task_id))
            synthesis_round = 1 if rejection is None else int(rejection.plan_revision) + 1
            if new_mode.synthesis_round_recorded(
                mission.id, goal_task_id, synthesis_round=synthesis_round
            ):
                continue
            subject = self._synthesizer_subject(
                mission.id, goal_task_id, ordinal=1, synthesis_round=synthesis_round
            )
            if self.store.get_intent_for_subject(subject) is not None:
                # P2.3e (H-L3-C1, three identical episodes).  The round is out and the
                # goal stays in ``goals_needing_method`` until its answer is recorded, so
                # this method used to ask again every cycle; ``create_service_intent`` is
                # idempotent per subject and handed the same intent back, and *that* was
                # reported as progress.  A progressing cycle never sleeps, so ``run()``
                # spun its whole ``max_cycles`` budget in ~28 s — the runtime's turn
                # tasks starved the whole time — and then left by the ``max_cycles``
                # exit with both planning turns submitted and nobody left to collect
                # them.  Asking once is the bound; waiting is not progress.
                continue
            try:
                await self._create_synthesizer_intent(
                    mission.id,
                    goal_task_id,
                    ordinal=1,
                    synthesis_round=synthesis_round,
                    review_feedback=(
                        () if rejection is None else self._review_feedback_for(rejection)
                    ),
                )
            except (ContractError, CommitRejected, BudgetError) as error:
                self._note(f"method synthesis for {goal_task_id} not requested: {error}")
                continue
            self._note(
                f"mission {mission.id}: method synthesis round {synthesis_round} requested for "
                f"{goal_task_id}"
                + ("" if rejection is None else " (after a root review rejection)")
            )
            progressed = True
        return progressed

    @staticmethod
    def _review_feedback_for(rejection: Any) -> tuple[str, ...]:
        """The root review's findings, in the shape the synthesiser's request carries.

        References and the reviewer's own words only: which method instance was
        adopted, on which revision, what the review package was, and each finding
        verbatim (bounded).  No paraphrase and no diagnosis — the synthesiser is the
        one being asked what a different method would look like.
        """

        reference = rejection.method_ref
        if str(getattr(rejection, "review_package_id", "") or ""):
            lines = [
                f"root review rejected the adopted method {reference.method_id}@"
                f"{int(reference.version)} (method instance {rejection.method_instance_id}, plan "
                f"revision {int(rejection.plan_revision)}, review package "
                f"{rejection.review_package_id}); every leaf of that method had been accepted "
                "and the MISSION_FINAL review still rejected the composed result"
            ]
        else:
            lines = [
                f"a read-only leaf rewrote the workspace under method {reference.method_id}@"
                f"{int(reference.version)} (method instance {rejection.method_instance_id}, plan "
                f"revision {int(rejection.plan_revision)}); {READ_ONLY_REWRITE_REPAIR_REASON}: "
                "put file changes in a write/patch step, not in a read-only leaf"
            ]
        for finding in list(rejection.findings)[:8]:
            severity = str(finding.get("severity", "")) or "finding"
            criterion = str(finding.get("criterion_id", "") or "")
            detail = str(finding.get("detail", ""))[:1200]
            lines.append(
                f"{severity}" + (f" on {criterion}" if criterion else "") + f": {detail}"
            )
        return tuple(lines)

    @staticmethod
    def _carried_review_feedback(intent: DispatchIntent) -> tuple[str, ...]:
        """The ``review_feedback`` a synthesis intent's request carried, for its retry.

        Read back from the sealed request rather than recomputed, so the second ask
        (P2.3g's structured retry) puts exactly the same question with the codec's
        problems added — and a request that carried none yields none.
        """

        message = intent.config.get("message")
        if not isinstance(message, Mapping):
            return ()
        try:
            payload = json.loads(str(message.get("content", "")))
        except (TypeError, ValueError):
            return ()
        if not isinstance(payload, Mapping):
            return ()
        return tuple(str(item) for item in payload.get("review_feedback", ()) or ())

    @staticmethod
    def _synthesizer_subject(
        mission_id: str, goal_task_id: str, *, ordinal: int, synthesis_round: int = 1
    ) -> str:
        """The MethodSynthesizer intent's subject — its creation key and its identity.

        P2.3j: round 1 keeps its exact spelling; a round opened after a root review
        rejection (``synthesis_round = plan_revision + 1``) has its own, so it is a
        different intent with its own idempotency.
        """

        if int(synthesis_round) <= 1:
            return f"{mission_id}:synthesizer:{goal_task_id}:{ordinal}"
        return f"{mission_id}:synthesizer:{goal_task_id}:round:{int(synthesis_round)}:{ordinal}"

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
            # Review P2-4: the test used to be ``is not PLANNING``, which was right while
            # the only planning round was the one that *produced* the first plan.  P2.3d
            # opens rounds on a Mission that is already ACTIVE (D5-A's repair, D5-B's
            # refinement), and dropping those silently left the Mission with the round
            # counted as used and never asked.  A Mission that has ended still drops.
            if mission is None or mission.status in TERMINAL_MISSION:
                self._deferred_planning.pop(mission_id, None)

    async def _try_planner_intent(self, mission_id: str, *, ordinal: int) -> bool:
        """Create the Planner intent, or — review P0-1 — wait (bounded) while its pool is
        cooling down; a package that would carry a credential stops planning visibly."""

        try:
            await self._create_planner_intent(mission_id, ordinal=ordinal)
        except GraphIntegrityError as error:
            # P2.3c part 2: the hierarchical package is built from the plan, so a
            # damaged plan is now noticed *before* a model call rather than after one.
            # It is still one Mission's stop and not the run's: §24.1 decision 11, and
            # ``GraphIntegrityError`` is a ``RuntimeError`` that ``_cycle`` does not
            # forgive.  Corruption is not a bad proposal, so the Planner is not asked
            # again — which is exactly what the damaged-plan witness asserts.
            damaged = self.store.get_mission(mission_id)
            if damaged is None:
                raise
            await self._plan_integrity_stop(damaged, error)
            return False
        except RoutingUnavailable as unavailable:
            since = self._deferred_planning.get(mission_id, (self.store.now, ordinal))[0]
            self._deferred_planning[mission_id] = (since, ordinal)
            if self.store.now - since >= self._config.profile_wait_seconds:
                self._deferred_planning.pop(mission_id, None)
                self._stop_planning_round(
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
            # Verification P2-F / mutation VO: ``ordinal`` is added only where it is
            # new information.  A Mission still in PLANNING writes exactly the payload
            # it wrote on main — the legacy ``MissionFailed.detail`` is a shipped shape
            # and this slice has no business widening it — while a Mission past PLANNING
            # is in a round that only P2.3d opens, where "which round" is the one thing
            # an operator cannot otherwise work out.
            rejected = self.store.get_mission(mission_id)
            detail: dict[str, Any] = {"error": str(error)[:300]}
            if rejected is not None and rejected.status is not MissionStatus.PLANNING:
                detail["ordinal"] = ordinal
            self._stop_planning_round(
                mission_id,
                reason="context_rejected",
                detail=detail,
                stop_reason=MissionStopReason.CONTEXT_REJECTED,
            )
            self._note(f"mission {mission_id}: planner package refused ({error})")
            return False
        self._deferred_planning.pop(mission_id, None)
        return True

    def _stop_planning_round(
        self,
        mission_id: str,
        *,
        reason: str,
        detail: Mapping[str, Any],
        stop_reason: MissionStopReason,
    ) -> None:
        """End a Mission whose Planner round cannot be opened, in its own phase's terms.

        Review P0-1 / P2-4.  ``fail_planning`` files the stop as a *planning* failure and
        it was the only ending here, which was right while every Planner round belonged
        to the phase that produces the first plan.  P2.3d opens rounds on a Mission that
        already holds a committed plan, and calling ``fail_planning`` on one of those
        says something untrue about it (``final_report.planning_failure`` on a Mission
        that was planned) and skips the cascade a Mission-level stop owes its open work.

        So: a Mission still in PLANNING ends exactly as it did before — byte for byte,
        which is what the legacy goldens read — and a Mission past it ends through
        ``fail_mission``, whose report carries the same reason and the Task rows.
        """

        mission = self.store.get_mission(mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            return
        if mission.status is MissionStatus.PLANNING:
            self.commit.fail_planning(
                mission_id, reason=reason, detail=detail, stop_reason=stop_reason
            )
            return
        self.commit.fail_mission(
            mission_id, stop_reason=stop_reason, detail={"reason": reason, **dict(detail)}
        )

    async def _planner_round_on_committed_plan(
        self, mission_id: str, *, ordinal: int, phase: str
    ) -> bool:
        """Open a Planner round for a Mission that already holds a plan (P2.3d).

        Review P0-1: ``_create_planner_intent`` reserves ``planner_reserve_tokens``
        against the Mission account and raises ``BudgetExhausted`` — a ``StoreError``
        that ``_cycle`` does not forgive and ``run()`` does not catch.  ``_start_planning``
        has caught it since step 6; D5-A's repair round and D5-B's refinement round had
        not, so a Mission that ran its account down — which is precisely the state the
        repair branch is reached in — took the whole process with it.

        One Mission's exhaustion is one Mission's stop (§24.1 decision 11).
        """

        try:
            return await self._try_planner_intent(mission_id, ordinal=ordinal)
        except BudgetExhausted as error:
            self._stop_planning_round(
                mission_id,
                reason="budget_exhausted",
                detail={
                    "dimension": error.dimension,
                    "requested": error.requested,
                    "remaining": error.remaining,
                    "account": error.account_id,
                    "phase": phase,
                    "ordinal": ordinal,
                    "scope": "global" if error.account_id == GLOBAL_ACCOUNT else "mission",
                },
                stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
            )
            self._note(
                f"mission {mission_id} stopped in {phase}: budget_exhausted ({error.dimension})"
            )
            return False

    async def _retry_deferred_planning(self) -> bool:
        progressed = False
        self._prune_deferred()
        for mission_id, (_since, ordinal) in list(self._deferred_planning.items()):
            mission = self.store.get_mission(mission_id)
            if mission is not None and mission.status is not MissionStatus.PLANNING:
                # Review P0-1: a deferred round belonging to a Mission that is already
                # ACTIVE is one of P2.3d's, and the retry must not carry its exhaustion
                # out of the loop either.  A Mission still in PLANNING keeps the exact
                # path it had, exception and all, so the legacy goldens do not move.
                if await self._planner_round_on_committed_plan(
                    mission_id, ordinal=ordinal, phase="deferred_planning"
                ):
                    progressed = True
                continue
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

    async def _refine_open_compounds(self, mission: Mission) -> bool:
        """Ask the Planner for a method for a compound this plan has not refined yet.

        P2.3d / defect D5-B.  ``_start_planning`` is the only caller of
        ``begin_planning`` and it runs once, while the Mission is CREATED; after the
        first ``PlanRevisionCommitted`` the Mission is ACTIVE and the Planner was never
        asked anything again.  §6.3's decomposition is recursive by construction, so a
        proposal with a nested compound was accepted, recorded as
        ``NEEDS_REFINEMENT``, and then hung for ever.

        The bound is **one refinement round per plan revision**, which needs no counter
        of its own: a round that succeeds commits a new revision and a round that does
        not leaves the revision where it was, so a Planner that cannot refine the goal
        is asked once and the Mission then goes idle with its stall recorded — rather
        than circling on the same question.
        """

        from ..contracts.htn import TaskForm

        new_mode = self._new_mode(mission)
        if new_mode is None or mission.status in TERMINAL_MISSION:
            return False
        active = new_mode.semantics().active_plan_revision(mission.id)
        if active is None:
            return False  # nothing is committed yet; ``_start_planning`` owns that
        revision = int(active.revision)
        if self._refinement_round_asked(mission.id, revision):
            return False
        if any(
            intent.kind == "plan" and intent.mission_id == mission.id
            for intent in self.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            )
        ):
            return False  # a planning round is already out; one question at a time
        try:
            network = new_mode.network(mission.id)
        except (GraphIntegrityError, StoreError):
            return False  # plan integrity is decided on its own path, not here
        # The same test ``goals_needing_method`` applies: a compound occurrence with no
        # adopted method instance is one nobody has refined.  ``ReadinessReason``'s
        # ``NEEDS_REFINEMENT`` is deliberately *not* it — §18.5 constraint 4 makes every
        # compound answer that, refined or not, so that a legacy status can never walk
        # one into the Worker path.
        open_compounds = [
            spec
            for spec in network.occurrences
            if spec.form is TaskForm.COMPOUND
            and network.adopted_instance_for(spec.occurrence_id) is None
        ]
        if not open_compounds:
            # In memory only: "this revision has nothing open" is a fact this process
            # can re-derive at any time, and writing an event for every finished plan
            # would put a row in the log for every cycle of every healthy Mission.  The
            # *ask*, below, is what needs to outlive the process.
            self._refinement_rounds[mission.id] = revision
            return False
        ordinal = self._next_planning_ordinal(mission.id)
        # Review P2-5: recorded **before** the intent, so a crash between the two ends
        # up asking nothing rather than asking twice, and recorded in the log rather
        # than on this instance, so a resumed Mission reads the same answer.
        self.commit.record_refinement_requested(
            mission.id,
            plan_revision=revision,
            ordinal=ordinal,
            open_goals=[str(spec.task_id) for spec in open_compounds],
        )
        self._refinement_rounds[mission.id] = revision
        self._note(
            f"mission {mission.id}: plan revision {revision} still holds "
            f"{len(open_compounds)} unrefined compound goal(s); asking the Planner again "
            f"(ordinal {ordinal})"
        )
        return await self._planner_round_on_committed_plan(
            mission.id, ordinal=ordinal, phase="compound_refinement"
        )

    def _refinement_round_asked(self, mission_id: str, revision: int) -> bool:
        """Has this plan revision already been put back to the Planner? (D5-B's bound)

        The in-memory dict is a cache in front of the log, not the answer: it saves
        reading the events on the cycles where a healthy plan has nothing open, and it
        is allowed to be empty — a fresh process falls through to the log and gets the
        same answer the process that wrote it would have given.
        """

        if self._refinement_rounds.get(mission_id) == revision:
            return True
        key = f"{REFINEMENT_REQUESTED}:{mission_id}:refine:{int(revision)}"
        asked = any(
            event.idempotency_key == key for event in self.store.list_events(mission_id)
        )
        if asked:
            self._refinement_rounds[mission_id] = revision
        return asked

    def _planning_rejections(self, mission_id: str) -> list[dict[str, Any]]:
        """Durable feedback for the next proposal (D3-2'): the recorded rejections."""

        return [
            {"reason": event.payload.get("reason"), "detail": event.payload.get("detail")}
            for event in self.store.list_events(mission_id)
            if event.type in {"TaskGraphRejected", "PlanningRejected"}
        ]

    def _hierarchical_planner_package(
        self, new_mode: HierarchicalDispatch, mission: Mission, *, ordinal: int
    ) -> Any:
        """Seal the hierarchical Planner's package (P2.3c part 2).

        The network is read through :meth:`HierarchicalDispatch.network`, the same
        call ``compile_proposal`` makes, so the package describes exactly the plan the
        commit will be checked against — and before the first revision exists that
        call already answers with the *seed* network, because the Planner's first job
        is to refine the root goal the Mission was opened for.  A damaged plan raises
        rather than producing a package about a plan that is not readable.

        A deployment without a ``PlanningWorld`` raises here too rather than falling
        back to the legacy package: §18.5 forbids the silent half-mode, and a Planner
        given the DAG package while the Commit Service expects a plan revision is
        exactly that.
        """

        # ``_seal`` is private to the context builder and is reached anyway, on
        # purpose: it renders the package *and* derives the context hash, and a second
        # renderer here would be a second answer to "what did the model see".  The
        # alternative — exporting a public alias — is a change to ``context/`` that
        # buys nothing but a name.  Reaching for the private one is the smaller debt
        # and is recorded in the journal as such.
        from ..context.context_builder import _seal
        from ..planning.htn.planner_package import hierarchical_planner_package

        world = new_mode.require_planning_world()
        network = new_mode.network(mission.id)
        # ``registry`` / ``catalog`` / ``predicates`` are *attributes* on a
        # ``PlanningWorld`` and ``capabilities`` / ``snapshot`` are calls — the same
        # split ``compile_proposal`` reads them with.  Spelled the same way here so
        # the package and the compiler cannot end up describing two different worlds.
        from ._read_set import SemanticReadSetChecker as _SemanticReadSetChecker

        snapshot = world.capabilities()
        records = tuple(getattr(snapshot, "records", ()) or ())
        # G1: one assessment, rendered into the prompt *and* recorded.  Computing it
        # twice would let the record and the message disagree about a world that moved
        # between them, and the record exists precisely to say what the model was told.
        reports = new_mode.method_applicability(mission.id)
        new_mode.record_method_applicability(mission.id, reports=reports)
        package = hierarchical_planner_package(
            mission,
            network,
            registry=world.registry,
            capabilities=[
                str(item.capability_id) for item in records if getattr(item, "available", False)
            ],
            unavailable_capabilities=[
                str(item.capability_id)
                for item in records
                if not getattr(item, "available", False)
            ],
            # Review F16 / P2.3c part 2c: the four-axis report is computed and handed
            # over instead of being declared and passed as ``()``.  ``facts`` is the
            # other half of the same repair: the read-set entry for every observation
            # this Mission recorded, so a Planner that cites a fact cites one the
            # library holds (part 2b's smoke stopped at ``READ_SET_UNRESOLVED``
            # because it had never been shown one).
            reports=reports,
            observations=new_mode.semantics().list_observations(mission.id),
            attempt_ordinal=ordinal,
            rejected=self._planning_rejections(mission.id) if ordinal > 1 else (),
            # Review P2-13: the read-set entry a fact is quoted by is computed by the
            # **checker that will re-check it**, never a second time here.
            read_item=_SemanticReadSetChecker(
                self.store, new_mode.semantics(), mission_id=mission.id
            ).read_item,
            # P2.3j: the occurrences whose adopted method the root review rejected —
            # the goal a repair round is *about*, which ``open_compound_goals`` cannot
            # list because it is refined.  Empty on every ordinary round.
            rejected_refinements_of=new_mode.rejected_refinements(mission.id),
            rejected_method_refs_of=new_mode.rejected_method_refs(mission.id),
        )
        return _seal(package)

    def _hierarchical_planner_template(self, mission_id: str) -> Any:
        """The hierarchical Planner's prompt, chosen by the **mode** (§18.5 rule 1).

        ``template_for`` honours a deployment's frozen ``prompt_versions`` pin for any
        template of the same *role*, and every code-domain deployment pins ``planner``
        to a DAG-Planner version.  So asking it for the hierarchical template returned
        the legacy one: the model was told to draw a task graph while being handed the
        hierarchical package, and every round came back ``proposal_unreadable`` —
        P2.3b's blocker (c) again, one layer further in.

        A pin is still honoured when it names a hierarchical version **written against
        the package this build assembles**, which is how a Mission stays replayable on
        the exact prompt it ran with.  Review P1-8: mode alone was not enough — every
        hierarchical version passed, so a pin on ``planner-hierarchical-v2`` produced
        the v2 prompt ("this package gives you no observation ids") against the v3
        package (which carries ``facts``), re-opening the ``READ_SET_UNRESOLVED`` the
        part-2c smoke was stuck on.  The pin now chooses among the versions of the
        current package version only; anything else falls back to that package's
        default prompt.
        """

        from ..runtime.role_templates import (
            PLANNER_HIERARCHICAL_V5,
            hierarchical_planner_versions,
        )

        candidate = self._template(PLANNER_HIERARCHICAL, mission_id)
        if candidate.prompt_version in hierarchical_planner_versions():
            return candidate
        # P2.3c part 2c: v3 is the one whose read-set rule matches the package the
        # branch above builds (it carries a ``facts`` section; v2 tells the model there
        # is none).  The prompt and the package are chosen together or not at all.
        # P2.3g: v4 is v3 minus the sentence that told the Planner to write a
        # ``<method_proposal>`` when no method applied; same package, so a pin on v3
        # still selects v3 above and the unpinned default is v4.
        # P2.3j: package v4 carries ``rejected_refinements``; v5 is the only prompt
        # written against it, so it is the default and the only pin honoured here.
        return PLANNER_HIERARCHICAL_V5

    def _hierarchical_worker_template(self, role: Any, mission_id: str) -> Any:
        """The Worker prompt that knows about output ports (part 2d, decision 4).

        Same rule as :meth:`_hierarchical_planner_template`, for the same reason: a
        deployment's frozen ``prompt_versions`` pins ``worker`` to a DAG-mode version,
        and ``worker-v3`` never asks the model which port its files belong to — so the
        accept side would refuse every leaf for ``OUTPUT_PORT_UNCLAIMED``.  A pin that
        names a hierarchical version is honoured, which is how a Mission stays
        replayable on the prompt it ran with; any other pin belongs to the other mode.

        P2.3d / defect D1: "a hierarchical version" is now per domain, and so is the
        fallback.
        """

        from ..runtime.role_templates import (
            hierarchical_worker_for_domain,
            hierarchical_worker_versions,
        )

        if role.name != "worker":
            return role
        if role.prompt_version in hierarchical_worker_versions():
            return role
        # P2.3d / defect D1: the fallback is the *domain's* hierarchical Worker, not a
        # constant.  Returning ``WORKER_HIERARCHICAL`` unconditionally threw away the
        # AppWorld template ``role_for_task`` had just selected and with it both
        # ``appworld_execute`` (``effective_tools`` walks ``role_tools``, so a tool the
        # role does not list is dropped however many other sets hold it) and the words
        # saying there is a simulated world — 20 of the Grok run's L1 episodes reported
        # ``tool_not_exposed`` and did nothing.  A domain that registers no hierarchical
        # Worker still falls back to the code-domain one.
        return hierarchical_worker_for_domain(self.commit.domain_for(mission_id))

    async def _create_planner_intent(self, mission_id: str, *, ordinal: int) -> DispatchIntent:
        from ..runtime.action_schema import planner_action_contract

        mission = self.store.get_mission(mission_id)
        assert mission is not None
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        source_binding = self._active_source_binding(mission_id)
        domain = self.commit.domain_for(mission_id)
        workload = None
        if domain.id == "doc-research-v1" and domain.version in {"6", "7", "8", "9"}:
            from ..context.source_workload import source_workload

            try:
                workload = source_workload(
                    store=self.store,
                    artifacts=self.assembled.workspaces.artifact_store,
                    mission=mission,
                    domain=domain,
                    versions=source_binding.get("source_versions", {}),
                    profiles=self._profiles,
                    config=self._config,
                )
            except (ValueError, OSError) as error:
                raise ContextRejected("registered source workload could not be verified") from error
        # P2.3c part 2 (P2.3b blocker c): the prompt and the package are chosen by the
        # Mission's *mode*, not independently.  A Planner asked for a
        # <plan_revision_proposal> while being handed the DAG package has nothing to
        # propose with — it cannot see the open goals, the registered methods or the
        # plan revision it is answering against — which is why every round came back
        # ``proposal_unreadable`` under a real model.  The legacy package is built by
        # the same call it always was, with the same arguments, so its bytes and its
        # context hash do not move (§18.5 rule 1).
        new_mode = self._new_mode(mission)
        if new_mode is not None:
            package = self._hierarchical_planner_package(new_mode, mission, ordinal=ordinal)
            template = self._hierarchical_planner_template(mission_id)
        else:
            package = build_planner_package(
                mission,
                workspace_files=sorted(set(seed) | set(source_binding.get("source_versions", {}))),
                source_versions=source_binding.get("source_versions"),
                attempt_ordinal=ordinal,
                rejected=self._planning_rejections(mission_id) if ordinal > 1 else (),
                deployed_layers=self._deployed,
                budget_floor=self._budget_floor(mission_id),
                domain=domain,
                workload=workload,
                action_candidate_contract=planner_action_contract(
                    mission_criteria=mission.success_criteria,
                    connectors=self._connectors,
                    deployment=self._config.deployment_policy,
                ),
            )
            template = self._template(PLANNER, mission_id)
        decision = self._route_service("planner", mission_id)
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

    def _accept_hierarchical_leaf(
        self,
        mission: Mission,
        task: Any,
        attempt: Any,
        *,
        result_id: str,
        layers: Sequence[Any],
        port_claims: Sequence[PortClaim] = (),
    ) -> None:
        """A verified primitive leaf → an ``Acceptance`` → the accepted-output index.

        P2.3c part 2b (§13 item 2).  ``accept_result`` moves the *Task* to COMPLETED,
        which is the legacy lifecycle and says nothing about AER: in the hierarchical
        mode a contribution is accepted by ``accept_review`` out of the review anchors,
        and only a recorded ``Acceptance`` lets a DATA consumer bind the producer's
        artifact at a declared port.  Without this call ``acceptance_outputs`` stayed
        empty and every consumer sat in ``WAITING_DATA`` forever.

        Legacy Missions never reach here — ``_new_mode`` answers None — and a
        compound is refused by the assembly rather than reviewed.  A refusal is
        *recorded and does not undo the verification*: the verdict is a fact that
        happened, and turning a refused acceptance into a failed result would throw
        away a passing run because the accept-side gate said "not yet".
        """

        new_mode = self._new_mode(mission)
        if new_mode is None:
            return
        from ..orchestrator.leaf_acceptance import LeafAcceptanceAssembly
        from .resolution_commits import ResolutionCommitRejected

        assembly = LeafAcceptanceAssembly(self.store, self.commit, dispatch=new_mode)
        producers = tuple(
            item for item in (getattr(attempt, "agent_id", None),) if isinstance(item, str) and item
        )
        try:
            receipt = assembly.accept(
                mission.id,
                str(task.id),
                result_id=str(result_id),
                layers=layers,
                artifacts=self.store.list_artifacts(attempt.id),
                producer_agent_ids=producers,
                reviewer_agent_id=f"critic:{attempt.id}",
                now_ms=int(self.store.now * 1000),
                command_id=f"accept:{result_id}",
                port_claims=port_claims,
            )
        except (ContractError, ResolutionCommitRejected, StoreError) as error:
            self._note(f"task {task.id}: acceptance refused ({error})")
            return
        self._note(f"task {task.id}: acceptance {receipt.acceptance_id} recorded")
        # P2.3l / N7: a last gating child of a nested compound just accepted.  Advance
        # the typed phase (so it reads composition_review) and form the inner
        # GoalResolution in this cycle — otherwise ORDER successors stay WAITING_ORDER
        # and the stall confirmation fires first (H-L4-M3-r0).
        try:
            new_mode.advance_compound_phases(mission.id)
            from .composition_review import CompositionAcceptanceAssembly

            CompositionAcceptanceAssembly(
                self.store,
                self.commit,
                dispatch=new_mode,
                issued_by=self._owner,
            ).resolve_ready(mission.id)
        except (GraphIntegrityError, ContractError, StoreError) as error:
            self._note(f"task {task.id}: inner composition review deferred ({error})")

    async def _create_synthesizer_intent(
        self,
        mission_id: str,
        goal_task_id: str,
        *,
        ordinal: int,
        schema_feedback: Sequence[str] = (),
        synthesis_round: int = 1,
        review_feedback: Sequence[str] = (),
    ) -> DispatchIntent:
        """The MethodSynthesizer's own dispatch (§7.3 source 4, §18.5 C8, §13 v1.4).

        P2.3j: ``synthesis_round`` / ``review_feedback`` are the round opened after a
        root review rejected the adopted method (``_request_method_synthesis``); the
        round number rides in the intent's config so the collector records the
        outcome under the right key, and the findings ride in the request as their
        own field.

        A **new role**, not a new version of an existing one, and that shows in three
        places rather than one:

        * its own role template (``METHOD_SYNTHESIZER``), so it cannot masquerade as a
          Task Critic;
        * its own budget account — the Mission's planning account, which is the
          ``mission_planning`` account of §13 v1.4 — so a synthesis round never lands
          on the Task budget of whatever goal happened to need a method;
        * its own typed context, ``synthesis.build_request``, which carries no
          Mission id, no principal and no budget field at all.

        The reply is admitted through
        :meth:`HierarchicalDispatch.apply_synthesizer_reply`, where the author is
        fixed at ``MODEL``.
        """

        from ..runtime.role_templates import METHOD_SYNTHESIZER

        mission = self.store.get_mission(mission_id)
        assert mission is not None
        new_mode = self._new_mode(mission)
        if new_mode is None:
            raise ContractError(
                "a MethodSynthesizer round belongs to a hierarchical Mission; a legacy "
                "Mission has no method library to extend (§18.5 rule 1)"
            )
        request = new_mode.synthesis_request(
            mission_id,
            goal_task_id,
            schema_feedback=tuple(schema_feedback),
            review_feedback=tuple(review_feedback),
        )
        # P2.3j: v3 is v2 plus the sentence that says what ``review_feedback`` is; a
        # deployment pinned to v1/v2 still gets its pin through ``_template``.
        template = self._template(METHOD_SYNTHESIZER, mission_id)
        decision = self._route_service("planner", mission_id)
        config = AgentConfig(
            name=f"method-synthesizer-{ordinal}",
            instructions=template.instructions,
            model_profile_ref=decision.profile_id,
            tool_names=(),
            # ``tool_names=()`` is what stops this agent calling a tool; the limit is a
            # *bound*, and :class:`AgentLimits` refuses a non-positive one — a zero here
            # raised ``ValueError`` before the intent was ever created, which the
            # part-3a smoke found on the root-review path (the identical spelling).
            limits=AgentLimits(
                max_model_calls_per_turn=2,
                max_tool_calls_per_turn=1,
                turn_deadline_seconds=self._config.turn_deadline_seconds,
            ),
        )
        message = user_message_json(json.dumps(request.to_json(), ensure_ascii=False))
        subject = self._synthesizer_subject(
            mission_id, goal_task_id, ordinal=ordinal, synthesis_round=synthesis_round
        )
        return self.commit.create_service_intent(
            kind="plan",
            subject_id=subject,
            mission_id=mission_id,
            # §13 v1.4: the cost lands on the Mission's planning account, never on the
            # Task account of the goal that needed the method.
            account_id=mission_account(mission_id),
            creation_key=subject,
            input_id="attempt-input",
            input_hash=sha256_hex(message),
            config={
                "agent_config": config.to_json(),
                "message": message,
                "context_version": request.content_hash(),
                "prompt_version": template.prompt_version,
                "base_version": mission.version,
                "ordinal": ordinal,
                "role": "method_synthesizer",
                "budget_account": str(ReviewAccount.MISSION_PLANNING),
                "goal_task_id": str(goal_task_id),
                "synthesis_round": int(synthesis_round),
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

    def _system_reservation(self, tokens: int, profile_id: str, calls: int) -> Reservation:
        from .mission_tail_commits import system_cost_upper

        profile = self._profiles[profile_id]
        table = profile.price_table
        if table is None:
            return self._reservation(tokens, profile_id)
        return Reservation(tokens, system_cost_upper(tokens,
            rate=max(table.input_micros_per_million_tokens, table.output_micros_per_million_tokens),
            physical_calls=calls))

    def _first_critic_reservation(self, budget: FirstRequestBudget, profile_id: str) -> Reservation:
        table = self._profiles[profile_id].price_table
        if table is None:
            return Reservation(budget.minimum_tokens, 0)
        input_tokens = budget.provider_input_cap.max_input_tokens
        output_tokens = budget.output_ceiling
        cost = ((input_tokens * table.input_micros_per_million_tokens + 999_999) // 1_000_000
                + (output_tokens * table.output_micros_per_million_tokens + 999_999) // 1_000_000)
        return Reservation(budget.minimum_tokens, cost)

    def _mission_system_tail_plan(self, mission: Mission, purpose: str, *, agent_config=None,
                                  critic=False, critic_ordinal=None):
        """Protect only the existing explicit system allowance, never enlarge it.

        The same frozen Mission policy supplies both original and consumption
        routes. Provider health/upgrade cannot silently reprice an existing hold.
        """
        from ..governance.mission_system_tail import SystemTailBinding, SystemTailRoute
        from ..governance.tail_budget import TailReserve
        from .mission_tail_commits import system_cost_upper

        if agent_config is not None:
            raw_limits = agent_config.get("limits") if isinstance(agent_config, Mapping) else None
            actual_calls = raw_limits.get("max_model_calls_per_turn") if isinstance(raw_limits, Mapping) else None
            ceiling = SYSTEM_CRITIC_MODEL_CALLS if critic else self._config.max_model_calls_per_turn
            if type(actual_calls) is not int or not 0 < actual_calls <= ceiling:
                raise BudgetError("system Agent limits exceed the frozen physical call bound")
            if critic and (type(critic_ordinal) is not int or not 1 <= critic_ordinal <= MAX_CRITIC_ATTEMPTS):
                raise BudgetError("system Critic ordinal exceeds the frozen physical call bound")

        report = mission.final_report or {}
        template = report.get("synthesis") or {}
        if purpose == "synthesis":
            if not template:
                return None
            limits = template.get("budget") or {}
            tokens = int(limits.get("max_tokens") or 0)
            attempts = int(limits.get("max_attempts") or 1)
            policy = template.get("verification_policy", self.commit.domain_for(mission.id).synthesis_default_policy)
            role = "synthesizer"
        else:
            tokens = int(report.get("conflict_reserve_tokens") or 0)
            attempts = min(2, mission.budget.max_attempts or 2)
            limits = {}
            policy = self.commit.domain_for(mission.id).conflict_template.policy
            role = "arbiter"
        if tokens <= 0:
            return None

        def route(name, task_kind):
            decision = self._router_for(mission.id).route(
                role=name, task_kind=task_kind, previous_attempts=(),
                unavailable_until={}, now=self.store.now,
            )
            profile = self._profiles[decision.profile_id]
            identity = {"schema": 1, "profile_id": profile.profile_id, "model": profile.model,
                        "provider_kind": profile.provider_kind, "context": profile.context_snapshot(),
                        "attempt_tokens": self._config.attempt_reserve_tokens,
                        "critic_tokens": self._config.critic_reserve_tokens,
                        "tool_cap": self._config.max_tool_calls_per_turn,
                        "max_model_calls_per_turn": self._config.max_model_calls_per_turn,
                        "critic_model_calls": SYSTEM_CRITIC_MODEL_CALLS,
                        "critic_turns": MAX_CRITIC_ATTEMPTS,
                        "empty_response_retries": self._config.empty_response_retries,
                        "default_max_output_tokens": profile.default_max_output_tokens,
                        "max_output_tokens_ceiling": profile.max_output_tokens_ceiling}
            return SystemTailRoute(decision.profile_id, decision.model, sha256_hex(identity),
                                   None if profile.price_table is None else profile.price_table.estimator())

        worker = route(role, str(report.get("task_kind") or "code"))
        critic = route("critic", None) if "critic_review" in policy else None
        # A single conservative ceiling over the total token allowance covers
        # either role; this is explicit price authority, not an estimated bill.
        rates = [max(r.price.input_micros_per_million_tokens, r.price.output_micros_per_million_tokens)
                 for r in (worker, critic) if r is not None and r.price is not None]
        # TerminationState.before_provider increments a durable ordinal BEFORE
        # every call; AgentTurn output-cap retry resets only phase, not totals.
        # Thus retries are already inside each AgentLimits cap, not a multiplier.
        calls = attempts * (self._config.max_model_calls_per_turn
                            + (MAX_CRITIC_ATTEMPTS * SYSTEM_CRITIC_MODEL_CALLS if critic else 0))
        cost = system_cost_upper(tokens, rate=max(rates), physical_calls=calls) if rates else 0
        tools_limit = limits.get("max_tool_calls", mission.budget.max_tool_calls)
        if tools_limit is None and self._config.global_budget is not None:
            tools_limit = self._config.global_budget.max_tool_calls
        tools = (0 if tools_limit is None else
                 min(int(tools_limit), self._config.max_tool_calls_per_turn * attempts))
        return TailReserve(tokens, cost, tools, attempts), SystemTailBinding(
            worker, critic, "runtime-system-physical-call-double-ceil-v2")

    async def _dispatch(self, intent: DispatchIntent) -> bool:
        """ORCH §4.3 steps 2–3 with the identity frozen in the intent (D5')."""

        if intent.kind == "attempt":
            deadline = self.commit.selection_deadline(intent.subject_id)
            if deadline is not None and self.store.now >= deadline:
                expired = self.commit.expire_selection_dispatch(intent.subject_id, owner=self._owner)
                if expired:
                    await self._release_attempt(intent.subject_id, cancel=True)
                return expired
        if self._pool_missing(intent):
            return False
        self._context_profile_for(intent.config)
        if intent.kind == "critic" and self._critic_subject_stopped(intent):
            return await self._collect_stopped_critic(intent)
        self._validate_mission_judge_intent(intent)
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
                if self._critic_subject_stopped(claimed):
                    return await self._collect_stopped_critic(claimed)
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
        frozen_intent = self.store.get_intent_for_subject(attempt.id)
        frozen_fragment = (
            None if frozen_intent is None
            else frozen_intent.config.get("validated_fragment_input")
        )
        if frozen_fragment is not None:
            try:
                from ..planning.fragments import current_task_revision

                consumer = self.store.get_task(attempt.task_id)
                if consumer is None or not isinstance(frozen_fragment, Mapping):
                    raise ContractError("validated fragment consumer intent unavailable")
                revision = current_task_revision(self.store, consumer)
                current_fragment = self.commit.fragment_input(
                    str(frozen_fragment["fragment_id"]),
                    consumer_task_revision_id=revision.revision_id,
                )
                current_bound = {
                    key: item for key, item in current_fragment.items()
                    if key != "consumer_task_revision_id"
                }
                frozen_bound = {
                    key: item for key, item in frozen_fragment.items()
                    if key != "consumer_task_revision_id"
                }
                if current_bound != frozen_bound:
                    raise ContractError("validated fragment frozen consumer provenance changed")
            except (ContractError, KeyError, TypeError) as error:
                raise ArtifactConflict(f"validated fragment consumer binding: {error}") from error
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
        fragment_files = self.commit.fragment_validation_inputs(attempt.task_id)
        inputs.update(fragment_files)
        # P3.2 D4 (review round 2 P2-4): a rebind — recover() and every dispatch — is
        # checked against the registered identity, never the directory's content
        base = sha256_hex(
            {
                "seed": {path: sha256_hex_text(content) for path, content in seed.items()},
                "inputs": {item.path: item.content_hash for item in self._upstream_inputs(attempt)},
                "previous": attempt.retry_of,
                **({"fragment_input_hashes": {path: sha256_hex_text(data)
                                               for path, data in fragment_files.items()}}
                   if fragment_files else {}),
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
                        current_bytes = read_nofollow(workspace.root / path)
                    except ArtifactStoreError:
                        current_bytes = None
                    if current_bytes != content:
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
        files.update(self.commit.fragment_validation_inputs(attempt.task_id))
        return files

    def _bind_agent(self, agent_id: str, config: Mapping[str, Any]) -> None:
        cap = config.get("max_tool_calls")
        context_profile = self._context_profile_for(config)
        attempt = self.store.get_attempt(str(config["attempt_id"]))
        if attempt is None:
            raise ContractError("Cannot bind an unknown Attempt")
        if self.commit.domain_for(attempt.mission_id).id == "appworld-v1":
            self.assembled.gateway.bind_appworld(attempt.mission_id)
        if self.commit.domain_for(attempt.mission_id).id == "agentdojo-v1":
            self.assembled.gateway.bind_agentdojo(attempt.mission_id)
        if self.commit.domain_for(attempt.mission_id).id == "are-v1":
            self.assembled.gateway.bind_are(attempt.mission_id)
        self.assembled.gateway.bind(
            agent_id,
            WorkspaceBinding(
                str(config["attempt_id"]),
                "work",
                True,
                tuple(config.get("allowed_tools", WORKER_TOOLS)),
                tuple(str(p) for p in config.get("untrusted_sources", ())),
                max_tool_calls=None if cap is None else int(cap),
                mission_id=attempt.mission_id,
                protected=self._read_only_inputs(str(config["attempt_id"])),
                protected_prefixes=tuple(config.get("source_roots", ())),
                denied_prefixes=self._config.deployment_policy.denied_path_prefixes,
                context_policy=context_profile.context_policy,
                tokenizer=context_profile.tokenizer,
            ),
        )

    def _validate_mission_judge_intent(self, intent: DispatchIntent) -> None:
        if intent.kind != "critic" or not intent.subject_id.startswith(
            f"{intent.mission_id}:judge:"
        ):
            return
        domain = self.commit.domain_for(intent.mission_id)
        if not requires_mission_source_binding(domain):
            return
        from ..verification.mission_sources import ensure_mission_tree

        mission = self.store.get_mission(intent.mission_id)
        if mission is None:
            raise ContractError("Mission source owner unavailable")
        if sha256_hex(intent.config.get("message")) != intent.input_hash:
            raise ContractError("Mission source request identity mismatch")
        from simple_harness.contracts import canonical_json as context_json

        message = intent.config.get("message")
        text = message.get("content") if isinstance(message, Mapping) else None
        catalog = intent.config.get("mission_source_catalog")
        view_id = intent.config.get("attempt_id")
        if not isinstance(text, str) or not isinstance(catalog, Mapping):
            raise ContractError("Mission source request catalog unavailable")
        if (
            f"## mission_source_catalog\n{context_json(dict(catalog))}\n\n" not in text
            or f"## attempt_id\n{view_id}\n\n" not in text
        ):
            raise ContractError("Mission source request catalog differs from frozen tree")
        ensure_mission_tree(self.store, mission, domain, self.assembled.workspaces, intent.config)

    def _bind_critic(self, agent_id: str, config: Mapping[str, Any]) -> None:
        context_profile = self._context_profile_for(config)
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
                context_policy=context_profile.context_policy,
                tokenizer=context_profile.tokenizer,
            ),
        )

    # ------------------------------------------------------------ knowledge (step 4)
    def _gather_knowledge(
        self, mission: Mission, task: Task, tasks_by_id: Mapping[str, Task],
        *, search_visibility: bool = False,
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
            self.commit.sync_host_knowledge(mission.id)
            records = self.store.list_knowledge(mission.id)
            claims = self.store.list_mission_claims(mission.id)
            document = self.commit.domain_for(mission.id).id == "doc-research-v1"
            stale = KnowledgeIndex.load(self.store, mission.id).stale() if document else None
            if stale and any(
                issue.get("code") == "ERROR" for issues in stale.values() for issue in issues
            ):
                raise RetrievalUnavailable("source dependency index could not be read")
            summaries = build_summaries(self.store, mission.id, stale=stale)
            disputes = disputed_claims(claims, mission_id=mission.id)
            if document:
                for conflict in self.store.list_conflicts(mission.id):
                    if conflict["state"] != "RESOLVED_BY_HUMAN":
                        continue
                    for item in disputes:
                        if item["claim_id"] in conflict["claim_ids"]:
                            item["human_arbitration"] = {
                                "conflict_id": conflict["conflict_id"],
                                "resolution": dict(conflict.get("resolution") or {}),
                                "marker": "人工裁决仅适用于本争议及其条件，不提升证据等级",
                            }
        except (StoreBusy, OSError, ValueError) as error:  # index unreadable / not ready
            raise RetrievalUnavailable(str(error)) from error
        ranked = rank_knowledge(
            task,
            records,
            tasks_by_id=tasks_by_id,
            limit=self._config.max_knowledge_items,
            stale=stale,
        )
        by_id = {record.id: record for record in records}
        from ..context.compression import GLOBAL_BRANCH, branch_of

        branch = branch_of(task, tasks_by_id)
        from ..context.role_visibility import SEARCH_ROLES, build_role_materials

        role_materials = None
        visible_records = [by_id[item.id] for item in ranked.items]
        search_role = role_for_task(task).name
        if search_visibility and search_role in SEARCH_ROLES:
            role_materials = build_role_materials(
                self.store, task=task, role=search_role, claims=claims, records=visible_records,
                artifact_store=self.assembled.workspaces.artifact_store, document=document,
            )
            visible_ids = {
                item["id"] for values in role_materials["sections"].values()
                for item in values if "id" in item
            }
            visible_records = [record for record in visible_records if record.id in visible_ids]
        scored = {item.id: item for item in ranked.items}
        return KnowledgeContext(
            retrieval=ranked,
            verified=tuple(knowledge_view(record, scored[record.id]) for record in visible_records),
            role_materials=role_materials,
            disputed=tuple(disputes),
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

    def _critic_subject_stopped(self, intent: DispatchIntent) -> bool:
        """Terminal ownership is global; a merely changed lease owner is not a stop."""

        mission = self.store.get_mission(intent.mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            return True
        attempt_id = intent.config.get("attempt_id")
        attempt = self.store.get_attempt(attempt_id) if isinstance(attempt_id, str) else None
        if attempt is None:  # Mission judge has a view id, not a worker Attempt.
            return False
        task = self.store.get_task(attempt.task_id)
        return attempt.status in TERMINAL_ATTEMPT or task is None or task.status in TERMINAL_TASK

    async def _collect_stopped_critic(self, intent: DispatchIntent) -> bool:
        # AGENT_CREATED can already have a real SDK turn after a lost submit
        # receipt. Inspect that exact turn; never submit again merely to find it.
        if intent.agent_id is not None and intent.expected_turn_id is not None:
            return await self._collect_after_stop(intent)
        self._settle_intent(intent, "FAILED")
        self._settle_service_if_known(intent.subject_id, intent.mission_id)
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
            if intent.kind == "attempt":
                await self._release_attempt(intent.subject_id, cancel=True)
            else:
                self.assembled.gateway.unbind(intent.agent_id)
                key = f"service:{intent.subject_id}:cancel"
                if key not in self._released:
                    self._released.add(key)
                    await self._cancel_turn(intent)
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
                # P2.3f: an existing turn is still "ours to wait for" — unless it is
                # waiting on a Provider hand-off nobody can resolve, which is the one
                # wait this loop ends itself (a re-hand-off, then the role's failure
                # door).  Everything else about a plan turn — slow, queued, mid-call —
                # is the executor's business and is not timed here.
                return (await self._resolve_provider_blocked_service(intent, liveness)) is not None
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
            try:
                # The commit deduplicates unchanged observations, but persists
                # blocker exits before testing their newly started stall window.
                attempt = self.commit.renew_lease(
                    attempt.id,
                    owner=self._owner,
                    lease_seconds=self._config.lease_seconds,
                    liveness=liveness.to_json(),
                    minimum_remaining_seconds=self._config.lease_seconds / 2,
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

    def _service_agent_ids(self, intent: DispatchIntent) -> list[str]:
        """Every executor this subject ever had, including abandoned re-hand-offs."""

        agents: list[str] = []
        seen: set[str] = set()
        for event in self.store.list_events(intent.mission_id):
            if event.type != SERVICE_INTENT_REHANDED_OFF:
                continue
            if event.payload.get("subject_id") != intent.subject_id:
                continue
            previous = event.payload.get("previous_agent_id")
            if isinstance(previous, str) and previous and previous not in seen:
                seen.add(previous)
                agents.append(previous)
        if isinstance(intent.agent_id, str) and intent.agent_id not in seen:
            agents.append(intent.agent_id)
        return agents

    def _import_usage(self, intent: DispatchIntent) -> None:
        agents = self._service_agent_ids(intent)
        if not agents:
            return
        mission = self.store.get_mission(intent.mission_id)
        include_unknown = mission is not None and is_hierarchical(mission)
        bridge = self.bridge_for(intent)
        facts = []
        for agent_id in agents:
            facts.extend(
                bridge.usage_facts(agent_id=agent_id, include_unknown=include_unknown)
            )
        if facts:
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
                if intent is not None and intent.config.get("provider_admission_fingerprint"):
                    # Guarded subjects use the all-Mission, original-binding
                    # accounting scanner after SDK recovery and on every cycle.
                    continue
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
            unknown_imported = self.commit.ledger.imported_unknown_count(subject_id) > 0
            unknown = self.commit.ledger.has_unknown_usage(subject_id)
        mission = self.store.get_mission(mission_id)
        hierarchical = mission is not None and is_hierarchical(mission)
        if unknown_imported and hierarchical:
            # P2.3l P1-1: credit known facts, release the reservation, keep the
            # unknown rows.  Legacy still holds the reservation (ORCH §12.2).
            self.commit.settle_subject_known(subject_id, mission_id, task_id=task_id)
            self._note(f"{subject_id}: known usage settled; unknown calls remain on the ledger")
            return
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
        # Review P1-1: an admitted synthesised method buys one more round.  Without it
        # the Mission raced two model calls against each other — planner ``n+1`` was
        # created the instant planner ``n`` was refused, and whether the synthesiser's
        # answer arrived before the ladder ran out decided whether the Mission lived.
        allowance = int(self._config.max_planning_attempts) + self._synthesis_credits(mission.id)
        if ordinal < allowance:
            if mission.status is MissionStatus.PLANNING:
                # The phase that produces the first plan, legacy included: unchanged,
                # exception and all.  ``_start_planning`` guards ordinal 1 and this rung
                # has never been guarded — widening that is a decision about the legacy
                # path and not one this slice gets to make on the way past.
                await self._try_planner_intent(mission.id, ordinal=ordinal + 1)
            else:
                # Verification of the P0-1 fix: it had closed only the *opening* of
                # D5-A's and D5-B's rounds.  When the answer to one of them is refused
                # the ladder climbs — with the runner passing ``max_planning_attempts=3``
                # that is simply the next thing that happens — and this rung was still
                # bare, so the same ``BudgetExhausted`` escaped ``_cycle()`` one rung
                # later, Mission left ACTIVE and no ``MissionFailed``.
                await self._planner_round_on_committed_plan(
                    mission.id, ordinal=ordinal + 1, phase="planning_ladder"
                )
        elif self._synthesis_intents_in_flight(mission.id):
            # The ladder is spent but the library is still being extended.  Ending here
            # would be ending on the old library; ``_after_synthesis_round`` reopens the
            # round when the answer lands, and ends the Mission when it is a refusal.
            self._note(
                f"mission {mission.id}: planning round {ordinal} rejected ({reason}); a method "
                "synthesis round is still out, so the ladder waits for its answer"
            )
        elif mission.status is MissionStatus.PLANNING:
            # P2.3l / N5: a Planner round that never reached a model (transport
            # unknown, 0 tokens) is not a planning failure — the Planner was never
            # heard.  ``runtime_unavailable`` is the existing stop reason for a
            # model service that stayed down.
            stop = (
                MissionStopReason.RUNTIME_UNAVAILABLE
                if reason == "provider_outcome_unknown"
                else MissionStopReason.PLANNING_FAILED
            )
            self.commit.fail_planning(
                mission.id,
                reason=reason,
                detail={"attempts": ordinal, **dict(detail)},
                stop_reason=stop,
            )
        else:
            # P2.3d: a Mission that already holds a committed plan is not killed by a
            # round that came *after* it — the root-review repair (D5-A) and the nested
            # compound refinement (D5-B) both run while the Mission is ACTIVE, and their
            # ladder is one round each rather than ``max_planning_attempts``.  The
            # rejection is recorded; the Mission carries on with the plan it has and,
            # if that plan cannot dispatch anything, stops through the stall path with
            # the refusals written down.
            self._note(
                f"mission {mission.id}: planning round {ordinal} rejected ({reason}); the "
                "committed plan stands and the Mission is not failed for it"
            )

    @staticmethod
    def _definite_auth_failure(error: Any) -> bool:
        """HTTP 401/402-class provider refusals the runtime already settled FAILED."""

        return bool(_error_codes(error) & DEFINITE_AUTH_CODES)

    async def _collect_plan(self, intent: DispatchIntent, result) -> None:  # type: ignore[no-untyped-def]
        mission = self.store.get_mission(intent.mission_id)
        assert mission is not None
        self._import_usage(intent)
        new_mode = self._new_mode(mission)
        if (
            result.state is not AgentTurnState.COMMITTED
            and new_mode is not None
            and self._definite_auth_failure(result.error)
        ):
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            detail = {"error": jsonable(result.error or {}), "auth": True}
            if mission.status is MissionStatus.PLANNING:
                self.commit.fail_planning(
                    mission.id,
                    reason="provider_unavailable",
                    detail=detail,
                    stop_reason=MissionStopReason.RUNTIME_UNAVAILABLE,
                )
            else:
                self.commit.fail_mission(
                    mission.id,
                    stop_reason=MissionStopReason.RUNTIME_UNAVAILABLE,
                    detail={"reason": "provider_unavailable", **detail},
                )
            self._note(
                f"{intent.subject_id}: definite provider auth/payment failure → "
                "runtime_unavailable"
            )
            return
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
        # P2.3c part 2c: a MethodSynthesizer round rides on the same ``plan`` intent
        # kind and is *not* a plan-revision proposal — its reply is a
        # ``<method_proposal>`` for the registry, not operations on this Mission's plan.
        # Part 2b created the intent and admitted the reply but wired nothing between
        # them, so a synthesis round's answer was collected as a plan proposal and
        # refused as unreadable.  The role is read from the intent's own config, which
        # is where ``_create_synthesizer_intent`` wrote it.
        if str(intent.config.get("role", "")) == "method_synthesizer":
            await self._collect_synthesizer(intent, result, mission, text)
            return
        # P2.3c part 3a: the root MISSION_FINAL reviewer rides on the same ``plan``
        # intent kind (it has no Attempt, so the ``critic`` kind's attempt-bound
        # collection does not apply) and its reply is a verdict, not a proposal.
        if str(intent.config.get("role", "")) == "root_reviewer":
            await self._collect_root_review(intent, result, mission, text)
            return
        # P2.3b: a hierarchical Mission's Planner speaks the typed contract (§18.3), so
        # the reply goes to the assembly and the flat-DAG path below is not entered.
        # (``new_mode`` was asked once, above, before the auth check.)
        if new_mode is not None:
            await self._collect_plan_hierarchical(intent, result, mission, text, new_mode)
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

    async def _collect_root_review(  # type: ignore[no-untyped-def]
        self, intent: DispatchIntent, result, mission: Mission, text: str
    ) -> None:
        """A ``<critic_verdict>`` reply → the official root ``ReviewRecord`` (AER I05).

        The conclusion is the reviewer's and nothing here adjusts it: ``PASS``
        becomes ``ACCEPT``, ``FAIL`` becomes ``REJECTED``, and a criterion the
        reviewer reported ``met: false`` is written ``FAIL`` — there is no branch
        that produces an ACCEPT out of a reply that did not say PASS.

        An unreadable reply writes **no** record.  ``parse_critic_verdict`` is strict
        on purpose (AER-V04: a malformed verdict is an error, never a PASS), and an
        answer we could not read is not a conclusion, so the package keeps its one
        official-record slot free and the Mission reaches the idle-stall path with
        ``HierarchicalRootReviewUnreadable`` written down.  Asking again with the same
        anchor would spend the Mission account on the same question.
        """

        from ..contracts.resolution import CriterionVerdict, ReviewVerdict

        new_mode = self._new_mode(mission)
        package_id = str(intent.config.get("review_package_id", ""))
        expected = [str(item) for item in intent.config.get("review_criteria", [])]
        if new_mode is None:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            return
        coordinator = self._root_review(mission, new_mode)
        try:
            package = coordinator.semantics.get_review_package(package_id)
        except StoreError as error:
            self._note(f"root review reply names no stored package ({error})")
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            return
        try:
            if result.state is not AgentTurnState.COMMITTED:
                raise ContractError(f"root reviewer turn failed: {dict(result.error or {})}")
            verdict = parse_critic_verdict(text, expected_criteria=expected)
        except (ContractError, BlockError) as error:
            coordinator.record_unreadable(
                mission.id,
                package,
                detail=str(error),
                reviewer_turn_id=str(getattr(result, "turn_id", "") or intent.intent_id),
            )
            self._note(f"mission {mission.id}: the root review reply was unreadable ({error})")
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            return
        verdicts = {
            str(item.get("criterion")): (
                CriterionVerdict.PASS if bool(item.get("met")) else CriterionVerdict.FAIL
            )
            for item in verdict.mission_criteria
        }
        try:
            record = coordinator.record_review(
                mission.id,
                package,
                verdict=ReviewVerdict.ACCEPT if verdict.passed else ReviewVerdict.REJECTED,
                criterion_verdicts=verdicts,
                reviewer_agent_id=str(intent.agent_id or "root-reviewer"),
                reviewer_turn_id=str(getattr(result, "turn_id", "") or intent.intent_id),
                findings=verdict.findings,
            )
        except (ContractError, StoreError) as error:
            self._note(f"mission {mission.id}: the root review record was refused ({error})")
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            return
        self._note(
            f"mission {mission.id}: root review {record.record_id} concluded {record.verdict!s}"
        )
        self._settle_intent(intent, "SETTLED" if verdict.passed else "FAILED")
        self._settle_service_if_known(intent.subject_id, mission.id)

    async def _collect_synthesizer(  # type: ignore[no-untyped-def]
        self, intent: DispatchIntent, result, mission: Mission, text: str
    ) -> None:
        """A ``<method_proposal>`` reply → the registry's admission protocol (§7.3).

        Assembly only: the parse, the four-axis admission decision and the author lock
        all live in :meth:`HierarchicalDispatch.apply_synthesizer_reply`, whose
        signature has no ``author`` parameter precisely so this call cannot present a
        model-written definition as anything else.  A refused proposal is **not** a
        planning failure: the Mission's plan is untouched, the registry simply did not
        take the definition, and the reason is recorded so an operator can see whether
        the model proposed something unsafe or something unimplementable.

        Two kinds of first reply earn one more ask on the same anchor, ordinal +1,
        bounded by ``MAX_SYNTHESIS_ASKS`` (P2.3g, P2.3i): one the codec could not read
        (``SynthesisReplyUnreadable``) and one the protocol read and refused for
        nothing but a correctable slip (``rejection_is_correctable``).  In both the
        second ask is opened **first** and the first ask is written down only once it
        is — a record that says "asked again" must not precede a reservation that may
        be refused (verification P2.3g P2-1).  A second ask the Mission cannot afford
        ends the Mission for the budget, in the budget's own words, exactly as a
        Planner round it cannot afford does.
        """

        from ..planning.htn.registry import RegistryAuthor
        from ..planning.htn.synthesis import (
            SynthesisReplyUnreadable,
            rejection_is_correctable,
            rejection_problems,
            synthesis_rejection_feedback,
            synthesis_schema_feedback,
        )

        new_mode = self._new_mode(mission)
        goal_task_id = str(intent.config.get("goal_task_id", ""))
        ordinal = int(intent.config.get("ordinal", 1))
        # P2.3j: which round this is (1 = pre-plan; n = after the root review rejected
        # the method adopted on revision n-1).  Read off the intent so the record and
        # the retry stay on the round the request was opened for.
        synthesis_round = int(intent.config.get("synthesis_round", 1) or 1)
        if new_mode is None:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            return
        # What the next ask would carry, and how this ask is written down once the
        # next one is really open.  Both stay ``None`` for a reply that is a conclusion.
        feedback: tuple[str, ...] | None = None
        record_first_ask: Any = None
        try:
            if result.state is not AgentTurnState.COMMITTED:
                raise ContractError(f"synthesizer turn failed: {dict(result.error or {})}")
            receipt = new_mode.apply_synthesizer_reply(mission.id, text)
            admitted = bool(receipt.admitted)
            problems = rejection_problems(receipt)
            method_ref = str(receipt.method_ref.method_id)
            verdict = str(receipt.verdict)
            if not admitted and rejection_is_correctable(receipt):
                # P2.3i: read, refused, and every problem names a reference or shape the
                # package already states the right value for (a port the type does not
                # declare, a ref not offered, a link to an unknown step …).  Not a
                # conclusion: the protocol's own lines go back as ``schema_feedback``.
                feedback = synthesis_rejection_feedback(receipt)

                def record_first_ask() -> None:
                    new_mode.record_synthesis_reply_rejected(
                        mission.id,
                        goal_task_id=goal_task_id,
                        ordinal=ordinal,
                        method_id=method_ref,
                        verdict=verdict,
                        problems=problems,
                    )

        except SynthesisReplyUnreadable as unreadable:
            # P2.3g: the reply could not be decoded — the registry never saw it.  That
            # is not an answer, so the same question is put once more with the codec's
            # problems attached.
            admitted, problems, method_ref, verdict = False, unreadable.problems, "", "UNREADABLE"
            feedback = synthesis_schema_feedback(unreadable)
            # The ``as`` name is unbound once the clause ends; the closure keeps the facts.
            block_defect = unreadable.block_defect

            def record_first_ask() -> None:
                new_mode.record_synthesis_reply_unreadable(
                    mission.id,
                    goal_task_id=goal_task_id,
                    ordinal=ordinal,
                    problems=problems,
                    block_defect=block_defect,
                )

        except (ContractError, BlockError, StoreError) as error:
            admitted, problems, method_ref, verdict = False, (str(error),), "", "UNREADABLE"

        retry_refused = ""
        exhausted: BudgetExhausted | None = None
        if feedback is not None and ordinal < MAX_SYNTHESIS_ASKS:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            try:
                await self._create_synthesizer_intent(
                    mission.id,
                    goal_task_id,
                    ordinal=ordinal + 1,
                    schema_feedback=feedback,
                    # P2.3j: the second ask stays on the round the first was opened
                    # for and carries the root review's findings it was carrying.
                    synthesis_round=synthesis_round,
                    review_feedback=self._carried_review_feedback(intent),
                )
            except BudgetExhausted as error:
                # The second ask reserves on the Mission's planning account like the
                # first; an account that cannot carry it stops the Mission for the
                # budget below, after the round it was in is concluded honestly.
                exhausted = error
                retry_refused = f"budget_exhausted: {error}"
            except (ContractError, CommitRejected, BudgetError, RoutingUnavailable) as refused:
                # No second ask could be opened: the round concludes on the reply it
                # has, with the reason the retry was not asked in its own field.
                retry_refused = f"{type(refused).__name__}: {refused}"
            else:
                record_first_ask()
                self._note(
                    f"method synthesis for {goal_task_id}: reply {ordinal} {verdict.lower()} "
                    f"({problems[0][:120] if problems else ''}); asking once more with the "
                    "problems attached"
                )
                return
        new_mode.record_synthesis_outcome(
            mission.id,
            goal_task_id=goal_task_id,
            admitted=admitted,
            problems=problems,
            method_id=method_ref,
            verdict=verdict,
            author=str(RegistryAuthor.MODEL),
            asks=ordinal,
            synthesis_round=synthesis_round,
            retry_refused=retry_refused,
        )
        self._note(
            f"method synthesis for {goal_task_id}: "
            f"{'admitted' if admitted else 'refused'} ({'; '.join(problems)[:200]})"
            + (f"; retry not asked: {retry_refused[:120]}" if retry_refused else "")
        )
        self._settle_intent(intent, "SETTLED" if admitted else "FAILED")
        self._settle_service_if_known(intent.subject_id, mission.id)
        if exhausted is not None:
            # One Mission's exhaustion is one Mission's stop (§24.1 decision 11), and
            # the stop says what ran out — not "the synthesis was refused", which is
            # not what happened to it.
            self._stop_planning_round(
                mission.id,
                reason="budget_exhausted",
                detail={
                    "dimension": exhausted.dimension,
                    "requested": exhausted.requested,
                    "remaining": exhausted.remaining,
                    "account": exhausted.account_id,
                    "phase": "method_synthesis",
                    "ordinal": ordinal + 1,
                    "goal_task_id": goal_task_id,
                    "scope": "global" if exhausted.account_id == GLOBAL_ACCOUNT else "mission",
                },
                stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
            )
            self._note(
                f"mission {mission.id} stopped in method_synthesis: budget_exhausted "
                f"({exhausted.dimension})"
            )
            return
        await self._after_synthesis_round(mission.id, admitted=admitted)

    async def _after_synthesis_round(self, mission_id: str, *, admitted: bool) -> None:
        """A synthesis round has concluded; somebody has to act on it (review P1-1).

        D2b opened the round — ``goals_needing_method`` stopped answering "look again"
        for a precondition two readings had already settled as unknowable — and nothing
        was wired to the *other* side of it.  The method was admitted, the outcome was
        recorded, the intent was settled, and all five callers of
        ``_try_planner_intent`` were elsewhere: the Mission had bought a method it never
        asked anybody to use.  The goal was still open, the Planner was never asked
        again, and the L3 episodes ended exactly where they had before the fix.

        A refused round is the other half and has to end the wait it caused: a Mission
        held in PLANNING only because this round was in flight would otherwise sit there
        with no intent and nothing to dispatch.
        """

        mission = self.store.get_mission(mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            return
        if self._planner_intents_in_flight(mission_id):
            return  # one question at a time; that round carries the new method already
        if admitted:
            ordinal = self._next_planning_ordinal(mission_id)
            self._note(
                f"mission {mission_id}: a synthesised method was admitted; asking the "
                f"Planner again (ordinal {ordinal})"
            )
            await self._planner_round_on_committed_plan(
                mission_id, ordinal=ordinal, phase="method_synthesis"
            )
            return
        if mission.status is MissionStatus.PLANNING and self._planning_ladder_spent(mission_id):
            self._stop_planning_round(
                mission_id,
                reason="method_synthesis_refused",
                detail={"attempts": self._planning_attempts(mission_id)},
                stop_reason=MissionStopReason.PLANNING_FAILED,
            )
            return
        if not admitted and self._read_only_rewrite_repairs(mission_id) > 0:
            self.commit.fail_mission(
                mission_id,
                stop_reason=MissionStopReason.NO_DISPATCHABLE_WORK,
                detail={
                    "reason": READ_ONLY_REWRITE_REPAIR_REASON,
                    "synthesis": "refused",
                    **self._read_only_rewrite_stop_detail(mission),
                },
            )
            self._note(
                f"mission {mission_id}: method synthesis refused after "
                f"{READ_ONLY_REWRITE_REPAIR_REASON}; this execution cycle ends"
            )

    def _planner_intents_in_flight(self, mission_id: str) -> bool:
        """An open ``plan`` intent that is a *Planner* round, not a synthesis round.

        The two ride on the same intent kind, so "is a plan intent open" answers yes to
        a synthesis round as well — which is right for "do not ask two questions at
        once" and wrong for "has the Planner already been asked".
        """

        return any(
            intent.kind == "plan"
            and intent.mission_id == mission_id
            and str(intent.config.get("role", "")) != "method_synthesizer"
            for intent in self.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            )
        )

    def _synthesis_intents_in_flight(self, mission_id: str) -> bool:
        return any(
            intent.kind == "plan"
            and intent.mission_id == mission_id
            and str(intent.config.get("role", "")) == "method_synthesizer"
            for intent in self.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            )
        )

    def _synthesis_credits(self, mission_id: str) -> int:
        """Planning rounds bought by a method the Mission synthesised for itself.

        One admitted method is one more question worth asking — the ladder's bound is
        "how many times may the Planner be wrong about the *same* library", and the
        library just changed.  Read off the log, so it is the same number after a
        restart; zero for a legacy Mission, which never writes these events.
        """

        return sum(
            1
            for event in self.store.list_events(mission_id)
            if event.type == SYNTHESIS_ROUND_RECORDED and bool(event.payload.get("admitted"))
        )

    def _planning_attempts(self, mission_id: str) -> int:
        return sum(
            1
            for event in self.store.list_events(mission_id)
            if event.type in {"TaskGraphRejected", "PlanningRejected"}
        )

    def _planning_ladder_spent(self, mission_id: str) -> bool:
        return self._planning_attempts(mission_id) >= (
            int(self._config.max_planning_attempts) + self._synthesis_credits(mission_id)
        )

    async def _collect_plan_hierarchical(  # type: ignore[no-untyped-def]
        self,
        intent: DispatchIntent,
        result,
        mission: Mission,
        text: str,
        new_mode: HierarchicalDispatch,
    ) -> None:
        """P2.3b: the typed Planner reply → one plan revision, or one named refusal.

        This method is assembly and nothing else: the parse, the compile, the bounded
        recompilation and the commit all live in
        :mod:`.hierarchical_dispatch`.  What belongs *here* is the part that is about
        the dispatch intent — importing the usage, settling the turn and taking the
        existing planning-rejection path when the round produced no revision, so a
        hierarchical Mission fails visibly through the same door as a legacy one.
        """

        try:
            if result.state is not AgentTurnState.COMMITTED:
                raise ContractError(f"planner turn failed: {dict(result.error or {})}")
            outcome = new_mode.apply_planner_reply(
                mission.id,
                text,
                principal=PlanPrincipal(
                    principal_id=intent.agent_id or self._owner,
                    scope_id="mission",
                    manager_epoch=new_mode.semantics().epoch(mission.id, "mission"),
                ),
                command_id=f"plan:{intent.intent_id}",
                source={
                    "intent_id": intent.intent_id,
                    "agent_id": intent.agent_id,
                    "turn_id": result.turn_id,
                },
            )
        except GraphIntegrityError as error:
            # Corruption is not a bad proposal: asking the Planner again cannot add a
            # semantic binding, so this Mission stops instead of burning its attempts.
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            await self._plan_integrity_stop(mission, error)
            return
        except NoApplicableMethodDeclared as declared:
            # P2.3g: the Planner answered, in the agreed shape, that nothing in the
            # library applies.  Its own reason code — not unreadable (the block was
            # fine) and not ungrounded (nothing was proposed) — and no repair hint,
            # because there is nothing to repair.  The rung is spent like any other
            # refused round; whether a synthesis round is opened is decided by
            # ``goals_needing_method`` (D2b), never by the Planner's say-so.
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            await self._planning_rejected(
                intent,
                reason=NO_APPLICABLE_METHOD,
                detail={
                    "proposal_id": declared.proposal_id,
                    "rationale": declared.rationale[:300],
                },
            )
            return
        except ContractError as error:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            detail: dict[str, Any] = {"error": str(error)[:300]}
            # §18.5 C8: a malformed block is repaired *within* the existing bounded
            # ladder — the one instruction that says what was wrong travels in the
            # durable rejection (which ``_planning_rejections`` feeds to the next
            # proposal), and no extra request is opened to launder the failure.
            cause = error.__cause__
            # P2.3d / defect D2c: "I could not read the block" and "I read it and it
            # breaks a rule" are different answers and used to share one reason code.
            # In the Grok acceptance run all six L3 planning failures were recorded as
            # ``proposal_unreadable`` while the Planner had in fact produced a
            # well-formed ``<plan_revision_proposal>`` that chose a NEEDS_EVIDENCE
            # method — so the event log said "the model cannot write the block" and an
            # operator looking for a formatting problem found none.
            # A turn that never committed produced no text at all, so there is nothing
            # to be "not grounded" about — that stays unreadable, like a malformed block.
            reason = "proposal_unreadable"
            if isinstance(cause, BlockError):
                detail["repair_hint"] = repair_hint(cause, PLAN_REVISION_PROPOSAL_TAG)
                detail["block_defect"] = cause.reason
                # P2.3g: the other role's block is its own reason — the two rounds the
                # Grok episode lost this way were filed "block_missing", and the next
                # round was told to write a block it had in fact written.
                if cause.reason == PROPOSAL_WRONG_BLOCK:
                    reason = PROPOSAL_WRONG_BLOCK
            elif result.state is AgentTurnState.COMMITTED:
                reason = PROPOSAL_NOT_GROUNDED
            await self._planning_rejected(intent, reason=reason, detail=detail)
            return
        except StoreConflict as error:
            # Verification P0-1 (second line): a commit that collides with a row the
            # store already holds — a re-created method instance id, for one — is a
            # refused round, not a crashed loop.  ``_cycle`` forgives StoreBusy and
            # CommitRejected only, so without this the Mission stayed ACTIVE forever
            # with ``run()`` gone.  The compiler refuses the known collision before
            # the commit; this catches whatever else the store may refuse.
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            await self._planning_rejected(
                intent,
                reason="plan_commit_refused",
                detail={"reason": "store_conflict", "error": str(error)[:300]},
            )
            return
        if not outcome.committed:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            await self._planning_rejected(
                intent,
                reason="plan_commit_refused",
                detail={
                    "proposal_id": outcome.proposal_id,
                    "reason": outcome.last_reason,
                    "attempts": outcome.attempts,
                },
            )
            return
        receipt = outcome.receipt
        assert receipt is not None
        new_mode.advance_compound_phases(mission.id)
        self._note(
            f"plan revision {receipt.new_plan_revision} committed for {mission.id} "
            f"(attempts={outcome.attempts})"
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
            error = result.error
            context_limit = (
                isinstance(error, Mapping)
                and error.get("error_code") == "context_required_content_too_large"
            )
            admission = (
                error.get("detail")
                if isinstance(error, Mapping)
                and error.get("error_code") == "provider_admission_denied"
                and error.get("source_kind") == "provider_admission"
                and error.get("retryable") is False
                else None
            )
            if not (isinstance(admission, Mapping)
                    and type(admission.get("schema_version")) is int
                    and admission.get("schema_version") == 1):
                admission = None
            mission_now = self.store.get_mission(attempt.mission_id)
            if (
                mission_now is not None
                and is_hierarchical(mission_now)
                and self._definite_auth_failure(error)
            ):
                self.commit.reject_result(
                    attempt.id,
                    turn_id=result.turn_id,
                    reason="turn_failed",
                    detail={
                        "error": jsonable(result.error or {}),
                        "error_kind": "provider_unavailable",
                    },
                )
                self._settle_intent(intent, "FAILED")
                self._settle_if_known(attempt)
                await self._release_attempt(attempt.id, cancel=False)
                self.commit.stop_task(
                    attempt.task_id,
                    stop_reason=MissionStopReason.RUNTIME_UNAVAILABLE,
                    detail={
                        "source_kind": "provider",
                        "retryable": False,
                        "error": jsonable(error or {}),
                    },
                )
                await self._release_mission(attempt.mission_id)
                self._note(
                    f"attempt {attempt.id}: definite provider auth/payment failure → stopped"
                )
                return
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason=("context_required_content_too_large" if context_limit else
                        "provider_admission_denied" if admission is not None else "turn_failed"),
                detail={
                    "error": jsonable(result.error or {}),
                    "error_kind": classify_turn_error(result.error),  # D6-4' classification
                },
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            if context_limit:
                self.commit.stop_task(
                    attempt.task_id, stop_reason=MissionStopReason.RUNTIME_UNAVAILABLE,
                    detail={"source_kind": "runtime_context", "retryable": False,
                            "error": jsonable(error)},
                )
                await self._release_mission(attempt.mission_id)
                self._note(f"attempt {attempt.id}: required context exceeds limit → stopped")
                return
            if admission is not None:
                reason = admission.get("reason_code")
                if reason == "usage_unresolved":
                    self.commit.wait_for_admission_usage(attempt.id)
                    self._note(f"attempt {attempt.id}: admission waits for usage; no new Attempt")
                    return
                if reason == "cancelled":
                    self.commit.cancel_mission(attempt.mission_id)
                else:
                    # Runtime/deadline is the existing budget time-cap category.
                    # Other admission failures retain their exact configuration /
                    # authority cause; they never enter provider health fallback.
                    stop = (MissionStopReason.BUDGET_EXHAUSTED
                            if reason in {"budget_exhausted", "deadline"}
                            else MissionStopReason.RUNTIME_UNAVAILABLE)
                    self.commit.stop_task(
                        attempt.task_id, stop_reason=stop,
                        detail={"source_kind": "provider_admission", "retryable": False,
                                "admission": dict(admission),
                                **({"dimension": "runtime"} if reason == "deadline" else {})},
                    )
                await self._release_mission(attempt.mission_id)
                self._note(f"attempt {attempt.id}: admission denied ({reason}) → stopped")
                return
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
        initial.update(self.commit.fragment_collection_baseline(attempt.id))
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
        # P2.3k verification P1-2: a hierarchical leaf whose type declares itself
        # read-only may not have changed a file it started from.  Grok C3's ``facts``
        # / ``reproduce`` leaves rewrote ``stats/window.py``; with ``code_test`` no
        # longer on such leaves, the declaration has to be enforced where the files
        # come in, not merely trusted.  Legacy Missions carry no binding and are
        # untouched (``new_mode`` answers None).
        new_mode = self._new_mode(mission)
        accepted_hashes = (
            {} if new_mode is None else self._accepted_path_hashes(mission.id)
        )
        if new_mode is not None:
            binding = new_mode.semantics().task_semantics_of(mission.id, task.id)
            rewrote = (
                []
                if binding is None
                else read_only_rewrites(
                    binding,
                    artifacts,
                    initial,
                    guarded=guarded,
                    accepted=accepted_hashes,
                )
            )
            if rewrote:
                self.commit.reject_result(
                    attempt.id,
                    turn_id=result.turn_id,
                    reason="read_only_leaf_rewrote_workspace",
                    detail={
                        "paths": rewrote,
                        "side_effect_kind": str(binding.side_effect_kind),
                        "capabilities": list(binding.capability_requirements),
                        "hint": (
                            "this leaf's task type is read-only: observe and report at "
                            "its declared output ports; do not change existing files"
                        ),
                    },
                )
                self._settle_intent(intent, "FAILED")
                self._settle_if_known(attempt)
                await self._release_attempt(attempt.id, cancel=False)
                count = self._read_only_rewrite_rejections(mission.id, task.id)
                if count >= MAX_READ_ONLY_REWRITE_REJECTIONS:
                    await self._escalate_read_only_rewrite(
                        mission, task, new_mode, rewrote=rewrote, count=count
                    )
                    return
                self._note(
                    f"attempt {attempt.id}: read-only leaf rewrote {rewrote} → RETRY_WAIT "
                    f"({count}/{MAX_READ_ONLY_REWRITE_REJECTIONS})"
                )
                return
        # P2.3m: drop files whose bytes already belong to a completed leaf.  Legacy
        # Missions cite upstream artifacts by listing them; applying this filter
        # there dropped those citations and left the static-DAG golden run waiting
        # on a result that never settled.
        consistent = (
            set()
            if new_mode is None
            else {
                artifact.path
                for artifact in artifacts
                if artifact.path in initial
                and accepted_hashes.get(artifact.path) == artifact.content_hash
            }
        )
        referenced = [
            artifact
            for artifact in artifacts
            if artifact.path not in consistent
            and (
                (
                    artifact.path not in guarded
                    and (
                        artifact.path in listed
                        or initial.get(artifact.path) != artifact.content_hash
                    )
                )
                or (artifact.path in guarded and artifact.path in listed)
            )
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
        # P2.3b / TG §7: a child's result advances its parent compound's *typed* phase.
        # It never creates an Attempt for the compound and never writes its Task row —
        # the phase is a projection of typed state, recorded as an event.
        # (``new_mode`` was asked once, above, before the read-only check.)
        if new_mode is not None:
            try:
                new_mode.advance_compound_phases(mission.id)
            except GraphIntegrityError as error:
                await self._plan_integrity_stop(mission, error)

    def _parse_envelope(
        self, text: str, attempt: Attempt, *, turn_id: str
    ) -> tuple[ResultEnvelope, str | None]:
        try:
            raw = extract_block(text, RESULT_ENVELOPE_TAG)
        except BlockError as error:
            raise ContractError(str(error)) from error
        # P2.3c part 2d, decision 4.  ``outputs`` is the hierarchical Worker's
        # "this file is what I produced at that port".  It is taken off the block
        # before ``ResultEnvelope.from_json`` because that contract refuses unknown
        # keys by design (§18.5: a model may not add fields to a system contract),
        # and it is checked *here* — against the ports this occurrence declares and
        # the files this Attempt actually wrote — so a bad claim is a bounded repair
        # on the same Attempt rather than a wrong artifact bound downstream.
        claims = self._port_claims_from(raw, attempt)
        client_ids = {raw.get("id"), raw.get("result_id")} - {None}
        if len(client_ids) > 1:
            raise ContractError("result carries both id and result_id with different values")
        client_result_id = None if not client_ids else str(next(iter(client_ids)))
        raw.pop("id", None)
        raw.pop("result_id", None)
        raw.setdefault("mission_id", attempt.mission_id)
        domain = self.commit.domain_for(attempt.mission_id)
        if domain.id == "doc-research-v1" and domain.version == "9":
            from ..verification.document_refs import expand_document_claim_refs

            intent = self.store.get_intent_for_subject(attempt.id)
            mission = self.store.get_mission(attempt.mission_id)
            if (intent is None or mission is None or intent.kind != "attempt"
                    or intent.mission_id != attempt.mission_id
                    or intent.subject_id != attempt.id):
                raise ContractError("document refs require the original Attempt intent")
            raw = expand_document_claim_refs(raw, intent_config=intent.config, mission=mission)
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
        if claims:
            self._port_claims[result_id] = claims
        return envelope, client_result_id

    def _port_claims_from(self, raw: dict[str, Any], attempt: Attempt) -> tuple[PortClaim, ...]:
        """Pop ``outputs`` off the block and check it, or refuse the block.

        Legacy Missions never declare a port, so ``outputs`` is absent, nothing is
        popped and the envelope parses exactly as it did before (§18.5 rule 1).
        """

        if "outputs" not in raw:
            return ()
        stated = raw.pop("outputs")
        mission = self.store.get_mission(attempt.mission_id)
        new_mode = None if mission is None else self._new_mode(mission)
        if new_mode is None:
            raise ContractError(
                "result has unknown fields: ['outputs']"  # the contract's own wording
            )
        declared = new_mode.declared_output_ports_for(attempt.mission_id, attempt.task_id)
        single = [item["port"] for item in declared if item.get("cardinality") == "single"]
        # The files this Attempt wrote are the ones this envelope *declares*: the
        # artifact rows are written from ``envelope.artifacts`` after the result is
        # accepted, so at parse time ``list_artifacts`` is empty and checking against
        # it refused every honest claim (found by the part 2d smoke, round 1).  Each of
        # those paths is separately checked against the real workspace before it
        # becomes an artifact, so a claim can never outlive a file that was not there.
        produced = [str(item) for item in (raw.get("artifacts") or []) if isinstance(item, str)]
        try:
            return parse_port_claims(
                stated,
                declared_ports=[item["port"] for item in declared],
                attempt_paths=produced,
                single_valued=single,
            )
        except BlockError as error:
            raise ContractError(str(error)) from error

    # ---------------------------------------------------------------- verify
    def _verification_superseded_by_accepted_sibling(self, result_id: str) -> bool:
        """Read the durable accept transaction; never settle or change a verdict."""
        losing = self.store.get_result(result_id)
        if losing is None:
            return False
        envelope = losing.envelope
        attempt = self.store.get_attempt(envelope.attempt_id)
        task = self.store.get_task(envelope.task_id)
        if (
            attempt is None
            or task is None
            or attempt.status is not AttemptStatus.SUPERSEDED
            or (attempt.failure or {}).get("reason") != "sibling_accepted"
            or attempt.task_id != task.id
            or attempt.mission_id != envelope.mission_id
            or task.mission_id != envelope.mission_id
            or task.status is not TaskStatus.COMPLETED
            or not task.accepted_result_id
            or task.accepted_result_id == result_id
            or losing.verification_state != "REJECTED"
            or losing.verdict != "superseded"
        ):
            return False
        accepted = self.store.get_result(task.accepted_result_id)
        if (
            accepted is None
            or accepted.verification_state != "DONE"
            or accepted.verdict != "PASS"
            or accepted.envelope.task_id != task.id
            or accepted.envelope.mission_id != task.mission_id
            or accepted.envelope.attempt_id == attempt.id
            or set(accepted.artifacts) != set(task.accepted_artifacts)
        ):
            return False
        winner = self.store.get_attempt(accepted.envelope.attempt_id)
        return (
            winner is not None
            and winner.status is AttemptStatus.COMPLETED
            and winner.task_id == task.id
            and winner.mission_id == task.mission_id
        )

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

        critic_admission_failure: _CriticAdmissionFailure | None = None

        async def recorder(layer: LayerResult) -> None:
            try:
                self._hold_lease(attempt.id)  # P1-3: a lost lease aborts verification
            except CommitRejected:
                if self._verification_superseded_by_accepted_sibling(result_id):
                    raise _AcceptedSiblingSupersededVerification from None
                raise
            detail = {"summary": layer.summary, **dict(layer.detail)}
            if layer.layer == "critic_review":
                if critic_admission_failure is not None:
                    detail["error"] = critic_admission_failure.error
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
            nonlocal critic_admission_failure
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
            except _CriticAdmissionFailure as error:
                critic_admission_failure = error
                raise
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
                    "ERROR" if supports_document_assessments(domain) else "FAIL",
                    "document assessment binding invalid",
                    {"reason": "assessment_binding_invalid", "error": str(error)},
                )
                await recorder(failure)
                self.commit.fail_result(result_id, failures=[failure.to_json()], owner=self._owner)
                return True
            evidence_resolver = EvidenceResolver(
                self.store, self.assembled.workspaces.artifact_store
            )
        try:
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
        except _AcceptedSiblingSupersededVerification:
            self._note(f"result {result_id}: obsolete verifier after sibling acceptance")
            return True
        if critic_admission_failure is not None:
            admission_detail_error = critic_admission_failure.error
            admission = admission_detail_error["detail"]
            reason = admission.get("reason_code")
            admission_failures = [
                {**item, "detail": {**dict(item.get("detail", {})), "error": admission_detail_error}}
                if item.get("layer") == "critic_review" else item
                for item in verdict.failures
            ]
            # Commit rejection and its terminal budget/configuration handling
            # atomically: recovery must never see a redo-eligible intermediate.
            with self.store.transaction():
                self.commit.fail_result(result_id, failures=admission_failures, owner=self._owner)
                if reason == "cancelled":
                    self.commit.cancel_mission(mission.id)
                else:
                    stop = (MissionStopReason.BUDGET_EXHAUSTED
                            if reason in {"budget_exhausted", "deadline"}
                            else MissionStopReason.RUNTIME_UNAVAILABLE)
                    self.commit.stop_task(task.id, stop_reason=stop, detail={
                        "source_kind": "provider_admission", "retryable": False,
                        "admission": admission, "result_id": result_id,
                        **({"dimension": "runtime"} if reason == "deadline" else {}),
                    })
            await self._release_mission(mission.id)
            self._note(f"task {task.id}: Critic admission denied ({reason}) -> stopped")
            return True
        if supports_document_assessments(domain) and assessment_binding is not None:
            from ..verification.assessments import validated_assessments
            from ..verification.conflicts import document_uncertainty_conflicts

            rule = next((r for r in verdict.layers if r.layer == "rule_check"), None)
            if rule is not None and rule.status in {"PASS", NEEDS_HUMAN}:
                assessments = validated_assessments(rule, binding=assessment_binding)
                conflicts = document_uncertainty_conflicts(
                    self.store,
                    mission_id=mission.id,
                    envelope=stored.envelope,
                    assessments=assessments,
                )
                if conflicts:
                    failure = LayerResult(
                        "rule_check",
                        "FAIL",
                        "uncertainty conflicts with another claim",
                        {
                            **dict(rule.detail),
                            "reason": "uncertainty_conflict",
                            "uncertainty_conflicts": conflicts,
                        },
                    )
                    await recorder(failure)
                    self.commit.fail_result(
                        result_id, failures=[failure.to_json()], owner=self._owner
                    )
                    return True
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
            if verdict.passed and self.commit.selection_policy_for(task.id) is not None:
                selection = self.commit.selection_round(task.id)
                assert selection is not None
                self.commit.record_candidate_ready(
                    result_id, owner=self._owner, round_id=selection["round_id"],
                    expected_round_version=selection["version"], command_id="ready:" + result_id,
                    connectors=self._connectors, deployment=self._config.deployment_policy,
                )
                return True
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
        if self.commit.selection_policy_for(task.id) is not None:
            return True
        accepted = verdict.passed and completed.status is TaskStatus.COMPLETED
        if accepted:
            self._fault("after_task_completed", "attempt")
            self._note(f"result {result_id} PASS → task {completed.id} COMPLETED")
            self._accept_hierarchical_leaf(
                mission,
                task,
                attempt,
                result_id=result_id,
                layers=verdict.layers,
                port_claims=self._port_claims.get(result_id, ()),
            )
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
            if supports_document_assessments(domain):
                failed_attempt = self.store.get_attempt(attempt.id)
                if (
                    failed_attempt is not None
                    and (failed_attempt.failure or {}).get("reason") == "inconclusive"
                ):
                    if self.commit.stop_inconclusive_task(task.id):
                        await self._release_mission(mission.id)
                        self._note(f"task {task.id} stopped: insufficient_evidence (retry limit)")
                    # Pure missing-limitations rework follows its own frozen allowance,
                    # not the generic Manager stall route (which could replace the Task).
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
                # An admitted fragment consumer has already used a Manager round
                # to replace A with F. Exhausting that separate governance budget
                # must not erase its own still-funded verification retry.
                fragment_parent = any(
                    parent is not None and "fragment_validation" in parent.context
                    for parent in (
                        self.store.get_task(parent_id) for parent_id in task.dependency_ids
                    )
                )
                frozen_attempt = self.store.get_intent_for_subject(attempt.id)
                fragment_retry = (
                    fragment_parent
                    and frozen_attempt is not None
                    and isinstance(
                        frozen_attempt.config.get("validated_fragment_input"), Mapping
                    )
                    and self._manager_rounds(mission.id)
                    >= int(self.policy_for(mission.id)["max_manager_rounds"])
                )
                if fragment_retry:
                    self._note(f"task {task.id}: retrying accepted fragment under original budget")
                else:
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
            if request["state"] == "GRANTED" and self.commit._is_document_conflict(task):
                # A legacy ordinary approval cannot resolve a document conflict.
                # Reuse valid checks, then request the bound arbitration on resume.
                human = None
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
            minimum_remaining_seconds=self._config.lease_seconds / 2,
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
        held: set[str] = set()
        for intent in self.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"):
            if intent.kind != "manager" or intent.mission_id != mission_id:
                continue
            if intent.config.get("task_id"):
                held.add(str(intent.config["task_id"]))
            validated = intent.config.get("validated_fragment")
            if isinstance(validated, Mapping) and validated.get("origin_task_id"):
                held.add(str(validated["origin_task_id"]))
        return held

    def _eligible_validated_fragment_consumers(
        self, summary: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Freeze only unstarted C[A,B] fully covered by accepted F's projection."""
        if summary.get("available") is not True:
            return []
        receipt = self.store.get_receipt(str(summary["projection_receipt_id"]))
        if not isinstance(receipt, Mapping):
            return []
        try:
            allowed = set(
                receipt["projection"]["origin_revision"]["execution_constraints"]["allowed_tools"]
            )
            mapping = tuple(summary["criterion_mapping"])
            if not mapping:
                return []
            origin_id = str(summary["origin_task_id"])
            validation_id = str(summary["validation_task_id"])
            all_tasks = {item.id: item for item in self.store.list_tasks(receipt["mission_id"])}
        except (KeyError, TypeError):
            return []
        eligible = []
        for task in all_tasks.values():
            if (
                task.kind != "work"
                or task.status is not TaskStatus.BLOCKED
                or self.store.list_attempts(task.id)
                or len(task.dependency_ids) != 2
                or origin_id not in task.dependency_ids
                or not all(
                    {item["origin_text"], item["text"]} & set(task.success_criteria)
                    for item in mapping
                )
                or not set(task.allowed_tools) <= allowed
            ):
                continue
            preserved = next(dep for dep in task.dependency_ids if dep != origin_id)
            other = all_tasks.get(preserved)
            if (
                other is None
                or other.kind != "work"
                or preserved == validation_id
                or {origin_id, validation_id}
                & {item.id for item in ancestors(preserved, all_tasks)}
            ):
                continue
            eligible.append({
                "task_id": task.id,
                "old_dependencies": list(task.dependency_ids),
                "preserved_dependency_id": preserved,
                "task_version": task.version,
                "contract_revision": task_contract_revision(_task_contract(task)),
            })
        return eligible

    async def _request_accepted_fragment_management(self, mission: Mission) -> bool:
        """Recoverable trigger after F acceptance; old management triggers are unchanged."""
        if mission.status is not MissionStatus.ACTIVE or not self._config.dynamic_graph:
            return False
        if self._manager_rounds(mission.id) >= int(self.policy_for(mission.id)["max_manager_rounds"]):
            return False
        for row in self.store.list_fragment_validations(mission.id):
            validation = self.store.get_task(row["validation_task_id"])
            if validation is None or validation.status is not TaskStatus.COMPLETED:
                continue
            summary = manager_validated_fragment(self.store, self.commit, validation.id)
            if not self._eligible_validated_fragment_consumers(summary):
                continue
            trigger = f"validated_fragment:{summary['fragment_id']}:{summary['validation_result_id']}"
            subject = f"{mission.id}:manager:{trigger}"
            if self.store.get_intent_for_subject(subject) is not None:
                continue
            accepted_result = self.store.get_result(summary["validation_result_id"])
            if accepted_result is None:
                continue
            created = await self._request_management(
                mission, validation, trigger=trigger,
                result_id=summary["validation_result_id"],
                attempt_id=accepted_result.envelope.attempt_id,
            )
            if created is not None:
                return True
        return False

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

        current_mission = self.store.get_mission(mission.id)
        current_task = self.store.get_task(task.id)
        completed_proposal = (
            current_task is not None
            and current_task.status is TaskStatus.COMPLETED
            and trigger.startswith(("proposed:", "validated_fragment:"))
        )
        if (current_mission is None or current_mission.status in TERMINAL_MISSION
                or current_task is None
                or (current_task.status in TERMINAL_TASK and not completed_proposal)):
            return None
        if self.commit.selection_policy_for(task.id) is not None:
            # Individual candidate failures stay inside their original round.
            # Only its durable, non-deadline empty decision can request one
            # independent fragment review; it never reopens candidate allocation.
            selection = self.commit.selection_round(task.id)
            empty_decision = (None if selection is None or not selection["decision_id"] else
                              self.store.get_receipt(selection["decision_id"]))
            if (selection is None or empty_decision is None or selection["state"] != "DECIDED"
                    or trigger != f"selection_fragment:{selection['round_id']}"
                    or empty_decision.get("action") != "stop"
                    or empty_decision.get("selected_results")
                    or empty_decision.get("reason") != "bounded_candidates_complete"
                    or self.store.now >= selection["deadline_at"]
                    or attempt_id not in selection["attempt_ids"]):
                return None
        if not self._config.dynamic_graph:  # D5-15: the layer's kill switch
            self._note(f"dynamic graph disabled: no management for {task.id} ({trigger})")
            return None
        subject = f"{mission.id}:manager:{trigger}"
        if self._new_mode(mission) is not None:
            # P2.3d / defect D4.  A Manager on a hierarchical Mission can only produce a
            # legacy ``TaskGraphChange``, and ``commit_graph_change`` refuses every one
            # of them (``SEMANTICS_IS_HIERARCHICAL``).  Opening the round anyway spends a
            # model call, a ``max_manager_rounds`` slot and — through the retry it does
            # not produce — a ``no_progress_limit`` slot, so the Mission stops with
            # ``management_exhausted`` or ``no_progress`` instead of with the reason the
            # Task actually failed for.  The refusal belongs here, before the request.
            self.commit.record_management_not_applicable(
                mission.id, task_id=task.id, trigger=trigger, subject=subject
            )
            self._note(
                f"hierarchical mission {mission.id}: no legacy management for {task.id} "
                f"({trigger}); the open door is a plan revision proposal"
            )
            return None
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
        fragment_origin = (
            manager_fragment_origin(self.store, self.commit._source_cas(), result_id)
            if result_id else {"available": False, "reason": "no_result"}
        )
        validated_fragment = None
        if trigger.startswith("validated_fragment:"):
            validated_fragment = manager_validated_fragment(self.store, self.commit, task.id)
            if (
                validated_fragment.get("available") is not True
                or validated_fragment["validation_result_id"] != result_id
                or trigger != f"validated_fragment:{validated_fragment['fragment_id']}:{result_id}"
            ):
                return None
            eligible = self._eligible_validated_fragment_consumers(validated_fragment)
            if not eligible:
                return None
            validated_fragment = {**validated_fragment, "eligible_consumers": eligible}
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
                fragment_origin=fragment_origin,
                validated_fragment=validated_fragment,
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
                "fragment_origin": fragment_origin,
                **({"validated_fragment": validated_fragment} if validated_fragment else {}),
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

    def _validate_validated_fragment_change(
        self, intent: DispatchIntent, change: TaskGraphChange
    ) -> Mapping[str, Any]:
        """One frozen F plus one independent branch may feed an unstarted BLOCKED C."""
        frozen = intent.config.get("validated_fragment")
        if not isinstance(frozen, Mapping) or frozen.get("available") is not True:
            raise ContractError("validated fragment Manager intent has no frozen acceptance")
        if change.base_graph_version != intent.config.get("graph_version"):
            raise ContractError("validated fragment Manager graph base changed")
        change_id = ids.commit_id(
            {"kind": "graph_change", "mission_id": intent.mission_id,
             "proposal": change.proposal_hash}, change.base_graph_version,
        )
        known = self.store.get_receipt(change_id)
        if known is not None:
            source = known.get("source", {})
            if (source.get("intent_id") != intent.intent_id
                    or source.get("fragment_id") != frozen["fragment_id"]
                    or source.get("validation_result_id") != frozen["validation_result_id"]):
                raise ContractError("validated fragment graph receipt has different provenance")
            return frozen
        current = manager_validated_fragment(
            self.store, self.commit, str(frozen["validation_task_id"])
        )
        for key in ("fragment_id", "projection_receipt_id", "validation_task_id",
                    "validation_result_id", "material_refs", "criterion_mapping"):
            if current.get(key) != frozen.get(key):
                raise ContractError("validated fragment acceptance or material changed")
        retargets = [op for op in change.operations if op.op == "retarget_dependencies"]
        cancels = [op for op in change.operations if op.op == "cancel_task"]
        if len(retargets) != 1 or len(cancels) > 1 or len(change.operations) != len(retargets) + len(cancels):
            raise ContractError("validated fragment permits one blocked consumer retarget and origin cancel")
        frozen_tasks = {item["task_id"]: item for item in frozen["tasks"]}
        eligible = {item["task_id"]: item for item in frozen.get("eligible_consumers", ())}
        consumer_id = str(retargets[0].args["task_id"])
        consumer = self.store.get_task(consumer_id)
        old = frozen_tasks.get(consumer_id)
        choice = eligible.get(consumer_id)
        if (choice is None or old is None or old["status"] != "BLOCKED" or old["attempts"] != 0
                or consumer is None or consumer.kind != "work"
                or consumer.status is not TaskStatus.BLOCKED
                or self.store.list_attempts(consumer_id)
                or list(consumer.dependency_ids) != old["dependencies"]
                or list(consumer.dependency_ids) != choice["old_dependencies"]
                or consumer.version != choice["task_version"]
                or task_contract_revision(_task_contract(consumer)) != choice["contract_revision"]):
            raise ContractError("validated fragment consumer is stale or has started")
        if not all(
            {item["origin_text"], item["text"]} & set(consumer.success_criteria)
            for item in frozen["criterion_mapping"]
        ):
            raise ContractError("validated fragment does not cover every mapped criterion")
        receipt = self.store.get_receipt(str(frozen["projection_receipt_id"]))
        if not isinstance(receipt, Mapping):
            raise ContractError("validated fragment projection receipt unavailable")
        allowed = set(receipt["projection"]["origin_revision"]["execution_constraints"]["allowed_tools"])
        if not set(consumer.allowed_tools) <= allowed:
            raise ContractError("validated fragment scope does not cover consumer")
        dependencies = tuple(str(item) for item in retargets[0].args["dependencies"])
        validation_id = str(frozen["validation_task_id"])
        other_id = str(choice["preserved_dependency_id"])
        if (
            len(dependencies) != 2
            or set(dependencies) != {validation_id, other_id}
            or set(choice["old_dependencies"]) != {str(frozen["origin_task_id"]), other_id}
        ):
            raise ContractError("validated fragment must replace only origin A with F")
        other = self.store.get_task(other_id)
        all_tasks = {item.id: item for item in self.store.list_tasks(intent.mission_id)}
        if (other_id not in frozen_tasks
                or other is None or other.kind != "work"
                or other_id in {consumer_id, frozen["origin_task_id"]}
                or {str(frozen["origin_task_id"]), validation_id}
                & {item.id for item in ancestors(other_id, all_tasks)}):
            raise ContractError("validated fragment second branch is not independent")
        if cancels and cancels[0].args["task_id"] != frozen["origin_task_id"]:
            raise ContractError("validated fragment can cancel only its original Task")
        return frozen

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
        current_task = self.store.get_task(task_id)
        completed_proposal = (
            current_task is not None
            and current_task.status is TaskStatus.COMPLETED
            and trigger.startswith(("proposed:", "validated_fragment:"))
        )
        if (mission.status in TERMINAL_MISSION or current_task is None
                or (current_task.status in TERMINAL_TASK and not completed_proposal)):
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            return
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
        fragment_decision = None
        try:
            if result.state is not AgentTurnState.COMMITTED:
                raise ContractError(f"manager turn failed: {jsonable(result.error or {})}")
            has_fragment = re.search(
                rf"<\s*/?\s*{FRAGMENT_VALIDATION_DECISION_TAG}\b", text
            ) is not None
            has_graph = re.search(
                rf"<\s*/?\s*{GRAPH_CHANGE_PROPOSAL_TAG}\b", text
            ) is not None
            if has_fragment and has_graph:
                raise ContractError("Manager cannot mix fragment and graph decisions")
            if trigger.startswith("selection_fragment:") and not has_fragment:
                raise ContractError("exhausted selection allows only independent fragment validation")
            if has_fragment:
                fragment_decision = FragmentValidationDecisionV1.from_json(
                    extract_block(text, FRAGMENT_VALIDATION_DECISION_TAG)
                )
                if fragment_decision.base_graph_version != intent.config.get("graph_version"):
                    raise ContractError("fragment base graph differs from Manager intent")
                validate_manager_fragment_choice(
                    fragment_decision.proposal, intent.config.get("fragment_origin")
                )
            else:
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
        if fragment_decision is not None:
            try:
                receipt = self.commit.commit_fragment_validation(
                    fragment_decision.proposal,
                    command_id=intent.intent_id,
                    base_graph_version=fragment_decision.base_graph_version,
                    source={
                        "intent_id": intent.intent_id,
                        "agent_id": intent.agent_id,
                        "turn_id": result.turn_id,
                    },
                    limits=limits,
                    selection_round_id=(trigger.removeprefix("selection_fragment:")
                                        if trigger.startswith("selection_fragment:") else None),
                )
            except SelectionFragmentExpired:
                self._settle_intent(intent, "FAILED")
                self._settle_service_if_known(intent.subject_id, mission.id)
                self.commit.stop_selection(task_id, reason="selection_fragment_expired")
                await self._release_mission(mission.id)
                return
            except (ContractError, CommitRejected, GraphChangeRejected, BudgetError) as error:
                self.commit.record_management_decided(
                    mission.id, task_id=task_id, trigger=trigger,
                    decision="rejected", detail={"error": str(error), "kind": "fragment_validation"},
                )
                self._settle_intent(intent, "SETTLED")
                self._settle_service_if_known(intent.subject_id, mission.id)
                await self._enforce_no_progress(mission, task)
                return
            self._fault("after_fragment_commit", "manager")
            self.commit.record_management_decided(
                mission.id, task_id=task_id, trigger=trigger,
                decision="fragment_validation",
                detail={
                    "fragment_id": receipt["fragment_id"],
                    "validation_task_id": receipt["validation_task_id"],
                    "graph_change_id": receipt["graph_change_id"],
                },
            )
            self._settle_intent(intent, "SETTLED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            return
        validated = None
        if trigger.startswith("validated_fragment:"):
            try:
                validated = self._validate_validated_fragment_change(intent, change)
            except ContractError as error:
                self.commit.record_management_decided(
                    mission.id, task_id=task_id, trigger=trigger,
                    decision="rejected", detail={"error": str(error), "kind": "validated_fragment"},
                )
                self._settle_intent(intent, "SETTLED")
                self._settle_service_if_known(intent.subject_id, mission.id)
                return
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
                    **({"fragment_id": validated["fragment_id"],
                        "validation_result_id": validated["validation_result_id"]}
                       if validated is not None else {}),
                },
                limits=limits,
                allow_rebase=validated is None,
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
            if validated is None and retry < 1:  # D5-10 / S5-08: old retry semantics
                await self._request_management(
                    mission,
                    task,
                    trigger=f"{trigger}:retry-{retry + 1}",
                    result_id=intent.config.get("result_id"),
                    attempt_id=intent.config.get("attempt_id"),
                )
            elif validated is None:
                await self._enforce_no_progress(mission, task)
            return
        if validated is not None:
            self._fault("after_validated_fragment_graph_commit", "manager")
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
        mission_source_binding: Mapping[str, Any] | None = None,
    ) -> CriticVerdict:
        from ..verification.critics import critic_schema_retry_feedback

        task_id = None if task is None else task.id
        last_error: ContractError | None = None
        for ordinal in range(1, MAX_CRITIC_ATTEMPTS + 1):
            current_mission = self.store.get_mission(mission.id)
            current_task = None if task_id is None else self.store.get_task(task_id)
            current_attempt = None if attempt_id is None else self.store.get_attempt(attempt_id)
            if (
                current_mission is None
                or current_mission.status in TERMINAL_MISSION
                or (current_task is not None and current_task.status in TERMINAL_TASK)
                or (current_attempt is not None and current_attempt.status in TERMINAL_ATTEMPT)
            ):
                raise CommitRejected("Critic subject stopped before dispatch")
            subject = f"{subject_prefix}:{ordinal}"
            intent = self.store.get_intent_for_subject(subject)
            if intent is not None:
                self._validate_mission_judge_intent(intent)
            else:
                template = self._template(CRITIC, mission.id)
                copy = self.assembled.workspaces.verification_view(view_id)
                source_attempt = None if attempt_id is None else self.store.get_attempt(attempt_id)
                source_binding = (
                    {} if source_attempt is None else self._frozen_source_binding(source_attempt)
                )
                untrusted = [
                    str(p) for p in (mission.final_report or {}).get("untrusted_sources", [])
                ]
                if mission_source_binding is not None:
                    source_binding = dict(mission_source_binding)
                    untrusted = sorted(set(untrusted) | set(source_binding["untrusted_sources"]))
                if source_binding:
                    untrusted = sorted(
                        set(untrusted) | set(source_binding.get("source_versions", {}))
                    )
                try:
                    package = build_critic_package(
                        mission,
                        task,
                        attempt_id=view_id,
                        artifacts=[
                            {
                                "path": a.path,
                                "content_hash": a.content_hash,
                                "size_bytes": a.size_bytes,
                            }
                            for a in artifacts
                        ],
                        test_output=test_output,
                        workspace_files=copy.list_files(),
                        knowledge=self._knowledge_or_unavailable(mission, task),
                        visibility="critic"
                        if task is not None and task.kind == "conflict"
                        else "verifier",
                        domain=self.commit.domain_for(mission.id),
                        source_versions=source_binding.get("source_versions"),
                        mission_source_catalog=source_binding.get("mission_source_catalog"),
                        feedback=critic_schema_retry_feedback(
                            last_error, prompt_version=template.prompt_version
                        ),
                    )
                except ContextRejected as error:
                    raise ContractError(f"critic package refused: {error}") from error
                try:
                    decision = self._route_service("critic", mission.id)
                except RoutingUnavailable as unavailable:
                    # review P0-1: an unavailable Critic makes the layer an ERROR (never a PASS)
                    raise ContractError(
                        f"critic runtime profile {unavailable.profile_id!r} unavailable"
                    ) from unavailable
                selection_deadline = (None if attempt_id is None or self.store.get_attempt(attempt_id) is None
                                      else self.commit.selection_deadline(attempt_id))
                remaining_selection = (self._config.turn_deadline_seconds if selection_deadline is None
                                       else selection_deadline - self.store.now)
                if remaining_selection <= 0:
                    raise ContractError("selection deadline elapsed before Critic")
                config = AgentConfig(
                    name=f"critic-{ordinal}",
                    instructions=template.instructions,
                    model_profile_ref=decision.profile_id,
                    tool_names=template.tool_names,
                    limits=AgentLimits(
                        max_model_calls_per_turn=SYSTEM_CRITIC_MODEL_CALLS,
                        max_tool_calls_per_turn=24,
                        turn_deadline_seconds=min(self._config.turn_deadline_seconds, remaining_selection),
                    ),
                )
                message = user_message_json(package.text)
                first_cap = None
                first_output_ceiling = None
                first_reservation = None
                first_unknown = None
                critic_tokens = self._config.critic_reserve_tokens
                if ordinal == 1 and attempt_id is not None and task_id is not None:
                    worker_intent = self.store.get_intent_for_subject(attempt_id)
                    if worker_intent is None:
                        raise ContractError("FIRST Critic has no original Worker intent")
                    frozen_first = worker_intent.config.get("first_critic_budget")
                    if frozen_first is not None:
                        if not isinstance(frozen_first, Mapping):
                            raise ContractError("frozen FIRST Critic budget is malformed")
                        try:
                            first_cap = ProviderInputCap.from_json(frozen_first.get("provider_input_cap"))
                        except (TypeError, ValueError) as error:
                            raise ContractError("frozen FIRST Critic input cap is malformed") from error
                        actual_first = self._first_critic_budget(decision)
                        if (not isinstance(actual_first, FirstRequestBudget)
                                or actual_first.provider_input_cap != first_cap
                                or frozen_first.get("output_ceiling") != actual_first.output_ceiling
                                or frozen_first.get("minimum_tokens") != actual_first.minimum_tokens
                                or frozen_first.get("cost_micros") != self._first_critic_reservation(
                                    actual_first, decision.profile_id).cost_micros):
                            raise ContractError("FIRST Critic route or cap differs from protected tail")
                        first_reservation = self._first_critic_reservation(actual_first, decision.profile_id)
                        first_output_ceiling = actual_first.output_ceiling
                    else:
                        first_unknown = worker_intent.config.get(
                            "first_critic_budget_unknown", "original_intent_has_no_first_cap"
                        )
                first_fields: dict[str, Any] = {}
                if first_cap is not None:
                    assert first_reservation is not None
                    first_fields = {
                        "provider_input_cap": first_cap.to_json(),
                        "provider_output_ceiling": first_output_ceiling,
                        "provider_first_cost_micros": first_reservation.cost_micros,
                    }
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
                        **source_binding,
                        "untrusted_sources": untrusted,
                        **self._service_config(decision),
                        **first_fields,
                        **({"first_critic_budget_unknown": first_unknown}
                           if first_unknown is not None else {}),
                    },
                    reservation=(first_reservation if first_reservation is not None else
                                 self._system_reservation(
                                     critic_tokens, decision.profile_id, SYSTEM_CRITIC_MODEL_CALLS,
                                 ) if task_id is not None and self.commit.system_task_hold(task_id) is not None
                                 else self._reservation(critic_tokens, decision.profile_id)),
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
            selection_deadline = (None if attempt_id is None or self.store.get_attempt(attempt_id) is None
                                  else self.commit.selection_deadline(attempt_id))
            deadline = min(self.store.now + self._critic_wait,
                           selection_deadline if selection_deadline is not None else float("inf"))
            intent, result = await self._await_service_turn(
                intent, deadline, attempt_id=attempt_id
            )
            if self._critic_subject_stopped(intent):
                await self._collect_after_stop(intent)
                raise CommitRejected("Critic subject stopped before verdict collection")
            if result is None:
                # An unanswered SDK turn is not a malformed *completed* verdict.
                # Keep SUBMITTED for after-stop collection, request cooperative
                # cancellation and fail this verification without a new ordinal.
                # Do not freeze a running invocation's provisional zero usage
                # into the append-only imported_usage ledger.
                self.assembled.gateway.unbind(intent.agent_id)
                await self._cancel_turn(intent)
                raise ContractError("critic did not answer within the wait window")
            self._import_usage(intent)
            self.assembled.gateway.unbind(intent.agent_id)
            critic_turn_error = result.error
            critic_admission_detail = (
                critic_turn_error.get("detail") if isinstance(critic_turn_error, Mapping) else None
            )
            if (result.state is not AgentTurnState.COMMITTED
                    and isinstance(critic_turn_error, Mapping)
                    and critic_turn_error.get("error_code") == "provider_admission_denied"
                    and critic_turn_error.get("source_kind") == "provider_admission"
                    and critic_turn_error.get("retryable") is False
                    and isinstance(critic_admission_detail, Mapping)
                    and type(critic_admission_detail.get("schema_version")) is int
                    and critic_admission_detail["schema_version"] == 1):
                self._note_turn_health(intent, result)
                self._settle_intent(intent, "FAILED")
                self._settle_service_if_known(subject, mission.id, task_id)
                # Outside the schema-retry catch below. A cold collector reads
                # the same SDK failure and takes the same non-retry path.
                raise _CriticAdmissionFailure(critic_turn_error)
            try:
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
            domain = self.commit.domain_for(mission.id)
            if task is not None and requires_document_critic_proof(domain):
                # The actual SDK COMMITTED output, not the mutable layer row, is
                # the durable verdict authority. Receipt + settlement are atomic.
                assert result is not None
                self.commit.settle_critic_verdict(intent.intent_id, result=result)
            else:
                self._settle_intent(intent, "SETTLED")
            self._settle_service_if_known(subject, mission.id, task_id)
            return verdict
        assert last_error is not None
        raise last_error

    async def _await_service_turn(  # type: ignore[no-untyped-def]
        self, intent: DispatchIntent, deadline: float, *, attempt_id: str | None
    ):
        """Dispatch a Critic intent and wait for its turn (the Critic runner's loop).

        Returns ``(intent, result)``; ``result`` is ``None`` when the wait window
        closed — or, P2.3f, when the turn was waiting on an unknown Provider outcome,
        was re-handed off once and was unknown again — and the caller's own
        "did not answer" door takes it from there.  Raises ``CommitRejected`` when the
        Critic's subject stopped meanwhile (the after-stop collector owns it then).
        """

        intent = await self._dispatch_until_submitted(intent, deadline)
        result = None
        while self.store.now < deadline:
            if self._critic_subject_stopped(intent):
                # The cycle's after-stop collector owns cancellation and cost
                # settlement from here, including after a process restart.
                await self._collect_after_stop(intent)
                raise CommitRejected("Critic subject stopped during verification")
            assert intent.agent_id and intent.expected_turn_id
            result = await self.bridge_for(intent).result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
            if result is not None:
                break
            # P2.3f: a Critic turn waiting on an unknown Provider outcome is not "the
            # Critic thinking"; it is a question nobody is answering.  Same bound and
            # same two steps as a Planner turn — re-hand-off once, then the caller's
            # own "did not answer" door (``result is None``).
            liveness = await self.bridge_for(intent).liveness(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
            outcome = await self._resolve_provider_blocked_service(intent, liveness)
            if outcome == "rehandoff":
                refreshed = self.store.get_intent(intent.intent_id)
                assert refreshed is not None
                intent = await self._dispatch_until_submitted(refreshed, deadline)
                continue
            if outcome == "give_up":
                break
            if attempt_id is not None:
                self._hold_lease(attempt_id)  # P1-3: keep the lease while the Critic thinks
            await asyncio.sleep(self._poll)
        return intent, result

    async def _dispatch_until_submitted(
        self, intent: DispatchIntent, deadline: float
    ) -> DispatchIntent:
        """Drive one service intent through create + submit (the Critic runner's loop)."""

        while intent.state in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
            if not await self._dispatch(intent):  # another owner holds the claim (P1-8)
                if self.store.now >= deadline:
                    raise ContractError("critic intent is claimed elsewhere; wait window over")
                await asyncio.sleep(self._poll)
            refreshed = self.store.get_intent(intent.intent_id)
            assert refreshed is not None
            intent = refreshed
        assert intent.agent_id and intent.expected_turn_id
        return intent

    # ------------------------------------------- P2.3f: unknown Provider outcomes
    @property
    def _service_blocker_limit(self) -> float:
        return min(float(self._config.stall_seconds), MAX_SERVICE_BLOCKER_SECONDS)

    @staticmethod
    def _provider_blocked(liveness: Liveness) -> bool:
        """The turn is waiting on a Provider hand-off whose outcome is unknown.

        Only the run's own *wait blocker* of kind ``provider`` counts.  The bridge also
        reports ``provider_slot_wait`` (queued behind the concurrency limit) and
        ``provider_response_wait`` (a call that is genuinely in progress) as
        ``blocked``; both are the executor making progress and neither is timed here.
        """

        blocker = liveness.blocker
        return (
            liveness.exists
            and not liveness.settled
            and isinstance(blocker, Mapping)
            and str(blocker.get("kind", "")) == "provider"
        )

    async def _resolve_provider_blocked_service(
        self, intent: DispatchIntent, liveness: Liveness
    ) -> str | None:
        """End the one wait the executor cannot end: a hand-off with an unknown outcome.

        P2.3f, found by the P2.3e probe episode.  The runtime hands a request off, the
        transport fails 0.2 s later, and — correctly — the invocation is settled
        UNKNOWN rather than FAILED: the request may have reached the model, and
        replaying it blindly would be a second charge for the same question.  The run
        then waits for a reconciliation observation that this deployment has nobody to
        make, and the intent stayed SUBMITTED until the caller's deadline (1800 s in
        the acceptance runner) with the Mission at PLANNING and nothing written down.

        The loop's answer is bounded and durable: after :attr:`_service_blocker_limit`
        seconds on the same blocker, the request is handed off **once more** to a new
        executor (:data:`MAX_SERVICE_REHANDOFFS`, recorded as
        ``ServiceIntentRehandedOff``); if that one is unknown too, the round ends
        through the role's own failure door — a Planner round is rejected with
        ``provider_outcome_unknown`` and the ladder decides, a MethodSynthesizer round
        is recorded ``UNANSWERED`` and the synthesis wait ends, a root review is
        recorded unreadable, and a Critic turn is handed back to its runner's own
        "did not answer" path.  The abandoned turn's charge stays unknown in the
        runtime ledger and keeps the reservation held, which is the honest count.

        Hierarchical Missions only.  A legacy Mission's Planner and Critic paths are
        pinned byte for byte by the recovery matrix and the event goldens, and the
        acceptance programme that needs this is the hierarchical one; widening it is a
        decision about the legacy path that this slice does not make.

        Returns ``None`` (keep waiting), ``"rehandoff"`` (a new executor is about to
        be dispatched) or ``"give_up"`` (the round was ended, or — for a Critic — is
        the runner's to end).
        """

        key = f"{intent.intent_id}:{intent.replays}"
        auth_blocked = self._definite_auth_failure(liveness.blocker)
        if not self._provider_blocked(liveness) and not auth_blocked:
            self._service_blocked_since.pop(key, None)
            return None
        mission = self.store.get_mission(intent.mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            return None
        new_mode = self._new_mode(mission)
        if new_mode is None:
            return None  # legacy: the executor's wait is the executor's, unchanged
        if auth_blocked:
            await self._give_up_blocked_plan_intent(
                intent,
                mission,
                new_mode,
                detail={"blocker": dict(liveness.blocker or {}), "auth": True, "rehandoffs": 0},
            )
            return "give_up"
        now = self.store.now
        since = self._service_blocked_since.get(key)
        if since is None:
            self._service_blocked_since[key] = now
            self._note(
                f"{intent.subject_id}: turn {intent.expected_turn_id} is waiting on an unknown "
                f"Provider outcome; acting in {self._service_blocker_limit:.0f}s"
            )
            return None
        waited = now - since
        if waited < self._service_blocker_limit:
            return None
        self._service_blocked_since.pop(key, None)
        detail = {
            "waited_seconds": round(waited, 3),
            "limit_seconds": self._service_blocker_limit,
            "blocker": dict(liveness.blocker or {}),
        }
        done = self.commit.rehandoffs_of(intent.subject_id, intent.mission_id)
        if done < MAX_SERVICE_REHANDOFFS:
            assert intent.agent_id is not None
            self.assembled.gateway.unbind(intent.agent_id)
            await self._cancel_turn(intent)  # advisory: the waiting run has no loop to stop
            self._release_unknown_grants(intent)
            self.commit.rehandoff_service_intent(
                intent.intent_id,
                owner=self._owner,
                lease_seconds=self._config.lease_seconds,
                reason="provider_outcome_unknown",
                detail=detail,
            )
            self._note(
                f"{intent.subject_id}: unknown Provider outcome for {waited:.0f}s; handed off "
                f"once more (re-hand-off {done + 1} of {MAX_SERVICE_REHANDOFFS})"
            )
            return "rehandoff"
        if intent.kind == "critic":
            self._note(
                f"{intent.subject_id}: unknown Provider outcome again after {done} re-hand-off(s); "
                "the Critic runner ends the wait"
            )
            return "give_up"
        await self._give_up_blocked_plan_intent(
            intent, mission, new_mode, detail={**detail, "rehandoffs": done}
        )
        return "give_up"

    async def _give_up_blocked_plan_intent(
        self,
        intent: DispatchIntent,
        mission: Mission,
        new_mode: HierarchicalDispatch,
        *,
        detail: Mapping[str, Any],
    ) -> None:
        """The second unknown outcome ends the round through the role's own door."""

        from ..planning.htn.registry import RegistryAuthor

        role = str(intent.config.get("role", ""))
        self._release_unknown_grants(intent)
        self._import_usage(intent)  # facts of the executor that did answer, if any
        self._settle_intent(intent, "FAILED")
        self._settle_service_if_known(intent.subject_id, mission.id)
        self._note(
            f"{intent.subject_id}: unknown Provider outcome again after "
            f"{detail.get('rehandoffs')} re-hand-off(s); the round ends"
        )
        if role == "method_synthesizer":
            goal_task_id = str(intent.config.get("goal_task_id", ""))
            new_mode.record_synthesis_outcome(
                mission.id,
                goal_task_id=goal_task_id,
                admitted=False,
                problems=(
                    f"provider_outcome_unknown after {detail.get('rehandoffs')} re-hand-off(s)",
                ),
                method_id="",
                verdict="UNANSWERED",
                author=str(RegistryAuthor.MODEL),
                asks=int(intent.config.get("ordinal", 1)),
            )
            await self._after_synthesis_round(mission.id, admitted=False)
            return
        if role == "root_reviewer":
            coordinator = self._root_review(mission, new_mode)
            package_id = str(intent.config.get("review_package_id", ""))
            try:
                package = coordinator.semantics.get_review_package(package_id)
            except StoreError as error:
                self._note(f"root review re-hand-off names no stored package ({error})")
                return
            coordinator.record_unreadable(
                mission.id,
                package,
                detail=f"provider_outcome_unknown after {detail.get('rehandoffs')} re-hand-off(s)",
                reviewer_turn_id=intent.expected_turn_id or "",
            )
            return
        await self._planning_rejected(
            intent, reason="provider_outcome_unknown", detail=dict(detail)
        )

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
            and not self.commit.candidate_is_waiting(stored.envelope.id)
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

    async def _selection_fragment_recovery(
        self, mission: Mission, task: Task, selection: Mapping[str, Any],
        decision: Mapping[str, Any],
    ) -> bool | None:
        """None means stop; False waits without claiming progress; True dispatched.

        The original round remains DECIDED and retains its cost, deadline and
        candidate cap. Only a separately accepted F and normal Manager graph
        commit can replace a blocked downstream dependency and cancel this A.
        """
        if (not self._config.dynamic_graph
                or decision.get("reason") != "bounded_candidates_complete"
                or self.store.now >= selection["deadline_at"]):
            return None
        trigger = f"selection_fragment:{selection['round_id']}"
        subject = f"{mission.id}:manager:{trigger}"
        intent = self.store.get_intent_for_subject(subject)
        if intent is None:
            # Stable original candidate order; one opportunity for this round,
            # never one Manager retry per failed candidate or per scheduler tick.
            for attempt_id in selection["attempt_ids"]:
                result = self.store.find_result_for_attempt(attempt_id)
                if result is None or result.verdict != "FAIL":
                    continue
                catalog = manager_fragment_origin(
                    self.store, self.commit._source_cas(), result.envelope.id,
                )
                if catalog.get("available") is not True:
                    continue
                try:
                    requested = await self._request_management(
                        mission, task, trigger=trigger, result_id=result.envelope.id,
                        attempt_id=attempt_id,
                    )
                except BudgetExhausted as error:
                    self.commit.stop_task(
                        task.id, stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
                        detail={"reason": "selection_fragment_management_unfunded",
                                "selection_round_id": selection["round_id"], "error": str(error)},
                    )
                    await self._release_mission(mission.id)
                    return True
                return True if requested is not None else None
            return None
        if intent.state in {"PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"}:
            return False
        if task.id in self._tasks_under_management(mission.id):
            return False  # F accepted; its frozen downstream graph decision is in flight.
        for receipt in self.store.list_fragment_validations(mission.id):
            if receipt.get("origin", {}).get("result_id") != intent.config.get("result_id"):
                continue
            validation = self.store.get_task(receipt["validation_task_id"])
            if validation is not None and validation.status not in TERMINAL_TASK:
                return False
        return None  # Invalid/no fragment, failed F, or rejected downstream patch: bounded stop.

    async def _drive_selection(self, mission: Mission, task: Task) -> bool:
        """Advance one bounded round; waiting candidates never impersonate running turns."""
        if task.status in TERMINAL_TASK or task.status is TaskStatus.BLOCKED or task.paused:
            return False
        selection = self.commit.selection_round(task.id)
        if selection is None:
            try:
                self.commit.begin_selection_round(task.id, command_id="round:" + task.id)
            except BudgetExhausted:
                self.commit.stop_task(task.id, stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
                                      detail={"reason": "selection_tail_unavailable"})
            return True
        if selection["state"] in {"COMMITTED", "EXHAUSTED", "INVALIDATED"}:
            return False
        # Takeover uses the original Attempt lease CAS. Never cancel another live owner.
        candidates = self.commit.selection_candidates(task.id)
        for candidate in candidates:
            if candidate["state"] != "READY":
                continue
            attempt = self.store.get_attempt(candidate["attempt_id"])
            if attempt is None or attempt.status in TERMINAL_ATTEMPT:
                continue
            # A past deadline forbids new exploration/C, but an expired READY
            # lease may still be acquired solely to finish best_complete_else_stop.
            if candidate["state"] == "READY":
                try:
                    self.commit.renew_lease(
                        attempt.id, owner=self._owner, lease_seconds=self._config.lease_seconds,
                        minimum_remaining_seconds=self._config.lease_seconds / 2,
                        liveness={"progress": attempt.progress_marker,
                                  "phase": "selection_finalize" if self.store.now >= selection["deadline_at"]
                                  else "selection_wait"},
                    )
                except CommitRejected:
                    return False
        try:
            decision = self.commit.decide_selection(
                task.id, owner=self._owner,
                command_id=f"decision:{selection['round_id']}:{selection['version']}",
                connectors=self._connectors, deployment=self._config.deployment_policy,
            )
        except CommitRejected:
            if self.store.now >= selection["deadline_at"]:
                self.commit.stop_selection(task.id, reason="selection_deadline_unavailable")
                await self._release_mission(mission.id)
                return True
            raise
        if decision is None:
            return False
        selection = self.commit.selection_round(task.id)
        assert selection is not None
        if decision["action"] == "stop":
            recovery = await self._selection_fragment_recovery(mission, task, selection, decision)
            if recovery is not None:
                return recovery
            self.commit.stop_selection(task.id, reason="selection_no_eligible_complete_candidate")
            await self._release_mission(mission.id)
            return True
        if decision["action"] == "synthesize" and selection["synthesis_attempt_id"] is None:
            current_task = self.store.get_task(task.id)
            assert current_task is not None
            return await self._next_attempt(
                mission, current_task, self.store.list_attempts(task.id),
                selection_decision=decision,
            )
        if decision["action"] == "synthesize":
            result = self.store.find_result_for_attempt(selection["synthesis_attempt_id"])
            if result is None or not self.commit.candidate_is_waiting(result.envelope.id):
                return False
            result_id = result.envelope.id
        else:
            result_id = decision["selected_results"][0]
        receipt = self.commit.accept_selected_result(
            result_id, round_id=selection["round_id"], decision_id=decision["receipt_id"],
            expected_round_version=selection["version"], owner=self._owner,
            command_id="selection-accept:" + result_id,
            connectors=self._connectors, deployment=self._config.deployment_policy,
        )
        if receipt.get("accepted"):
            for sibling in self.store.list_attempts(task.id):
                if sibling.status is AttemptStatus.SUPERSEDED:
                    await self._release_attempt(sibling.id, cancel=True)
        return True

    async def _decide(self, mission: Mission) -> bool:
        # Review F1, before anything else: a hierarchical Mission on a deployment with
        # no assembly is not scheduled, not judged and not handed to ``allocate()``.
        if self._assembly_missing(mission, at="decide"):
            return False
        tasks = self.store.list_tasks(mission.id)
        if not tasks or mission.status is not MissionStatus.ACTIVE:
            return False
        if await self._request_accepted_fragment_management(mission):
            return True
        if any(t.paused and t.pause_reason == "provider_admission:usage_unresolved" for t in tasks):
            # Read the SDK's actual reconciliation records before importing and
            # settling; elapsed time and a new owner are not evidence of zero cost.
            if self._provider_admission is not None:
                for pool in self.assembled.pools.values():
                    guard = self._admission_for(pool.profile.profile_id)
                    if guard is not None:
                        guard.recover(pool.bridge.runtime.uow)
            self._reimport_unsettled(mission)
            resumed = any(self.commit.resume_admission_usage(t.id) for t in tasks
                          if t.paused and t.pause_reason == "provider_admission:usage_unresolved")
            if resumed:
                return True
        live = [  # D5-4 / R4: superseded work is history and a paused route is not required
            t
            for t in tasks
            if t.status is not TaskStatus.CANCELLED
            and not (t.paused and t.status in {TaskStatus.READY, TaskStatus.BLOCKED})
        ]
        # P2.3b / TG §7: in the new mode "everything is done" is read from the
        # projection and the Resolutions, never from a sweep of ``TaskStatus`` — and
        # the root review does not run until every gating child has been accepted.
        new_mode = self._new_mode(mission)
        if new_mode is not None:
            # P2.3l / N7: form inner GoalResolutions before asking who is ready to
            # dispatch.  ORDER successors of a nested compound stay WAITING_ORDER
            # until the compound is ACCEPTED, which only a GoalResolution can say.
            try:
                new_mode.advance_compound_phases(mission.id)
                from .composition_review import CompositionAcceptanceAssembly

                CompositionAcceptanceAssembly(
                    self.store,
                    self.commit,
                    dispatch=new_mode,
                    issued_by=self._owner,
                ).resolve_ready(mission.id)
            except (GraphIntegrityError, ContractError, StoreError) as error:
                self._note(
                    f"mission {mission.id}: inner composition review deferred ({error})"
                )
        try:
            settled = (
                new_mode.root_review_ready(mission.id)
                if new_mode is not None
                else bool(live) and all(task.status is TaskStatus.COMPLETED for task in live)
            )
        except GraphIntegrityError as error:
            await self._plan_integrity_stop(mission, error)
            return True
        if settled:
            current = self.store.get_mission(mission.id)  # not the cycle's stale snapshot
            if current is None or current.status is not MissionStatus.ACTIVE:
                return False
            # P2.3c part 3a: the root ``MISSION_FINAL`` review is *cut and reviewed*
            # before the resolution is offered.  Part 2d's smoke stopped exactly here
            # — ``ROOT_REVIEW_PACKAGE_MISSING`` — because nothing in ``src`` produced
            # the anchor ``root_resolution_inputs`` reads.  The coordinator is
            # deliberately a separate step and not folded into the trigger: the
            # trigger reads anchors and never writes them, and a review that writes
            # its own conclusion is the shape §21.5 exists to forbid.
            if new_mode is not None and await self._advance_root_review(current, new_mode):
                return True
            if new_mode is not None and not await self._root_resolution_formed(current, new_mode):
                # §21.5 hard invariant, "wrongly declared complete = 0": a hierarchical
                # Mission reaches COMPLETED only *after* its root GoalResolution is
                # formed, and that resolution is formed only by
                # ``commit_goal_resolution`` — out of the success formula, the final
                # acceptance and the delivery contract, never out of "every gating
                # child was accepted" (which is what ``settled`` says) and never out of
                # the Mission's own status string (§6.3, §8.1).  When it cannot be
                # formed the Mission stays ACTIVE with the refusal recorded, which is a
                # Mission that is visibly not finished rather than one wrongly declared
                # finished.  ``False`` and not ``True``: nothing moved, so the loop goes
                # idle instead of re-offering a resolution that is refused for the same
                # reason forever.
                return False
            try:
                if any(c.startswith(ACTION_PREFIX) for c in current.success_criteria):
                    return await self._decide_actions(current, live)  # D7-7' two-stage judgment
                return await self._judge(current, live)
            except ContractError as error:
                if not requires_mission_source_binding(self.commit.domain_for(current.id)):
                    raise
                self.commit.fail_mission(
                    current.id,
                    stop_reason=MissionStopReason.VERIFIER_UNAVAILABLE,
                    detail={"source_assessment_error": str(error)},
                )
                await self._release_mission(current.id)
                return True
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
        compare_tasks = [t for t in tasks if self.commit.selection_policy_for(t.id) is not None]
        for search_task in compare_tasks:
            if await self._drive_selection(mission, search_task):
                return True
        # Completed compare predecessors are still allocator dependency facts.
        # Removing them here makes a READY downstream Task appear blocked forever.
        tasks = [t for t in tasks if t.status in TERMINAL_TASK or t not in compare_tasks or (
            (selection := self.commit.selection_round(t.id)) is not None
            and selection["state"] == "COLLECTING"
            and len(selection["attempt_ids"]) < selection["policy"]["max_candidates"]
        )]
        pending = self._tasks_under_management(mission.id)
        if pending:  # D5-6: no new Attempt while the Manager decides about the Task
            tasks = [t for t in tasks if t.id not in pending]
        bound = self.policy_for(mission.id)  # step 9 (plan D9-4'): the Mission's own version
        # P2.3c part 2 / §18.5 constraint 4 / §24.1 decision 6: a hierarchical Mission
        # allocates over *admissions*, never over the READY string.  P2.3b only had the
        # form gate here, so a DATA consumer whose producer had not been accepted was
        # dispatched with no inputs and ran anyway; ``allocate_v2`` takes only records
        # ``admit_for_dispatch`` built out of a READY_CANDIDATE readiness report, so an
        # occurrence waiting on data, evidence, an approval or a refinement is withheld
        # with a named reason instead of quietly running.  The legacy entry is untouched
        # and stays the entry for a Mission that has no semantic bindings.
        admissions: DispatchAdmissions | None = None
        # One name, two plan shapes: the legacy ``AllocationPlan`` grants ``Task``
        # objects and ``AllocationPlanV2`` grants ``EligiblePrimitiveTask`` admissions.
        # ``granted_ids`` is where the two meet, and it is a list of *ids* on purpose —
        # the row is re-read inside the loop anyway, so carrying either object past
        # this point would only invite one branch to read the other's fields.
        plan: AllocationPlan | AllocationPlanV2
        granted_ids: list[tuple[str, int]]
        if new_mode is not None:
            try:
                # P2.3c part 2b: re-read every acceptance a declared DATA edge rests on
                # and record the licence to bind it (I19: recompute rather than reuse
                # the old TRUE).  It runs *before* the readiness read because the
                # resolver looks the witness up by acceptance id; issuing it afterwards
                # would leave the consumer in WAITING_DATA for one whole cycle after its
                # producer was accepted.
                new_mode.issue_input_witnesses(
                    mission.id,
                    new_mode.network(mission.id),
                    now_ms=int(self.store.now * 1000),
                )
                # P2.3c part 2c: the same act on the START-precondition lane, and for
                # the same reason.  A leaf under a gated method inherits its parent
                # method's ``applicable_when`` as a SELECT precondition, and TG §9
                # refuses to dispatch it without a purpose=START witness — which
                # nothing issued, so the real-model smoke committed a plan and then
                # withheld every leaf with ``witness_missing`` for ever.
                new_mode.issue_start_witnesses(
                    mission.id,
                    new_mode.network(mission.id),
                    now_ms=int(self.store.now * 1000),
                )
                admissions = new_mode.admissions(mission.id)
            except GraphIntegrityError as error:
                await self._plan_integrity_stop(mission, error)
                return True
            new_mode.record_withheld(mission.id, admissions)
            plan = allocate_v2(
                tasks,
                attempts,
                admissions.bindings,
                admissions.readiness,
                concurrency_limit=min(
                    int(bound["mission_concurrency"]), self._config.max_concurrency
                ),
                candidates_per_task=int(bound["candidates_per_task"]),
                now=self.store.now,
                aging_window_seconds=float(bound["aging_window_seconds"]),
                mission_max_tokens=mission.budget.max_tokens,
                pressure=self._pressure,
                reduced_concurrency_ratio=self._config.reduced_concurrency_ratio,
                exploration_slots=int(bound["exploration_slots"]),
                weights=bound["allocator_weights"],
                waiting_attempt_ids=self.commit.selection_waiting_ids(),
            )
            granted_ids = [(str(item.task_id), n) for item, n in plan.grants]
        else:
            plan = allocate(
                tasks,
                attempts,
                concurrency_limit=min(
                    int(bound["mission_concurrency"]), self._config.max_concurrency
                ),
                candidates_per_task=int(bound["candidates_per_task"]),
                now=self.store.now,
                aging_window_seconds=float(bound["aging_window_seconds"]),
                mission_max_tokens=mission.budget.max_tokens,
                pressure=self._pressure,  # D6-3: the gate outside the §29.3 formula
                reduced_concurrency_ratio=self._config.reduced_concurrency_ratio,
                exploration_slots=int(bound["exploration_slots"]),
                weights=bound["allocator_weights"],
                waiting_attempt_ids=self.commit.selection_waiting_ids(),
                selection_task_ids=frozenset(t.id for t in compare_tasks),
            )
            granted_ids = [(granted.id, candidate) for granted, candidate in plan.grants]
        progressed = False
        for task_id, _candidate in granted_ids:
            current_task = self.store.get_task(task_id)
            assert current_task is not None
            task = current_task
            if task.status in TERMINAL_TASK:
                continue
            score = plan.scores.get(task.id)
            if await self._next_attempt(
                mission,
                task,
                self.store.list_attempts(task.id),
                admission=None if admissions is None else admissions.admission_for(task.id),
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

    def _root_review(self, mission: Mission, new_mode: HierarchicalDispatch) -> Any:
        """The deployment's root-review coordinator for this Mission.

        Built per call rather than held: it carries no Mission state, and a held one
        would outlive the store handle a restart replaces.
        """

        from .root_review import RootReviewCoordinator

        return RootReviewCoordinator(
            self.store,
            self.commit,
            new_mode,
            scope_id="mission",
            issued_by=self._owner,
            max_cuts_per_revision=self._config.max_root_review_cuts,
        )

    async def _advance_root_review(
        self, mission: Mission, new_mode: HierarchicalDispatch
    ) -> bool:
        """Move the root ``MISSION_FINAL`` review one step, or say why it cannot.

        ``True`` means *something happened* — a package was cut, a reviewer was
        asked — and the cycle counts as progress.  ``False`` means the review needs
        nothing from this loop, which is true both when it is ``READY`` (the caller
        goes on to offer the resolution) and when it is stuck: a stuck review is left
        for part 2d's idle-stall path, which records every gate still holding the
        Mission and stops it after one confirming cycle, rather than being retried
        here every cycle on the Mission's own account.
        """

        from .root_review import RootReviewStatus

        coordinator = self._root_review(mission, new_mode)
        try:
            state = coordinator.state(mission.id)
        except (GraphIntegrityError, ContractError, StoreError) as error:
            self._note(f"mission {mission.id}: the root review state could not be read ({error})")
            return False
        if state.status in {
            RootReviewStatus.READY,
            RootReviewStatus.ALREADY_RESOLVED,
            RootReviewStatus.NOT_READY,
            RootReviewStatus.UNREADABLE_PLAN,
        }:
            return False
        if state.status is RootReviewStatus.CUT_BUDGET_SPENT:
            # Once per revision, and then silence: the stall record is what an
            # operator reads next, and a second line every cycle would bury it.
            coordinator.record_cut_budget_spent(mission.id, state)
            self._note(f"mission {mission.id}: root review not re-cut ({state.detail})")
            return False
        if state.status is RootReviewStatus.REVIEW_REJECTED:
            # §9.1's decision table, never a silent retry.  The record is already
            # written and announced by ``record_review``; this loop does not get to
            # ask the same question again with the same anchor.  What it *may* do —
            # P2.3d / defect D5-A — is hand the blocking findings back to the Planner
            # once per plan revision, which is the table's "content defect → a new
            # attempt against the same duty" branch expressed at the plan level.
            if await self._repair_after_root_review(mission, new_mode, state):
                return True
            self._note(f"mission {mission.id}: {state.detail}")
            return False
        if state.needs_cut:
            try:
                package = coordinator.cut(mission.id, now_ms=int(self.store.now * 1000))
            except (ContractError, StoreError) as error:
                self._note(f"mission {mission.id}: the root review could not be cut ({error})")
                return False
            self._note(
                f"mission {mission.id}: root review cut {package.package_id} over "
                f"requirements revision {int(package.binding.requirements_revision)} and "
                f"{len(package.child_acceptance_refs)} contribution(s)"
                + (f" (re-cut: {', '.join(state.stale_reasons)})" if state.stale_reasons else "")
            )
            await self._ask_root_reviewer(mission, coordinator, package)
            return True
        if state.status is RootReviewStatus.AWAITING_REVIEW and state.package is not None:
            # The intent's creation key is the package id, so this is idempotent: a
            # package already out for review is not asked about twice.
            return await self._ask_root_reviewer(mission, coordinator, state.package)
        return False

    async def _repair_after_root_review(
        self, mission: Mission, new_mode: HierarchicalDispatch, state: Any
    ) -> bool:
        """§9.1's minimal repair branch: one more Planner round on blocking findings.

        P2.3d / defect D5-A.  Before this, a root review that concluded REJECT was the
        end of the Mission: ``record_review`` wrote the record, announced
        :data:`~.root_review.ROOT_REVIEW_REJECTED`, and the loop returned False.  There
        was no re-planning, no further work and no route by which the finding reached
        anybody — the Mission went idle and stopped with
        ``hierarchical_no_dispatchable_work``.  Ten episodes of the Grok acceptance run
        ended that way with every leaf accepted and, in nine of them, a deliverable the
        official grader passed.

        Three things keep this from becoming the silent retry §9.1 forbids:

        * only a **blocker** finding opens it.  A reviewer that rejected over minor
          limitations is not asking for a new plan, and re-planning on that would be
          this loop deciding the review was wrong;
        * the findings travel as a durable ``PlanningRejected`` record, which is what
          ``_planning_rejections`` feeds into the next proposal — the Planner is told
          what the reviewer said, not merely asked again;
        * the bound is **per Mission** (``max_root_review_repairs``, default 1), and
          a revision is never re-planned twice.  Verification P1-1 of P2.3j: counted
          per revision, every replacement revision earned a fresh repair, and with two
          methods in the library the Planner oscillated outer → alt → outer until a
          budget ran out (or the re-adoption collided with the retired instance's id).
          A Mission out of repairs falls through to the idle stall exactly as before,
          with the rejection in its stop report.

        Review P2-3: "one more round" is what *this* branch opens, not what the Mission
        then spends.  The round it opens is an ordinary Planner round, so if its
        proposal is refused ``_planning_rejected`` climbs the ordinary ladder — up to
        ``max_planning_attempts`` in total, not one.  That is the intended behaviour
        (a repair whose first proposal was unreadable is not a repair that was tried);
        what is bounded here is how many times a *root review rejection* may reopen
        planning at all.  The cross-revision bound is ``max_root_review_cuts``: each
        repair produces a new revision whose acceptances move the contributions, which
        spends a cut, and the cut budget ends the chain with
        ``HierarchicalRootReviewCutBudgetSpent``.
        """

        if self._config.max_root_review_repairs < 1:
            return False
        package = getattr(state, "package", None)
        if package is None:
            return False
        findings = self._root_review_findings(mission.id, str(package.package_id))
        blocking = [
            item for item in findings if str(item.get("severity", "")).lower() == "blocker"
        ]
        if not blocking:
            return False
        active = new_mode.semantics().active_plan_revision(mission.id)
        revision = 0 if active is None else int(active.revision)
        used = self._root_review_repairs(mission.id)
        if used >= int(self._config.max_root_review_repairs):
            self._note(
                f"mission {mission.id}: root review rejected plan revision {revision}, and "
                f"this Mission has already been re-planned after a root review {used} time(s) "
                f"(max_root_review_repairs={int(self._config.max_root_review_repairs)})"
            )
            return False
        if self._root_review_repairs(mission.id, revision):
            # Verification P1-1: the record below is keyed by revision, and a revision
            # is never re-planned twice — that part of the old bound still holds.
            self._note(
                f"mission {mission.id}: root review rejected plan revision {revision}, "
                "which a repair round has already been opened for"
            )
            return False
        ordinal = self._next_planning_ordinal(mission.id)
        # P2.3j: *which* adopted instance the review rejected, written into the record
        # the round is opened from.  The package's ``rejected_refinements`` and the
        # synthesis judgment both read it back (``rejected_refinements``), so the
        # repair round is about a named instance and not about "the root, somehow".
        rejected: dict[str, Any] = {}
        try:
            network = new_mode.network(mission.id)
            root = network.root_occurrence_ids[0] if network.root_occurrence_ids else None
            adopted = None if root is None else network.adopted_instance_for(root)
            if adopted is not None:
                rejected = {
                    "occurrence_id": str(root),
                    "method_instance_id": str(adopted.instance_id),
                    "method_ref": adopted.method_ref.to_json(),
                }
        except (GraphIntegrityError, ContractError, StoreError, KeyError):
            rejected = {}
        self.commit.record_planning_rejected(
            mission.id,
            ordinal=ordinal,
            reason=ROOT_REVIEW_REPAIR_REASON,
            # Review P2-2: its own key.  This record says why the round is being opened;
            # the round's own answer is written later under the ordinal key, and under
            # one key the second write was dropped.  Keyed by revision because the bound
            # is per revision — a second write here would mean the bound did not hold.
            key=f"{mission.id}:root-review-repair:{revision}",
            detail={
                "plan_revision": revision,
                "package_id": str(package.package_id),
                "repair_round": used + 1,
                "max_root_review_repairs": int(self._config.max_root_review_repairs),
                "findings": blocking[:16],
                **rejected,
            },
        )
        self._note(
            f"mission {mission.id}: root review rejected with {len(blocking)} blocking "
            f"finding(s); asking the Planner again (ordinal {ordinal}, revision {revision})"
        )
        return await self._planner_round_on_committed_plan(
            mission.id, ordinal=ordinal, phase="root_review_repair"
        )

    def _root_review_findings(self, mission_id: str, package_id: str) -> list[dict[str, Any]]:
        """The findings the reviewer filed against this package, newest record wins."""

        from .root_review import ROOT_REVIEW_REJECTED

        for event in reversed(self.store.list_events(mission_id)):
            if event.type != ROOT_REVIEW_REJECTED:
                continue
            if str(event.payload.get("package_id", "")) != package_id:
                continue
            return [dict(item) for item in event.payload.get("findings", []) or []]
        return []

    def _root_review_repairs(self, mission_id: str, revision: int | None = None) -> int:
        """Repair rounds already opened for this Mission (or for one plan revision).

        Verification P1-1: the bound is the Mission's, so the default counts every
        repair record; ``revision`` narrows it to the "never twice for one revision"
        check and to the stop report's per-revision line.
        """

        return sum(
            1
            for event in self.store.list_events(mission_id)
            if event.type == "PlanningRejected"
            and event.payload.get("reason") == ROOT_REVIEW_REPAIR_REASON
            and (
                revision is None
                or int((event.payload.get("detail") or {}).get("plan_revision", -1))
                == int(revision)
            )
        )

    def _root_review_stop_detail(
        self, mission: Mission, new_mode: HierarchicalDispatch
    ) -> dict[str, Any]:
        """What the stop report says about the root review, when it is why (P2.3j).

        Empty when the review is not in a rejected or budget-spent state — an idle
        Mission whose root review never ran has nothing to say here.  Otherwise the
        status, the package, the findings the reviewer filed, and how many repair
        rounds this revision spent: the reason is ``root_review_rejected``, the same
        code the repair record carries, so one grep finds both.
        """

        from .root_review import RootReviewStatus

        try:
            state = self._root_review(mission, new_mode).state(mission.id)
        except (GraphIntegrityError, ContractError, StoreError):
            return {}
        if state.status not in {
            RootReviewStatus.REVIEW_REJECTED,
            RootReviewStatus.CUT_BUDGET_SPENT,
        }:
            return {}
        package = getattr(state, "package", None)
        package_id = "" if package is None else str(package.package_id)
        active = new_mode.semantics().active_plan_revision(mission.id)
        revision = 0 if active is None else int(active.revision)
        return {
            "root_review": {
                "reason": ROOT_REVIEW_REPAIR_REASON,
                "status": str(state.status),
                "package_id": package_id,
                "plan_revision": revision,
                "repairs_used": self._root_review_repairs(mission.id),
                "repairs_used_on_revision": self._root_review_repairs(mission.id, revision),
                "rejected_method_refs": [
                    reference.to_json()
                    for refs in new_mode.rejected_method_refs(mission.id).values()
                    for reference in refs
                ],
                "max_root_review_repairs": int(self._config.max_root_review_repairs),
                "findings": self._root_review_findings(mission.id, package_id)[:16]
                if package_id
                else [],
                "detail": str(state.detail)[:600],
            }
        }

    def _accepted_path_hashes(self, mission_id: str) -> dict[str, str]:
        """Files a COMPLETED leaf already produced, keyed by path.  Later writers win.

        P2.3m: Grok H-L3-C1-r0's apply-patch leaf listed ``metrics/collector.py`` on
        its result (not only the ``patch`` port file).  The verify leaf then wrote
        the same bytes.  Those files are accepted work even when they are not the
        declared port output.
        """

        completed = {
            task.id
            for task in self.store.list_tasks(mission_id)
            if task.status is TaskStatus.COMPLETED
        }
        hashes: dict[str, str] = {}
        for artifact in self.store.list_mission_artifacts(mission_id):
            if artifact.task_id in completed:
                hashes[artifact.path] = artifact.content_hash
        return hashes

    def _read_only_rewrite_rejections(self, mission_id: str, task_id: str) -> int:
        return sum(
            1
            for event in self.store.list_events(mission_id)
            if event.type == "ResultRejected"
            and event.task_id == task_id
            and event.payload.get("reason") == "read_only_leaf_rewrote_workspace"
        )

    def _read_only_rewrite_repairs(self, mission_id: str) -> int:
        return sum(
            1
            for event in self.store.list_events(mission_id)
            if event.type == "PlanningRejected"
            and event.payload.get("reason") == READ_ONLY_REWRITE_REPAIR_REASON
        )

    def _read_only_rewrite_stop_detail(
        self, mission: Mission, new_mode: HierarchicalDispatch | None = None
    ) -> dict[str, Any]:
        """Named stall payload when a read-only rewrite repair is why there is no work."""

        if self._read_only_rewrite_repairs(mission.id) < 1:
            return {}
        revision = 0
        if new_mode is not None:
            active = new_mode.semantics().active_plan_revision(mission.id)
            revision = 0 if active is None else int(active.revision)
        findings: list[dict[str, Any]] = []
        for event in self.store.list_events(mission.id):
            if event.type != "PlanningRejected":
                continue
            if event.payload.get("reason") != READ_ONLY_REWRITE_REPAIR_REASON:
                continue
            findings = list((event.payload.get("detail") or {}).get("findings") or [])
        return {
            "read_only_rewrite": {
                "reason": READ_ONLY_REWRITE_REPAIR_REASON,
                "plan_revision": revision,
                "repairs_used": self._read_only_rewrite_repairs(mission.id),
                "max_repairs": MAX_READ_ONLY_REWRITE_REPAIRS,
                "findings": findings[:8],
            }
        }

    async def _escalate_read_only_rewrite(
        self,
        mission: Mission,
        task: Task,
        new_mode: HierarchicalDispatch,
        *,
        rewrote: Sequence[str],
        count: int,
    ) -> None:
        """P2.3m: N refusals of a genuine new write → planning, not another Attempt."""

        # Close the stuck leaf so a repair revision can retire the method
        # (P2.3j: ``running_work_not_reconciled`` otherwise).  RETRY_WAIT is not
        # terminal; leaving it open made Grok C1 burn the attempts budget instead.
        if task.status not in TERMINAL_TASK:
            self.commit._cancel_task_entity(  # noqa: SLF001
                task.id, reason=READ_ONLY_REWRITE_REPAIR_REASON, replaced_by=None
            )
        used = self._read_only_rewrite_repairs(mission.id)
        if used >= MAX_READ_ONLY_REWRITE_REPAIRS:
            self.commit.fail_mission(
                mission.id,
                stop_reason=MissionStopReason.NO_DISPATCHABLE_WORK,
                detail={
                    "reason": READ_ONLY_REWRITE_REPAIR_REASON,
                    "task_id": task.id,
                    "paths": list(rewrote),
                    "rejections": count,
                    **self._read_only_rewrite_stop_detail(mission, new_mode),
                },
            )
            self._note(
                f"mission {mission.id}: read-only leaf {task.id} rewrote {list(rewrote)} "
                f"{count} time(s); repair budget spent → {READ_ONLY_REWRITE_REPAIR_REASON}"
            )
            return
        if new_mode.planner_round_in_flight(mission.id):
            return
        active = new_mode.semantics().active_plan_revision(mission.id)
        revision = 0 if active is None else int(active.revision)
        rejected: dict[str, Any] = {}
        occurrence_id = ""
        try:
            network = new_mode.network(mission.id)
            spec = next(
                (item for item in network.occurrences if str(item.task_id) == task.id),
                None,
            )
            if spec is not None:
                occurrence_id = str(spec.occurrence_id)
            root = network.root_occurrence_ids[0] if network.root_occurrence_ids else None
            adopted = None if root is None else network.adopted_instance_for(root)
            if adopted is not None:
                rejected = {
                    "occurrence_id": str(root),
                    "method_instance_id": str(adopted.instance_id),
                    "method_ref": adopted.method_ref.to_json(),
                }
        except (GraphIntegrityError, ContractError, StoreError, KeyError, StopIteration):
            rejected = {}
        ordinal = self._next_planning_ordinal(mission.id)
        self.commit.record_planning_rejected(
            mission.id,
            ordinal=ordinal,
            reason=READ_ONLY_REWRITE_REPAIR_REASON,
            key=f"{mission.id}:read-only-rewrite:{task.id}:{revision}",
            detail={
                "plan_revision": revision,
                "repair_round": used + 1,
                "task_id": task.id,
                "occurrence_id": occurrence_id or rejected.get("occurrence_id", ""),
                "paths": list(rewrote),
                "rejections": count,
                "findings": [
                    {
                        "severity": "blocker",
                        "detail": (
                            f"read-only leaf {task.id} rewrote {list(rewrote)} {count} "
                            "times (read_only_leaf_rewrote_workspace). This leaf's task "
                            "type is read-only; the method must put file changes in a "
                            "write/patch step (repo.write / apply-patch), not in a "
                            "verify/inspect/summarize/facts/reproduce leaf."
                        ),
                    }
                ],
                **rejected,
            },
        )
        self._note(
            f"mission {mission.id}: read-only leaf {task.id} rewrote {list(rewrote)} "
            f"{count} time(s); asking the Planner (ordinal {ordinal}, "
            f"{READ_ONLY_REWRITE_REPAIR_REASON})"
        )
        await self._planner_round_on_committed_plan(
            mission.id, ordinal=ordinal, phase="read_only_rewrite_repair"
        )

    def _next_planning_ordinal(self, mission_id: str) -> int:
        """One past the highest ordinal any planning round of this Mission has used.

        The ordinal *is* the planner intent's creation key (``…:planner:<n>``), so the
        next free one is found by asking for each in turn — reusing a spent ordinal
        would return the old intent and the repair round would never be dispatched.
        """

        ordinal = 1
        while self.store.get_intent_for_subject(f"{mission_id}:planner:{ordinal}") is not None:
            ordinal += 1
        return ordinal

    async def _ask_root_reviewer(
        self, mission: Mission, coordinator: Any, package: Any
    ) -> bool:
        """Create the root reviewer's intent.  Idempotent per review package.

        ``kind="plan"`` rather than ``"critic"``: the ``critic`` kind is the Task
        Critic's, which is bound to an Attempt and collected by the attempt runner,
        and a review of the whole composition has no Attempt.  The *role* is its own
        (``root_reviewer``) and so is the account — ``ReviewAccount.MISSION``, never a
        Task budget (§13 v1.4, §18.5).
        """

        from ..runtime.role_templates import ROOT_REVIEWER
        from .root_review import MAX_ROOT_REVIEW_ASKS, ROOT_REVIEW_UNREADABLE

        # An unreadable reply is not an answer, so this is not asking the same
        # question twice: it is the same question put once more, with the parse error
        # attached, exactly as ``critic_schema_retry_feedback`` does for the Task
        # Critic.  Bounded at two — a model that cannot produce the block twice is a
        # deployment problem, and the Mission goes to the idle-stall path with the
        # reason written down rather than spending the Mission account in a loop.
        unreadable = [
            event
            for event in self.store.list_events(mission.id)
            if event.type == ROOT_REVIEW_UNREADABLE
            and str((event.payload or {}).get("package_id", "")) == str(package.package_id)
        ]
        ordinal = len(unreadable) + 1
        if ordinal > MAX_ROOT_REVIEW_ASKS:
            return False
        subject = f"{mission.id}:root-review:{package.package_id}:{ordinal}"
        if self.store.get_intent_for_subject(subject) is not None:
            return False
        request = coordinator.request(
            mission.id,
            package,
            schema_feedback=(
                ""
                if not unreadable
                else "上一次回答无法解析："
                + str((unreadable[-1].payload or {}).get("detail", ""))
                + "。请重新给出同一份判断，整段回答只包含一个 <critic_verdict>…</critic_verdict> 块。"
            ),
        )
        template = self._template(ROOT_REVIEWER, mission.id)
        decision = self._route_service("critic", mission.id)
        config = AgentConfig(
            name=f"root-reviewer-{str(package.package_id)[-8:]}",
            instructions=template.instructions,
            model_profile_ref=decision.profile_id,
            # A root review reads the package it was handed and answers; it has no
            # tools.  ``tool_names=()`` is the gate — the limit is only a bound, and
            # :class:`AgentLimits` refuses a non-positive one, so a zero here raised
            # ``ValueError`` and the Mission died on the way to its own review.
            tool_names=(),
            limits=AgentLimits(
                max_model_calls_per_turn=2,
                max_tool_calls_per_turn=1,
                turn_deadline_seconds=self._config.turn_deadline_seconds,
            ),
        )
        message = user_message_json(json.dumps(request.to_json(), ensure_ascii=False))
        try:
            self.commit.create_service_intent(
                kind="plan",
                subject_id=subject,
                mission_id=mission.id,
                # §13 v1.4: the cost of a MISSION_FINAL review lands on the Mission
                # account.  ``coordinator.account`` is read from
                # ``account_for_purpose`` so the two cannot drift.
                account_id=mission_account(mission.id),
                creation_key=subject,
                input_id="attempt-input",
                input_hash=sha256_hex(message),
                config={
                    "agent_config": config.to_json(),
                    "message": message,
                    "context_version": request.content_hash(),
                    "prompt_version": template.prompt_version,
                    "base_version": mission.version,
                    "ordinal": 1,
                    "role": "root_reviewer",
                    "budget_account": str(coordinator.account),
                    "review_package_id": str(package.package_id),
                    "review_criteria": list(request.criterion_ids),
                    **self._service_config(decision),
                },
                reservation=self._reservation(
                    self._config.critic_reserve_tokens, decision.profile_id
                ),
            )
        except (ContractError, CommitRejected, BudgetError, RoutingUnavailable) as error:
            self._note(f"mission {mission.id}: the root reviewer was not asked ({error})")
            return False
        self._note(f"mission {mission.id}: root reviewer asked about {package.package_id}")
        return True

    async def _root_resolution_formed(
        self, mission: Mission, new_mode: HierarchicalDispatch
    ) -> bool:
        """Whether this Mission's root ``GoalResolution`` stands (P2.3c part 2).

        Offers it once when it does not, and reports the refusal.  The decision itself
        is entirely the Commit Service's — this is the trigger, not a second judge —
        and the delivery contract is read from the Mission's requirements so the stage
        a root must reach is the one the goal asked for rather than one this call
        invented (AER §6.1).
        """

        semantics = new_mode.semantics()
        try:
            inputs = new_mode.root_resolution_inputs(mission.id)
        except GraphIntegrityError as error:
            await self._plan_integrity_stop(mission, error)
            return False
        if inputs.reason == "ALREADY_RESOLVED":
            return True
        required_stage = None
        receipt_ids: tuple[str, ...] = ()
        if inputs.requirements is not None and inputs.requirements.delivery_contract_ref:
            # The goal declared a delivery contract, so the root is not resolved until
            # a recorded DeliveryReceipt says the output travelled that far.  CONFIRMED
            # is the strongest stage there is, and asking for less than the strongest
            # would be this call relaxing the contract on the goal's behalf.
            required_stage = DeliveryStage.CONFIRMED
            # Review F5: only the receipts this root may legitimately quote.  Naming
            # every receipt the Mission ever recorded meant a single one written for a
            # retired branch — or for an Acceptance later superseded — refused the
            # *whole* command (``DELIVERY_RECEIPT_INVALID``) for good.  The filter is
            # the accept side's own predicate, asked before the command is built rather
            # than written a second time here.
            receipt_ids = eligible_root_receipts(
                semantics,
                mission.id,
                obligation_id=inputs.obligation_id,
                method_instance_id=inputs.method_instance_id,
                required_stage=required_stage,
            )
        outcome = new_mode.attempt_root_resolution(
            mission.id,
            principal=PlanPrincipal(
                principal_id=self._owner,
                scope_id="mission",
                manager_epoch=semantics.epoch(mission.id, "mission"),
            ),
            command_id=f"{mission.id}:root-resolution",
            required_delivery_stage=required_stage,
            delivery_receipt_ids=receipt_ids,
            source={"trigger": "decide", "orchestrator": self._owner},
        )
        if not outcome.committed:
            self._note(
                f"mission {mission.id} root resolution not formed: "
                f"{outcome.reason} ({outcome.detail[:200]})"
            )
        return outcome.committed

    def _hierarchical_judgment_inputs(
        self, mission: Mission, new_mode: HierarchicalDispatch, tasks: Sequence[Task]
    ) -> list[UpstreamInput]:
        """The Mission Judge's tree for a hierarchical Mission (P2.3k / defect N3).

        Read from what the root ``GoalResolution`` was formed out of and nothing else:
        the CURRENT acceptances of the adopted plan (``root_contributions``), each
        acceptance's accepted artifacts, and the outputs P2.3h indexed on declared
        ports.  One path written by several contributions is not a conflict here —
        ordering in this mode is the typed network's, not ``dependency_ids`` — so the
        rule is override, weighted ``(criterion-linked, port-indexed, acceptance
        order)``: the output of a step the plan made answerable for a root criterion
        wins, then an output the reviewer read at a declared port, then the later
        acceptance (the clock is the tie-break, never the rule).

        P2.3k verification P1-1 (AER I05, "what the reviewer saw is what is
        delivered"): an output a root criterion is linked to is **never dropped**.
        When two criterion-linked port outputs land on one path — C1-r1's ``verify``
        ``report`` and ``summarize`` ``summary`` were both ``REPORT.md`` — the loser
        keeps its bytes in the tree under ``accepted-outputs/<task>/<port>/<path>``,
        and the record names both artifacts.  Nothing raises; what was superseded
        and where it was kept is written down once, in
        ``ArtifactMergeNotApplicableUnderHierarchical``.
        """

        semantics = new_mode.semantics()
        contributing = {
            item
            for ids in new_mode.root_contributions(mission.id).values()
            for item in ids
        }
        # ``list_acceptances`` orders by ``accepted_at_ms`` then id: the clock is the
        # tie-break between contributions, never the rule.
        acceptances = [
            item
            for item in semantics.list_acceptances(mission.id)
            if str(item.acceptance_id) in contributing
        ]
        rank_of = {str(item.acceptance_id): index for index, item in enumerate(acceptances)}
        linked = {
            str(item.task_id)
            for item in self._root_review(mission, new_mode).carried_criteria(mission.id)
        }
        tasks_by_id = {task.id: task for task in tasks}
        # path → every placement offered for it, each (weight, task_id, artifact, port)
        offered: dict[str, list[tuple[tuple[int, int, int], str, Artifact, str | None]]] = {}

        def place(task_id: str, artifact: Artifact | None, rank: int, port: str | None) -> None:
            if artifact is None:
                return
            weight = (int(task_id in linked), int(port is not None), rank)
            offered.setdefault(artifact.path, []).append((weight, task_id, artifact, port))

        for acceptance in acceptances:
            task_id = str(acceptance.task_id)
            rank = rank_of[str(acceptance.acceptance_id)]
            task = tasks_by_id.get(task_id) or self.store.get_task(task_id)
            for artifact_id in () if task is None else task.accepted_artifacts:
                place(task_id, self.store.get_artifact(artifact_id), rank, None)
        for row in semantics.list_acceptance_outputs(mission.id):
            acceptance_id = str(row.get("acceptance_id", ""))
            if acceptance_id not in rank_of:
                continue
            place(
                str(row.get("producer_task_ref", "")),
                self.store.get_artifact(str(row.get("artifact_id", ""))),
                rank_of[acceptance_id],
                str(row.get("output_port", "")) or None,
            )
        tree: dict[str, UpstreamInput] = {}
        superseded: list[dict[str, Any]] = []
        for path in sorted(offered):
            placements = sorted(offered[path], key=lambda item: item[0], reverse=True)
            _weight, kept_task, kept_artifact, _port = placements[0]
            tree[path] = UpstreamInput(kept_task, path, kept_artifact.content_hash, kept_artifact.id)
            losers: list[dict[str, Any]] = []
            seen_artifacts = {kept_artifact.id}
            for weight, task_id, artifact, port in placements[1:]:
                if artifact.id in seen_artifacts or artifact.content_hash == kept_artifact.content_hash:
                    continue  # the same bytes under another placement are not a loss
                seen_artifacts.add(artifact.id)
                kept_at = None
                if weight[0] and port is not None:
                    # I05: evidence a root criterion was judged on stays deliverable.
                    kept_at = f"accepted-outputs/{task_id}/{port}/{path}"
                    tree[kept_at] = UpstreamInput(task_id, kept_at, artifact.content_hash, artifact.id)
                losers.append(
                    {
                        "task_id": task_id,
                        "artifact_id": artifact.id,
                        "content_hash": artifact.content_hash,
                        "linked": bool(weight[0]),
                        "port": port,
                        "kept_at": kept_at,
                    }
                )
            if not losers:
                continue
            kept_by = "acceptance_order"
            if kept_task in linked:
                kept_by = (
                    "acceptance_order_between_linked"
                    if any(item["linked"] for item in losers)
                    else "criterion_link"
                )
            superseded.append(
                {
                    "path": path,
                    "kept_task_id": kept_task,
                    "kept_artifact_id": kept_artifact.id,
                    "kept_content_hash": kept_artifact.content_hash,
                    "kept_by": kept_by,
                    "superseded_task_ids": list(dict.fromkeys(item["task_id"] for item in losers)),
                    "superseded": losers,
                }
            )
        self.commit.record_artifact_merge_not_applicable(
            mission.id,
            subject=f"{mission.id}:judge:artifact-merge",
            contributions=len(acceptances),
            artifacts=len(tree),
            superseded=superseded,
        )
        if superseded:
            self._note(
                f"hierarchical mission {mission.id}: judgment tree keeps the criterion-linked / "
                f"port / last writer of {[item['path'] for item in superseded]}; a linked loser "
                "is kept under accepted-outputs/; the legacy merge is not applied"
            )
        return [tree[path] for path in sorted(tree)]

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
        selection_decision: Mapping[str, Any] | None = None,
        admission: Any = None,
    ) -> bool:
        # Review F1: every other caller of this entry (repair, selection, a manual
        # drive) must be fail-closed too, or the refusal in ``_decide`` would only
        # cover the common path.
        if self._assembly_missing(mission, at="next_attempt"):
            return False
        # P2.3b / §18.5 rule 4: before anything else, a compound is refused here with
        # NEEDS_REFINEMENT.  The gate is ``form`` from the semantic binding, not the
        # status string and not the semantics version — ``TaskStatus.READY`` on a
        # compound is a rebuildable display index and never a permission to dispatch.
        new_mode = self._new_mode(mission)
        if new_mode is not None:
            try:
                intercepted = new_mode.intercept_worker_dispatch(mission.id, task.id)
            except GraphIntegrityError as error:
                await self._plan_integrity_stop(mission, error)
                return True
            if intercepted is not None:
                self._note(
                    f"task {task.id} not dispatched: {intercepted.reason} "
                    f"(occurrence {intercepted.occurrence_id})"
                )
                return False
            if (
                self._read_only_rewrite_rejections(mission.id, task.id)
                >= MAX_READ_ONLY_REWRITE_REJECTIONS
            ):
                # P2.3m: the collector already opened the planning repair (or stopped
                # the Mission).  Do not spend another Attempt on the same occurrence.
                return False
            # P2.3c part 2 / TG §8.3: the dispatch transaction re-checks.  An
            # ``EligiblePrimitiveTask`` is *not* a capability — it records that a
            # controlled check passed at ``admitted_at_ms`` and grants nothing — so
            # this entry refuses a Task that arrived without one, however it got here.
            # ``_decide`` hands its admission down so the common path does not re-read
            # the whole plan; every other caller (repair, selection, a manual drive)
            # pays for the fresh read rather than skipping the gate.
            if admission is None:
                try:
                    admission = new_mode.admissions(mission.id).admission_for(task.id)
                except GraphIntegrityError as error:
                    await self._plan_integrity_stop(mission, error)
                    return True
            # Review F11: ``isinstance`` and not a duck-typed ``gate_passed`` probe.
            # ``EligiblePrimitiveTask.gate_passed`` is guarded by the admission token,
            # but a structural test would let *any* object carrying a true attribute of
            # that name through this door — which is exactly the "admission with a flag"
            # shape ``admissions()`` is written to avoid.
            if not isinstance(admission, EligiblePrimitiveTask) or not admission.gate_passed:
                self._note(
                    f"task {task.id} not dispatched: no admission from the readiness gate "
                    "(§18.5 constraint 4)"
                )
                return False
        # A Worker already in flight can produce another pending verification.
        # Account for that obligation before creating more work, across Missions.
        active_missions = {item.id for item in self._active_missions()}
        pending_verification = sum(
            stored.envelope.mission_id in active_missions
            and not self.commit.candidate_is_waiting(stored.envelope.id)
            for stored in self.store.list_results_by_verification("PENDING", "RUNNING")
        )
        potential_results = sum(
            attempt.mission_id in active_missions
            and self.store.find_result_for_attempt(attempt.id) is None
            for attempt in self.store.list_attempts_by_status(
                "PENDING", "CLAIMED", "RUNNING", "SUBMITTED", "VERIFYING"
            )
        )
        if pending_verification + potential_results >= self._config.max_pending_verifications:
            return False
        # a repair follows the last *failed* Attempt; a parallel candidate follows nobody
        previous = next(
            (a for a in reversed(attempts) if a.status in TERMINAL_ATTEMPT and a.failure), None
        )
        if selection_decision is not None:
            previous = None
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
        # P2.3b / §24.1 decision 4: in the new mode it starts from the resolved
        # InputManifest instead, so an ORDER-only predecessor contributes nothing.
        all_tasks = {t.id: t for t in self.store.list_tasks(mission.id)}
        upstream_tasks = ancestors(task.id, all_tasks)
        try:
            inputs = (
                new_mode.attempt_inputs(mission.id, task.id)
                if new_mode is not None
                else merge_accepted(
                    upstream_tasks, self._artifacts_by_task(upstream_tasks), tasks_by_id=all_tasks
                )
            )
        except GraphIntegrityError as error:
            await self._plan_integrity_stop(mission, error)
            return True
        except ArtifactConflict as error:
            self.commit.stop_task(
                task.id,
                stop_reason=MissionStopReason.ARTIFACT_CONFLICT,
                detail={"error": str(error)},
            )
            await self._release_mission(mission.id)
            self._note(f"task {task.id} stopped: artifact conflict ({error})")
            return True
        validated_input = None
        validated_context = None
        fragment_parents = [
            parent for parent in upstream_tasks
            if parent.id in task.dependency_ids and "fragment_validation" in parent.context
        ]
        if fragment_parents:
            try:
                if len(fragment_parents) != 1:
                    raise ContractError("consumer has multiple direct fragment validations")
                fragment_id = fragment_parents[0].context["fragment_validation"]["fragment_id"]
                validated_input = self.commit.fragment_input(
                    fragment_id,
                    **({"selection_consumer_task_id": task.id}
                       if attempts and self.commit.selection_policy_for(task.id) is not None else
                       {"retry_consumer_task_id": task.id} if attempts
                       else {"ready_consumer_task_id": task.id}),
                )
                actual = {(item.artifact_id, item.path, item.content_hash) for item in inputs}
                if not validated_input["material_refs"] or any(
                    (item["artifact_id"], item["path"], item["content_hash"]) not in actual
                    for item in validated_input["material_refs"]
                ):
                    raise ContractError("validated fragment material is absent from actual inputs")
                validated_context = consumer_fragment_context(validated_input)
                assert_no_secrets(validated_input)
            except (ContractError, ContextRejected) as error:
                self.commit.stop_task(
                    task.id, stop_reason=MissionStopReason.CONTEXT_REJECTED,
                    detail={"error": str(error)[:300]},
                )
                await self._release_mission(mission.id)
                return True
        bound = self.policy_for(mission.id)  # step 9 (plan D9-4'): the Mission's own version
        role = self._template(role_for_task(task), mission.id)  # D5-9: approach
        if new_mode is not None:
            role = self._hierarchical_worker_template(role, mission.id)
        if selection_decision is not None:
            from ..runtime.role_templates import SYNTHESIZER
            role = self._template(SYNTHESIZER, mission.id)
            role = replace(role, prompt_version=role.prompt_version + ":compare-v2",
                           instructions=role.instructions + "\n候选输入不是正式知识；读取selection_inputs中的"
                           "独立候选文件，对照原完整Task合同生成新输出。不得将输入PASS视为输出PASS；"
                           "used_knowledge只能填写当前ContextPackage.verified_knowledge中的真实id；"
                           "若该目录为空，填写空数组[]，不要为满足通用综合提示虚构id。"
                           "候选artifact_id、result ID和验证片段fragment_id仅用于输入血缘与材料，"
                           "都不是知识id，不得填写到used_knowledge。相同逻辑文件由你明确合成新文件。")
            inputs = [*inputs, *(
                UpstreamInput(artifact.task_id, artifact.path, artifact.content_hash, artifact.id)
                for artifact in self.commit.selection_input_artifacts(selection_decision["receipt_id"])
            )]
        untrusted = [str(p) for p in (mission.final_report or {}).get("untrusted_sources", [])]
        try:
            knowledge = self._gather_knowledge(
                mission, task, all_tasks, search_visibility=True
            )
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
        source_binding = (self.commit.fragment_validation_binding(task.id)
                          if "fragment_validation" in task.context
                          else self._active_source_binding(mission.id))
        fragment_files = self.commit.fragment_validation_inputs(task.id)
        source_versions = source_binding.get("source_versions")
        source_paths = set(source_versions or {})
        if source_binding:
            untrusted = sorted(set(untrusted) | source_paths)
        previous_files = sorted({*seed, *(item.path for item in inputs), *source_paths, *fragment_files})
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
            from ..runtime.action_schema import worker_action_contract

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
                action_candidate_contract=(
                    worker_action_contract(
                        mission_criteria=mission.success_criteria,
                        task_criteria=task.success_criteria,
                        task_outputs=task.outputs,
                        connectors=self._connectors,
                        deployment=self._config.deployment_policy,
                    )
                ),
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
        if selection_decision is not None:
            from ..context.context_builder import _seal
            package = _seal({**dict(package.package), "selection_inputs": {
                "data_not_instruction": True, "version": "candidate-inputs-v1",
                "decision_id": selection_decision["receipt_id"],
                "inputs": [{"path": a.path, "hash": a.content_hash, "artifact_id": a.id}
                           for a in self.commit.selection_input_artifacts(selection_decision["receipt_id"])],
                "marker": "UNVERIFIED candidate material; C needs its own complete verification",
            }})
        if new_mode is not None:
            # P2.3c part 2d, decision 4: tell the leaf which output ports its own
            # occurrence declares.  The names are the plan's, not the model's — the
            # model supplies the *local key* (which file) and nothing else (TG design
            # §3.2).  An empty list means nothing downstream consumes this leaf, and
            # the envelope's ``outputs`` may then be omitted.
            declared_ports = new_mode.declared_output_ports_for(mission.id, task.id)
            if declared_ports:
                from ..context.context_builder import _seal
                package = _seal({**dict(package.package), "declared_output_ports": {
                    "data_not_instruction": True,
                    "version": "declared-output-ports-v1",
                    "ports": [dict(item) for item in declared_ports],
                }})
            # P2.3h: tell the leaf which **root** criteria the plan hangs on it, with
            # the method's own ``evidence_requirement`` for each — the sentence its
            # output at the listed ports has to satisfy, because that output is what
            # the root review reads for that criterion.  Absent when it carries none.
            carried = new_mode.carried_root_criteria_for(mission.id, task.id)
            if carried:
                from ..context.context_builder import _seal
                package = _seal({**dict(package.package), "carried_root_criteria": {
                    "data_not_instruction": True,
                    "version": "carried-root-criteria-v1",
                    "note": (
                        "the root (MISSION_FINAL) review judges each root_criterion_id "
                        "below on this task's accepted output at the listed ports; that "
                        "output must show what evidence_requirement states"
                    ),
                    "criteria": [dict(item) for item in carried],
                }})
        fragment_context = self.commit.fragment_validation_context(task.id)
        if fragment_context:
            from ..context.context_builder import _seal
            package = _seal({**dict(package.package), "fragment_scope": {
                **dict(fragment_context), "data_not_instruction": True,
            }})
        if validated_context is not None:
            from ..context.context_builder import _seal
            package = _seal({**dict(package.package), "validated_fragment_input": {
                **validated_context, "data_not_instruction": True,
            }})
        selection = self.commit.selection_round(task.id)
        remaining_selection = (self._config.turn_deadline_seconds if selection is None else
                               selection["deadline_at"] - self.store.now)
        if remaining_selection <= 0:
            return False
        # D6-7: Mission ∩ Task ∩ Role ∩ Deployment, frozen into the intent below
        allowed = effective_tools(
            mission_tools=mission.allowed_tools,
            task_tools=task.allowed_tools,
            role_tools=(
                (*role.tool_names, *self._config.agentdojo_tool_schemas)
                if self.commit.domain_for(mission.id).id == "agentdojo-v1"
                else (*role.tool_names, *self._config.are_tool_schemas)
                if self.commit.domain_for(mission.id).id == "are-v1"
                else role.tool_names
            ),
            deployment=self._config.deployment_policy,
        )
        # D6-8: the Attempt's tool-call cap = the deployment's per-turn cap, narrowed by the
        # Task budget's own dimension; it is reserved up front and enforced at the gateway
        tool_cap = self._config.max_tool_calls_per_turn
        if task.budget.max_tool_calls is not None:
            tool_cap = min(tool_cap, task.budget.max_tool_calls)
        system_hold = self.commit.system_task_hold(task.id)
        if self._tool_calls_limited(mission, task) and system_hold is None:
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
                    self._config.turn_deadline_seconds, remaining_selection,
                    float(task.budget.max_runtime_seconds or self._config.turn_deadline_seconds),
                ),
            ),
        )
        message = user_message_json(package.text)
        tokens = self._config.attempt_reserve_tokens
        critic_tail = None
        first_critic_binding: dict[str, Any] = {}
        if "critic_review" in task.verification_policy and selection_decision is None:
            try:
                critic_decision = self._route_service("critic", mission.id)
            except RoutingUnavailable as unavailable:
                return await self._defer_for_profile(mission, task, unavailable)
            first = self._first_critic_budget(critic_decision)
            if isinstance(first, FirstRequestBudget):
                first_reservation = self._first_critic_reservation(first, critic_decision.profile_id)
                first_critic_binding = {"first_critic_budget": {
                    "provider_input_cap": first.provider_input_cap.to_json(),
                    "output_ceiling": first.output_ceiling,
                    "minimum_tokens": first.minimum_tokens,
                    "cost_micros": first_reservation.cost_micros,
                }}
                critic_tail = first_reservation
            else:
                first_critic_binding = {"first_critic_budget_unknown": first.reason}
                critic_tail = self._reservation(
                    self._config.critic_reserve_tokens, critic_decision.profile_id
                )
        self._deferred.pop(task.id, None)
        if system_hold is not None:
            # The original Task hold already protects both roles. Do not create
            # another FIRST Critic reservation against its fully reserved cap.
            critic_tail = None
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
                critic_share = (critic_tail.tokens if critic_tail is not None else
                                self._config.critic_reserve_tokens
                                if "critic_review" in task.verification_policy else 0)
                head_room = remaining - critic_share  # keep the Critic's own share free
                if 0 < head_room < tokens:
                    tokens = head_room
        if selection_decision is not None:
            assert selection is not None
            critic_tokens = self._config.critic_reserve_tokens if "critic_review" in task.verification_policy else 0
            tokens = min(tokens, selection["policy"]["synthesis_reserve"]["tokens"] - critic_tokens)
            if tokens <= 0:
                self.commit.stop_selection(task.id, reason="selection_tail_insufficient")
                return True
        if system_hold is not None:
            held = self.commit.protected_tail_hold(system_hold["hold_id"])
            allowance = None if held is None else self.commit.ledger.reservation(held["subject_id"])
            if allowance is None:
                raise ContractError("system Task has no original protected allowance")
            frozen_first = first_critic_binding.get("first_critic_budget")
            critic_share = (frozen_first["minimum_tokens"] if frozen_first is not None else
                            self._config.critic_reserve_tokens if "critic_review" in task.verification_policy else 0)
            room = allowance["reserved_tokens"] - critic_share
            if allowance["state"] == "SETTLED" or room <= 0:
                self.commit.stop_task(task.id, stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
                                      detail={"reason": "system_tail_exhausted", "dimension": "tokens"})
                await self._release_mission(mission.id)
                return True
            tokens = min(tokens, room)
            if frozen_first is not None:
                cost_room = allowance["reserved_cost_micros"] - frozen_first["cost_micros"]
                if cost_room < 0:
                    self.commit.stop_task(task.id, stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
                                          detail={"reason": "system_first_critic_cost_insufficient", "dimension": "cost_micros"})
                    await self._release_mission(mission.id)
                    return True
                while tokens > 0 and self._system_reservation(
                    tokens, decision.profile_id, config.limits.max_model_calls_per_turn
                ).cost_micros > cost_room:
                    tokens //= 2
                if tokens <= 0:
                    self.commit.stop_task(task.id, stop_reason=MissionStopReason.BUDGET_EXHAUSTED,
                                          detail={"reason": "system_first_critic_cost_insufficient", "dimension": "cost_micros"})
                    await self._release_mission(mission.id)
                    return True
            if self._tool_calls_limited(mission, task):
                tool_cap = min(tool_cap, allowance["reserved_tool_calls"])
                config = replace(config, limits=replace(config.limits, max_tool_calls_per_turn=tool_cap))
        try:
            attempt, _intent = self.commit.create_attempt(
                task.id,
                selection_decision_id=None if selection_decision is None else selection_decision["receipt_id"],
                selection_owner=self._owner if selection_decision is not None else None,
                critic_tail=critic_tail,
                role=role.name,
                model=decision.model,
                prompt_version=role.prompt_version,
                context_version=package.context_version,
                reservation=replace(
                    (self._system_reservation(tokens, decision.profile_id,
                                              config.limits.max_model_calls_per_turn)
                     if system_hold is not None else self._reservation(tokens, decision.profile_id)),
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
                    **first_critic_binding,
                    "knowledge": knowledge.frozen_ids,  # D4-10: what this Attempt saw
                    "retrieval_version": knowledge.retrieval.version,
                    "retrieval_status": knowledge.retrieval.status,
                    "context_builder_version": CONTEXT_BUILDER_VERSION,
                    "untrusted_sources": untrusted,
                    **({"validated_fragment_input": validated_input}
                       if validated_input is not None else {}),
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
            from .commit_service import InconclusiveRetryExhausted

            if isinstance(error, InconclusiveRetryExhausted):
                if self.commit.stop_inconclusive_task(task.id):
                    await self._release_mission(mission.id)
                    self._note(f"task {task.id} stopped: insufficient_evidence (retry limit)")
                    return True
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
                if (
                    task.kind == "conflict"
                    and reason is MissionStopReason.MAX_ATTEMPTS_REACHED
                    and self.commit.domain_for(mission.id).id != "doc-research-v1"
                ):
                    return self._arbitrate_conflict(mission, task, detail)  # D7-8' ①
                self.commit.stop_task(task.id, stop_reason=reason, detail=detail)
                self._note(f"task {task.id} stopped: {reason} ({error.dimension})")
            await self._release_mission(mission.id)
            return True
        except BudgetError as error:
            if system_hold is None:
                raise
            self.commit.stop_task(
                task.id, stop_reason=MissionStopReason.RUNTIME_UNAVAILABLE,
                detail={"reason": "system_tail_binding_unavailable", "error": str(error)},
            )
            await self._release_mission(mission.id)
            return True
        self._note(
            f"attempt {attempt.id} created (retry_of={attempt.retry_of}, inputs={len(inputs)})"
        )
        return True

    def _refresh_document_judgments(
        self,
        mission: Mission,
        judgments: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        domain = self.commit.domain_for(mission.id)
        result = [dict(item) for item in judgments]
        if not requires_mission_source_binding(domain):
            return result
        from ..verification.mission_coverage import mission_coverage

        coverage = mission_coverage(
            self.store, mission, domain, artifact_store=self.assembled.workspaces.artifact_store
        )
        assessed = {
            item["text"]: item for item in coverage["criteria"] if item["verdict"] != "STRUCTURAL"
        }
        for item in result:
            row = assessed.get(item["criterion"])
            if row is not None:
                item.update(
                    criterion_id=row["criterion_id"],
                    met=row["verdict"] in {"PASS", "INCONCLUSIVE"},
                    verdict=row["verdict"],
                    judge="document_coverage",
                    reason="; ".join(row["reasons"]),
                    limitations=list(row["limitations"]),
                    task_assessment_receipt_ids=list(row["task_assessment_receipt_ids"]),
                    excluded_claim_ids=list(row["excluded_claim_ids"]),
                    source_provenance_issues=list(row["source_provenance_issues"]),
                )
        return result

    async def _judge(self, mission: Mission, tasks: Sequence[Task]) -> bool:
        key = judgment_key(tasks)
        cached = self.commit.criteria_judgment(mission.id, key)  # booked only for an arbitration
        if cached is not None and await self._stop_document_insufficient(mission):
            return True
        evaluated = cached if cached is not None else await self._evaluate_criteria(mission, tasks)
        if evaluated is None:
            return True
        judgments, summary = evaluated
        judgments = self._refresh_document_judgments(mission, judgments)
        ruled, created = self._arbitrated(mission, tasks, key, judgments)
        if ruled is None:  # D7-8' ②: a person rules first; the judgment is kept meanwhile
            if cached is None:
                self.commit.record_criteria_judgment(mission.id, key, judgments, summary=summary)
            return created or cached is None
        judged = self.commit.judge_mission(mission.id, judgments=ruled, summary=summary)
        self._note(f"mission {mission.id} judged: {judged.status} ({judged.stop_reason})")
        return True

    async def _stop_document_insufficient(self, mission: Mission) -> bool:
        domain = self.commit.domain_for(mission.id)
        if not supports_document_assessments(domain):
            return False
        try:
            stopped = self.commit.stop_insufficient_mission(mission.id)
        except ContractError as error:
            self.commit.fail_mission(
                mission.id,
                stop_reason=MissionStopReason.VERIFIER_UNAVAILABLE,
                detail={"assessment_error": str(error)},
            )
            await self._release_mission(mission.id)
            return True
        if stopped is not None:
            await self._release_mission(mission.id)
            self._note(f"mission {mission.id} stopped: insufficient_evidence")
            return True
        return False

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
        domain = self.commit.domain_for(mission.id)
        document_coverage = None
        if supports_document_assessments(domain):
            if await self._stop_document_insufficient(mission):
                return None
            from ..verification.mission_coverage import mission_coverage

            document_coverage = mission_coverage(
                self.store, mission, domain, artifact_store=self.assembled.workspaces.artifact_store
            )
        all_tasks = {t.id: t for t in tasks}
        # P2.3k / defect N3.  The legacy merge reads "independent branches" off
        # ``Task.dependency_ids``, which a materialised occurrence leaves empty by
        # design (§18.5 constraint 4) — so on a hierarchical Mission any two leaves that
        # wrote one path were an ``ArtifactConflict``, and the Grok C3 episodes failed a
        # Mission whose root ``GoalResolution`` already stood, one event after
        # ``GoalResolutionCommitted``.  The tree is read from the resolution's own
        # contributions there (same shape as P2.3d's D4: a mode branch and a record);
        # the legacy Mission keeps the legacy rule, byte for byte.
        new_mode = self._new_mode(mission)
        try:
            merged = (
                self._hierarchical_judgment_inputs(mission, new_mode, tasks)
                if new_mode is not None
                else merge_accepted(
                    list(tasks), self._artifacts_by_task(tasks), tasks_by_id=all_tasks
                )
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
        mission_sources = None
        if requires_mission_source_binding(domain):
            from ..verification.mission_sources import ensure_mission_tree, prepare_mission_tree

            existing = next(
                (
                    self.store.get_intent_for_subject(f"{mission.id}:judge:{n}")
                    for n in range(1, MAX_CRITIC_ATTEMPTS + 1)
                    if self.store.get_intent_for_subject(f"{mission.id}:judge:{n}") is not None
                ),
                None,
            )
            try:
                if existing is not None:
                    self._validate_mission_judge_intent(existing)
                    view_id = str(existing.config["attempt_id"])
                    mission_sources = {
                        k: existing.config[k]
                        for k in (
                            "mission_source_catalog",
                            "mission_judge_tree",
                            "source_roots",
                            "untrusted_sources",
                        )
                    }
                else:
                    mission_sources = prepare_mission_tree(
                        self.store,
                        mission,
                        domain,
                        self.assembled.workspaces.artifact_store,
                        seed=seed,
                        files=files,
                    )
                copy = ensure_mission_tree(
                    self.store,
                    mission,
                    domain,
                    self.assembled.workspaces,
                    {**mission_sources, "attempt_id": view_id},
                )
            except ContractError as error:
                self.commit.fail_mission(
                    mission.id,
                    stop_reason=MissionStopReason.VERIFIER_UNAVAILABLE,
                    detail={"source_binding_error": str(error)},
                )
                return None
        else:
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
            not c.startswith(("pytest:", "file:", ACTION_PREFIX))
            and (document_coverage is None or c.startswith("arbitration:"))
            for c in mission.success_criteria
        )
        critic: CriticVerdict | None = None
        reused_critic = False
        if needs_critic:
            if (
                len(tasks) == 1
                and stored is not None
                and not requires_mission_source_binding(domain)
            ):
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
                        mission_source_binding=mission_sources,
                    )
                except (ContractError, BudgetExhausted) as error:
                    self._note(f"mission {mission.id}: independent judge unavailable ({error})")
        judgments: list[dict[str, Any]] = []
        for ordinal, criterion in enumerate(mission.success_criteria):
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
            elif (
                document_coverage is not None
                and document_coverage["criteria"][ordinal]["verdict"] != "STRUCTURAL"
            ):
                assessed = document_coverage["criteria"][ordinal]
                judgments.append(
                    {
                        "criterion": criterion,
                        "criterion_id": assessed["criterion_id"],
                        "met": assessed["verdict"] in {"PASS", "INCONCLUSIVE"},
                        "verdict": assessed["verdict"],
                        "judge": "document_coverage",
                        "reason": "; ".join(assessed["reasons"]),
                        "limitations": list(assessed["limitations"]),
                        "task_assessment_receipt_ids": list(
                            assessed["task_assessment_receipt_ids"]
                        ),
                    }
                )
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
        if await self._stop_document_insufficient(mission):
            return True
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
        plain = self._refresh_document_judgments(mission, plain)
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
        for ordinal, criterion in enumerate(mission.success_criteria):
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
