# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""G5: the matrix may name a hierarchical arm, and the frozen four keep their bytes.

The Host's acceptance runner needs an arm identity for the hierarchical executor so
that an HTN run can be paired against the frozen S/R/D/F baseline.  The arm set was
closed to those four, so an `H` run had nowhere to be declared.

What must stay true is the *identity* of the baseline: a manifest that names only
S/R/D/F must serialise to the same bytes, and therefore hash to the same fingerprint
and generate the same run ids, as it did before `H` existed.  That is why `H` is
appended after the four rather than sorted in, and why `ARMS` still means the four.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_orchestrator.evaluation.experiment import (
    ARM_NAMES,
    ARMS,
    HIERARCHICAL_ARM,
    ArmSpec,
    ExperimentBudget,
    ExperimentManifest,
)

# The bytes and the fingerprint of the frozen four-arm manifest, recorded here so a
# later widening of the arm set cannot move them without this test going red.
LEGACY_FINGERPRINT = "c7f7fc57b16731484ba622a53d93f610b7262fed68d094f9b20e8b75dc61a81d"
LEGACY_FIRST_RUN_ID = "7b2959502ca1b567122fcfc8ff6902e079a41093c923c42451259b3ecd18798e"


def budget() -> ExperimentBudget:
    return ExperimentBudget(
        input_tokens=1000, output_tokens=1000, total_tokens=2000, calls=10, seconds=60.0
    )


def manifest(arms: tuple[str, ...]) -> ExperimentManifest:
    return ExperimentManifest(
        experiment_id="exp-1",
        provider="fake",
        model="fake-model",
        budget=budget(),
        task_ids=("t1", "t2"),
        repetitions=1,
        seed=7,
        physical_slots=1,
        arms=tuple(ArmSpec(arm, f"fake-{arm}-v1") for arm in arms),
    )


def test_the_hierarchical_arm_is_named_after_the_frozen_four() -> None:
    assert ARMS == ("S", "R", "D", "F")
    assert HIERARCHICAL_ARM == "H"
    assert ARM_NAMES == ("S", "R", "D", "F", "H")


def test_an_arm_spec_accepts_the_hierarchical_name() -> None:
    assert ArmSpec("H", "htn-v1").arm == "H"
    with pytest.raises(ValueError, match="arm must be"):
        ArmSpec("X", "x-v1")


def test_a_manifest_may_declare_the_hierarchical_arm() -> None:
    plan = manifest(ARM_NAMES)
    assert tuple(arm.arm for arm in plan.arms) == ARM_NAMES
    assert {run.arm for run in plan.runs()} == set(ARM_NAMES)
    assert len(plan.runs()) == len(plan.task_ids) * len(ARM_NAMES)


def test_the_four_arm_manifest_keeps_its_bytes_and_its_run_ids() -> None:
    plan = manifest(ARMS)
    assert plan.to_dict()["arms"] == [{"arm": arm, "executor_id": f"fake-{arm}-v1"} for arm in ARMS]
    assert plan.fingerprint == LEGACY_FINGERPRINT
    assert [run.arm for run in plan.runs()[:4]] == list(ARMS)
    assert plan.runs()[0].run_id == LEGACY_FIRST_RUN_ID


def test_adding_the_hierarchical_arm_is_a_different_experiment() -> None:
    """A five-arm manifest is not the four-arm one wearing a hat: it hashes apart."""

    assert manifest(ARM_NAMES).fingerprint != manifest(ARMS).fingerprint


@pytest.mark.parametrize(
    "arms",
    [("S", "R", "D"), ("S", "R", "D", "H"), ("H", "S", "R", "D", "F"), ("S", "R", "D", "F", "F")],
)
def test_a_partial_or_reordered_declaration_is_still_refused(arms: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="declare each arm exactly once"):
        manifest(arms)


def test_the_hierarchical_arm_survives_a_replace() -> None:
    plan = manifest(ARM_NAMES)
    again = replace(plan, seed=8)
    assert tuple(arm.arm for arm in again.arms) == ARM_NAMES
