# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Context Builder (§10), step-2 subset.

Assembles the *task package* an Agent starts from.  Of §10's eleven items this
step provides 1 (Mission root goal), 2 (Task Contract), 6 (failure history of
previous Attempts), 8 (latest Verifier feedback), 9 (tools & permissions), 10
(budget) and 11 (structured output requirement).  Items 3–5 and 7 (dependencies,
branch summary, Verified Knowledge, disputed Claims) need the DAG / Blackboard of
steps 3–4 and are deliberately absent — never fabricated (ORCH §5.2).

The package is serialised deterministically; its hash is the Attempt's
``context_version`` (§26.3) and is frozen inside the dispatch intent (D5').
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from .. import __version__ as PACKAGE_VERSION
from ..contracts import Attempt, Mission, Task
from ..contracts.models import sha256_hex

CONTEXT_BUILDER_VERSION = "context-builder-v1"


@dataclass(frozen=True, slots=True)
class TaskPackage:
    text: str
    context_version: str
    package: Mapping[str, Any]


def _render(package: Mapping[str, Any]) -> str:
    """Human/model readable rendering with a stable field order."""

    lines = []
    for key, value in package.items():
        if isinstance(value, (dict, list)):
            lines.append(f"## {key}\n{canonical_json(value)}")
        else:
            lines.append(f"## {key}\n{value}")
    return "\n\n".join(lines)


def _seal(package: dict[str, Any]) -> TaskPackage:
    package = {"context_builder_version": CONTEXT_BUILDER_VERSION, **package}
    version = "ctx-" + sha256_hex(package)[:16]
    return TaskPackage(text=_render(package), context_version=version, package=package)


def build_worker_package(
    mission: Mission,
    task: Task,
    attempt: Attempt,
    *,
    previous_attempts: Sequence[Attempt],
    verifier_feedback: Sequence[Mapping[str, Any]],
    workspace_files: Sequence[str],
    dependencies: Sequence[Mapping[str, Any]] = (),
) -> TaskPackage:
    failures = [
        {
            "attempt_id": previous.id,
            "status": str(previous.status),
            "failure": dict(previous.failure or {}),
        }
        for previous in previous_attempts
        if previous.failure is not None
    ]
    package: dict[str, Any] = {
        "role": "worker",
        "mission_root_goal": mission.goal,  # §10 item 1
        "mission_success_criteria": list(mission.success_criteria),
        "task_contract": {  # §10 item 2
            "task_id": task.id,
            "task_version": task.version,
            "goal": task.goal,
            "rationale": task.rationale,
            "success_criteria": list(task.success_criteria),
            "verification_policy": list(task.verification_policy),
        },
        "attempt": {
            "attempt_id": attempt.id,
            "ordinal": attempt.ordinal,
            "retry_of": attempt.retry_of,
        },
        "dependencies": [dict(item) for item in dependencies],  # §10 item 3 (step 3)
        "failure_history": failures,  # §10 item 6
        "verifier_feedback": [dict(item) for item in verifier_feedback],  # §10 item 8
        "feedback": list(attempt.feedback),
        "tools_and_permissions": {  # §10 item 9
            "allowed_tools": list(task.allowed_tools),
            "workspace": "isolated; only the listed tools reach it",
            "workspace_files": list(workspace_files),
        },
        "budget": {  # §10 item 10
            "reserved": attempt.budget_reserved.to_json(),
            "task": task.budget.to_json(),
        },
        "output_contract": "<result_envelope>{json}</result_envelope>",  # §10 item 11
        "absent_by_design": [
            "branch_summary",
            "verified_knowledge",
            "disputed_claims",
        ],
    }
    return _seal(package)


def build_planner_package(
    mission: Mission, *, workspace_files: Sequence[str], attempt_ordinal: int
) -> TaskPackage:
    package: dict[str, Any] = {
        "role": "planner",
        "mission": {
            "mission_id": mission.id,
            "goal": mission.goal,
            "success_criteria": list(mission.success_criteria),
            "allowed_tools": list(mission.allowed_tools),
            "budget": mission.budget.to_json(),
            "risk_level": mission.risk_level,
        },
        "planning_attempt": attempt_ordinal,
        "workspace_files": list(workspace_files),
        "constraint": "exactly one Task; success_criteria must be machine-checkable",
        "output_contract": "<task_proposal>{json}</task_proposal>",
        "package_version": PACKAGE_VERSION,
    }
    return _seal(package)


def build_critic_package(
    mission: Mission,
    task: Task,
    *,
    attempt_id: str,
    artifacts: Sequence[Mapping[str, Any]],
    test_output: str | None,
    workspace_files: Sequence[str],
) -> TaskPackage:
    package: dict[str, Any] = {
        "role": "critic",
        "mission_root_goal": mission.goal,
        "mission_success_criteria": list(mission.success_criteria),
        "task_contract": {
            "task_id": task.id,
            "task_version": task.version,
            "goal": task.goal,
            "success_criteria": list(task.success_criteria),
        },
        "attempt_id": attempt_id,
        "submitted_artifacts": [dict(item) for item in artifacts],
        "test_output": test_output,
        "workspace_files": list(workspace_files),
        "visibility": "verification copy only; the Worker's own explanation is withheld (§10.2)",
        "output_contract": "<critic_verdict>{json}</critic_verdict>",
    }
    return _seal(package)


__all__ = (
    "CONTEXT_BUILDER_VERSION",
    "TaskPackage",
    "build_critic_package",
    "build_planner_package",
    "build_worker_package",
)
