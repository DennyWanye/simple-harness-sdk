"""P03: production preview refuses DATA, coverage, and resource-invalid plans purely."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_projection_integrity as projection
from test_h1h_p02_compiler_cycles import _snapshot
from test_h1h_preview_compiler_refusal import (
    _capture_production_preview_inputs,
    _with_network,
)

from agent_orchestrator.contracts.htn import ResourceRef, TaskForm
from agent_orchestrator.planning.plan_preview import PreviewUnavailable


def _root(network: Any) -> tuple[Any, Any]:
    occurrence = next(item for item in network.occurrences if item.form is TaskForm.COMPOUND)
    binding = next(item for item in network.task_bindings if item.task_id == occurrence.task_id)
    return occurrence, binding


def _data_multi_binding(inputs: Any):
    network = inputs.network
    root_occurrence, root_binding = _root(network)
    port = projection.port("preview-single")
    altered_root = replace(root_binding, input_ports=(*root_binding.input_ports, port))
    producers = (
        projection.occurrence("preview-producer-o1", "preview-producer-t1"),
        projection.occurrence("preview-producer-o2", "preview-producer-t2"),
    )
    bindings = (
        projection.binding("preview-producer-t1", outputs=(port,)),
        projection.binding("preview-producer-t2", outputs=(port,)),
    )
    requirements = (
        projection.data(
            "preview-data-1",
            "preview-producer-o1",
            port.port_key,
            str(root_occurrence.occurrence_id),
            port.port_key,
        ),
        projection.data(
            "preview-data-2",
            "preview-producer-o2",
            port.port_key,
            str(root_occurrence.occurrence_id),
            port.port_key,
        ),
    )
    return replace(
        network,
        occurrences=(*network.occurrences, *producers),
        task_bindings=(
            *(altered_root if item.task_id == root_binding.task_id else item for item in network.task_bindings),
            *bindings,
        ),
        data_requirements=(*network.data_requirements, *requirements),
        root_occurrence_ids=(
            *network.root_occurrence_ids,
            *(item.occurrence_id for item in producers),
        ),
    )


def _coverage_missing(inputs: Any):
    network = inputs.network
    _occurrence, root_binding = _root(network)
    signature = replace(
        root_binding.goal_signature,
        coverage_criteria=(
            *root_binding.goal_signature.coverage_criteria,
            "preview-required-criterion",
        ),
    )
    altered_root = replace(root_binding, goal_signature=signature)
    return replace(
        network,
        task_bindings=tuple(
            altered_root if item.task_id == root_binding.task_id else item
            for item in network.task_bindings
        ),
    )


def _resource_conflict(inputs: Any):
    network = inputs.network
    resource = ResourceRef(namespace="workspace", object_id="preview-shared-output")
    occurrences = (
        projection.occurrence("preview-writer-o1", "preview-writer-t1"),
        projection.occurrence("preview-writer-o2", "preview-writer-t2"),
    )
    bindings = (
        projection.binding("preview-writer-t1", writes=(resource,)),
        projection.binding("preview-writer-t2", writes=(resource,)),
    )
    return replace(
        network,
        occurrences=(*network.occurrences, *occurrences),
        task_bindings=(*network.task_bindings, *bindings),
        root_occurrence_ids=(
            *network.root_occurrence_ids,
            *(item.occurrence_id for item in occurrences),
        ),
    )


@pytest.mark.parametrize(
    ("case", "mutate", "expected_code"),
    (
        ("data-multi-binding", _data_multi_binding, "DATA_UNBOUND"),
        ("coverage-missing", _coverage_missing, "COVERAGE_GAP"),
        ("resource-conflict", _resource_conflict, "STRUCTURE_INVALID"),
    ),
    ids=("data-multi-binding", "coverage-missing", "resource-conflict"),
)
def test_p03_structural_refusal_preserves_store_budget_events_and_files(
    tmp_path: Path, case: str, mutate: Any, expected_code: str
) -> None:
    async def run() -> None:
        proposal, inputs, dispatch = await _capture_production_preview_inputs(
            tmp_path, key=f"p03-{case}"
        )
        before = _snapshot(tmp_path)
        result = dispatch.preview_plan_proposal(
            proposal, inputs=_with_network(inputs, mutate(inputs))
        )
        assert isinstance(result, PreviewUnavailable)
        assert result.reason == "candidate_rejected"
        assert expected_code in result.mapped_problems
        assert _snapshot(tmp_path) == before

    asyncio.run(run())
