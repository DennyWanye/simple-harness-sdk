# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""One input manifest from readiness to admission (NEXT-TG-1.0 §5.2).

Readiness judged a consumer against one resolution; ``admissions()`` then resolved
the inputs a second time (without the read's clock) and, when that second answer
had no manifest, substituted an empty one.  Now the admission takes the very
resolution the report was computed from, the report carries the hash of the
manifest it checked, and ``admit_for_dispatch`` refuses any other manifest.  A
consumer that declares no input still gets its explicit empty manifest; a required
port the plan drew no edge for does not.
"""

from __future__ import annotations

import dataclasses

import pytest
from test_htn_end_to_end import (
    World,
    _accept_leaf,
    _leaf_task,
    _review_task,
    _revoke,
    committed,
)

from agent_orchestrator.artifacts.input_bindings import InputManifest, ResolutionProblemKind
from agent_orchestrator.graph.eligibility import NotEligible, ReadinessReason, admit_for_dispatch


@pytest.fixture
def live(tmp_path) -> World:
    return committed(tmp_path, demand=True)


def _occurrence(world: World, task_id: str):
    return next(spec for spec in world.network().occurrences if str(spec.task_id) == task_id)


def _review_ready(world: World) -> None:
    _accept_leaf(world)
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)


def test_a_consumer_with_no_input_is_admitted_with_the_empty_manifest_readiness_checked(live: World) -> None:
    leaf = _leaf_task(live)
    view = live.dispatch.read(live.mission.id)
    occurrence = _occurrence(live, leaf).occurrence_id
    report = view.reports[occurrence]
    assert report.ready
    checked = view.resolutions[occurrence].manifest
    assert checked is not None and checked.bindings == () and checked.is_frozen
    assert report.input_manifest_hash == checked.manifest_hash()
    admitted = live.dispatch.admissions(live.mission.id).readiness[leaf]
    assert admitted.input_manifest_hash == report.input_manifest_hash


def test_the_admission_does_not_resolve_the_inputs_a_second_time(live: World) -> None:
    _review_ready(live)
    calls: list[str] = []
    original = type(live.dispatch).resolved_inputs

    def counting(self, mission_id, network, spec, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(spec.task_id))
        return original(self, mission_id, network, spec, **kwargs)

    type(live.dispatch).resolved_inputs = counting  # type: ignore[method-assign]
    try:
        admissions = live.dispatch.admissions(live.mission.id)
    finally:
        type(live.dispatch).resolved_inputs = original  # type: ignore[method-assign]
    assert _review_task(live) in admissions.readiness
    # once per occurrence, inside read() — never again for the admitted ones
    assert sorted(calls) == sorted(str(spec.task_id) for spec in live.network().occurrences)


def test_a_manifest_other_than_the_checked_one_is_refused(live: World) -> None:
    _review_ready(live)
    review = _review_task(live)
    view = live.dispatch.read(live.mission.id)
    occurrence = _occurrence(live, review).occurrence_id
    report, task_view = view.reports[occurrence], view.views[occurrence]
    checked = view.resolutions[occurrence].manifest
    assert report.ready and checked is not None and checked.bindings
    # the checked manifest itself is admissible ...
    admit_for_dispatch(report, task_view, view.plan, checked, now_ms=1_000_000)
    # ... an empty stand-in for the same consumer is not
    with pytest.raises(NotEligible, match="not the manifest readiness checked"):
        admit_for_dispatch(
            report, task_view, view.plan, InputManifest(consumer_task_ref=checked.consumer_task_ref),
            now_ms=1_000_000,
        )
    # ... and neither is another consumer's manifest
    other = view.resolutions[_occurrence(live, _leaf_task(live)).occurrence_id].manifest
    with pytest.raises(NotEligible):
        admit_for_dispatch(report, task_view, view.plan, other, now_ms=1_000_000)


def test_a_ready_report_without_its_resolution_is_refused_not_filled_with_an_empty_manifest(
    live: World,
) -> None:
    _review_ready(live)
    real = type(live.dispatch).read

    def without_resolutions(self, mission_id, **kwargs):  # type: ignore[no-untyped-def]
        return dataclasses.replace(real(self, mission_id, **kwargs), resolutions={})

    type(live.dispatch).read = without_resolutions  # type: ignore[method-assign]
    try:
        admissions = live.dispatch.admissions(live.mission.id)
    finally:
        type(live.dispatch).read = real  # type: ignore[method-assign]
    assert admissions.readiness == {}
    refusal = admissions.refusal_for(_review_task(live))
    assert refusal is not None and "input_resolution_absent" in refusal.detail_codes


def test_a_required_port_the_plan_drew_no_edge_for_is_not_an_empty_success(live: World) -> None:
    review = _review_task(live)
    network = live.network()
    spec = _occurrence(live, review)
    assert any(port.required for port in network.binding_for_occurrence(spec.occurrence_id).input_ports)
    cut = dataclasses.replace(network, data_requirements=())
    result = live.dispatch.resolved_inputs(live.mission.id, cut, spec)
    assert result.manifest is None
    assert ResolutionProblemKind.UNBOUND_REQUIRED_PORT in result.kinds


def test_a_revoked_producer_after_readiness_leaves_no_admission(live: World) -> None:
    receipt = _accept_leaf(live)
    live.dispatch.issue_input_witnesses(live.mission.id, live.network(), now_ms=1_000_000)
    assert _review_task(live) in live.dispatch.admissions(live.mission.id).readiness
    _revoke(live, str(receipt.acceptance_id))
    live.dispatch.issue_input_witnesses(live.mission.id, live.network(), now_ms=1_100_000)
    refusal = live.dispatch.admissions(live.mission.id).refusal_for(_review_task(live))
    assert refusal is not None and refusal.reason is not ReadinessReason.READY_CANDIDATE
