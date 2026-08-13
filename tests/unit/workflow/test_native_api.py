# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import fields

import pytest

from simple_harness.workflow.native import (
    NativeCheckpointStore,
    NativeCommitResult,
    NativeExecution,
    NativeExecutionInfo,
    NativeExecutionPolicy,
    NativeSnapshotEnvelope,
    NativeTask,
    NativeWorkflowExecutable,
    NodeTaskOutcome,
    ProjectionContext,
    TerminalCommitProjectionPort,
    TerminalProjectionDescriptor,
    TerminalProjectionPort,
    TerminalProjectionPrepareReceipt,
)


def test_native_executable_preserves_source_execution_surface() -> None:
    source_methods = {
        "__init__",
        "_config",
        "_entry_task",
        "ainvoke",
        "resume",
        "astream",
        "_drive",
        "_terminal_projection",
        "terminal_intents",
        "_state_blob_refs",
        "_patch_blob_refs",
        "_policy",
        "_identity",
        "_run_task_worker",
        "_run_frontier_tasks",
        "_next_frontier",
    }
    assert source_methods <= set(vars(NativeWorkflowExecutable))


def test_native_constructor_requires_generic_projection_authorities() -> None:
    signature = inspect.signature(NativeWorkflowExecutable)
    assert list(signature.parameters)[:4] == [
        "workflow",
        "store",
        "terminal_projection_port",
        "terminal_commit_projection_port",
    ]
    assert signature.parameters["terminal_projection_port"].default is inspect.Parameter.empty
    assert signature.parameters["terminal_commit_projection_port"].default is inspect.Parameter.empty
    assert signature.parameters["progress_port"].default is None
    assert signature.parameters["observer_port"].default is None


def test_native_disposition_and_projection_contracts_are_real_types() -> None:
    assert inspect.isclass(NativeCheckpointStore)
    for target in (
        NativeCommitResult,
        NativeExecution,
        NativeExecutionInfo,
        NativeExecutionPolicy,
        NativeSnapshotEnvelope,
        NativeTask,
        NodeTaskOutcome,
        ProjectionContext,
        TerminalProjectionDescriptor,
        TerminalProjectionPrepareReceipt,
    ):
        assert inspect.isclass(target)
    assert inspect.isclass(TerminalProjectionPort)
    assert inspect.isclass(TerminalCommitProjectionPort)


def test_native_store_projection_prepare_api_snapshot() -> None:
    assert isinstance(vars(NativeCheckpointStore)["transaction_owner"], property)
    read = inspect.signature(NativeCheckpointStore.read_terminal_projection_prepare)
    assert list(read.parameters) == [
        "self", "operation_id", "expected_head", "configurable"
    ]
    prepare = inspect.signature(NativeCheckpointStore.prepare_terminal_projection)
    assert list(prepare.parameters) == [
        "self",
        "operation_id",
        "expected_head",
        "descriptor_digest",
        "input_hash",
        "output",
        "output_hash",
        "blob_refs",
        "configurable",
    ]
    frontier = inspect.signature(NativeCheckpointStore.commit_frontier)
    assert "terminal_projection_prepare_id" in frontier.parameters


def test_projection_context_is_recursively_immutable_and_detached() -> None:
    source = {"nested": {"items": [{"value": 1}]}}
    context = ProjectionContext(
        "run", "workflow", "1", "checkpoint", source, 10.0, 20.0
    )
    source["nested"] = {}
    nested = context.state_summary["nested"]
    assert isinstance(nested, Mapping)
    items = nested["items"]
    assert isinstance(items, tuple)
    assert items[0]["value"] == 1  # type: ignore[index]
    with pytest.raises(TypeError):
        context.state_summary["new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        nested["new"] = 1  # type: ignore[index]
    names = {field.name for field in fields(context)}
    assert names == {
        "run_id",
        "workflow_name",
        "workflow_version",
        "checkpoint_id",
        "state_summary",
        "logical_timestamp",
        "deadline",
    }
    assert not any(callable(getattr(context, name)) for name in names)
