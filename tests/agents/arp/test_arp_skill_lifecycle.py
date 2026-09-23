"""Skill lifecycle (§9.6–9.7): trial under an isolated scope with a complete lock and the
approved policy, immutable per-command binding, admission only through the official
acceptance port (frozen as pending until the Assurance successor lands), suspend /
resume / retire rules."""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from skill_fixture import AcceptingAssurance, acceptance_pin, activation_of, admit_command, admit_skill, import_skill, lifecycle_command, md_bundle, trial_command

from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import ASSURANCE_SUCCESSOR_PENDING


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_trial_binds_an_isolated_scope_and_is_idempotent_per_command(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            lifecycle, service = runtime.arp.lifecycle, runtime.arp.catalogue
            data = md_bundle("trial-skill")
            revision = import_skill(runtime, data, command="i1").revision
            command = trial_command(runtime, revision)
            epoch = service.epoch()
            binding = lifecycle.begin_trial(command, caller=trusted_caller(), command_id="t1")
            activation = activation_of(runtime, revision)
            assert activation.state == "TRIAL" and activation.evaluation_ref == Pin.from_json(binding["evaluation_ref"])
            assert binding["credential_policy"] == "NO_PRODUCTION_CREDENTIALS" and binding["allowed_tool_refs"] == [] and binding["invocation_refs"] == []
            assert binding["lock_ref"] == command["dependency_lock_ref"] and binding["policy_ref"] == command["evaluation_policy_ref"]
            assert binding["scope_ref"]["id"].startswith("skill-trial:trial-skill@1:") and binding["expires_at_ms"] > runtime.arp.ports.clock_ms()
            assert service.epoch() == epoch + 1
            # Re-sent command: the same binding, no second trial, no epoch step.
            assert lifecycle.begin_trial(command, caller=trusted_caller(), command_id="t1") == binding and service.epoch() == epoch + 1
            # Same command id with another body is a conflict.
            other = {**command, "evaluation_policy_ref": Pin("policy", "other", 0, "0" * 64).to_json()}
            with pytest.raises(ArpError) as refused:
                lifecycle.begin_trial(other, caller=trusted_caller(), command_id="t1")
            assert refused.value.code == "SOURCE_HASH_CONFLICT"
            # A new command on a revision already in TRIAL is refused by state, and a
            # stale expected revision by CAS.
            with pytest.raises(ArpError) as stale:
                lifecycle.begin_trial(command, caller=trusted_caller(), command_id="t2")
            assert stale.value.code == "EXPECTED_REVISION_MISMATCH"
            with pytest.raises(ArpError) as state:
                lifecycle.begin_trial({**command, "expected_activation_revision": activation.row_version}, caller=trusted_caller(), command_id="t3")
            assert state.value.code == "STATE_COMBINATION_INVALID"

    _run(case())


def test_trial_refuses_wrong_policy_wrong_lock_and_incomplete_lock(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            lifecycle = runtime.arp.lifecycle
            revision = import_skill(runtime, md_bundle("guarded"), command="i1").revision
            command = trial_command(runtime, revision)
            with pytest.raises(ArpError) as policy:
                lifecycle.begin_trial({**command, "evaluation_policy_ref": Pin("policy", "other", 0, "0" * 64).to_json()}, caller=trusted_caller(), command_id="p1")
            assert policy.value.code == "POLICY_CONFLICT"
            with pytest.raises(ArpError) as lock:
                lifecycle.begin_trial({**command, "dependency_lock_ref": Pin("dependency_lock", "x", 0, "1" * 64).to_json()}, caller=trusted_caller(), command_id="l1")
            assert lock.value.code == "SOURCE_HASH_CONFLICT"
            assert activation_of(runtime, revision).state == "QUARANTINED"
            # A caller that is not a TrustedCaller never reaches the catalogue.
            with pytest.raises(ArpError) as anon:
                lifecycle.begin_trial(command, caller=None, command_id="a1")  # type: ignore[arg-type]
            assert anon.value.code == "AUTHORITY_SOURCE_MISSING"

    _run(case())


def test_admit_is_refused_by_name_while_the_assurance_successor_is_pending(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))  # no acceptance port: the frozen default
        async with runtime:
            lifecycle = runtime.arp.lifecycle
            revision = import_skill(runtime, md_bundle("pending"), command="i1").revision
            binding = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t1")
            with pytest.raises(ArpError) as refused:
                lifecycle.admit(admit_command(runtime, revision, binding), caller=trusted_caller(), command_id="a1")
            assert refused.value.code == "SKILL_EVALUATION_INCOMPLETE"
            assert refused.value.detail["successor"] == ASSURANCE_SUCCESSOR_PENDING
            assert activation_of(runtime, revision).state == "TRIAL"
            # The lifecycle verb form is refused the same way, and never without an acceptance.
            with pytest.raises(ArpError) as none:
                lifecycle.transition(lifecycle_command(runtime, revision, "ADMIT", acceptance=None), caller=trusted_caller(), command_id="a2")
            assert none.value.code == "SKILL_EVALUATION_INCOMPLETE" and none.value.field_path == "$.evaluation_ref"
            with pytest.raises(ArpError) as pending:
                lifecycle.transition(lifecycle_command(runtime, revision, "ADMIT", acceptance=acceptance_pin(binding)), caller=trusted_caller(), command_id="a3")
            assert pending.value.detail["successor"] == ASSURANCE_SUCCESSOR_PENDING

    _run(case())


def test_the_catalogue_itself_never_admits_a_skill_without_a_recorded_acceptance(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]), acceptance=AcceptingAssurance())
        async with runtime:
            lifecycle, service = runtime.arp.lifecycle, runtime.arp.catalogue
            revision = import_skill(runtime, md_bundle("direct"), command="i1").revision
            binding = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t1")
            # A management caller going straight to the catalogue cannot skip the acceptance port.
            with pytest.raises(ArpError) as direct:
                service.transition(revision.pin, state="ADMITTED", caller=trusted_caller(), command_id="bypass-1")
            assert direct.value.code == "SKILL_EVALUATION_INCOMPLETE" and activation_of(runtime, revision).state == "TRIAL"
            with pytest.raises(ArpError) as forged:
                service.transition(revision.pin, state="ADMITTED", caller=trusted_caller(), command_id="bypass-2", admission_ref=acceptance_pin(binding))
            assert forged.value.code == "SKILL_EVALUATION_INCOMPLETE"  # no admission row for it
            lifecycle.admit(admit_command(runtime, revision, binding), caller=trusted_caller(), command_id="a1")
            lifecycle.suspend({"schema_version": 1, "skill_ref": revision.pin.to_json(), "reason": "停"}, caller=trusted_caller(), command_id="s1")
            # Even with the recorded acceptance, a resume must name it (RESUME checks live above).
            with pytest.raises(ArpError) as resumed:
                service.transition(revision.pin, state="ADMITTED", caller=trusted_caller(), command_id="bypass-3")
            assert resumed.value.code == "SKILL_EVALUATION_INCOMPLETE" and activation_of(runtime, revision).state == "SUSPENDED"

    _run(case())


