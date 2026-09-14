# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Named SDK agent through pinned ARE's public runner injection points.

Only local, host-authored hard-validation Scenarios execute in this slice.
Gaia2 phase settings can be frozen for review, but the full protocol is gated:
judge/soft-check and A2A model integrations are deliberately not substituted.
Pinned HF file export omits world_logs; SDK usage lives in independent receipts.
The caller must run inside its owned process deadline (Host's
scripts/run_resource_bounded.py); thread joins here are not hard termination.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from are.simulation.agents.agent_builder import AbstractAgentBuilder
from are.simulation.agents.agent_config_builder import AbstractAgentConfigBuilder
from are.simulation.agents.agent_log import FinalAnswerLog, StopLog
from are.simulation.agents.are_simulation_agent_config import (
    ARESimulationBaseAgentConfig,
    MainAgentConfig,
    RunnableARESimulationAgentConfig,
)
from are.simulation.data_handler.importer import JsonScenarioImporter
from are.simulation.multi_scenario_runner import MultiScenarioRunner
from are.simulation.scenario_runner import ScenarioRunner
from are.simulation.scenarios.config import MultiScenarioRunnerConfig, ScenarioRunnerConfig
from are.simulation.scenarios.scenario import Scenario
from are.simulation.scenarios.scenario_imported_from_json.scenario import ScenarioImportedFromJson
from are.simulation.scenarios.utils.scenario_expander import EnvEventsConfig
from are.simulation.types import CapabilityTag, ToolAugmentationConfig
from pydantic import Field

from .are_bridge import SimpleHarnessAREAgent
from .are_orchestrator import AREOrchestratorRunner

AGENT_NAME: Literal["simple_harness_orchestrator"] = "simple_harness_orchestrator"
ARE_COMMIT = "87ebd38f31aafae0f11e14f55617903196236cfb"
OUTER_DEADLINE_REQUIREMENT = "Host scripts/run_resource_bounded.py owns process hard deadline"


class BenchmarkGateError(ValueError):
    """A required execution dependency is absent; no fallback is allowed."""


class AREOrchestratorAgentConfig(MainAgentConfig, RunnableARESimulationAgentConfig):
    agent_name: Literal["simple_harness_orchestrator"] = AGENT_NAME
    base_agent_config: ARESimulationBaseAgentConfig = Field(
        default_factory=ARESimulationBaseAgentConfig
    )

    def get_base_agent_config(self) -> ARESimulationBaseAgentConfig:
        return self.base_agent_config

    def validate_model(self, agent_config_dict: dict[str, Any]) -> AREOrchestratorAgentConfig:
        return type(self).model_validate(agent_config_dict)


class AREOrchestratorConfigBuilder(AbstractAgentConfigBuilder):
    def build(self, agent_name: str) -> AREOrchestratorAgentConfig:
        if agent_name != AGENT_NAME:
            raise BenchmarkGateError(f"unsupported agent: {agent_name}")
        return AREOrchestratorAgentConfig()


class _LoggedAgent(SimpleHarnessAREAgent):
    def __init__(self, runner: AREOrchestratorRunner, env: Any, max_turns: int | None):
        super().__init__(runner, max_turns=max_turns)
        self.sdk_runner = runner
        self.environment = env  # Host side only; never passed to the SDK runner.

    def run_scenario(self, scenario, notification_system, initial_agent_logs=None):
        result = None
        try:
            result = super().run_scenario(scenario, notification_system, initial_agent_logs)
            metadata = self.sdk_runner.execution_metadata()
            if (
                not metadata["missions"]
                or metadata["unknown_usage_calls"]
                or any(m["mission_status"] != "COMPLETED" for m in metadata["missions"])
            ):
                raise BenchmarkGateError(
                    "SDK Mission incomplete/unknown; official success is unavailable"
                )
            if result.output is not None:
                self.environment.append_to_world_logs(
                    FinalAnswerLog(
                        timestamp=self.environment.time_manager.time(),
                        agent_id=AGENT_NAME,
                        content=result.output,
                    )
                )
            return result
        finally:
            # Native StopLog is round-trippable and explicitly excluded from LLM
            # context by ARE. Do not export hidden Scenario/judge fields here.
            self.environment.append_to_world_logs(
                StopLog(
                    timestamp=self.environment.time_manager.time(),
                    agent_id=AGENT_NAME,
                    content=json.dumps(
                        {
                            "sdk": self.sdk_runner.execution_metadata(),
                            "terminal_reason": (result.metadata or {}).get("terminal_reason")
                            if result is not None
                            else "runner_error",
                            "outer_deadline_requirement": OUTER_DEADLINE_REQUIREMENT,
                        },
                        ensure_ascii=False,
                    ),
                )
            )


