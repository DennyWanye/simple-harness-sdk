# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-D2: the 23 Host verbs through ``RuntimePlaneService.handle`` (HOST-DTOS §1–§7):
envelope rules, the settings round trip, summaries / manifests before and after the
first request, catalogue and skill verbs (inline and artifact-ref payloads), destroy /
resume / rebuild, management search and read, command receipts and replays."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider
from skill_fixture import AcceptingAssurance, admit_command, artifact, install_command, lifecycle_command, md_bundle, trial_command

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp import store
from simple_harness.agents.arp.codec import check
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.strict import canonical
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.api import RuntimePlaneService

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


def req(verb: str, payload, *, subject: str, command_id: str | None = None, expected_revision: int | None = None, cursor: str | None = None, limit: int = 8, payload_ref=None) -> dict:  # type: ignore[no-untyped-def]
    return {
        "schema_version": 1, "verb": verb, "command_id": command_id, "subject_id": subject, "expected_revision": expected_revision,
        "cursor": cursor, "limit": limit, "payload_ref": payload_ref, "payload": payload,
    }


async def _turn(agent, text: str, input_id: str):  # type: ignore[no-untyped-def]
    receipt = await agent.submit(text, input_id=input_id)
    return await agent.wait_turn(receipt.turn_id, timeout=10)


def _ok(response: dict) -> dict:
    assert response["error"] is None, response["error"]
    return response["items"][0]


def _err(response: dict) -> str:
    assert response["error"] is not None and response["items"] == [] and response["command_receipt"] is None
    return response["error"]["code"]