def test_admit_suspend_resume_retire_with_an_accepting_port(tmp_path) -> None:
    async def case() -> None:
        assurance = AcceptingAssurance()
        runtime = build(tmp_path, ScriptedProvider([]), acceptance=assurance)
        async with runtime:
            lifecycle, service = runtime.arp.lifecycle, runtime.arp.catalogue
            revision = import_skill(runtime, md_bundle("lifecycle"), command="i1").revision
            binding = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t1")
            # Wrong acceptance (not for this binding) is refused, TRIAL stays.
            with pytest.raises(ArpError) as wrong:
                lifecycle.admit(admit_command(runtime, revision, binding, tag="forged"), caller=trusted_caller(), command_id="a0")
            assert wrong.value.code == "SKILL_EVALUATION_INCOMPLETE" and activation_of(runtime, revision).state == "TRIAL"
            # Scope / policy must be the binding's own.
            with pytest.raises(ArpError) as scope:
                lifecycle.admit({**admit_command(runtime, revision, binding), "scope_ref": Pin("scope", "elsewhere", 0, "2" * 64).to_json()}, caller=trusted_caller(), command_id="a0b")
            assert scope.value.code == "REF_OUTSIDE_SCOPE"
            admitted = lifecycle.admit(admit_command(runtime, revision, binding), caller=trusted_caller(), command_id="a1")
            assert admitted.state == "ADMITTED" and assurance.calls[-1][1] == acceptance_pin(binding)
            assert service.usable(admitted, now_ms=runtime.arp.ports.clock_ms()) == (True, [])
            # Re-sent admit: same outcome; another acceptance on an admitted skill: conflict.
            assert lifecycle.admit(admit_command(runtime, revision, binding), caller=trusted_caller(), command_id="a1").state == "ADMITTED"
            with pytest.raises(ArpError) as twice:
                lifecycle.admit(admit_command(runtime, revision, binding, tag="second"), caller=trusted_caller(), command_id="a2")
            assert twice.value.code == "STATE_COMBINATION_INVALID"
            # Suspend refuses new use at once (usable=false), resume needs the same acceptance.
            suspended = lifecycle.suspend({"schema_version": 1, "skill_ref": revision.pin.to_json(), "reason": "运营暂停"}, caller=trusted_caller(), command_id="s1")
            assert suspended.state == "SUSPENDED" and service.usable(suspended, now_ms=runtime.arp.ports.clock_ms())[0] is False
            with pytest.raises(ArpError) as other:
                lifecycle.transition(lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance_pin(binding, "other")), caller=trusted_caller(), command_id="r0")
            assert other.value.code == "SKILL_TRIAL_REQUIRED"
            resumed = lifecycle.transition(lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance_pin(binding)), caller=trusted_caller(), command_id="r1")
            assert resumed.state == "ADMITTED" and resumed.evaluation_ref == Pin.from_json(binding["evaluation_ref"])
            # Retire is terminal.
            retired = lifecycle.transition(lifecycle_command(runtime, revision, "RETIRE", acceptance=None), caller=trusted_caller(), command_id="x1")
            assert retired.state == "RETIRED"
            with pytest.raises(ArpError) as dead:
                lifecycle.transition(lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance_pin(binding)), caller=trusted_caller(), command_id="r2")
            assert dead.value.code == "STATE_COMBINATION_INVALID"
            with pytest.raises(ArpError) as dead_trial:
                lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t9")
            assert dead_trial.value.code == "STATE_COMBINATION_INVALID"

    _run(case())


