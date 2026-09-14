# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Bookkeeping tests with injected fakes; none are S/R/D/F algorithms."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from agent_orchestrator.evaluation.experiment import (
    ARMS,
    ArmExecutor,
    ArmSpec,
    ExecutionCounters,
    ExecutionResult,
    ExperimentBudget,
    ExperimentManifest,
    RunContext,
    run_experiment,
)


def manifest() -> ExperimentManifest:
    return ExperimentManifest(
        experiment_id="offline-matrix",
        provider="fake-provider",
        model="fake-model",
        budget=ExperimentBudget(100, 100, 150, 5, 1),
        task_ids=("fake-task",),
        repetitions=1,
        seed=17,
        physical_slots=2,
        arms=tuple(ArmSpec(arm, f"fake-{arm}-v1") for arm in ARMS),
    )


def counters() -> ExecutionCounters:
    return ExecutionCounters("fake-provider", "fake-model", 10, 5, 15, 1, 1)


async def successful(context: RunContext) -> ExecutionResult:
    return ExecutionResult(True, counters())


def bindings(plan: ExperimentManifest, execute=successful) -> dict[str, ArmExecutor]:
    return {
        arm.arm: ArmExecutor(arm.arm, arm.executor_id, plan.provider, plan.model, execute)
        for arm in plan.arms
    }


def read(root: Path) -> dict:
    return json.loads((root / "experiment.json").read_text())


def test_manifest_deep_freeze_unique_ids_and_paired_seed() -> None:
    tasks = ["task-a", "task-b"]
    plan = replace(manifest(), task_ids=tasks, repetitions=2)
    tasks.append("changed-after-declaration")
    assert plan.task_ids == ("task-a", "task-b")
    with pytest.raises(FrozenInstanceError):
        setattr(plan.budget, "calls", 999)
    with pytest.raises(FrozenInstanceError):
        setattr(plan, "seed", 99)
    runs = plan.runs()
    assert len(runs) == len({run.run_id for run in runs}) == 16
    assert runs == plan.runs()
    for offset in range(0, len(runs), 4):
        group = runs[offset : offset + 4]
        assert set(run.arm for run in group) == set(ARMS)
        assert len({(run.task_id, run.repetition, run.seed) for run in group}) == 1
    assert plan.fingerprint != replace(plan, seed=18).fingerprint


def test_four_task_schedule_balances_every_arm_position():
    plan = replace(manifest(), task_ids=("a", "b", "c", "d"), repetitions=1)
    runs = plan.runs()
    assert [r.arm for r in runs[:4]] == list(ARMS)
    for position in range(4):
        assert {runs[task * 4 + position].arm for task in range(4)} == set(ARMS)
    assert plan.to_dict()["schedule_version"] == "latin-square-v1"


def test_serial_admission_counters_are_durable_and_replay_does_not_execute(tmp_path: Path) -> None:
    plan = manifest()
    invoked = []

    async def execute(context: RunContext) -> ExecutionResult:
        receipt = read(tmp_path)
        active = [record for record in receipt["runs"] if record["status"] == "running"]
        assert len(active) == 1
        assert active[0]["run"]["run_id"] == context.run.run_id
        assert receipt["manifest"] == context.manifest.to_dict()
        context.report_usage(counters())
        assert read(tmp_path)["runs"][len(invoked)]["counters"]["total_tokens"] == 15
        invoked.append(context.run.arm)
        await asyncio.sleep(0)
        return ExecutionResult(True, counters())

    executors = bindings(plan, execute)
    first = asyncio.run(run_experiment(plan, executors=executors, evidence_root=tmp_path))
    second = asyncio.run(run_experiment(plan, executors=executors, evidence_root=tmp_path))
    assert invoked == list(ARMS)
    assert first == second == read(tmp_path)
    assert all(record["status"] == "success" for record in first["runs"])


