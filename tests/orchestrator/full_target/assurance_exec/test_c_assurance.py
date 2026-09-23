# SPDX-License-Identifier: Apache-2.0
"""C group (consistency / concurrency / recovery / migration): plan cases C01–C06.

C01 and C02 run in-process over the real Store/Commit (C01 on the assured
fixture runtime with a scripted reviewer: original critic entry, official
record, current ACCEPT certificate, original acceptance writer). C03–C05 run the
item 6/7/8 seams (real Orchestrator + install_assurance, real AssuranceTick) as
child processes and assert on their reports; C04 adds the cursor atomicity
counter-case in-process. C06 migrates a real pre-Assurance library. No model, no
Host (C07 is the Host/UI half, see test_c07_host_api.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from _deploy import PRINCIPAL, deployment, emit_notification, spec

from agent_orchestrator.assurance.certificates import UseCertificate
from agent_orchestrator.assurance.codec import AssuranceError
from agent_orchestrator.assurance.evidence import ReadItem
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts.evidence_state import ObservationRecord, QueryCompleteness
from agent_orchestrator.contracts.models import Budget, Event, Mission, MissionStatus
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.assurance_reads import read_epochs_locked, require_epochs_locked
from agent_orchestrator.storage.assurance_store import AssuranceStore
from agent_orchestrator.storage.assurance_work import AssuranceWorkStore, WorkTarget
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.store import (
    InjectedCrash,
    SchemaIncompatible,
    Store,
    StoreConflict,
    StoreError,
)

SDK_ROOT = Path(__file__).resolve().parents[4]
SEAMS = SDK_ROOT / "scripts/assurance_seams"
HASH = "a" * 64
NOW = 1_700_000_000_000


def _count(store, sql, *params):
    return store.connection.execute(sql, params).fetchone()[0]


def _seam(name):
    completed = subprocess.run([sys.executable, str(SEAMS / name)], capture_output=True, text=True, timeout=900,
                               cwd=str(SDK_ROOT))
    assert completed.returncode == 0, (name, completed.stderr[-4000:])
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    assert summary["status"] == "PASS", summary
    return json.loads(Path(summary["evidence"]).read_text())


# --------------------------------------------------------------------------- C01
def test_acceptance_atomic_faults(tmp_path):
    sys.path.insert(0, str(SEAMS))
    from _assured_fixture import AssuredRuntime, count  # noqa: E402  (real fixture runtime)

    accept_reply = {"schema_version": 2, "verdict": "ACCEPT", "assessments": [
        {"criterion_id": "criterion-report", "verdict": "PASS", "evidence_ids": [], "reason": "fixture", "limitations": []}],
        "findings": []}

    async def body():
        async with AssuredRuntime(tmp_path, [accept_reply]) as rt:
            store, mission_id = rt.store, rt.mission.id
            verdict, record = await rt.run_critic()
            assert verdict.passed
            rt.record_critic_layer(record)
            rt.settle_fixture_worker()
            certificates = "SELECT COUNT(*) FROM assurance_use_certificates"
            acceptances = "SELECT COUNT(*) FROM acceptances WHERE mission_id=?"
            receipts = "SELECT COUNT(*) FROM commit_receipts WHERE kind='AssuranceUseCertified'"
            usage_rows = "SELECT COUNT(*) FROM imported_usage"
            usage_before = count(store, usage_rows) if store.has_table("imported_usage") else None

            def untouched():
                assert count(store, certificates) == 0 and count(store, acceptances, mission_id) == 0
                assert count(store, receipts) == 0
                assert store.count_events(mission_id, "AssuranceUseCertified") == 0
                assert store.count_events(mission_id, "AcceptanceCommitted") == 0
                assert count(store, "SELECT COUNT(*) FROM operation_acceptance_scopes WHERE mission_id=?", mission_id) == 0
                assert not store.connection.in_transaction

            # Cut 1: the certificate write itself fails inside the acceptance UoW.
            with patch.object(AssuranceStore, "record_certificate", side_effect=OSError("cut: certificate")):
                with pytest.raises(OSError):
                    rt.accept_now()
            untouched()
            # Cut 2: the receipt insert fails after the certificate row was written.
            with patch.object(Store, "insert_receipt", side_effect=OSError("cut: receipt")):
                with pytest.raises(OSError):
                    rt.accept_now()
            untouched()
            # Cut 3: the original event emit for the acceptance fails.
            original_emit = rt.commit._emit

            def failing_emit(kind, *args, **kwargs):
                if kind == "AcceptanceCommitted":
                    raise OSError("cut: event")
                return original_emit(kind, *args, **kwargs)

            with patch.object(rt.commit, "_emit", failing_emit):
                with pytest.raises(OSError):
                    rt.accept_now()
            untouched()
            # Cut 4: the process "dies" at the original accept fault point (inside the accept transaction).
            store.arm("after_accept_before_supersede")
            with pytest.raises(InjectedCrash):
                rt.accept_now()
            assert store.fired == ["after_accept_before_supersede"]
            untouched()
            # The real path commits Acceptance + Contribution + certificate + receipt + event together.
            completed = rt.accept_now()
            assert completed.accepted_result_id == rt.stored.envelope.id
            assert count(store, certificates) == 1 and count(store, acceptances, mission_id) == 1
            assert count(store, receipts) == 1 and store.count_events(mission_id, "AssuranceUseCertified") == 1
            assert store.count_events(mission_id, "AcceptanceCommitted") == 1
            # Replay of the same command: the same receipt, no second certificate, event or charge.
            replay = rt.accept_now()
            assert getattr(replay, "replayed", True) and replay.accepted_result_id == completed.accepted_result_id
            assert count(store, certificates) == 1 and count(store, receipts) == 1
            assert store.count_events(mission_id, "AssuranceUseCertified") == 1
            assert store.count_events(mission_id, "AcceptanceCommitted") == 1
            rt.settle_fixture_worker()  # the same usage fact again is not a second charge
            if usage_before is not None:
                assert count(store, usage_rows) == usage_before
            assert rt.provider.calls == 1

    asyncio.run(body())


# --------------------------------------------------------------------------- C02
def test_two_connection_concurrency(tmp_path):
    async def checks(world, assured):
        first = world.store
        second = Store.open(first.path)  # an independent connection to the same library
        try:
            epochs = read_epochs_locked(second.connection, assured.id) if second.connection.in_transaction else None
            with second.read_view() as connection:
                epochs = read_epochs_locked(connection, assured.id)
            cert = UseCertificate(
                mission_id=assured.id, consumer_kind="result", consumer_id="result-c02", scope_id="scope-c02",
                principal_id=PRINCIPAL.principal_id, purpose="ACCEPT", truth="TRUE", freshness="CURRENT",
                availability="READABLE", decision="USABLE", coverage="COMPLETE", policy_ref=Pin("policy-1", 1, HASH),
                read_set=(ReadItem("OBJECT", "o", HASH), ReadItem("QUERY_SET", "q", HASH), ReadItem("ACCESS", "a", HASH),
                          ReadItem("POLICY", "p", HASH)),
                clean_support_refs=(AssuranceRef("review", Pin("rec", 1, HASH)),), issued_at_ms=NOW, not_after_ms=None,
                reasons=("fixture",), mission_epoch=epochs.mission, environment_epoch=epochs.environment,
                clock_generation=epochs.clock_generation, root_incarnation_id="root-c02")
            other_body = UseCertificate.from_json({**cert.to_json(), "consumer_id": "result-other"})
            # Same command id from two writers: one adoption, an identical replay is a
            # read, a different body under the same id is a conflict (never REPLACE).
            assert AssuranceStore(second).record_certificate("cert-c02", cert) is True
            assert AssuranceStore(first).record_certificate("cert-c02", cert) is False
            with pytest.raises(AssuranceError) as raised:
                AssuranceStore(first).record_certificate("cert-c02", other_body)
            assert raised.value.code == "IMMUTABLE_IDENTITY_CONFLICT"
            assert _count(first, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", assured.id) == 1
            # Two consumers ingesting the same page: the cursor CAS admits one.
            emit_notification(world, assured)
            version = first.connection.execute(
                "SELECT row_version FROM assurance_event_cursors WHERE mission_id=? AND consumer='REVIEW'",
                (assured.id,)).fetchone()["row_version"]
            target = WorkTarget("review-c02", HASH)
            now = int(first.now * 1000)
            AssuranceWorkStore(first).ingest(assured.id, "REVIEW", expected_version=version,
                                             classify=lambda e, c: (target,), now_ms=now)
            with pytest.raises(StoreConflict):
                AssuranceWorkStore(second).ingest(assured.id, "REVIEW", expected_version=version,
                                                  classify=lambda e, c: (target,), now_ms=now)
            assert _count(second, "SELECT COUNT(*) FROM assurance_pending_work WHERE mission_id=? AND consumer='REVIEW'",
                          assured.id) == 1
            # Revocation racing an acceptance: the counter-source lands on the other
            # connection while the accept holds its captured epochs -> latest-source guard.
            with first.read_view() as connection:
                captured = read_epochs_locked(connection, assured.id)
                HtnStore(second).insert_observation(assured.id, ObservationRecord(
                    observation_id="obs-c02-revoke", proposition_key="fixture.revoked#1", polarity=False,
                    source_ref=TypedRef(kind=TypedRefKind.SOURCE, id="src-c02", revision=1, content_hash=HASH),
                    observed_at_ms=now, recorded_at_ms=now, coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE,
                    coverage_scope="scope-c02", query_watermark_ms=now, observer_id="observer-c02"))
            with first.read_view() as connection:
                with pytest.raises(AssuranceError) as raised:
                    require_epochs_locked(connection, assured.id, captured, now_ms=now)
                assert raised.value.code == "RECHECK_REQUIRED"
            # A held write lock on one connection makes the other wait, then both writes land (no lost update).
            second.connection.execute("BEGIN IMMEDIATE")
            second.connection.execute("INSERT INTO assurance_pending_work(mission_id,consumer,work_key,trigger_event_id,"
                                      "target_epoch,target_fingerprint,state,row_version,tries,not_before_ms) "
                                      "SELECT mission_id,'CLOSEOUT','lock-c02',trigger_event_id,target_epoch,"
                                      "target_fingerprint,'PENDING',1,0,not_before_ms FROM assurance_pending_work "
                                      "WHERE mission_id=? AND consumer='REVIEW'", (assured.id,))
            outcome = {}

            def contended():
                started = time.monotonic()
                try:
                    AssuranceWorkStore(first).ingest(assured.id, "VALIDITY", expected_version=1,
                                                     classify=lambda e, c: (target,), now_ms=now)
                    outcome["result"] = "ok"
                except (StoreError, sqlite3.OperationalError) as error:
                    outcome["result"] = type(error).__name__
                outcome["waited"] = time.monotonic() - started

            worker = threading.Thread(target=contended)
            worker.start()
            time.sleep(0.3)
            assert "result" not in outcome  # blocked behind the other writer
            second.connection.execute("COMMIT")
            worker.join(timeout=10)
            assert outcome["result"] == "ok" and outcome["waited"] >= 0.25
            rows = second.connection.execute(
                "SELECT consumer,work_key FROM assurance_pending_work WHERE mission_id=? ORDER BY consumer",
                (assured.id,)).fetchall()
            assert [tuple(r) for r in rows] == [("CLOSEOUT", "lock-c02"), ("REVIEW", "review-c02"), ("VALIDITY", "review-c02")]
            assert world.sent == []  # no side effect left the process
        finally:
            second.close()

    async def body():
        async with deployment(tmp_path) as world:
            assured, _ = world.commit.create_mission(spec("assured-c02"))
            await checks(world, assured)

    asyncio.run(body())


# --------------------------------------------------------------------------- C03
def test_review_cold_resume():
    report = _seam("recovery-seam.py")
    lease = report["lease_recovery"]
    assert lease["dead_claim"]["state"] == "RUNNING" and lease["dead_claim"]["owner"] == "crashed-runner"
    assert lease["after_lease_elapsed"]["state"] == "DONE" and lease["after_lease_elapsed"]["owner"] is None
    assert lease["after_lease_elapsed"]["tries"] >= lease["dead_claim"]["tries"]
    assert lease["ordinals"] == [1]  # the committed invocation is reused; no second model call
    assert report["new_pin"]["provider_calls"] <= lease["provider_calls"] == report["provider_calls"]  # no call after recovery
    orphan = report["orphan_pin"]
    assert orphan["before"][0]["state"] == "PREPARING" and orphan["blob_still_present"] is True
    assert orphan["rolled_back_startup"]["clock_state"] == "ROLLBACK" and orphan["rolled_back_startup"]["pins_released"] == []
    assert orphan["rolled_back_startup"]["pins_release_deferred"] == [orphan["before"][0]["pin_id"]]
    assert orphan["startup"]["pins_released"] == [orphan["before"][0]["pin_id"]]
    states = [pin["state"] for pin in report["new_pin"]["pins"]]
    assert states[:2] == ["RELEASED", "BOUND"] and set(states[2:]) <= {"BOUND"}  # the orphan, its new identity, later reviews
    assert report["restart_rollback"]["run2_startup"]["clock_state"] == "ROLLBACK"
    assert report["restart_rollback"]["work_during_rollback"] and report["clock_rollback"]["after"]["clock_state"] == "STABLE"


# --------------------------------------------------------------------------- C04
def test_event_cursor_atomicity(tmp_path):
    async def body():
        async with deployment(tmp_path) as world:
            assured, _ = world.commit.create_mission(spec("assured-c04"))
            store = world.store
            work = AssuranceWorkStore(store)
            emit_notification(world, assured)
            emit_notification(world, assured)
            cursor = store.connection.execute(
                "SELECT last_event_seq,row_version FROM assurance_event_cursors WHERE mission_id=? AND consumer='CLOSEOUT'",
                (assured.id,)).fetchone()
            seen = []

            def classify(event, consumer):
                seen.append(event.seq)
                if len(seen) == 2:
                    raise OSError("exit before ACK")
                return (WorkTarget("closeout-c04", HASH),)

            with pytest.raises(OSError):
                work.ingest(assured.id, "CLOSEOUT", expected_version=cursor["row_version"], classify=classify,
                            now_ms=int(store.now * 1000))
            after = store.connection.execute(
                "SELECT last_event_seq,row_version FROM assurance_event_cursors WHERE mission_id=? AND consumer='CLOSEOUT'",
                (assured.id,)).fetchone()
            assert tuple(after) == tuple(cursor)  # nothing ACKed before the work was durable
            assert _count(store, "SELECT COUNT(*) FROM assurance_pending_work WHERE mission_id=? AND consumer='CLOSEOUT'",
                          assured.id) == 0
            # A lost cursor is rebuilt explicitly at startup, never guessed at ingest.
            with pytest.raises(AssuranceError) as raised:
                AssuranceWorkStore(store).ingest("mission-none", "CLOSEOUT", expected_version=1,
                                                 classify=lambda e, c: (), now_ms=int(store.now * 1000))
            assert raised.value.code == "CURSOR_UNINITIALIZED"
    asyncio.run(body())
    report = _seam("four-consumer-seam.py")
    install = report["production_install"]
    assert install["idle_rounds"] == 0  # no self-triggering when nothing changed
    assert install["cursor_rebuild"]["missions"] == 1
    assert len(install["sent"]) == 1 and report["notify"]["receipt"] is True
    assert set(install["cursor_seq"]) == {"REVIEW", "VALIDITY", "CLOSEOUT", "NOTIFY"}
    assert install["lanes"] == {"assured": "ASSURANCE_1_1", "legacy": "LEGACY", "v1_unselected": "COMPLETION_V1"}


# --------------------------------------------------------------------------- C05
def test_closeout_and_notification():
    report = _seam("final-writer-seam.py")
    assert report["judged"]["mission_status"] == "ACTIVE"  # judge records, never finalizes
    assert report["judged"]["closeout"]["state"] == "DRAINING"
    assert {"OPEN_INTENTS", "OPEN_RESERVATIONS"} <= set(report["judged"]["closeout"]["reasons"])
    assert report["draining"]["open"]["settle_refusal"]
    contract = report["final_writer_contract"]
    assert contract["row"]["state"] == "FINALIZED" and contract["mission_status"] == "COMPLETED"
    assert contract["receipt"]["final_event_type"] == "MissionCompleted"
    assert contract["refusals"] and contract["post_final"]
    assert len(contract["sent"]) == 1 and len(contract["notification_requests"]) >= 1
    install = report["production_install"]
    assert install["finalizer"] == "assurance_final_writer.finalize_assured_mission"
    assert len(install["sent"]) == 1 and install["cancel_notice"][0]["final_event_type"] == "MissionCancelled"
    assert install["legacy_notice"] == []


# --------------------------------------------------------------------------- C06
def _legacy_library(path, versions):
    connection = sqlite3.connect(path, isolation_level=None, timeout=5.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    store = Store(connection, Path(path), time.time)
    with store.transaction() as tx:
        for migration in schema.MIGRATIONS[:versions]:
            store._apply_migration(tx, migration)
    mission = Mission("mission-legacy", "legacy goal", ("c",), (), (), "low", Budget(), "tenant-legacy",
                      MissionStatus.CREATED, 1.0, 1, "legacy-key")
    store.insert_mission(mission, spec_hash="b" * 64)
    for index in range(3):
        store.append_event(Event(f"event-legacy-{index}", "MissionCreated" if index == 0 else "MissionNote",
                                 "trace-legacy", mission.id, None, None, "system", "legacy",
                                 {"index": index}, f"legacy-{index}", 1.0 + index))
    return store


def _dump(connection, tables):
    return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables}


def _ddl_hash(connection):
    rows = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps([list(row) for row in rows], sort_keys=True).encode()).hexdigest()


def test_real_migration_and_legacy(tmp_path):
    assurance = schema.MIGRATIONS[-1]
    assert assurance.name == "orchestrator-assurance-exec-v1.1"
    legacy_path = tmp_path / "legacy.db"
    legacy = _legacy_library(str(legacy_path), len(schema.MIGRATIONS) - 1)
    legacy_tables = ("missions", "events", "orch_schema_migrations")
    before = _dump(legacy.connection, legacy_tables)
    ddl_before = {row["name"]: row["sql"] for row in legacy.connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")}
    assert not legacy.has_table("assurance_use_certificates")
    legacy.close()
    # Upgrade: the real migration chain applies the Assurance migration on top, with a backup.
    upgraded = Store.open(legacy_path)
    assert upgraded.has_table("assurance_use_certificates") and upgraded.has_table("assurance_environment_state")
    after = _dump(upgraded.connection, ("missions", "events"))
    assert after["missions"] == before["missions"] and after["events"] == before["events"]
    migrations = [tuple(row) for row in upgraded.connection.execute(
        "SELECT version,name,checksum FROM orch_schema_migrations ORDER BY version")]
    assert migrations[:-1] == [row[:3] for row in before["orch_schema_migrations"]]
    assert migrations[-1] == (assurance.version, assurance.name, assurance.checksum)
    for name, ddl in ddl_before.items():
        current = upgraded.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        assert current is not None and current[0] == ddl, name  # legacy DDL untouched
    backup = legacy_path.with_name(f"{legacy_path.name}.pre-schema-{assurance.version}.backup")
    assert backup.is_file()
    # The legacy Mission keeps its lane; reopen is idempotent; a full new library matches.
    # A pre-Assurance Mission has no creation contract: it is never adopted into a
    # lane by the migration itself (deployment reconciliation classifies it LEGACY).
    with pytest.raises(AssuranceError) as raised:
        AssuranceStore(upgraded).lane("mission-legacy")
    assert raised.value.code == "CREATION_CONTRACT_UNRESOLVED"
    ddl_after = _ddl_hash(upgraded.connection)
    upgraded.close()
    reopened = Store.open(legacy_path)
    assert _ddl_hash(reopened.connection) == ddl_after
    assert _dump(reopened.connection, ("missions", "events")) == after
    fresh = Store.open(tmp_path / "fresh.db")
    assert _ddl_hash(fresh.connection) == ddl_after
    # Triggers: an inventoried source table refuses REPLACE-style rewrites of Assurance events.
    from agent_orchestrator.assurance.event_kinds import EVENT_REF_KINDS
    source_kind = sorted(EVENT_REF_KINDS.values())[0]
    with pytest.raises(sqlite3.IntegrityError):
        reopened.connection.execute("UPDATE events SET type=? WHERE event_id='event-legacy-1'", (source_kind,))
    # FK: Assurance rows cannot reference a Mission that does not exist.
    with pytest.raises(sqlite3.IntegrityError):
        reopened.connection.execute(
            "INSERT INTO assurance_use_certificates(certificate_id,mission_id,consumer_kind,consumer_id,purpose,scope_id,"
            "read_set_hash,certificate_hash,certificate_json,issued_at_ms,not_after_ms) VALUES('c','mission-none','r','r',"
            "'ACCEPT','s',?,?,'{}',1,NULL)", (HASH, HASH))
    reopened.close()
    fresh.close()
    # Wrong checksum: the library is refused, not silently re-migrated.
    tampered = sqlite3.connect(legacy_path)
    tampered.execute("UPDATE orch_schema_migrations SET checksum='0' WHERE version=?", (assurance.version,))
    tampered.commit()
    tampered.close()
    with pytest.raises(SchemaIncompatible):
        Store.open(legacy_path)
    # A failing migration rolls the whole upgrade back: the legacy library stays at its version.
    second = tmp_path / "legacy-2.db"
    _legacy_library(str(second), len(schema.MIGRATIONS) - 1).close()
    broken = schema.Migration(assurance.version, assurance.name,
                              assurance.ddl + "\nCREATE TABLE broken(x INTEGER REFERENCES nope(y));\nINSERT INTO broken VALUES(1);")
    with patch.object(schema, "MIGRATIONS", (*schema.MIGRATIONS[:-1], broken)):
        with pytest.raises(sqlite3.Error):
            Store.open(second)
    rows = sqlite3.connect(second).execute("SELECT MAX(version) FROM orch_schema_migrations").fetchone()[0]
    assert rows == assurance.version - 1
    tables = {row[0] for row in sqlite3.connect(second).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "assurance_use_certificates" not in tables and "broken" not in tables
    again = Store.open(second)
    assert again.has_table("assurance_use_certificates")
    again.close()
    # Kill: a writer killed inside an open transaction leaves nothing half-written.
    third = tmp_path / "legacy-3.db"
    _legacy_library(str(third), len(schema.MIGRATIONS)).close()
    script = (
        "import sqlite3,sys,time,os\n"
        f"c=sqlite3.connect({str(third)!r},isolation_level=None)\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "for i in range(2000): c.execute(\"INSERT INTO events(event_id,idempotency_key,type,trace_id,mission_id,task_id,attempt_id,actor_type,actor_id,payload_json,created_at,schema_version) VALUES(?,?,'MissionNote','t','mission-legacy',NULL,NULL,'system','kill','{}',1.0,1)\",(f'kill-{i}',f'kill-{i}'))\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(30)\n")
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    survivor = Store.open(third)
    assert survivor.count_events("mission-legacy") == 3
    assert _ddl_hash(survivor.connection) == ddl_after
    survivor.close()
