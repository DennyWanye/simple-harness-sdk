"""Real command admission before a Run exists, not a synthetic Run ledger."""

import pytest
from test_command_ingress import _start

from simple_harness import CommandErrorCode
from simple_harness.execution.command_ingress import CommandIngress
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork


def test_pre_run_retry_reject_is_fully_paged_after_reopen(tmp_path):
    path = tmp_path / "commands.db"
    with Database.open(path) as database:
        ingress = CommandIngress(database)
        ingress.submit_start(_start(), now=1)
        first = ingress.claim_next(owner_id="worker", now=2, lease_seconds=10)
        ingress.retry(first, error_code="PRIVATE_CANARY", retry_at=3, now=2.5)
        second = ingress.claim_next(owner_id="worker", now=3, lease_seconds=10)
        ingress.reject(second, error_code=CommandErrorCode.INTENT_CONFLICT, now=4)
        assert SqliteExecutionUnitOfWork(database).read_run("run-1") is None
    with Database.open(path) as database:
        reader = SqliteExecutionUnitOfWork(database)
        first = reader.open_command_operation_audit("start-1", page_size=2)
        assert first.metadata["run_materialized"] is False
        assert first.metadata["coverage_gaps"] == ()
        operations = list(first.operations)
        page = first
        while page.next_cursor:
            page = reader.read_command_operation_audit_page("start-1", cursor=page.next_cursor)
            assert page.snapshot_hash == first.snapshot_hash
            operations.extend(page.operations)
        assert [item.operation_name for item in operations] == [
            "command.accepted",
            "command.claimed",
            "command.retry",
            "command.claimed",
            "command.rejected",
        ]
        assert "PRIVATE_CANARY" not in str([item.to_json() for item in operations])
        assert operations[2].error_code is None and operations[2].error_code_hash
        assert operations[3].error_code is None and operations[3].error_code_hash is None
        assert operations[-1].error_code == "command_intent_conflict"
        assert operations[1].owner_ref_hash == operations[-1].owner_ref_hash
        assert operations[-1].claim_epoch == operations[-1].attempt_count == 2
        assert reader.read_run("run-1") is None


def test_cancel_before_create_records_all_cancelled_commands(tmp_path):
    from test_command_ingress import _continuation

    from simple_harness import CancelCommandIntent, RunId

    with Database.open(tmp_path / "cancel.db") as database:
        ingress, reader = CommandIngress(database), SqliteExecutionUnitOfWork(database)
        ingress.submit_start(_start(), now=1)
        ingress.submit_continue(_continuation("continue-1", "continuation-1"), now=2)
        cancel = CancelCommandIntent("deployment/phone", "key-1", "cancel-1", RunId("run-1"))
        ingress.submit_cancel(cancel, now=3)
        before = {}
        for command_id, last in [
            ("start-1", "cancelled"),
            ("continue-1", "cancelled"),
            ("cancel-1", "applied"),
        ]:
            page = reader.open_command_operation_audit(command_id)
            assert page.metadata["coverage_gaps"] == ()
            assert page.metadata["run_materialized"] is False
            assert page.operations[-1].operation_name == "command." + last
            if last == "cancelled":
                from simple_harness.execution.audit import audit_reference

                assert page.operations[-1].causal_command_ref == audit_reference(
                    "control", "cancel-1"
                )
            before[command_id] = [item.to_json() for item in page.operations]
        ingress.submit_cancel(cancel, now=4)
        for command_id, expected in before.items():
            assert [
                item.to_json()
                for item in reader.open_command_operation_audit(command_id).operations
            ] == expected
        assert reader.read_run("run-1") is None