@pytest.mark.parametrize(
    "change",
    [
        {"experiment_id": "different"},
        {"provider": "other-provider"},
        {"model": "other-model"},
        {"task_ids": ("different-task",)},
        {"repetitions": 2},
        {"seed": 18},
        {"physical_slots": 3},
        {"budget": ExperimentBudget(101, 100, 150, 5, 1)},
        {"budget": ExperimentBudget(100, 101, 150, 5, 1)},
        {"budget": ExperimentBudget(100, 100, 151, 5, 1)},
        {"budget": ExperimentBudget(100, 100, 150, 6, 1)},
        {"budget": ExperimentBudget(100, 100, 150, 5, 2)},
        {"arms": tuple(ArmSpec(arm, f"fake-{arm}-v2") for arm in ARMS)},
    ],
)
def test_replay_refuses_every_frozen_configuration_change(tmp_path: Path, change: dict) -> None:
    plan = manifest()
    asyncio.run(run_experiment(plan, executors=bindings(plan), evidence_root=tmp_path))
    original = (tmp_path / "experiment.json").read_bytes()
    changed = replace(plan, **change)
    with pytest.raises(ValueError, match="configuration mismatch"):
        asyncio.run(run_experiment(changed, executors=bindings(changed), evidence_root=tmp_path))
    assert (tmp_path / "experiment.json").read_bytes() == original


def test_binding_substitution_or_missing_arm_is_rejected_before_execution(tmp_path: Path) -> None:
    plan = manifest()
    for attribute, value in (
        ("arm", "S"),
        ("provider", "other"),
        ("model", "other"),
        ("executor_id", "fallback"),
    ):
        executors = bindings(plan)
        executors["R"] = replace(executors["R"], **{attribute: value})
        with pytest.raises(ValueError, match="binding mismatch"):
            asyncio.run(run_experiment(plan, executors=executors, evidence_root=tmp_path))
    executors = bindings(plan)
    del executors["F"]
    with pytest.raises(ValueError, match="exactly S/R/D/F"):
        asyncio.run(run_experiment(plan, executors=executors, evidence_root=tmp_path))
    assert not (tmp_path / "experiment.json").exists()


def test_failure_and_deadline_retain_usage_and_are_not_replaced(tmp_path: Path) -> None:
    plan = replace(manifest(), budget=replace(manifest().budget, seconds=0.2))
    invoked = []

    async def execute(context: RunContext) -> ExecutionResult:
        invoked.append(context.run.arm)
        context.report_usage(counters())
        if context.run.arm == "D":
            raise RuntimeError("fake executor failed after a call")
        if context.run.arm == "F":
            await asyncio.Event().wait()
        return ExecutionResult(context.run.arm != "R", counters())

    executors = bindings(plan, execute)
    result = asyncio.run(run_experiment(plan, executors=executors, evidence_root=tmp_path))
    assert [record["status"] for record in result["runs"]] == [
        "success",
        "failure",
        "failure",
        "deadline",
    ]
    assert all(record["counters"]["calls"] == 1 for record in result["runs"])
    asyncio.run(run_experiment(plan, executors=executors, evidence_root=tmp_path))
    assert invoked == list(ARMS)


def test_cancelled_and_crash_interrupted_identity_is_not_retried(tmp_path: Path) -> None:
    plan = manifest()
    invoked = []

    async def cancel_first() -> None:
        entered = asyncio.Event()

        async def execute(context: RunContext) -> ExecutionResult:
            invoked.append(context.run.arm)
            context.report_usage(counters())
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        task = asyncio.create_task(
            run_experiment(plan, executors=bindings(plan, execute), evidence_root=tmp_path)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_first())
    receipt = read(tmp_path)
    assert [record["status"] for record in receipt["runs"]] == [
        "interrupted",
        "pending",
        "pending",
        "pending",
    ]
    # Simulate a crash before the terminal receipt was durable.
    receipt["runs"][0]["status"] = "running"
    (tmp_path / "experiment.json").write_text(json.dumps(receipt))

    async def resume(context: RunContext) -> ExecutionResult:
        invoked.append(context.run.arm)
        return ExecutionResult(True, counters())

    result = asyncio.run(
        run_experiment(plan, executors=bindings(plan, resume), evidence_root=tmp_path)
    )
    assert invoked == list(ARMS)
    assert result["runs"][0]["status"] == "interrupted"
    assert result["runs"][0]["counters"]["calls"] == 1


