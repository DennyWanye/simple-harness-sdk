"""Real stage/release authority rejects cross-request settlement before mutation."""

import pytest

from simple_harness.execution.context_staging import ContextStageKind, ContextStagingRepository
from simple_harness.execution.sqlite import Database
from simple_harness.execution.sqlite.stage_audit import begin_release, settle_release
from simple_harness.execution.uow import UnitOfWorkConflict


def queued(database, name):
    repo = ContextStagingRepository(database)
    claim = repo.claim(
        stage_id=name,
        kind=ContextStageKind.ROOT,
        identity_key=name,
        user_id="user",
        session_id="session",
        input_hash="a" * 64,
        mode="sdk_prepared",
        owner_id="owner",
        now=1,
        lease_seconds=20,
    )
    repo.complete(
        claim.record,
        private_snapshot={"provider_messages": []},
        memory_result_id=None,
        memory_result_hash=None,
        now=2,
        release_id="release-" + name,
        release_query_id="query-" + name,
        release_query_hash="b" * 64,
        release_result_id="result-" + name,
        release_result_hash="c" * 64,
        release_retry_at=2,
    )


def test_foreign_release_cannot_borrow_actual_call(tmp_path):
    with Database.open(tmp_path / "release.db") as db:
        queued(db, "one")
        queued(db, "two")
        with db.transaction() as connection:
            call, request = begin_release(connection, "release-one", now=3)
        with pytest.raises(UnitOfWorkConflict):
            with db.transaction() as connection:
                settle_release(connection, "release-two", call, now=4, returned=True)
        rows = db.connection.execute(
            "SELECT state,attempt_count FROM memory_recall_releases"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("pending", 0), ("pending", 0)]
        with db.transaction() as connection:
            settle_release(connection, "release-one", call, now=4, returned=True)
        assert tuple(
            db.connection.execute(
                "SELECT state,attempt_count FROM memory_recall_releases "
                "WHERE release_id='release-one'"
            ).fetchone()
        ) == ("released", 1)


def test_uninstrumented_release_transition_remains_gap_after_cleanup(tmp_path):
    from simple_harness.execution.sqlite.stage_audit import _rows, stage_coverage

    with Database.open(tmp_path / "legacy-release.db") as db:
        queued(db, "one")
        with db.transaction() as connection:
            connection.execute(
                "UPDATE memory_recall_releases SET attempt_count=1,state='released',released_at=4 "
                "WHERE release_id='release-one'"
            )
            connection.execute("DELETE FROM memory_recall_releases WHERE release_id='release-one'")
            assert "release_call_unverified" in stage_coverage(_rows(connection, "one"))


def test_stage_cursor_domain_cleanup_prefix_and_new_calls(tmp_path):
    from simple_harness.execution.audit import RunAuditUnavailable
    from simple_harness.execution.sqlite import SqliteExecutionUnitOfWork

    path = tmp_path / "pages.db"
    with Database.open(path) as db:
        queued(db, "one")
        queued(db, "two")
        reader = SqliteExecutionUnitOfWork(db)
        first = reader.open_context_stage_operation_audit("one", page_size=1)
        expected = []
        cursor = first.next_cursor
        while cursor:
            page = reader.read_context_stage_operation_audit_page("one", cursor=cursor)
            expected.append(page)
            cursor = page.next_cursor
        with pytest.raises(RunAuditUnavailable):
            reader.read_context_stage_operation_audit_page("two", cursor=first.next_cursor)
        with pytest.raises(RunAuditUnavailable):
            reader.read_command_operation_audit_page("one", cursor=first.next_cursor)
        with db.transaction() as connection:
            call, _ = begin_release(connection, "release-one", now=3)
            settle_release(connection, "release-one", call, now=4, returned=True)
        repo = ContextStagingRepository(db)
        repo.cleanup(now=30, older_than=1, limit=10)
        assert repo.get("one") is None
    with Database.open(path) as db:
        reader = SqliteExecutionUnitOfWork(db)
        cursor = first.next_cursor
        for expected_page in expected:
            actual = reader.read_context_stage_operation_audit_page("one", cursor=cursor)
            assert actual == expected_page
            cursor = actual.next_cursor
        current = reader.open_context_stage_operation_audit("one")
        assert any(
            o.operation_name == "memory.release_recall" and o.state == "returned"
            for o in current.operations
        )
        assert any(o.operation_name == "stage.deleted" for o in current.operations)
