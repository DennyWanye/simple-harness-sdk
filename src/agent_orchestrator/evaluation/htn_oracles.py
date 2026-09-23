# SPDX-License-Identifier: Apache-2.0
"""H8 domain graders. Run after execution; never expose hidden scores to an Agent."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from .htn_matrix import EvidenceFile, ScenarioDefinition


def write_evidence(root: Path, relative: str, value: Any) -> EvidenceFile:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ContractError("evidence output escapes its episode directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    content = canonical_json(json.loads(json.dumps(value, allow_nan=False))).encode("utf-8")
    # A grader rerun must preserve the earlier score; differing output gets a new
    # run/attempt directory rather than overwriting historical failure evidence.
    if path.exists() and path.read_bytes() != content:
        raise ContractError("episode evidence already contains a different result")
    path.write_bytes(content)
    return EvidenceFile(relative, hashlib.sha256(content).hexdigest())


async def grade_code(scenario: ScenarioDefinition, workspace: Path, root: Path, *, executor: Any) -> tuple[bool, EvidenceFile]:
    if scenario.oracle.get("kind") != "isolated_pytest":
        raise ContractError("code oracle mismatch")
    immutable = scenario.oracle["immutable_files"]
    changed = [name for name, body in immutable.items()
               if not (workspace / name).is_file() or (workspace / name).read_text() != body]
    if changed:
        return False, write_evidence(root, "oracle/code.json", {"passed": False, "reason": "oracle_modified",
            "paths": changed, "oracle_hash": content_hash_of(dict(scenario.oracle))})
    hidden = scenario.oracle.get("hidden_files", {})
    test_paths = scenario.oracle.get("test_paths", ["tests/test_target.py"])
    if (not isinstance(hidden, dict) or set(hidden) & set(immutable)
            or not isinstance(test_paths, list) or not test_paths
            or any(name not in {*immutable, *hidden} or not name.endswith(".py") for name in test_paths)):
        raise ContractError("code oracle test paths must name distinct frozen test files")
    for name, body in {**immutable, **hidden}.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or name == "target.py" or not isinstance(body, str):
            raise ContractError("code oracle files must be confined test sources")
    from ..runtime.sandbox import SandboxSpec
    target = workspace / "target.py"
    with tempfile.TemporaryDirectory(prefix="h6-hidden-oracle-", dir=root) as scratch:
        grading_workspace = workspace
        if hidden:
            # Hidden checks enter only this final grader directory, after the
            # Mission has ended. Never seed them into a model-readable workspace.
            grading_workspace = Path(scratch)
            if target.is_file():
                (grading_workspace / "target.py").write_bytes(target.read_bytes())
            for name, body in {**immutable, **hidden}.items():
                destination = grading_workspace / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(body)
        completed = await executor.execute(
            (executor.interpreter, "-m", "pytest", "-q", *test_paths),
            cwd=str(grading_workspace), spec=SandboxSpec(wall_seconds=float(scenario.oracle["timeout_seconds"])))
        hidden_unchanged = all((grading_workspace / name).is_file()
            and (grading_workspace / name).read_text() == body for name, body in hidden.items())
    unchanged = all((workspace / name).is_file() and (workspace / name).read_text() == body
                    for name, body in immutable.items())
    receipt = {"passed": completed.exit_code == 0 and completed.status == "ok"
                   and not completed.timed_out and not completed.limit_exceeded
                   and not completed.residual_pids and unchanged and hidden_unchanged,
               "oracle_unchanged": unchanged, "execution": completed.to_json(),
               "hidden_oracle_unchanged": hidden_unchanged,
               "oracle_hash": content_hash_of(dict(scenario.oracle)),
               "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None}
    return bool(receipt["passed"]), write_evidence(root, "oracle/code.json", receipt)


def grade_drone(scenario: ScenarioDefinition, *, database: Path, mission_id: str,
                root: Path) -> tuple[bool, EvidenceFile]:
    if scenario.oracle.get("kind") != "drone_sqlite_state":
        raise ContractError("drone oracle mismatch")
    # Read one actual SQLite snapshot. The model's textual completion claim and
    # report are deliberately not inputs to the simulator state oracle.
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT state_json FROM episodes WHERE mission_id=?", (mission_id,)).fetchone()
        commands = db.execute("SELECT call_id,command_hash,receipt_json FROM commands WHERE mission_id=? ORDER BY rowid", (mission_id,)).fetchall()
    if row is None:
        raise ContractError("drone oracle cannot find the actual episode")
    state = json.loads(row[0])
    receipts = []
    for call_id, command_hash, raw in commands:
        receipt = json.loads(raw)
        body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        if (receipt.get("call_id") != call_id or receipt.get("command_hash") != command_hash
                or receipt.get("mission_id") != mission_id or receipt.get("receipt_hash") != content_hash_of(body)
                or receipt.get("after_hash") != content_hash_of(receipt["state"])):
            raise ContractError("drone command receipt is not bound to its actual stored state")
        receipts.append(receipt)
    point = scenario.oracle["capture_at"]
    capture_ids = {receipt["call_id"] for receipt in receipts if receipt["command"] == "capture"}
    captured = any(c["capture_id"] in capture_ids and all(c.get(axis) == point[axis] for axis in ("x", "y", "z"))
                   for c in state["captures"])
    landed = state["airborne"] is False and state["z"] == 0
    last_matches = bool(receipts) and receipts[-1]["after_hash"] == content_hash_of(state)
    success = captured and landed and last_matches
    summary = {"passed": success, "captured_at_requested_point": captured, "landed": landed,
               "final_state_matches_receipt": last_matches, "state_hash": content_hash_of(state),
               "command_receipt_hashes": [r["receipt_hash"] for r in receipts], "simulated": True,
               "oracle_hash": content_hash_of(dict(scenario.oracle))}
    return success, write_evidence(root, "oracle/drone.json", summary)


def grade_appworld(scenario: ScenarioDefinition, episode: Any, root: Path) -> tuple[bool, EvidenceFile]:
    if scenario.oracle.get("kind") != "appworld_external_evaluator" or episode.config.task_id != scenario.fixture["task_id"]:
        raise ContractError("AppWorld oracle requires the exact running dataset episode")
    result = episode.finalize()
    # Keep the official evaluator payload in the local evidence directory. Do not
    # reinterpret an executor's success flag as an external benchmark score.
    ref = write_evidence(root, "oracle/appworld.json", result)
    success = result.get("success")
    if type(success) is not bool:
        raise ContractError("official AppWorld evaluator did not return a boolean success result")
    return success, ref
