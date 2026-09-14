# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Offline coordinator contracts. No real AppWorld or model requests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_orchestrator.evaluation.appworld import AppWorldEpisode
from agent_orchestrator.evaluation.appworld_experiment import (
    AppWorldPilotConfig,
    run_appworld_pilot,
)
from agent_orchestrator.evaluation.experiment import (
    ARMS,
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
)
from simple_harness import Message, MessageRole
from simple_harness.contracts import RequestId
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderUsage,
)

TARGET = ProviderTarget("fake", "pilot-model", "pricing", "https://example.test", "v1")


class World:
    def __init__(self, **kwargs):
        self.config = kwargs
        self.task = SimpleNamespace(instruction="Perform the public task.", hidden_answer="SECRET")
        self.evaluated = self.saved = self.closed = 0
        self.actions = []

    def execute(self, code):
        self.actions.append(code)
        return "observed"

    def save(self):
        self.saved += 1

    def evaluate(self):
        self.evaluated += 1
        return SimpleNamespace(to_dict=lambda: {"success": False, "score": 0.25})

    def close(self):
        self.closed += 1


class Provider:
    target = TARGET

    def __init__(self, unknown=False):
        self.unknown = unknown
        self.calls = 0

    async def invoke(self, request, *, cancel):
        self.calls += 1
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "done"),
            model=TARGET.model,
            usage=None if self.unknown else ProviderUsage(12, 3, 15),
        )


def config():
    manifest = ExperimentManifest(
        "appworld-pilot",
        TARGET.provider_id,
        TARGET.model,
        ExperimentBudget(100, 100, 150, 3, 10),
        ("one", "two", "three", "four"),
        1,
        100,
        1,
        tuple(ArmSpec(arm, f"arm-{arm}-v1") for arm in ARMS),
    )
    return AppWorldPilotConfig(manifest, "source-v1", "profile-v1", "config-v1", "env-v1", 6)


def request(role):
    return ProviderRequest(
        RequestId(f"request-{role}"),
        (Message(MessageRole.USER, role),),
        max_output_tokens=20,
    )


def receipt(root: Path):
    return json.loads((root / "experiment.json").read_text())


def inputs(worlds, providers, execute):
    def episode_factory(app_config):
        def world_factory(**kwargs):
            world = World(**kwargs)
            worlds.append(world)
            return world

        return AppWorldEpisode(app_config, world_factory=world_factory)

    def provider_factory(_, timeout):
        assert timeout == 6
        provider = Provider()
        providers.append(provider)
        return provider

    return dict(
        provider_factory=provider_factory,
        estimator_factory=lambda _: lambda request: 15,
        tokenizer=object(),
        context_policy=object(),
        episode_factory=episode_factory,
        arm_executor=execute,
    )


@pytest.mark.asyncio
async def test_fixed_matrix_shared_meter_host_score_and_resume(tmp_path):
    worlds, providers, seen = [], [], []

    async def execute(arm, episode, runtime, root):
        current = receipt(tmp_path)
        assert len([r for r in current["runs"] if r["status"] == "running"]) == 1
        assert (tmp_path / "appworld-pilot.json").exists()
        assert (
            json.loads((tmp_path / "appworld-pilot.json").read_text())["provider_timeout_seconds"]
            == 6
        )
        assert episode.config.random_seed == 100
        assert episode.agent.instruction == "Perform the public task."
        assert not hasattr(episode.agent, "hidden_answer")
        assert not hasattr(episode.agent, "evaluate")
        assert worlds[-1].evaluated == 0
        assert runtime.provider.provider is providers[-1]
        assert runtime.budget == config().manifest.budget
        assert runtime.physical_slots == 1
        assert root.name == episode.config.experiment_name
        episode.agent.execute("public action")
        await runtime.provider.invoke(request(arm), cancel=CancelToken())
        seen.append((episode.config.task_id, arm, id(runtime.provider)))
        return {"arm": arm, "completed": True}

    params = inputs(worlds, providers, execute)
    result = await run_appworld_pilot(config(), evidence_root=tmp_path, **params)
    assert len(seen) == len(worlds) == len(providers) == 16
    assert len({world.config["experiment_name"] for world in worlds}) == 16
    assert {arm for _, arm, _ in seen} == set(ARMS)
    assert all(world.saved == 2 and world.evaluated == world.closed == 1 for world in worlds)
    assert all(r["status"] == "success" for r in result["runs"])
    assert all(r["pilot"]["runtime_success"] is True for r in result["runs"])
    assert all(r["pilot"]["official_success"] is False for r in result["runs"])
    assert all(r["pilot"]["usage"]["known_counters"]["calls"] == 1 for r in result["runs"])
    assert await run_appworld_pilot(config(), evidence_root=tmp_path, **params) == result
    assert len(worlds) == 16


