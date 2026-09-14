# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Offline lifecycle checks; no AppWorld package or task data required."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from agent_orchestrator.evaluation.appworld import AppWorldConfig, AppWorldEpisode


class FakeWorld:
    def __init__(self, task_id: str, experiment_name: str) -> None:
        self.task = SimpleNamespace(instruction=f"instruction for {task_id}", ground_truth="hidden")
        self.task_id = task_id
        self.experiment_name = experiment_name
        self.calls: list[str] = []
        self.outputs: list[str] = []
        self.saved = 0
        self.closed = 0
        self.evaluated = 0
        self.fail_execute = False
        self.snapshots: dict[str, list[str]] = {}

    def checkpoint(self, name: str) -> None:
        self.snapshots[name] = list(self.calls)

    def restore(self, name: str) -> None:
        self.calls = list(self.snapshots[name])

    def execute(self, code: str) -> str:
        self.calls.append(code)
        if self.fail_execute:
            raise RuntimeError("execution failed")
        output = f"output {len(self.calls)}"
        self.outputs.append(output)
        return output

    def save(self) -> None:
        self.saved += 1

    def evaluate(self) -> SimpleNamespace:
        self.evaluated += 1
        return SimpleNamespace(to_dict=lambda: {"success": len(self.calls) > 0})

    def close(self) -> None:
        self.closed += 1


def test_one_world_agent_observations_and_host_evaluation() -> None:
    worlds: list[FakeWorld] = []

    def factory(**kwargs: str) -> FakeWorld:
        world = FakeWorld(**kwargs)
        worlds.append(world)
        return world

    episode = AppWorldEpisode(AppWorldConfig("task-a", "experiment-a"), world_factory=factory)
    agent = episode.agent
    assert agent.instruction == "instruction for task-a"
    assert not hasattr(agent, "task")
    assert not hasattr(agent, "ground_truth")
    assert not hasattr(agent, "evaluate")
    assert not hasattr(agent, "finalize")
    assert not hasattr(agent, "_world")
    assert agent.execute("print(1)") == {"output": "output 1"}
    assert agent.execute("print(2)") == {"output": "output 2"}
    assert json.loads(json.dumps(agent.execute("print(3)"))) == {"output": "output 3"}
    assert len(worlds) == 1
    assert worlds[0].calls == ["print(1)", "print(2)", "print(3)"]
    assert worlds[0].saved == 3
    assert worlds[0].evaluated == 0

    result = episode.finalize()
    assert result == {"success": True}
    assert worlds[0].saved == 4
    assert worlds[0].evaluated == worlds[0].closed == 1
    assert episode.finalize() == result
    with pytest.raises(RuntimeError, match="finalized"):
        agent.execute("print(4)")


def test_candidate_restore_is_host_only_and_does_not_score_candidates():
    world = FakeWorld("task-r", "experiment-r")
    with AppWorldEpisode(
        AppWorldConfig("task-r", "experiment-r"), world_factory=lambda **_: world
    ) as episode:
        assert not hasattr(episode.agent, "checkpoint")
        assert not hasattr(episode.agent, "restore")
        initial = episode.checkpoint()
        episode.agent.execute("candidate one")
        first = episode.checkpoint()
        episode.restore(initial)
        assert world.calls == []
        episode.agent.execute("candidate two")
        episode.restore(first)
        assert world.calls == ["candidate one"] and world.evaluated == 0
        with pytest.raises(ValueError, match="unknown episode"):
            episode.restore("foreign")
    assert world.evaluated == 1
    with pytest.raises(RuntimeError, match="finalized"):
        episode.restore(first)


@pytest.mark.parametrize("failure", [ValueError("agent failed"), TimeoutError("episode timed out")])
def test_context_exit_evaluates_and_closes_on_agent_failure(failure: Exception) -> None:
    world = FakeWorld("task-b", "experiment-b")
    with pytest.raises(type(failure), match=str(failure)):
        with AppWorldEpisode(
            AppWorldConfig("task-b", "experiment-b"), world_factory=lambda **_: world
        ) as episode:
            raise failure
    assert episode.agent.instruction == "instruction for task-b"
    assert world.saved == world.evaluated == world.closed == 1


