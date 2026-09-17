# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3k / defect N4: a read-only leaf does not carry the ``code_test`` layer.

Grok C3-r0 / C3-r1: the ``facts`` and ``reproduce`` leaves — ``side_effect_kind =
external_read``, ``repo.read`` / ``tests.run`` only — each failed their first Attempt
with ``VerificationFailed(code_test: 1 failed, 1 passed)``: the layer ran the whole
suite on a workspace whose baseline is red by construction, and the model then had to
patch ``stats/window.py`` inside a *read-only* leaf to get through.  C1-r1's
``reproduce`` leaf was sent back the same way for the red reproduction test it had
itself written.  Two extra Attempts and roughly a third more tokens per episode, for a
check that can only measure the patch step's work.

``occurrence_policy`` copied the system default (``format_check, rule_check,
code_test``) onto every occurrence.  It now reads the leaf's binding: a leaf whose
type declares a read-only side effect, no write capability and no resource writes is
not given ``code_test`` — unless one of its own criteria names a ``pytest:`` target,
in which case the criterion nobody would check wins, as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import _both_lane_world, _task_of  # noqa: E402

from agent_orchestrator.contracts.htn import (  # noqa: E402
    GoalSignature,
    ObligationId,
    ResourceRef,
    SideEffectKind,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.models import STEP2_IMPLEMENTED_LAYERS  # noqa: E402
from agent_orchestrator.contracts.semantic_base import VersionedRef  # noqa: E402
from agent_orchestrator.orchestrator.occurrence_tasks import (  # noqa: E402
    occurrence_policy,
    read_only_leaf,
)

DEPLOYED = frozenset(STEP2_IMPLEMENTED_LAYERS)


def _binding(
    *,
    side_effect: SideEffectKind | None,
    capabilities: tuple[str, ...] = ("repo.read",),
    writes: tuple[ResourceRef, ...] = (),
) -> TaskSemanticBindingV1:
    return TaskSemanticBindingV1(
        task_id=TaskRef("task-leaf"),
        obligation_id=ObligationId("obl-leaf"),
        contract_revision=1,
        contract_hash="0" * 64,
        form=TaskForm.PRIMITIVE,
        goal_signature=GoalSignature(
            signature_id="code.read-repository-facts",
            version=1,
            parameter_schema_ref=VersionedRef(
                id="code.repository-only", version=1, content_hash="a" * 64
            ),
            output_schema_ref=VersionedRef(id="code.outputs", version=1, content_hash="b" * 64),
            statement="read",
            coverage_criteria=(),
        ),
        operator_ref=VersionedRef(
            id="code.op-read-repository-facts", version=1, content_hash="c" * 64
        ),
        capability_requirements=capabilities,
        resource_writes=writes,
        side_effect_kind=side_effect,
    )


# ======================================================================================
# 1. The rule itself
# ======================================================================================


def test_a_read_only_leaf_is_not_given_code_test() -> None:
    assert "code_test" not in occurrence_policy(("c-facts",), DEPLOYED, read_only=True)
    assert occurrence_policy(("c-facts",), DEPLOYED, read_only=True) == (
        "format_check",
        "rule_check",
    )


def test_a_writing_leaf_keeps_the_system_default() -> None:
    assert occurrence_policy(("c-patch",), DEPLOYED) == ("format_check", "rule_check", "code_test")
    assert occurrence_policy(("c-patch",), DEPLOYED, read_only=False) == (
        "format_check",
        "rule_check",
        "code_test",
    )


def test_a_pytest_criterion_on_a_read_only_leaf_still_runs_code_test() -> None:
    """A criterion nobody would check is worse than a redundant layer (host 0.9.8)."""

    assert "code_test" in occurrence_policy(
        ("pytest:tests/test_x.py",), DEPLOYED, read_only=True
    )


def test_a_declared_policy_is_narrowed_but_not_rewritten_for_a_read_only_leaf() -> None:
    """A deployment that states a policy said what it meant; the rule only drops the
    layer it would otherwise have added by default."""

    assert occurrence_policy(
        ("c-facts",), DEPLOYED, ("format_check", "code_test"), read_only=True
    ) == ("format_check", "code_test")


# ======================================================================================
# 2. What makes a leaf read-only, read off its binding
# ======================================================================================


def test_read_only_is_side_effect_plus_no_write_capability_plus_no_resource_writes() -> None:
    assert read_only_leaf(_binding(side_effect=SideEffectKind.EXTERNAL_READ)) is True
    assert read_only_leaf(_binding(side_effect=SideEffectKind.NONE)) is True
    assert read_only_leaf(_binding(side_effect=SideEffectKind.LOCAL_WRITE)) is False
    assert read_only_leaf(_binding(side_effect=SideEffectKind.EXTERNAL_STATE_WRITE)) is False
    assert (
        read_only_leaf(
            _binding(side_effect=SideEffectKind.EXTERNAL_READ, capabilities=("repo.write",))
        )
        is False
    )
    assert (
        read_only_leaf(
            _binding(
                side_effect=SideEffectKind.EXTERNAL_READ,
                writes=(ResourceRef(namespace="repo", object_id="workspace"),),
            )
        )
        is False
    )


def test_a_binding_that_declares_no_side_effect_is_not_assumed_read_only() -> None:
    """Silence is not a declaration: a leaf whose type said nothing keeps the default."""

    assert read_only_leaf(_binding(side_effect=None)) is False


# ======================================================================================
# 3. The shipped code domain, materialised for real (the C3 plan)
# ======================================================================================


def test_the_c3_plans_read_only_leaves_are_materialised_without_code_test(tmp_path) -> None:
    service, mission, _semantics, _world, dispatch = _both_lane_world(tmp_path)
    tasks = {task.id: task for task in service.store.list_tasks(mission.id)}
    policies = {
        step: tasks[_task_of(dispatch, mission.id, type_id)].verification_policy
        for step, type_id in (
            ("facts", "code.read-repository-facts"),
            ("reproduce", "code.reproduce-failure"),
            ("patch", "code.apply-patch"),
            ("verify", "code.verify-tests"),
        )
    }
    for step in ("facts", "reproduce", "verify"):
        assert "code_test" not in policies[step], (step, policies[step])
        assert policies[step] == ("format_check", "rule_check"), (step, policies[step])
    assert policies["patch"] == ("format_check", "rule_check", "code_test")


def test_the_task_committed_proposal_says_the_same(tmp_path) -> None:
    """The durable record a Worker's verification is later read from (C3's
    ``TaskCommitted.proposal.verification_policy`` carried ``code_test`` on every leaf)."""

    service, mission, _semantics, _world, dispatch = _both_lane_world(tmp_path)
    facts = _task_of(dispatch, mission.id, "code.read-repository-facts")
    patch = _task_of(dispatch, mission.id, "code.apply-patch")
    committed = {
        event.task_id: event.payload["proposal"]["verification_policy"]
        for event in service.store.list_events(mission.id)
        if event.type == "TaskCommitted"
    }
    assert "code_test" not in committed[facts]
    assert "code_test" in committed[patch]