class Blobs:
    """Test double for the Host's authenticated artifact reader."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put(self, data: bytes) -> Pin:
        pin = artifact(data)
        self.data[pin.id] = data
        return pin

    def put_json(self, value) -> Pin:  # type: ignore[no-untyped-def]
        return self.put(canonical(value))

    def __call__(self, pin: Pin) -> bytes:
        return self.data[pin.id]


def test_settings_round_trip_changes_only_later_requests(tmp_path) -> None:
    async def case() -> None:
        blobs = Blobs()
        runtime = build(tmp_path, ScriptedProvider(["记住了。", "好的。", "再好。", "又好。"]), artifacts=blobs)
        async with runtime:
            service = RuntimePlaneService(runtime)
            caller = trusted_caller("host")
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            a = agent.agent_id
            # 1. GET before the first model request: adoption 1, profile policy, no context yet.
            got = _ok(await service.handle(req("agent_context_settings_get", {"session_id": session.session_id}, subject=a), caller=caller))
            assert got["adoption_revision"] == 1 and got["view_revision"] == 1 and got["last_context_ref"] is None
            p1 = got["effective_policy_ref"]
            assert got["configured_policy"]["policy_id"] == p1["id"] and got["next_request_only"] is True
            summary = _ok(await service.handle(req("agent_context_summary", {"session_id": session.session_id, "context_id": None, "cursor": None, "limit": 8}, subject=a), caller=caller))
            assert summary["context_id"] is None and summary["retrieval_status"] is None and summary["configured_context_tokens"] == got["configured_policy"]["max_context_tokens"]
            assert (await _turn(agent, "你好。", "i0")).state is AgentTurnState.COMMITTED
            # 2. Submit a new policy body as the next revision of the same policy id.
            policy = dict(got["configured_policy"])
            policy["recall_max_tokens"] = policy["recall_max_tokens"] + 8
            submit = req("agent_context_policy_submit", {"schema_version": 1, "policy": policy, "expected_policy_ref": p1}, subject=a, command_id="C1", expected_revision=0)
            submitted = _ok(response := await service.handle(submit, caller=caller))
            p2 = submitted["policy_ref"]
            assert p2["revision"] == p1["revision"] + 1 and response["command_receipt"]["command_id"] == "C1" and response["view_revision"] == p2["revision"]
            assert submitted["command_receipt"] == response["command_receipt"] and submitted["approval_ref"]["kind"] == "authority"
            fetched = _ok(await service.handle(req("agent_context_policy_get", {"policy_ref": p2}, subject=a), caller=caller))
            assert fetched["recall_max_tokens"] == policy["recall_max_tokens"]
            # The same submit command replays with its original receipt and result (no third revision).
            again = await service.handle(submit, caller=caller)
            assert again["error"] is None and again["items"][0] == submitted and again["command_receipt"] == response["command_receipt"]
            assert store.latest_policy_revision(runtime.uow.database.connection, p1["id"]) == p2["revision"]
            # 3. Update: envelope.expected_revision must pair with expected_adoption_revision.
            update = {"schema_version": 1, "session_id": session.session_id, "candidate_policy_ref": p2, "expected_effective_policy_ref": p1, "expected_adoption_revision": 1}
            bad = await service.handle(req("agent_context_settings_update", update, subject=a, command_id="C2", expected_revision=2), caller=caller)
            assert _err(bad) == "EXPECTED_REVISION_MISMATCH"
            updated = _ok(response := await service.handle(req("agent_context_settings_update", update, subject=a, command_id="C2", expected_revision=1), caller=caller))
            assert updated["adoption_revision"] == 2 and updated["effective_policy_ref"] == p2 and response["view_revision"] == 2
            receipt = response["command_receipt"]
            assert receipt["outcome"] == "APPLIED" and receipt["result_revision"] == 2 and receipt["subject_session_id"] == session.session_id
            # 4. A stale update (adoption already 2) is refused without a receipt.
            stale = await service.handle(req("agent_context_settings_update", update, subject=a, command_id="C4", expected_revision=1), caller=caller)
            assert _err(stale) == "POLICY_CONFLICT"
            # The next request is frozen under P2 / adoption 2; the earlier one keeps P1.
            assert (await _turn(agent, "再来。", "i1")).state is AgentTurnState.COMMITTED
            latest = store.latest_context(runtime.uow.database.connection, session.session_id)
            assert latest.manifest["effective_context_policy_ref"] == p2 and latest.manifest["adoption_revision"] == 2
            history = _ok(await service.handle(req("agent_context_history", {"session_id": session.session_id, "context_id": None, "cursor": None, "limit": 8}, subject=a), caller=caller))
            assert [h["adoption_revision"] for h in history["items"]] == [1, 2] and history["has_more"] is False
            # 5. Receipt after a disconnect, and the exact replay of C2 (original result, not today's).
            got_receipt = _ok(await service.handle(req("agent_command_receipt_get", {"command_id": "C2"}, subject=a), caller=caller))
            assert got_receipt == receipt
            replay = await service.handle(req("agent_context_settings_update", update, subject=a, command_id="C2", expected_revision=1), caller=caller)
            assert replay["command_receipt"] == receipt and replay["items"][0] == updated
            other = dict(update, expected_adoption_revision=2)
            conflict = await service.handle(req("agent_context_settings_update", other, subject=a, command_id="C2", expected_revision=2), caller=caller)
            assert _err(conflict) == "EXPECTED_REVISION_MISMATCH"
            missing = await service.handle(req("agent_command_receipt_get", {"command_id": "C4"}, subject=a), caller=caller)
            assert _err(missing) == "SOURCE_UNAVAILABLE"
            # Another subject cannot probe the receipt by command id; it reads as absent.
            probe = await service.handle(req("agent_command_receipt_get", {"command_id": "C2"}, subject="agent:other"), caller=caller)
            assert _err(probe) == "SOURCE_UNAVAILABLE"
            # The creation's own adoption command id cannot be hijacked with a stale body.
            hijack = dict(update, expected_adoption_revision=7)
            forged = await service.handle(req("agent_context_settings_update", hijack, subject=a, command_id=f"{session.session_id}:adopt:1", expected_revision=7), caller=caller)
            assert _err(forged) == "EXPECTED_REVISION_MISMATCH"
            assert [r[0] for r in runtime.uow.database.connection.execute("SELECT adoption_revision FROM arp_context_policy_adoptions WHERE session_id=? ORDER BY 1", (session.session_id,))] == [1, 2]
            # An artifact-ref settings update obeys the same revision pairing as inline.
            policy3 = dict(policy)
            policy3["recall_max_tokens"] += 8
            p3 = _ok(await service.handle(req("agent_context_policy_submit", {"schema_version": 1, "policy": policy3, "expected_policy_ref": p2}, subject=a, command_id="C6", expected_revision=0), caller=caller))["policy_ref"]
            update3 = {"schema_version": 1, "session_id": session.session_id, "candidate_policy_ref": p3, "expected_effective_policy_ref": p2, "expected_adoption_revision": 2}
            ref = blobs.put_json(update3).to_json()
            unpaired = await service.handle(req("agent_context_settings_update", None, subject=a, command_id="C7", expected_revision=999, payload_ref=ref), caller=caller)
            assert _err(unpaired) == "EXPECTED_REVISION_MISMATCH"
            third = _ok(await service.handle(req("agent_context_settings_update", None, subject=a, command_id="C7", expected_revision=2, payload_ref=ref), caller=caller))
            assert third["adoption_revision"] == 3 and third["effective_policy_ref"] == p3
            # Replaying C2 after adoption 3 still returns the adoption-2 view it produced.
            old_view = await service.handle(req("agent_context_settings_update", update, subject=a, command_id="C2", expected_revision=1), caller=caller)
            assert old_view["items"][0]["adoption_revision"] == 2 and old_view["command_receipt"] == receipt
            # Search after the update runs under the adopted policy, not the frozen profile policy.
            assert (await _turn(agent, "蓝鲸 是 暗号。", "i2")).state is AgentTurnState.COMMITTED
            page = _ok(await service.handle(req("agent_session_history_search", {"schema_version": 1, "query": "蓝鲸", "cursor": None, "limit": 4, "max_bytes": 8192}, subject=a, limit=4), caller=caller))
            assert page["receipt"]["policy_ref"] == p3
            # Another subject cannot read this session's settings.
            foreign = await service.handle(req("agent_context_settings_get", {"session_id": session.session_id}, subject="agent:other"), caller=caller)
            assert _err(foreign) == "SESSION_IDENTITY_MISMATCH"

    asyncio.run(case())


def test_summary_manifest_pages_and_envelope_rules(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。", "好的。"]))
        async with runtime:
            service = RuntimePlaneService(runtime)
            caller = trusted_caller("host")
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            a = agent.agent_id
            session = store.read_live_session(runtime.uow.database.connection, a)
            assert (await _turn(agent, "蓝鲸 是 暗号。", "i0")).state is AgentTurnState.COMMITTED
            assert (await _turn(agent, "再说一次。", "i1")).state is AgentTurnState.COMMITTED
            read = {"session_id": session.session_id, "context_id": None, "cursor": None, "limit": 8}
            response = await service.handle(req("agent_context_summary", read, subject=a), caller=caller)
            summary = _ok(response)
            latest = store.latest_context(runtime.uow.database.connection, session.session_id)
            assert summary["context_id"] == latest.context_id and summary["historical"] is False and summary["manifest_ref"] == latest.pin.to_json()
            assert summary["retrieval_status"] is not None and summary["session_state"] == "ACTIVE" and summary["current_access"] == "ALLOWED"
            assert response["as_of_context_id"] == latest.context_id and response["view_revision"] >= 1
            # The first context is historical when read by id.
            first = runtime.uow.database.connection.execute("SELECT context_id FROM arp_context_requests ORDER BY created_at_ms, provider_request_ordinal LIMIT 1").fetchone()[0]
            old = _ok(await service.handle(req("agent_context_summary", dict(read, context_id=first), subject=a), caller=caller))
            assert old["historical"] is (first != latest.context_id) and old["context_id"] == first
            # Manifest paging walks HEADER → SECTIONS → RECENT_GROUPS → RECALL → AUTHORITIES with a bound cursor.
            fields = []
            cursor = None
            for _ in range(20):
                page = _ok(r := await service.handle(req("agent_context_manifest", dict(read, cursor=cursor, limit=2), subject=a, cursor=cursor, limit=2), caller=caller))
                assert r["next_cursor"] == page["next_cursor"] and page["manifest_hash"] == latest.manifest_hash
                fields.append(page["field"])
                if page["next_cursor"] is None:
                    break
                cursor = page["next_cursor"]
            assert fields[0] == "HEADER" and fields[-1] == "AUTHORITIES" and "SECTIONS" in fields
            bad_cursor = await service.handle(req("agent_context_manifest", dict(read, cursor="0000000000000000:SECTIONS:0", limit=2), subject=a, cursor="0000000000000000:SECTIONS:0", limit=2), caller=caller)
            assert _err(bad_cursor) == "CURSOR_UNKNOWN"
            # Envelope rules.
            two = req("agent_context_summary", read, subject=a, payload_ref=Pin("artifact", "x", 0, "a" * 64).to_json())
            assert _err(await service.handle(two, caller=caller)) == "HOST_PAYLOAD_MISMATCH"
            echo = req("agent_context_manifest", dict(read, cursor=None, limit=3), subject=a, limit=4)
            assert _err(await service.handle(echo, caller=caller)) == "CURSOR_REQUEST_MISMATCH"
            unknown = dict(req("agent_context_summary", read, subject=a), verb="agent_nope")
            assert _err(await service.handle(unknown, caller=caller)) == "UNSUPPORTED_HOST_VERB"
            assert _err(await service.handle(req("agent_context_summary", read, subject=a), caller=None)) == "AUTHORITY_SOURCE_MISSING"  # type: ignore[arg-type]
            no_command = req("agent_session_destroy", {"schema_version": 1, "session_id": session.session_id, "expected_generation": 1, "command_id": "d", "reason": "USER_DESTROY", "retention_policy_ref": runtime.arp.ports.profile.refs.retention_policy_ref.to_json()}, subject=a, expected_revision=session.row_version)
            assert _err(await service.handle(no_command, caller=caller)) == "MISSING_FIELD"
            # Management search and read use their own purposes on the same engine.
            search = {"schema_version": 1, "query": "蓝鲸", "cursor": None, "limit": 4, "max_bytes": 8192}
            page = _ok(r := await service.handle(req("agent_session_history_search", search, subject=a, limit=4), caller=caller))
            assert page["cursor_purpose"] == "MANAGEMENT_SEARCH" and r["view_revision"] == page["receipt"]["index_upper_commit"]
            read_page = _ok(r := await service.handle(req("agent_session_history_read", {"schema_version": 1, "seq_from": 1, "seq_to": 4, "cursor": None, "max_bytes": 8192}, subject=a), caller=caller))
            assert read_page["cursor_purpose"] == "MANAGEMENT_READ" and r["view_revision"] == read_page["journal_highwater"] and read_page["items"]

    asyncio.run(case())


def test_catalogue_and_skill_verbs_inline_and_by_artifact_ref(tmp_path) -> None:
    async def case() -> None:
        blobs = Blobs()
        runtime = build(tmp_path, ScriptedProvider(["ok"]), acceptance=AcceptingAssurance(), artifacts=blobs)
        async with runtime:
            service = RuntimePlaneService(runtime)
            caller = trusted_caller("host")
            ns = runtime.arp.catalogue.namespace_id
            listing = {"namespace_id": ns, "kind": "TOOL", "cursor": None, "limit": 8}
            tools = _ok(r := await service.handle(req("agent_tool_catalogue", listing, subject=ns), caller=caller))
            assert tools["access_view"] == "MANAGEMENT" and tools["items"] and r["view_revision"] == tools["registry_epoch"]
            assert _err(await service.handle(req("agent_tool_catalogue", listing, subject="someone-else"), caller=caller)) == "REF_OUTSIDE_SCOPE"
            assert _err(await service.handle(req("agent_skills_list", listing, subject=ns), caller=caller)) == "CATALOGUE_KIND_MISMATCH"
            caps = _ok(await service.handle(req("agent_capabilities_list", dict(listing, kind="CAPABILITY"), subject=ns), caller=caller))
            assert caps["items"]
            # Install: the bundle bytes come from the authenticated artifact reader, the command itself by artifact ref.
            data = md_bundle("writer")
            blobs.put(data)
            command = install_command(runtime, data)
            command_ref = blobs.put_json(command)
            installed = _ok(r := await service.handle(req("agent_skill_install", None, subject=ns, command_id="I1", expected_revision=command["expected_catalogue_revision"], payload_ref=command_ref.to_json()), caller=caller))
            assert installed["kind"] == "SKILL" and installed["state"] == "QUARANTINED" and r["command_receipt"]["result_ref"] == installed["definition_ref"]
            wrong = await service.handle(req("agent_skill_install", command, subject=ns, command_id="I2", expected_revision=command["expected_catalogue_revision"] + 7), caller=caller)
            assert _err(wrong) == "EXPECTED_REVISION_MISMATCH"
            skills = _ok(await service.handle(req("agent_skills_list", dict(listing, kind="SKILL"), subject=ns), caller=caller))
            assert [i["definition_ref"] for i in skills["items"]] == [installed["definition_ref"]]
            details = _ok(await service.handle(req("agent_skill_details", {"skill_ref": installed["definition_ref"], "cursor": None, "limit": 8}, subject=ns), caller=caller))
            assert details["skill_ref"] == installed["definition_ref"] and details["files"]
            revision = cat.resolve_pin(runtime.arp.catalogue.connection, ns, Pin.from_json(installed["definition_ref"]))
            # Trial → admit → suspend → resume → retire, every write fenced by the activation revision.
            trial = trial_command(runtime, revision)
            stale = await service.handle(req("agent_skill_trial", trial, subject=ns, command_id="T1", expected_revision=trial["expected_activation_revision"] + 1), caller=caller)
            assert _err(stale) == "EXPECTED_REVISION_MISMATCH"
            binding = _ok(r := await service.handle(req("agent_skill_trial", trial, subject=ns, command_id="T1", expected_revision=trial["expected_activation_revision"]), caller=caller))
            assert binding["skill_ref"] == installed["definition_ref"] and r["view_revision"] == installed["state_revision"] + 1
            replay = await service.handle(req("agent_skill_trial", trial, subject=ns, command_id="T1", expected_revision=trial["expected_activation_revision"]), caller=caller)
            assert replay["items"][0] == binding and replay["command_receipt"] == r["command_receipt"]
            admitted = _ok(r := await service.handle(req("agent_skill_admit", admit_command(runtime, revision, binding), subject=ns, command_id="A1", expected_revision=r["view_revision"]), caller=caller))
            assert admitted["state"] == "ADMITTED" and admitted["current_usable"] is True
            suspended = _ok(r := await service.handle(req("agent_skill_suspend", {"schema_version": 1, "skill_ref": installed["definition_ref"], "reason": "运营暂停"}, subject=ns, command_id="S1", expected_revision=r["view_revision"]), caller=caller))
            assert suspended["state"] == "SUSPENDED" and suspended["current_usable"] is False
            acceptance = Pin.from_json(admit_command(runtime, revision, binding)["evaluation_acceptance_ref"])
            wrong_action = await service.handle(req("agent_skill_retire", lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance), subject=ns, command_id="X1", expected_revision=r["view_revision"]), caller=caller)
            assert _err(wrong_action) == "ENUM"
            resumed = _ok(r := await service.handle(req("agent_skill_resume", lifecycle_command(runtime, revision, "RESUME", acceptance=acceptance), subject=ns, command_id="R1", expected_revision=r["view_revision"]), caller=caller))
            assert resumed["state"] == "ADMITTED"
            retired = _ok(r := await service.handle(req("agent_skill_retire", lifecycle_command(runtime, revision, "RETIRE", acceptance=acceptance), subject=ns, command_id="Z1", expected_revision=r["view_revision"]), caller=caller))
            assert retired["state"] == "RETIRED" and r["command_receipt"]["result_revision"] == retired["state_revision"]
            got = _ok(await service.handle(req("agent_command_receipt_get", {"command_id": "Z1"}, subject=ns), caller=caller))
            assert got == r["command_receipt"]

    asyncio.run(case())


def test_destroy_resume_and_rebuild_through_the_envelope(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            service = RuntimePlaneService(runtime)
            caller = trusted_caller("host")
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            a = agent.agent_id
            assert (await _turn(agent, "你好。", "i0")).state is AgentTurnState.COMMITTED
            session = store.read_live_session(runtime.uow.database.connection, a)
            delete = {"schema_version": 1, "session_id": session.session_id, "expected_generation": session.generation, "command_id": "D1", "reason": "USER_DESTROY", "retention_policy_ref": runtime.arp.ports.profile.refs.retention_policy_ref.to_json()}
            stale = await service.handle(req("agent_session_destroy", delete, subject=a, command_id="D1", expected_revision=session.row_version + 1), caller=caller)
            assert _err(stale) == "EXPECTED_REVISION_MISMATCH"
            view = _ok(r := await service.handle(req("agent_session_destroy", delete, subject=a, command_id="D1", expected_revision=session.row_version), caller=caller))
            assert view["state"] == "PURGING" and view["purge_progress"]["destroy_command_id"] == "D1" and r["view_revision"] == view["row_version"]
            assert r["command_receipt"]["result_ref"]["kind"] == "session" and r["command_receipt"]["subject_agent_id"] == a
            rebuild = {"schema_version": 1, "session_id": session.session_id, "expected_generation": view["generation"], "old_index_generation": 1, "embedding_deployment_ref": Pin("deployment", "embedding:none", 0, "f" * 64).to_json(), "chunker_ref": Pin("policy", "chunker", 1, "a" * 64).to_json(), "view_policy_ref": Pin("policy", "view", 1, "b" * 64).to_json(), "reason": "x"}
            refused = await service.handle(req("agent_session_rebuild", rebuild, subject=a, command_id="RB1", expected_revision=view["row_version"]), caller=caller)
            assert _err(refused) == "SESSION_PURGED"
            for _ in range(6):
                runtime.arp.tick()
            purged = store.read_session(runtime.uow.database.connection, session.session_id)
            assert purged.state == "PURGED"
            # The replay returns the original PURGING view and receipt, not today's PURGED row.
            replay = await service.handle(req("agent_session_destroy", delete, subject=a, command_id="D1", expected_revision=session.row_version), caller=caller)
            assert replay["items"][0] == view and replay["command_receipt"] == r["command_receipt"]
            resume = {"schema_version": 1, "session_id": session.session_id, "agent_id": a, "expected_generation": purged.generation, "expected_row_version": purged.row_version, "destroy_command_id": "D9", "destroy_command_hash": purged.destroy_command_hash, "command_id": "RS1"}
            mismatch = await service.handle(req("agent_session_destroy_resume", resume, subject=a, command_id="RS1", expected_revision=purged.row_version), caller=caller)
            assert _err(mismatch) == "PURGE_IDENTITY_MISMATCH"
            # Reads on a purged session still answer the management summary by state.
            summary = _ok(await service.handle(req("agent_context_summary", {"session_id": session.session_id, "context_id": None, "cursor": None, "limit": 8}, subject=a), caller=caller))
            assert summary["session_state"] == "PURGED"
            gone = await service.handle(req("agent_session_history_read", {"schema_version": 1, "seq_from": 1, "seq_to": 2, "cursor": None, "max_bytes": 4096}, subject=a), caller=caller)
            assert _err(gone) in ("SESSION_NOT_ACTIVE", "SESSION_PURGED")

    asyncio.run(case())


def test_host_settings_flow_fixture_decodes_against_the_service(tmp_path) -> None:
    """The machine fixture's envelopes are accepted structurally by the same codec the service uses."""

    flow = json.loads((Path(__file__).parent / "data" / "host-settings-flow.json").read_text(encoding="utf-8"))["flow"]
    for item in flow:
        check("HostRequest", item["request"])
