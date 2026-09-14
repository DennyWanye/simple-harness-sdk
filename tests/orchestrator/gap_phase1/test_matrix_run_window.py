# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Offline matrix window and physical-handoff admission contracts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent_orchestrator.evaluation.appworld import AppWorldEpisode
from agent_orchestrator.evaluation.appworld_experiment import (
    AppWorldMatrixConfig,
    AppWorldPilotConfig,
    run_appworld_matrix,
)
from agent_orchestrator.evaluation.experiment import (
    ARMS,
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
)
from agent_orchestrator.evaluation.metered_provider import RunWindowDenied, UnknownProviderUsage
from agent_orchestrator.evaluation.run_window import ProviderWindowSchedule, UtcWeeklyWindow
from simple_harness import Message, MessageRole
from simple_harness.contracts import RequestId
from simple_harness.providers import (
    CancelToken,
    ProviderRequest,
    ProviderResponse,
    ProviderTarget,
    ProviderUsage,
)

TARGET = ProviderTarget("fake", "window-model", "pricing", "https://example.test", "v1")
SCHEDULE = ProviderWindowSchedule("fake", "idle-v1", (UtcWeeklyWindow(0, 0, 60),))
MONDAY = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


def config(**changes):
    manifest = ExperimentManifest(
        "window-matrix",
        "fake",
        "window-model",
        ExperimentBudget(1000, 1000, 2000, 3, 20),
        tuple(f"task-{n:02d}" for n in range(12)),
        2,
        100,
        1,
        tuple(ArmSpec(arm, f"arm-{arm}") for arm in ARMS),
    )
    return replace(
        AppWorldMatrixConfig(
            manifest,
            "source",
            "profile",
            "config",
            "environment",
            6,
            run_window=SCHEDULE,
            queue_wait_seconds=3,
            drain_margin_seconds=2,
            max_inflight_tokens=100,
        ),
        **changes,
    )


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class World:
    def __init__(self):
        self.task = SimpleNamespace(instruction="Do task")

    def save(self):
        pass

    def evaluate(self):
        return SimpleNamespace(to_dict=lambda: {"success": True})

    def close(self):
        pass


class Provider:
    target = TARGET

    def __init__(self, *, unknown=False):
        self.calls = 0
        self.unknown = unknown

    async def invoke(self, request, *, cancel):
        self.calls += 1
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "ok"),
            model=TARGET.model,
            usage=None if self.unknown else ProviderUsage(10, 2, 12),
        )


def args(worlds, providers, execute, *, unknown=False):
    def episode_factory(app_config):
        worlds.append(app_config)
        return AppWorldEpisode(app_config, world_factory=lambda **kwargs: World())

    def provider_factory(context, timeout):
        assert timeout == 6
        provider = Provider(unknown=unknown)
        providers.append(provider)
        return provider

    return dict(
        provider_factory=provider_factory,
        estimator_factory=lambda _: lambda request: 10,
        tokenizer=object(),
        context_policy=object(),
        episode_factory=episode_factory,
        arm_executor=execute,
    )


async def call(runtime, arm):
    return await runtime.provider.invoke(
        ProviderRequest(
            RequestId(f"request-{arm}"), (Message(MessageRole.USER, arm),), max_output_tokens=10
        ),
        cancel=CancelToken(),
    )


def receipt(root):
    return json.loads((root / "experiment.json").read_text())


