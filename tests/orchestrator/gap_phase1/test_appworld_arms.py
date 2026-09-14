"""Actual BaseAgent identity, tool flow and host-only R selection controls."""

import json
from types import SimpleNamespace

import pytest

from agent_orchestrator.evaluation.appworld import AppWorldConfig, AppWorldEpisode
from agent_orchestrator.evaluation.appworld_arms import ArmRuntime, execute_arm
from agent_orchestrator.evaluation.experiment import ExperimentBudget
from simple_harness import Message, MessageRole
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import UpperBoundTokenizer
from simple_harness.contracts import CallId
from simple_harness.providers import ProviderResponse, ProviderToolCall, ProviderUsage


class World:
    task = SimpleNamespace(instruction="Make the world value correct.")

    def __init__(self):
        self.value = 0
        self.saved = {}
        self.evaluations = 0

    def execute(self, code):
        self.value = int(code)
        return str(self.value)

    def save(self):
        pass

    def close(self):
        pass

    def checkpoint(self, name):
        self.saved[name] = self.value

    def restore(self, name):
        self.value = self.saved[name]

    def evaluate(self):
        self.evaluations += 1
        return SimpleNamespace(to_dict=lambda: {"success": self.value == 1})


class Provider:
    def __init__(self, arm):
        self.steps = [("appworld_execute", {"code": "1"}), "first done"]
        if arm == "R":
            self.steps += [
                ("appworld_execute", {"code": "2"}),
                "second done",
                '{"selected_candidate":1}',
            ]
        self.requests = []

    async def invoke(self, request, *, cancel):
        self.requests.append(request)
        step = self.steps.pop(0)
        tools = (
            ()
            if isinstance(step, str)
            else (ProviderToolCall(CallId(str(len(self.requests))), step[0], step[1]),)
        )
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, step if isinstance(step, str) else ""),
            tool_calls=tools,
            model="agent-model",
            usage=ProviderUsage(100, 10, 110),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["S", "R"])
async def test_base_agent_arms_execute_real_gateway_and_r_reuses_same_identity(tmp_path, arm):
    world = World()
    provider = Provider(arm)
    with AppWorldEpisode(
        AppWorldConfig("task", "experiment"), world_factory=lambda **_: world
    ) as episode:
        result = await execute_arm(
            arm,
            episode,
            ArmRuntime(
                provider,
                "agent-model",
                UpperBoundTokenizer(),
                ContextPolicy(),
                ExperimentBudget(100000, 100000, 200000, 30, 20),
                default_output_tokens=1024,
                maximum_output_tokens=8192,
            ),
            tmp_path / arm,
        )
        assert world.evaluations == 0 and world.value == 1
        assert len(result["agent_ids"]) == 1
        assert result["turns"] == (1 if arm == "S" else 3)
        assert len(provider.requests) == (2 if arm == "S" else 5)
        if arm == "R":
            assert result["selected_candidate"] == 1
            assert any("first done" in str(m.content) for m in provider.requests[2].messages)
            assert (
                len(
                    {r["agent_id"] for r in json.loads((tmp_path / arm / "turns.json").read_text())}
                )
                == 1
            )
    assert world.evaluations == 1


@pytest.mark.asyncio
async def test_local_budget_refusal_finishes_base_agent_without_unknown_wait(tmp_path):
    from dataclasses import replace

    from agent_orchestrator.evaluation.experiment import ArmSpec, ExperimentManifest, RunContext
    from agent_orchestrator.evaluation.metered_provider import MeteredProvider
    from simple_harness.providers import ProviderTarget

    world = World()
    provider = Provider("S")
    provider.target = ProviderTarget("fake", "agent-model", "fake", "https://example.test", "v1")
    budget = ExperimentBudget(100000, 10000, 110000, 1, 20)
    manifest = ExperimentManifest(
        "denial",
        "fake",
        "agent-model",
        budget,
        ("task",),
        1,
        0,
        1,
        tuple(ArmSpec(a, a) for a in ("S", "R", "D", "F")),
    )
    meter = MeteredProvider(
        provider,
        RunContext(manifest, manifest.runs()[0], lambda _: None),
        estimate_input_tokens=lambda _: 10000,
    )
    with AppWorldEpisode(
        AppWorldConfig("task", "denial"), world_factory=lambda **_: world
    ) as episode:
        result = await execute_arm(
            "S",
            episode,
            ArmRuntime(
                meter,
                "agent-model",
                UpperBoundTokenizer(),
                ContextPolicy(),
                replace(budget, calls=3),
                default_output_tokens=1024,
                maximum_output_tokens=8192,
            ),
            tmp_path / "denial",
        )
        assert result["runtime_states"] == ["failed"]
        assert meter.counters.calls == 1 and meter.unknown_usage_calls == 0
        assert len(provider.requests) == 1
