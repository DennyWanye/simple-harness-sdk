"""Stable audit pages from actual SQLite execution, not diagnostic projections."""

import asyncio
import sqlite3

import pytest

from simple_harness import CallId, ExecutionSessionId, Message, MessageRole, RequestId, RunId
from simple_harness.execution.audit import RunAuditUnavailable
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.providers import ProviderResponse, ProviderToolCall
from simple_harness.runtime import RunStart
from simple_harness.tools.authorization import AuthorizationDecision, AuthorizationResult

from .test_react_sqlite_runtime import (
    AuthorizationScenario,
    PhysicalToolCounter,
    Provider,
    authorization_runtime,
    start_authorization_wait,
    wait_for_scenario,
)


def collect(uow, first):
    items = list(first.operations)
    page = first
    while page.next_cursor:
        following = uow.read_run_operation_audit_page(RunId(first.run_id), cursor=page.next_cursor)
        assert following.snapshot_hash == first.snapshot_hash
        assert following.page_index == page.page_index + 1
        assert following.total_operations == first.total_operations
        items.extend(following.operations)
        page = following
    assert len(items) == first.total_operations
    assert len({(o.kind, o.record_type, o.source_id, o.source_version) for o in items}) == len(
        items
    )
    return [o.to_json() for o in items]


def test_kernel_more_than_256_operations_are_exhausted_without_truncation(tmp_path):
    class Allow(AuthorizationScenario):
        async def prepare(self, prepared):
            return AuthorizationResult(AuthorizationDecision.ALLOW, receipt_ref="allow")

    class Batch(Provider):
        async def invoke(self, request, *, cancel):
            self.requests.append(request)
            return ProviderResponse(
                request.request_id,
                Message(MessageRole.ASSISTANT, "done"),
                model="model",
                tool_calls=tuple(
                    ProviderToolCall(CallId(f"call-{n}"), "count_note", {"n": n}) for n in range(60)
                )
                if len(self.requests) == 1
                else (),
            )

    async def case():
        physical = PhysicalToolCounter()
        provider = Batch()
        from simple_harness.tools import FunctionTool, ToolRegistry, ToolResult, ToolSpec

        registry = ToolRegistry()

        async def count_note(arguments, context):
            physical.calls += 1
            return ToolResult.succeeded(context.call_id)

        registry.register(
            FunctionTool(
                ToolSpec(
                    "count_note",
                    "Count a distinct note.",
                    {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                        "required": ["n"],
                        "additionalProperties": False,
                    },
                ),
                count_note,
            )
        )
        runtime, uow, database = authorization_runtime(
            tmp_path / "large.db",
            authorization=Allow(),
            physical=physical,
            provider=provider,
            registry=registry,
            owner_id="pages",
            clock=lambda: 10.0,
        )
        await runtime.start()
        await runtime.client.start(
            RunStart(
                ExecutionSessionId("session-fault"),
                RunId("run-fault"),
                RequestId("request-fault"),
                "turn-fault",
                {
                    "messages": [{"role": "user", "content": "write"}],
                    "capability_snapshot": {"tools": ["count_note"]},
                    "max_output_tokens": 100,
                },
                1,
            )
        )
        await runtime.wait_idle(RunId("run-fault"))
        assert physical.calls == 60 and len(provider.requests) == 2
        assert uow.read_run("run-fault").state.value == "completed"
        assert uow.read_run_operation_audit(RunId("run-fault")).truncated
        bounded = uow.read_run_operation_audit(RunId("run-fault"), limit=4096)
        first = await runtime.client.open_run_operation_audit(RunId("run-fault"), page_size=37)
        assert first.total_operations > 256 and len(first.operations) == 37
        assert collect(uow, first) == [o.to_json() for o in bounded.operations]
        assert first.metadata["history_coverage"] == "partial"
        await runtime.close()
        database.close()
        reopened = Database.open(tmp_path / "large.db")
        assert collect(SqliteExecutionUnitOfWork(reopened), first) == [
            o.to_json() for o in bounded.operations
        ]
        assert physical.calls == 60 and len(provider.requests) == 2
        reopened.close()

    asyncio.run(case())


def test_kernel_wait_snapshot_survives_terminal_and_reopen(tmp_path):
    async def case():
        physical = PhysicalToolCounter()
        runtime, uow, database = authorization_runtime(
            tmp_path / "wait.db",
            authorization=AuthorizationScenario(),
            physical=physical,
            owner_id="pages",
            clock=lambda: 10.0,
        )
        decision = await start_authorization_wait(runtime, uow)
        old = uow.read_run_operation_audit(RunId("run-fault"), limit=4096)
        first = await runtime.client.open_run_operation_audit(RunId("run-fault"), page_size=2)
        await runtime.client.decide_authorization(
            RunId("run-fault"),
            decision_id=decision.decision_id,
            nonce=str(decision.request["nonce"]),
            expected_version=decision.version,
            decision=AuthorizationDecision.ALLOW,
        )
        await wait_for_scenario(runtime, lambda: physical.calls == 1)
        await runtime.wait_idle(RunId("run-fault"))
        assert collect(uow, first) == [o.to_json() for o in old.operations]
        new = await runtime.client.open_run_operation_audit(RunId("run-fault"), page_size=2)
        assert new.snapshot_hash != first.snapshot_hash
        assert first.metadata["run_state"] == "waiting"
        assert new.metadata["run_state"] == "completed"
        await runtime.close()
        database.close()
        reopened = Database.open(tmp_path / "wait.db")
        assert collect(SqliteExecutionUnitOfWork(reopened), first) == [
            o.to_json() for o in old.operations
        ]
        assert physical.calls == 1
        reopened.close()

    asyncio.run(case())


