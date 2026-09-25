# SPDX-License-Identifier: Apache-2.0
"""2026-09-25 UI 全量点击：未启用执行图的任务读取时如实返回 NOT_ENABLED。

产品任务不启用执行图内核；读取以前报 TASKGRAPH_POLICY_UNAVAILABLE /
HISTORY_INTEGRITY（要人工修复的损坏信号），实际什么也没坏。
"""
from types import SimpleNamespace

import pytest

from agent_orchestrator.api.taskgraph import TaskGraphReadApi, TaskGraphReadError
from agent_orchestrator.contracts import Budget, Mission, MissionStatus
from agent_orchestrator.graph.task_network import DEFAULT_PROJECTION_BUDGET
from agent_orchestrator.storage.store import Store
from agent_orchestrator.storage.taskgraph_store import TaskGraphStore


def _unused(*_args, **_kwargs):
    raise AssertionError("a Mission without TaskGraph must not reach graph readers")


def test_reads_of_a_mission_without_taskgraph_say_not_enabled(tmp_path):
    store = Store.open(tmp_path / "orch.db")
    store.insert_mission(Mission(
        id="m-plain", goal="g", success_criteria=("ok",), stop_conditions=(), allowed_tools=(),
        risk_level="sandbox", budget=Budget(max_tokens=1000, max_attempts=2), tenant_id="tenant-a",
        status=MissionStatus.CREATED, created_at=1.0, version=1, idempotency_key="m-plain",
    ), spec_hash="f" * 64)
    reads = TaskGraphReadApi(
        SimpleNamespace(store=store), tenant_id="tenant-a", principal=object(),
        history=TaskGraphStore(store), current_reader=_unused, epoch_reader=_unused,
        resolution_reader=_unused, source_validator=lambda *_: None,
        current_source_validator=_unused, convergence_diagnostics=_unused,
        graph_budget=DEFAULT_PROJECTION_BUDGET, seed_reader=_unused,
    )
    for read in (lambda: reads.snapshot("m-plain"),
                 lambda: reads.diff("m-plain", 1, 1),
                 lambda: reads.convergence("m-plain")):
        with pytest.raises(TaskGraphReadError) as caught:
            read()
        assert caught.value.error.code == "NOT_ENABLED"
