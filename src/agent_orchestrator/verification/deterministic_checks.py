# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Deterministic verification layers (§14.1 layers 1, 2 and 4).

* ``format_check``  — the Result Envelope parsed against §26.4 (already done by the
  collector; recorded here as a layer so the policy is auditable).
* ``rule_check``    — artifacts referenced by the envelope exist in the verification
  copy with the recorded hash; claims cite evidence; ``file:`` criteria hold.
* ``code_test``     — pytest in the verification copy (child process, timeout).

Each layer returns a ``LayerResult`` with PASS / FAIL / ERROR; NOT_REQUIRED is
produced by the router for layers the Task policy did not ask for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..artifacts.workspace import Workspace, sha256_file
from ..contracts import Artifact, ResultEnvelope, Task
from ..runtime.tool_gateway import run_pytest

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True, slots=True)
class LayerResult:
    layer: str
    status: str
    summary: str
    detail: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "status": self.status,
            "summary": self.summary,
            "detail": dict(self.detail),
        }


def format_check(envelope: ResultEnvelope, *, client_result_id: str | None) -> LayerResult:
    return LayerResult(
        "format_check",
        PASS,
        "Result Envelope conforms to §26.4",
        {
            "result_id": envelope.id,
            "client_result_id": client_result_id,
            "outcome": str(envelope.outcome),
        },
    )


def rule_check(
    envelope: ResultEnvelope,
    task: Task,
    *,
    artifacts: Sequence[Artifact],
    verification_copy: Workspace,
    tampered: Sequence[str] = (),
) -> LayerResult:
    problems: list[str] = [
        f"protected seed file rewritten by the Worker: {path}" for path in tampered
    ]
    by_path = {artifact.path: artifact for artifact in artifacts}
    if envelope.outcome.value != "candidate":
        problems.append(f"outcome is {envelope.outcome}, not a candidate")
    if not envelope.artifacts:
        problems.append("no artifacts were submitted")
    for reference in envelope.artifacts:
        artifact = by_path.get(reference)
        if artifact is None:
            problems.append(f"artifact {reference!r} is not a recorded workspace file")
            continue
        try:
            actual = sha256_file(verification_copy.resolve(reference))
        except Exception as error:  # noqa: BLE001
            problems.append(
                f"artifact {reference!r} is unreadable in the verification copy: {error}"
            )
            continue
        if actual != artifact.content_hash:
            problems.append(f"artifact {reference!r} hash differs from the recorded artifact")
    if not envelope.claims:
        problems.append("no claims were submitted")
    if envelope.claims and not envelope.evidence:
        problems.append("claims cite no evidence")
    for criterion in task.success_criteria:
        if criterion.startswith("file:"):
            relative = criterion.removeprefix("file:")
            try:
                if not verification_copy.resolve(relative).is_file():
                    problems.append(f"success criterion {criterion!r} not met: file missing")
            except Exception as error:  # noqa: BLE001
                problems.append(f"success criterion {criterion!r} unreadable: {error}")
    status = FAIL if problems else PASS
    return LayerResult(
        "rule_check",
        status,
        "; ".join(problems)
        if problems
        else "artifacts, hashes, claims and file criteria consistent",
        {"problems": problems, "checked_artifacts": list(envelope.artifacts)},
    )


async def code_test(
    task: Task,
    *,
    verification_copy: Workspace,
    timeout: float,
    mission_criteria: Sequence[str] = (),
) -> LayerResult:
    """Run every ``pytest:`` criterion (Task and Mission) in the verification copy."""

    targets: list[str | None] = []
    for criterion in (*task.success_criteria, *mission_criteria):
        if criterion.startswith("pytest:"):
            target = criterion.removeprefix("pytest:").strip() or None
            if target not in targets:
                targets.append(target)
    if not targets:
        targets.append(None)
    runs: list[dict[str, Any]] = []
    failed = False
    for target in targets:
        if target is not None:
            try:
                verification_copy.resolve(target)
            except Exception as error:  # noqa: BLE001
                runs.append({"target": target, "error": str(error)})
                failed = True
                continue
        run = await run_pytest(str(verification_copy.root), path=target, timeout=timeout)
        runs.append({"target": target, **run.to_json(), "passed": run.passed})
        failed = failed or not run.passed
    summary = (
        "all pytest targets passed"
        if not failed
        else "pytest failed: "
        + "; ".join(
            (r.get("stdout") or r.get("error") or "")[-300:].strip().splitlines()[-1]
            if (r.get("stdout") or r.get("error"))
            else "no output"
            for r in runs
            if not r.get("passed")
        )
    )
    return LayerResult("code_test", FAIL if failed else PASS, summary, {"runs": runs})


__all__ = (
    "ERROR",
    "FAIL",
    "NOT_REQUIRED",
    "PASS",
    "LayerResult",
    "code_test",
    "format_check",
    "rule_check",
)
