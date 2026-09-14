# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Offline AppWorld matrix admission, identity, and recovery contracts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_orchestrator.evaluation.appworld import AppWorldEpisode
from agent_orchestrator.evaluation.appworld_experiment import (
    AppWorldMatrixConfig,
    AppWorldPilotConfig,
    run_appworld_matrix,
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

TARGET = ProviderTarget("fake", "matrix-model", "pricing", "https://example.test", "v1")
TASKS = tuple(f"task-{index:02d}" for index in range(12))


def config(**changes):
    manifest = ExperimentManifest(
        "appworld-matrix-96",
        TARGET.provider_id,
        TARGET.model,
        ExperimentBudget(1000, 1000, 2000, 3, 20),
        TASKS,
        2,
        100,
        1,
        tuple(ArmSpec(arm, f"arm-{arm}-v1") for arm in ARMS),
    )
    return replace(
        AppWorldMatrixConfig(manifest, "source-v1", "profile-v1", "config-v1", "env-v1", 6),
        **changes,
    )


class World:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.task = SimpleNamespace(instruction="Use the public task.")
        self.saved = self.evaluated = self.closed = 0

    def execute(self, code):
        return "observed"

    def save(self):
        self.saved += 1

    def evaluate(self):
        self.evaluated += 1
        return SimpleNamespace(to_dict=lambda: {"success": True})

    def close(self):
        self.closed += 1


class Provider:
    target = TARGET

    def __init__(self, *, unknown=False):
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


def factories(worlds, providers, execute, *, unknown=False):
    def episode_factory(app_config):
        def world_factory(**kwargs):
            world = World(**kwargs)
            worlds.append(world)
            return world

        return AppWorldEpisode(app_config, world_factory=world_factory)

    def provider_factory(context, timeout):
        assert timeout == 6
        provider = Provider(unknown=unknown)
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


async def invoke_one(runtime, arm):
    return await runtime.provider.invoke(
        ProviderRequest(
            RequestId(f"request-{arm}"),
            (Message(MessageRole.USER, arm),),
            max_output_tokens=20,
        ),
        cancel=CancelToken(),
    )


def receipt(root: Path):
    return json.loads((root / "experiment.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_general_entrypoint_accepts_smaller_frozen_matrix(tmp_path):
    worlds, providers, seen = [], [], []
    smaller = config(manifest=replace(config().manifest, task_ids=TASKS[:2], repetitions=1))

    async def execute(arm, episode, runtime, root):
        seen.append((episode.config.task_id, arm, episode.config.random_seed))
        return {"mission_status": "COMPLETED"}

    result = await run_appworld_matrix(
        smaller, evidence_root=tmp_path, **factories(worlds, providers, execute)
    )
    assert len(result["runs"]) == 8
    assert seen == [(run.task_id, run.arm, run.seed) for run in smaller.manifest.runs()]
    assert all(row["status"] == "success" for row in result["runs"])


@pytest.mark.asyncio
async def test_96_episode_enumeration_scoring_and_idempotent_resume(tmp_path):
    worlds, providers, seen = [], [], []

    async def execute(arm, episode, runtime, root):
        current = receipt(tmp_path)
        assert sum(row["status"] == "running" for row in current["runs"]) == 1
        assert episode.agent.instruction == "Use the public task."
        assert not hasattr(episode.agent, "evaluate")
        assert worlds[-1].evaluated == 0
        assert runtime.provider.provider is providers[-1]
        assert runtime.physical_slots == 1
        assert root.name == episode.config.experiment_name
        await invoke_one(runtime, arm)
        seen.append((episode.config.task_id, arm, episode.config.random_seed, root.name))
        return {"mission_status": "COMPLETED"}

    params = factories(worlds, providers, execute)
    result = await run_appworld_matrix(config(), evidence_root=tmp_path, **params)
    expected = config().manifest.runs()
    assert len(result["runs"]) == len(expected) == 96
    assert seen == [(run.task_id, run.arm, run.seed, run.run_id) for run in expected]
    assert len({item[3] for item in seen}) == 96
    assert {seed for _, _, seed, _ in seen} == {100, 101}
    assert len(worlds) == len(providers) == 96
    assert all(w.saved == 1 and w.evaluated == w.closed == 1 for w in worlds)
    assert all(row["status"] == "success" for row in result["runs"])
    assert all(row["matrix"]["valid_success"] is True for row in result["runs"])
    assert all(row["matrix"]["usage"]["known_counters"]["calls"] == 1 for row in result["runs"])
    identity = json.loads((tmp_path / "appworld-matrix.json").read_text())
    assert identity["world_seed"] == "manifest_run_seed"
    assert await run_appworld_matrix(config(), evidence_root=tmp_path, **params) == result
    assert len(worlds) == len(providers) == 96


@pytest.mark.asyncio
async def test_unknown_usage_stops_and_resume_does_not_reissue(tmp_path):
    worlds, providers = [], []

    async def execute(arm, episode, runtime, root):
        with pytest.raises(Exception, match="usage"):
            await invoke_one(runtime, arm)
        return {"mission_status": "COMPLETED"}

    params = factories(worlds, providers, execute, unknown=True)
    result = await run_appworld_matrix(config(), evidence_root=tmp_path, **params)
    assert len(worlds) == len(providers) == 1
    assert result["runs"][0]["status"] == "failure"
    assert result["runs"][0]["matrix"]["stop_matrix"] is True
    assert result["runs"][0]["matrix"]["usage"]["unknown_usage_calls"] == 1
    assert result["runs"][0]["matrix"]["usage"]["actual_tokens_complete"] is False
    assert [row["status"] for row in result["runs"][1:]] == ["pending"] * 95
    assert await run_appworld_matrix(config(), evidence_root=tmp_path, **params) == result
    assert len(worlds) == len(providers) == 1


@pytest.mark.asyncio
async def test_cancel_marks_admitted_run_interrupted_then_resumes_pending_only(tmp_path):
    worlds, providers, seen = [], [], []
    entered = asyncio.Event()

    async def blocked(arm, episode, runtime, root):
        entered.set()
        await asyncio.Event().wait()

    params = factories(worlds, providers, blocked)
    running = asyncio.create_task(run_appworld_matrix(config(), evidence_root=tmp_path, **params))
    await asyncio.wait_for(entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert receipt(tmp_path)["runs"][0]["status"] == "interrupted"
    assert worlds[0].closed == worlds[0].evaluated == 1

    async def complete(arm, episode, runtime, root):
        seen.append(root.name)
        return {"mission_status": "COMPLETED"}

    params["arm_executor"] = complete
    result = await run_appworld_matrix(config(), evidence_root=tmp_path, **params)
    assert result["runs"][0]["status"] == "interrupted"
    assert seen == [run.run_id for run in config().manifest.runs()[1:]]
    assert len(worlds) == len(providers) == 96
    assert all(row["status"] == "success" for row in result["runs"][1:])


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
        {"manifest": replace(config().manifest, seed=101)},
        {"manifest": replace(config().manifest, repetitions=3)},
        {"manifest": replace(config().manifest, task_ids=TASKS[:-1])},
    ],
)
async def test_wrong_identity_rejected_before_provider_or_world(tmp_path, change):
    worlds, providers = [], []

    async def execute(*args):
        return {"mission_status": "COMPLETED"}

    params = factories(worlds, providers, execute)
    await run_appworld_matrix(config(), evidence_root=tmp_path, **params)
    with pytest.raises(ValueError, match="mismatch"):
        await run_appworld_matrix(replace(config(), **change), evidence_root=tmp_path, **params)
    assert len(worlds) == len(providers) == 96


@pytest.mark.asyncio
async def test_pilot_and_matrix_roots_cannot_be_cross_resumed(tmp_path):
    worlds, providers = [], []

    async def execute(*args):
        return {"mission_status": "COMPLETED"}

    params = factories(worlds, providers, execute)
    with pytest.raises(ValueError, match="four"):
        AppWorldPilotConfig(config().manifest, "s", "p", "c", "e", 6)
    await run_appworld_matrix(config(), evidence_root=tmp_path, **params)
    pilot_manifest = replace(config().manifest, task_ids=TASKS[:4], repetitions=1)
    pilot = AppWorldPilotConfig(pilot_manifest, "source-v1", "profile-v1", "config-v1", "env-v1", 6)
    with pytest.raises(ValueError, match="identity"):
        await run_appworld_pilot(pilot, evidence_root=tmp_path, **params)
    assert len(worlds) == len(providers) == 96


@pytest.mark.asyncio
async def test_damaged_run_identity_rejected_before_provider_or_world(tmp_path):
    worlds, providers = [], []

    async def execute(*args):
        return {"mission_status": "COMPLETED"}

    params = factories(worlds, providers, execute)
    smaller = config(manifest=replace(config().manifest, task_ids=TASKS[:2], repetitions=1))
    await run_appworld_matrix(smaller, evidence_root=tmp_path, **params)
    original_count = len(worlds)
    document = receipt(tmp_path)
    document["runs"][0]["run"]["run_id"] = "wrong-run-id"
    (tmp_path / "experiment.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="run identities"):
        await run_appworld_matrix(smaller, evidence_root=tmp_path, **params)
    assert len(worlds) == len(providers) == original_count


@pytest.mark.asyncio
async def test_real_matrix_receipt_analysis_and_source_binding(tmp_path):
    from agent_orchestrator.evaluation.matrix_analysis import analyze_frozen_matrix

    worlds, providers = [], []
    frozen = config(manifest=replace(config().manifest, task_ids=TASKS[:1], repetitions=1))

    async def execute(arm, episode, runtime, root):
        await invoke_one(runtime, arm)
        return {"mission_status": "COMPLETED"}

    kwargs = factories(worlds, providers, execute)
    result = await run_appworld_matrix(frozen, evidence_root=tmp_path, **kwargs)
    identity = json.loads((tmp_path / "appworld-matrix.json").read_text())
    report = analyze_frozen_matrix(result, identity)
    assert report["paired"]["confirmed"] is True
    assert report["arms"]["S"]["usage_complete"] is True
    assert report["arms"]["S"]["cached_tokens"] is None
    document = receipt(tmp_path)
    document["matrix_identity"]["source_fingerprint"] = "different-source"
    (tmp_path / "experiment.json").write_text(json.dumps(document))
    count = len(providers)
    with pytest.raises(ValueError, match="source/profile/config identity"):
        await run_appworld_matrix(frozen, evidence_root=tmp_path, **kwargs)
    assert len(providers) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["capacity", "token_budget"])
async def test_swallowed_known_zero_refusal_is_durable_and_not_valid_success(tmp_path, denial):
    from agent_orchestrator.evaluation.metered_provider import ExperimentBudgetExhausted

    worlds, providers = [], []
    manifest = replace(config().manifest, task_ids=TASKS[:1], repetitions=1)
    cfg = config(manifest=manifest, max_inflight_tokens=30 if denial == "capacity" else None)

    async def execute(arm, episode, runtime, root):
        req = ProviderRequest(
            RequestId(f"denied-{arm}"), (Message(MessageRole.USER, arm),),
            max_output_tokens=20 if denial == "capacity" else 1001,
        )
        with pytest.raises(ExperimentBudgetExhausted):
            await runtime.provider.invoke(req, cancel=CancelToken())
        # Refusal persisted before control returns to the Agent, not only at finalization.
        running = next(row for row in receipt(tmp_path)["runs"] if row["status"] == "running")
        assert running["admission_denials"][0]["physical_calls"] == 0
        return {"mission_status": "COMPLETED"}

    params = factories(worlds, providers, execute)
    result = await run_appworld_matrix(cfg, evidence_root=tmp_path, **params)
    assert all(row["status"] == "failure" for row in result["runs"])
    assert all(p.calls == 0 for p in providers)
    for row in result["runs"]:
        scored = row["matrix"]
        assert scored["official_success"] is True
        assert scored["runtime_success"] is True
        assert scored["valid_success"] is False
        assert scored["usage"]["unknown_usage_calls"] == 0
        assert scored["usage"]["known_counters"]["calls"] == 0
        assert scored["admission_denials"][0]["reason"] == "ExperimentBudgetExhausted"
    assert await run_appworld_matrix(cfg, evidence_root=tmp_path, **params) == result
    assert len(providers) == 4
