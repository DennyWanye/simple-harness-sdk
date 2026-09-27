# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""FAST / WAIT / SLOW / STOP progress routes (NEXT-TG-1.0 §3).

A pure mapping from facts the loop already trusts to a route and a named reason.
It opens no store, calls no model, writes nothing and grants nothing: the loop
gathers the facts, and every Commit and hand-off downstream re-checks the world.

The first consumer is the idle path. Before it, the stall record and the stall
confirmation each carried their own list of "this is a legal wait", and the lists
had drifted apart: the record did not know a judged Mission waiting for its
closeout (real run 2026-09-28, mission-e5f82ae8c9f24ff8), and neither knew an
UNKNOWN action under reconciliation or a person's pending approval, which §3.5
routes to WAIT. Both now ask :func:`idle_verdict`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Route(StrEnum):
    FAST = "FAST"
    WAIT = "WAIT"
    SLOW = "SLOW"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class ProgressDecision:
    """A route and why — advice and diagnosis, never a state, a budget or a licence."""

    route: Route
    scope_ref: str
    action: str
    reason_code: str
    wake: str | None = None
    issue_key: str | None = None


@dataclass(frozen=True, slots=True)
class IdleFacts:
    """What the loop knows about one ACTIVE Mission when a cycle did nothing."""

    mission_id: str
    all_rows_terminal: bool = False
    closeout_pending: bool = False
    root_resolved: bool = False
    running_rows: bool = False
    planning_wait: bool = False
    taskgraph_sources: bool = False
    operation_completion: bool = False
    assurance_work: bool = False
    unknown_actions: bool = False
    approvals_pending: bool = False
    repair_continuation_waiting: bool = False
    #: the plan withholds or admits something; ``None`` when it was not read
    plan_has_work: bool | None = None


#: §3.4 steps 3 and 6: existing work first, then named external waits. Order is
#: the reason reported when several hold; every entry is a WAIT with a wake source.
_WAITS: tuple[tuple[str, str, str], ...] = (
    ("closeout_pending", "CLOSEOUT_CONVERGING", "assurance closeout evaluation"),
    ("root_resolved", "ROOT_RESOLVED_AWAITING_JUDGMENT", "mission judgment"),
    ("running_rows", "WORK_RUNNING", "attempt result"),
    ("unknown_actions", "OPERATION_RECONCILIATION", "action reconciliation"),
    ("approvals_pending", "APPROVAL_PENDING", "a person's approval"),
    ("operation_completion", "OPERATION_OUTCOME_PENDING", "operation outcome"),
    ("assurance_work", "ASSURANCE_WORK_QUEUED", "assurance work"),
    ("repair_continuation_waiting", "REPAIR_CONTINUATION_WAITING", "repair continuation"),
    ("planning_wait", "PLANNING_WAIT", "planning wait target"),
    ("taskgraph_sources", "TASKGRAPH_SOURCES_PENDING", "taskgraph notification"),
)


def idle_verdict(facts: IdleFacts) -> ProgressDecision:
    """Route one idle Mission: WAIT on a named source, or a stop candidate.

    A STOP here is only a *candidate*: the loop records it and confirms it with one
    more full cycle before ending the Mission (§9.1 repeated no progress). Anything
    the facts cannot classify waits with ``SYSTEM_DIAGNOSIS_REQUIRED`` — never a
    default SLOW and never a stop (§3.4 step 7).
    """

    scope = f"mission:{facts.mission_id}"
    if facts.all_rows_terminal:
        # Nothing is left to dispatch because nothing is left: finishing is the
        # judge's and the finalizer's business, not a stall.
        return ProgressDecision(Route.FAST, scope, "FINALIZE", "ALL_WORK_TERMINAL")
    for name, reason, wake in _WAITS:
        if getattr(facts, name):
            return ProgressDecision(Route.WAIT, scope, "WAIT", reason, wake=wake)
    if facts.plan_has_work is None:
        return ProgressDecision(
            Route.WAIT, scope, "DIAGNOSE", "SYSTEM_DIAGNOSIS_REQUIRED", wake="plan readable"
        )
    if not facts.plan_has_work:
        return ProgressDecision(
            Route.WAIT, scope, "DIAGNOSE", "SYSTEM_DIAGNOSIS_REQUIRED", wake="plan change"
        )
    return ProgressDecision(Route.STOP, scope, "CONFIRM_STALL", "NO_DISPATCHABLE_WORK")


__all__ = ("IdleFacts", "ProgressDecision", "Route", "idle_verdict")
