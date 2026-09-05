"""New SQLite WAITING -> exact installed old runtime -> new audit continuity oracle.

Run from source with --old-python pointing at an isolated frozen 0.7.2 install.
Only the public runtime fixture is shared; each process imports its own SDK.
"""

import argparse
import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path


def fixtures():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/integration/runtime"))
    import test_react_sqlite_runtime

    return test_react_sqlite_runtime


async def old_resume(path, decision_id):
    import simple_harness
    from simple_harness import RunId
    from simple_harness.tools.authorization import AuthorizationDecision

    assert importlib.metadata.version("simple-harness-sdk") == "0.7.2"
    assert "site-packages" in simple_harness.__file__
    f = fixtures()
    physical = f.PhysicalToolCounter()

    class FinalProvider(f.Provider):
        async def invoke(self, request, *, cancel):
            self.requests.append(request)
            return f.ProviderResponse(
                request.request_id,
                f.Message(f.MessageRole.ASSISTANT, "done"),
                model="model",
            )

    provider = FinalProvider()
    runtime, uow, database = f.authorization_runtime(
        path,
        authorization=f.AuthorizationScenario(),
        physical=physical,
        owner_id="old-runtime",
        clock=lambda: 10.0,
        emit_tool_call=False,
        provider=provider,
    )
    await runtime.start()
    decision = uow.read_decision(decision_id)
    await runtime.client.decide_authorization(
        RunId("run-fault"),
        decision_id=decision.decision_id,
        nonce=str(decision.request["nonce"]),
        expected_version=decision.version,
        decision=AuthorizationDecision.ALLOW,
    )
    await f.wait_for_scenario(runtime, lambda: physical.calls == 1)
    await runtime.wait_idle(RunId("run-fault"))
    assert uow.read_run("run-fault").state.value == "completed"
    assert physical.calls == 1
    assert len(provider.requests) == 1
    await runtime.close()
    database.close()
    print(
        json.dumps(
            {
                "old_version": "0.7.2",
                "physical_calls": physical.calls,
                "provider_calls": len(provider.requests),
                "state": "completed",
            }
        )
    )


async def oracle(path, old_python):
    from simple_harness import RunId
    from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork

    f = fixtures()
    runtime, uow, database = f.authorization_runtime(
        path,
        authorization=f.AuthorizationScenario(),
        physical=f.PhysicalToolCounter(),
        owner_id="new-runtime",
        clock=lambda: 10.0,
    )
    decision = await f.start_authorization_wait(runtime, uow)
    await runtime.close()
    database.close()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            old_python,
            str(Path(__file__).resolve()),
            "--resume",
            decision.decision_id,
            "--database",
            str(path),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    print(result.stdout)
    if result.returncode:
        raise AssertionError(result.stderr)
    with Database.open(path) as reopened:
        snapshot = SqliteExecutionUnitOfWork(reopened).read_run_operation_audit(
            RunId("run-fault"),
            limit=4096,
        )
        metadata = snapshot.to_json()
        assert metadata["recording_coverage"] == "unverified", metadata
        assert "canonical_event_interval_unverified" in metadata["coverage_gaps"], metadata
        assert metadata["recording_contract_version"] == 2, metadata
        print(
            json.dumps(
                {
                    key: metadata[key]
                    for key in (
                        "recording_coverage",
                        "recording_contract_version",
                        "coverage_gaps",
                    )
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-python")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--resume")
    args = parser.parse_args()
    if args.resume:
        asyncio.run(old_resume(args.database, args.resume))
    else:
        assert args.old_python and not args.database.exists()
        asyncio.run(oracle(args.database, args.old_python))
