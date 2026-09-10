# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Versioned transitions for Mission / Task / Attempt / Claim (§25, §17.3).

Each helper returns a *new* frozen entity at ``version + 1`` after asserting the
transition is one drawn in §25.  The Commit Service writes the returned entity
with a CAS on the old version, so a stale writer can never skip a state.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..contracts import (
    Attempt,
    AttemptStatus,
    Claim,
    ClaimStatus,
    Mission,
    MissionStatus,
    Task,
    TaskStatus,
    assert_attempt_transition,
    assert_claim_transition,
    assert_mission_transition,
    assert_task_transition,
)


def next_mission(mission: Mission, status: MissionStatus | None = None, **changes: Any) -> Mission:
    if status is not None and status is not mission.status:
        assert_mission_transition(mission.status, status)
        changes["status"] = status
    return replace(mission, version=mission.version + 1, **changes)


def next_task(task: Task, status: TaskStatus | None = None, **changes: Any) -> Task:
    if status is not None and status is not task.status:
        assert_task_transition(task.status, status)
        changes["status"] = status
    return replace(task, version=task.version + 1, **changes)


def next_attempt(attempt: Attempt, status: AttemptStatus | None = None, **changes: Any) -> Attempt:
    if status is not None and status is not attempt.status:
        assert_attempt_transition(attempt.status, status)
        changes["status"] = status
    return replace(attempt, version=attempt.version + 1, **changes)


def next_claim(claim: Claim, status: ClaimStatus | None = None, **changes: Any) -> Claim:
    if status is not None and status is not claim.status:
        assert_claim_transition(claim.status, status)
        changes["status"] = status
    return replace(claim, version=claim.version + 1, **changes)


__all__ = ("next_attempt", "next_claim", "next_mission", "next_task")