@pytest.mark.asyncio
async def test_failure_retained_then_resume_only_pending(tmp_path):
    worlds, providers = [], []
    attempts = []

    async def execute(arm, episode, runtime, root):
        attempts.append(arm)
        if len(attempts) == 1:
            raise ValueError("local arm failure")
        return {"arm": arm}

    params = inputs(worlds, providers, execute)
    result = await run_appworld_pilot(config(), evidence_root=tmp_path, **params)
    first = result["runs"][0]
    assert first["status"] == "failure"
    assert first["pilot"]["runtime_error"] == "ValueError"
    assert first["pilot"]["official_success"] is False
    assert first["pilot"]["usage"]["known_counters"]["calls"] == 0
    assert len(attempts) == 16
    assert all(w.closed == w.evaluated == 1 for w in worlds)
    assert await run_appworld_pilot(config(), evidence_root=tmp_path, **params) == result
    assert len(attempts) == 16


@pytest.mark.asyncio
async def test_unknown_physical_usage_stops_remaining_pending(tmp_path):
    worlds, providers = [], []

    async def execute(arm, episode, runtime, root):
        with pytest.raises(Exception, match="usage"):
            await runtime.provider.invoke(request(arm), cancel=CancelToken())
        return {"arm": arm}

    params = inputs(worlds, providers, execute)
    params["provider_factory"] = lambda _, timeout: (
        providers.append(Provider(unknown=True)) or providers[-1]
    )
    result = await run_appworld_pilot(config(), evidence_root=tmp_path, **params)
    assert len(worlds) == len(providers) == 1
    assert result["runs"][0]["status"] == "failure"
    usage = result["runs"][0]["pilot"]["usage"]
    assert usage["known_counters"]["calls"] == 1
    assert usage["known_counters"]["total_tokens"] == 0  # Known lower bound, not zero actual.
    assert usage["unknown_usage_calls"] == 1 and usage["actual_tokens_complete"] is False
    assert all(r["status"] == "pending" for r in result["runs"][1:])
    assert worlds[0].evaluated == worlds[0].closed == 1
    await run_appworld_pilot(config(), evidence_root=tmp_path, **params)
    assert len(worlds) == 1


@pytest.mark.asyncio
async def test_unknown_watcher_cancels_stalled_arm_before_episode_deadline(tmp_path):
    worlds, providers = [], []
    cleaned = asyncio.Event()

    async def execute(arm, episode, runtime, root):
        try:
            with pytest.raises(Exception, match="usage"):
                await runtime.provider.invoke(request(arm), cancel=CancelToken())
            await asyncio.Event().wait()  # SDK has entered UNKNOWN and stalled.
        finally:
            cleaned.set()

    params = inputs(worlds, providers, execute)
    params["provider_factory"] = lambda _, timeout: (
        providers.append(Provider(unknown=True)) or providers[-1]
    )
    result = await asyncio.wait_for(
        run_appworld_pilot(config(), evidence_root=tmp_path, **params), 2
    )
    assert cleaned.is_set()
    assert result["runs"][0]["pilot"]["runtime_error"] == "UnknownProviderUsage"
    assert result["runs"][0]["pilot"]["stop_matrix"] is True
    assert all(r["status"] == "pending" for r in result["runs"][1:])
    assert worlds[0].closed == worlds[0].evaluated == 1


@pytest.mark.asyncio
async def test_environment_setup_failure_stops_without_fake_zero_usage(tmp_path):
    worlds, providers = [], []
    params = inputs(worlds, providers, lambda *args: None)

    def unavailable(_):
        raise ConnectionError("world service unavailable")

    params["episode_factory"] = unavailable
    result = await run_appworld_pilot(config(), evidence_root=tmp_path, **params)
    assert result["runs"][0]["pilot"]["stop_matrix"] is True
    assert result["runs"][0]["pilot"]["runtime_error"] == "ConnectionError"
    assert len(providers) == 1 and worlds == []
    assert all(r["status"] == "pending" for r in result["runs"][1:])


