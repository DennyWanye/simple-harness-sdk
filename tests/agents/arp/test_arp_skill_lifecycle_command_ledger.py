# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Skill suspend / resume / retire keep an owner-side ledger by command id (RP-D2 leftover).

Crash window: the owner moved the activation but the Host receipt was never written.
The Host retries the same command with the revision it prepared against; that must
replay through the owner (one transition, one receipt), not die on the moved fence.
"""

from __future__ import annotations

import asyncio

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from skill_fixture import AcceptingAssurance, admit_command, install_command, lifecycle_command, md_bundle, trial_command
from test_arp_runtime_plane import Blobs, _err, _ok, req

from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.api import RuntimePlaneService


def test_crash_window_retries_replay_through_the_owner(tmp_path) -> None:
    async def case() -> None:
        blobs = Blobs()
        runtime = build(tmp_path, ScriptedProvider(["ok"]), acceptance=AcceptingAssurance(), artifacts=blobs)
        async with runtime:
            service = RuntimePlaneService(runtime)
            caller = trusted_caller("host")
            ns = runtime.arp.catalogue.namespace_id
            lifecycle = runtime.arp.lifecycle
            data = md_bundle("writer")
            blobs.put(data)
            command = install_command(runtime, data)
            installed = _ok(await service.handle(req("agent_skill_install", command, subject=ns, command_id="I1", expected_revision=command["expected_catalogue_revision"]), caller=caller))
            revision = cat.resolve_pin(runtime.arp.catalogue.connection, ns, Pin.from_json(installed["definition_ref"]))
            trial = trial_command(runtime, revision)
            binding = _ok(r := await service.handle(req("agent_skill_trial", trial, subject=ns, command_id="T1", expected_revision=trial["expected_activation_revision"]), caller=caller))
            _ok(r := await service.handle(req("agent_skill_admit", admit_command(runtime, revision, binding), subject=ns, command_id="A1", expected_revision=r["view_revision"]), caller=caller))
            acceptance = Pin.from_json(admit_command(runtime, revision, binding)["evaluation_acceptance_ref"])

            # suspend: the owner applied S1, the Host receipt is missing; the retry replays.
            suspend = {"schema_version": 1, "skill_ref": installed["definition_ref"], "reason": "运营暂停"}
            before = r["view_revision"]
            lifecycle.suspend(suspend, caller=caller, command_id="S1")
            suspended = _ok(r := await service.handle(req("agent_skill_suspend", suspend, subject=ns, command_id="S1", expected_revision=before), caller=caller))
            assert suspended["state"] == "SUSPENDED" and r["command_receipt"]["result_revision"] == before + 1

            # resume and retire: the same window.
            resume = lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance)
            before = r["view_revision"]
            lifecycle.transition(resume, caller=caller, command_id="R1")
            resumed = _ok(r := await service.handle(req("agent_skill_resume", resume, subject=ns, command_id="R1", expected_revision=before), caller=caller))
            assert resumed["state"] == "ADMITTED" and r["view_revision"] == before + 1
            retire = lifecycle_command(runtime, revision, "RETIRE", acceptance=acceptance)
            before = r["view_revision"]
            lifecycle.transition(retire, caller=caller, command_id="Z1")
            retired = _ok(await service.handle(req("agent_skill_retire", retire, subject=ns, command_id="Z1", expected_revision=before), caller=caller))
            assert retired["state"] == "RETIRED"

            # An unknown command id against the moved fence is still refused.
            stale = await service.handle(req("agent_skill_suspend", suspend, subject=ns, command_id="S9", expected_revision=before), caller=caller)
            assert _err(stale) == "EXPECTED_REVISION_MISMATCH"

    asyncio.run(case())


def test_a_command_id_names_one_lifecycle_command(tmp_path) -> None:
    async def case() -> None:
        blobs = Blobs()
        runtime = build(tmp_path, ScriptedProvider(["ok"]), acceptance=AcceptingAssurance(), artifacts=blobs)
        async with runtime:
            service = RuntimePlaneService(runtime)
            caller = trusted_caller("host")
            ns = runtime.arp.catalogue.namespace_id
            lifecycle = runtime.arp.lifecycle
            data = md_bundle("writer")
            blobs.put(data)
            command = install_command(runtime, data)
            installed = _ok(await service.handle(req("agent_skill_install", command, subject=ns, command_id="I1", expected_revision=command["expected_catalogue_revision"]), caller=caller))
            revision = cat.resolve_pin(runtime.arp.catalogue.connection, ns, Pin.from_json(installed["definition_ref"]))
            trial = trial_command(runtime, revision)
            _ok(await service.handle(req("agent_skill_trial", trial, subject=ns, command_id="T1", expected_revision=trial["expected_activation_revision"]), caller=caller))
            suspend = {"schema_version": 1, "skill_ref": installed["definition_ref"], "reason": "运营暂停"}
            lifecycle.suspend(suspend, caller=caller, command_id="S1")
            retire = lifecycle_command(runtime, revision, "RETIRE", acceptance=None)
            with pytest.raises(ArpError) as info:
                lifecycle.transition(retire, caller=caller, command_id="S1")  # S1 already suspended this skill
            assert info.value.code == "SOURCE_HASH_CONFLICT"

    asyncio.run(case())
