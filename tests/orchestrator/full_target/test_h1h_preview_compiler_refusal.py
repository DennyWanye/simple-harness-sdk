# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P02/P03 compiler-refusal preservation through real production PreviewInputs.

The collector is entered only to capture its frozen proposal and PreviewInputs.  Each
case then replaces one frozen input value, recomputes the mandatory source hash, and
calls the production dispatch preview.  No compiler, projection validator, report,
provider, or result is mocked.

Compiler refusals must retain their real typed reports through preview and collector.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_projection_integrity as projection
from test_h1h_preview_purity import _PreviewInputsCaptured
from test_h1i_production_entry import (
    _config,
    _open_planner_round,
    _refine_reply,
    _seed_new_protocol,
)

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.htn import OrderConstraint, PortCardinality, PortSpec, TaskForm
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.plan_preview import (
    PreviewInputs,
    PreviewUnavailable,
    _source_snapshot_payload,
)
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def _with_network(inputs: PreviewInputs, network: Any) -> PreviewInputs:
    """A frozen-input mutation is legal only when its matching source hash is refreshed."""

    return replace(
        inputs,
        network=network,
        source_network_hash=sha256_hex(_source_snapshot_payload(network)),
    )


async def _capture_production_preview_inputs(
    tmp_path: Path, *, key: str
) -> tuple[Any, PreviewInputs, Any]:
    provider = RoleScriptedProvider({"planner": []})
    async with Orchestrator(_config(tmp_path), provider) as loop:
        mission, _env, _contract, dispatch = _seed_new_protocol(loop, tmp_path, key=key)
        opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
        PlanningAuthorizationApi(
            loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
        ).issue(mission.id, command_id=f"{key}:grant", request_id=opener.intent_id)

        captured: dict[str, Any] = {}
        original = dispatch.preview_plan_proposal

        def capture(proposal: Any, *, inputs: Any) -> Any:
            captured.update(proposal=proposal, inputs=inputs)
            raise _PreviewInputsCaptured()

        dispatch.preview_plan_proposal = capture  # type: ignore[method-assign]
        try:
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                _refine_reply(opener.config["planning_package"]),
                dispatch,
            )
        except _PreviewInputsCaptured:
            pass
        finally:
            dispatch.preview_plan_proposal = original  # type: ignore[method-assign]
        assert set(captured) == {"proposal", "inputs"}
        assert isinstance(captured["inputs"], PreviewInputs)
        # preview is pure and needs only the already frozen values, so it remains valid
        # after the store-owning context exits.
        return captured["proposal"], captured["inputs"], dispatch


def _cycle_network(inputs: PreviewInputs):
    """Add two typed primitive occurrences and a real ORDER cycle to the frozen network."""

    left_task, right_task = "preview-cycle-left", "preview-cycle-right"
    left_occurrence, right_occurrence = "preview-cycle-o-left", "preview-cycle-o-right"
    network = inputs.network
    return replace(
        network,
        task_bindings=(
            *network.task_bindings,
            projection.binding(left_task),
            projection.binding(right_task),
        ),
        occurrences=(
            *network.occurrences,
            projection.occurrence(left_occurrence, left_task),
            projection.occurrence(right_occurrence, right_task),
        ),
        order_constraints=(
            *network.order_constraints,
            OrderConstraint(before=left_occurrence, after=right_occurrence),
            OrderConstraint(before=right_occurrence, after=left_occurrence),
        ),
        root_occurrence_ids=(*network.root_occurrence_ids, left_occurrence, right_occurrence),
    )


def _missing_required_port_network(inputs: PreviewInputs):
    """Keep the real root, but give it a typed required input with no DATA producer."""

    network = inputs.network
    root_task_ids = {
        str(occurrence.task_id)
        for occurrence in network.occurrences
        if occurrence.form is TaskForm.COMPOUND
    }
    root = next(
        binding for binding in network.task_bindings if str(binding.task_id) in root_task_ids
    )
    required = PortSpec(
        port_key="preview-required-input",
        schema_ref=root.goal_signature.parameter_schema_ref,
        cardinality=PortCardinality.SINGLE,
        required=True,
    )
    altered = replace(root, input_ports=(*root.input_ports, required))
    return replace(
        network,
        task_bindings=tuple(altered if item is root else item for item in network.task_bindings),
    )


@pytest.mark.parametrize(
    ("case", "mutate", "expected_code"),
    (
        ("order-cycle", _cycle_network, "ORDER_CYCLE"),
        ("unbound-port", _missing_required_port_network, "DATA_UNBOUND"),
    ),
)
def test_p02_p03_real_compiler_refusal_keeps_typed_reason(
    tmp_path: Path, case: str, mutate: Any, expected_code: str
) -> None:
    async def run() -> None:
        proposal, inputs, dispatch = await _capture_production_preview_inputs(
            tmp_path, key=f"p02-p03-{case}"
        )
        result = dispatch.preview_plan_proposal(
            proposal, inputs=_with_network(inputs, mutate(inputs))
        )
        assert isinstance(result, PreviewUnavailable)
        assert result.reason == "candidate_rejected"
        # The report was produced by the real compiler/validator and must survive its
        # refusal, allowing the collector to persist the specific feedback rather than
        # treating every structural case as an untyped internal failure.
        assert expected_code in result.mapped_problems

    asyncio.run(run())