@pytest.mark.asyncio
async def test_host_evaluator_failure_closes_world_and_stops_matrix(tmp_path):
    worlds, providers = [], []

    class BrokenScore(World):
        def evaluate(self):
            self.evaluated += 1
            raise RuntimeError("official evaluator unavailable")

    def episode_factory(app_config):
        def world_factory(**kwargs):
            world = BrokenScore(**kwargs)
            worlds.append(world)
            return world

        return AppWorldEpisode(app_config, world_factory=world_factory)

    async def execute(arm, episode, runtime, root):
        episode.agent.execute("safe action")
        return {"completed": True}

    params = inputs(worlds, providers, execute)
    params["episode_factory"] = episode_factory
    result = await run_appworld_pilot(config(), evidence_root=tmp_path, **params)
    assert worlds[0].saved == 2 and worlds[0].evaluated == worlds[0].closed == 1
    assert result["runs"][0]["pilot"]["runtime_success"] is True
    assert result["runs"][0]["pilot"]["official_success"] is None
    assert result["runs"][0]["pilot"]["finalization_error"] == "RuntimeError"
    assert result["runs"][0]["status"] == "failure"
    assert all(r["status"] == "pending" for r in result["runs"][1:])


@pytest.mark.asyncio
async def test_cancel_finalizes_world_and_keeps_rest_pending(tmp_path):
    worlds, providers = [], []
    entered = asyncio.Event()

    async def execute(arm, episode, runtime, root):
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    params = inputs(worlds, providers, execute)
    task = asyncio.create_task(run_appworld_pilot(config(), evidence_root=tmp_path, **params))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worlds[0].closed == worlds[0].evaluated == 1
    assert receipt(tmp_path)["runs"][0]["status"] == "interrupted"
    assert all(r["status"] == "pending" for r in receipt(tmp_path)["runs"][1:])
    # World lease was released even under cancellation.
    AppWorldEpisode(config=world_config(), world_factory=lambda **kw: World(**kw)).finalize()


def world_config():
    from agent_orchestrator.evaluation.appworld import AppWorldConfig

    return AppWorldConfig("one", "new-episode")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"source_fingerprint": "other"},
        {"profile_fingerprint": "other"},
        {"config_fingerprint": "other"},
        {"environment_fingerprint": "other"},
        {"provider_timeout_seconds": 7},
        {"remote_environment_url": "https://different.example.test"},
        {"manifest": replace(config().manifest, budget=ExperimentBudget(101, 100, 150, 3, 10))},
    ],
)
async def test_identity_mismatch_before_world_or_provider(tmp_path, change):
    worlds, providers = [], []

    async def execute(*args):
        return {"ok": True}

    params = inputs(worlds, providers, execute)
    await run_appworld_pilot(config(), evidence_root=tmp_path, **params)
    count = len(worlds)
    with pytest.raises(ValueError, match="mismatch"):
        await run_appworld_pilot(replace(config(), **change), evidence_root=tmp_path, **params)
    assert len(worlds) == count


def test_fixed_seed_and_exact_four_task_validation():
    with pytest.raises(ValueError, match="seed"):
        replace(config(), manifest=replace(config().manifest, seed=101))
    with pytest.raises(ValueError, match="four"):
        replace(config(), manifest=replace(config().manifest, task_ids=("one",)))


@pytest.mark.asyncio
async def test_failed_mission_return_is_not_successful_runtime(tmp_path):
    worlds, providers = [], []

    async def execute(*args):
        return {"mission_status": "FAILED", "stop_reason": "budget_exhausted"}

    result = await run_appworld_pilot(
        config(), evidence_root=tmp_path, **inputs(worlds, providers, execute)
    )
    assert all(r["status"] == "failure" for r in result["runs"])
    assert all(r["pilot"]["executor_returned"] for r in result["runs"])
    assert all(r["pilot"]["runtime_success"] is False for r in result["runs"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared,expected", [("success", True), ("fail", False), ("pending", False)]
)
async def test_false_completion_uses_saved_declaration_and_external_score(
    tmp_path, declared, expected
):
    worlds, providers = [], []

    async def execute(arm, episode, *args):
        worlds[-1].evaluate = lambda: SimpleNamespace(
            to_dict=lambda: {"success": False, "declared_task_status": declared}
        )
        return {"runtime_states": ["committed"]}

    result = await run_appworld_pilot(
        config(), evidence_root=tmp_path, **inputs(worlds, providers, execute)
    )
    assert all(r["pilot"]["false_completion"] is expected for r in result["runs"])
