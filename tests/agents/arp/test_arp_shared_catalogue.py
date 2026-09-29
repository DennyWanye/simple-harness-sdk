# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""NEXT-TG-1.0 §11: one Skill catalogue authority across the pools of a deployment.

Two real ARP runtimes (an owner pool and a member pool, separate libraries and roots)
share one ``SharedSkillCatalogue``.  A Skill imported and admitted once on the owner is
mirrored into the member with the same skill / revision / hash and the same recorded
acceptance; each pool loads it in its own Session and records its own use; one
suspension on the owner refuses the next use in both pools before any mirror runs; a
restart rebuilds the same view; the member takes no Skill command of its own; an
unadmitted revision and a Skill whose required tool the member lacks stay unusable there.
"""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from skill_fixture import AcceptingAssurance, admit_skill, artifact, import_skill, install_command, lifecycle_command, md_bundle, native_bundle, trial_command

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp import store
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.shared_catalogue import SharedSkillCatalogue, mirror_caller
from simple_harness.agents.arp.strict import digest

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


def _pools(tmp_path, shared: SharedSkillCatalogue, *, member_tools: bool = True):  # type: ignore[no-untyped-def]
    owner = build(tmp_path / "owner", ScriptedProvider(["好的。"] * 8), acceptance=AcceptingAssurance())
    member = build(tmp_path / "member", ScriptedProvider(["好的。"] * 8), catalogue_authority=shared)
    shared.bind_owner("owner", owner)
    shared.bind_member("member", member)
    return owner, member


def _state(runtime, pin) -> str | None:  # type: ignore[no-untyped-def]
    service = runtime.arp.catalogue
    row = cat.read_activation(service.connection, service.namespace_id, "SKILL", pin.id, pin.revision)
    return None if row is None else row.state


async def _session(runtime, key: str):  # type: ignore[no-untyped-def]
    agent = await runtime.create(CONFIG, creation_key=key, caller=trusted_caller())
    receipt = await agent.submit("开始。", input_id=f"{key}:i0")
    await agent.wait_turn(receipt.turn_id, timeout=10)
    return store.read_live_session(runtime.uow.database.connection, agent.run_id)


def _load(runtime, session, pin, call: str):  # type: ignore[no-untyped-def]
    return runtime.arp.skill_use.load(session, call_id=call, request={"skill_ref": pin.to_json()})


def _code(call) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(ArpError) as refused:
        call()
    return refused.value.code


def test_import_once_same_version_in_every_pool_separate_use_one_revocation(tmp_path) -> None:
    async def case() -> None:
        shared = SharedSkillCatalogue(caller=mirror_caller("realm-test"))
        owner, member = _pools(tmp_path, shared)
        async with owner, member:
            revision = import_skill(owner, md_bundle("shared", "记住口令 青鸟。"), command="i1").revision
            admit_skill(owner, revision, command="c1")
            report = shared.sync()
            [row] = report["members"]["member"]["skills"]
            assert row["mirrored"] is True and row["state"] == "ADMITTED"
            mc = member.arp.catalogue
            mirrored = cat.read_revision(mc.connection, mc.namespace_id, "SKILL", revision.entry_id, revision.revision)
            assert mirrored.pin == revision.pin  # same skill / revision / content hash
            owner_row = cat.read_activation(owner.arp.catalogue.connection, owner.arp.catalogue.namespace_id, "SKILL", revision.entry_id, 1)
            member_row = cat.read_activation(mc.connection, mc.namespace_id, "SKILL", revision.entry_id, 1)
            assert member_row.evaluation_ref == owner_row.evaluation_ref
            assert member.arp.lifecycle.admission_for(mirrored, member_row.evaluation_ref) == owner.arp.lifecycle.admission_for(revision, owner_row.evaluation_ref)
            assert shared.sync() == report  # idempotent: nothing moves on a second pass

            # each pool uses it in its own Session and records its own use
            owner_session, member_session = await _session(owner, "k-owner"), await _session(member, "k-member")
            owner_use = _load(owner, owner_session, revision.pin, "call-1")
            member_use = _load(member, member_session, revision.pin, "call-1")
            assert owner_use["skill_ref"] == member_use["skill_ref"] == revision.pin.to_json()
            assert store.read_skill_use(member.uow.database.connection, member_use["use_id"]) == member_use
            assert store.read_skill_use(owner.uow.database.connection, member_use["use_id"]) is None

            # one suspension on the owner refuses the next use in both pools — no mirror yet
            owner.arp.lifecycle.transition(lifecycle_command(owner, revision, "SUSPEND", acceptance=None), caller=trusted_caller(), command_id="s1")
            assert _state(member, revision.pin) == "ADMITTED"  # the mirror lags ...
            with pytest.raises(ArpError) as refused:
                _load(member, member_session, revision.pin, "call-2")
            assert refused.value.code == "SKILL_NOT_ADMITTED" and "SHARED_STATE_SUSPENDED" in refused.value.detail["reasons"]
            assert _code(lambda: _load(owner, owner_session, revision.pin, "call-2")) == "SKILL_NOT_ADMITTED"
            shared.sync()
            assert _state(member, revision.pin) == "SUSPENDED"  # ... and follows on the next pass
            # resumed on the owner under the same official acceptance: usable again everywhere
            acceptance = owner.arp.lifecycle.admission_for(revision, owner_row.evaluation_ref)
            owner.arp.lifecycle.transition(lifecycle_command(owner, revision, "RESUME", acceptance=acceptance), caller=trusted_caller(), command_id="r1")
            shared.sync()
            assert _state(member, revision.pin) == "ADMITTED"
            assert _load(member, member_session, revision.pin, "call-3")["skill_ref"] == revision.pin.to_json()

            # the product entry: reads and writes go to the owner only; other verbs are refused
            namespace = owner.arp.catalogue.namespace_id
            listed = await shared.handle({"schema_version": 1, "verb": "agent_skills_list", "command_id": None,
                                          "subject_id": namespace, "expected_revision": None, "cursor": None, "limit": 10,
                                          "payload_ref": None, "payload": {"namespace_id": namespace, "kind": "SKILL", "cursor": None, "limit": 10}},
                                         caller=trusted_caller())
            assert listed["error"] is None and listed["items"][0]["items"][0]["definition_ref"] == revision.pin.to_json()
            with pytest.raises(ArpError) as refused:
                await shared.handle({"verb": "agent_session_destroy"}, caller=trusted_caller())
            assert refused.value.code == "UNSUPPORTED_HOST_VERB"

    asyncio.run(case())


def test_members_take_no_skill_command_and_unadmitted_revisions_stay_unusable(tmp_path) -> None:
    async def case() -> None:
        shared = SharedSkillCatalogue(caller=mirror_caller("realm-test"))
        owner, member = _pools(tmp_path, shared)
        async with owner, member:
            data = md_bundle("direct")
            assert _code(lambda: member.arp.skills.import_bundle(install_command(member, data), data, caller=trusted_caller(), command_id="x")) == "REF_OUTSIDE_SCOPE"
            first = import_skill(owner, md_bundle("versioned", "第一版。"), command="i1").revision
            admit_skill(owner, first, command="c1")
            second = import_skill(owner, md_bundle("versioned", "第二版。"), command="i2").revision  # QUARANTINED
            shared.sync()
            assert (_state(member, first.pin), _state(member, second.pin)) == ("ADMITTED", "QUARANTINED")
            session = await _session(member, "k")
            assert _code(lambda: _load(member, session, second.pin, "c")) == "SKILL_TRIAL_REQUIRED"
            assert _code(lambda: member.arp.lifecycle.begin_trial(trial_command(member, second), caller=trusted_caller(), command_id="t")) == "REF_OUTSIDE_SCOPE"
            assert _code(lambda: member.arp.lifecycle.transition(
                lifecycle_command(member, first, "SUSPEND", acceptance=None), caller=trusted_caller(), command_id="s")) == "REF_OUTSIDE_SCOPE"
            # a pin the owner does not hold is refused even when a member row claims it
            assert shared.skill_view(Pin("skill", first.entry_id, first.revision, "0" * 64))["present"] is False

    asyncio.run(case())


def test_a_restart_rebuilds_the_same_view(tmp_path) -> None:
    async def case() -> None:
        shared = SharedSkillCatalogue(caller=mirror_caller("realm-test"))
        owner, member = _pools(tmp_path, shared)
        async with owner, member:
            revision = import_skill(owner, md_bundle("kept"), command="i1").revision
            admit_skill(owner, revision, command="c1")
            before = shared.sync()
        owner.uow.database.close()
        member.uow.database.close()
        again = SharedSkillCatalogue(caller=mirror_caller("realm-test"))
        owner, member = _pools(tmp_path, again)
        async with owner, member:
            epoch = member.arp.catalogue.epoch()
            assert again.sync() == before and member.arp.catalogue.epoch() == epoch
            session = await _session(member, "k2")
            assert _load(member, session, revision.pin, "c")["skill_ref"] == revision.pin.to_json()

    asyncio.run(case())


def test_a_skill_whose_required_tool_the_member_lacks_is_not_usable_there(tmp_path) -> None:
    async def case() -> None:
        shared = SharedSkillCatalogue(caller=mirror_caller("realm-test"))
        owner, member = _pools(tmp_path, shared)
        async with owner, member:
            service = owner.arp.catalogue
            sample = cat.latest_revision(service.connection, service.namespace_id, "TOOL", "session_history_read")
            only_owner, _ = service.register("TOOL", {**dict(sample.body), "tool_id": "owner_only_tool", "implementation_digest": digest("owner-only")},
                                             entry_id="owner_only_tool", caller=trusted_caller(), command_id="tool")
            for state in ("TRIAL", "ADMITTED"):
                service.transition(only_owner.pin, state=state, caller=trusted_caller(), command_id=f"tool:{state}")
            bundle = native_bundle(owner, skill_id="needs-tool", implementation={"kind": "INSTRUCTIONS"},
                                   files={"SKILL.md": (b"# needs\n", "INSTRUCTIONS")}, required_tool_refs=[only_owner.pin.to_json()])
            revision = import_skill(owner, bundle, command="i1", fmt="NATIVE").revision
            admit_skill(owner, revision, command="c1")
            [row] = shared.sync()["members"]["member"]["skills"]
            assert row["mirrored"] is False and row["error"] == "DEPENDENCY_UNRESOLVED"
            session = await _session(member, "k")
            assert _code(lambda: _load(member, session, revision.pin, "c")) in {"SKILL_TRIAL_REQUIRED", "DEPENDENCY_UNRESOLVED"}
            owner_session = await _session(owner, "k")
            assert _load(owner, owner_session, revision.pin, "c")["skill_ref"] == revision.pin.to_json()

    asyncio.run(case())


def test_the_product_entry_installs_once_shows_every_pool_and_suspends_everywhere(tmp_path) -> None:
    async def case() -> None:
        shared = SharedSkillCatalogue(caller=mirror_caller("realm-test"))
        bundles: dict[str, bytes] = {}
        owner = build(tmp_path / "owner", ScriptedProvider(["好的。"] * 4), acceptance=AcceptingAssurance(),
                      artifacts=lambda ref: bundles[ref.content_hash])
        member = build(tmp_path / "member", ScriptedProvider(["好的。"] * 4), catalogue_authority=shared)
        shared.bind_owner("owner", owner)
        shared.bind_member("member", member)
        async with owner, member:
            data = md_bundle("entry", "按步骤做。")
            ref = artifact(data)
            bundles[ref.content_hash] = data
            installed = await shared.install(ref, data_format="SKILL_MD", caller=trusted_caller(), command_id="install-1")
            assert installed["error"] is None and installed["mirror"]["members"]["member"]["skills"][0]["state"] == "QUARANTINED"
            again = await shared.install(ref, data_format="SKILL_MD", caller=trusted_caller(), command_id="install-1")
            assert again["error"] is None and again["items"] == installed["items"]  # the same command replays
            [item] = shared.overview()["skills"]
            assert item["state"] == "QUARANTINED" and set(item["pools"]) == {"owner", "member"}
            assert not any(pool["usable"] for pool in item["pools"].values())
            revision = cat.resolve_pin(owner.arp.catalogue.connection, owner.arp.catalogue.namespace_id, Pin.from_json(item["skill_ref"]))
            admit_skill(owner, revision, command="c1")
            shared.sync()
            [item] = shared.overview()["skills"]
            assert item["pools"]["owner"]["usable"] and item["pools"]["member"]["usable"]
            suspended = await shared.lifecycle(revision.pin, "SUSPEND", caller=trusted_caller(), command_id="s1", reason="暂停")
            assert suspended["error"] is None
            [item] = shared.overview()["skills"]
            assert item["state"] == "SUSPENDED" and item["pools"]["member"]["state"] == "SUSPENDED"
            assert not item["pools"]["owner"]["usable"] and not item["pools"]["member"]["usable"]
            resumed = await shared.lifecycle(revision.pin, "RESUME", caller=trusted_caller(), command_id="r1", reason="恢复")
            assert resumed["error"] is None
            [item] = shared.overview()["skills"]
            assert item["pools"]["owner"]["usable"] and item["pools"]["member"]["usable"]

    asyncio.run(case())


def test_a_skill_in_trial_is_usable_only_by_its_own_evaluation_mission_in_every_pool(tmp_path) -> None:
    """NEXT-TG-1.0 §11 admission evaluation: the evaluation Mission really uses the Skill in
    TRIAL (SKILL-CATALOGUE §3); any other Mission, an undispatched or expired trial, and a
    suspension on the owner are refused — in the owner and in a member pool alike."""
    async def case() -> None:
        shared = SharedSkillCatalogue(caller=mirror_caller("realm-test"))
        owner, member = _pools(tmp_path, shared)
        async with owner, member:
            revision = import_skill(owner, md_bundle("trial", "试用口令 白鹭。"), command="i1").revision
            binding = owner.arp.lifecycle.begin_trial(trial_command(owner, revision), caller=trusted_caller(), command_id="t1")
            shared.sync()
            assert _state(member, revision.pin) == "TRIAL"
            owner_session, member_session = await _session(owner, "k-owner"), await _session(member, "k-member")
            missions = {"owner": "mission-eval", "member": "mission-eval"}
            owner.arp.skill_use.session_mission = lambda session: missions["owner"]
            member.arp.skill_use.session_mission = lambda session: missions["member"]
            # not dispatched yet: nobody may use it
            assert _code(lambda: _load(owner, owner_session, revision.pin, "c1")) == "SKILL_NOT_ADMITTED"
            assert _code(lambda: _load(member, member_session, revision.pin, "c1")) == "SKILL_NOT_ADMITTED"
            evaluation = Pin.from_json(binding["evaluation_ref"])
            owner.arp.lifecycle.record_evaluation_dispatch(evaluation, mission_id="mission-eval", task_id="root", caller=trusted_caller(), command_id="d1")
            assert shared.trial_mission_for(revision.pin) == "mission-eval"
            # its own evaluation Mission: usable in both pools, each records its own use
            assert _load(owner, owner_session, revision.pin, "c2")["skill_ref"] == revision.pin.to_json()
            assert _load(member, member_session, revision.pin, "c2")["skill_ref"] == revision.pin.to_json()
            # another Mission, or a Session without a Mission: refused
            missions["member"] = "mission-other"
            assert _code(lambda: _load(member, member_session, revision.pin, "c3")) == "SKILL_NOT_ADMITTED"
            member.arp.skill_use.session_mission = lambda session: None
            assert _code(lambda: _load(member, member_session, revision.pin, "c4")) == "SKILL_NOT_ADMITTED"
            member.arp.skill_use.session_mission = lambda session: missions["member"]
            missions["member"] = "mission-eval"
            # a suspension on the owner refuses the next use everywhere, before any mirror
            owner.arp.lifecycle.transition(lifecycle_command(owner, revision, "SUSPEND", acceptance=None), caller=trusted_caller(), command_id="s1")
            assert _code(lambda: _load(member, member_session, revision.pin, "c5")) == "SKILL_NOT_ADMITTED"
            assert _code(lambda: _load(owner, owner_session, revision.pin, "c5")) == "SKILL_NOT_ADMITTED"

    asyncio.run(case())


def test_an_expired_trial_is_not_usable_even_by_its_evaluation_mission(tmp_path) -> None:
    async def case() -> None:
        now = {"ms": 1_800_000_000_000}
        owner = build(tmp_path / "owner", ScriptedProvider(["好的。"] * 4), acceptance=AcceptingAssurance(), clock_ms=lambda: now["ms"])
        async with owner:
            revision = import_skill(owner, md_bundle("late", "迟到。"), command="i1").revision
            binding = owner.arp.lifecycle.begin_trial(trial_command(owner, revision), caller=trusted_caller(), command_id="t1")
            owner.arp.lifecycle.record_evaluation_dispatch(Pin.from_json(binding["evaluation_ref"]), mission_id="mission-eval", task_id="root",
                                                           caller=trusted_caller(), command_id="d1")
            session = await _session(owner, "k")
            owner.arp.skill_use.session_mission = lambda session: "mission-eval"
            assert _load(owner, session, revision.pin, "c1")["skill_ref"] == revision.pin.to_json()
            now["ms"] = int(binding["expires_at_ms"]) + 1
            assert _code(lambda: _load(owner, session, revision.pin, "c2")) == "SKILL_NOT_ADMITTED"

    asyncio.run(case())
