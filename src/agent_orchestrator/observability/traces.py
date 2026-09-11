# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Contribution attribution (original §23.3 "需要记录哪些知识位于最终成功路径上";
theory 12 §16 / 13 §17 Credit Assignment; ORCH §10.2 ``observability/traces.py``; plan
D8-4 / D8-4').

From the Mission's final products — the integrated tree the judgment used, i.e. every
completed Task's accepted artifacts merged along the dependency chain — to the Task,
Attempt, Agent, role, model and prompt version that produced them, the verification
layers that let them through, the knowledge path (``lineage``) and the actions and
people on the way.  Everything that did not reach the success path is listed as
exploration spending with its reason, and the Mission's imported usage is split row by
row into path / exploration / services with an "unclassified" bucket that must stay
empty.  A record that is missing is reported as a break — never bridged."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from ..artifacts.versioning import ArtifactConflict, ancestors, merge_accepted
from .lineage import lineage
from .metrics import _service_role

if TYPE_CHECKING:
    from ..storage.store import Store

ATTRIBUTION_VERSION = "attribution-v1"
UNPRICED_NOTE = "unpriced deployment: money is not recorded (null, never zero)"


def _bucket() -> dict[str, Any]:
    return {"tokens": 0, "cost_micros": 0, "priced": True, "rows": 0}


def _add(bucket: dict[str, Any], tokens: int, cost: int | None, unpriced: bool) -> None:
    bucket["tokens"] += int(tokens)
    bucket["rows"] += 1
    if cost is None or unpriced:
        bucket["priced"] = False
    else:
        bucket["cost_micros"] += int(cost)


def _money(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "tokens": bucket["tokens"],
        "rows": bucket["rows"],
        "cost_micros": bucket["cost_micros"] if bucket["priced"] and bucket["rows"] else None,
        "cost_note": None if bucket["priced"] or not bucket["rows"] else UNPRICED_NOTE,
    }