def test_cursor_cross_run_tamper_missing_and_page_corruption_fail_closed(tmp_path):
    from simple_harness.execution.sqlite.audit_pages import _root

    from .test_h13_tool_recovery import _unknown

    database, uow, *_ = _unknown(tmp_path)
    first = uow.open_run_operation_audit(RunId("run-1"), page_size=2)
    cursor = first.next_cursor
    with pytest.raises(RunAuditUnavailable, match="run_mismatch"):
        uow.read_run_operation_audit_page(RunId("other"), cursor=cursor)
    for bad in ("../escape", cursor[:-1] + ("0" if cursor[-1] != "0" else "1")):
        with pytest.raises(RunAuditUnavailable, match="cursor_invalid"):
            uow.read_run_operation_audit_page(RunId("run-1"), cursor=bad)
    path = _root(database) / (first.snapshot_hash + ".sqlite")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE pages SET payload='[]' WHERE page_index=1")
    with pytest.raises(RunAuditUnavailable, match="page_hash_mismatch"):
        uow.read_run_operation_audit_page(RunId("run-1"), cursor=cursor)
    path.unlink()
    with pytest.raises(RunAuditUnavailable, match="snapshot_unavailable"):
        uow.read_run_operation_audit_page(RunId("run-1"), cursor=cursor)
    database.close()


def test_capacity_and_timeout_do_not_publish_partial_snapshot(tmp_path, monkeypatch):
    from simple_harness.execution.sqlite import audit_pages

    from .test_h13_tool_recovery import _unknown

    database, uow, *_ = _unknown(tmp_path)
    before = uow.read_run_operation_audit(RunId("run-1")).snapshot_hash
    for setting, value, reason in (("MAX_BYTES", 1, "capacity"), ("MAX_SECONDS", -1, "timeout")):
        with monkeypatch.context() as patch:
            patch.setattr(audit_pages, setting, value)
            with pytest.raises(RunAuditUnavailable, match=reason):
                uow.open_run_operation_audit(RunId("run-1"), page_size=2)
        assert list(audit_pages._root(database).iterdir()) == []
    assert uow.read_run_operation_audit(RunId("run-1")).snapshot_hash == before
    database.close()


def test_manifest_tamper_and_old_normalizer_are_not_exported(tmp_path, monkeypatch):
    from simple_harness.execution.sqlite import audit_pages

    from .test_h13_tool_recovery import _unknown

    database, uow, *_ = _unknown(tmp_path)
    first = uow.open_run_operation_audit(RunId("run-1"), page_size=2)
    with monkeypatch.context() as patch:
        patch.setattr(audit_pages, "NORMALIZER", "future-safe-normalizer")
        with pytest.raises(RunAuditUnavailable, match="version_unavailable"):
            uow.read_run_operation_audit_page(RunId("run-1"), cursor=first.next_cursor)
    path = audit_pages._root(database) / (first.snapshot_hash + ".sqlite")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE manifest SET payload='{}'")
    with pytest.raises(RunAuditUnavailable, match="manifest_hash_mismatch"):
        uow.read_run_operation_audit_page(RunId("run-1"), cursor=first.next_cursor)
    database.close()


def test_failed_atomic_publish_and_other_dataset_do_not_fall_back(tmp_path, monkeypatch):
    from simple_harness.execution.sqlite import audit_pages

    from .test_h13_tool_recovery import _unknown

    database, uow, *_ = _unknown(tmp_path)

    def fail_publish(*args):
        raise OSError("injected disk error")

    with monkeypatch.context() as patch:
        patch.setattr(audit_pages.os, "replace", fail_publish)
        with pytest.raises(RunAuditUnavailable, match="snapshot_unavailable"):
            uow.open_run_operation_audit(RunId("run-1"), page_size=2)
    assert list(audit_pages._root(database).iterdir()) == []
    first = uow.open_run_operation_audit(RunId("run-1"), page_size=2)
    other_path = tmp_path / "other"
    other_path.mkdir()
    other_database, other_uow, *_ = _unknown(other_path)
    # Same canonical Run ID in a different real SQLite dataset is not the snapshot.
    with pytest.raises(RunAuditUnavailable, match="snapshot_unavailable"):
        other_uow.read_run_operation_audit_page(RunId("run-1"), cursor=first.next_cursor)
    other_database.close()
    database.close()


@pytest.mark.parametrize(
    "replacement", ["empty", "different_start", "different_owner", "before_cut"]
)
def test_in_place_database_restore_cannot_reuse_old_run_cursor(tmp_path, replacement):
    import shutil

    from .test_h13_tool_recovery import _unknown

    database, uow, *_ = _unknown(tmp_path)
    first = uow.open_run_operation_audit(RunId("run-1"), page_size=2)
    path = database.path
    database.close()
    other = Database.open(tmp_path / "replacement.db")
    if replacement != "empty":
        SqliteExecutionUnitOfWork(other).create_with_start_snapshot(
            execution_session_id="s",
            run_id="run-1",
            request_id="q",
            profile_key="agent.general",
            driver_kind="react",
            snapshot={"schema_version": 1, "different": True}
            if replacement == "different_start"
            else {"schema_version": 1},
            event_id="created",
            now=1.0,
            user_id="other-owner" if replacement == "different_owner" else "harness-system",
        )
    other.close()
    inode = path.stat().st_ino
    shutil.copyfile(tmp_path / "replacement.db", path)
    assert path.stat().st_ino == inode
    reopened = Database.open(path)
    with pytest.raises(RunAuditUnavailable, match="incarnation|cut"):
        SqliteExecutionUnitOfWork(reopened).read_run_operation_audit_page(
            RunId("run-1"),
            cursor=first.next_cursor,
        )
    reopened.close()
