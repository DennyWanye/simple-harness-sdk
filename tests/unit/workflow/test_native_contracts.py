# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from simple_harness.workflow.contracts import NodeExecutionIdentity, StatePatch
from simple_harness.workflow.errors import InvalidStatePatch
from simple_harness.workflow.native import (
    InMemoryNativeCheckpointStore,
    NativeCheckpointStore,
    NativeExecutionInfo,
    NativeExecutionPolicy,
    NativeSnapshotEnvelope,
    NativeTask,
    NodeTaskOutcome,
)


def _task() -> NativeTask:
    return NativeTask("task-1", "node-1", "invoke-1", "activation-1")


def _snapshot() -> NativeSnapshotEnvelope:
    return NativeSnapshotEnvelope(
        "thread-1",
        "",
        "checkpoint-1",
        None,
        "run-1",
        1,
        0,
        {"value": 1},
        (_task(),),
    )


def _info() -> NativeExecutionInfo:
    return NativeExecutionInfo(
        "thread-1",
        "run-1",
        "checkpoint-1",
        "",
        "task-1",
        1,
        None,
        "activation-1",
        "invoke-1",
    )


def test_native_task_and_snapshot_are_detached_and_canonical() -> None:
    task_input = {"nested": [1]}
    task = NativeTask("task-1", "node-1", "invoke-1", "activation-1", input=task_input)
    state = {"nested": [1]}
    snapshot = NativeSnapshotEnvelope(
        "thread-1", "", "checkpoint-1", None, "run-1", 1, 0, state, (task,)
    )
    task_input["nested"].append(2)
    state["nested"].append(2)

    assert task.to_dict()["input"] == {"nested": [1]}
    assert snapshot.to_dict()["state"] == {"nested": [1]}


@pytest.mark.parametrize(
    "build",
    (
        lambda: NativeTask("", "node", "invoke", "activation"),
        lambda: NativeTask("task", "node", "invoke", "activation", join_epoch=-1),
        lambda: NativeTask("task", "node", "invoke", "activation", retry_attempt=0),
        lambda: NativeExecutionPolicy(0),
        lambda: NativeExecutionPolicy(True),
        lambda: NativeSnapshotEnvelope("thread", "", "checkpoint", None, "run", 0, 0, {}, ()),
    ),
)
def test_native_contracts_reject_invalid_identity_or_bounds(build) -> None:
    with pytest.raises((InvalidStatePatch, ValueError)):
        build()


def test_node_outcome_validates_patch_and_identity() -> None:
    identity = NodeExecutionIdentity(
        "workflow",
        "v1",
        "thread-1",
        "run-1",
        "checkpoint-1",
        "",
        "task-1",
        "node-1",
        1,
    )
    outcome = NodeTaskOutcome(_task(), StatePatch({"value": 1}), (), None, identity)
    assert outcome.patch == StatePatch({"value": 1})
    with pytest.raises(TypeError):
        NodeTaskOutcome("task", {}, (), None, _info())  # type: ignore[arg-type]


def test_in_memory_store_is_protocol_and_replays_exact_writes() -> None:
    async def case() -> None:
        store = InMemoryNativeCheckpointStore()
        assert isinstance(store, NativeCheckpointStore)
        snapshot = await store.ensure_genesis(
            operation_id="genesis", snapshot=_snapshot(), configurable={}
        )
        assert snapshot.checkpoint_id == "checkpoint-1"
        await store.commit_task_result(
            operation_id="task-write",
            expected_head="checkpoint-1",
            task=_task(),
            execution_info=_info(),
            patch=StatePatch({"value": 2}),
            blob_refs=(),
            consumed_interrupt_ids=(),
            configurable={},
        )
        await store.commit_task_result(
            operation_id="task-write",
            expected_head="checkpoint-1",
            task=_task(),
            execution_info=_info(),
            patch=StatePatch({"value": 2}),
            blob_refs=(),
            consumed_interrupt_ids=(),
            configurable={},
        )
        execution = await store.load_execution(
            run_id="run-1", thread_id="thread-1", checkpoint_ns=""
        )
        assert execution.pending_results == {"task-1": StatePatch({"value": 2})}

        intent = {
            "intent_id": "terminal",
            "event_key": "run:terminal",
            "event_type": "workflow.final",
            "payload": {"status": "completed"},
        }
        committed = await store.commit_frontier(
            operation_id="frontier-1",
            expected_head="checkpoint-1",
            state={"value": 2},
            frontier=(),
            completed_activations={"node-1": ("activation-1",)},
            join_firings=(),
            consumed_interrupt_ids=(),
            intents=(intent,),
            blob_refs=(),
            terminal_status="completed",
            terminal_error=None,
            recovery_action=None,
            configurable={},
        )
        assert committed.snapshot.step == 1
        assert committed.snapshot.parent_checkpoint_id == "checkpoint-1"
        assert len(committed.materialized_event_ids) == 1
        assert store.pending == {}

    asyncio.run(case())


