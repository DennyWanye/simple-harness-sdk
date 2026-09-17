# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3d / defect D3: the finalizer step's output port is declared, told and enforced.

The Grok acceptance run (H arm, 2026-09-17) lost 10 of 40 episodes here, and every
one of them looked like a success until the last step: the plan committed, all four
leaves ran, ``code_test`` passed, each leaf was accepted — and then

    AcceptanceCommitted{accepted_outputs: []}      ← the finalizer leaf
    HierarchicalRootReviewRejected{c-test-passes: FAIL,
        "no readable proof … evidence.kind=none: no artifact was delivered on a
         declared output port"}

Three readers shared one rule — "a port is declared when a ``DataRequirement``
consumes it" — and the seed method ``code.fix-by-patch`` hangs the root criterion
``c-test-passes`` on the ``verify`` step, whose ``report`` port nothing downstream
consumes.  So the leaf was never told the port existed (no ``declared_output_ports``
section in its context), never wrote ``outputs``, and the accept side had no
declared port left unclaimed to refuse.  The defect surfaced two steps later, in the
one place that cannot act on it.

This file is the invariant that keeps the three readers together: a criterion link
*is* a consumer, because the root's success criterion is what reads that artifact.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from htn_world import method, out, param, step  # noqa: E402
from test_htn_end_to_end import (  # noqa: E402
    World,
    _accept_leaf,
    _Artifact,
    _leaf_task,
    _review_task,
    committed,
)

from agent_orchestrator.contracts.htn import OccurrenceId, TaskForm  # noqa: E402
from agent_orchestrator.orchestrator.accepted_outputs import (  # noqa: E402
    coverage_in_revision,
    criterion_linked_occurrences,
    declared_output_ports,
    output_ports_in_revision,
)
from agent_orchestrator.orchestrator.resolution_commits import (  # noqa: E402
    ResolutionCommitRejected,
)
from agent_orchestrator.runtime.output_blocks import PortClaim  # noqa: E402


@pytest.fixture
def live(tmp_path) -> World:
    return committed(tmp_path, demand=True)


def _occurrence(world: World, task_id: str) -> OccurrenceId:
    return OccurrenceId(world.occurrence_of(task_id))


def _revision(world: World) -> int:
    active = world.semantics.active_plan_revision(world.mission.id)
    assert active is not None
    return int(active.revision)


def _rows_ports(world: World, task_id: str):
    return output_ports_in_revision(
        world.semantics,
        world.mission.id,
        _revision(world),
        _occurrence(world, task_id),
        task_id,
    )


# ======================================================================================
# 1. the rule itself
# ======================================================================================


def test_the_finalizer_step_is_a_criterion_linked_occurrence(live: World) -> None:
    """``_outer`` links ``c-root`` to the ``review`` step, which is also the finalizer."""

    covered = criterion_linked_occurrences(
        coverage_in_revision(live.semantics, live.mission.id, _revision(live))
    )
    assert _occurrence(live, _review_task(live)) in covered
    assert _occurrence(live, _leaf_task(live)) not in covered, (
        "the producing leaf carries no parent criterion; only the finalizer does"
    )


def test_a_criterion_linked_finalizer_declares_the_port_its_contract_names(
    live: World,
) -> None:
    """Defect D3's core.  Nothing consumes ``verdict``; the root criterion reads it."""

    assert set(declared_output_ports(live.network(), _occurrence(live, _review_task(live)))) == {
        "verdict"
    }


def test_the_consumed_port_still_carries_the_edges_schema(live: World) -> None:
    """A criterion link adds ports; it never relabels one a live edge already declares."""

    leaf = _occurrence(live, _leaf_task(live))
    ports = declared_output_ports(live.network(), leaf)
    edge = next(
        item for item in live.network().data_requirements if item.producer_occurrence == leaf
    )
    assert ports["result"].to_json() == edge.schema_ref.to_json()


def test_the_three_readers_give_the_same_answer(live: World) -> None:
    """The network reader, the rows reader and the accept side are one function.

    **Mutation**: teach any one of them the old "consumed only" rule and this goes
    red — which is the shape of the defect, three readers agreeing with each other
    and disagreeing with the root reviewer.
    """

    for task_id in (_leaf_task(live), _review_task(live)):
        network_answer = declared_output_ports(live.network(), _occurrence(live, task_id))
        rows_answer = _rows_ports(live, task_id)
        assert dict(network_answer) == dict(rows_answer), task_id


# ======================================================================================
# 2. told: the leaf's own context package
# ======================================================================================


def test_the_finalizer_leaf_is_told_about_its_declared_output_port(live: World) -> None:
    """The evidence pack's ``verify`` attempt intent had **no** ``declared_output_ports``
    section at all, so the model had no port name to copy into ``outputs``."""

    reported = live.dispatch.declared_output_ports_for(live.mission.id, _review_task(live))
    assert [item["port"] for item in reported] == ["verdict"]
    assert reported[0]["required"] is True


# ======================================================================================
# 3. enforced: an unclaimed finalizer port is refused, not left empty
# ======================================================================================


def test_a_finalizer_that_claims_no_port_is_refused(live: World) -> None:
    """The refusal the ten lost episodes never got.

    Refusing here routes the leaf down the ordinary retry path with a message the
    Worker can act on.  Leaving it empty is what happened instead: the acceptance
    passed silently with ``accepted_outputs: []`` and the root reviewer — correctly —
    rejected a criterion with no readable proof.
    """

    with pytest.raises(ResolutionCommitRejected) as refused:
        _accept_leaf(
            live,
            task_id=_review_task(live),
            result_id="result-review",
            artifacts=(_Artifact("artifact-2", "out/verdict.json"),),
            port_claims=(),
        )
    assert refused.value.reason == "OUTPUT_PORT_UNCLAIMED"
    assert "verdict" in str(refused.value)
    assert live.semantics.list_acceptance_outputs(live.mission.id) == ()


def test_a_finalizer_that_claims_its_port_is_accepted_and_indexed(live: World) -> None:
    receipt = _accept_leaf(
        live,
        task_id=_review_task(live),
        result_id="result-review",
        artifacts=(_Artifact("artifact-2", "out/verdict.json"),),
        port_claims=(PortClaim(port_key="verdict", path="out/verdict.json"),),
    )
    rows = live.semantics.list_acceptance_outputs(live.mission.id)
    assert [(row["output_port"], row["artifact_id"]) for row in rows] == [
        ("verdict", "artifact-2")
    ]
    assert rows[0]["acceptance_id"] == receipt.acceptance_id


# ======================================================================================
# 4. the boundary: neither consumed nor criterion-linked is still "no port"
# ======================================================================================


def _unlinked_method():
    """``leaf → review`` as in ``_outer``, plus an ``audit`` step nothing reads.

    ``audit`` consumes the leaf's ``result`` and declares a ``finding`` port; no edge
    consumes ``finding`` and no ``criterion_link`` names the step.  §24.1 decision 4
    still holds for it — an index entry exists only where something reads it — so the
    widening must not turn every declared port into an obligation.
    """

    return method(
        "plan.outer",
        "plan.goal",
        parameter_schema="plan.goal.params",
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "review",
                "plan.review",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf", "result")},
                capabilities=("plan.read",),
            ),
            step(
                "audit",
                "plan.audit",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf", "result")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "review", "c-reviewed"),),
        finalizer="review",
    )


@pytest.fixture
def three_step(tmp_path) -> World:
    import test_htn_end_to_end as e2e

    original = e2e._outer
    e2e._outer = lambda method_id="plan.outer": _unlinked_method()
    try:
        return committed(tmp_path, demand=True, key="p23d-audit")
    finally:
        e2e._outer = original


def test_a_step_neither_consumed_nor_linked_still_declares_no_port(three_step: World) -> None:
    audit = next(
        str(spec.task_id)
        for spec in three_step.network().occurrences
        if str(
            three_step.network().binding_for_occurrence(spec.occurrence_id).goal_signature.signature_id
        )
        == "plan.audit"
    )
    assert declared_output_ports(three_step.network(), _occurrence(three_step, audit)) == {}
    assert three_step.dispatch.declared_output_ports_for(three_step.mission.id, audit) == ()


# ======================================================================================
# 5. review P2-6 / P2-7 / P2-10: the edges of the widened rule
# ======================================================================================


def _linked_world(tmp_path, *, key: str) -> World:
    """A plan whose criterion link points at a step that is **not** the finalizer.

    ``coverage_from_slots`` resolves a link's ``child_step`` to that slot, so the rule
    was never finalizer-specific — but every fixture in this file, in
    ``test_evidence_saturation`` and in the seed method ``code.fix-by-patch`` happened
    to link the finalizer, so "criterion-linked" and "is the finalizer" were the same
    set and nothing could tell which one the code was reading (review P2-10).

    The linked step is a new task type ``plan.probe`` with three output ports:
    ``result`` (consumed by the review edge), ``note`` (required, consumed by nobody)
    and ``aside`` (optional, consumed by nobody) — which is what P2-6 needs to say
    which of the last two is owed.
    """

    import test_htn_end_to_end as e2e

    original_env, original_outer = e2e._env, e2e._outer

    def env_with_probe(mission: str):
        env = original_env(mission)
        env.register_type(
            "plan.probe",
            parameters=(("subject", "string"),),
            outputs=(
                ("result", "plan.result"),
                ("note", "plan.note"),
                ("aside", "plan.aside", False),
            ),
            capabilities=("plan.read",),
            domain="plan",
        )
        return env

    def outer_linked_to_probe(method_id: str = "plan.outer"):
        return method(
            method_id,
            "plan.goal",
            parameter_schema="plan.goal.params",
            steps=(
                step(
                    "probe",
                    "plan.probe",
                    TaskForm.PRIMITIVE,
                    {"subject": param("subject")},
                    capabilities=("plan.read",),
                ),
                step(
                    "review",
                    "plan.review",
                    TaskForm.PRIMITIVE,
                    {"subject": param("subject"), "result": out("probe", "result")},
                    capabilities=("plan.read",),
                ),
            ),
            links=(("c-root", "probe", "c-done"),),
            finalizer="review",
        )

    e2e._env, e2e._outer = env_with_probe, outer_linked_to_probe
    try:
        return committed(tmp_path, demand=True, key=key)
    finally:
        e2e._env, e2e._outer = original_env, original_outer


def _probe_task(world: World) -> str:
    from test_htn_end_to_end import _task_of

    return _task_of(world, "plan.probe")


@pytest.fixture
def linked(tmp_path) -> World:
    return _linked_world(tmp_path, key="p23d-linked-nonfinal")


def test_a_criterion_link_to_a_non_finalizer_step_declares_that_steps_ports(
    linked: World,
) -> None:
    """The rule is "criterion-linked", not "is the finalizer"."""

    probe, review = _occurrence(linked, _probe_task(linked)), _occurrence(
        linked, _review_task(linked)
    )
    covered = criterion_linked_occurrences(
        coverage_in_revision(linked.semantics, linked.mission.id, _revision(linked))
    )
    assert probe in covered and review not in covered, "the link points at the probe step"
    assert "note" in _rows_ports(linked, _probe_task(linked)), (
        "the step owes its unconsumed required port because the root criterion reads it"
    )
    # The finalizer is not criterion-linked in this plan, so its own unconsumed port is
    # exactly what it was before D3: nobody's.
    assert "verdict" not in _rows_ports(linked, _review_task(linked))


def test_an_optional_port_of_a_criterion_linked_step_is_not_owed(linked: World) -> None:
    """Review P2-6 (mutation M05 survived): which of the two readings this is.

    "Declared" means "owed": ``declared_output_ports_for`` reports every port in the
    set as ``required: True`` and ``OUTPUT_PORT_UNCLAIMED`` refuses a leaf that skipped
    one.  An ``required=False`` port carried into that set would be reported to the
    model as required and enforced as required, which is the opposite of what the
    contract says about it — and carrying it in with ``required=False`` instead would
    mean a port that is announced and never enforced, which is a longer way of saying
    nothing.  So criterion linkage contributes the producer's **required** ports only.

    A port an edge *consumes* is unaffected either way: the consumer's requirement is
    what puts it in the set, and that has been true since before D3.
    """

    ports = _rows_ports(linked, _probe_task(linked))
    assert "note" in ports, "required and criterion-linked: owed"
    assert "aside" not in ports, "optional: the contract does not ask for it"
    assert "result" in ports, "consumed by the review edge, as it always was"


def test_the_network_reader_matches_the_binding_by_occurrence_not_by_position(
    linked: World,
) -> None:
    """Review P2-7: ``zip(task_bindings, occurrences)`` assumed two orders agree.

    ``HierarchicalDispatch.network()`` does build them side by side, but a snapshot
    that came out of ``compile_proposal`` is "the old bindings then the new ones"
    (``compiler.py``), which is not the occurrence order — and the *wrong* binding here
    would declare another step's ports on this one.  ``binding_for_occurrence`` is the
    lookup that cannot be off by a position.
    """

    import inspect

    from agent_orchestrator.orchestrator import accepted_outputs as module

    assert "zip(network.task_bindings" not in inspect.getsource(module.declared_output_ports)
    network = linked.dispatch.network(linked.mission.id)
    probe = _occurrence(linked, _probe_task(linked))
    assert dict(declared_output_ports(network, probe)) == dict(
        _rows_ports(linked, _probe_task(linked))
    ), "the network reader and the row reader still give one answer"

    # And the answer survives a snapshot whose two sequences are in different orders.
    from dataclasses import replace

    shuffled = replace(network, task_bindings=tuple(reversed(network.task_bindings)))
    assert dict(declared_output_ports(shuffled, probe)) == dict(
        declared_output_ports(network, probe)
    )
