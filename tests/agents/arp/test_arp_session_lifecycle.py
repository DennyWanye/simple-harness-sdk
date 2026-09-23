# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-D1: destroy → DRAINING → disposal proof → PURGING (rename → delete) → PURGED, the R1
recovery matrix on real directories, typed blocks that never revive the Session, the
authenticated resume, the rebuild gate and the retention permit."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.arp.creation import MARKER_FILE
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.indexing import VIEW_POLICY_HASH
from simple_harness.agents.arp.partition import NO_EMBEDDING_FINGERPRINT
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.contracts import AgentTurnState

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")


async def _turn(agent, text: str, input_id: str):  # type: ignore[no-untyped-def]
    receipt = await agent.submit(text, input_id=input_id)
    return await agent.wait_turn(receipt.turn_id, timeout=10)


def _delete_command(runtime, session, command_id: str = "destroy-1", *, generation: int | None = None) -> dict:  # type: ignore[no-untyped-def]
    return {
        "schema_version": 1, "session_id": session.session_id, "expected_generation": session.generation if generation is None else generation,
        "command_id": command_id, "reason": "USER_DESTROY", "retention_policy_ref": runtime.arp.ports.profile.refs.retention_policy_ref.to_json(),
    }


def _resume_command(session, command_id: str) -> dict:  # type: ignore[no-untyped-def]
    return {
        "schema_version": 1, "session_id": session.session_id, "agent_id": session.agent_id, "expected_generation": session.generation,
        "expected_row_version": session.row_version, "destroy_command_id": session.destroy_command_id,
        "destroy_command_hash": session.destroy_command_hash, "command_id": command_id,
    }


def _session(runtime, session_id: str):  # type: ignore[no-untyped-def]
    return store.read_session(runtime.uow.database.connection, session_id)


def _dirs(runtime, session):  # type: ignore[no-untyped-def]
    root = runtime.arp.root
    progress = session.purge_progress
    return root.resolve_relative(progress["source_relative_directory"]), root.resolve_relative(progress["trash_relative_directory"])


def _ticks_until(runtime, session_id: str, state: str, *, limit: int = 8):  # type: ignore[no-untyped-def]
    for _ in range(limit):
        runtime.arp.tick()
        session = _session(runtime, session_id)
        if session.state == state:
            return session
    raise AssertionError(f"session never reached {state}: {_session(runtime, session_id).state} {_session(runtime, session_id).purge_progress}")


def _receipt(runtime, kind: str, key: str):  # type: ignore[no-untyped-def]
    return store.read_original_receipt(runtime.uow.database.connection, kind=kind, receipt_key=key)


async def _seed(runtime):  # type: ignore[no-untyped-def]
    agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
    assert (await _turn(agent, "你好，请记住蓝鲸。", "i0")).state is AgentTurnState.COMMITTED
    return agent, store.read_live_session(runtime.uow.database.connection, agent.agent_id)


