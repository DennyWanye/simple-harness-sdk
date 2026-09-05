"""Actual public pre-Run context call, no fake Run/command, durable stage pages."""

import asyncio

import pytest

from simple_harness import AgentIdentity, RunId
from simple_harness.contracts import Message, MessageRole
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.runtime.conversation_memory import ConversationTurnInput

from .test_kernel_start import StableContextProvider, StableMemory, _ProviderCrashCut, runtime


def test_no_run_context_crash_is_readable_after_reopen_without_reexecution(tmp_path):
    async def case():
        provider = StableContextProvider(crash_first=True)
        app, _, database = runtime(tmp_path, agent_memory=StableMemory(), context_provider=provider)
        await app.start()
        value = ConversationTurnInput(
            AgentIdentity("deployment", "household", "actor", "session"),
            Message(MessageRole.USER, "PRIVATE-stage-user"),
            "PRIVATE-stage-user",
        )
        with pytest.raises(_ProviderCrashCut):
            await app.client.start_conversation(value, run_id=RunId("no-run"))
        assert app.client.query(RunId("no-run")) is None
        stage_id = provider.requests[0].preparation_id
        page = await app.client.open_context_stage_operation_audit(stage_id, page_size=1)
        pieces = [page]
        while pieces[-1].next_cursor:
            pieces.append(
                await app.client.read_context_stage_operation_audit_page(
                    stage_id, cursor=pieces[-1].next_cursor
                )
            )
        operations = [o for p in pieces for o in p.operations]
        calls = [o for o in operations if o.record_type == "boundary"]
        assert len(calls) == 1 and calls[0].state == "unknown"
        assert "PRIVATE-stage-user" not in str([p.to_json() for p in pieces])
        assert (
            database.connection.execute("SELECT COUNT(*) FROM conversation_commands").fetchone()[0]
            == 0
        )
        await app.close()
        database.close()
        with Database.open(tmp_path / "runtime.db") as reopened:
            reader = SqliteExecutionUnitOfWork(reopened)
            cursor = page.next_cursor
            for expected in pieces[1:]:
                actual = reader.read_context_stage_operation_audit_page(stage_id, cursor=cursor)
                assert actual == expected
                cursor = actual.next_cursor
        assert len(provider.requests) == 1

    asyncio.run(case())


def test_actual_preparation_is_bound_to_run_and_pages_do_not_call_provider(tmp_path):
    async def case():
        provider = StableContextProvider()
        app, uow, database = runtime(
            tmp_path, agent_memory=StableMemory(), context_provider=provider
        )
        await app.start()
        try:
            value = ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session"),
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            await app.client.start_conversation(value, run_id=RunId("prepared-run"))
            await app.wait_idle(RunId("prepared-run"))
            page = await app.client.open_run_operation_audit(RunId("prepared-run"), page_size=1)
            operations = list(page.operations)
            while page.next_cursor:
                page = await app.client.read_run_operation_audit_page(
                    RunId("prepared-run"), cursor=page.next_cursor
                )
                operations.extend(page.operations)
            calls = [o for o in operations if o.operation_name == "context.prepare"]
            assert len(calls) == 1 and calls[0].state == "returned"
            assert calls[0].result_hash is not None
            assert not any(
                gap.startswith("stage_") for gap in page.to_json()["metadata"]["coverage_gaps"]
            )
            assert len(provider.requests) == 1
        finally:
            await app.close()
            database.close()

    asyncio.run(case())


def test_run_retains_missing_stage_family_gap_from_actual_start(tmp_path):
    async def case():
        provider = StableContextProvider()
        app, uow, database = runtime(
            tmp_path, agent_memory=StableMemory(), context_provider=provider
        )
        await app.start()
        try:
            value = ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session"),
                Message(MessageRole.USER, "hello"),
                "hello",
            )
            await app.client.start_conversation(value, run_id=RunId("missing-stage-run"))
            await app.wait_idle(RunId("missing-stage-run"))
            from simple_harness.execution.sqlite.stage_audit import run_coverage

            with database.transaction() as connection:
                connection.execute("DROP TRIGGER sdk_stage_audit_no_delete")
                connection.execute("DELETE FROM sdk_stage_audit_events")
                gaps = run_coverage(connection, "missing-stage-run")
                assert "stage_consumption_authority_unverified" in gaps
        finally:
            await app.close()
            database.close()

    asyncio.run(case())


@pytest.mark.parametrize("failure", [False, True])
def test_actual_release_return_or_lost_ack_is_durable_without_run(tmp_path, failure):
    async def case():
        from simple_harness.execution.context_staging import (
            ContextStageKind,
            ContextStagingRepository,
        )

        class Memory(StableMemory):
            async def release_recall(self, request):
                self.releases.append(request)
                if failure:
                    raise TimeoutError("PRIVATE-lost-ack")

        memory = Memory()
        app, _, db = runtime(
            tmp_path, agent_memory=memory, context_provider=StableContextProvider()
        )
        repo = ContextStagingRepository(db)
        claim = repo.claim(
            stage_id="release-stage",
            kind=ContextStageKind.ROOT,
            identity_key="release",
            user_id="user",
            session_id="session",
            input_hash="a" * 64,
            mode="consumer_prepared",
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
            release_id="release",
            release_query_id="query",
            release_query_hash="b" * 64,
            release_result_id="result",
            release_result_hash="c" * 64,
            release_retry_at=2,
        )
        await app.start()
        try:
            assert len(memory.releases) == 1
            page = await app.client.open_context_stage_operation_audit("release-stage")
            calls = [o for o in page.operations if o.operation_name == "memory.release_recall"]
            assert len(calls) == 1 and calls[0].state == ("unknown" if failure else "returned")
            assert calls[0].handed_off_at == 10 and calls[0].settled_at == 10
            assert "PRIVATE-lost-ack" not in str(page.to_json())
            assert db.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            assert len(memory.releases) == 1
        finally:
            await app.close()
            db.close()

    asyncio.run(case())
