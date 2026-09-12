# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A08 real two-pool SDK backup; no fabricated PASS, no OS-kill claim."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import replace

import pytest
from graph_helpers7 import node, spec
from test_multi_mission_load import MeasuredProvider, _mission
from test_provider_budget_guard import ActualProvider, Counter, grants

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.artifacts.store import read_verified
from agent_orchestrator.artifacts.workspace import WorkspaceManager
from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import MissionSpec, Reservation
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.agent_worker import user_message_json
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RoutingRules, RuntimeProfile
from agent_orchestrator.storage import offline_backup as backup
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import RoleScriptedProvider, envelope_step, package_of
from simple_harness.agents import AgentConfig
from simple_harness.agents.context.budget import ContextPolicy

IDENTITY = backup.BackupSourceIdentity("a" * 40, "b" * 64)


@pytest.fixture
def source(tmp_path):
    provider = MeasuredProvider()
    provider.release_workers.set()
    cfg = OrchestratorConfig(evidence_root=tmp_path / "source", max_concurrency=2)
    profiles = {
        name: RuntimeProfile(name, provider, "agent-model", context_policy=ContextPolicy())
        for name in ("default", "critic")
    }

    async def prepare():
        async with Orchestrator(
            cfg,
            profiles=profiles,
            routing=RoutingRules("default", by_role={"critic": "critic"}),
            provider_token_estimator=Counter(1000),
            poll_interval=0.002,
        ) as orch:
            missions = {name: await _mission(orch, name) for name in ("slow", "human")}
            await asyncio.wait_for(orch.run(), 15)
            assert orch.store.get_mission(missions["slow"][0].id).status is MissionStatus.COMPLETED
            assert orch.store.list_approvals(missions["human"][0].id)[0]["state"] == "PENDING"
            assert provider.reviewed
            for pool in orch.assembled.pools.values():
                assert (
                    pool.runtime.uow.database.connection.execute(
                        "SELECT COUNT(*) FROM provider_invocations"
                    ).fetchone()[0]
                    > 0
                )
            return missions

    missions = asyncio.run(prepare())
    return cfg, profiles, provider, missions


def make_backup(source, destination):
    cfg, profiles, _, _ = source
    return backup.backup_offline(
        cfg,
        profiles,
        WorkspaceManager(cfg.workspaces_root),
        instance_lock_path=cfg.evidence_root / ".instance.lock",
        destination=destination,
        source_identity=IDENTITY,
    )


@pytest.mark.parametrize("state", ["active", "superseded", "revoked", "pending_new"])
def test_required_source_cas_missing_refuses_before_publication(source, tmp_path, state):
    cfg, profiles, _, _ = source

    async def register():
        # Reopen with the original fixture's frozen admission and role routing.
        # Assembly must validate every persisted intent before source mutation.
        async with Orchestrator(
            cfg,
            profiles=profiles,
            routing=RoutingRules("default", by_role={"critic": "critic"}),
            provider_token_estimator=Counter(1000),
        ) as orch:
            mission = await orch.submit_mission(
                MissionSpec(
                    goal="offline source integrity",
                    success_criteria=("file:report.md",),
                    tenant_id="source-owner",
                    idempotency_key="backup-source",
                    domain=DOC_DOMAIN,
                )
            )
            api = MissionControlV1(
                orch, tenant_id=mission.tenant_id, principal=Principal("source-reviewer")
            )
            command = {"mission_id": mission.id, "path": "sources/original.md"}
            registered = api.register_source(
                {
                    **command,
                    "content": "Original source only in CAS.\n",
                    "kind": "markdown",
                    "idempotency_key": "register",
                }
            )
            required = registered["version_hash"]
            if state != "active":
                body = {**command, "expected_version_hash": required, "idempotency_key": "change"}
                if state == "revoked":
                    pending = api.revoke_source({**body, "reason": "withdrawn"})
                else:
                    pending = api.supersede_source(
                        {
                            **body,
                            "content": "Replacement source only in CAS.\n",
                            "kind": "markdown",
                        }
                    )
                if state == "pending_new":
                    required = pending["version_hash"]
                    assert orch.store.get_source(mission.id, command["path"], required) is None
                else:
                    api.decide(pending["request_id"], "approve", nonce="review-source")
            assert required not in {a.content_hash for a in orch.store.list_all_artifacts()}
            return WorkspaceManager(cfg.workspaces_root).artifact_store.path_for(required)

    missing = asyncio.run(register())
    missing.unlink()  # actual registered/pending source loss, no manufactured DB state
    with pytest.raises(backup.OfflineBackupError, match="required_source_cas"):
        make_backup(source, tmp_path / "missing-source")
    assert not (tmp_path / "missing-source").exists()
    assert not list(tmp_path.glob(".missing-source.offline-*"))


