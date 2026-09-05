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


def test_late_old_call_cannot_change_recreated_stage_origin(tmp_path):
    import json

    from simple_harness.execution.sqlite.stage_audit import begin_prepare, finish

    with Database.open(tmp_path / "generation.db") as db:
        repo = ContextStagingRepository(db)

        def claim(owner, now):
            return repo.claim(
                stage_id="same",
                kind=ContextStageKind.ROOT,
                identity_key="same",
                user_id="user",
                session_id="session",
                input_hash="a" * 64,
                mode="sdk_prepared",
                owner_id=owner,
                now=now,
                lease_seconds=2,
            )

        old = claim("old", 1)
        with db.transaction() as connection:
            old_call = begin_prepare(connection, old.record, request_hash="b" * 64, now=1)
        repo.complete(
            old.record,
            private_snapshot={"provider_messages": []},
            memory_result_id=None,
            memory_result_hash=None,
            now=2,
        )
        assert repo.cleanup(now=4, older_than=1, limit=10) == 1
        current = claim("current", 5)
        current_origin = db.connection.execute(
            "SELECT incarnation FROM sdk_stage_audit_events WHERE operation='stage.created' "
            "ORDER BY event_seq DESC LIMIT 1"
        ).fetchone()[0]
        with db.transaction() as connection:
            finish(connection, old_call, state="returned", result_hash="c" * 64, now=6)
            current_call = begin_prepare(connection, current.record, request_hash="d" * 64, now=6)
            assert current_call.incarnation == current_origin
        takeover = claim("takeover", 8)
        with db.transaction() as connection:
            next_call = begin_prepare(connection, takeover.record, request_hash="e" * 64, now=8)
            assert next_call.incarnation == current_origin
        values = db.connection.execute(
            "SELECT incarnation,payload_json FROM sdk_stage_audit_events "
            "WHERE operation='stage.claimed'"
        ).fetchall()
        assert len(values) == 1 and values[0][0] == current_origin
        assert json.loads(values[0][1])["state"] == "preparing"


def test_late_release_return_does_not_settle_identical_recreated_queue(tmp_path):
    from simple_harness.execution.sqlite.stage_audit import _rows, stage_operations

    with Database.open(tmp_path / "release-generation.db") as db:
        queued(db, "one")
        with db.transaction() as connection:
            old_call, _ = begin_release(connection, "release-one", now=3)
        assert ContextStagingRepository(db).cleanup(now=20, older_than=1, limit=10) == 1
        queued(db, "one")
        with db.transaction() as connection:
            settle_release(connection, "release-one", old_call, now=21, returned=True)
        row = db.connection.execute(
            "SELECT state,attempt_count FROM memory_recall_releases WHERE release_id='release-one'"
        ).fetchone()
        assert tuple(row) == ("pending", 0)
        calls = [
            o
            for o in stage_operations(_rows(db.connection, "one", old_call.incarnation))
            if o.record_type == "boundary"
        ]
        assert len(calls) == 1 and calls[0].state == "returned"


def test_saved_continuation_binding_uses_exact_indexed_owner(tmp_path):
    from simple_harness.execution.audit import RunAuditUnavailable
    from simple_harness.execution.sqlite import SqliteExecutionUnitOfWork
    from simple_harness.execution.sqlite.stage_audit import run_stage_cut

    with Database.open(tmp_path / "continuation-binding.db") as db:
        uow = SqliteExecutionUnitOfWork(db)
        uow.create_with_start_snapshot(
            execution_session_id="session",
            run_id="run",
            request_id="request",
            profile_key="agent.general",
            driver_kind="react",
            snapshot={},
            event_id="created",
            user_id="user",
            now=1,
        )
        repo = ContextStagingRepository(db)
        claim = repo.claim(
            stage_id="continuation-stage",
            kind=ContextStageKind.CONTINUATION,
            identity_key="continuation",
            user_id="user",
            session_id="session",
            input_hash="a" * 64,
            mode="consumer_prepared",
            owner_id="owner",
            now=2,
            lease_seconds=5,
        )
        staged = repo.complete(
            claim.record,
            private_snapshot={"provider_messages": []},
            memory_result_id=None,
            memory_result_hash=None,
            now=3,
        )
        uow.enqueue_continuation(
            continuation_id="continuation",
            run_id="run",
            payload={"prepared_context": {"provider_messages": []}},
            now=4,
            context_stage_id=staged.stage_id,
            context_stage_hash=staged.private_snapshot_hash,
        )
        saved = run_stage_cut(db.connection, "run")
        assert len(saved) == 1
        queries = []
        db.connection.set_trace_callback(queries.append)
        assert run_stage_cut(db.connection, "run", saved=saved) == saved
        db.connection.set_trace_callback(None)
        assert len(queries) == 3
        assert all("LEFT JOIN" not in q for q in queries)
        for query in queries:
            plan = db.connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            assert len(plan) == 1 and "SEARCH " in plan[0][3] and "SCAN " not in plan[0][3]
        with pytest.raises(RunAuditUnavailable, match="stage_run_binding_unavailable"):
            run_stage_cut(db.connection, "other-run", saved=saved)