class AREOrchestratorAgentBuilder(AbstractAgentBuilder):
    def __init__(
        self, runner_factory: Callable[[AREOrchestratorAgentConfig], AREOrchestratorRunner]
    ):
        self.runner_factory = runner_factory
        self.agents: list[_LoggedAgent] = []
        self.environments: list[Any] = []

    def list_agents(self) -> list[str]:
        return [AGENT_NAME]

    def build(self, agent_config, env=None, mock_responses=None):
        if env is not None and not any(existing is env for existing in self.environments):
            self.environments.append(env)
        if not isinstance(agent_config, AREOrchestratorAgentConfig):
            raise BenchmarkGateError("explicit AREOrchestratorAgentConfig required")
        if agent_config.get_agent_name() != AGENT_NAME or mock_responses is not None:
            raise BenchmarkGateError("no default/mock-response agent fallback")
        if env is None:
            raise BenchmarkGateError("official Environment required")
        # The factory receives only the public model configuration, never env,
        # Scenario, oracle, validation callbacks or score data.
        runner = self.runner_factory(agent_config.model_copy(deep=True))
        if not isinstance(runner, AREOrchestratorRunner):
            raise BenchmarkGateError("factory must produce a real AREOrchestratorRunner")
        target = agent_config.base_agent_config.llm_engine_config
        if (runner.context.manifest.model, runner.context.manifest.provider) != (
            target.model_name,
            target.provider,
        ) or runner.provider.target.endpoint_identity != target.endpoint:
            runner.close()
            raise BenchmarkGateError("official config differs from frozen SDK provider identity")
        if any(
            agent.sdk_runner is runner or agent.sdk_runner.root == runner.root
            for agent in self.agents
        ):
            runner.close()
            raise BenchmarkGateError("each Scenario must own a fresh SDK runner/evidence root")
        agent = _LoggedAgent(runner, env, agent_config.max_turns)
        self.agents.append(agent)
        return agent


class _OwnedProcessMultiScenarioRunner(MultiScenarioRunner):
    def _setup_signal_handlers(self):
        # Parent process owns signals. All official scheduling/export code is inherited.
        pass


class AREBenchmark:
    def __init__(self, runner_factory, *, outer_deadline_seconds: float):
        if (
            isinstance(outer_deadline_seconds, bool)
            or not math.isfinite(outer_deadline_seconds)
            or outer_deadline_seconds <= 0
        ):
            raise BenchmarkGateError("finite positive parent-owned process deadline required")
        self.outer_deadline_seconds = outer_deadline_seconds  # Requirement, not a kill guarantee.
        self.agent_builder = AREOrchestratorAgentBuilder(runner_factory)
        self.config_builder = AREOrchestratorConfigBuilder()
        self.scenario_runner = ScenarioRunner(
            agent_builder=self.agent_builder,
            agent_config_builder=self.config_builder,
        )

    @staticmethod
    def _check_hard(config: ScenarioRunnerConfig, scenarios: list[Scenario]) -> None:
        if config.agent != AGENT_NAME or config.oracle or config.judge_only:
            raise BenchmarkGateError("named agent execution with oracle=False is required")
        if config.a2a_app_prop:
            raise BenchmarkGateError("A2A model/provider integration is not available")
        if config.judge_engine_config is not None:
            raise BenchmarkGateError("softcheck/judge engine integration is not available")
        if config.simulated_generation_time_mode != "measured":
            raise BenchmarkGateError("only live ARE time without model pause is supported")
        if not config.export or not config.output_dir or config.trace_dump_format != "both":
            raise BenchmarkGateError(
                "explicit output_dir and official both-format trace export required"
            )
        if config.tool_augmentation_config is not None or config.env_events_config is not None:
            raise BenchmarkGateError("noise phase preprocessing is not part of hard-only execution")
        if not scenarios:
            raise BenchmarkGateError("at least one hard-validation Scenario is required")
        for scenario in scenarios:
            if not isinstance(scenario, Scenario) or isinstance(scenario, ScenarioImportedFromJson):
                raise BenchmarkGateError(
                    "local host-authored hard Scenarios only; benchmark judge required"
                )
            if not scenario._initialized:
                raise BenchmarkGateError("initialize the official Scenario before execution")
            if scenario.has_a2a_augmentation or getattr(scenario, "judge", None) is not None:
                raise BenchmarkGateError("attached A2A/softcheck judge dependency is unsupported")

    def _join_environments(self, first: int) -> None:
        # Official ScenarioRunner.stop() does not join its environment thread.
        # Never return success while that owned environment can still mutate.
        for env in self.agent_builder.environments[first:]:
            env.stop()
            env.join()

    def run_hard(self, config: ScenarioRunnerConfig, scenario: Scenario):
        self._check_hard(config, [scenario])
        first = len(self.agent_builder.environments)
        try:
            return self.scenario_runner.run(config, scenario)
        finally:
            self._join_environments(first)

    def run_hard_many(self, config: MultiScenarioRunnerConfig, scenarios: list[Scenario]):
        self._check_hard(config, scenarios)
        if (
            config.executor_type != "sequential"
            or config.max_concurrent_scenarios != 1
            or config.enable_caching
            or config.timeout_seconds is not None
        ):
            raise BenchmarkGateError(
                "this slice requires sequential single-scenario slots, no cache, "
                "and parent-only timeout"
            )
        first = len(self.agent_builder.environments)
        try:
            return _OwnedProcessMultiScenarioRunner(
                agent_builder=self.agent_builder,
                agent_config_builder=self.config_builder,
            ).run(config, scenarios)
        finally:
            self._join_environments(first)


