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
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState

from ..artifacts.versioning import (
    ArtifactConflict,
    UpstreamInput,
    ancestors,
    merge_accepted,
    next_versions,
)
from ..artifacts.workspace import sha256_file
from ..context.context_builder import (
    CONTEXT_BUILDER_VERSION,
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
from ..governance.budgets import BudgetExhausted
from ..graph.changes import ChangeLimits, TaskGraphChange
from ..memory.summaries import build_summaries
from ..memory.verified_knowledge import KnowledgeIndex
from ..planning.manager import terminal_task
from ..planning.planner import parse_task_graph_proposal
from ..runtime.agent_worker import AgentBridge, Liveness, user_message_json
from ..runtime.assembly import (
    AssembledOrchestratorRuntime,
    OrchestratorConfig,
    assemble_orchestrator_runtime,
)
from ..runtime.output_blocks import BlockError, extract_block, outside_text
from ..runtime.role_templates import (
    CRITIC,
    GRAPH_CHANGE_PROPOSAL_TAG,
    MANAGER,
    PLANNER,
    RESULT_ENVELOPE_TAG,
    role_for_task,
)
from ..runtime.tool_gateway import CRITIC_TOOLS, WORKER_TOOLS, WorkspaceBinding, run_pytest
from ..scheduling.allocator import allocate
from ..storage.store import DispatchIntent, InjectedCrash, Store, StoreBusy
from ..verification.critics import CriticVerdict, parse_critic_verdict
from ..verification.deterministic_checks import LayerResult
from ..verification.verifier_router import VerifierRouter
from .commit_service import (
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
)
MAX_CRITIC_ATTEMPTS = 2


class Orchestrator:
    def __init__(
        self,
        config: OrchestratorConfig,
        provider,  # type: ignore[no-untyped-def]
        *,
        owner: str | None = None,
        poll_interval: float = 0.05,
        critic_wait_seconds: float = 120.0,
    ) -> None:
        # D3-10': ``owner`` is this instance's identity for orchestration leases *and* for
        # the SDK runtime (``owner_id``); the SDK ``owner_scope`` is one constant for all.
        self._owner = owner or f"orchestrator-{os.getpid()}"
        self._config = replace(config, owner_id=self._owner)
        self._provider = provider
        self._poll = poll_interval
        self._critic_wait = critic_wait_seconds
        self._store: Store | None = None
        self._commit: CommitService | None = None
        self._assembled: AssembledOrchestratorRuntime | None = None
        self._bridge: AgentBridge | None = None
        self._router = VerifierRouter(test_timeout=config.test_timeout_seconds)
        self._critic_verdicts: dict[str, CriticVerdict] = {}
        self._client_ids: dict[str, str | None] = {}
        self._released: set[str] = set()
        self.progress_log: list[str] = []
        self.cancel_receipts: list[dict[str, Any]] = []

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> Orchestrator:
        self._store = Store.open(self._config.orchestrator_db)
        self._commit = CommitService(self._store, conflict_tasks=self._config.knowledge_sharing)
        self._assembled = assemble_orchestrator_runtime(self._config, self._provider)
        await self._assembled.runtime.__aenter__()
        self._bridge = AgentBridge(self._assembled.runtime, unpriced=self._config.unpriced)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._assembled is not None:
            await self._assembled.runtime.__aexit__(*exc_info)
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
    def bridge(self) -> AgentBridge:
        assert self._bridge is not None
        return self._bridge

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
        mission, _ = self.commit.create_mission(spec)
        return mission

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
        await self.bridge.recover()

    async def run(self, *, max_cycles: int = 10_000, until_idle: bool = True) -> None:
        """Drive the loop until idle.  ``max_cycles`` bounds *progressing* cycles (work
        done), never the waiting: a slow real model turn may keep the loop polling for
        many minutes and must not end the run early (step 4 real-run finding)."""

        await self.recover()
        cycles = 0
        idle_rounds = 0
        while cycles < max_cycles:
            progressed = await self._cycle()
            if progressed:
                cycles += 1
                idle_rounds = 0
                continue
            if not until_idle:
                return
            if self._has_inflight():
                idle_rounds = 0
                await asyncio.sleep(self._poll)
                continue
            idle_rounds += 1
            if idle_rounds >= 2:
                return
            await asyncio.sleep(self._poll)

    # ---------------------------------------------------------------- cycle
    def _active_missions(self) -> list[Mission]:
        return [m for m in self.store.list_missions() if m.status not in TERMINAL_MISSION]

    def _has_inflight(self) -> bool:
        """A submitted turn counts as in flight until it is collected — also for a
        terminal Mission (a superseded / cancelled Attempt's cost and late result are
        still collected); critic turns are collected inline by their runner."""

        return any(intent.kind != "critic" for intent in self.store.list_intents("SUBMITTED"))

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
        for stored in self.store.list_results_by_verification("PENDING", "RUNNING"):
            if stored.envelope.mission_id not in active:
                continue
            if await self._verify(stored.envelope.id):
                progressed = True
        for mission in self._active_missions():
            if await self._decide(mission):
                progressed = True
        return progressed

    # ------------------------------------------------------------- planning
    async def _start_planning(self, mission: Mission) -> None:
        self.commit.begin_planning(mission.id)
        await self._create_planner_intent(mission.id, ordinal=1)

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
        package = build_planner_package(
            mission,
            workspace_files=sorted(seed),
            attempt_ordinal=ordinal,
            rejected=self._planning_rejections(mission_id) if ordinal > 1 else (),
        )
        config = AgentConfig(
            name=f"planner-{ordinal}",
            instructions=PLANNER.instructions,
            model_profile_ref=self._config.model,
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
                "prompt_version": PLANNER.prompt_version,
                "base_version": mission.version,
                "ordinal": ordinal,
            },
            reservation=self._reservation(self._config.planner_reserve_tokens),
        )

    # -------------------------------------------------------------- dispatch
    def _reservation(self, tokens: int) -> Reservation:
        table = self._config.price_table
        if table is None:
            return Reservation(tokens=tokens, cost_micros=0)
        rate = max(table.input_micros_per_million_tokens, table.output_micros_per_million_tokens)
        return Reservation(tokens=tokens, cost_micros=(tokens * rate + 999_999) // 1_000_000)

    async def _dispatch(self, intent: DispatchIntent) -> bool:
        """ORCH §4.3 steps 2–3 with the identity frozen in the intent (D5')."""

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
            agent_id, _run_id, _ = await self.bridge.create(
                creation_key=claimed.creation_key, config_json=config["agent_config"]
            )
            expected = await self.bridge.expected_turn_id(
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
                receipt = await self.bridge.submit(
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

    def _bind_workspace(self, attempt: Attempt) -> None:
        mission = self.store.get_mission(attempt.mission_id)
        assert mission is not None
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        previous = None
        if attempt.retry_of is not None:
            previous = self.assembled.workspaces.root / attempt.retry_of
        inputs: dict[str, Path] = {}
        for item in self._upstream_inputs(attempt):
            artifact = self.store.get_artifact(item.artifact_id)
            source = None if artifact is None else Path(artifact.storage_uri)
            if source is None or not source.is_file() or sha256_file(source) != item.content_hash:
                raise ArtifactConflict(
                    f"upstream artifact {item.artifact_id} ({item.path}) is missing or changed"
                )
            inputs[item.path] = source
        workspace = self.assembled.workspaces.create(
            attempt.id, seed=seed, previous=previous, inputs=inputs
        )
        task = self.store.get_task(attempt.task_id)
        if task is not None:
            for path, content in self._protected_files(mission, task, attempt).items():
                if (
                    workspace.read_text(path) != content
                    if (workspace.root / path).is_file()
                    else True
                ):
                    workspace.write_text(path, content)

    def _bind_agent(self, agent_id: str, config: Mapping[str, Any]) -> None:
        self.assembled.gateway.bind(
            agent_id,
            WorkspaceBinding(
                str(config["attempt_id"]),
                "work",
                True,
                tuple(config.get("allowed_tools", WORKER_TOOLS)),
                tuple(str(p) for p in config.get("untrusted_sources", ())),
            ),
        )

    def _bind_critic(self, agent_id: str, config: Mapping[str, Any]) -> None:
        self.assembled.gateway.bind(
            agent_id,
            WorkspaceBinding(
                str(config["attempt_id"]),
                "verify",
                False,
                CRITIC_TOOLS,
                tuple(str(p) for p in config.get("untrusted_sources", ())),
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
        try:
            result = await self.bridge.result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
        except Exception as error:  # noqa: BLE001 - AgentNotFound & co.: not alive (P0-3)
            self._note(f"{intent.subject_id}: executor unreachable ({error})")
            result = None
        if result is None:
            return await self._observe_liveness(intent)
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
            result = await self.bridge.result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
        except Exception:  # noqa: BLE001 - executor gone: nothing more to collect
            result = None
            liveness = Liveness(False, None, False, None, None, False)
        else:
            liveness = (
                Liveness(True, None, False, None, None, True)
                if result is not None
                else await self.bridge.liveness(
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
        liveness: Liveness = await self.bridge.liveness(
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
            receipt = await self.bridge.runtime.cancel_turn(
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
                liveness = await self.bridge.liveness(
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
        facts = self.bridge.usage_facts(agent_id=intent.agent_id)
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
            await self._create_planner_intent(mission.id, ordinal=ordinal + 1)
        else:
            self.commit.fail_planning(
                mission.id, reason=reason, detail={"attempts": ordinal, **dict(detail)}
            )

    async def _collect_plan(self, intent: DispatchIntent, result) -> None:  # type: ignore[no-untyped-def]
        mission = self.store.get_mission(intent.mission_id)
        assert mission is not None
        self._import_usage(intent)
        text = "" if result.public_output is None else str(result.public_output.content)
        echoed = self.bridge.echoed_models(agent_id=intent.agent_id or "")
        if echoed and echoed != {self._config.model}:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self.commit.fail_planning(
                mission.id,
                reason="model_echo_mismatch",
                detail={"expected": self._config.model, "echoed": sorted(echoed)},
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
        echoed = self.bridge.echoed_models(agent_id=intent.agent_id or "")
        if echoed and echoed != {self._config.model}:
            # D10': the provider answered as a different model; charges are unknown and
            # the deployment binding is wrong.  Fail fast and visibly, hold the reservation.
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="model_echo_mismatch",
                detail={"expected": self._config.model, "echoed": sorted(echoed)},
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            self.commit.stop_task(
                attempt.task_id,
                stop_reason=MissionStopReason.MODEL_ECHO_MISMATCH,
                detail={"expected": self._config.model, "echoed": sorted(echoed)},
            )
            await self._release_mission(attempt.mission_id)
            self._note(f"attempt {attempt.id}: model echo mismatch {sorted(echoed)} → stopped")
            return
        if result.state is AgentTurnState.FAILED:
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="turn_failed",
                detail={"error": jsonable(result.error or {})},
            )
            self._settle_intent(intent, "FAILED")
            self._settle_if_known(attempt)
            await self._release_attempt(attempt.id, cancel=False)
            self._note(f"attempt {attempt.id}: SDK turn failed → RETRY_WAIT")
            return
        text = "" if result.public_output is None else str(result.public_output.content)
        try:
            envelope, client_result_id = self._parse_envelope(text, attempt, turn_id=result.turn_id)
        except ContractError as error:
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="envelope_invalid",
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
        missing = [path for path in envelope.artifacts if path not in known]
        if missing:
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
            path
            for path in listed
            if path in guarded and by_path[path].content_hash != guarded[path]
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
        copy = self.assembled.workspaces.verification_copy(attempt.id, protected=protected)
        artifacts = [
            a for a in self.store.list_artifacts(attempt.id) if a.id in set(stored.artifacts)
        ]

        async def recorder(layer: LayerResult) -> None:
            self._hold_lease(attempt.id)  # P1-3: a lost lease aborts the verification
            self.commit.record_verification_layer(
                result_id,
                layer=layer.layer,
                status=layer.status,
                detail={"summary": layer.summary, **dict(layer.detail)},
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
        )
        if verdict.critic is not None:
            self._critic_verdicts[result_id] = verdict.critic
        try:
            if verdict.passed:
                completed = self.commit.accept_result(
                    result_id,
                    verifier_results=[
                        layer.to_json() for layer in verdict.layers if layer.status == "PASS"
                    ],
                    owner=self._owner,
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
        else:
            self._note(f"result {result_id} FAIL at {verdict.short_circuited_at}")
        return True

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

    def _protected_files(self, mission: Mission, task: Task, attempt: Attempt) -> dict[str, str]:
        """Protected seed files plus every upstream input the Task did not declare as
        one of its ``outputs`` (D3-7': a downstream Worker may not silently rewrite what
        its dependencies delivered; rewriting an undeclared path needs a new Task)."""

        protected = self._protected_seed(mission, task)
        declared = set(task.outputs)
        for item in self._upstream_inputs(attempt):
            if item.path in declared:  # the Task declared it will rewrite this path
                continue
            artifact = self.store.get_artifact(item.artifact_id)
            source = None if artifact is None else Path(artifact.storage_uri)
            if source is None or not source.is_file():
                raise ArtifactConflict(
                    f"upstream artifact {item.artifact_id} ({item.path}) is missing"
                )
            try:
                protected[item.path] = source.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise ArtifactConflict(f"upstream artifact {item.path} is not text") from error
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
        if rounds >= self._config.max_manager_rounds:
            self.commit.stop_task(
                task.id,
                stop_reason=MissionStopReason.MANAGEMENT_EXHAUSTED,
                detail={
                    "rounds": rounds,
                    "max_manager_rounds": self._config.max_manager_rounds,
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
            "no_progress_limit": self._config.no_progress_limit,
            "management_rounds_remaining": self._config.max_manager_rounds - rounds,
        }
        try:
            knowledge = self._gather_knowledge(mission, task, {t.id: t for t in tasks})
        except RetrievalUnavailable as error:
            knowledge = KnowledgeContext.unavailable(str(error))
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
        )
        config = AgentConfig(
            name=f"manager-{rounds + 1}",
            instructions=MANAGER.instructions,
            model_profile_ref=self._config.model,
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
                "prompt_version": MANAGER.prompt_version,
                "task_id": task.id,
                "trigger": trigger,
                "result_id": result_id,
                "attempt_id": attempt_id,
                "graph_version": int(report.get("graph_version") or 1),
                "no_progress_count": no_progress,
            },
            reservation=self._reservation(self._config.manager_reserve_tokens),
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
        if count >= self._config.no_progress_limit:
            self.commit.stop_task(
                task.id,
                stop_reason=MissionStopReason.NO_PROGRESS,
                detail={
                    "no_progress_count": count,
                    "no_progress_limit": self._config.no_progress_limit,
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
        echoed = self.bridge.echoed_models(agent_id=intent.agent_id or "")
        if echoed and echoed != {self._config.model}:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self.commit.stop_task(
                task_id,
                stop_reason=MissionStopReason.MODEL_ECHO_MISMATCH,
                detail={"expected": self._config.model, "echoed": sorted(echoed)},
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
            change = TaskGraphChange.from_json(raw)
        except (ContractError, BlockError) as error:
            self._settle_intent(intent, "FAILED")
            self._settle_service_if_known(intent.subject_id, mission.id)
            self._note(f"manager proposal unusable for {task_id}: {error}")
            await self._manager_unusable(intent, reason=f"proposal_unreadable: {error}")
            return
        self._settle_intent(intent, "SETTLED")
        self._settle_service_if_known(intent.subject_id, mission.id)
        task = self.store.get_task(task_id)
        assert task is not None
        limits = ChangeLimits(
            max_graph_depth=self._config.max_graph_depth,
            max_proposals_per_agent=self._config.max_proposals_per_agent,
            max_supersede_chain=self._config.max_supersede_chain,
        )
        no_progress = int(intent.config.get("no_progress_count", 0))
        if not change.operations or all(op.op == "set_priority" for op in change.operations):
            self.commit.record_management_decided(
                mission.id,
                task_id=task_id,
                trigger=trigger,
                decision="keep",
                detail={"rationale": change.rationale},
            )
            if no_progress >= self._config.no_progress_limit:
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
        untrusted = [str(p) for p in (mission.final_report or {}).get("untrusted_sources", [])]
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
        )
        task_id = None if task is None else task.id
        last_error: ContractError | None = None
        for ordinal in range(1, MAX_CRITIC_ATTEMPTS + 1):
            subject = f"{subject_prefix}:{ordinal}"
            config = AgentConfig(
                name=f"critic-{ordinal}",
                instructions=CRITIC.instructions,
                model_profile_ref=self._config.model,
                tool_names=CRITIC.tool_names,
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
                    "prompt_version": CRITIC.prompt_version,
                    "untrusted_sources": untrusted,
                },
                reservation=self._reservation(self._config.critic_reserve_tokens),
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
                result = await self.bridge.result(
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
            await self._judge(current, live)
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
        plan = allocate(
            tasks,
            attempts,
            concurrency_limit=self._config.max_concurrency,
            candidates_per_task=self._config.candidates_per_task,
        )
        progressed = False
        for granted, _candidate in plan.grants:
            current_task = self.store.get_task(granted.id)
            assert current_task is not None
            task = current_task
            if task.status in TERMINAL_TASK:
                continue
            if await self._next_attempt(mission, task, self.store.list_attempts(task.id)):
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
        self, mission: Mission, task: Task, attempts: Sequence[Attempt]
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
        role = role_for_task(task)  # D5-9: the Manager may have switched the approach
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
        placeholder = Attempt(
            id=ids.attempt_id(task.id, len(attempts) + 1),
            task_id=task.id,
            mission_id=mission.id,
            role=role.name,
            model=self._config.model,
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
        previous_files = sorted({*seed, *(item.path for item in inputs)})
        if previous is not None:
            try:
                previous_files = self.assembled.workspaces.get(previous.id).list_files()
            except Exception:  # noqa: BLE001
                pass
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
        )
        allowed = tuple(name for name in role.tool_names if name in set(task.allowed_tools))
        config = AgentConfig(
            name=f"{role.name}-{placeholder.ordinal}",
            instructions=role.instructions,
            model_profile_ref=self._config.model,
            tool_names=allowed,
            limits=AgentLimits(
                max_model_calls_per_turn=self._config.max_model_calls_per_turn,
                max_tool_calls_per_turn=self._config.max_tool_calls_per_turn,
                turn_deadline_seconds=min(
                    self._config.turn_deadline_seconds,
                    float(task.budget.max_runtime_seconds or self._config.turn_deadline_seconds),
                ),
            ),
        )
        message = user_message_json(package.text)
        tokens = self._config.attempt_reserve_tokens
        if task.budget.max_tokens is not None:
            # D3-5': explorative candidates share the Task's token budget evenly
            tokens = min(tokens, max(1, task.budget.max_tokens // self._config.candidates_per_task))
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
                model=self._config.model,
                prompt_version=role.prompt_version,
                context_version=package.context_version,
                reservation=self._reservation(tokens),
                intent_config={
                    "agent_config": config.to_json(),
                    "message": message,
                    "attempt_id": placeholder.id,
                    "allowed_tools": list(allowed),
                    "context_version": package.context_version,
                    "prompt_version": role.prompt_version,
                    "task_version": task.version,
                    "role": role.name,
                    "knowledge": knowledge.frozen_ids,  # D4-10: what this Attempt saw
                    "retrieval_version": knowledge.retrieval.version,
                    "retrieval_status": knowledge.retrieval.status,
                    "context_builder_version": CONTEXT_BUILDER_VERSION,
                    "untrusted_sources": untrusted,
                },
                input_hash=sha256_hex(message),
                retry_of=placeholder.retry_of,
                feedback=feedback,
                candidates_per_task=self._config.candidates_per_task,
                inputs=[item.to_json() for item in inputs],
                max_open_attempts=self._config.max_concurrency,
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
            if error.account_id == mission_account(mission.id):
                # D3-12': the Mission pool itself is exhausted (any dimension) — no Task
                # is to blame and the stop reason is the pool's: budget_exhausted
                reason = MissionStopReason.BUDGET_EXHAUSTED
                self.commit.fail_mission(mission.id, stop_reason=reason, detail=detail)
                self._note(
                    f"mission {mission.id} stopped: {reason} ({error.dimension}, mission pool)"
                )
            else:
                self.commit.stop_task(task.id, stop_reason=reason, detail=detail)
                self._note(f"task {task.id} stopped: {reason} ({error.dimension})")
            await self._release_mission(mission.id)
            return True
        self._note(
            f"attempt {attempt.id} created (retry_of={attempt.retry_of}, inputs={len(inputs)})"
        )
        return True

    async def _judge(self, mission: Mission, tasks: Sequence[Task]) -> None:
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
            return
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        files: dict[str, Path] = {}
        artifacts: list[Artifact] = []
        for item in merged:
            artifact = self.store.get_artifact(item.artifact_id)
            if artifact is None:
                continue
            files[item.path] = Path(artifact.storage_uri)
            artifacts.append(artifact)
        # P0-2: one judgment tree per orchestrator instance — another instance may be
        # running pytest in its own; the judgment Commit itself is idempotent
        view_id = f"{mission.id}-judge-{self._owner}"
        copy = self.assembled.workspaces.integrated_copy(view_id, seed=seed, files=files)
        terminal = terminal_task(list(tasks))
        stored = self.store.get_result(terminal.accepted_result_id or "")
        summary = "" if stored is None else stored.envelope.summary
        test_runs: dict[str, dict[str, Any]] = {}
        for criterion in mission.success_criteria:
            if not criterion.startswith("pytest:"):
                continue
            target = criterion.removeprefix("pytest:").strip() or None
            try:
                if target is not None:
                    copy.resolve(target)
                test_run = await run_pytest(
                    str(copy.root), path=target, timeout=self._config.test_timeout_seconds
                )
                test_runs[criterion] = {**test_run.to_json(), "passed": test_run.passed}
            except Exception as error:  # noqa: BLE001
                test_runs[criterion] = {"passed": False, "error": str(error), "stdout": ""}
        needs_critic = any(not c.startswith(("pytest:", "file:")) for c in mission.success_criteria)
        critic: CriticVerdict | None = None
        if needs_critic:
            if len(tasks) == 1 and stored is not None:
                critic = self._critic_verdicts.get(stored.envelope.id)
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
        judgments = []
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
                        "reason": "no independent judge ran"
                        if found is None
                        else found.get("reason"),
                    }
                )
        judged = self.commit.judge_mission(mission.id, judgments=judgments, summary=summary)
        self._note(f"mission {mission.id} judged: {judged.status} ({judged.stop_reason})")


def sha256_hex_text(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = ("FAULT_POINTS", "InjectedCrash", "Orchestrator")
