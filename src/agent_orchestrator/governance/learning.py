# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Rule improvement from history (original §28 stage four "收集 Trace → 离线训练或规则
改进 → 生成新策略版本"; theory 13 §18 Reputation, §19 "先可以更新规则，进一步再训练";
ORCH-BUILD §11.2 / §11.3; plan D9-5' / D9-6').

The learner reads finished Missions from history libraries — each copied once and
opened read-only — and proposes a policy candidate only when the history is trustworthy,
of one kind and large enough:

* every training Mission must replay completely (coverage 100 %, no mismatch, no gap)
  and its attribution must reconcile; an untrustworthy record refuses the whole
  proposal and is named;
* fixtures and real runs are never mixed; Missions of unknown provenance (older
  libraries) are left out and listed;
* too few Missions, or no applicable rule with enough samples, is "insufficient" —
  nothing is registered and no improvement is claimed.

Rules (``rules-v1``, heuristics, not a trained model — the report says so):

* **R1 allocator weights** — only contested allocations (more eligible Tasks than free
  slots) are compared; per §29.3 part (``uncertainty`` excluded: it moves with retries),
  a part systematically higher on exploration Attempts than on success-path Attempts
  lowers its weight by one step, the reverse raises it; at most two parts per proposal;
  both groups need ``min_group`` distinct Missions.
* **R2 model routing** — when first attempts of a task kind on the deployment's default
  profile fail with a Wilson lower bound ≥ 0.5 over at least ``min_group`` Missions, the
  kind is routed to the default profile's escalation target (only profiles the
  deployment has; not applicable when a ``by_role`` worker rule would override it).

