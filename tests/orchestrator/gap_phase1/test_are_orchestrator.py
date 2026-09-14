"""Real official ARE objects and SDK Orchestrator; scripted provider, no network/judge."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

pytest.importorskip("are")

from are.simulation.apps.app import App
from are.simulation.notification_system import BaseNotificationSystem, Message, MessageType
from are.simulation.scenarios.scenario import Scenario
from are.simulation.time_manager import TimeManager
from are.simulation.tool_utils import app_tool, env_tool
from are.simulation.types import OracleEvent

from agent_orchestrator.evaluation.are_bridge import AREWorkerStillRunning, SimpleHarnessAREAgent
from agent_orchestrator.evaluation.are_orchestrator import (
    RUNTIME_TERMINATION_BOUNDARY,
    AREArgumentsError,
    AREOrchestratorRunner,
    _decode,
)
from agent_orchestrator.evaluation.experiment import (
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
    RunContext,
)
from agent_orchestrator.governance.domains import ARE_PROFILE, resolve_domain
from agent_orchestrator.runtime.role_templates import ROLES, template_for_domain
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
    role_of,
)
from agent_orchestrator.verification.domain_handlers import handler_for
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer
from simple_harness.providers import ProviderTarget


class Counter:
    fingerprint = "synthetic-are-counter-v1"
    bound_protocol = "synthetic-fixed-v1"
    requires_prior_output_reserve = False

    def estimate_input_tokens(self, request):
        return 8000


def context(*, calls=30, seconds=30, slots=2, reports=None):
    manifest = ExperimentManifest(
        "synthetic-are",
        "fixture",
        "agent-model",
        ExperimentBudget(900000, 100000, 1000000, calls, seconds),
        ("public-note",),
        1,
        0,
        slots,
        tuple(ArmSpec(arm, "sdk-are-v1") for arm in ("S", "R", "D", "F")),
    )
    return RunContext(
        manifest, manifest.runs()[2], reports.append if reports is not None else lambda _: None
    )


def runner(tmp_path, provider, **kwargs):
    provider.target = ProviderTarget(
        "fixture", "agent-model", "fixture", "https://example.test", "v1"
    )
    return AREOrchestratorRunner(
        provider=provider,
        context=kwargs.pop("context", context()),
        token_counter=Counter(),
        tokenizer=UpperBoundTokenizer(),
        context_policy=ContextPolicy(),
        evidence_root=tmp_path / "episode",
        default_output_tokens=1024,
        maximum_output_tokens=4096,
        dynamic_graph=False,
        poll_seconds=0.005,
        **kwargs,
    )


class PublicApp(App):
    def __init__(self):
        super().__init__(name="public")
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False
        self.hidden = "PRIVATE_APP_STATE"

    @app_tool()
    def read(self, payload: dict, suffix: str = "DEFAULT") -> str:
        """Read an original public argument object.

        :param payload: Unmodified argument object.
        :param suffix: Optional suffix.
        """
        self.calls.append((payload, suffix))
        if self.block:
            self.entered.set()
            assert self.release.wait(8)
        return json.dumps(payload, ensure_ascii=False) + suffix

    @env_tool()
    def hidden_action(self) -> str:
        """Environment-only action."""
        return self.hidden


def environment(turns=1):
    app = PublicApp()
    scenario = Scenario(
        scenario_id="public-id",
        apps=[app],
        start_time=900.0,
        nb_turns=turns,
        events=[OracleEvent(event_id="PRIVATE_ORACLE")],
        comment="PRIVATE_JUDGE_SCORE",
        additional_system_prompt="PUBLIC_SYSTEM_PROMPT",
    )
    clock = TimeManager()
    clock.reset(start_time=1000.0)
    system = BaseNotificationSystem()
    system.initialize(clock)
    put(
        system,
        MessageType.USER_MESSAGE,
        "Read the public payload and consume late conditions",
        1000,
    )
    return app, scenario, clock, system


def put(system, kind, text, at):
    system.message_queue.put(Message(kind, text, datetime.fromtimestamp(at, tz=UTC)))


def proposal():
    return graph_proposal_step(
        [
            {
                "key": "answer",
                "goal": "Read and consume notifications; answer in REPORT.md",
                "rationale": "one visible task",
                "dependencies": [],
                "success_criteria": ["file:REPORT.md"],
                "verification_policy": ["format_check", "rule_check", "critic_review"],
                "allowed_tools": [
                    "public__read",
                    "poll_notifications",
                    "workspace_write_file",
                    "workspace_read_file",
                    "workspace_list",
                ],
                "budget": {"max_tokens": 400000, "max_attempts": 1},
                "priority": 1.0,
                "outputs": ["REPORT.md"],
            }
        ]
    )


def scripts(*, worker=None):
    return {
        "planner": [proposal()],
        "worker": worker
        or [
            (
                "public__read",
                {
                    "arguments_json": ('{"payload":{"path":"/original","password":"synthetic",'
                                       '"nested":[1,null,true]}}')
                },
            ),
            ("workspace_write_file", {"path": "REPORT.md", "content": "observed DEFAULT"}),
            envelope_step(
                summary="observed DEFAULT",
                artifacts=["REPORT.md"],
                claims=["Read the public payload"],
            ),
        ],
        "critic": [critic_step(verdict="PASS", criteria_met=True)],
    }


def effects(run, turn=1):
    with sqlite3.connect(run.root / f"turn-{turn:04d}" / "orchestrator" / "execution.db") as db:
        return db.execute(
            "select tool_name, state, handoff_attempt, rehandoff_count from execution_effects"
        ).fetchall()


def test_real_mission_delayed_notification_consumed_and_clock_not_paused(tmp_path):
    app, scenario, clock, system = environment()
    late_ready = threading.Event()

    class Provider(RoleScriptedProvider):
        async def invoke(self, request, *, cancel):
            if role_of(request) == "worker" and not late_ready.is_set():
                # Mission already in progress; enqueue a future notification and
                # advance ARE time by one hour without waiting one wall hour.
                put(
                    system,
                    MessageType.ENVIRONMENT_NOTIFICATION,
                    "LATE_CONDITION: answer BLUE",
                    4500,
                )
                put(system, MessageType.USER_MESSAGE, "LATE_USER: suffix GREEN", 4501)
                clock.add_offset(3600)
                await asyncio.sleep(0.04)
                late_ready.set()
            return await super().invoke(request, cancel=cancel)

    def observed(request):
        wire = str(request.messages)
        (tmp_path / "observed.txt").write_text(wire)
        assert "LATE_CONDITION: answer BLUE" in wire
        assert "LATE_USER: suffix GREEN" in wire
        assert "4600" in wire or "01:16:" in wire
        assert "DEFAULT" in wire
        return "workspace_write_file", {"path": "REPORT.md", "content": "BLUE GREEN DEFAULT"}

    worker = scripts()["worker"][:1] + [
        ("poll_notifications", {}),
        observed,
        envelope_step(
            summary="BLUE GREEN DEFAULT",
            artifacts=["REPORT.md"],
            claims=["Read the public payload"],
        ),
    ]
    provider = Provider(scripts(worker=worker))
    handoffs, reports = [], []
    run = runner(
        tmp_path,
        provider,
        before_handoff=handoffs.append,
        max_inflight_tokens=18048,
        context=context(reports=reports),
    )
    agent = SimpleHarnessAREAgent(run, poll_seconds=0.005)
    result = agent.run_scenario(scenario, system)
    assert result.output == "BLUE GREEN DEFAULT"
    assert result.metadata["terminal_reason"] == "max_turns"
    assert result.metadata["turns"] == 1
    assert result.metadata["sdk"]["missions"][0]["mission_id"] == run.last_result["mission_id"]
    assert run.last_result["mission_status"] == "COMPLETED"
    assert run.last_result["domain"] == "are-v1"
    assert run.last_result["benchmark_success"] == "external_evaluation_pending"
    assert app.calls == [
        ({"path": "/original", "password": "synthetic", "nested": [1, None, True]}, "DEFAULT")
    ]
    assert provider.calls == run.meter.counters.calls == len(handoffs) == 6
    assert reports[-1].calls == 6 and run.meter.counters.peak_physical_slots <= 2
    assert set(provider.by_role) == {"planner", "worker", "critic"}
    wire = str(provider.requests)
    for secret in ("PRIVATE_APP_STATE", "PRIVATE_ORACLE", "PRIVATE_JUDGE_SCORE", "hidden_action"):
        assert secret not in wire
    for req in provider.requests:
        if role_of(req) in {"planner", "critic"}:
            assert not {"public__read", "poll_notifications"} & {tool.name for tool in req.tools}
    assert ("public__read", "succeeded", 1, 0) in effects(run)
    assert ("poll_notifications", "succeeded", 1, 0) in effects(run)
    assert agent.join(0) and run._closed


def test_environment_stop_during_provider_call_is_cooperative_and_metered(tmp_path):
    app, scenario, clock, system = environment()
    cancelled = threading.Event()

    class Provider(RoleScriptedProvider):
        async def invoke(self, request, *, cancel):
            if role_of(request) == "worker":
                self.requests.append(request)
                put(system, MessageType.ENVIRONMENT_STOP, "STOP", 1200)
                clock.add_offset(250)
                try:
                    await cancel.wait()
                finally:
                    cancelled.set()
                raise asyncio.CancelledError()
            return await super().invoke(request, cancel=cancel)

    provider = Provider(scripts())
    run = runner(tmp_path, provider)
    agent = SimpleHarnessAREAgent(run, poll_seconds=0.005)
    result = agent.run_scenario(scenario, system)
    assert result.output is None and result.metadata["terminal_reason"] == "environment_stop"
    assert cancelled.is_set() and agent.join(0)
    assert provider.calls == run.meter.counters.calls == 2
    assert run.meter.unknown_usage_calls == 1 and not app.calls
    assert json.loads((run.root / "turn-0001/episode.json").read_text())["state"] == "interrupted"


def test_stop_blocked_official_apptool_remains_unknown_and_join_reports_running(tmp_path):
    app, scenario, clock, system = environment()
    app.block = True
    provider = RoleScriptedProvider(scripts())
    run = runner(tmp_path, provider)
    agent = SimpleHarnessAREAgent(run, poll_seconds=0.005)

    async def exercise():
        pending = asyncio.create_task(agent.arun_scenario(scenario, system, join_timeout=0.02))
        try:
            assert await asyncio.to_thread(app.entered.wait, 5)
            put(system, MessageType.ENVIRONMENT_STOP, "STOP", 1200)
            clock.add_offset(250)
            await asyncio.sleep(0.08)
            assert not pending.done() and not agent.join(0)
            record = json.loads((run.root / "turn-0001/episode.json").read_text())
            assert record["state"] == "stopping"
            assert record["runtime_termination_boundary"] == RUNTIME_TERMINATION_BOUNDARY
            pending.cancel()
            with pytest.raises(AREWorkerStillRunning):
                await pending
            assert not agent.join(0)
        finally:
            app.release.set()
            assert await asyncio.to_thread(agent.join, 5)

    asyncio.run(exercise())
    assert provider.calls == 2 and len(app.calls) == 1
    assert effects(run) == [("public__read", "unknown", 1, 0)]
    audit = json.loads((run.root / "turn-0001/gateway.json").read_text())["calls"]
    assert audit[0]["outcome"] == "unknown"
    assert run._closed


@pytest.mark.parametrize("call_limit", [10, 7])
def test_two_turns_share_loop_asyncclient_and_cumulative_budget(tmp_path, call_limit):
    import httpx

    app, scenario, clock, system = environment(turns=2)
    loops, closed, handoffs = [], [], []

    class Provider(RoleScriptedProvider):
        client = None

        async def invoke(self, request, *, cancel):
            loops.append(asyncio.get_running_loop())
            if self.client is None:
                self.client = httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _: httpx.Response(200))
                )
            assert (await self.client.get("https://synthetic.invalid")).status_code == 200
            result = await super().invoke(request, cancel=cancel)
            if role_of(request) == "critic" and self.by_role["critic"] == 1:
                put(system, MessageType.USER_MESSAGE, "SECOND_TURN", 1400)
                clock.add_offset(500)
            return result

        async def close(self):
            assert asyncio.get_running_loop() is loops[0]
            await self.client.aclose()
            closed.append(True)

    first, second = scripts(), scripts()
    provider = Provider({key: first[key] + second[key] for key in first})
    run = runner(
        tmp_path,
        provider,
        context=context(calls=call_limit),
        close_provider=provider.close,
        before_handoff=handoffs.append,
    )
    agent = SimpleHarnessAREAgent(run, poll_seconds=0.005)
    if call_limit == 7:
        with pytest.raises(RuntimeError, match="ARE Mission did not complete"):
            agent.run_scenario(scenario, system)
        assert provider.calls == run.meter.counters.calls == len(handoffs) == 7
        assert run.meter.unknown_usage_calls == 0
        assert closed == [True] and provider.client.is_closed
        assert run.last_result["mission_status"] != "COMPLETED"
        return
    result = agent.run_scenario(scenario, system)
    assert result.output == "observed DEFAULT" and result.metadata["turns"] == 2
    assert len(set(loops)) == 1 and closed == [True] and provider.client.is_closed
    assert provider.calls == run.meter.counters.calls == len(handoffs) == 10
    assert len(app.calls) == 2
    assert json.loads((run.root / "turn-0001/episode.json").read_text())["counters"]["calls"] == 5
    assert json.loads((run.root / "turn-0002/episode.json").read_text())["counters"]["calls"] == 10
    run.close()
    assert closed == [True]
    with pytest.raises(FileExistsError):
        runner(tmp_path, RoleScriptedProvider({})).run_turn(
            notifications=(), state=None, tools=None, stop_event=threading.Event()
        )


def test_distinct_domain_and_existing_code_v3_identity():
    assert resolve_domain("are-v1") is ARE_PROFILE
    assert handler_for(ARE_PROFILE).name == "are"
    assert resolve_domain("code-v1").version == "3"
    for name, role in ROLES.items():
        assert template_for_domain(role, ARE_PROFILE, {}).prompt_version == f"{name}-are-v1"
        assert (
            template_for_domain(role, resolve_domain("agentdojo-v1"), {}).prompt_version
            == f"{name}-agentdojo-v1"
        )


@pytest.mark.parametrize("value", ["[]", "null", "{broken", '{"x":1,"x":2}', '{"x":NaN}'])
def test_transport_rejects_ambiguous_arguments(value):
    with pytest.raises(AREArgumentsError):
        _decode({"arguments_json": value})


@pytest.mark.parametrize("domain", ["code-v1", "doc-research-v1", "appworld-v1", "agentdojo-v1"])
def test_foreign_domain_cannot_use_are_tools(tmp_path, domain):
    from agent_orchestrator.contracts import ContractError
    from agent_orchestrator.governance.policies import DeploymentPolicy
    from agent_orchestrator.orchestrator.commit_service import MissionSpec
    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    provider = RoleScriptedProvider({})
    cfg = OrchestratorConfig(
        evidence_root=tmp_path / "orch",
        are_invoke=lambda *args: {},
        are_tool_schemas={"read": {"type": "object", "properties": {}}},
        # Deploy both environments so an AgentDojo Mission reaches the ARE
        # isolation check, not an unrelated missing-environment check.
        agentdojo_invoke=lambda *args: {},
        deployment_policy=DeploymentPolicy(
            allowed_tools=("read",), are_tools=("read",), local_code_execution=False
        ),
    )

    async def exercise():
        async with Orchestrator(cfg, provider) as orch:
            with pytest.raises(ContractError):
                await orch.submit_mission(
                    MissionSpec(
                        goal="read",
                        success_criteria=("file:REPORT.md",),
                        tenant_id="test",
                        idempotency_key="test",
                        allowed_tools=("read",),
                        domain=domain,
                    )
                )

    asyncio.run(exercise())
    assert provider.calls == 0


def test_are_capability_snapshot_is_distinct_and_defaults_do_not_add_identity(tmp_path):
    from agent_orchestrator.governance.policies import DeploymentPolicy, policy_snapshot
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    default = policy_snapshot(OrchestratorConfig(evidence_root=tmp_path))
    assert "are_invoke" not in default["config"]
    assert "are_tool_schemas" not in default["config"]
    assert "are_tools" not in DeploymentPolicy().to_json()
    schemas = {"read": {"type": "object", "properties": {}}}
    cfg = OrchestratorConfig(
        evidence_root=tmp_path,
        are_invoke=lambda *args: {},
        are_tool_schemas=schemas,
        deployment_policy=DeploymentPolicy(
            allowed_tools=("read",), are_tools=("read",), local_code_execution=False
        ),
    )
    snapshot = policy_snapshot(cfg)
    assert snapshot["config"]["are_tool_schemas"] == schemas
    assert "agentdojo_invoke" not in snapshot["config"]
    for role in resolve_domain("code-v1").role_templates:
        template = template_for_domain(ROLES[role], resolve_domain("code-v1"), {})
        assert template.prompt_version == f"{role}-code-observation-v3"


@pytest.mark.parametrize("slots,capacity,window", [(3, None, None), (2, 1, None), (2, None, False)])
def test_physical_or_weighted_capacity_or_window_refuses_before_provider(
    tmp_path,
    slots,
    capacity,
    window,
):
    from agent_orchestrator.evaluation.metered_provider import RunWindowDenied

    app, scenario, clock, system = environment()
    provider = RoleScriptedProvider(scripts())
    handoffs = []

    def handoff(wait):
        if window is False:
            raise RunWindowDenied("synthetic closed window")
        handoffs.append(wait)

    if slots == 3:
        with pytest.raises(ValueError, match="physical capacity"):
            runner(tmp_path, provider, context=context(slots=slots))
        assert provider.calls == 0
        return
    run = runner(tmp_path, provider, max_inflight_tokens=capacity, before_handoff=handoff)
    with pytest.raises(RuntimeError, match="ARE Mission did not complete"):
        SimpleHarnessAREAgent(run).run_scenario(scenario, system)
    assert not provider.calls and not app.calls and not handoffs
    assert run.meter.counters.calls == 0 and run.meter.unknown_usage_calls == 0


def test_wall_deadline_is_not_extended_by_simulated_time(tmp_path, monkeypatch):
    import time

    app, scenario, clock, system = environment()
    entered, stopped = threading.Event(), threading.Event()

    # Moving simulated time backward must not extend the wall-time allowance.
    def no_pause():
        pytest.fail("runner must never pause the ARE clock")

    monkeypatch.setattr(clock, "pause", no_pause)

    class Slow(RoleScriptedProvider):
        async def invoke(self, request, *, cancel):
            self.requests.append(request)
            entered.set()
            clock.add_offset(-100)
            try:
                await cancel.wait()
            finally:
                stopped.set()
            raise asyncio.CancelledError()

    provider = Slow(scripts())
    run = runner(tmp_path, provider, context=context(seconds=0.25))
    started = time.monotonic()
    result = SimpleHarnessAREAgent(run).run_scenario(scenario, system)
    assert entered.is_set() and stopped.is_set()
    assert result.output is None and time.monotonic() - started < 3
    assert run.meter.counters.calls == 1 and run.meter.unknown_usage_calls == 1
    assert not app.calls
