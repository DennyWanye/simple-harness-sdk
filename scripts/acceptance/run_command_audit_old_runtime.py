"""Exact installed old072 command writes must remain visible as audit gaps."""

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path


def fixtures():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/execution"))
    from test_command_ingress import _start

    return _start


def old(path, phase):
    import simple_harness
    from simple_harness.execution.command_ingress import CommandIngress
    from simple_harness.execution.sqlite import Database

    assert importlib.metadata.version("simple-harness-sdk") == "0.7.2"
    assert "site-packages" in simple_harness.__file__
    with Database.open(path) as database:
        assert database.schema_version == 7
        ingress = CommandIngress(database)
        if phase == "legacy":
            ingress.submit_start(fixtures()(), now=1)
        claim = ingress.claim_next(owner_id="old-worker", now=4, lease_seconds=10)
        assert claim is not None
        receipt = ingress.retry(claim, error_code="old-runtime-retry", retry_at=5, now=4.5)
        print(
            json.dumps({"old_version": "0.7.2", "phase": phase, "command_version": receipt.version})
        )


def oracle(path, old_python, phase):
    from simple_harness import CommandErrorCode
    from simple_harness.execution.command_ingress import CommandIngress
    from simple_harness.execution.sqlite import Database, SqliteExecutionUnitOfWork

    assert not path.exists()
    if phase == "middle":
        with Database.open(path) as database:
            ingress = CommandIngress(database)
            ingress.submit_start(fixtures()(), now=1)
            first = ingress.claim_next(owner_id="new-worker", now=2, lease_seconds=10)
            ingress.retry(first, error_code="new-runtime-retry", retry_at=3, now=2.5)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [old_python, str(Path(__file__).resolve()), "--database", str(path), "--old-phase", phase],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    print(result.stdout)
    assert result.returncode == 0, result.stderr
    with Database.open(path) as database:
        assert database.schema_version == 7 and database.audit_schema_version == 1
        ingress = CommandIngress(database)
        claim = ingress.claim_next(owner_id="new-worker", now=6, lease_seconds=10)
        ingress.reject(claim, error_code=CommandErrorCode.INTENT_CONFLICT, now=7)
        reader = SqliteExecutionUnitOfWork(database)
        assert reader.read_run("run-1") is None
        page = reader.open_command_operation_audit("start-1", page_size=2)
        assert page.metadata["recording_coverage"] == "unverified"
        assert "command_transition_history_unverified" in page.metadata["coverage_gaps"]
        versions, operations = [], []
        first = page
        while True:
            versions.extend(item.source_version for item in page.operations)
            operations.extend(item.operation_name for item in page.operations)
            if not page.next_cursor:
                break
            page = reader.read_command_operation_audit_page("start-1", cursor=page.next_cursor)
        assert versions == ([1, 2, 3, 6, 7] if phase == "middle" else [3, 4, 5]), versions
        if phase == "legacy":
            assert operations[0] == "command.legacy_baseline"
            assert "command_introduction_unverified" in page.metadata["coverage_gaps"]
        print(
            json.dumps(
                {
                    "phase": phase,
                    "versions": versions,
                    "coverage_gaps": list(first.metadata["coverage_gaps"]),
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--old-python")
    parser.add_argument("--phase", choices=["legacy", "middle"], default="middle")
    parser.add_argument("--old-phase", choices=["legacy", "middle"])
    args = parser.parse_args()
    if args.old_phase:
        old(args.database, args.old_phase)
    else:
        oracle(args.database, args.old_python, args.phase)
