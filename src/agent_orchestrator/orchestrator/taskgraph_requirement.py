# SPDX-License-Identifier: Apache-2.0
"""A Mission that must run on the strict TaskGraph (NEXT-TG-1.0 §6.4).

The requirement is written once, in the Mission's creation transaction, by the
deployment that creates it — never by a model, a page flag or an environment
variable.  Until the Mission is actually bound (``taskgraph_policy_bindings``, written
only by the authenticated EnableTaskGraphContract command) it may neither dispatch a
planning request nor commit a plan: it waits.  There is no fallback to the
unbound path, and an existing Mission is never marked afterwards.
"""
from __future__ import annotations

from typing import Any

from ..storage.store import Store, StoreError
from .taskgraph_dispatch import taskgraph_enabled
from .taskgraph_policy import KERNEL_VERSION

#: The one source this deployment writes: every new Mission it creates.
DEPLOYMENT_DEFAULT = "deployment_default"


def require_taskgraph(store: Store, mission_id: str, *, source: str = DEPLOYMENT_DEFAULT) -> None:
    """Mark a Mission being created as strict-TaskGraph-only, in its own transaction.

    Refused for a Mission that already has a plan revision or an Attempt: an older
    Mission is not converted, and no history is back-filled.
    """
    if not store.connection.in_transaction:
        raise StoreError("TASKGRAPH_REQUIREMENT_TRANSACTION_REQUIRED")
    if not isinstance(source, str) or not source:
        raise StoreError("TASKGRAPH_REQUIREMENT_SOURCE_REQUIRED")
    mission = store.get_mission(mission_id)
    if mission is None:
        raise StoreError("MISSION_NOT_FOUND")
    # The same eligibility the enable command checks: a Mission that could never be
    # bound (legacy semantics, or not the planning-decision protocol) must not be
    # marked — it would either wait forever or run unbound under a strict label.
    from ..contracts.planning_decisions import PLANNING_DECISION_V1
    from .plan_commits import HIERARCHICAL_SEMANTICS, semantics_of
    from .planning_protocol_binding import planning_protocol_for_mission
    protocol = planning_protocol_for_mission(store, mission_id)
    if (semantics_of(mission) != HIERARCHICAL_SEMANTICS or protocol is None
            or protocol.get("protocol_version") != PLANNING_DECISION_V1):
        raise StoreError("TASKGRAPH_REQUIREMENT_MISSION_NOT_ELIGIBLE")
    db = store.connection
    if (db.execute("SELECT 1 FROM plan_revisions WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone()
            or db.execute("SELECT 1 FROM attempts WHERE mission_id=? LIMIT 1", (mission_id,)).fetchone()):
        raise StoreError("TASKGRAPH_REQUIREMENT_ONLY_AT_CREATION")
    db.execute("INSERT OR IGNORE INTO taskgraph_requirements VALUES (?,?,?,?)",
               (mission_id, KERNEL_VERSION, source, store.now))


def taskgraph_required(store: Store, mission_id: str) -> bool:
    if not store.has_table("taskgraph_requirements"):
        return False
    row = store.connection.execute(
        "SELECT kernel_version FROM taskgraph_requirements WHERE mission_id=?", (mission_id,)).fetchone()
    if row is not None and row[0] != KERNEL_VERSION:
        raise StoreError("TASKGRAPH_KERNEL_UNSUPPORTED")
    return row is not None


def awaiting_taskgraph(store: Store, mission_id: str) -> bool:
    """Required but not (yet) bound: no plan may be dispatched or committed."""
    return taskgraph_required(store, mission_id) and not taskgraph_enabled(store, mission_id)


def current_planning_grant(store: Store, mission_id: str) -> bool:
    """Whether the Mission holds a planning grant the enable command could use now.

    The same currency the enable authority reads (active, inside its window, the
    Mission's current policy, REFINE and REPAIR allowed); the issuer is the
    deployment's single principal on the desktop.
    """
    from ..governance.planning_authorization import planning_policy_for_mission
    from ..storage.planning_admission_store import PlanningAdmissionStore
    policy, now = planning_policy_for_mission(store, mission_id), int(store.now * 1000)
    admission = PlanningAdmissionStore(store)
    for (grant_id,) in store.connection.execute(
            "SELECT DISTINCT grant_id FROM planning_lane_grants WHERE mission_id=?", (mission_id,)).fetchall():
        grant = admission.get_grant(grant_id)
        if (grant is not None and grant["active"] and grant["policy_hash"] == policy.policy_hash
                and grant["not_before_ms"] <= now < grant["expires_at_ms"]
                and {"REFINE", "REPAIR/REPLACE_METHOD"} <= set(grant["allowed_decisions"])):
            return True
    return False


def awaits_taskgraph(store: Store, intent: Any) -> bool:
    """A planning request of a Mission still waiting for its TaskGraph binding.

    Held only while a current planning grant exists — the binding can then still
    come.  Without one (expired, revoked) the request is released to its original
    admission, whose normal refusal asks for authority again; the commit gate still
    refuses any unbound plan, so release never means an unbound plan.

    Method synthesis is not a planning request (it needs no planning authority and
    commits no plan), so it is not held here.
    """
    if intent.kind != "plan":
        return False
    from ..storage.planning_decision_store import PlanningDecisionStore
    with store.read_view():
        if not awaiting_taskgraph(store, intent.mission_id):
            return False
        if PlanningDecisionStore(store).get_planning_request_for_intent(intent.intent_id) is None:
            return False
        return current_planning_grant(store, intent.mission_id)


def missions_awaiting_taskgraph(store: Store) -> list[str]:
    """Non-terminal Missions that are required but not yet bound, oldest first."""
    if not store.has_table("taskgraph_requirements"):
        return []
    rows = store.connection.execute(
        "SELECT r.mission_id FROM taskgraph_requirements r JOIN missions m ON m.mission_id=r.mission_id "
        "LEFT JOIN taskgraph_policy_bindings b ON b.mission_id=r.mission_id "
        "WHERE b.mission_id IS NULL AND m.status NOT IN ('COMPLETED','FAILED','CANCELLED') "
        "ORDER BY r.created_at, r.mission_id").fetchall()
    return [str(row[0]) for row in rows]


def enable_command_id(mission_id: str) -> str:
    """The one command id for a Mission's enable: concurrent tries yield one binding."""
    return f"taskgraph-enable:{mission_id}:{KERNEL_VERSION}"


__all__ = ("DEPLOYMENT_DEFAULT", "require_taskgraph", "taskgraph_required", "awaiting_taskgraph",
           "current_planning_grant", "awaits_taskgraph", "missions_awaiting_taskgraph", "enable_command_id")