Reputation (role × prompt version × profile) is reported as evidence, never as a reason
to skip verification (ORCH-BUILD §11.2)."""

from __future__ import annotations

import statistics
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..observability.evaluation import library_digest, wilson
from ..observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
    library_copy,
)
from ..observability.traces import attribution
from ..storage.store import Store
from .promotion import (
    WEIGHT_RANGES,
    code_versions,
    interpreter_versions,
    overlay,
    params_hash,
    task_identity_hash,
)

LEARNER_VERSION = "rules-v1"
STEP = 0.05
R1_PARTS = tuple(part for part in WEIGHT_RANGES if part != "uncertainty")
FAILED_REASONS = frozenset(
    {"verification_failed", "outcome_failure", "outcome_no_progress", "envelope_invalid"}
)
TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
FIXTURE_NOTE = "规则改进（启发式规则，非训练模型）；fixture 历史只证明机制，不代表真实质量"
REAL_NOTE = "规则改进（启发式规则，非训练模型）；结论须经离线评测与人工批准，不据此宣称提升"


@dataclass(frozen=True)
class LearningResult:
    outcome: str  # candidate | no_change | insufficient | refused
    params: dict[str, Any] | None
    manifest: dict[str, Any]
    reasons: list[str] = field(default_factory=list)
    note: str = REAL_NOTE


def task_identity(mission: Any) -> str:
    """The task a Mission asked for (plan D9-6'): the same fields a MissionSpec gives."""

    report = dict(mission.final_report or {})
    return task_identity_hash(
        {
            "goal": mission.goal,
            "success_criteria": list(mission.success_criteria),
            "allowed_tools": list(mission.allowed_tools),
            "task_kind": str(report.get("task_kind") or "code"),
            "workspace_seed": dict(report.get("workspace_seed") or {}),
        }
    )


# ------------------------------------------------------------------ reading history
def _read_library(path: Path) -> dict[str, Any]:
    library = path / "orchestrator.db" if path.is_dir() else path
    digest = library_digest(library)
    out: dict[str, Any] = {
        "training": [],
        "excluded": [],
        "untrusted": [],
        "r1": [],
        "r2": [],
        "reputation": [],
    }
    with tempfile.TemporaryDirectory(prefix="orch-learn-") as scratch:
        store = Store.open_readonly(library_copy(library, Path(scratch)))
        try:
            for mission in store.list_missions():
                status = str(mission.status)
                if status not in TERMINAL:
                    out["excluded"].append({"mission_id": mission.id, "reason": "not terminal"})
                    continue
                binding = store.get_mission_policy(mission.id)
                kind = "unknown" if binding is None else str(binding.get("provider_kind"))
                if kind == "unknown":
                    out["excluded"].append(
                        {
                            "mission_id": mission.id,
                            "reason": "来源不明（旧版本库或未记录 provider 种类）",
                        }
                    )
                    continue
                events = events_from_store(store, mission.id)
                projection = Projection().feed(events)
                projection.check_structure()
                comparison = compare(
                    projection.formal(), formal_from_snapshot(store.snapshot(mission.id))
                )
                if projection.gaps or not comparison["consistent"] or comparison["coverage"] < 1.0:
                    out["untrusted"].append(
                        f"{mission.id}: replay coverage {comparison['coverage']}, "
                        f"{len(comparison['mismatches'])} mismatches, gaps "
                        f"{sorted({g['rule'] for g in projection.gaps})}"
                    )
                    continue
                traced = attribution(store, mission.id)
                if not traced.get("cost", {}).get("reconciled"):
                    out["untrusted"].append(f"{mission.id}: attribution does not reconcile")
                    continue
                report = dict(mission.final_report or {})
                task_kind = str(report.get("task_kind") or "code")
                out["training"].append(
                    {
                        "mission_id": mission.id,
                        "library": str(library),
                        "library_sha256": digest,
                        "created_at": mission.created_at,
                        "status": status,
                        "provider_kind": kind,
                        "policy_version_id": None if binding is None else binding.get("version_id"),
                        "task_identity": task_identity(mission),
                    }
                )
                on_path = {a["attempt_id"]: bool(a["on_success_path"]) for a in traced["attempts"]}
                for event in events:
                    if event.get("type") != "AllocationDecided":
                        continue
                    payload = dict(event.get("payload") or {})
                    eligible, slots = payload.get("eligible"), payload.get("slots")
                    out["r1"].append(
                        {
                            "mission_id": mission.id,
                            "parts": dict(payload.get("parts") or {}),
                            "on_path": on_path.get(str(event.get("attempt_id")), False),
                            "contested": eligible is not None
                            and slots is not None
                            and int(eligible) > int(slots),
                        }
                    )
                usage = dict(
                    store.connection.execute(
                        "SELECT subject_id, SUM(input_tokens + output_tokens) FROM imported_usage"
                        " WHERE mission_id = ? GROUP BY subject_id",
                        (mission.id,),
                    ).fetchall()
                )
                for task in store.list_tasks(mission.id):
                    for attempt in store.list_attempts(task.id):
                        reason = str((attempt.failure or {}).get("reason") or "")
                        if attempt.ordinal == 1:
                            out["r2"].append(
                                {
                                    "mission_id": mission.id,
                                    "task_kind": task_kind,
                                    "profile": attempt.runtime_profile_id,
                                    "failed": reason in FAILED_REASONS,
                                }
                            )
                        out["reputation"].append(
                            {
                                "role": attempt.role,
                                "prompt_version": attempt.prompt_version,
                                "profile": attempt.runtime_profile_id,
                                "completed": str(attempt.status) == "COMPLETED",
                                "verification_failed": reason == "verification_failed",
                                "tokens": int(usage.get(attempt.id) or 0),
                            }
                        )
        finally:
            store.close()
    return out


# ------------------------------------------------------------------ the rules
def rule_weights(
    rows: Sequence[Mapping[str, Any]],
    base_weights: Mapping[str, float],
    *,
    min_group: int,
    margin: float,
) -> tuple[dict[str, float], dict[str, Any]]:
    """R1 (plan D9-5'): contested allocations only; at most two parts, one step each."""

    contested = [r for r in rows if r.get("contested")]
    path_missions = {r["mission_id"] for r in contested if r["on_path"]}
    exploration_missions = {r["mission_id"] for r in contested if not r["on_path"]}
    stats: dict[str, Any] = {
        "fired": False,
        "contested_allocations": len(contested),
        "missions_on_path": len(path_missions),
        "missions_exploring": len(exploration_missions),
        "sufficient": len(path_missions) >= min_group and len(exploration_missions) >= min_group,
        "parts": {},
    }
    if not stats["sufficient"]:
        return {}, stats
    gaps: dict[str, float] = {}
    for part in R1_PARTS:
        on_path = [
            float(r["parts"][part]) for r in contested if r["on_path"] and part in r["parts"]
        ]
        exploring = [
            float(r["parts"][part]) for r in contested if not r["on_path"] and part in r["parts"]
        ]
        if not on_path or not exploring:
            continue
        gap = statistics.mean(exploring) - statistics.mean(on_path)
        stats["parts"][part] = {
            "path_mean": round(statistics.mean(on_path), 4),
            "exploration_mean": round(statistics.mean(exploring), 4),
            "gap": round(gap, 4),
        }
        if abs(gap) > margin:
            gaps[part] = gap
    changes: dict[str, float] = {}
    for part in sorted(gaps, key=lambda p: -abs(gaps[p]))[:2]:
        low, high = WEIGHT_RANGES[part]
        direction = -1.0 if gaps[part] > 0 else 1.0  # higher on exploration → weigh it less
        moved = round(min(high, max(low, float(base_weights[part]) + direction * STEP)), 6)
        if moved != float(base_weights[part]):
            changes[part] = moved
    stats["fired"] = bool(changes)
    stats["changes"] = dict(changes)
    return changes, stats


def rule_routing(
    rows: Sequence[Mapping[str, Any]],
    routing: Any,
    base_routing: Mapping[str, Any],
    *,
    min_group: int,
) -> tuple[dict[str, str], dict[str, Any]]:
    """R2 (plan D9-5'): route a task kind whose first attempts keep failing on the
    default profile to that profile's escalation target."""

    stats: dict[str, Any] = {"fired": False, "applicable": True, "sufficient": False, "groups": {}}
    default = None if routing is None else routing.default
    target = None if routing is None else dict(routing.escalate).get(default)
    if routing is None or target is None:
        stats.update(applicable=False, reason="部署路由没有从默认档位出发的升级目标")
        return {}, stats
    if "worker" in dict(routing.by_role):
        stats.update(applicable=False, reason="部署对 worker 设了 by_role，by_task_kind 不起作用")
        return {}, stats
    stats["default"], stats["target"] = default, target
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["profile"] == default:
            groups[str(row["task_kind"])].append(row)
    changes: dict[str, str] = {}
    current = dict(base_routing.get("by_task_kind") or {})
    for kind, items in sorted(groups.items()):
        missions = len({r["mission_id"] for r in items})
        failures = sum(1 for r in items if r["failed"])
        interval = wilson(failures, len(items)) or [0.0, 0.0]
        stats["groups"][kind] = {
            "missions": missions,
            "first_attempts": len(items),
            "failures": failures,
            "wilson_low": interval[0],
        }
        if missions >= min_group:
            stats["sufficient"] = True
            if interval[0] >= 0.5 and current.get(kind) != target:
                changes[kind] = str(target)
    stats["fired"] = bool(changes)
    return changes, stats


def _reputation(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["role"]), str(row["prompt_version"]), str(row["profile"]))].append(row)
    out = []
    for (role, prompt, profile), items in sorted(groups.items()):
        completed = sum(1 for r in items if r["completed"])
        interval = wilson(completed, len(items))  # at least one Attempt per group
        out.append(
            {
                "role": role,
                "prompt_version": prompt,
                "profile": profile,
                "attempts": len(items),
                "success_rate": round(completed / len(items), 4),
                "wilson95": interval,
                "verification_failed_rate": round(
                    sum(1 for r in items if r["verification_failed"]) / len(items), 4
                ),
                "mean_tokens": round(statistics.mean(r["tokens"] for r in items), 1),
            }
        )
    return out


