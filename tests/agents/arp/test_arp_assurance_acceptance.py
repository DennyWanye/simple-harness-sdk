# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""BW09: the real ``SkillAcceptancePort`` over the Assurance 1.1 ledger.

Real components: the assured fixture runtime (real Store / Commit / critic entry /
official record / original acceptance writer, scripted reviewer) mints the ACCEPT
use certificate; the ARP runtime (real catalogue, lifecycle, dispatch link) admits a
Skill only through that certificate.  No test double signs a PASS.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from skill_fixture import activation_of, import_skill, md_bundle, trial_command

from simple_harness.agents.arp.assurance_acceptance import ASSURANCE_1_1, AssuranceSkillAcceptance, evaluation_mission_key
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin

SDK_ROOT = Path(__file__).resolve().parents[3]
ACCEPT_REPLY = {
    "schema_version": 2,
    "verdict": "ACCEPT",
    "assessments": [{"criterion_id": "criterion-report", "verdict": "PASS", "evidence_ids": [], "reason": "fixture", "limitations": []}],
    "findings": [],
}


def _assured_runtime(root):  # type: ignore[no-untyped-def]
    seams = str(SDK_ROOT / "scripts/assurance_seams")
    if seams not in sys.path:
        sys.path.insert(0, seams)
    from _assured_fixture import AssuredRuntime  # noqa: E402  (real fixture runtime)

    return AssuredRuntime(root, [ACCEPT_REPLY])


def _admit(binding: dict, acceptance: Pin, revision) -> dict:  # type: ignore[no-untyped-def]
    return {
        "schema_version": 1, "skill_ref": revision.pin.to_json(), "evaluation_acceptance_ref": acceptance.to_json(),
        "evaluation_policy_ref": binding["policy_ref"], "scope_ref": binding["scope_ref"],
    }


def _bind_mission(store, mission_id: str, evaluation: Pin) -> None:  # type: ignore[no-untyped-def]
    """What the Host does at dispatch: the evaluation Mission is created under the evaluation's
    own idempotency key (the fixture's world was created before the trial, so the key is set here)."""

    key = evaluation_mission_key(evaluation)
    with store.transaction() as connection:
        connection.execute("UPDATE missions SET idempotency_key=?, json=json_set(json,'$.idempotency_key',?) WHERE mission_id=?", (key, key, mission_id))


def _certificate(store):  # type: ignore[no-untyped-def]
    rows = store.connection.execute("SELECT certificate_id, certificate_hash, purpose, consumer_kind FROM assurance_use_certificates").fetchall()
    assert len(rows) == 1 and rows[0][2] == "ACCEPT" and rows[0][3] == "ACCEPTANCE"
    return Pin("acceptance", str(rows[0][0]), 0, str(rows[0][1]))


