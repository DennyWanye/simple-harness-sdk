"""O4 observer oracles: inspect every Mission, including deliberate bad fixtures.

No nested pytest invocation. These exercise the plugin against real Store and
CommitService; intentional findings stay in the report, never become exclusions.
"""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_orchestrator.contracts import Event
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.storage.store import Store


@pytest.fixture
def audit():
    path = Path(__file__).parents[1] / "p33_replay_audit.py"
    spec = importlib.util.spec_from_file_location("p33_replay_audit_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observer = module.ReplayAudit()
    observer.install()
    observer.nodeid = "oracle::normal_and_negative"
    try:
        yield observer
    finally:
        observer.uninstall()


def mission(store, key):
    return CommitService(store).create_mission(
        MissionSpec(
            goal="审计", success_criteria=("file:x.md",), tenant_id="t", idempotency_key=key
        )
    )[0]


def probe(store, mid, key="probe", kind="Probe"):
    store.append_event(
        Event(
            id=key,
            type=kind,
            trace_id="trace",
            mission_id=mid,
            task_id=None,
            attempt_id=None,
            actor_type="system",
            actor_id="oracle",
            payload={},
            idempotency_key=key,
            created_at=1.0,
        )
    )


def rows(audit, mid):
    return [row for row in audit.report()["observations"] if row["mission_id"] == mid]


def test_close_and_unclosed_teardown_both_capture_clean_real_missions(tmp_path, audit):
    first = Store.open(tmp_path / "first.db")
    a = mission(first, "a")
    first.close()
    second = Store.open(tmp_path / "second.db")
    b = mission(second, "b")
    try:
        audit.sweep("teardown")
        assert {row["mission_id"] for row in audit.report()["observations"]} == {a.id, b.id}
        assert all(
            row["comparison"]["coverage"] == 1 for row in rows(audit, a.id) + rows(audit, b.id)
        )
        assert audit.report()["gate"] == "PASS"
    finally:
        second.close()


def test_unknown_and_formal_drift_are_not_a_whole_test_exemption(tmp_path, audit):
    store = Store.open(tmp_path / "all.db")
    a, b = mission(store, "negative"), mission(store, "positive")
    probe(store, a.id)
    damaged = {**a.to_json(), "status": "PLANNING"}
    store.connection.execute(
        "UPDATE missions SET status='PLANNING', json=? WHERE mission_id=?",
        (json.dumps(damaged), a.id),
    )
    store.close()
    [bad], [good] = rows(audit, a.id), rows(audit, b.id)
    assert bad["unknown_event_types"] == {"Probe": 1}
    assert bad["comparison"]["mismatches"]
    assert good["unknown_event_types"] == {} and good["comparison"]["mismatches"] == []
    assert bad["nodeid"] == good["nodeid"] == "oracle::normal_and_negative"
    assert audit.report()["gate"] == "OPEN"


def test_corrupt_mission_json_does_not_hide_its_events_or_other_missions(tmp_path, audit):
    store = Store.open(tmp_path / "broken.db")
    a, b = mission(store, "bad-json"), mission(store, "good")
    probe(store, a.id)
    store.connection.execute("UPDATE missions SET json='{}' WHERE mission_id=?", (a.id,))
    store.close()
    [bad] = rows(audit, a.id)
    assert bad["unknown_event_types"] == {"Probe": 1}
    assert any(error["stage"] == "snapshot" for error in bad["errors"])
    assert rows(audit, b.id)[0]["comparison"]["coverage"] == 1
    assert audit.report()["gate"] == "OPEN"


def test_events_without_mission_row_remain_in_all_mission_inventory(tmp_path, audit):
    store = Store.open(tmp_path / "orphan.db")
    a = mission(store, "owner")
    store.connection.execute("PRAGMA foreign_keys=OFF")
    store.connection.execute("UPDATE events SET mission_id='orphan' WHERE mission_id=?", (a.id,))
    store.close()
    assert rows(audit, a.id) and rows(audit, "orphan")
    assert rows(audit, "orphan")[0]["errors"]
    assert audit.report()["gate"] == "OPEN"


def test_memory_and_same_path_reopen_preserve_nodeid_and_do_not_double_count_unknowns(
    tmp_path, audit
):
    memory = Store.open(":memory:")
    a = mission(memory, "memory")
    memory.close()
    path = tmp_path / "reopen.db"
    store = Store.open(path)
    b = mission(store, "disk")
    probe(store, b.id)
    audit.sweep("teardown")
    store.close()
    audit.nodeid = "oracle::reopen"
    Store.open(path).close()
    report = audit.report()
    assert rows(audit, a.id)
    assert {row["nodeid"] for row in rows(audit, b.id)} == {
        "oracle::normal_and_negative",
        "oracle::reopen",
    }
    assert report["mission_unknown_event_types"] == {"Probe": 1}
    assert report["raw_unknown_event_types"] == {"PolicySeeded": 2, "Probe": 1}


def test_deleted_export_does_not_exempt_the_complete_original_database(tmp_path, audit):
    store = Store.open(tmp_path / "complete.db")
    a = mission(store, "complete")
    (tmp_path / "partial.jsonl").write_text("")
    store.close()
    [row] = rows(audit, a.id)
    assert row["event_count"] > 0 and row["comparison"]["coverage"] == 1
    assert audit.report()["gate"] == "PASS"


def test_scan_uses_all_events_beyond_store_default_page(tmp_path, audit):
    store = Store.open(tmp_path / "many.db")
    a = mission(store, "many")
    # Real persisted events, not a mock list_events that could mask truncation.
    with store.transaction():
        for ordinal in range(10_001):
            probe(store, a.id, f"probe-{ordinal}")
    store.close()
    [row] = rows(audit, a.id)
    assert row["event_count"] > 10_001
    assert row["unknown_event_types"] == {"Probe": 10_001}
    assert audit.report()["mission_unknown_event_types"] == {"Probe": 10_001}
    assert audit.report()["raw_unknown_event_types"] == {"PolicySeeded": 1, "Probe": 10_001}


def test_observer_does_not_persist_raw_event_payloads(tmp_path, audit):
    store = Store.open(tmp_path / "payload.db")
    a = mission(store, "payload")
    probe(store, a.id)
    marker = "private-payload-do-not-export"
    store.connection.execute(
        "UPDATE events SET payload_json=? WHERE type='Probe'", (json.dumps({"private": marker}),)
    )
    before = store.connection.total_changes
    audit.sweep("teardown")
    assert store.connection.total_changes == before
    assert marker not in json.dumps(audit.report())
    store.close()


def test_deployment_stream_is_retained_and_checked_by_its_real_registry_scope(tmp_path, audit):
    store = Store.open(tmp_path / "deployment.db")
    a = mission(store, "normal")
    store.close()
    report = audit.report()
    assert {row["mission_id"] for row in report["observations"]} == {a.id}
    [global_stream] = report["global_streams"]
    assert global_stream["stream_id"] == "deployment"
    assert "mission_id" not in global_stream
    assert global_stream["event_types"] == {"PolicySeeded": 1}
    assert global_stream["registry_consistency"] == []
    assert global_stream["unknown_event_types"] == {}
    assert global_stream["gaps"] == []
    assert {gap["rule"] for gap in global_stream["raw_projection_gaps"]} == {
        "mission_created_missing"
    }
    assert report["raw_unknown_event_types"] == {"PolicySeeded": 1}
    assert report["mission_unknown_event_types"] == report["global_unknown_event_types"] == {}
    assert report["gate"] == "PASS"


@pytest.mark.parametrize("damage", ["unknown_global", "policy_on_mission", "registry_drift"])
def test_scope_routing_does_not_hide_unknowns_or_global_formal_drift(tmp_path, audit, damage):
    store = Store.open(tmp_path / "scope.db")
    a = mission(store, "normal")
    if damage == "unknown_global":
        probe(store, "deployment")
    elif damage == "policy_on_mission":
        probe(store, a.id, kind="PolicySeeded")
    else:
        store.set_policy_version_status(store.active_policy()["version_id"], "RETIRED")
    store.close()
    report = audit.report()
    if damage == "unknown_global":
        assert report["global_unknown_event_types"] == {"Probe": 1}
        assert report["raw_unknown_event_types"] == {"PolicySeeded": 1, "Probe": 1}
    elif damage == "policy_on_mission":
        assert report["mission_unknown_event_types"] == {"PolicySeeded": 1}
        assert report["raw_unknown_event_types"] == {"PolicySeeded": 2}
    else:
        assert report["global_streams"][0]["registry_consistency"]
    assert report["gate"] == "OPEN"


def test_discovery_finds_real_subprocess_mission_without_parent_store_open(tmp_path, audit):
    path = tmp_path / "child" / "orchestrator.db"
    code = """
import sys
from agent_orchestrator.storage.store import Store
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
s = Store.open(sys.argv[1])
m, _ = CommitService(s).create_mission(MissionSpec(
    goal='child', success_criteria=('file:x.md',), tenant_id='t', idempotency_key='child'))
print(m.id)
s.close()
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    mid = child.stdout.strip().splitlines()[-1]
    assert rows(audit, mid) == []  # no parent Store registration
    audit.discover([tmp_path], "after_teardown")
    [row] = rows(audit, mid)
    assert row["acquisition"] == "discovered"
    assert row["comparison"]["coverage"] == 1 and row["errors"] == []
    assert audit.report()["global_streams"]  # do not drop the child's policy timeline
    assert audit.report()["gate"] == "PASS"


def test_discovery_keeps_direct_sqlite_bad_mission_without_migrating_it(tmp_path, audit):
    path = tmp_path / "manual.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE missions(mission_id TEXT, json TEXT)")
        connection.execute("INSERT INTO missions VALUES ('manual', '{}')")
    before = path.read_bytes()
    audit.discover([tmp_path], "after_teardown")
    [row] = rows(audit, "manual")
    assert row["acquisition"] == "discovered" and row["errors"]
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert [
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ] == ["missions"]
    assert audit.report()["gate"] == "OPEN"


def test_discovery_reports_previously_observed_database_deletion(tmp_path, audit):
    path = tmp_path / "deleted.db"
    store = Store.open(path)
    a = mission(store, "deleted")
    store.close()
    path.unlink()
    audit.discover([tmp_path], "after_teardown")
    report = audit.report()
    assert rows(audit, a.id)  # keep the earlier snapshot, too
    assert any(e["error"] == "deleted_database" for e in report["store_errors"])
    assert report["gate"] == "OPEN"


def test_bad_event_json_keeps_raw_unknown_detection_and_decode_error(tmp_path, audit):
    store = Store.open(tmp_path / "bad-event.db")
    a = mission(store, "bad-event")
    probe(store, a.id)
    store.connection.execute("UPDATE events SET payload_json='{' WHERE type='MissionCreated'")
    store.close()
    report = audit.report()
    [row] = rows(audit, a.id)
    assert row["event_types"]["Probe"] == 1
    assert row["event_scan_complete"] is False and row["errors"]
    assert report["raw_unknown_event_types"] == {"PolicySeeded": 1, "Probe": 1}
    assert report["mission_unknown_event_types"] == {"Probe": 1}
    assert report["unknown_scan_complete"] is False and report["gate"] == "OPEN"


def test_global_finding_resolves_to_exact_exported_observation(tmp_path, audit):
    store = Store.open(tmp_path / "lookup.db")
    mission(store, "lookup")
    probe(store, "deployment")
    store.close()
    report = audit.report()
    [finding] = report["findings"]
    [row] = [
        r for r in report["global_streams"] if r["observation_id"] == finding["observation_id"]
    ]
    assert row["stream_id"] == "deployment" and row["unknown_event_types"] == {"Probe": 1}


def test_discovery_reads_committed_wal_from_live_child_without_parent_store_open(tmp_path, audit):
    path, ready = tmp_path / "wal.db", tmp_path / "ready.json"
    code = """
import json, sys
from pathlib import Path
from agent_orchestrator.storage.store import Store
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
s = Store.open(sys.argv[1])
m, _ = CommitService(s).create_mission(MissionSpec(
    goal='wal child', success_criteria=('file:x.md',), tenant_id='t', idempotency_key='wal'))
Path(sys.argv[2]).write_text(json.dumps({'mission_id': m.id}))
sys.stdin.readline()
s.close()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child did not create its committed Mission"
        mid = json.loads(ready.read_text())["mission_id"]
        assert Path(str(path) + "-wal").stat().st_size > 0
        assert rows(audit, mid) == []
        audit.discover([tmp_path], "after_teardown")
        [row] = rows(audit, mid)
        assert row["acquisition"] == "discovered" and row["comparison"]["coverage"] == 1
        assert audit.report()["gate"] == "PASS"
    finally:
        try:
            child.communicate("done\n", timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            raise
    assert child.returncode == 0


def test_observed_capture_is_consistent_when_other_connection_commits_between_reads(
    tmp_path, audit, monkeypatch
):
    path = tmp_path / "race.db"
    reader = Store.open(path)
    a = mission(reader, "race")
    writer = Store.open(path)
    namespace = audit.capture.__func__.__globals__
    original = namespace["events_from_store"]
    committed = []

    def commit_between_reads(store, mid):
        events = original(store, mid)
        if store is reader and mid == a.id and not committed:
            # A legitimate atomic commit from another connection after the event
            # read, before Store.snapshot. No fixture-only table manipulation.
            CommitService(writer).begin_planning(a.id)
            committed.append(True)
        return events

    monkeypatch.setitem(namespace, "events_from_store", commit_between_reads)
    try:
        audit.capture(reader, "between_reads")
        assert committed == [True]
        [row] = [r for r in rows(audit, a.id) if "between_reads" in r["phases"]]
        assert row["comparison"]["coverage"] == 1 and row["comparison"]["mismatches"] == []
        assert row["observer_owned_read_snapshot"] is True
        assert row["in_transaction"] is False
        assert reader.connection.in_transaction is False
        assert str(reader.get_mission(a.id).status) == "PLANNING"
        assert audit.report()["gate"] == "PASS"
    finally:
        reader.close()
        writer.close()


def test_observer_does_not_commit_or_hide_a_producer_open_transaction(tmp_path, audit):
    store = Store.open(tmp_path / "transaction.db")
    a = mission(store, "transaction")
    with store.transaction():
        CommitService(store).begin_planning(a.id)
        audit.capture(store, "producer_transaction")
        assert store.connection.in_transaction is True
        [row] = [r for r in rows(audit, a.id) if "producer_transaction" in r["phases"]]
        assert row["in_transaction"] is True
        assert row["observer_owned_read_snapshot"] is False
        assert audit.report()["gate"] == "OPEN"
    store.close()