def test_in_memory_store_rejects_stale_head_and_identity_drift() -> None:
    async def case() -> None:
        store = InMemoryNativeCheckpointStore()
        await store.ensure_genesis(operation_id="genesis", snapshot=_snapshot(), configurable={})
        with pytest.raises(InvalidStatePatch, match="head"):
            await store.commit_task_result(
                operation_id="task-write",
                expected_head="stale",
                task=_task(),
                execution_info=_info(),
                patch={"value": 2},
                blob_refs=(),
                consumed_interrupt_ids=(),
                configurable={},
            )
        with pytest.raises(InvalidStatePatch, match="identity"):
            await store.load_execution(run_id="other", thread_id="thread-1", checkpoint_ns="")

    asyncio.run(case())


def test_in_memory_store_rejects_non_idempotent_pending_write() -> None:
    async def case() -> None:
        store = InMemoryNativeCheckpointStore()
        await store.ensure_genesis(operation_id="genesis", snapshot=_snapshot(), configurable={})
        common = {
            "operation_id": "task-write",
            "expected_head": "checkpoint-1",
            "task": _task(),
            "execution_info": _info(),
            "blob_refs": (),
            "consumed_interrupt_ids": (),
            "configurable": {},
        }
        await store.commit_task_result(patch=StatePatch({"value": 1}), **common)
        with pytest.raises(InvalidStatePatch, match="changed"):
            await store.commit_task_result(patch=StatePatch({"value": 2}), **common)

    asyncio.run(case())


def test_in_memory_route_retry_interrupt_and_failure_contracts() -> None:
    async def case() -> None:
        store = InMemoryNativeCheckpointStore()
        await store.ensure_genesis(operation_id="genesis", snapshot=_snapshot(), configurable={})
        selected = await store.commit_route_selection(
            operation_id="route",
            expected_head="checkpoint-1",
            source="node-1",
            selected_route="continue",
            next_frontier_payload_hash="sha256:frontier",
            task_id="task-1",
            configurable={},
        )
        assert selected["selected_route"] == "continue"
        with pytest.raises(InvalidStatePatch, match="changed"):
            await store.commit_route_selection(
                operation_id="route",
                expected_head="checkpoint-1",
                source="node-1",
                selected_route="stop",
                next_frontier_payload_hash="sha256:frontier",
                task_id="task-1",
                configurable={},
            )

        await store.commit_retry(
            operation_id="retry",
            expected_head="checkpoint-1",
            task=_task(),
            error=RuntimeError("retry"),
            next_attempt_at=10.0,
            configurable={},
        )
        execution = await store.load_execution(
            run_id="run-1", thread_id="thread-1", checkpoint_ns=""
        )
        assert execution.snapshot.frontier[0].retry_attempt == 2
        assert execution.route_selections["task-1"] == selected

        interrupt = {"interrupt_id": "interrupt-1", "task_id": "task-1"}
        await store.commit_interrupt(
            operation_id="interrupt",
            expected_head="checkpoint-1",
            task=_task(),
            interrupt=interrupt,
            configurable={},
        )
        await store.commit_interrupt(
            operation_id="interrupt",
            expected_head="checkpoint-1",
            task=_task(),
            interrupt=interrupt,
            configurable={},
        )
        with pytest.raises(InvalidStatePatch, match="changed"):
            await store.commit_interrupt(
                operation_id="interrupt",
                expected_head="checkpoint-1",
                task=_task(),
                interrupt={"interrupt_id": "interrupt-2", "task_id": "task-1"},
                configurable={},
            )

        await store.commit_failure(
            operation_id="failure",
            expected_head="checkpoint-1",
            task=_task(),
            error=RuntimeError("failed"),
            configurable={},
        )
        await store.commit_engine_failure(
            operation_id="engine-failure",
            expected_head="checkpoint-1",
            frontier=(_task(),),
            error=ValueError("failed"),
            configurable={},
        )
        assert store.failures == {"task-1": "RuntimeError"}

    asyncio.run(case())
