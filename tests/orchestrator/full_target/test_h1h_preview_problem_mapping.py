"""P10: preview problem mapping is exhaustive and fails closed on drift."""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.graph.projection_validation import (
    ProblemKind,
    ProjectionProblem,
    ProjectionReport,
)
from agent_orchestrator.planning.htn.validation import (
    DeltaProblem,
    DeltaProblemKind,
    DeltaReport,
)
from agent_orchestrator.planning.plan_preview import (
    _DELTA_KIND_TO_CODE,
    _PROJECTION_KIND_TO_CODE,
    map_delta_problems,
)


def _projection(*problems: ProjectionProblem) -> ProjectionReport:
    return ProjectionReport(budget_version=1, problems=problems, topological_order=None)


def _report(*problems: DeltaProblem, projection: ProjectionReport | None = None) -> DeltaReport:
    checked = projection or _projection()
    return DeltaReport(
        problems=problems,
        projection_report=checked,
        refinement_report=checked,
    )


def _projection_key(kind: ProblemKind) -> str:
    return str(kind).split(".")[-1].upper()


def test_p10_mapping_tables_cover_every_current_typed_problem_kind() -> None:
    """Adding an enum member without choosing a wire code is an immediate red test."""

    assert set(_PROJECTION_KIND_TO_CODE) == {_projection_key(kind) for kind in ProblemKind}
    assert set(_DELTA_KIND_TO_CODE) == set(DeltaProblemKind) - {DeltaProblemKind.PROJECTION_DEFECT}


def test_p10_real_delta_report_maps_every_current_delta_and_projection_kind() -> None:
    """Exercise the mapper with real DeltaReport/ProjectionReport values, never a fake OK."""

    projection = _projection(
        *(ProjectionProblem(kind, detail=f"projection:{kind}") for kind in ProblemKind)
    )
    report = _report(
        *(
            DeltaProblem(kind, detail=f"delta:{kind}")
            for kind in DeltaProblemKind
            if kind is not DeltaProblemKind.PROJECTION_DEFECT
        ),
        DeltaProblem(DeltaProblemKind.PROJECTION_DEFECT, detail="projection defects"),
        projection=projection,
    )

    mapped = map_delta_problems(report)
    expected = {
        *_DELTA_KIND_TO_CODE.values(),
        *_PROJECTION_KIND_TO_CODE.values(),
    }
    assert set(mapped) == expected


def test_p10_unmapped_current_delta_is_rejected_instead_of_silently_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future DeltaProblemKind has the same outcome as this removed current mapping."""

    kind = DeltaProblemKind.NOT_REDUCIBLE
    monkeypatch.delitem(_DELTA_KIND_TO_CODE, kind)

    with pytest.raises(ContractError, match="unmapped delta problem"):
        map_delta_problems(_report(DeltaProblem(kind, detail="must be mapped")))


def test_p10_unmapped_current_projection_is_rejected_instead_of_silently_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future Projection ProblemKind has the same outcome as this removed mapping."""

    kind = ProblemKind.CYCLE
    monkeypatch.delitem(_PROJECTION_KIND_TO_CODE, _projection_key(kind))
    report = _report(
        DeltaProblem(DeltaProblemKind.PROJECTION_DEFECT, detail="must be mapped"),
        projection=_projection(ProjectionProblem(kind, detail="must be mapped")),
    )

    with pytest.raises(ContractError, match="unmapped projection problem"):
        map_delta_problems(report)
