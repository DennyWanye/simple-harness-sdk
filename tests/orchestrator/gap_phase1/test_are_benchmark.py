"""Pinned official ScenarioRunner/MultiScenarioRunner; no LLM, network or datasets."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import pytest

pytest.importorskip("are")

from are.simulation.agents.agent_builder import AgentBuilder
from are.simulation.agents.agent_config_builder import AgentConfigBuilder
from are.simulation.agents.agent_log import FinalAnswerLog, StopLog
from are.simulation.apps.agent_user_interface import AgentUserInterface
from are.simulation.scenario_runner import ScenarioRunner
from are.simulation.scenarios.config import MultiScenarioRunnerConfig, ScenarioRunnerConfig
from are.simulation.scenarios.scenario import Scenario
from are.simulation.types import AgentValidationEvent, EventRegisterer, EventType, OracleEvent

from agent_orchestrator.evaluation.are_benchmark import (
    AGENT_NAME,
    AREBenchmark,
    AREOrchestratorAgentBuilder,
    AREOrchestratorAgentConfig,
    AREOrchestratorConfigBuilder,
    BenchmarkGateError,
    Gaia2Protocol,
    JudgeTarget,
    read_official_trace,
)
from agent_orchestrator.evaluation.are_orchestrator import AREOrchestratorRunner
from agent_orchestrator.evaluation.experiment import (
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
    RunContext,
)
from agent_orchestrator.evaluation.metered_provider import RunWindowDenied
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
    role_of,
)
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer
from simple_harness.providers import ProviderTarget


class HardScenario(Scenario):
    start_time: float = 1000.0
    duration: float = 10.0
    nb_turns: int | None = 1

    def init_and_populate_apps(self):
        self.apps = [AgentUserInterface()]

    def build_events_flow(self):
        aui = self.apps[0]
        with EventRegisterer.capture_mode():
            request = aui.send_message_to_agent(content="Say BLUE.").with_type(EventType.USER)
            answer = aui.send_message_to_user(content="BLUE")
        oracle = OracleEvent.from_event(answer).with_id("PRIVATE_ORACLE_CANARY")
        oracle.depends_on(request)
        # Official native hard validation. The ordinary Scenario.validate invokes
        # final_validation_checks; a report alone cannot fulfill this milestone.
        check = AgentValidationEvent(
            milestones=[
                lambda env, event: (
                    event.function_name() == "send_message_to_user"
                    and event.action.args.get("content") == "BLUE"
                )
            ]
        ).with_id("PRIVATE_HARD_CHECK")
        # Native AgentValidationEvent registers validators but does not dispatch
        # successors; schedule the user request independently after registration.
        request.depends_on(delay_seconds=0.01)
        self.events = [check, request, oracle]
        self.comment = "PRIVATE_SCORE_CANARY"


def case(name="synthetic-hard"):
    scenario = HardScenario(scenario_id=name)
    scenario.initialize()
    return scenario


class Counter:
    fingerprint = "scripted-are-benchmark-counter-v1"
    bound_protocol = "scripted-fixed-v1"
    requires_prior_output_reserve = False

    def estimate_input_tokens(self, request):
        return 12000


def scripted(answer="BLUE"):
    tools = [
        "AgentUserInterface__send_message_to_user",
        "workspace_write_file",
        "workspace_read_file",
        "workspace_list",
    ]
    return {
        "planner": [
            graph_proposal_step(
                [
                    {
                        "key": "answer",
                        "goal": "Send BLUE to the user and write REPORT.md",
                        "rationale": "one public request",
                        "dependencies": [],
                        "success_criteria": ["file:REPORT.md"],
                        "verification_policy": ["format_check", "rule_check", "critic_review"],
                        "allowed_tools": tools,
                        "budget": {"max_tokens": 400000, "max_attempts": 1},
                        "priority": 1.0,
                        "outputs": ["REPORT.md"],
                    }
                ]
            )
        ],
        "worker": [
            (
                "AgentUserInterface__send_message_to_user",
                {
                    "arguments_json": json.dumps({"content": answer}),
                },
            ),
            ("workspace_write_file", {"path": "REPORT.md", "content": f"Sent {answer}"}),
            envelope_step(
                summary=f"Sent {answer}",
                artifacts=["REPORT.md"],
                claims=[f"Sent {answer} to the user"],
            ),
        ],
        "critic": [critic_step(verdict="PASS", criteria_met=True)],
    }


def factory(tmp_path, providers, mode="good", close_provider=None):
    def create(config):
        assert isinstance(config, AREOrchestratorAgentConfig)
        assert "PRIVATE" not in str(config.get_model_dump())
        assert config.base_agent_config.llm_engine_config.model_name == "agent-model"

        class SlowProvider(RoleScriptedProvider):
            async def invoke(self, request, *, cancel):
                if mode == "stop" and role_of(request) == "worker":
                    self.requests.append(request)
                    await cancel.wait()
                    raise asyncio.CancelledError()
                return await super().invoke(request, cancel=cancel)

        provider = SlowProvider(scripted("RED" if mode == "wrong" else "BLUE"))
        provider.target = ProviderTarget(
            "fixture", "agent-model", "fixture", "https://synthetic.invalid", "v1"
        )
        providers.append(provider)
        manifest = ExperimentManifest(
            "scripted-are-benchmark",
            "fixture",
            "agent-model",
            ExperimentBudget(900000, 100000, 1000000, 30, 15),
            (str(len(providers)),),
            1,
            0,
            2,
            tuple(ArmSpec(arm, "sdk-are-v1") for arm in ("S", "R", "D", "F")),
        )

        def handoff(wait):
            if mode == "deny":
                raise RunWindowDenied("synthetic local admission denial")

        return AREOrchestratorRunner(
            provider=provider,
            context=RunContext(manifest, manifest.runs()[2], lambda _: None),
            token_counter=Counter(),
            tokenizer=UpperBoundTokenizer(),
            context_policy=ContextPolicy(),
            evidence_root=tmp_path / f"sdk-{len(providers)}",
            default_output_tokens=1024,
            maximum_output_tokens=4096,
            close_provider=close_provider,
            before_handoff=handoff,
            dynamic_graph=False,
        )

    return create


def config(tmp_path, *, multi=False):
    values = dict(
        model="agent-model",
        model_provider="fixture",
        endpoint="https://synthetic.invalid",
        agent=AGENT_NAME,
        oracle=False,
        export=True,
        output_dir=str(tmp_path / "traces"),
        trace_dump_format="both",
        max_turns=1,
    )
    if multi:
        return MultiScenarioRunnerConfig(
            **values, executor_type="sequential", max_concurrent_scenarios=1, enable_caching=False
        )
    return ScenarioRunnerConfig(**values)


@pytest.mark.parametrize(
    "mode,success", [("good", True), ("wrong", False), ("deny", False), ("stop", False)]
)
def test_official_scenario_runner_hard_validation_and_trace_roundtrip(
    tmp_path, monkeypatch, mode, success
):
    def forbidden(*args, **kwargs):
        pytest.fail("official default agent/model engine must never be constructed")

    monkeypatch.setattr(AgentBuilder, "build", forbidden)
    monkeypatch.setattr(AgentConfigBuilder, "build", forbidden)
    from are.simulation.agents.llm.llm_engine_builder import LLMEngineBuilder

    monkeypatch.setattr(LLMEngineBuilder, "create_engine", forbidden)
    providers = []
    benchmark = AREBenchmark(factory(tmp_path, providers, mode), outer_deadline_seconds=30)
    assert type(benchmark.scenario_runner) is ScenarioRunner
    scenario = case()
    if mode == "stop":
        scenario.duration = 2.0
    result = benchmark.run_hard(config(tmp_path), scenario)
    assert result.success is success, result
    agent = benchmark.agent_builder.agents[0]
    assert not agent.environment.thread.is_alive()
    assert agent.sdk_runner._closed and agent.join(0)
    assert result.export_path
    trace = Path(result.export_path)
    assert trace.parent.name == "hf"
    assert (trace.parent.parent / "lite" / trace.name).is_file()
    imported, events, logs = read_official_trace(trace)
    assert imported.scenario_id == scenario.scenario_id
    assert "PRIVATE_ORACLE_CANARY" in trace.read_text()  # stays in HOST trace
    assert "PRIVATE" not in str(providers[0].requests)
    # Pinned upstream HF export omits world_logs; native lite export and SDK
    # receipts retain them. Do not fabricate logs in the official HF trace.
    assert logs == []
    logs = agent.environment.get_world_logs()
    stop = next(log for log in logs if isinstance(log, StopLog) and log.agent_id == AGENT_NAME)
    assert stop.get_content_for_llm() is None
    metadata = json.loads(stop.content)["sdk"]
    assert metadata["domain"] == "are-v1"
    if mode in {"good", "wrong"}:
        assert providers[0].calls == metadata["counters"]["calls"] == 5
        assert metadata["missions"][0]["mission_status"] == "COMPLETED"
        assert any(isinstance(log, FinalAnswerLog) for log in logs)
        sent = [event for event in events if event.function_name() == "send_message_to_user"]
        assert len(sent) == 1
        assert sent[0].action.args["content"] == ("RED" if mode == "wrong" else "BLUE")
        assert all(event.event_id != "PRIVATE_ORACLE_CANARY" for event in events)
    elif mode == "deny":
        assert not providers[0].calls and metadata["unknown_usage_calls"] == 0
        assert metadata["admission_denials"]
        assert all(
            item["physical_calls"] == item["total_tokens"] == 0
            for item in metadata["admission_denials"]
        )
        episode = json.loads((agent.sdk_runner.root / "turn-0001/episode.json").read_text())
        assert episode["admission_denials"] == metadata["admission_denials"]
    else:
        assert metadata["unknown_usage_calls"] == 1 and not metadata["admission_denials"]
        assert json.loads(stop.content)["terminal_reason"] == "environment_stop"


def test_official_multi_runner_reuses_injected_builders_without_global_signals(
    tmp_path, monkeypatch
):
    providers = []
    benchmark = AREBenchmark(factory(tmp_path, providers), outer_deadline_seconds=30)
    monkeypatch.setattr(
        signal, "signal", lambda *args: pytest.fail("global signal handler installed")
    )
    monkeypatch.setattr(signal, "alarm", lambda *args: pytest.fail("global alarm installed"))
    result = benchmark.run_hard_many(config(tmp_path, multi=True), [case("first"), case("second")])
    assert result.successful_count == 2 and len(result.scenario_results) == 2
    assert len(providers) == 2 and [p.calls for p in providers] == [5, 5]
    assert all(not agent.environment.thread.is_alive() for agent in benchmark.agent_builder.agents)
    assert (tmp_path / "traces/output.jsonl").is_file()
    for value in result.scenario_results.values():
        assert value.success is True
        assert read_official_trace(Path(value.export_path))[1]


def test_builder_names_configuration_and_no_fallback():
    builder = AREOrchestratorAgentBuilder(lambda _: pytest.fail("factory called"))
    assert builder.list_agents() == [AGENT_NAME]
    with pytest.raises(BenchmarkGateError):
        AREOrchestratorConfigBuilder().build("default")
    cfg = AREOrchestratorConfigBuilder().build(AGENT_NAME)
    assert cfg.get_agent_name() == AGENT_NAME
    assert cfg.validate_model(cfg.get_model_dump()) == cfg
    assert cfg.get_model_json_schema()["properties"]["agent_name"]["const"] == AGENT_NAME
    with pytest.raises(BenchmarkGateError):
        builder.build(AgentConfigBuilder().build("default"))
    with pytest.raises(BenchmarkGateError):
        builder.build(cfg, mock_responses=[])


@pytest.mark.parametrize(
    "override,gate",
    [
        ({"agent": "default"}, "named"),
        ({"oracle": True}, "oracle"),
        ({"a2a_app_prop": 1}, "A2A"),
        ({"trace_dump_format": "hf"}, "trace"),
        ({"judge_engine_config": {"model_name": "judge"}}, "judge"),
        ({"simulated_generation_time_mode": "fixed"}, "live ARE"),
    ],
)
def test_hard_dependencies_rejected_before_environment_or_provider(tmp_path, override, gate):
    benchmark = AREBenchmark(lambda _: pytest.fail("factory called"), outer_deadline_seconds=30)
    cfg = ScenarioRunnerConfig(**{**config(tmp_path).model_dump(), **override})
    with pytest.raises(BenchmarkGateError, match=gate):
        benchmark.run_hard(cfg, case())
    assert not benchmark.agent_builder.environments


def test_gaia2_three_phases_are_frozen_named_and_explicitly_gated():
    values = dict(
        model="agent",
        provider="provider",
        endpoint="https://agent.invalid",
        dataset="synthetic/gaia2",
        dataset_revision="frozen-dataset-revision",
        split="test",
    )
    protocol = Gaia2Protocol.freeze(**values)
    frozen = protocol.to_json()
    assert frozen["agent"] == AGENT_NAME and frozen["oracle"] is False
    assert frozen["hf_upload"] is None and not frozen["formal_gaia2_validated"]
    phases = frozen["phases"]
    assert [p["phase_name"] for p in phases] == ["standard", "agent2agent", "noise"]
    assert phases[0]["configs"] == ["ambiguity", "adaptability", "execution", "search", "time"]
    assert [p["num_runs"] for p in phases] == [3, 3, 3]
    assert phases[1]["configs"] == phases[2]["configs"] == ["mini"]
    assert phases[1]["a2a_app_prop"] == 1.0
    assert phases[2]["tool_augmentation_config"]["tool_failure_probability"] == 0.1
    assert phases[2]["env_events_config"]["num_env_events_per_minute"] == 10
    assert phases[2]["env_events_config"]["env_events_seed"] == 0
    assert protocol.fingerprint == Gaia2Protocol.freeze(**values).fingerprint
    frozen["phases"][0]["num_runs"] = 1
    assert protocol.to_json()["phases"][0]["num_runs"] == 3
    with pytest.raises(BenchmarkGateError, match="missing independent judge"):
        protocol.require_executable("standard")
    with pytest.raises(BenchmarkGateError, match="missing A2A"):
        protocol.require_executable("agent2agent")
    judge = JudgeTarget("separate-judge", "explicit-provider", "https://judge.invalid")
    configured = Gaia2Protocol.freeze(**values, judge=judge, a2a=judge)
    with pytest.raises(BenchmarkGateError, match="softcheck/judge engine integration unavailable"):
        configured.require_executable("noise")
    assert configured.fingerprint != protocol.fingerprint
    with pytest.raises(BenchmarkGateError, match="separate"):
        Gaia2Protocol.freeze(
            **values, judge=JudgeTarget("agent", "provider", "https://agent.invalid")
        )


def test_official_max_turns_zero_closes_owned_provider_without_a_mission(tmp_path):
    providers, closed = [], []

    async def close():
        closed.append(asyncio.get_running_loop())

    benchmark = AREBenchmark(
        factory(tmp_path, providers, close_provider=close), outer_deadline_seconds=30
    )
    cfg = config(tmp_path)
    cfg.max_turns = 0
    result = benchmark.run_hard(cfg, case())
    assert result.success is False  # cannot pass hard validation by skipping the Mission
    assert len(providers) == 1 and providers[0].calls == 0
    assert len(closed) == 1
    agent = benchmark.agent_builder.agents[0]
    agent.sdk_runner.close()
    assert len(closed) == 1 and not agent.environment.thread.is_alive()


@pytest.mark.parametrize(
    "override",
    [
        {"timeout_seconds": 1},
        {"executor_type": "process"},
        {"max_concurrent_scenarios": 2},
        {"enable_caching": True},
    ],
)
def test_multi_scope_refuses_alarm_parallel_and_cache_before_execution(tmp_path, override):
    benchmark = AREBenchmark(lambda _: pytest.fail("factory called"), outer_deadline_seconds=30)
    cfg = MultiScenarioRunnerConfig(**{**config(tmp_path, multi=True).model_dump(), **override})
    with pytest.raises(BenchmarkGateError, match="parent-only timeout"):
        benchmark.run_hard_many(cfg, [case()])
    assert not benchmark.agent_builder.environments
