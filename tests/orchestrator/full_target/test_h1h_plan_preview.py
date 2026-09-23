# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Focused H1H-P behavior: pre-admission and pure preview boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_planning_decision_admission import _context, _valid_envelope

from agent_orchestrator.contracts.htn import ReadItem, ReadItemKind
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.planning.decision_adapter import AdapterContext, adapt_for_preview
from agent_orchestrator.planning.decision_admission import (
    NoMutationDecision,
    PreAdmittedPlanningDecision,
    pre_admit_planning_decision,
)
from agent_orchestrator.planning.plan_preview import (
    CandidatePreview,
    PreviewUnavailable,
    preview_candidate,
)


def _adapter_context() -> AdapterContext:
    return AdapterContext(
        mission_id="mission-1",
        base_plan_revision=7,
        proposal_id="pd-" + "1" * 24,
        read_set=(ReadItem(ReadItemKind.TASK, "t-1", 1, "a" * 64),),
    )

@pytest.mark.parametrize("fixture", ("wait", "no-change", "declare-blocked"))
def test_state_free_decisions_stop_before_shape_or_operation_preview(
    monkeypatch, fixture: str
) -> None:
    decision = _valid_envelope(fixture)
    result = pre_admit_planning_decision(decision, context=_context())
    assert isinstance(result, NoMutationDecision)
    assert result.decision_type.value in {"WAIT", "NO_CHANGE", "DECLARE_BLOCKED"}

    def fail(*args, **kwargs):
        raise AssertionError("state-free decision entered candidate preview")

    monkeypatch.setattr(
        "agent_orchestrator.planning.plan_preview.compile_candidate_from_snapshot", fail
    )
    assert adapt_for_preview(result, context=_adapter_context()) is result


def test_refine_is_pre_admitted_then_becomes_a_typed_candidate() -> None:
    decision = _valid_envelope("refine")
    result = pre_admit_planning_decision(decision, context=_context())
    assert isinstance(result, PreAdmittedPlanningDecision)
    proposal = adapt_for_preview(result, context=_adapter_context())
    assert proposal.proposal_id == "pd-" + "1" * 24
    assert len(proposal.operations) == 1


def test_preview_rejects_a_candidate_not_bound_to_the_frozen_input() -> None:
    decision = _valid_envelope("refine")
    result = pre_admit_planning_decision(decision, context=_context())
    proposal = adapt_for_preview(result, context=_adapter_context())
    # Preview inputs are deliberately not assembled with defaults by this seam.
    assert isinstance(
        preview_candidate(proposal, inputs=object()),  # type: ignore[arg-type]
        PreviewUnavailable,
    )


def test_preview_commit_passes_the_same_compilation_without_recompiling(monkeypatch) -> None:
    """H1-H final commit consumes the exact object returned by preview."""

    network = SimpleNamespace(plan_revision=7)
    compilation = SimpleNamespace(network=network)
    preview = object.__new__(CandidatePreview)
    object.__setattr__(preview, "source_snapshot_hash", "snapshot")
    object.__setattr__(preview, "compilation_hash", "compilation")
    object.__setattr__(preview, "compilation", compilation)
    proposal = SimpleNamespace(
        expected_plan_revision=7,
        operations=(),
        proposal_id="proposal-1",
    )
    dispatch = object.__new__(HierarchicalDispatch)
    dispatch.require_hierarchical = lambda mission_id: object()  # type: ignore[attr-defined]
    dispatch.network = lambda mission_id: network  # type: ignore[attr-defined]
    dispatch.commit = SimpleNamespace(commit_plan_revision=lambda command, principal: object())
    seen: list[object] = []

    def build_command(*args, **kwargs):
        seen.append(args[2])
        return object()

    dispatch.build_command = build_command  # type: ignore[method-assign]
    monkeypatch.setattr(
        "agent_orchestrator.planning.plan_preview._source_snapshot_payload",
        lambda value: {"network": "frozen"},
    )
    monkeypatch.setattr(
        "agent_orchestrator.contracts.models.sha256_hex",
        lambda value: "snapshot",
    )
    monkeypatch.setattr(
        dispatch,
        "compile_proposal",
        lambda *args, **kwargs: pytest.fail("preview commit recompiled the candidate"),
    )

    result = dispatch.commit_preview_plan_proposal(
        "mission-1",
        proposal,
        preview=preview,
        principal=SimpleNamespace(),
        command_id="plan:1",
    )

    assert result.committed
    assert seen == [compilation]
