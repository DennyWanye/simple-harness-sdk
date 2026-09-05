"""Exact072 intermediate committed-turn call is not erased by a later new writer."""

import argparse
import asyncio
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path


def run(path, old_python):
    from simple_harness import AgentMemoryError, AgentMemoryErrorCode, RunId
    from simple_harness.execution.memory_outbox import MemoryDispatcher, MemoryOutboxRepository
    from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork

    class Memory:
        calls = 0

        async def record_committed_turn(self, turn):
            self.calls += 1
            raise AgentMemoryError(AgentMemoryErrorCode.TRANSIENT)

    def dispatch(database, now):
        memory = Memory()
        dispatcher = MemoryDispatcher(
            MemoryOutboxRepository(database),
            memory,
            owner_id="worker",
            clock=lambda: now,
            lease_seconds=1,
        )
        assert asyncio.run(dispatcher.run_once()) and memory.calls == 1

    if old_python is None:
        import simple_harness

        assert importlib.metadata.version("simple-harness-sdk") == "0.7.2"
        assert "site-packages" in simple_harness.__file__
        with Database.open(path) as database:
            dispatch(database, 5)
        print("exact072 actual SDK-to-Memory calls=1")
        return
    assert not path.exists()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/execution"))
    from test_memory_outbox_uow import _setup, _spec, _terminal

    database, uow, lease, fence = _setup(path)
    _terminal(uow, lease, fence, _spec())
    dispatch(database, 3)
    database.close()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [old_python, str(Path(__file__).resolve()), "--database", str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    print(result.stdout)
    with Database.open(path) as database:
        dispatch(database, 8)
        audit = SqliteExecutionUnitOfWork(database).read_run_operation_audit(RunId("run-1"))
        calls = sorted(
            o.claim_epoch
            for o in audit.operations
            if o.operation_name == "memory.record_committed_turn"
        )
        assert calls == [1, 3]
        assert "memory_outbox_claim_history_unverified" in audit.coverage_gaps
        print("recorded epochs", calls, "gap", "memory_outbox_claim_history_unverified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--old-python")
    args = parser.parse_args()
    run(args.database, args.old_python)