# ------------------------------------------------------------------ the learner
def learn(
    histories: Sequence[Path],
    *,
    base_params: Mapping[str, Any],
    routing: Any = None,
    min_missions: int = 6,
    min_group: int = 5,
    margin: float = 0.1,
) -> LearningResult:
    read = [_read_library(Path(p)) for p in histories]
    training = sorted(
        (t for r in read for t in r["training"]), key=lambda t: (t["created_at"], t["mission_id"])
    )
    excluded = [e for r in read for e in r["excluded"]]
    untrusted = [u for r in read for u in r["untrusted"]]
    kinds = sorted({t["provider_kind"] for t in training})
    manifest: dict[str, Any] = {
        "kind": "rule_improvement",
        "learner": LEARNER_VERSION,
        "training": training,
        "window": {
            "start": training[0]["created_at"] if training else None,
            "end": training[-1]["created_at"] if training else None,
        },
        "excluded": excluded,
        "provider_kinds": kinds,
        "code_versions": code_versions(),
        "interpreter_versions": interpreter_versions(),
        "base_params_hash": params_hash(base_params),
        "settings": {
            "min_missions": min_missions,
            "min_group": min_group,
            "margin": margin,
            "step": STEP,
        },
        "rules": {},
        "reputation": _reputation([row for r in read for row in r["reputation"]]),
    }
    note = FIXTURE_NOTE if kinds == ["fixtures"] else REAL_NOTE

    def result(
        outcome: str, reasons: list[str], params: dict[str, Any] | None = None
    ) -> LearningResult:
        return LearningResult(outcome, params, manifest, reasons, note)

    if untrusted:
        return result("refused", ["训练记录不可信（replay / 对账），整份提议拒绝："] + untrusted)
    if len(kinds) > 1:
        return result("refused", [f"训练数据混有 {kinds}：fixtures 与 real 不能混用"])
    if len(training) < min_missions:
        return result(
            "insufficient",
            [f"训练 Mission 数 {len(training)} < {min_missions}：缺少足够样本，不提出候选"],
        )
    weights, r1 = rule_weights(
        [row for r in read for row in r["r1"]],
        base_params["allocator_weights"],
        min_group=min_group,
        margin=margin,
    )
    routes, r2 = rule_routing(
        [row for r in read for row in r["r2"]], routing, base_params["routing"], min_group=min_group
    )
    manifest["rules"] = {"R1": r1, "R2": r2}
    if not weights and not routes:
        applicable = [s for s in (r1, r2) if s.get("applicable", True)]
        if not any(s["sufficient"] for s in applicable):
            return result(
                "insufficient", ["适用规则的分组样本都不足 min_group：缺少足够样本，不提出候选"]
            )
        return result("no_change", ["样本足够，但没有规则触发：不提出候选"])
    partial: dict[str, Any] = {}
    if weights:
        partial["allocator_weights"] = weights
    if routes:
        partial["routing"] = {
            "by_task_kind": {**dict(base_params["routing"]["by_task_kind"]), **routes}
        }
    return result("candidate", [], overlay(base_params, partial))


