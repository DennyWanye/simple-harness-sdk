"""Exact072 writer between new delivery attempts must retain its version gap."""

import argparse
import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path


async def run(path, old_python):
    from simple_harness.contracts import RunId
    from simple_harness.execution.delivery import DeliveryDispatcher
    from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork

    class Sink:
        calls = 0

        async def deliver(self, payload, *, idempotency_key):
            self.calls += 1
            raise RuntimeError("private-delivery-canary")

    if old_python is None:
        import simple_harness

        assert importlib.metadata.version("simple-harness-sdk") == "0.7.2"
        assert "site-packages" in simple_harness.__file__
        with Database.open(path) as database:
            uow = SqliteExecutionUnitOfWork(database)
            sink = Sink()
            assert await DeliveryDispatcher(uow, {"presenter": sink}, clock=lambda: 6).run_once()
            assert sink.calls == 1 and uow.read_delivery("delivery-1").version == 5
            print(json.dumps(dict(old_version="0.7.2", sink_calls=1, delivery_version=5)))
        return
    assert not path.exists()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/integration/execution"))
    from test_delivery_operation_audit import terminal

    with Database.open(path) as database:
        uow = await terminal(database)
        sink = Sink()
        assert await DeliveryDispatcher(uow, {"presenter": sink}, clock=lambda: 5).run_once()
        assert uow.read_delivery("delivery-1").version == 3 and sink.calls == 1
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
        uow = SqliteExecutionUnitOfWork(database)
        sink = Sink()
        assert await DeliveryDispatcher(uow, {"presenter": sink}, clock=lambda: 7).run_once()
        audit = uow.read_run_operation_audit(RunId("run-1"))
        versions = sorted(
            o.source_version
            for o in audit.operations
            if o.kind == "delivery" and o.record_type == "transition"
        )
        assert versions == [0, 1, 2, 3, 6, 7, 8], versions
        assert "delivery_version_history_unverified" in audit.coverage_gaps
        assert "private-delivery-canary" not in json.dumps(audit.to_json())
        print(json.dumps(dict(versions=versions, coverage_gaps=list(audit.coverage_gaps))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--old-python")
    args = parser.parse_args()
    asyncio.run(run(args.database, args.old_python))