def test_skill_is_admitted_only_through_the_official_assurance_acceptance(tmp_path) -> None:
    async def case() -> None:
        async with _assured_runtime(tmp_path / "assured") as rt:
            verdict, record = await rt.run_critic()
            assert verdict.passed
            rt.record_critic_layer(record)
            rt.settle_fixture_worker()
            reader = AssuranceSkillAcceptance(
                store=rt.store, clock_ms=lambda: int(rt.store.now * 1000), root_incarnation=rt.commit._assurance_root_gate.require_execution,
            )
            runtime = build(tmp_path / "arp", ScriptedProvider([]), acceptance=reader)
            async with runtime:
                lifecycle = runtime.arp.lifecycle
                assert reader.dispatches is not None  # bound by the runtime factory
                revision = import_skill(runtime, md_bundle("assured-skill"), command="i1").revision
                binding = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t1")
                evaluation = Pin.from_json(binding["evaluation_ref"])
                mission_id, task_id = rt.mission.id, rt.task.id

                # Before the acceptance writer ran there is no certificate at all: a pin that
                # names an absent certificate is refused by name, nothing is admitted.
                absent = Pin("acceptance", "cert-absent", 0, "0" * 64)
                with pytest.raises(ArpError) as no_task:
                    lifecycle.record_evaluation_dispatch(evaluation, mission_id=mission_id, task_id="", caller=trusted_caller(), command_id="d0")
                assert no_task.value.code == "MISSING_FIELD"
                lifecycle.record_evaluation_dispatch(evaluation, mission_id=mission_id, task_id=task_id, caller=trusted_caller(), command_id="d1")
                with pytest.raises(ArpError) as missing:
                    lifecycle.admit(_admit(binding, absent, revision), caller=trusted_caller(), command_id="a0")
                assert missing.value.code == "SKILL_EVALUATION_INCOMPLETE" and missing.value.detail["reason"] == "MISSION_KEY_MISMATCH"
                _bind_mission(rt.store, mission_id, evaluation)
                with pytest.raises(ArpError) as missing:
                    lifecycle.admit(_admit(binding, absent, revision), caller=trusted_caller(), command_id="a0")
                assert missing.value.code == "SKILL_EVALUATION_INCOMPLETE" and missing.value.detail["reason"] == "CERTIFICATE_MISSING"
                assert missing.value.detail["successor"] == ASSURANCE_1_1

                # The original acceptance writer commits Acceptance + certificate + receipt together.
                completed = rt.accept_now()
                assert completed.accepted_result_id == rt.stored.envelope.id
                acceptance = _certificate(rt.store)

                # A pin with the right id but another hash cannot pass as the certificate.
                with pytest.raises(ArpError) as forged:
                    lifecycle.admit(_admit(binding, Pin("acceptance", acceptance.id, 0, "f" * 64), revision), caller=trusted_caller(), command_id="a1")
                assert forged.value.code == "SOURCE_HASH_CONFLICT"
                assert activation_of(runtime, revision).state == "TRIAL"

                # Under the current root, with the dispatch link and the USABLE ACCEPT
                # certificate, the Skill is admitted; the admission records that certificate.
                admitted = lifecycle.admit(_admit(binding, acceptance, revision), caller=trusted_caller(), command_id="a2")
                assert admitted.state == "ADMITTED" and lifecycle.admission_for(revision, evaluation) == acceptance
                view = reader.verify(binding, acceptance)
                assert view["accepted"] is True and view["mission_id"] == mission_id and view["task_id"] == task_id
                assert view["certificate_hash"] == acceptance.content_hash and view["successor"] == ASSURANCE_1_1
                # Re-sent admit: the same admission, no second receipt.
                assert lifecycle.admit(_admit(binding, acceptance, revision), caller=trusted_caller(), command_id="a2").row_version == admitted.row_version

                # A second evaluation cannot borrow the same mission's certificate: the
                # dispatch link is one mission per evaluation.
                other = import_skill(runtime, md_bundle("other-skill"), command="i2").revision
                other_binding = lifecycle.begin_trial(trial_command(runtime, other), caller=trusted_caller(), command_id="t2")
                other_evaluation = Pin.from_json(other_binding["evaluation_ref"])
                with pytest.raises(ArpError) as undispatched:
                    lifecycle.admit(_admit(other_binding, acceptance, other), caller=trusted_caller(), command_id="a3")
                assert undispatched.value.code == "SKILL_EVALUATION_INCOMPLETE" and undispatched.value.detail["reason"] == "EVALUATION_NOT_DISPATCHED"
                with pytest.raises(ArpError) as taken:
                    lifecycle.record_evaluation_dispatch(other_evaluation, mission_id=mission_id, task_id=task_id, caller=trusted_caller(), command_id="d2")
                assert taken.value.code == "SOURCE_HASH_CONFLICT"
                # Dispatched to a mission that does not exist in the ledger: refused by name.
                lifecycle.record_evaluation_dispatch(other_evaluation, mission_id="mission-elsewhere", task_id=task_id, caller=trusted_caller(), command_id="d2")
                with pytest.raises(ArpError) as mismatch:
                    lifecycle.admit(_admit(other_binding, acceptance, other), caller=trusted_caller(), command_id="a3")
                assert mismatch.value.code == "SKILL_EVALUATION_INCOMPLETE" and mismatch.value.detail["reason"] == "MISSION_MISSING"
                assert activation_of(runtime, other).state == "TRIAL"
                # The dispatch link is immutable: re-sent returns it, another mission is refused.
                assert lifecycle.record_evaluation_dispatch(other_evaluation, mission_id="mission-elsewhere", task_id=task_id, caller=trusted_caller(), command_id="d2")["mission_id"] == "mission-elsewhere"
                with pytest.raises(ArpError) as moved:
                    lifecycle.record_evaluation_dispatch(other_evaluation, mission_id="mission-third", task_id=task_id, caller=trusted_caller(), command_id="d3")
                assert moved.value.code == "SOURCE_HASH_CONFLICT"

                # A certificate issued under another root incarnation is refused: a restored
                # root must re-authorize, it does not inherit admissions.
                stale_root = AssuranceSkillAcceptance(store=rt.store, clock_ms=reader.clock_ms, root_incarnation=lambda: "root-restored")
                stale_root.bind_lifecycle(lifecycle)
                with pytest.raises(ArpError) as restored:
                    stale_root.verify(binding, acceptance)
                assert restored.value.detail["reason"] == "ROOT_CHANGED"
                # An expired certificate is refused by the clock.
                late = AssuranceSkillAcceptance(store=rt.store, clock_ms=lambda: 2**53, root_incarnation=None)
                late.bind_lifecycle(lifecycle)
                not_after = rt.store.connection.execute("SELECT not_after_ms FROM assurance_use_certificates").fetchone()[0]
                if not_after is not None:
                    with pytest.raises(ArpError) as expired:
                        late.verify(binding, acceptance)
                    assert expired.value.detail["reason"] == "CERTIFICATE_EXPIRED"
                # An unbound reader cannot answer at all.
                with pytest.raises(ArpError) as unbound:
                    AssuranceSkillAcceptance(store=rt.store, clock_ms=reader.clock_ms).verify(binding, acceptance)
                assert unbound.value.code == "SOURCE_UNAVAILABLE"

    asyncio.run(case())