def test_destroy_drains_proves_disposal_and_purges_the_real_directory(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            lifecycle = runtime.arp.sessions
            source_before = runtime.arp.root.resolve_relative(session.relative_directory)
            assert (source_before / MARKER_FILE).is_file()
            after = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            # Frozen destroy identity + generation fence; the proof was complete at once.
            assert after.destroy_command_id == "destroy-1" and after.generation == session.generation + 1
            assert after.state == "PURGING" and after.purge_progress["phase"] == "RENAME_PENDING"
            assert after.sealed_highwater is not None and after.delete_proof_ref is not None
            disposal = _receipt(runtime, "session_disposal", f"{session.session_id}:destroy-1")
            assert disposal["all_safe"] is True and [c["kind"] for c in disposal["collections"]] == [
                "TURNS", "CALLS", "UNKNOWN", "PENDING_IMPORTS", "INDEX_WRITERS", "TEMP_ROOTS", "READ_HANDLES"
            ]
            for witness in disposal["collections"]:
                assert _receipt(runtime, "disposal_reader", witness["reader_receipt_ref"]["id"].split(":", 1)[1]) is not None
            assert runtime.binding(agent.agent_id).lifecycle == "closed"
            # No new inputs, no history/model reads on a draining session.
            with pytest.raises(Exception):
                await agent.submit("还在吗", input_id="late")
            with pytest.raises(ArpError) as refused:
                runtime.arp.retriever._session(session.agent_id)
            assert refused.value.code in ("SESSION_NOT_ACTIVE", "SESSION_PURGED", "SESSION_DRAINING")
            view = lifecycle.view(session.session_id)
            assert view["state"] == "PURGING" and view["deletable"] is False and view["purge_progress"]["destroy_command_id"] == "destroy-1"
            # PURGE job: rename → delete → finalize, one observed step per tick.
            purged = _ticks_until(runtime, session.session_id, "PURGED")
            source, trash = _dirs(runtime, purged)
            assert not source.exists() and not trash.exists()
            progress = purged.purge_progress
            assert progress["phase"] == "DELETE_CONFIRMED" and progress["blocking"] is None
            rename = _receipt(runtime, "purge_rename", f"{session.session_id}:destroy-1")
            delete = _receipt(runtime, "purge_delete", f"{session.session_id}:destroy-1")
            assert rename["recovered"] is False and delete["absence"] is False
            job = store.read_job_by_key(runtime.uow.database.connection, session.session_id, "PURGE", "destroy-1")
            assert job.state == "DONE" and job.result_receipt_ref is not None
            events = [e for e in store.list_event_bindings(runtime.uow.database.connection, event_type="RuntimeSessionStateChanged") if e.body["session_ref"]["id"] == session.session_id]
            assert [e.body["new_state"] for e in events] == ["ACTIVE", "DRAINING", "PURGING", "PURGING", "PURGING", "PURGED"]
            assert all(e.body["purge_progress_hash"] is not None for e in events[1:])
            # Same command replays; another destroy command is refused by name; the row can never revive.
            again = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            assert again.state == "PURGED"
            with pytest.raises(ArpError) as other:
                await lifecycle.destroy(_delete_command(runtime, session, "destroy-2", generation=purged.generation), caller=trusted_caller("d2"), command_id="destroy-2")
            assert other.value.code == "SESSION_PURGED"
            with pytest.raises(sqlite3.IntegrityError):
                runtime.uow.database.connection.execute("UPDATE arp_agent_sessions SET state='ACTIVE', row_version=row_version+1 WHERE session_id=?", (session.session_id,))
            assert lifecycle.view(session.session_id)["state"] == "PURGED"

    asyncio.run(case())


def test_destroy_waits_for_leased_index_work_and_reports_the_blocker(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            connection = runtime.uow.database.connection
            done = store.list_session_jobs(connection, session.session_id, kinds=("INDEX",), states=("DONE",))
            assert done, "the finished Turn must have materialised at least one closed group"
            with runtime.uow.database.transaction() as txn:
                store.put_job_locked(
                    txn, job_id="index-manual", session_id=session.session_id, kind="INDEX", semantic_key="manual", payload=done[0].payload,
                    generation=session.generation, next_at_ms=0, deadline_ms=10**12,
                )
                leased = store.claim_jobs_locked(txn, owner_id="other-owner", now_ms=1, lease_ms=10**14, session_id=session.session_id)
            assert [j.job_id for j in leased] == ["index-manual"]
            lifecycle = runtime.arp.sessions
            after = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            assert after.state == "DRAINING" and after.purge_progress["phase"] == "DRAINING"
            view = lifecycle.view(session.session_id)
            assert [r["id"] for r in view["blocking_refs"]] == ["index-manual"] and view["deletable"] is False
            assert store.read_job_by_key(connection, session.session_id, "PURGE", "destroy-1") is None
            outcome = runtime.arp.tick()
            assert outcome["draining"] == 1 and _session(runtime, session.session_id).state == "DRAINING"
            # The other owner hands the job back; the next pass cancels it (session not ACTIVE) and the proof seals.
            with runtime.uow.database.transaction() as txn:
                store.complete_job_locked(txn, store.read_job(txn, "index-manual"), state="PENDING", owner_id="other-owner", next_at_ms=0)
            runtime.arp.tick()
            assert store.read_job(connection, "index-manual").state == "CANCELLED"
            sealed = _session(runtime, session.session_id)
            assert sealed.state == "PURGING"
            assert _ticks_until(runtime, session.session_id, "PURGED").state == "PURGED"

    asyncio.run(case())


def test_destroy_with_an_open_turn_cancels_it_and_waits_for_settlement(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["记住了。", "迟到的回答。"], blocked=True)
        runtime = build(tmp_path, provider)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            provider.allow.set()
            assert (await _turn(agent, "你好。", "i0")).state is AgentTurnState.COMMITTED
            provider.allow.clear()
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            receipt = await agent.submit("慢一点。", input_id="i1")
            await asyncio.sleep(0.05)
            lifecycle = runtime.arp.sessions
            lifecycle.drain_timeout_s = 0.2
            after = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            assert after.state == "DRAINING"
            disposal = lifecycle.capture_disposal(after)
            turns = [c for c in disposal.body["collections"] if c["kind"] == "TURNS"][0]
            assert turns["count"] == 1 and turns["refs"][0]["id"] == receipt.turn_id and not disposal.all_safe
            provider.allow.set()
            result = await agent.wait_turn(receipt.turn_id, timeout=10)
            assert result.state in (AgentTurnState.COMMITTED, AgentTurnState.FAILED)
            assert _ticks_until(runtime, session.session_id, "PURGED").state == "PURGED"

    asyncio.run(case())


@pytest.mark.parametrize("point,expected", [("purge.after_rename", "purge_rename"), ("purge.after_delete", "purge_delete")])
def test_crash_between_file_operation_and_receipt_recovers_under_the_same_destroy(tmp_path, point: str, expected: str) -> None:
    async def case() -> None:
        armed = {"on": True}

        def fault(name: str) -> None:
            if armed["on"] and name == point:
                raise RuntimeError(f"crash at {name}")

        runtime = build(tmp_path, ScriptedProvider(["记住了。"]), fault=fault)
        async with runtime:
            agent, session = await _seed(runtime)
            lifecycle = runtime.arp.sessions
            sealed = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            assert sealed.state == "PURGING"
            source, trash = _dirs(runtime, sealed)
            if point == "purge.after_delete":
                renamed = lifecycle.advance(session.session_id)
                assert renamed.purge_progress["phase"] == "RENAMED" and trash.is_dir()
            with pytest.raises(RuntimeError):
                lifecycle.advance(session.session_id)
            # The file operation happened, its receipt did not: the row still names the old phase.
            stale = _session(runtime, session.session_id)
            assert stale.purge_progress["phase"] == ("RENAME_PENDING" if point == "purge.after_rename" else "RENAMED")
            assert not source.exists() and (trash.is_dir() if point == "purge.after_rename" else not trash.exists())
            armed["on"] = False
            recovered = lifecycle.advance(session.session_id)
            body = _receipt(runtime, expected, f"{session.session_id}:destroy-1")
            if point == "purge.after_rename":
                assert recovered.purge_progress["phase"] == "RENAMED" and body["recovered"] is True
            else:
                assert recovered.purge_progress["phase"] == "DELETE_CONFIRMED" and body["absence"] is True
            assert _ticks_until(runtime, session.session_id, "PURGED").state == "PURGED"

    asyncio.run(case())


def test_dual_directory_blocks_durably_until_an_authenticated_resume_reinspects(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            lifecycle = runtime.arp.sessions
            connection = runtime.uow.database.connection
            sealed = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            source, trash = _dirs(runtime, sealed)
            shutil.copytree(source, trash)  # a stray duplicate with the same marker
            runtime.arp.tick()
            blocked = _session(runtime, session.session_id)
            block = blocked.purge_progress["blocking"]
            assert blocked.state == "PURGING" and blocked.purge_progress["phase"] == "RENAME_PENDING"
            assert block["code"] == "DUAL_DIRECTORY" and block["retry_mode"] == "MANUAL" and block["retry_at_ms"] is None
            assert source.is_dir() and trash.is_dir(), "nothing was moved or deleted"
            observation = _receipt(runtime, "purge_observation", f"{session.session_id}:destroy-1:{sealed.row_version}")
            assert observation["source_state"] == "MATCH" and observation["trash_state"] == "MATCH"
            job = store.read_job_by_key(connection, session.session_id, "PURGE", "destroy-1")
            assert job.state == "BLOCKED"
            view = lifecycle.view(session.session_id)
            assert view["blocking_refs"] == [block["observation_ref"]] and view["last_lifecycle_receipt_ref"] == block["observation_ref"]
            # Later passes do nothing on a MANUAL block, and SQL cannot revive the row either.
            runtime.arp.tick()
            assert _session(runtime, session.session_id).row_version == blocked.row_version
            for target in ("ACTIVE", "QUARANTINED"):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute("UPDATE arp_agent_sessions SET state=?, row_version=row_version+1 WHERE session_id=?", (target, session.session_id))
            # The rebuild API refuses a destroy identity outright.
            with pytest.raises(ArpError) as rebuild:
                lifecycle.rebuild(_rebuild_command(runtime, blocked, 1), caller=trusted_caller("r"), command_id="rebuild-x")
            assert rebuild.value.code == "SESSION_PURGED"
            # A resume that names another destroy identity is refused; a stale row version too.
            wrong = _resume_command(blocked, "resume-0")
            wrong["destroy_command_id"] = "destroy-9"
            with pytest.raises(ArpError) as mismatch:
                lifecycle.resume_destroy(wrong, caller=trusted_caller("rs"), command_id="resume-0")
            assert mismatch.value.code == "PURGE_IDENTITY_MISMATCH"
            stale = _resume_command(blocked, "resume-0")
            stale["expected_row_version"] = blocked.row_version - 1
            with pytest.raises(ArpError) as old:
                lifecycle.resume_destroy(stale, caller=trusted_caller("rs"), command_id="resume-0")
            assert old.value.code == "GENERATION_STALE"
            # Resume while the duplicate is still there: re-inspected, still blocked, first_seen kept.
            still = lifecycle.resume_destroy(_resume_command(blocked, "resume-1"), caller=trusted_caller("rs"), command_id="resume-1")
            assert still.purge_progress["blocking"]["code"] == "DUAL_DIRECTORY"
            assert still.purge_progress["blocking"]["first_seen_at_ms"] == block["first_seen_at_ms"]
            assert still.purge_progress["blocking"]["observation_ref"] != block["observation_ref"]
            # The operator removes the duplicate outside the tool; only a new resume continues the phase.
            shutil.rmtree(trash)
            renamed = lifecycle.resume_destroy(_resume_command(still, "resume-2"), caller=trusted_caller("rs"), command_id="resume-2")
            assert renamed.purge_progress["phase"] == "RENAMED" and renamed.purge_progress["blocking"] is None
            assert not source.exists() and trash.is_dir()
            assert store.read_job_by_key(connection, session.session_id, "PURGE", "destroy-1").state == "PENDING"
            # The same resume command again is a replay: no second inspection, same row.
            replay = lifecycle.resume_destroy(_resume_command(still, "resume-2"), caller=trusted_caller("rs"), command_id="resume-2")
            assert replay.row_version == renamed.row_version
            assert _ticks_until(runtime, session.session_id, "PURGED").state == "PURGED"

    asyncio.run(case())


def test_marker_mismatch_and_ambiguous_paths_block_without_touching_files(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            lifecycle = runtime.arp.sessions
            sealed = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            source, trash = _dirs(runtime, sealed)
            original = (source / MARKER_FILE).read_bytes()
            (source / MARKER_FILE).write_bytes(original.replace(b"session-", b"sessiom-", 1))
            blocked = lifecycle.advance(session.session_id)
            assert blocked.purge_progress["blocking"]["code"] == "MARKER_MISMATCH" and source.is_dir()
            (source / MARKER_FILE).write_bytes(original)
            # Both directories gone before any rename proof: ambiguous, never "purged".
            shutil.move(str(source), str(tmp_path / "elsewhere"))
            ambiguous = lifecycle.resume_destroy(_resume_command(blocked, "resume-1"), caller=trusted_caller("rs"), command_id="resume-1")
            assert ambiguous.purge_progress["blocking"]["code"] == "DELETE_STATE_AMBIGUOUS" and ambiguous.state == "PURGING"
            shutil.move(str(tmp_path / "elsewhere"), str(source))
            clean = lifecycle.resume_destroy(_resume_command(ambiguous, "resume-2"), caller=trusted_caller("rs"), command_id="resume-2")
            assert clean.purge_progress["phase"] == "RENAMED" and clean.purge_progress["blocking"] is None
            assert _ticks_until(runtime, session.session_id, "PURGED").state == "PURGED"

    asyncio.run(case())


def _rebuild_command(runtime, session, old_generation: int) -> dict:  # type: ignore[no-untyped-def]
    index = runtime.arp.index
    embedding = index.embedding_resource_ref or Pin("deployment", "embedding:none", 0, NO_EMBEDDING_FINGERPRINT)
    return {
        "schema_version": 1, "session_id": session.session_id, "expected_generation": session.generation, "old_index_generation": old_generation,
        "embedding_deployment_ref": embedding.to_json(), "chunker_ref": Pin("policy", "chunker", 1, index.chunker).to_json(),
        "view_policy_ref": Pin("policy", "view", 1, VIEW_POLICY_HASH).to_json(), "reason": "operator rebuild",
    }


def test_rebuild_only_for_quarantined_sessions_without_a_destroy_identity(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            lifecycle = runtime.arp.sessions
            connection = runtime.uow.database.connection
            published = store.active_index_publication(connection, session.session_id)
            assert published is not None
            with pytest.raises(ArpError) as active:
                lifecycle.rebuild(_rebuild_command(runtime, session, published.index_generation), caller=trusted_caller("r"), command_id="rebuild-0")
            assert active.value.code == "STATE_COMBINATION_INVALID"
            isolated = lifecycle.quarantine(session.session_id, caller=trusted_caller("q"), command_id="quarantine-1", reason="index anomaly")
            assert isolated.state == "QUARANTINED" and isolated.generation == session.generation + 1 and isolated.destroy_command_id is None
            with pytest.raises(ArpError) as stale:
                lifecycle.rebuild(_rebuild_command(runtime, isolated, published.index_generation + 5), caller=trusted_caller("r"), command_id="rebuild-1")
            assert stale.value.code == "GENERATION_STALE"
            rebuilt = lifecycle.rebuild(_rebuild_command(runtime, isolated, published.index_generation), caller=trusted_caller("r"), command_id="rebuild-2")
            assert rebuilt.state == "ACTIVE" and rebuilt.generation == isolated.generation
            fresh = runtime.arp.index.state_for(rebuilt).generation
            assert fresh.index_generation == published.index_generation + 1
            pending = store.list_session_jobs(connection, session.session_id, kinds=("INDEX",), states=("PENDING",))
            assert pending and all(int(j.payload["index_generation"]) == fresh.index_generation for j in pending)
            for _ in range(4):
                runtime.arp.tick()
            republished = store.active_index_publication(connection, session.session_id)
            assert republished.index_generation == fresh.index_generation
            # A replayed rebuild command returns the row without a second generation.
            assert lifecycle.rebuild(_rebuild_command(runtime, isolated, published.index_generation), caller=trusted_caller("r"), command_id="rebuild-2").state == "ACTIVE"
            assert runtime.arp.index.state_for(rebuilt).generation.index_generation == fresh.index_generation

    asyncio.run(case())


def test_retention_permit_is_the_only_key_to_the_central_row(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            lifecycle = runtime.arp.sessions
            connection = runtime.uow.database.connection
            with pytest.raises(ArpError) as early:
                lifecycle.permit_session_erasure(session.session_id, caller=trusted_caller("p"), command_id="permit-0")
            assert early.value.code == "RETENTION_BLOCKED"
            await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            purged = _ticks_until(runtime, session.session_id, "PURGED")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM arp_agent_sessions WHERE session_id=?", (session.session_id,))
            permit = lifecycle.permit_session_erasure(session.session_id, caller=trusted_caller("p"), command_id="permit-1")
            assert permit["row_key"] == json.dumps([session.session_id]) and permit["body_hash"] == purged.create_command_hash
            assert lifecycle.permit_session_erasure(session.session_id, caller=trusted_caller("p"), command_id="permit-1") == permit
            row = connection.execute("SELECT row_key FROM arp_retention_permits WHERE table_name='arp_agent_sessions'").fetchone()
            assert row[0] == connection.execute("SELECT json_array(?)", (session.session_id,)).fetchone()[0]
            # The guard now lets the row go (the delete itself stays the retention service's call);
            # the attempt is rolled back so the rest of the ledger keeps its references.
            connection.execute("BEGIN")
            try:
                connection.execute("DELETE FROM arp_agent_sessions WHERE session_id=?", (session.session_id,))
            except sqlite3.IntegrityError as error:
                assert "retention permit required" not in str(error)
            finally:
                connection.execute("ROLLBACK")

    asyncio.run(case())


# ---- fixes after the independent review (blocking findings 1–3) ------------------------------

SECRET = "工程暗号 是 蓝鲸七号，请牢记。"
FILLERS = [f"第{i}条闲聊：今天天气不错，我们聊聊别的话题 {i}。" for i in range(6)]
SMALL = dict(
    input_limit=72,
    max_output=16,
    policy_overrides=dict(
        max_context_tokens=72, output_reserve_tokens=16, safety_reserve_tokens=2, tool_headroom_tokens=2,
        recent_min_tokens=12, recall_max_tokens=24, fixed_soft_max_tokens=32,
    ),
)


def test_destroy_makes_an_unfinished_recall_stale_and_still_purges(tmp_path) -> None:
    async def case() -> None:
        armed = {"on": False}

        def fault(name: str) -> None:
            if armed["on"] and name == "recall.after_page":
                raise RuntimeError("exit between pages")

        provider = ScriptedProvider(["记住了。"] + ["好的。"] * len(FILLERS) + ["x", "y"])
        runtime = build(tmp_path, provider, fault=fault, **SMALL)
        async with runtime:
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert (await _turn(agent, SECRET, "i0")).state is AgentTurnState.COMMITTED
            for i, filler in enumerate(FILLERS, 1):
                assert (await _turn(agent, filler, f"i{i}")).state is AgentTurnState.COMMITTED
            connection = runtime.uow.database.connection
            armed["on"] = True
            receipt = await agent.submit("请问工程暗号 是什么？", input_id="ask")
            try:
                await agent.wait_turn(receipt.turn_id, timeout=2)
            except Exception:  # noqa: BLE001 - the crash leaves the recall row SCANNING
                pass
            armed["on"] = False
            pending = store.list_pending_recalls(connection)
            assert len(pending) == 1 and pending[0].phase == "SCANNING"
            session = store.read_live_session(connection, agent.agent_id)
            lifecycle = runtime.arp.sessions
            lifecycle.drain_timeout_s = 0.5
            after = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            stale = store.read_context_recall(connection, pending[0].recall_key)
            assert stale.phase == "STALE" and stale.terminal and store.list_pending_recalls(connection) == ()
            assert after.state in ("DRAINING", "PURGING")
            purged = _ticks_until(runtime, session.session_id, "PURGED")
            assert purged.state == "PURGED"

    asyncio.run(case())


def test_partial_delete_keeps_the_marker_so_the_same_destroy_can_retry(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            lifecycle = runtime.arp.sessions
            sealed = await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            renamed = lifecycle.advance(session.session_id)
            source, trash = _dirs(runtime, renamed)
            assert renamed.purge_progress["phase"] == "RENAMED" and trash.is_dir()
            stuck = trash / "zz-held"
            stuck.mkdir()
            (stuck / "open.tmp").write_bytes(b"x")
            stuck.chmod(0o500)  # its file cannot be unlinked → the delete fails part-way
            try:
                blocked = lifecycle.advance(session.session_id)
                block = blocked.purge_progress["blocking"]
                assert block["code"] == "FILE_BUSY" and block["retry_mode"] == "SAME_DESTROY_BACKOFF" and block["retry_at_ms"] is not None
                assert blocked.purge_progress["phase"] == "RENAMED"
                assert (trash / MARKER_FILE).is_file(), "the marker is deleted last, so the directory still identifies itself"
                assert runtime.arp.tick()["jobs"] >= 0 and _session(runtime, session.session_id).purge_progress["blocking"]["code"] == "FILE_BUSY"
            finally:
                stuck.chmod(0o700)
            resumed = lifecycle.resume_destroy(_resume_command(blocked, "resume-1"), caller=trusted_caller("rs"), command_id="resume-1")
            assert resumed.purge_progress["phase"] == "DELETE_CONFIRMED" and resumed.purge_progress["blocking"] is None and not trash.exists()
            assert _ticks_until(runtime, session.session_id, "PURGED").state == "PURGED"

    asyncio.run(case())


def test_lost_embedding_call_is_settled_as_unknown_before_its_job_is_cancelled(tmp_path) -> None:
    from simple_harness.agents.arp import embedding_call
    from simple_harness.agents.memory.embedding import HashEmbedder

    async def case() -> None:
        armed = {"on": True}

        def fault(name: str) -> None:
            if armed["on"] and name == "index.before_embed":
                armed["on"] = False
                raise RuntimeError("process exit after the intent")

        runtime = build(tmp_path, ScriptedProvider(["记住了。"]), fault=fault, embedding=HashEmbedder(dim=32))
        async with runtime:
            agent, session = await _seed(runtime)
            connection = runtime.uow.database.connection
            open_before = embedding_call.list_open_intents(connection, agent.agent_id)
            assert len(open_before) == 1, "one intent was written and its receipt never came back"
            blocked_jobs = store.list_session_jobs(connection, session.session_id, kinds=("INDEX",), states=("BLOCKED",))
            assert len(blocked_jobs) == 1 and open_before[0][0].startswith(blocked_jobs[0].job_id)
            lifecycle = runtime.arp.sessions
            capture = lifecycle.capture_disposal(session)
            unknown = [c for c in capture.body["collections"] if c["kind"] == "UNKNOWN"][0]
            assert unknown["count"] == 1 and unknown["refs"][0]["id"] == open_before[0][0] and not capture.all_safe
            await lifecycle.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
            settled = embedding_call.read_call(connection, open_before[0][0])
            assert settled["status"] == "UNKNOWN" and settled["detail"] == "result_lost_before_receipt"
            assert embedding_call.list_open_intents(connection, agent.agent_id) == ()
            assert store.read_job(connection, blocked_jobs[0].job_id).state == "CANCELLED"
            assert _ticks_until(runtime, session.session_id, "PURGED").state == "PURGED"

    asyncio.run(case())


def test_a_creation_key_never_revives_a_destroyed_session(tmp_path) -> None:
    """Found by the RP-E2 random test (seed 11): re-creating with the key of a PURGED session
    re-wrote the marker into the purged directory. Refused by name at every non-ACTIVE state."""

    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider(["记住了。"]))
        async with runtime:
            agent, session = await _seed(runtime)
            sealed = await lifecycle_destroy(runtime, session)
            source, _trash = _dirs(runtime, sealed)
            with pytest.raises(ArpError) as during:
                await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert during.value.code == "SESSION_NOT_ACTIVE"
            purged = _ticks_until(runtime, session.session_id, "PURGED")
            assert purged.state == "PURGED" and not source.exists()
            with pytest.raises(ArpError) as after:
                await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            assert after.value.code == "SESSION_PURGED" and not source.exists()
            assert store.read_session(runtime.uow.database.connection, session.session_id).state == "PURGED"
            # Another key still creates a fresh Agent in its own directory; a QUARANTINED
            # session is live and recoverable, so its key replays the same Agent (review).
            other = await runtime.create(CONFIG, creation_key="k2", caller=trusted_caller())
            assert other.agent_id != agent.agent_id and store.read_live_session(runtime.uow.database.connection, other.agent_id).state == "ACTIVE"
            isolated = runtime.arp.sessions.quarantine(store.read_live_session(runtime.uow.database.connection, other.agent_id).session_id, caller=trusted_caller("q"), command_id="quarantine-1", reason="index anomaly")
            assert isolated.state == "QUARANTINED"
            replayed = await runtime.create(CONFIG, creation_key="k2", caller=trusted_caller())
            assert replayed.agent_id == other.agent_id and store.read_session(runtime.uow.database.connection, isolated.session_id).state == "QUARANTINED"

    asyncio.run(case())


async def lifecycle_destroy(runtime, session):  # type: ignore[no-untyped-def]
    return await runtime.arp.sessions.destroy(_delete_command(runtime, session), caller=trusted_caller("d1"), command_id="destroy-1")
