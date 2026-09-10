# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""The Orchestrator control loop (§4, §7.5, §24) for the single-Task Mission of step 2.

Observe → Plan → Allocate → Execute → Verify → Commit → Repeat, as a
deterministic *workflow shell* around the model calls (theory 08-8): every
action below is an idempotent Commit, so ``run()`` can be interrupted at any
instruction and restarted (``recover()`` first) without a second execution, a
second delivery or a second charge.  Fault points (``self._fault(...)``) mark the
six cross-database crash instants of the recovery matrix (plan D14').
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState

from ..context.context_builder import (
    build_critic_package,
    build_planner_package,
    build_worker_package,
)
from ..contracts import (
    TERMINAL_MISSION,
    Artifact,
    Attempt,
    AttemptStatus,
    ContractError,
    Mission,
    MissionStatus,
    MissionStopReason,
    ResultEnvelope,
    Task,
    TaskStatus,
    ids,
)
from ..contracts.models import sha256_hex
from ..governance.budgets import BudgetExhausted
from ..planning.planner import parse_task_proposal
from ..runtime.agent_worker import AgentBridge, Liveness, user_message_json
from ..runtime.assembly import (
    AssembledOrchestratorRuntime,
    OrchestratorConfig,
    assemble_orchestrator_runtime,
)
from ..runtime.output_blocks import BlockError, extract_block, outside_text
from ..runtime.role_templates import CRITIC, PLANNER, RESULT_ENVELOPE_TAG, WORKER
from ..runtime.tool_gateway import CRITIC_TOOLS, WORKER_TOOLS, WorkspaceBinding
from ..storage.store import DispatchIntent, InjectedCrash, Store
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
)
MAX_PLANNING_ATTEMPTS = 2
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
        self._config = config
        self._provider = provider
        self._owner = owner or f"orchestrator-{os.getpid()}"
        self._poll = poll_interval
        self._critic_wait = critic_wait_seconds
        self._store: Store | None = None
        self._commit: CommitService | None = None
        self._assembled: AssembledOrchestratorRuntime | None = None
        self._bridge: AgentBridge | None = None
        self._router = VerifierRouter(test_timeout=config.test_timeout_seconds)
        self._critic_verdicts: dict[str, CriticVerdict] = {}
        self._client_ids: dict[str, str | None] = {}
        self.progress_log: list[str] = []

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> Orchestrator:
        self._store = Store.open(self._config.orchestrator_db)
        self._commit = CommitService(self._store)
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

    def arm_fault(self, point: str, *, kind: str | None = None) -> None:
        """Arm a crash at ``point``; ``kind`` restricts it to plan / attempt / critic intents."""

        if point not in FAULT_POINTS:
            raise ValueError(f"unknown fault point {point}")
        self.store.arm(point if kind is None else f"{point}:{kind}")

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
        """§16.4 recovery: wake SDK turns first, then let the loop re-drive intents."""

        await self.bridge.recover()

    async def run(self, *, max_cycles: int = 10_000, until_idle: bool = True) -> None:
        await self.recover()
        cycles = 0
        idle_rounds = 0
        while cycles < max_cycles:
            cycles += 1
            progressed = await self._cycle()
            if progressed:
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
        return bool(self.store.list_intents("SUBMITTED"))

    async def _cycle(self) -> bool:
        progressed = False
        for mission in self._active_missions():
            if mission.status is MissionStatus.CREATED:
                await self._start_planning(mission)
                progressed = True
        for intent in self.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED"):
            if await self._dispatch(intent):
                progressed = True
        for intent in self.store.list_intents("SUBMITTED"):
            if intent.kind == "critic":
                continue  # collected inline by the critic runner
            if await self._collect(intent):
                progressed = True
        for stored in self.store.list_results_by_verification("PENDING", "RUNNING"):
            await self._verify(stored.envelope.id)
            progressed = True
        for mission in self._active_missions():
            if await self._decide(mission):
                progressed = True
        return progressed

    # ------------------------------------------------------------- planning
    async def _start_planning(self, mission: Mission) -> None:
        self.commit.begin_planning(mission.id)
        await self._create_planner_intent(mission.id, ordinal=1)

    async def _create_planner_intent(self, mission_id: str, *, ordinal: int) -> DispatchIntent:
        mission = self.store.get_mission(mission_id)
        assert mission is not None
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        package = build_planner_package(
            mission, workspace_files=sorted(seed), attempt_ordinal=ordinal
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
            self._bind_workspace(attempt)
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
            receipt = await self.bridge.submit(
                agent_id=claimed.agent_id, input_id=claimed.input_id, message_json=config["message"]
            )
            self._fault("after_submit", claimed.kind)
            self.commit.record_submitted(claimed.intent_id, receipt=receipt)
            self._note(f"dispatched {claimed.kind} {claimed.subject_id} → agent {claimed.agent_id}")
        return True

    def _bind_workspace(self, attempt: Attempt) -> None:
        mission = self.store.get_mission(attempt.mission_id)
        assert mission is not None
        seed = dict((mission.final_report or {}).get("workspace_seed", {}))
        previous = None
        if attempt.retry_of is not None:
            previous = self.assembled.workspaces.root / attempt.retry_of
        self.assembled.workspaces.create(attempt.id, seed=seed, previous=previous)

    def _bind_agent(self, agent_id: str, config: Mapping[str, Any]) -> None:
        self.assembled.gateway.bind(
            agent_id,
            WorkspaceBinding(
                str(config["attempt_id"]),
                "work",
                True,
                tuple(config.get("allowed_tools", WORKER_TOOLS)),
            ),
        )

    def _bind_critic(self, agent_id: str, config: Mapping[str, Any]) -> None:
        self.assembled.gateway.bind(
            agent_id, WorkspaceBinding(str(config["attempt_id"]), "verify", False, CRITIC_TOOLS)
        )

    # --------------------------------------------------------------- collect
    async def _collect(self, intent: DispatchIntent) -> bool:
        assert intent.agent_id is not None and intent.expected_turn_id is not None
        result = await self.bridge.result(agent_id=intent.agent_id, turn_id=intent.expected_turn_id)
        if result is None:
            return await self._observe_liveness(intent)
        self._fault("after_turn_committed", intent.kind)
        if intent.kind == "plan":
            await self._collect_plan(intent, result)
        elif intent.kind == "attempt":
            await self._collect_attempt(intent, result)
        return True

    async def _observe_liveness(self, intent: DispatchIntent) -> bool:
        if intent.kind != "attempt":
            return False
        attempt = self.store.get_attempt(intent.subject_id)
        assert attempt is not None and intent.agent_id and intent.expected_turn_id
        liveness: Liveness = await self.bridge.liveness(
            agent_id=intent.agent_id, turn_id=intent.expected_turn_id
        )
        now = self.store.now
        if liveness.alive:
            if (
                liveness.blocked
                or attempt.lease_expires_at is None
                or now >= attempt.lease_expires_at - self._config.lease_seconds / 2
            ):
                self.commit.renew_lease(
                    attempt.id,
                    owner=self._owner,
                    lease_seconds=self._config.lease_seconds,
                    liveness=liveness.to_json(),
                )
            return False
        if not liveness.exists:
            self.commit.mark_attempt_lost(attempt.id, reason="executor_turn_missing")
            self._settle_intent(intent, "FAILED")
            self._note(f"attempt {attempt.id} LOST: turn missing")
            return True
        return False

    def _settle_intent(self, intent: DispatchIntent, state: str) -> None:
        current = self.store.get_intent(intent.intent_id)
        assert current is not None
        updated = DispatchIntent(
            **{**current.to_json(), "state": state, "version": current.version + 1}
        )
        self.store.update_intent(updated, expected_version=current.version)

    def _import_usage(self, intent: DispatchIntent) -> None:
        assert intent.agent_id is not None
        facts = self.bridge.usage_facts(agent_id=intent.agent_id)
        self.commit.import_usage(intent.subject_id, intent.mission_id, facts)

    async def _collect_plan(self, intent: DispatchIntent, result) -> None:  # type: ignore[no-untyped-def]
        mission = self.store.get_mission(intent.mission_id)
        assert mission is not None
        self._import_usage(intent)
        ordinal = int(intent.config.get("ordinal", 1))
        text = "" if result.public_output is None else str(result.public_output.content)
        try:
            if result.state is not AgentTurnState.COMMITTED:
                raise ContractError(f"planner turn failed: {dict(result.error or {})}")
            proposal = parse_task_proposal(text)
            task, _ = self.commit.commit_task_proposal(
                mission.id,
                proposal,
                base_version=int(intent.config["base_version"]),
                source={
                    "intent_id": intent.intent_id,
                    "agent_id": intent.agent_id,
                    "turn_id": result.turn_id,
                },
            )
            self._note(f"task committed {task.id}")
        except (ContractError, CommitRejected) as error:
            self._note(f"planning attempt {ordinal} rejected: {error}")
            self._settle_intent(intent, "FAILED")
            self.commit.settle_subject(intent.subject_id, mission.id)
            if ordinal < MAX_PLANNING_ATTEMPTS:
                await self._create_planner_intent(mission.id, ordinal=ordinal + 1)
            else:
                self.commit.fail_planning(
                    mission.id, reason=str(error), detail={"attempts": ordinal}
                )
            return
        self._settle_intent(intent, "SETTLED")
        self.commit.settle_subject(intent.subject_id, mission.id)

    async def _collect_attempt(self, intent: DispatchIntent, result) -> None:  # type: ignore[no-untyped-def]
        attempt = self.store.get_attempt(intent.subject_id)
        assert attempt is not None
        self._import_usage(intent)
        if attempt.status is not AttemptStatus.RUNNING:
            self._settle_intent(intent, "SETTLED")
            return
        if (
            result.state is not AgentTurnState.FAILED
            and self.bridge.has_unknown_charge(agent_id=intent.agent_id or "")
            and not self._config.unpriced
        ):
            self._note(f"attempt {attempt.id}: unknown provider charge, holding reservation")
        if result.state is AgentTurnState.FAILED:
            self.commit.reject_result(
                attempt.id,
                turn_id=result.turn_id,
                reason="turn_failed",
                detail={"error": dict(result.error or {})},
            )
            self._settle_intent(intent, "FAILED")
            self.commit.settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)
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
            self.commit.settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)
            self._note(f"attempt {attempt.id}: envelope invalid → RETRY_WAIT ({error})")
            return
        workspace = self.assembled.workspaces.get(attempt.id)
        artifacts = workspace.snapshot(
            mission_id=attempt.mission_id,
            task_id=attempt.task_id,
            produced_by=intent.agent_id or attempt.id,
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
            self.commit.settle_subject(attempt.id, attempt.mission_id, task_id=attempt.task_id)
            self._note(f"attempt {attempt.id}: artifacts missing → RETRY_WAIT")
            return
        referenced = [
            artifact for artifact in artifacts if artifact.path in set(envelope.artifacts)
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
    async def _verify(self, result_id: str) -> None:
        stored = self.commit.start_verification(result_id)
        attempt = self.store.get_attempt(stored.envelope.attempt_id)
        task = self.store.get_task(stored.envelope.task_id)
        mission = self.store.get_mission(stored.envelope.mission_id)
        assert attempt is not None and task is not None and mission is not None
        copy = self.assembled.workspaces.verification_copy(attempt.id)
        artifacts = [
            a for a in self.store.list_artifacts(attempt.id) if a.id in set(stored.artifacts)
        ]

        async def recorder(layer: LayerResult) -> None:
            self.commit.record_verification_layer(
                result_id,
                layer=layer.layer,
                status=layer.status,
                detail={"summary": layer.summary, **dict(layer.detail)},
            )
            if layer.status == "PASS":
                self._fault("after_layer_pass", "attempt")

        async def run_critic(test_output: str | None) -> CriticVerdict:
            return await self._run_critic(mission, task, attempt, artifacts, test_output)

        verdict = await self._router.verify(
            mission=mission,
            task=task,
            envelope=stored.envelope,
            artifacts=artifacts,
            verification_copy=copy,
            client_result_id=self._client_ids.get(result_id),
            run_critic=run_critic,
            recorder=recorder,
        )
        if verdict.critic is not None:
            self._critic_verdicts[result_id] = verdict.critic
        if verdict.passed:
            self.commit.accept_result(
                result_id,
                verifier_results=[
                    layer.to_json() for layer in verdict.layers if layer.status == "PASS"
                ],
            )
            self._note(f"result {result_id} PASS → task {task.id} COMPLETED")
        else:
            self.commit.fail_result(result_id, failures=verdict.failures)
            self._note(f"result {result_id} FAIL at {verdict.short_circuited_at}")

    async def _run_critic(
        self,
        mission: Mission,
        task: Task,
        attempt: Attempt,
        artifacts: Sequence[Artifact],
        test_output: str | None,
    ) -> CriticVerdict:
        copy = self.assembled.workspaces.verification_view(attempt.id)
        package = build_critic_package(
            mission,
            task,
            attempt_id=attempt.id,
            artifacts=[
                {"path": a.path, "content_hash": a.content_hash, "size_bytes": a.size_bytes}
                for a in artifacts
            ],
            test_output=test_output,
            workspace_files=copy.list_files(),
        )
        last_error: ContractError | None = None
        for ordinal in range(1, MAX_CRITIC_ATTEMPTS + 1):
            subject = f"{attempt.id}:critic:{ordinal}"
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
                account_id=task_account(task.id),
                creation_key=subject,
                input_id="attempt-input",
                input_hash=sha256_hex(message),
                config={
                    "agent_config": config.to_json(),
                    "message": message,
                    "attempt_id": attempt.id,
                    "context_version": package.context_version,
                    "prompt_version": CRITIC.prompt_version,
                },
                reservation=self._reservation(self._config.critic_reserve_tokens),
                task_id=task.id,
                attempt_id=attempt.id,
            )
            while intent.state in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
                await self._dispatch(intent)
                refreshed = self.store.get_intent(intent.intent_id)
                assert refreshed is not None
                intent = refreshed
            assert intent.agent_id and intent.expected_turn_id
            deadline = self.store.now + self._critic_wait
            result = None
            while self.store.now < deadline:
                result = await self.bridge.result(
                    agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                )
                if result is not None:
                    break
                await asyncio.sleep(self._poll)
            self._import_usage(intent)
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
                self.commit.settle_subject(subject, mission.id, task_id=task.id)
                continue
            self._settle_intent(intent, "SETTLED")
            self.commit.settle_subject(subject, mission.id, task_id=task.id)
            return verdict
        assert last_error is not None
        raise last_error

    # --------------------------------------------------------------- decide
    async def _decide(self, mission: Mission) -> bool:
        progressed = False
        for task in self.store.list_tasks(mission.id):
            if task.status is TaskStatus.COMPLETED and mission.status is MissionStatus.ACTIVE:
                await self._judge(mission, task)
                return True
            if task.status not in {TaskStatus.READY, TaskStatus.ACTIVE}:
                continue
            attempts = self.store.list_attempts(task.id)
            if any(
                a.status
                not in {
                    AttemptStatus.RETRY_WAIT,
                    AttemptStatus.LOST,
                    AttemptStatus.TIMED_OUT,
                    AttemptStatus.CANCELLED,
                    AttemptStatus.SUPERSEDED,
                    AttemptStatus.COMPLETED,
                }
                for a in attempts
            ):
                continue  # an Attempt is in flight
            if await self._next_attempt(mission, task, attempts):
                progressed = True
        return progressed

    async def _next_attempt(
        self, mission: Mission, task: Task, attempts: Sequence[Attempt]
    ) -> bool:
        previous = attempts[-1] if attempts else None
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
        placeholder = Attempt(
            id=ids.attempt_id(task.id, len(attempts) + 1),
            task_id=task.id,
            mission_id=mission.id,
            role=WORKER.name,
            model=self._config.model,
            prompt_version=WORKER.prompt_version,
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
        previous_files = sorted(seed)
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
        )
        allowed = tuple(name for name in WORKER_TOOLS if name in set(task.allowed_tools))
        config = AgentConfig(
            name=f"worker-{placeholder.ordinal}",
            instructions=WORKER.instructions,
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
            tokens = min(tokens, task.budget.max_tokens)
        try:
            attempt, _intent = self.commit.create_attempt(
                task.id,
                role=WORKER.name,
                model=self._config.model,
                prompt_version=WORKER.prompt_version,
                context_version=package.context_version,
                reservation=self._reservation(tokens),
                intent_config={
                    "agent_config": config.to_json(),
                    "message": message,
                    "attempt_id": placeholder.id,
                    "allowed_tools": list(allowed),
                    "context_version": package.context_version,
                    "prompt_version": WORKER.prompt_version,
                    "task_version": task.version,
                },
                input_hash=sha256_hex(message),
                retry_of=placeholder.retry_of,
                feedback=feedback,
            )
        except BudgetExhausted as error:
            reason = (
                MissionStopReason.MAX_ATTEMPTS_REACHED
                if error.dimension == "attempts"
                else MissionStopReason.BUDGET_EXHAUSTED
            )
            self.commit.stop_task(
                task.id,
                stop_reason=reason,
                detail={
                    "dimension": error.dimension,
                    "requested": error.requested,
                    "remaining": error.remaining,
                    "account": error.account_id,
                },
            )
            self._note(f"task {task.id} stopped: {reason} ({error.dimension})")
            return True
        self._note(f"attempt {attempt.id} created (retry_of={attempt.retry_of})")
        return True

    async def _judge(self, mission: Mission, task: Task) -> None:
        stored = self.store.get_result(task.accepted_result_id or "")
        layers = (
            {}
            if stored is None
            else {item["layer"]: item for item in self.store.list_verifications(stored.envelope.id)}
        )
        critic = None if stored is None else self._critic_verdicts.get(stored.envelope.id)
        copy = None
        try:
            copy = self.assembled.workspaces.verification_view(
                task.accepted_result_id and stored.envelope.attempt_id or ""
            )
        except Exception:  # noqa: BLE001
            copy = None
        judgments = []
        for criterion in mission.success_criteria:
            if criterion.startswith("pytest:"):
                layer = layers.get("code_test")
                met = bool(layer and layer["status"] == "PASS")
                judgments.append(
                    {
                        "criterion": criterion,
                        "met": met,
                        "judge": "code_test",
                        "reason": None if layer is None else layer["detail"].get("summary"),
                    }
                )
            elif criterion.startswith("file:"):
                relative = criterion.removeprefix("file:")
                met = bool(copy is not None and copy.resolve(relative).is_file())
                judgments.append(
                    {
                        "criterion": criterion,
                        "met": met,
                        "judge": "rule_check",
                        "reason": "file exists" if met else "file missing",
                    }
                )
            else:
                item = None
                if critic is not None:
                    item = next(
                        (c for c in critic.mission_criteria if c.get("criterion") == criterion),
                        None,
                    )
                judgments.append(
                    {
                        "criterion": criterion,
                        "met": bool(item and item.get("met")),
                        "judge": "critic_review",
                        "reason": None
                        if item is None
                        else item.get("reason", "no independent judge ran"),
                    }
                )
        judged = self.commit.judge_mission(
            mission.id,
            judgments=judgments,
            summary="" if stored is None else stored.envelope.summary,
        )
        self._note(f"mission {mission.id} judged: {judged.status} ({judged.stop_reason})")


__all__ = ("FAULT_POINTS", "InjectedCrash", "Orchestrator")