@pytest.mark.parametrize("source_name", ["source", "bundle"])
def test_case_alias_destination_inside_source_is_rejected_without_changes(tmp_path, source_name):
    root = tmp_path / source_name
    nested = root / "workspaces"
    nested.mkdir(parents=True)
    original = nested / "original.txt"
    original.write_bytes(b"must remain unchanged")
    alias = tmp_path / source_name.upper() / "WORKSPACES"
    if not alias.exists():
        pytest.skip("requires a case-insensitive filesystem; no emulated case alias")
    assert alias.samefile(nested)
    before = (original.stat().st_ino, original.read_bytes(), tuple(root.rglob("*")))
    with pytest.raises(backup.OfflineBackupError, match="destination_inside_source"):
        backup._destination(alias / "new-target", outside=root.resolve())
    assert not (nested / "new-target").exists()
    assert (original.stat().st_ino, original.read_bytes(), tuple(root.rglob("*"))) == before


def test_actual_source_citations_survive_isolated_restore_and_pending_approval(tmp_path):
    """Actual SDK/Verifier receipts, source history and pending bytes, no fake bindings."""
    path = "sources/report.md"
    quote = "资料只记录一次离线实验。"
    original = quote + "\r\n保留完整的范围与限制。\r\n"
    report = "分析报告：只归因于原始来源。\n"
    version = hashlib.sha256(original.encode()).hexdigest()
    reviewed = []

    def amend(body):
        return {
            **body,
            "evidence": [],
            "claims": [
                {
                    "content": quote,
                    "confidence": 0.8,
                    "citations": [
                        {
                            "path": path,
                            "version": version,
                            "start_line": 1,
                            "end_line": 1,
                            "quote": quote,
                        }
                    ],
                }
            ],
        }

    def assess(request):
        tools = [json.loads(m.content) for m in request.messages if str(m.role) == "tool"]
        actual = tools[-1]["value"]["content"]
        reviewed.append(actual)
        met = actual == report
        return (
            "<critic_verdict>"
            + json.dumps(
                {
                    "verdict": "PASS" if met else "FAIL",
                    "findings": [] if met else [{"severity": "blocker", "detail": "wrong bytes"}],
                    "mission_criteria": [
                        {"criterion": c, "met": met, "reason": "actual report read"}
                        for c in package_of(request)["mission_success_criteria"]
                    ],
                }
            )
            + "</critic_verdict>"
        )

    provider = RoleScriptedProvider(
        {
            "worker": [
                ("workspace_read_file", {"path": path}),
                ("workspace_write_file", {"path": "a.md", "content": report}),
                envelope_step(
                    summary="归因分析", artifacts=["a.md"], claims=[quote], override=amend
                ),
            ],
            "critic": [("workspace_read_file", {"path": "a.md"}), assess],
        }
    )
    cfg = OrchestratorConfig(evidence_root=tmp_path / "source-doc")
    profiles = {
        name: RuntimeProfile(name, provider, "agent-model", context_policy=ContextPolicy())
        for name in ("default", "critic")
    }
    routing = RoutingRules("default", by_role={"critic": "critic"})

    def control(orch, mid):
        return MissionControlV1(
            orch,
            tenant_id=orch.store.get_mission(mid).tenant_id,
            principal=Principal("source-reviewer"),
        )

    async def prepare():
        async with Orchestrator(
            cfg, profiles=profiles, routing=routing, provider_token_estimator=Counter(1000)
        ) as orch:
            mission = await orch.submit_mission(
                spec(domain=DOC_DOMAIN, success_criteria=("file:a.md",))
            )
            api = control(orch, mission.id)
            command = {"mission_id": mission.id, "path": path}
            api.register_source(
                {**command, "content": original, "kind": "markdown", "idempotency_key": "original"}
            )
            planning = orch.commit.begin_planning(mission.id)
            [task], _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                tokens=80_000,
                                success_criteria=["file:a.md", "cite:" + path],
                                verification_policy=["format_check", "rule_check", "critic_review"],
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "backup fixture"},
            )
            assert await orch._next_attempt(orch.store.get_mission(mission.id), task, [])
            [attempt] = orch.store.list_attempts(task.id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert await orch._dispatch(intent)
            intent = orch.store.get_intent(intent.intent_id)

            async def completed():
                while True:
                    result = await orch.bridge_for(intent).result(
                        agent_id=intent.agent_id,
                        turn_id=intent.expected_turn_id,
                    )
                    if result is not None:
                        return result
                    await asyncio.sleep(0.002)

            await orch._collect_attempt(intent, await asyncio.wait_for(completed(), 10))
            result = orch.store.find_result_for_attempt(attempt.id)
            assert result is not None
            assert await asyncio.wait_for(orch._verify(result.envelope.id), 10)
            assert orch.store.get_task(task.id).accepted_result_id == result.envelope.id
            assert reviewed == [report]
            rows = orch.store.list_criterion_assessments(mission.id, result_id=result.envelope.id)
            row = next(row for row in rows if row["evidence_refs"])
            args = {
                "result_id": result.envelope.id,
                "receipt_id": row["receipt_id"],
                "citation_index": 0,
            }
            before = api.citation_read(mission.id, **args)
            assert before["text"] == original and before["historical_verdict"] == "PASS"
            replacement = api.supersede_source(
                {
                    **command,
                    "content": "新版记录。\n",
                    "kind": "markdown",
                    "expected_version_hash": version,
                    "idempotency_key": "replace",
                }
            )
            api.decide(replacement["request_id"], "approve", nonce="approve-replace")
            withdrawn = api.revoke_source(
                {
                    **command,
                    "expected_version_hash": replacement["version_hash"],
                    "reason": "撤销新版",
                    "idempotency_key": "revoke",
                }
            )
            api.decide(withdrawn["request_id"], "approve", nonce="approve-revoke")
            pending_text = "待审批第三版，不能被遗漏。\n"
            pending = api.supersede_source(
                {
                    **command,
                    "content": pending_text,
                    "kind": "markdown",
                    "expected_version_hash": replacement["version_hash"],
                    "idempotency_key": "pending",
                }
            )
            expected = api.citation_read(mission.id, **args)
            assert expected["source_state"]["active_version_hash"] is None
            assert expected["source_state"]["superseded_by"] == replacement["version_hash"]
            for pool in orch.assembled.pools.values():
                assert (
                    pool.runtime.uow.database.connection.execute(
                        "SELECT COUNT(*) FROM provider_invocations WHERE state='succeeded'"
                    ).fetchone()[0]
                    > 0
                )
            return mission.id, args, expected, pending, pending_text, replacement["version_hash"]

    mid, args, expected, pending, pending_text, replaced_hash = asyncio.run(prepare())
    receipt = make_backup((cfg, profiles, provider, {}), tmp_path / "source-bundle")
    manifest = json.loads((tmp_path / "source-bundle" / "manifest.json").read_text())
    assert set(manifest["required_source_cas"]) == {version, replaced_hash, pending["version_hash"]}
    cfg.evidence_root.rename(tmp_path / "hidden-doc-original")
    calls = provider.calls
    target = tmp_path / "restored-doc"
    backup.restore_offline(
        tmp_path / "source-bundle",
        destination=target,
        expected_manifest_sha256=receipt["manifest_sha256"],
    )

    async def read_restored():
        async with Orchestrator(
            replace(cfg, evidence_root=target),
            profiles=profiles,
            routing=routing,
            provider_token_estimator=Counter(1000),
        ) as orch:
            api = control(orch, mid)
            assert api.citation_read(mid, **args) == expected
            offset, chunks = 0, []
            while True:
                page = api.citation_read(mid, **args, offset=offset, limit=5)
                assert page["block_id"] == expected["block_id"]
                chunks.append(page["text"])
                if page["next_offset"] is None:
                    break
                assert page["next_offset"] > offset
                offset = page["next_offset"]
            assert "".join(chunks).encode() == original.encode()
            historical = orch.store.get_source(mid, path, replaced_hash)
            assert historical["revoked"] is True
            assert orch.store.get_approval(pending["request_id"])["state"] == "PENDING"
            api.decide(pending["request_id"], "approve", nonce="approve-after-restore")
            assert orch.store.get_source(mid, path)["version_hash"] == pending["version_hash"]
            assert orch.commit._source_cas().read(pending["version_hash"]) == pending_text.encode()
            assert api.citation_read(mid, **args)["text"] == original
            assert provider.calls == calls  # restore/read/approval must not run SDK work

    asyncio.run(read_restored())
    # Reproduce an older producer's incomplete inventory, with a trusted manifest
    # digest: restore must derive required bytes from the actual SQL rows again.
    damaged = tmp_path / "incomplete-source-bundle"
    shutil.copytree(tmp_path / "source-bundle", damaged)
    rel = "artifacts/sha256/" + pending["version_hash"]
    (damaged / rel).unlink()
    del manifest["files"][rel]
    manifest["required_source_cas"].remove(pending["version_hash"])
    (damaged / "manifest.json").unlink()  # only the deliberately damaged copy
    digest = backup._write(damaged / "manifest.json", manifest)
    with pytest.raises(backup.OfflineBackupError, match="required_source_cas"):
        backup.restore_offline(
            damaged, destination=tmp_path / "incomplete-target", expected_manifest_sha256=digest
        )
    assert not (tmp_path / "incomplete-target").exists()
    assert not list(tmp_path.glob(".incomplete-target.offline-*"))


def test_two_real_pools_wal_cas_and_frozen_receipts_restore_without_source(source, tmp_path):
    cfg, profiles, provider, missions = source
    # Keep a committed WAL page outside the main db file while the helper copies.
    writer = sqlite3.connect(cfg.orchestrator_db, isolation_level=None)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE scheduler_state SET updated_at=updated_at+1")
    expected_state = writer.execute("SELECT * FROM scheduler_state ORDER BY key").fetchall()
    try:
        assert cfg.orchestrator_db.with_name("orchestrator.db-wal").stat().st_size > 0
        receipt = make_backup(source, tmp_path / "bundle")
    finally:
        writer.close()
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text())
    with sqlite3.connect(tmp_path / "bundle" / "orchestrator.db") as copied:
        assert (
            copied.execute("SELECT * FROM scheduler_state ORDER BY key").fetchall()
            == expected_state
        )
    assert len(manifest["databases"]) == 3
    assert manifest["source_identity"] == IDENTITY.to_json()
    assert manifest["derived_paths"]
    assert {
        item["profile_id"] for item in manifest["databases"] if item["role"] == "execution"
    } == {
        "default",
        "critic",
    }
    # Fail if a restored artifact accidentally resolves through the original root.
    cfg.evidence_root.rename(tmp_path / "hidden-original")
    original_calls = len(provider.trace)
    restored = backup.restore_offline(
        tmp_path / "bundle",
        destination=tmp_path / "restored",
        expected_manifest_sha256=receipt["manifest_sha256"],
    )
    assert len(provider.trace) == original_calls  # restore never dispatches
    assert restored["formal_fingerprints"] == manifest["formal_fingerprints"]
    store = Store.open_readonly(tmp_path / "restored" / "orchestrator.db")
    try:
        for artifact in store.list_all_artifacts():
            assert artifact.storage_uri.startswith(str(tmp_path / "restored"))
            assert hashlib.sha256(read_verified(artifact)).hexdigest() == artifact.content_hash
        assert store.get_mission(missions["slow"][0].id).status is MissionStatus.COMPLETED
        assert store.list_approvals(missions["human"][0].id)[0]["state"] == "PENDING"
    finally:
        store.close()

    async def resume():
        target = replace(cfg, evidence_root=tmp_path / "restored")
        async with Orchestrator(
            target,
            profiles=profiles,
            routing=RoutingRules("default", by_role={"critic": "critic"}),
            provider_token_estimator=Counter(1000),
        ) as orch:
            await asyncio.wait_for(orch.run(), 10)
            assert orch.store.get_mission(missions["slow"][0].id).status is MissionStatus.COMPLETED
            assert len(provider.trace) == original_calls

    asyncio.run(resume())