def test_an_acceptance_issued_before_the_dispatch_was_recorded_cannot_admit(tmp_path) -> None:
    """Review finding (2026-09-24): linking an evaluation to an already-accepted Mission after
    the fact must not promote the Skill, even when the Mission carries the evaluation's key."""

    async def case() -> None:
        async with _assured_runtime(tmp_path / "assured") as rt:
            verdict, record = await rt.run_critic()
            assert verdict.passed
            rt.record_critic_layer(record)
            rt.settle_fixture_worker()
            reader = AssuranceSkillAcceptance(store=rt.store, clock_ms=lambda: int(rt.store.now * 1000))
            runtime = build(tmp_path / "arp", ScriptedProvider([]), acceptance=reader)
            async with runtime:
                lifecycle = runtime.arp.lifecycle
                revision = import_skill(runtime, md_bundle("late-link-skill"), command="i1").revision
                binding = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t1")
                evaluation = Pin.from_json(binding["evaluation_ref"])
                _bind_mission(rt.store, rt.mission.id, evaluation)
                # The acceptance writer runs first; only then does someone record the dispatch.
                rt.accept_now()
                acceptance = _certificate(rt.store)
                lifecycle.record_evaluation_dispatch(evaluation, mission_id=rt.mission.id, task_id=rt.task.id, caller=trusted_caller(), command_id="d1")
                with pytest.raises(ArpError) as late:
                    lifecycle.admit(_admit(binding, acceptance, revision), caller=trusted_caller(), command_id="a1")
                assert late.value.code == "SKILL_EVALUATION_INCOMPLETE" and late.value.detail["reason"] == "ACCEPTANCE_BEFORE_DISPATCH"
                assert activation_of(runtime, revision).state == "TRIAL"

    asyncio.run(case())