def test_resume_after_an_expired_binding_needs_a_new_trial(tmp_path) -> None:
    async def case() -> None:
        now = {"ms": 1_700_000_000_000}
        assurance = AcceptingAssurance()
        runtime = build(tmp_path, ScriptedProvider([]), acceptance=assurance, clock_ms=lambda: now["ms"])
        async with runtime:
            lifecycle = runtime.arp.lifecycle
            revision = import_skill(runtime, md_bundle("expiring"), command="i1").revision
            binding = admit_skill(runtime, revision, command="c1")
            lifecycle.suspend({"schema_version": 1, "skill_ref": revision.pin.to_json(), "reason": "检查"}, caller=trusted_caller(), command_id="s1")
            now["ms"] = int(binding["expires_at_ms"]) + 1
            with pytest.raises(ArpError) as expired:
                lifecycle.transition(lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance_pin(binding)), caller=trusted_caller(), command_id="r1")
            assert expired.value.code == "SKILL_TRIAL_REQUIRED"
            # A new trial from SUSPENDED is allowed and yields a new binding.
            fresh = lifecycle.begin_trial(trial_command(runtime, revision), caller=trusted_caller(), command_id="t2")
            assert fresh["evaluation_ref"] != binding["evaluation_ref"] and activation_of(runtime, revision).state == "TRIAL"

    _run(case())


def test_lifecycle_verbs_check_cas_and_refuse_trial_without_a_policy(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]), acceptance=AcceptingAssurance())
        async with runtime:
            lifecycle = runtime.arp.lifecycle
            revision = import_skill(runtime, md_bundle("verbs"), command="i1").revision
            with pytest.raises(ArpError) as trial:
                lifecycle.transition(lifecycle_command(runtime, revision, "TRIAL", acceptance=None), caller=trusted_caller(), command_id="v1")
            assert trial.value.code == "MISSING_FIELD"
            binding = admit_skill(runtime, revision, command="c1")
            stale = {**lifecycle_command(runtime, revision, "SUSPEND", acceptance=None), "expected_activation_revision": 1}
            with pytest.raises(ArpError) as cas:
                lifecycle.transition(stale, caller=trusted_caller(), command_id="v2")
            assert cas.value.code == "EXPECTED_REVISION_MISMATCH"
            assert lifecycle.transition(lifecycle_command(runtime, revision, "SUSPEND", acceptance=None), caller=trusted_caller(), command_id="v3").state == "SUSPENDED"
            # Re-sent ADMIT-shaped RESUME with the admitting acceptance replays once admitted.
            lifecycle.transition(lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance_pin(binding)), caller=trusted_caller(), command_id="v4")
            assert lifecycle.transition(lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance_pin(binding)), caller=trusted_caller(), command_id="v4").state == "ADMITTED"

    _run(case())