def attribution(store: Store, mission_id: str) -> dict[str, Any]:  # noqa: C901 - one report
    mission = store.get_mission(mission_id)
    if mission is None:
        return {
            "version": ATTRIBUTION_VERSION,
            "mission_id": mission_id,
            "breaks": [{"missing": "mission"}],
        }
    tasks = store.list_tasks(mission_id)
    by_id = {task.id: task for task in tasks}
    breaks: list[dict[str, Any]] = []
    success = str(mission.status) == "COMPLETED"

    def accepted_attempt(task: Any) -> str | None:
        if not task.accepted_result_id:
            return None
        stored = store.get_result(task.accepted_result_id)
        if stored is None:
            breaks.append({"missing": "result", "id": task.accepted_result_id, "task_id": task.id})
            return None
        return str(stored.envelope.attempt_id)

    # -- the final products and the success path (plan D8-4' rules)
    products: list[dict[str, Any]] = []
    path_tasks: set[str] = set()
    if success:
        live = [t for t in tasks if str(t.status) == "COMPLETED"]
        artifacts_by_task: dict[str, list[Any]] = {}
        for task in live:
            found = []
            for artifact_id in task.accepted_artifacts:
                artifact = store.get_artifact(artifact_id)
                if artifact is None:
                    breaks.append({"missing": "artifact", "id": artifact_id, "task_id": task.id})
                    continue
                found.append(artifact)
            artifacts_by_task[task.id] = found
        try:
            merged = merge_accepted(live, artifacts_by_task, tasks_by_id=by_id)
        except ArtifactConflict as error:
            breaks.append({"missing": "integrated_tree", "error": str(error)})
            merged = []
        for item in merged:
            path_tasks.add(item.task_id)
            path_tasks.update(
                t.id for t in ancestors(item.task_id, by_id) if str(t.status) == "COMPLETED"
            )
        final_ids = {item.artifact_id for item in merged}
        for item in merged:
            producer = by_id.get(item.task_id)
            attempt_id = None if producer is None else accepted_attempt(producer)
            attempt = None if attempt_id is None else store.get_attempt(attempt_id)
            layers = (
                []
                if producer is None or not producer.accepted_result_id
                else [
                    {
                        "layer": v["layer"],
                        "status": v["status"],
                        "verifier_version": (v.get("detail") or {}).get("verifier_version"),
                    }
                    for v in store.list_verifications(producer.accepted_result_id)
                    if v["status"] in {"PASS", "NEEDS_HUMAN"}
                ]
            )
            products.append(
                {
                    "path": item.path,
                    "content_hash": item.content_hash,
                    "artifact_id": item.artifact_id,
                    "task_id": item.task_id,
                    "result_id": None if producer is None else producer.accepted_result_id,
                    "attempt_id": attempt_id,
                    "agent_id": None if attempt is None else attempt.agent_id,
                    "role": None if attempt is None else attempt.role,
                    "model": None if attempt is None else attempt.model,
                    "runtime_profile_id": None if attempt is None else attempt.runtime_profile_id,
                    "prompt_version": None if attempt is None else attempt.prompt_version,
                    "verified_by": layers,
                }
            )
    else:
        final_ids = set()
    path_task_view = [
        {
            "task_id": tid,
            "kind": by_id[tid].kind,
            "accepted_result_id": by_id[tid].accepted_result_id,
            "artifacts": [
                {
                    "artifact_id": aid,
                    "in_final_products": aid in final_ids,
                }  # overridden upstream work stays on the path
                for aid in by_id[tid].accepted_artifacts
            ],
        }
        for tid in sorted(path_tasks)
    ]

    # -- the knowledge path; a refuted claim's Attempt is exploration unless on the path anyway
    knowledge = (
        lineage(store, mission_id)
        if success
        else {"knowledge": [], "claims": [], "attempts": [], "edges": []}
    )
    refuted = {
        str(c.get("source_attempt")) for c in knowledge.get("claims", []) if c.get("source_attempt")
    }
    path_attempts = {aid for tid in path_tasks if (aid := accepted_attempt(by_id[tid]))}
    path_attempts.update(
        str(a["attempt_id"])
        for a in knowledge.get("attempts", [])
        if str(a["attempt_id"]) not in refuted
    )

    # -- usage, row by row (plan D8-4')
    attempts = {a.id: a for t in tasks for a in store.list_attempts(t.id)}
    per_attempt: dict[str, dict[str, Any]] = defaultdict(_bucket)
    per_attempt_verification: dict[str, dict[str, Any]] = defaultdict(_bucket)
    services: dict[str, dict[str, Any]] = defaultdict(_bucket)
    unclassified = _bucket()
    unclassified_subjects: list[str] = []
    total = _bucket()
    unknown_rows = 0
    usage_by_subject: dict[str, int] = defaultdict(int)
    rows = store.connection.execute(
        "SELECT subject_id, input_tokens + output_tokens, cost_micros, unpriced, unknown"
        " FROM imported_usage WHERE mission_id = ?",
        (mission_id,),
    ).fetchall()
    for subject, tokens, cost, unpriced, unknown in rows:
        subject = str(subject)
        tokens = int(tokens or 0)
        _add(total, tokens, cost, bool(unpriced))
        usage_by_subject[subject] += tokens
        unknown_rows += int(bool(unknown))
        owner = subject.split(":critic:")[0] if ":critic:" in subject else None
        if subject in attempts:
            _add(per_attempt[subject], tokens, cost, bool(unpriced))
        elif owner is not None and owner in attempts:
            _add(per_attempt_verification[owner], tokens, cost, bool(unpriced))
        else:
            role = _service_role(subject)
            if role in {"planner", "manager"}:
                _add(services[role], tokens, cost, bool(unpriced))
            elif role == "critic":  # the Mission-level judgment Critic
                _add(services["judge"], tokens, cost, bool(unpriced))
            else:
                _add(unclassified, tokens, cost, bool(unpriced))
                unclassified_subjects.append(subject)
    tool_calls: dict[str, int] = {}
    action_reservations = []
    for subject, settled_calls, state in store.connection.execute(
        "SELECT subject_id, settled_tool_calls, state FROM budget_reservations"
        " WHERE mission_id = ?",
        (mission_id,),
    ).fetchall():
        if str(subject).startswith("action:"):
            action_reservations.append(
                {"subject_id": subject, "settled_tool_calls": settled_calls, "state": state}
            )
        else:
            tool_calls[str(subject)] = int(settled_calls or 0)

    path_bucket, exploration_bucket = _bucket(), _bucket()
    broken_tasks = {b["task_id"] for b in breaks if b.get("missing") == "result"}
    attempt_view = []
    for attempt_id, attempt in attempts.items():
        owner_task = by_id.get(attempt.task_id)
        on_path = attempt_id in path_attempts
        reason = None
        if not on_path:
            status = str(attempt.status)
            if attempt_id in refuted:
                reason = "claim_refuted"
            elif owner_task is not None and str(owner_task.status) == "CANCELLED":
                reason = "task_superseded_or_cancelled"
            elif status == "SUPERSEDED":
                reason = "candidate_superseded"
            elif status in {"RETRY_WAIT", "LOST", "TIMED_OUT", "CANCELLED"}:
                reason = f"attempt_{status.lower()}"
            elif attempt.task_id in broken_tasks:  # review P2-6: the record is missing, say so
                reason = "record_missing"
            elif not success:
                reason = "mission_not_completed"
            else:
                reason = "not_used_by_final_products"
        work, check = (
            per_attempt.get(attempt_id, _bucket()),
            per_attempt_verification.get(attempt_id, _bucket()),
        )
        target = path_bucket if on_path else exploration_bucket
        for bucket in (work, check):
            target["tokens"] += bucket["tokens"]
            target["rows"] += bucket["rows"]
            target["cost_micros"] += bucket["cost_micros"]
            target["priced"] = target["priced"] and bucket["priced"]
        attempt_view.append(
            {
                "attempt_id": attempt_id,
                "task_id": attempt.task_id,
                "agent_id": attempt.agent_id,
                "role": attempt.role,
                "model": attempt.model,
                "runtime_profile_id": attempt.runtime_profile_id,
                "prompt_version": attempt.prompt_version,
                "status": str(attempt.status),
                "on_success_path": on_path,
                # review P1-1 (plan D8-4'' revision): an Attempt whose own accepted products are
                # on the path stays there, flagged when one of its claims was refuted
                "claim_refuted": attempt_id in refuted,
                "exploration_reason": reason,
                "work": _money(work),
                "verification": _money(check),
                "tool_calls": tool_calls.get(attempt_id, 0),
            }
        )
    service_total = sum(b["tokens"] for b in services.values())
    # review P1-4: reconciled = nothing unclassified, the buckets add up, and the budget
    # ledger (an independent record: what each settled reservation was charged) agrees
    settled = {
        str(subject): int(tokens or 0)
        for subject, tokens in store.connection.execute(
            "SELECT subject_id, settled_tokens FROM budget_reservations"
            " WHERE mission_id = ? AND state = 'SETTLED' AND subject_id NOT LIKE 'action:%'",
            (mission_id,),
        ).fetchall()
    }
    ledger = {
        "settled_tokens": sum(settled.values()),
        "usage_of_settled_subjects": sum(usage_by_subject.get(s, 0) for s in settled),
        "unsettled_usage_tokens": sum(t for s, t in usage_by_subject.items() if s not in settled),
        "mismatched_subjects": sorted(
            s for s, t in settled.items() if usage_by_subject.get(s, 0) != t
        ),
    }
    reconciled = (
        unclassified["rows"] == 0
        and path_bucket["tokens"] + exploration_bucket["tokens"] + service_total == total["tokens"]
        and not ledger["mismatched_subjects"]
    )

    # -- actions and people on the way (step 7)
    actions = []
    if store.has_table("actions"):
        for action in store.list_actions(mission_id, "SUCCEEDED"):
            request_id = action.get("approval_request_id")
            approvers = (
                []
                if not request_id
                else [
                    d["principal_id"]
                    for d in store.list_decisions(str(request_id))
                    if d["decision"] == "grant"
                ]
            )
            actions.append(
                {
                    "action_key": action["action_key"],
                    "target": action.get("target"),
                    "receipt_hash": (action.get("receipt") or {}).get("receipt_hash"),
                    "decision_receipts": list(action.get("decision_receipts") or []),
                    "approved_by": approvers,
                }
            )
    human = {
        "wait_seconds": round(store.human_wait_seconds(mission_id, store.now), 3)
        if store.has_table("approvals")
        else 0.0,
        "decisions": sum(
            len(store.list_decisions(r["request_id"])) for r in store.list_approvals(mission_id)
        )
        if store.has_table("approvals")
        else 0,
        "overrides": len(store.list_overrides(mission_id))
        if store.has_table("human_overrides")
        else 0,
    }
    return {
        "version": ATTRIBUTION_VERSION,
        "mission_id": mission_id,
        "mission_status": str(mission.status),
        "success_path": success,
        "final_products": products,
        "path_tasks": path_task_view,
        "knowledge_path": {
            "knowledge": [k.get("id") for k in knowledge.get("knowledge", [])],
            "refuted_claims": [c.get("id") for c in knowledge.get("claims", [])],
            "refuted_on_path": sorted(refuted & path_attempts),
            "edges": knowledge.get("edges", []),
        },
        "attempts": attempt_view,
        "actions": actions,
        "action_reservations": action_reservations,
        "human": human,
        "cost": {
            "success_path": _money(path_bucket),
            "exploration": _money(exploration_bucket),
            "services": {role: _money(b) for role, b in sorted(services.items())},
            "unclassified": {**_money(unclassified), "subjects": unclassified_subjects},
            "total": _money(total),
            "unknown_usage_rows": unknown_rows,
            "ledger": ledger,
            "reconciled": reconciled,
        },
        "breaks": [b for n, b in enumerate(breaks) if b not in breaks[:n]],  # review P2-6
    }


__all__ = ("ATTRIBUTION_VERSION", "attribution")