def test_actual_host_equivalent_instance_lock_and_wrong_root_refuse(source, tmp_path):
    cfg = source[0]
    # Identical path + flock protocol to Host InstanceLock, using a separate fd.
    fd = os.open(cfg.evidence_root / ".instance.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(backup.OfflineBackupError, match="instance_busy"):
            make_backup(source, tmp_path / "busy")
        assert not (tmp_path / "busy").exists()
    finally:
        os.close(fd)
    with pytest.raises(backup.OfflineBackupError, match="instance_lock_path"):
        backup.backup_offline(
            cfg,
            source[1],
            WorkspaceManager(cfg.workspaces_root),
            instance_lock_path=tmp_path / ".instance.lock",
            destination=tmp_path / "wrong",
            source_identity=IDENTITY,
        )
    assert not (tmp_path / "wrong").exists()


def test_sqlite_writer_refused_and_all_locks_block_concurrent_admission(
    source, tmp_path, monkeypatch
):
    cfg = source[0]
    other = sqlite3.connect(cfg.execution_db, isolation_level=None, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(backup.OfflineBackupError, match="database_busy"):
            make_backup(source, tmp_path / "blocked")
        other.rollback()
    finally:
        other.close()
    original = backup._copy_database
    observed = []

    def probe(path, target):
        for db in (
            cfg.orchestrator_db,
            cfg.execution_db,
            cfg.evidence_root / "execution-critic.db",
        ):
            connection = sqlite3.connect(db, isolation_level=None, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
                observed.append(db.name)
            finally:
                connection.close()
        return original(path, target)

    monkeypatch.setattr(backup, "_copy_database", probe)
    make_backup(source, tmp_path / "locked-copy")
    assert len(observed) == 9


@pytest.mark.parametrize("damage", ["missing_db", "context", "cas", "manifest", "extra_db"])
def test_restore_rejects_damage_without_publishing_or_overwriting(source, tmp_path, damage):
    receipt = make_backup(source, tmp_path / "bundle")
    root = tmp_path / "bundle"
    if damage == "missing_db":
        (root / "execution-critic.db").unlink()
    elif damage == "context":
        (root / "execution.db.context.json").write_text("{}")
    elif damage == "cas":
        damaged = next((root / "artifacts" / "sha256").iterdir())
        damaged.chmod(0o600)
        damaged.write_bytes(b"wrong bytes")
    elif damage == "manifest":
        (root / "manifest.json").write_text("{}")
    else:
        shutil.copyfile(root / "execution.db", root / "execution-foreign.db")
    with pytest.raises(backup.OfflineBackupError):
        backup.restore_offline(
            root,
            destination=tmp_path / "target",
            expected_manifest_sha256=receipt["manifest_sha256"],
        )
    assert not (tmp_path / "target").exists()
    assert not list(tmp_path.glob(".target.offline-*"))


def test_unknown_profile_and_publish_collision_never_overwrite(source, tmp_path, monkeypatch):
    cfg = source[0]
    foreign = cfg.evidence_root / "execution-foreign.db"
    shutil.copyfile(cfg.execution_db, foreign)
    with pytest.raises(backup.OfflineBackupError, match="execution_inventory"):
        make_backup(source, tmp_path / "bad")
    foreign.unlink()
    original = backup._publish

    def raced(stage, target):
        target.mkdir()
        (target / "owner.txt").write_text("other creator")
        return original(stage, target)

    monkeypatch.setattr(backup, "_publish", raced)
    with pytest.raises(backup.OfflineBackupError, match="publish"):
        make_backup(source, tmp_path / "race")
    assert (tmp_path / "race" / "owner.txt").read_text() == "other creator"
    assert not (tmp_path / "race" / "manifest.json").exists()
    assert not list(tmp_path.glob(".race.offline-*"))


def test_live_sdk_without_host_lock_is_not_offline(tmp_path):
    async def exercise():
        provider = MeasuredProvider()
        cfg = OrchestratorConfig(evidence_root=tmp_path / "live")
        profile = RuntimeProfile("default", provider, "agent-model")
        async with Orchestrator(
            cfg, profiles={"default": profile}, provider_token_estimator=Counter(1000)
        ) as orch:
            await _mission(orch, "slow")
            runner = asyncio.create_task(orch.run())
            try:

                async def entered():
                    while provider.active != 1:
                        await asyncio.sleep(0.002)

                await asyncio.wait_for(entered(), 5)
                with pytest.raises(backup.OfflineBackupError, match="live_lease|handoff"):
                    backup.backup_offline(
                        cfg,
                        {"default": profile},
                        orch.assembled.workspaces,
                        instance_lock_path=cfg.evidence_root / ".instance.lock",
                        destination=tmp_path / "not-offline",
                        source_identity=IDENTITY,
                    )
                assert not (tmp_path / "not-offline").exists()
            finally:
                provider.release_workers.set()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("state", ["unknown", "succeeded_without_usage"])
def test_actual_uncertain_call_keeps_original_hold_receipt_and_never_replays(tmp_path, state):
    class UncertainProvider(ActualProvider):
        async def invoke(self, request, *, cancel):
            response = await super().invoke(request, cancel=cancel)
            if state == "unknown":
                raise RuntimeError("external response lost after actual invocation")
            return replace(response, usage=None)

    async def exercise():
        provider = UncertainProvider()
        cfg = OrchestratorConfig(evidence_root=tmp_path / "source")
        profile = RuntimeProfile("default", provider, "agent-model")
        async with Orchestrator(
            cfg, profiles={"default": profile}, provider_token_estimator=Counter(1000)
        ) as orch:
            mission, task = await _mission(orch, "slow")
            runtime = orch.assembled.runtime
            agent = await runtime.create(
                AgentConfig(name="uncertain", instructions="answer", model_profile_ref="default"),
                creation_key="uncertain",
            )
            attempt, intent = orch.commit.create_attempt(
                task.id,
                role="worker",
                model="agent-model",
                prompt_version="worker-v2",
                context_version="ctx",
                reservation=Reservation(6000, 0),
                input_hash="fixture",
                intent_config={
                    "runtime_profile_id": "default",
                    "agent_config": agent.config.to_json(),
                    "message": user_message_json("request"),
                    "provider_admission_fingerprint": orch._provider_admission.fingerprint,
                },
            )
            orch.commit.claim_intent(intent.intent_id, owner=orch._owner, lease_seconds=60)
            orch.commit.record_agent_created(
                intent.intent_id,
                agent_id=agent.agent_id,
                expected_turn_id=agent.turn_id_for(intent.input_id),
            )
            submitted = await agent.submit("request", input_id=intent.input_id)
            orch.commit.record_submitted(
                intent.intent_id, receipt={"turn_id": submitted.turn_id, "seq": submitted.seq}
            )

            async def wait_unknown():
                while not grants(orch.commit) or grants(orch.commit)[0]["state"] != "UNKNOWN":
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_unknown(), 5)
            orch.commit.cancel_mission(mission.id)
            await runtime.cancel_turn(
                agent.agent_id, submitted.turn_id, command_id="offline-cancel", wait_timeout=5
            )
            assert provider.calls == 1
        with sqlite3.connect(cfg.orchestrator_db) as db:
            original_holds = db.execute(
                "SELECT * FROM budget_reservations ORDER BY subject_id"
            ).fetchall()
        with sqlite3.connect(cfg.execution_db) as db:
            original_invocations = db.execute(
                "SELECT * FROM provider_invocations ORDER BY invocation_id"
            ).fetchall()
            original_events = db.execute(
                "SELECT * FROM run_events ORDER BY run_id,durable_seq"
            ).fetchall()
        receipt = backup.backup_offline(
            cfg,
            {"default": profile},
            WorkspaceManager(cfg.workspaces_root),
            instance_lock_path=cfg.evidence_root / ".instance.lock",
            destination=tmp_path / "bundle",
            source_identity=IDENTITY,
        )
        assert receipt["pending_reconciliation"]["execution.db"]
        restored = backup.restore_offline(
            tmp_path / "bundle",
            destination=tmp_path / "restored",
            expected_manifest_sha256=receipt["manifest_sha256"],
        )
        assert restored["pending_reconciliation"] == receipt["pending_reconciliation"]
        assert (
            restored["runtime_started"] is False
            and restored["external_effects_rolled_back"] is False
        )
        with sqlite3.connect(tmp_path / "restored" / "orchestrator.db") as db:
            assert (
                db.execute("SELECT * FROM budget_reservations ORDER BY subject_id").fetchall()
                == original_holds
            )
        with sqlite3.connect(tmp_path / "restored" / "execution.db") as db:
            assert (
                db.execute("SELECT * FROM provider_invocations ORDER BY invocation_id").fetchall()
                == original_invocations
            )
            assert (
                db.execute("SELECT * FROM run_events ORDER BY run_id,durable_seq").fetchall()
                == original_events
            )
        assert provider.calls == 1
        async with Orchestrator(
            replace(cfg, evidence_root=tmp_path / "restored"),
            profiles={"default": profile},
            provider_token_estimator=Counter(1000),
        ) as recovered:
            await asyncio.wait_for(recovered.run(), 10)
            assert recovered.store.get_mission(mission.id).status is MissionStatus.CANCELLED
            assert grants(recovered.commit)[0]["state"] == "UNKNOWN"
            actual_holds = recovered.store.connection.execute(
                "SELECT * FROM budget_reservations ORDER BY subject_id"
            ).fetchall()
            assert [tuple(row) for row in actual_holds] == original_holds
            assert provider.calls == 1  # ordinary recovery must not replay the uncertain call

    asyncio.run(exercise())


def test_partial_backup_and_restore_failures_publish_nothing(source, tmp_path, monkeypatch):
    original = backup._copy_database

    def fail_second(path, target):
        if path.name == "execution.db":
            raise OSError("injected storage failure")
        original(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(backup, "_copy_database", fail_second)
        with pytest.raises(backup.OfflineBackupError, match="storage failure"):
            make_backup(source, tmp_path / "partial")
    assert not (tmp_path / "partial").exists()
    assert not list(tmp_path.glob(".partial.offline-*"))
    receipt = make_backup(source, tmp_path / "bundle")
    update = Store.update_artifact_storage

    def fail_after_update(store, changes):
        update(store, changes)
        raise OSError("derived row update failure")

    monkeypatch.setattr(Store, "update_artifact_storage", fail_after_update)
    with pytest.raises(backup.OfflineBackupError, match="derived row update failure"):
        backup.restore_offline(
            tmp_path / "bundle",
            destination=tmp_path / "partial-restore",
            expected_manifest_sha256=receipt["manifest_sha256"],
        )
    assert not (tmp_path / "partial-restore").exists()
    assert not list(tmp_path.glob(".partial-restore.offline-*"))
    assert (
        hashlib.sha256((tmp_path / "bundle" / "manifest.json").read_bytes()).hexdigest()
        == receipt["manifest_sha256"]
    )
