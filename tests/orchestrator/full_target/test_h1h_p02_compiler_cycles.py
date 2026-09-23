"""P02: production preview refuses refinement and ORDER cycles without side effects."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_projection_integrity as projection
from test_h1h_preview_compiler_refusal import (
    _capture_production_preview_inputs,
    _cycle_network,
    _with_network,
)

from agent_orchestrator.contracts.htn import (
    MethodInstanceId,
    Requiredness,
    ReusePolicy,
    TaskForm,
)
from agent_orchestrator.planning.plan_preview import PreviewUnavailable


def _snapshot(root: Path) -> tuple[tuple[str, str], ...]:
    """Byte snapshot of Store, task/plan/event/budget rows, and repository files."""

    return tuple(
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _refinement_cycle(inputs: Any):
    network = inputs.network
    root_occurrence = next(item for item in network.occurrences if item.form is TaskForm.COMPOUND)
    instance_id = MethodInstanceId("preview-refinement-cycle")
    loop = projection.instance(
        str(instance_id),
        str(root_occurrence.task_id),
        str(root_occurrence.occurrence_id),
        (
            (
                "self",
                str(root_occurrence.occurrence_id),
                Requiredness.REQUIRED,
                ReusePolicy.SHARE_ACTIVE,
            ),
        ),
        method="preview.self-refinement",
        obligation=str(root_occurrence.obligation_id),
    )
    # Leave the original root open for the genuine REFINE. The refinement
    # validator checks every candidate configuration, including this alternative.
    return replace(network, method_instances=(*network.method_instances, loop))



@pytest.mark.parametrize(
    ("case", "mutate", "expected_code"),
    (
        ("refinement-cycle", _refinement_cycle, "REFINEMENT_CYCLE"),
        ("order-cycle", _cycle_network, "ORDER_CYCLE"),
    ),
    ids=("refinement-cycle", "order-cycle"),
)
def test_p02_cycle_refusal_preserves_store_budget_events_and_files(
    tmp_path: Path, case: str, mutate: Any, expected_code: str
) -> None:
    async def run() -> None:
        proposal, inputs, dispatch = await _capture_production_preview_inputs(
            tmp_path, key=f"p02-{case}"
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