def read_official_trace(path: Path):
    """Host-only trace re-read; the returned oracle/events must never enter a runner."""
    return JsonScenarioImporter().import_from_json(Path(path).read_bytes())


@dataclass(frozen=True)
class JudgeTarget:
    model: str
    provider: str
    endpoint: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in asdict(self).values()):
            raise BenchmarkGateError(
                "judge requires explicit model/provider/endpoint; no agent fallback"
            )


@dataclass(frozen=True)
class Gaia2Protocol:
    """Immutable review configuration, not a claim that Gaia2 is runnable/validated.

    The JSON freezes all three official phase settings and three repetitions.
    Execution deliberately refuses until independent judge/softcheck and A2A
    dependencies are implemented; supplying a target string alone is not enough.
    """

    configuration_json: str

    @classmethod
    def freeze(
        cls,
        *,
        model: str,
        provider: str,
        endpoint: str,
        dataset: str,
        dataset_revision: str,
        split: str,
        judge: JudgeTarget | None = None,
        a2a: JudgeTarget | None = None,
    ) -> Gaia2Protocol:
        if not all(
            isinstance(v, str) and v.strip()
            for v in (model, provider, endpoint, dataset, dataset_revision, split)
        ):
            raise BenchmarkGateError("explicit agent target, dataset revision and split required")
        if judge is not None and (judge.model, judge.provider, judge.endpoint) == (
            model,
            provider,
            endpoint,
        ):
            raise BenchmarkGateError("judge must be separate from the agent target")
        phases = [
            {
                "phase_name": "standard",
                "configs": [tag.value.lower() for tag in CapabilityTag.gaia2_capabilities()],
                "a2a_app_prop": 0.0,
                "tool_augmentation_config": None,
                "env_events_config": None,
            },
            {
                "phase_name": "agent2agent",
                "configs": ["mini"],
                "a2a_app_prop": 1.0,
                "tool_augmentation_config": None,
                "env_events_config": None,
            },
            {
                "phase_name": "noise",
                "configs": ["mini"],
                "a2a_app_prop": 0.0,
                "tool_augmentation_config": asdict(ToolAugmentationConfig()),
                "env_events_config": asdict(
                    EnvEventsConfig(
                        num_env_events_per_minute=10,
                        env_events_seed=0,
                    )
                ),
            },
        ]
        for phase in phases:
            phase["num_runs"] = 3
        return cls(
            json.dumps(
                {
                    "schema": "simple-harness-are-gaia2-protocol-v1",
                    "are_commit": ARE_COMMIT,
                    "agent": AGENT_NAME,
                    "model": model,
                    "model_provider": provider,
                    "endpoint": endpoint,
                    "dataset": dataset,
                    "dataset_revision": dataset_revision,
                    "split": split,
                    "oracle": False,
                    "judge": asdict(judge) if judge else None,
                    "a2a": asdict(a2a) if a2a else None,
                    "a2a_app_agent": "default_app_agent",
                    "phases": phases,
                    "trace_dump_format": "both",
                    "hf_upload": None,
                    "runner": "official MultiScenarioRunner with injected named builders",
                    "simulated_generation_time_mode": "measured",
                    "clock_policy": "live_are_no_model_pause",
                    "outer_deadline_requirement": OUTER_DEADLINE_REQUIREMENT,
                    "formal_gaia2_validated": False,
                    "executor_type": "sequential",
                    "max_concurrent_scenarios": 1,
                    "timeout_seconds": None,
                    "enable_caching": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @property
    def fingerprint(self) -> str:
        return sha256(self.configuration_json.encode()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return json.loads(self.configuration_json)

    def require_executable(self, phase_name: str) -> None:
        config = self.to_json()
        if phase_name not in {p["phase_name"] for p in config["phases"]}:
            raise BenchmarkGateError("unknown Gaia2 phase")
        gates = []
        if config["judge"] is None:
            gates.append("missing independent judge model/provider/endpoint")
        gates.append("softcheck/judge engine integration unavailable")
        if phase_name == "agent2agent":
            if config["a2a"] is None:
                gates.append("missing A2A model/provider/endpoint")
            gates.append(
                "A2A integration unavailable (upstream Multi drops provider/endpoint fields)"
            )
        gates.append("full three-run dataset phase launcher not validated")
        raise BenchmarkGateError("; ".join(gates))
