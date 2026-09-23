# SPDX-License-Identifier: Apache-2.0
"""Grade an actual executor-run check from its own receipts; never re-execute.

The only inputs are the original ``code_test`` LayerResult as the verifier
produced it (per-target ``TestRun`` + sandbox ``ExecutionReceipt``) and the
registered assertion key. Missing executor facts stay UNKNOWN; exit 0 alone is
not a PASS: the run must be bound to the exact Result/workspace snapshot and the
executor must have reported passing pytest node ids.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .checks import Grade
from .codec import AssuranceError, canonical, digest, integer, one_of, text
from .local_checks import AssertionOutput, RecordedLocalCheck
from .refs import AssuranceRef

OUTPUT_SCHEMA = "assurance-executor-check-output-v1"
_NODE = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL)\s+(\S+)", re.M)


def pytest_nodeids(output: str) -> dict[str, list[str]]:
    """Node ids from pytest's short summary (``-rA``); SKIPPED lines carry none."""
    found: dict[str, list[str]] = {
        "PASSED": [],
        "FAILED": [],
        "ERROR": [],
        "XPASS": [],
        "XFAIL": [],
    }
    for status, node in _NODE.findall(str(output or "")):
        if node not in found[status]:
            found[status].append(node)
    return found


def executor_run_facts(
    result: Mapping[str, Any], *, layer: str
) -> tuple[str, Grade, dict[str, Any]]:
    """(execution_state, assertion grade, evidence document) from the actual run.

    ERROR: nothing executed, a target without an executor receipt, an executor
    that could not clean up (status != ok / limit exceeded / no exit code).
    CANCELLED: any target timed out. SUCCEEDED: every target ran to an exit code
    under a clean receipt; then FAIL when any exit code is non-zero, UNKNOWN when
    the run is not bound to this Result's workspace snapshot or the executor
    reported no passing node id, PASS otherwise.
    """
    if result.get("layer") != layer:
        raise AssuranceError("CHECK_ASSERTION_INVALID")
    detail = result.get("detail") or {}
    runs = list(detail.get("runs") or [])
    states: list[str] = []
    documented: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, Mapping):
            raise AssuranceError("CHECK_ASSERTION_INVALID")
        receipt = run.get("receipt")
        nodes = pytest_nodeids(str(run.get("stdout") or ""))
        row: dict[str, Any] = {
            "target": run.get("target"),
            "returncode": run.get("returncode"),
            "timed_out": bool(run.get("timed_out")),
            "command": list(run.get("command") or []),
            "error": run.get("error"),
            "no_tests_collected": run.get("no_tests_collected") is True,
            "nodeids": nodes,
            "stdout_tail": str(run.get("stdout") or "")[-8000:],
            "receipt": None,
        }
        if not isinstance(receipt, Mapping):
            states.append("ERROR")  # no executor fact for this target
            documented.append(row)
            continue
        row["receipt"] = {
            key: receipt.get(key)
            for key in (
                "execution_id",
                "kind",
                "isolated",
                "environment_digest",
                "effective_limits",
                "exit_code",
                "truncated",
                "timed_out",
                "limit_exceeded",
                "tree_killed",
                "status",
            )
        }
        row["receipt"]["residual_pids"] = len(receipt.get("residual_pids") or [])
        documented.append(row)
        if receipt.get("timed_out") or run.get("timed_out"):
            states.append("CANCELLED")
        elif (
            receipt.get("status") != "ok"
            or receipt.get("limit_exceeded")
            # tree_killed is the executor reaping the finished run's process
            # tree, which it reports for clean runs too; not an execution error.
            or receipt.get("exit_code") is None
            or run.get("returncode") is None
        ):
            states.append("ERROR")
        else:
            states.append("SUCCEEDED")
    if not runs:
        state = "ERROR"
    elif "ERROR" in states:
        state = "ERROR"
    elif "CANCELLED" in states:
        state = "CANCELLED"
    else:
        state = "SUCCEEDED"
    scope = detail.get("observation_scope")
    grade = Grade.UNKNOWN
    reason = "executor facts incomplete"
    # The verifier's courtesy whole-tree run (no ``pytest:`` target named) on a
    # workspace pytest collected nothing from: exit 5, no node ids.  It attests
    # nothing, so it is graded PASS only as "nothing to attest" and only when bound
    # to the unchanged snapshot; a named target that collected nothing stays FAIL.
    vacuous = bool(runs) and all(
        row["target"] is None and row["no_tests_collected"] and run.get("returncode") == 5
        for run, row in zip(runs, documented, strict=True)
    )
    if state == "SUCCEEDED":
        if vacuous:
            if not isinstance(scope, Mapping):
                reason = "run not bound to this Result's unchanged workspace snapshot"
            else:
                grade = Grade.PASS
                reason = "no pytest target named; the executor collected no tests (exit 5), nothing to attest"
        elif any(int(run.get("returncode")) != 0 or not run.get("passed") for run in runs):
            grade, reason = Grade.FAIL, "a pytest target exited non-zero"
        elif not isinstance(scope, Mapping):
            reason = "run not bound to this Result's unchanged workspace snapshot"
        elif not all(row["nodeids"]["PASSED"] for row in documented) or any(
            row["nodeids"]["FAILED"] or row["nodeids"]["ERROR"] for row in documented
        ):
            reason = "executor reported no passing node id for a target"
        else:
            grade, reason = Grade.PASS, "every target exited 0 with reported passing node ids"
    document = {
        "schema": OUTPUT_SCHEMA,
        "layer": layer,
        "execution_state": state,
        "verdict": grade.value,
        "reason": reason,
        "targets": [row["target"] for row in documented],
        "runs": documented,
        "observation_scope": dict(scope) if isinstance(scope, Mapping) else None,
        "failure_nodes": list(detail.get("failure_nodes") or []),
        "layer_status": result.get("status"),
    }
    canonical(document)
    return state, grade, document


def record_actual_run(
    spec: AssuranceRef,
    *,
    mission_id: str,
    subject_hash: str,
    input_manifest_hash: str,
    environment_hash: str,
    implementation_hash: str,
    now_ms: Callable[[], int],
    state: str,
    outputs: tuple[AssertionOutput, ...],
) -> RecordedLocalCheck:
    """The receipt frame for a run that already happened elsewhere (the executor)."""
    if spec.kind != "check_spec":
        raise AssuranceError("CHECK_SPEC_REF_REQUIRED")
    one_of(state, {"SUCCEEDED", "ERROR", "CANCELLED"})
    if len(outputs) > 256 or any(not isinstance(row, AssertionOutput) for row in outputs):
        raise AssuranceError("CHECK_ASSERTION_INVALID")
    keys = [row.assertion_key for row in outputs]
    if len(keys) != len(set(keys)):
        raise AssuranceError("DUPLICATE_ASSERTION")
    started = integer(now_ms())
    body: dict[str, Any] = {
        "schema_version": 1,
        "mission_id": text(mission_id),
        "check_spec_ref": spec.to_json(),
        "subject_hash": digest(subject_hash),
        "input_manifest_hash": digest(input_manifest_hash),
        "environment_hash": digest(environment_hash),
        "run_nonce": str(uuid.uuid4()),
        "started_at_ms": started,
        "finished_at_ms": max(started, integer(now_ms())),
        "execution_state": state,
        "assertions": [row.to_json() for row in outputs],
        "recorder_implementation_hash": digest(implementation_hash),
    }
    return RecordedLocalCheck(canonical(body), outputs)


__all__ = ("OUTPUT_SCHEMA", "executor_run_facts", "pytest_nodeids", "record_actual_run")