def test_duplicate_replay_identity_is_refused(tmp_path: Path) -> None:
    plan = manifest()
    asyncio.run(run_experiment(plan, executors=bindings(plan), evidence_root=tmp_path))
    receipt = read(tmp_path)
    receipt["runs"][1]["run"] = receipt["runs"][0]["run"]
    (tmp_path / "experiment.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="identities or states mismatch"):
        asyncio.run(run_experiment(plan, executors=bindings(plan), evidence_root=tmp_path))


@pytest.mark.parametrize(
    "reported",
    [
        ExecutionCounters("other", "fake-model", 10, 5, 15, 1, 1),
        ExecutionCounters("fake-provider", "other", 10, 5, 15, 1, 1),
        ExecutionCounters("fake-provider", "fake-model", 101, 0, 101, 1, 1),
        ExecutionCounters("fake-provider", "fake-model", 0, 101, 101, 1, 1),
        ExecutionCounters("fake-provider", "fake-model", 76, 75, 151, 1, 1),
        ExecutionCounters("fake-provider", "fake-model", 10, 5, 15, 6, 1),
        ExecutionCounters("fake-provider", "fake-model", 10, 5, 15, 1, 3),
    ],
)
def test_swallowed_budget_or_provider_violation_cannot_become_success(
    tmp_path: Path, reported: ExecutionCounters
) -> None:
    plan = manifest()

    async def execute(context: RunContext) -> ExecutionResult:
        try:
            context.report_usage(reported)
        except ValueError:
            pass
        return ExecutionResult(True, counters())

    result = asyncio.run(
        run_experiment(plan, executors=bindings(plan, execute), evidence_root=tmp_path)
    )
    assert all(record["status"] == "failure" for record in result["runs"])


def test_swallowed_deadline_stays_deadline_and_unknown_usage_stays_unknown(tmp_path: Path) -> None:
    plan = replace(manifest(), budget=replace(manifest().budget, seconds=0.2))

    async def execute(context: RunContext) -> ExecutionResult:
        if context.run.arm != "S":
            raise RuntimeError("no counters reported")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ExecutionResult(True, counters())
        raise AssertionError("unreachable")

    result = asyncio.run(
        run_experiment(plan, executors=bindings(plan, execute), evidence_root=tmp_path)
    )
    assert result["runs"][0]["status"] == "deadline"
    assert all(record["counters"] is None for record in result["runs"][1:])


def test_concurrent_runner_cannot_admit_duplicate_execution(tmp_path: Path) -> None:
    plan = manifest()
    invoked = []

    async def scenario() -> None:
        entered = asyncio.Event()

        async def execute(context: RunContext) -> ExecutionResult:
            invoked.append(context.run.run_id)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        executors = bindings(plan, execute)
        first = asyncio.create_task(
            run_experiment(plan, executors=executors, evidence_root=tmp_path)
        )
        await entered.wait()
        try:
            with pytest.raises(OSError):
                await run_experiment(plan, executors=executors, evidence_root=tmp_path)
        finally:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

    asyncio.run(scenario())
    assert len(invoked) == 1


def test_failed_admission_write_preserves_receipt_and_does_not_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = manifest()
    invoked = []
    real_replace = os.replace

    def fail_admission(source, destination) -> None:
        pending_write = json.loads(Path(source).read_text())
        if any(record["status"] == "running" for record in pending_write["runs"]):
            raise OSError("fake disk failure during atomic replace")
        real_replace(source, destination)

    async def execute(context: RunContext) -> ExecutionResult:
        invoked.append(context.run.arm)
        return ExecutionResult(True, counters())

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_admission)
        with pytest.raises(OSError, match="fake disk failure"):
            asyncio.run(
                run_experiment(plan, executors=bindings(plan, execute), evidence_root=tmp_path)
            )
    assert invoked == []
    assert all(record["status"] == "pending" for record in read(tmp_path)["runs"])
    assert not list(tmp_path.glob(".experiment.json.*"))
    asyncio.run(run_experiment(plan, executors=bindings(plan, execute), evidence_root=tmp_path))
    assert invoked == list(ARMS)
