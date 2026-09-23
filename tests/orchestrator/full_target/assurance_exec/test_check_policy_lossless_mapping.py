# SPDX-License-Identifier: Apache-2.0
"""The lossless per-Scope check-policy mapping a deployment approves (plan §5.1).

Host real-model run 2 (2026-09-23, mission-99f3fee9c19e6299): every content
review failed with CHECK_POLICY_UNRESOLVED because nothing in production approved
the per-Scope check policy. The Host now projects it from the original
requirements through ``lossless_scope_mapping`` and approves it under its own
caller; this pins what that projection is and that the approval it feeds succeeds.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_orchestrator.assurance.checks import CriterionPolicy
from agent_orchestrator.assurance.codec import AssuranceError
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_check_policy import lossless_scope_mapping

SDK_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SDK_ROOT / "scripts/assurance_seams"))


def test_lossless_mapping_spells_out_the_original_and_its_approval_succeeds(tmp_path):
    from _assured_fixture import build_world

    world, task, stored, artifact, scope_ref = build_world(tmp_path / "w", approve_policy=False)
    commit, mission_id = world.service, world.mission.id
    requirements_ref, derived_scope_ref, mapping = lossless_scope_mapping(
        commit, mission_id=mission_id, scope_id=scope_ref.pin.id)
    assert derived_scope_ref == scope_ref
    assert mapping == (CriterionPolicy("criterion-report", "SEMANTIC", ()),)
    assert requirements_ref.kind == "requirements" and requirements_ref.pin.revision == 1
    before = commit.store.connection.execute(
        "SELECT COUNT(*) FROM assurance_criterion_policies WHERE mission_id=?", (mission_id,)).fetchone()[0]
    assert before == 0
    ref = commit.approve_assurance_check_policy(
        tenant_id=world.mission.tenant_id, mission_id=mission_id, command_id="host-check-policy:" + scope_ref.pin.id,
        principal=Principal("host-authenticated-user"), requirements_ref=requirements_ref,
        completion_scope=derived_scope_ref, candidate_mapping=mapping)
    assert ref.kind == "check_policy"
    after = commit.store.connection.execute(
        "SELECT COUNT(*) FROM assurance_criterion_policies WHERE mission_id=?", (mission_id,)).fetchone()[0]
    assert after == 1
    # Replay of the same command is the same approval, not a second policy.
    again = commit.approve_assurance_check_policy(
        tenant_id=world.mission.tenant_id, mission_id=mission_id, command_id="host-check-policy:" + scope_ref.pin.id,
        principal=Principal("host-authenticated-user"), requirements_ref=requirements_ref,
        completion_scope=derived_scope_ref, candidate_mapping=mapping)
    assert again == ref


def test_unknown_scope_is_unresolved_never_invented(tmp_path):
    from _assured_fixture import build_world

    world, *_ = build_world(tmp_path / "w", approve_policy=False)
    try:
        lossless_scope_mapping(world.service, mission_id=world.mission.id, scope_id="op-completion-scope-none")
    except AssuranceError as error:
        assert error.code == "CHECK_POLICY_UNRESOLVED"
    else:
        raise AssertionError("an unknown Scope must be unresolved")