def test_failed_execution_still_saves_and_final_evaluation_is_separate() -> None:
    world = FakeWorld("task-c", "experiment-c")
    world.fail_execute = True
    episode = AppWorldEpisode(
        AppWorldConfig("task-c", "experiment-c"), world_factory=lambda **_: world
    )
    with pytest.raises(RuntimeError, match="execution failed"):
        episode.agent.execute("bad code")
    assert world.saved == 1
    assert world.evaluated == 0
    assert episode.close() == {"success": True}
    assert world.saved == 2
    assert world.evaluated == world.closed == 1


def test_evaluator_failure_still_closes_and_blocks_further_execution() -> None:
    class FailingEvaluationWorld(FakeWorld):
        def evaluate(self) -> SimpleNamespace:
            self.evaluated += 1
            raise RuntimeError("evaluator failed")

    world = FailingEvaluationWorld("task-error", "experiment-error")
    episode = AppWorldEpisode(
        AppWorldConfig("task-error", "experiment-error"), world_factory=lambda **_: world
    )
    with pytest.raises(RuntimeError, match="evaluator failed"):
        episode.finalize()
    assert world.saved == world.evaluated == world.closed == 1
    with pytest.raises(RuntimeError, match="finalized"):
        episode.agent.execute("too late")


def test_separate_episodes_get_separate_worlds() -> None:
    worlds: list[FakeWorld] = []

    def factory(**kwargs: str) -> FakeWorld:
        world = FakeWorld(**kwargs)
        worlds.append(world)
        return world

    for task_id in ("task-1", "task-2"):
        with AppWorldEpisode(
            AppWorldConfig(task_id, "experiment"), world_factory=factory
        ) as episode:
            assert episode.agent.instruction == f"instruction for {task_id}"
            episode.agent.execute(task_id)
    assert worlds[0] is not worlds[1]
    assert [world.calls for world in worlds] == [["task-1"], ["task-2"]]
    assert all(world.saved == 2 and world.closed == 1 for world in worlds)


def test_overlapping_episodes_are_rejected_and_lease_is_released() -> None:
    first = FakeWorld("task-one", "experiment")
    episode = AppWorldEpisode(
        AppWorldConfig("task-one", "experiment"), world_factory=lambda **_: first
    )
    with pytest.raises(RuntimeError, match="another AppWorld episode"):
        AppWorldEpisode(
            AppWorldConfig("task-two", "experiment"),
            world_factory=lambda **_: FakeWorld("task-two", "experiment"),
        )
    assert first.closed == 0
    episode.finalize()
    second = FakeWorld("task-two", "experiment")
    AppWorldEpisode(
        AppWorldConfig("task-two", "experiment"), world_factory=lambda **_: second
    ).finalize()
    assert first.closed == second.closed == 1


def test_execution_is_serialized_against_other_execution_and_finalization() -> None:
    entered = Event()
    release = Event()

    class BlockingWorld(FakeWorld):
        def execute(self, code: str) -> str:
            if code == "first":
                entered.set()
                assert release.wait(timeout=2)
            return super().execute(code)

    world = BlockingWorld("task-d", "experiment-d")
    episode = AppWorldEpisode(
        AppWorldConfig("task-d", "experiment-d"), world_factory=lambda **_: world
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(episode.agent.execute, "first")
        assert entered.wait(timeout=2)
        second = pool.submit(episode.agent.execute, "second")
        release.set()
        assert first.result(timeout=2) == {"output": "output 1"}
        assert second.result(timeout=2) == {"output": "output 2"}
    assert world.calls == ["first", "second"]
    assert episode.finalize() == {"success": True}


@pytest.mark.parametrize("task_id,experiment_name", [("", "x"), ("x", " ")])
def test_config_requires_task_and_experiment(task_id: str, experiment_name: str) -> None:
    with pytest.raises(ValueError):
        AppWorldConfig(task_id, experiment_name)