def test_command_snapshot_prefix_survives_retry_and_reopen(tmp_path):
    path = tmp_path / "prefix.db"
    with Database.open(path) as database:
        ingress, reader = CommandIngress(database), SqliteExecutionUnitOfWork(database)
        ingress.submit_start(_start(), now=1)
        claim = ingress.claim_next(owner_id="worker", now=2, lease_seconds=10)
        first = reader.open_command_operation_audit("start-1", page_size=1)
        assert first.total_operations == 2 and first.next_cursor
        ingress.retry(claim, error_code="retry", retry_at=3, now=2.5)
        second = ingress.claim_next(owner_id="worker", now=3, lease_seconds=10)
        ingress.reject(second, error_code=CommandErrorCode.INTENT_CONFLICT, now=4)
    with Database.open(path) as database:
        reader = SqliteExecutionUnitOfWork(database)
        following = reader.read_command_operation_audit_page("start-1", cursor=first.next_cursor)
        assert following.snapshot_hash == first.snapshot_hash
        assert following.total_operations == 2 and following.next_cursor is None
        assert following.operations[0].operation_name == "command.claimed"
        assert reader.open_command_operation_audit("start-1").total_operations == 5


def test_command_event_failure_rolls_back_original_claim(tmp_path):
    import sqlite3

    with Database.open(tmp_path / "rollback.db") as database:
        ingress = CommandIngress(database)
        before = ingress.submit_start(_start(), now=1)
        database.connection.execute(
            "CREATE TRIGGER inject_command_audit_failure BEFORE INSERT ON sdk_command_audit_events "
            "WHEN NEW.operation='claimed' BEGIN SELECT RAISE(ABORT,'injected audit failure'); END",
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
            ingress.claim_next(owner_id="worker", now=2, lease_seconds=10)
        assert ingress.get("start-1") == before
        page = SqliteExecutionUnitOfWork(database).open_command_operation_audit("start-1")
        assert page.total_operations == 1
        assert page.metadata["coverage_gaps"] == ()


def test_command_pages_exhaust_more_than_256_and_reject_cross_domain(tmp_path):
    from test_command_ingress import _continuation

    from simple_harness import RunId
    from simple_harness.execution.audit import RunAuditUnavailable

    with Database.open(tmp_path / "many.db") as database:
        ingress, reader = CommandIngress(database), SqliteExecutionUnitOfWork(database)
        ingress.submit_start(_start(), now=1)
        claim = ingress.claim_next(owner_id="worker", now=2, lease_seconds=1000)
        for n in range(270):
            claim = ingress.heartbeat(claim, now=3 + n, lease_seconds=1000)
        ingress.submit_continue(_continuation("continue-1", "continuation-1"), now=274)
        first = reader.open_command_operation_audit("start-1", page_size=31)
        assert first.total_operations == 272
        assert first.metadata["coverage_gaps"] == ()
        with pytest.raises(RunAuditUnavailable, match="audit_cursor_command_mismatch"):
            reader.read_command_operation_audit_page("continue-1", cursor=first.next_cursor)
        with pytest.raises(RunAuditUnavailable, match="audit_cursor_domain_mismatch"):
            reader.read_run_operation_audit_page(RunId("run-1"), cursor=first.next_cursor)
        versions = [item.source_version for item in first.operations]
        page = first
        while page.next_cursor:
            page = reader.read_command_operation_audit_page("start-1", cursor=page.next_cursor)
            versions.extend(item.source_version for item in page.operations)
        assert versions == list(range(1, 273))
        assert ingress.get("start-1").version == 272


def test_command_cursor_rejects_in_place_different_intent_restore(tmp_path):
    import sqlite3
    from dataclasses import replace

    from simple_harness import RequestId
    from simple_harness.execution.audit import RunAuditUnavailable

    path = tmp_path / "original.db"
    with Database.open(path) as database:
        ingress, reader = CommandIngress(database), SqliteExecutionUnitOfWork(database)
        ingress.submit_start(_start(), now=1)
        ingress.claim_next(owner_id="worker", now=2, lease_seconds=10)
        first = reader.open_command_operation_audit("start-1", page_size=1)
    other_path = tmp_path / "other.db"
    with Database.open(other_path) as database:
        CommandIngress(database).submit_start(
            replace(_start(), request_id=RequestId("different")), now=1
        )
    source, target = sqlite3.connect(other_path), sqlite3.connect(path)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    with Database.open(path) as database:
        with pytest.raises(RunAuditUnavailable, match="audit_command_incarnation_mismatch"):
            SqliteExecutionUnitOfWork(database).read_command_operation_audit_page(
                "start-1", cursor=first.next_cursor
            )
