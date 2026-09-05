"""Public terminal payload identity must not be confused with whole-row hashes."""

import asyncio
import hashlib
import json

import pytest

from simple_harness import AgentIdentity, RunId
from simple_harness.contracts import Message, MessageRole
from simple_harness.execution.audit import RunAuditUnavailable
from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork
from simple_harness.runtime.conversation_memory import ConversationTurnInput

from .test_kernel_start import ModeRouterDriver, StableContextProvider, StableMemory, runtime


def test_actual_terminal_exact_identity_public_page_and_reopen(tmp_path):
    async def case():
        app, uow, db = runtime(
            tmp_path,
            driver=ModeRouterDriver(),
            agent_memory=StableMemory(),
            context_provider=StableContextProvider(),
        )
        run_id = RunId("terminal-private-id")
        await app.start()
        value = ConversationTurnInput(
            AgentIdentity("deployment", "household", "actor", "session"),
            Message(MessageRole.USER, "hello"),
            "hello",
        )
        await app.client.start_conversation(value, run_id=run_id)
        await app.wait_idle(run_id)
        event = db.connection.execute(
            "SELECT * FROM run_events WHERE run_id=? AND kind='run.completed'", (run_id.value,)
        ).fetchone()
        payload = json.loads(event["payload_json"])
        assert payload["sdk_memory_outbox"]["schema_version"] == 1
        expected_hash = hashlib.sha256(event["payload_json"].encode()).hexdigest()
        snapshot = await app.client.read_run_operation_audit(run_id)
        evidence = snapshot.terminal_evidence
        from simple_harness import RunTerminalAuditEvidenceV1

        assert RunTerminalAuditEvidenceV1.from_json(evidence.to_json()) == evidence
        assert evidence.event_payload_hash == expected_hash
        assert evidence.event_record_hash != expected_hash
        assert evidence.matches(
            event_id=event["event_id"], payload_hash=expected_hash, state="completed"
        )
        assert not evidence.matches(
            event_id=event["event_id"], payload_hash="0" * 64, state="completed"
        )
        assert not evidence.matches(
            event_id="another-event", payload_hash=expected_hash, state="completed"
        )
        assert not evidence.matches(
            event_id=event["event_id"], payload_hash=expected_hash, state="failed"
        )
        assert event["event_id"] not in str(evidence.to_json())
        from simple_harness.execution.memory_outbox import MemoryOutboxRepository

        assert (
            MemoryOutboxRepository(db).claim(owner_id="later-worker", now=11, lease_seconds=5)
            is not None
        )
        page = await app.client.open_run_operation_audit(run_id, page_size=1)
        assert page.to_json()["metadata"]["terminal_evidence"] == evidence.to_json()
        await app.close()
        db.close()
        with Database.open(tmp_path / "runtime.db") as reopened:
            reader = SqliteExecutionUnitOfWork(reopened)
            after = reader.read_run_operation_audit(run_id)
            assert after.terminal_evidence == evidence
            continued = reader.read_run_operation_audit_page(run_id, cursor=page.next_cursor)
            assert continued.to_json()["metadata"]["terminal_evidence"] == evidence.to_json()
            plan = reopened.connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM run_events WHERE run_id=? "
                "AND kind IN ('run.completed','run.failed','run.cancelled') LIMIT 2",
                (run_id.value,),
            ).fetchall()
            assert len(plan) == 1 and "sdk_audit_terminal_events_idx" in plan[0][3]
            assert "SEARCH " in plan[0][3] and "SCAN " not in plan[0][3]
            with reopened.transaction() as c:
                c.execute("UPDATE runs SET state='failed' WHERE run_id=?", (run_id.value,))
            with pytest.raises(RunAuditUnavailable):
                reader.read_run_operation_audit_page(run_id, cursor=page.next_cursor)
            with reopened.transaction() as c:
                c.execute("UPDATE runs SET state='completed' WHERE run_id=?", (run_id.value,))
                c.execute(
                    "UPDATE run_events SET payload_json='{}' WHERE event_id=?", (event["event_id"],)
                )
            with pytest.raises(RunAuditUnavailable):
                reader.read_run_operation_audit_page(run_id, cursor=page.next_cursor)
            with reopened.transaction() as c:
                c.execute(
                    "UPDATE run_events SET payload_json=? WHERE event_id=?",
                    (event["payload_json"], event["event_id"]),
                )
                c.execute(
                    "INSERT INTO run_events(event_id,run_id,kind,payload_json,created_at,durable_seq) SELECT ?,?,'run.failed','{}',10,MAX(durable_seq)+1 FROM run_events",
                    ("conflicting-terminal", run_id.value),
                )
            with pytest.raises(RunAuditUnavailable):
                reader.read_run_operation_audit(run_id)
            with pytest.raises(RunAuditUnavailable):
                reader.read_run_operation_audit_page(run_id, cursor=page.next_cursor)

    asyncio.run(case())
