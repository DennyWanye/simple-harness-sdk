# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Metrics (§23.2, theory 12-3 / 12-14, plan D6-10 / D6-11').

What can be filled from this build's records: system health (Attempt outcomes,
timeouts, backpressure transitions and the peak queue observations), cost (tokens by
role and by runtime profile; money only when the deployment is priced — otherwise
``null`` with the reason, never zero), knowledge reuse, verification pass rate, Mission
duration, and the role distribution against §29.2's starting mix (all eight §9.2 roles,
``null`` where the original gives no share)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..runtime.role_templates import ROLE_MIX_START
from ..storage.store import Store

METRICS_VERSION = "metrics-v1"
ALL_ROLES = (
    "explorer",
    "exploiter",
    "critic",
    "simplifier",
    "connector",
    "failure_analyst",
    "synthesizer",
    "verifier",
)


def _service_role(subject_id: str) -> str:
    for marker, role in ((":planner:", "planner"), (":manager:", "manager"), (":critic", "critic")):
        if marker in subject_id:
            return role
    if "-judge-" in subject_id:
        return "critic"
    return "service"


def metrics(store: Store, mission_id: str, *, unpriced: bool) -> dict[str, Any]:
    attempts = [a for t in store.list_tasks(mission_id) for a in store.list_attempts(t.id)]
    by_id = {a.id: a for a in attempts}
    statuses = Counter(str(a.status) for a in attempts)
    intents = {
        i.subject_id: i
        for i in store.list_intents(
            "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
        )
        if i.mission_id == mission_id
    }
    tokens_by_role: Counter[str] = Counter()
    tokens_by_profile: Counter[str] = Counter()
    cost_by_role: Counter[str] = Counter()
    cost_by_profile: Counter[str] = Counter()
    rows = store.connection.execute(
        "SELECT subject_id, input_tokens + output_tokens, cost_micros FROM imported_usage"
        " WHERE mission_id = ?",
        (mission_id,),
    ).fetchall()
    for subject_id, tokens, cost in rows:
        attempt = by_id.get(subject_id)
        if attempt is not None:
            role, profile = attempt.role, attempt.runtime_profile_id
        else:
            intent = intents.get(subject_id)
            role = _service_role(subject_id)
            profile = str((intent.config if intent else {}).get("runtime_profile_id", "default"))
        tokens_by_role[role] += int(tokens)
        tokens_by_profile[profile] += int(tokens)
        if cost is not None:
            cost_by_role[role] += int(cost)
            cost_by_profile[profile] += int(cost)
    passed = store.count_events(mission_id, "VerificationPassed")
    failed = store.count_events(mission_id, "VerificationFailed")
    events = store.list_events(mission_id)
    created = next((e.created_at for e in events if e.type == "MissionCreated"), None)
    ended = next(
        (
            e.created_at
            for e in events
            if e.type in {"MissionCompleted", "MissionFailed", "MissionCancelled"}
        ),
        None,
    )
    backpressure = store.get_scheduler_state("backpressure") or {}
    peaks: dict[str, int] = {}
    for entry in backpressure.get("log") or []:
        dim = str(entry.get("dimension"))
        peaks[dim] = max(peaks.get(dim, 0), int(entry.get("observed", 0)))
    critic_turns = sum(1 for s in intents if _service_role(s) == "critic")
    role_counts = Counter(a.role for a in attempts)
    role_counts["critic"] += critic_turns
    total_roles = sum(role_counts.values()) or 1
    role_mix = {
        role: {
            "start_share": ROLE_MIX_START.get(role),
            "observed": role_counts.get(role, 0),
            "observed_share": round(role_counts.get(role, 0) / total_roles, 4),
        }
        for role in ALL_ROLES
    }
    role_mix["worker"] = {  # this build's default Worker template sits outside the §9.2 names
        "start_share": None,
        "observed": role_counts.get("worker", 0),
        "observed_share": round(role_counts.get("worker", 0) / total_roles, 4),
    }
    return {
        "version": METRICS_VERSION,
        "mission_id": mission_id,
        "health": {
            "attempts": len(attempts),
            "attempts_by_status": dict(statuses),
            "timed_out": statuses.get("TIMED_OUT", 0),
            "lost": statuses.get("LOST", 0),
            "tool_calls_rejected": store.count_events(mission_id, "ToolCallRejected"),
            "backpressure_transitions": len(backpressure.get("log") or []),
            "peak_observed": peaks,
            "profile_unavailable_events": store.count_events(
                mission_id, "RuntimeProfileUnavailable"
            ),
        },
        "cost": {
            "tokens_by_role": dict(tokens_by_role),
            "tokens_by_profile": dict(tokens_by_profile),
            "cost_micros_by_role": None if unpriced else dict(cost_by_role),
            "cost_micros_by_profile": None if unpriced else dict(cost_by_profile),
            "cost_note": "unpriced deployment: money is not recorded (never written as zero)"
            if unpriced
            else None,
        },
        "verification": {
            "passed": passed,
            "failed": failed,
            "pass_rate": None if passed + failed == 0 else round(passed / (passed + failed), 4),
        },
        "knowledge": {
            "reuse_events": store.count_events(mission_id, "KnowledgeUsed"),
            "committed": store.count_events(mission_id, "KnowledgeCommitted"),
        },
        "mission": {
            "duration_seconds": None
            if created is None or ended is None
            else round(ended - created, 3),
            "graph_changes": store.count_events(mission_id, "TaskGraphChanged"),
            "escalations": sum(
                1 for e in events if e.type == "ModelRouted" and e.payload.get("escalated_from")
            ),
        },
        "role_mix": role_mix,
    }


__all__ = ("ALL_ROLES", "METRICS_VERSION", "metrics")