def learn_and_register(
    commit: Any,
    histories: Sequence[Path],
    *,
    base_params: Mapping[str, Any],
    routing: Any = None,
    **settings: Any,
) -> dict[str, Any]:
    """Learn, then register the candidate as a proposal — or record the refusal and
    register nothing (plan D9-6')."""

    outcome = learn(histories, base_params=base_params, routing=routing, **settings)
    if outcome.outcome == "candidate" and outcome.params is not None:
        proposal = commit.propose_policy(
            outcome.params, manifest=outcome.manifest, source="learner"
        )
        return {"result": outcome, "proposal": proposal, "refusal": None}
    summary = {
        "kind": outcome.manifest["kind"],
        "learner": outcome.manifest["learner"],
        "outcome": outcome.outcome,
        "training_missions": len(outcome.manifest["training"]),
        "window": outcome.manifest["window"],
        "code_versions": outcome.manifest["code_versions"],
    }
    refusal = commit.refuse_policy_proposal(manifest=summary, reasons=list(outcome.reasons))
    return {"result": outcome, "proposal": None, "refusal": refusal}


__all__ = (
    "LEARNER_VERSION",
    "LearningResult",
    "learn",
    "learn_and_register",
    "rule_routing",
    "rule_weights",
    "task_identity",
)