@pytest.mark.asyncio
async def test_96_pending_pause_resume_and_frozen_identity(tmp_path):
    worlds, providers = [], []
    clock = Clock(MONDAY.replace(hour=0, minute=59, second=40))

    async def execute(arm, episode, runtime, root):
        await call(runtime, arm)
        return {"mission_status": "COMPLETED"}

    params = args(worlds, providers, execute)
    first = await run_appworld_matrix(config(), evidence_root=tmp_path, clock=clock, **params)
    assert [row["status"] for row in first["runs"]] == ["pending"] * 96
    assert first["runs"][0]["run_window"]["next_allowed_start"] == "2026-09-21T00:00:00+00:00"
    assert receipt(tmp_path) == first
    assert not worlds and not providers
    changed = replace(
        config(), run_window=ProviderWindowSchedule("fake", "idle-v2", (UtcWeeklyWindow(0, 0, 60),))
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        await run_appworld_matrix(changed, evidence_root=tmp_path, clock=clock, **params)
    with pytest.raises(ValueError, match="identity mismatch"):
        await run_appworld_matrix(
            replace(config(), max_inflight_tokens=101),
            evidence_root=tmp_path,
            clock=clock,
            **params,
        )
    for changed_margin in (
        replace(config(), queue_wait_seconds=4),
        replace(config(), drain_margin_seconds=3),
    ):
        with pytest.raises(ValueError, match="identity mismatch"):
            await run_appworld_matrix(
                changed_margin, evidence_root=tmp_path, clock=clock, **params
            )
    assert not worlds and not providers
    clock.value = MONDAY.replace(minute=1)
    resumed = await run_appworld_matrix(config(), evidence_root=tmp_path, clock=clock, **params)
    assert len(worlds) == len(providers) == 96
    assert all(p.calls == 1 for p in providers)
    assert all(row["status"] == "success" for row in resumed["runs"])
    assert "run_window" not in resumed["runs"][0]
    assert (
        await run_appworld_matrix(config(), evidence_root=tmp_path, clock=clock, **params)
        == resumed
    )
    assert len(providers) == 96


@pytest.mark.asyncio
async def test_handoff_clock_advance_denies_zero_physical_and_pauses_pending(tmp_path):
    worlds, providers = [], []
    clock = Clock(MONDAY.replace(minute=1))

    async def execute(arm, episode, runtime, root):
        clock.value = MONDAY.replace(minute=59, second=55)
        with pytest.raises(RunWindowDenied):
            await call(runtime, arm)
        return {"mission_status": "COMPLETED"}

    result = await run_appworld_matrix(
        config(), evidence_root=tmp_path, clock=clock, **args(worlds, providers, execute)
    )
    assert len(worlds) == len(providers) == 1
    assert providers[0].calls == 0
    assert result["runs"][0]["status"] == "failure"
    assert result["runs"][0]["matrix"]["usage"]["known_counters"]["calls"] == 0
    assert result["runs"][0]["matrix"]["usage"]["unknown_usage_calls"] == 0
    assert [row["status"] for row in result["runs"][1:]] == ["pending"] * 95
    assert result["runs"][1]["run_window"]["next_allowed_start"] == "2026-09-21T00:00:00+00:00"
    assert receipt(tmp_path) == result


@pytest.mark.asyncio
async def test_clock_advance_during_weighted_queue_denies_second_physical_call(tmp_path):
    worlds, providers = [], []
    clock = Clock(MONDAY.replace(minute=1))
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(Provider):
        async def invoke(self, request, *, cancel):
            self.calls += 1
            entered.set()
            await release.wait()
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "ok"),
                model=TARGET.model,
                usage=ProviderUsage(10, 2, 12),
            )

    async def execute(arm, episode, runtime, root):
        first = asyncio.create_task(call(runtime, arm))
        await entered.wait()
        second = asyncio.create_task(call(runtime, arm))
        await asyncio.sleep(0)
        clock.value = MONDAY.replace(minute=59, second=55)
        release.set()
        await first
        with pytest.raises(RunWindowDenied):
            await second
        return {"mission_status": "COMPLETED"}

    params = args(worlds, providers, execute)

    def provider_factory(context, timeout):
        provider = SlowProvider()
        providers.append(provider)
        return provider

    params["provider_factory"] = provider_factory
    result = await run_appworld_matrix(config(), evidence_root=tmp_path, clock=clock, **params)
    assert len(providers) == 1 and providers[0].calls == 1
    assert result["runs"][0]["matrix"]["usage"]["known_counters"]["calls"] == 1
    assert result["runs"][0]["matrix"]["usage"]["unknown_usage_calls"] == 0
    assert result["runs"][1]["status"] == "pending"


@pytest.mark.asyncio
async def test_actual_call_before_peak_accounted_and_unknown_still_blocks(tmp_path):
    worlds, providers = [], []
    clock = Clock(MONDAY.replace(minute=59, second=30))

    async def execute(arm, episode, runtime, root):
        if providers[-1].unknown:
            with pytest.raises(UnknownProviderUsage):
                await call(runtime, arm)
        else:
            await call(runtime, arm)
        return {"mission_status": "COMPLETED"}

    small = replace(config(), manifest=replace(config().manifest, task_ids=("one",), repetitions=1))
    known_root = tmp_path / "known"
    known = await run_appworld_matrix(
        small, evidence_root=known_root, clock=clock, **args(worlds, providers, execute)
    )
    assert len(providers) == 4 and all(provider.calls == 1 for provider in providers)
    assert all(row["matrix"]["usage"]["known_counters"]["calls"] == 1 for row in known["runs"])
    assert all(row["status"] == "success" for row in known["runs"])
    worlds.clear()
    providers.clear()
    result = await run_appworld_matrix(
        small, evidence_root=tmp_path, clock=clock, **args(worlds, providers, execute, unknown=True)
    )
    assert len(providers) == 1 and providers[0].calls == 1
    assert result["runs"][0]["matrix"]["usage"]["known_counters"]["calls"] == 1
    assert result["runs"][0]["matrix"]["usage"]["unknown_usage_calls"] == 1
    assert result["runs"][0]["matrix"]["stop_matrix"] is True
    assert [row["status"] for row in result["runs"][1:]] == ["pending"] * 3
    pilot = AppWorldPilotConfig(
        replace(config().manifest, task_ids=("a", "b", "c", "d"), repetitions=1),
        "source",
        "profile",
        "config",
        "environment",
        6,
    )
    assert "run_window" not in pilot.identity()
    assert "max_inflight_tokens" not in pilot.identity()