def test_p04_real_projection_bound_refusal_keeps_planning_bound_code(tmp_path: Path) -> None:
    async def run() -> None:
        proposal, inputs, dispatch = await _capture_production_preview_inputs(
            tmp_path, key="p04-preview-bound"
        )
        tiny_budget = replace(inputs.budget, max_nodes=1, max_edges=1, max_fan_out=1)
        result = dispatch.preview_plan_proposal(
            proposal,
            inputs=replace(
                _with_network(inputs, inputs.network),
                budget=tiny_budget,
            ),
        )
        assert isinstance(result, PreviewUnavailable)
        assert result.reason == "candidate_rejected"
        assert "PLANNING_BOUND_REACHED" in result.mapped_problems

    asyncio.run(run())


@pytest.mark.parametrize("unknown_code", (False, True))
def test_real_collector_persists_the_compiler_bound_code(
    tmp_path: Path, unknown_code: bool
) -> None:
    from test_h1h_preview_purity import _tree_hash

    from agent_orchestrator.storage.htn_store import HtnStore
    from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore

    async def run() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="preview-bound-collector"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id="preview-bound-grant", request_id=opener.intent_id)
            original = dispatch.preview_plan_proposal

            def bounded(proposal, *, inputs):
                result = original(
                    proposal,
                    inputs=replace(
                        inputs,
                        budget=replace(inputs.budget, max_nodes=1, max_edges=1, max_fan_out=1),
                    ),
                )

                return (
                    replace(result, mapped_problems=("future_unknown_code",))
                    if unknown_code
                    else result
                )

            dispatch.preview_plan_proposal = bounded
            before_tree = _tree_hash(tmp_path / "repo")
            try:
                await loop._collect_plan_decision(
                    opener,
                    object(),
                    mission,
                    _refine_reply(opener.config["planning_package"]),
                    dispatch,
                )
            finally:
                dispatch.preview_plan_proposal = original
            row = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert row is not None
            assert row["status"] == "REJECTED"
            if unknown_code:
                assert row["rejection_codes"] == ["INTERNAL_CONTRACT_ERROR"]
                evaluated = [
                    e
                    for e in loop.store.list_events(mission.id)
                    if e.type == "PlanningDecisionEvaluated"
                ]
                assert evaluated
                assert evaluated[-1].payload["rejection_codes"] == ["INTERNAL_CONTRACT_ERROR"]
            else:
                assert "PLANNING_BOUND_REACHED" in row["rejection_codes"]
                assert "INTERNAL_CONTRACT_ERROR" not in row["rejection_codes"]
            assert "COVERAGE_GAP" not in row["rejection_codes"]
            assert not HtnStore(loop.store).list_plan_revisions(mission.id)
            assert not loop.store.list_actions(mission.id)
            assert _tree_hash(tmp_path / "repo") == before_tree
            assert provider.calls == 0
            assert not loop.assembled.gateway.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "code",
    (
        "STRUCTURE_INVALID",
        "REFINEMENT_CYCLE",
        "ORDER_CYCLE",
        "DATA_UNBOUND",
        "COVERAGE_GAP",
        "METHOD_INAPPLICABLE",
        "EVIDENCE_REQUIRED",
        "EVIDENCE_CONFLICT",
        "REQUEST_BINDING_STALE",
        "PLANNING_BOUND_REACHED",
        "INTERNAL_CONTRACT_ERROR",
    ),
)
def test_plan_admission_keeps_exact_typed_preview_code(code: str) -> None:
    from test_planning_decision_admission import _context, _valid_envelope

    from agent_orchestrator.planning.decision_admission import (
        PlanShapeView,
        admit_planning_decision,
    )

    feedback = admit_planning_decision(
        _valid_envelope("refine"),
        context=_context(plan_shape=PlanShapeView(mapped_problems=(code,))),
    )
    assert tuple(map(str, feedback.rejection_codes)) == (code,)


@pytest.mark.parametrize("missing", ("registry", "evidence"))
def test_p04_missing_frozen_input_cannot_construct_preview(tmp_path, missing):
    from agent_orchestrator.contracts.models import ContractError
    async def run():
        _, inputs, _ = await _capture_production_preview_inputs(tmp_path, key="p04-missing-" + missing)
        with pytest.raises(ContractError, match="wrong frozen input type"):
            replace(inputs, **{missing: None})
    asyncio.run(run())


def test_p04_absent_validator_returns_source_unavailable(tmp_path, monkeypatch):
    async def run():
        proposal, inputs, dispatch = await _capture_production_preview_inputs(tmp_path, key="p04-no-validator")
        # Dependency fault only: no fabricated validator, compiler or report.
        monkeypatch.setattr("agent_orchestrator.planning.plan_preview.validate_delta", None)
        result = dispatch.preview_plan_proposal(proposal, inputs=inputs)
        assert isinstance(result, PreviewUnavailable)
        assert result.reason == "SOURCE_UNAVAILABLE" and result.delta_report is None
    asyncio.run(run())


def test_p04_real_partial_check_cannot_produce_valid_preview(tmp_path):
    async def run():
        proposal, inputs, dispatch = await _capture_production_preview_inputs(tmp_path, key="p04-partial")
        result = dispatch.preview_plan_proposal(proposal, inputs=_with_network(inputs, _cycle_network(inputs)))
        assert isinstance(result, PreviewUnavailable) and result.reason == "candidate_rejected"
        # The actual projection validator emits CYCLE plus PARTIAL_CHECK because
        # depth/resource checks cannot finish for the cyclic subgraph.
        assert "ORDER_CYCLE" in result.mapped_problems
        assert "INTERNAL_CONTRACT_ERROR" in result.mapped_problems
    asyncio.run(run())
