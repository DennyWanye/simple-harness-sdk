# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Evaluation (original §23.4 Offline Evaluation / A/B / Ablation; theory 12 §6, §8, §10,
§11, §15; ORCH §10.2 ``observability/evaluation.py``; plan D8-6' / D8-7' / D8-8').

A fixed set of cases runs under several strategies, several independent trials each,
inside the same budget bounds; every run is a *new* Mission in its own new directory and
library (``eval:<plan>:<strategy>:<case>:<trial>``), so an evaluation never touches an
old Mission's facts.  The report gives, per strategy, the success rate with its sample
size and Wilson interval, time and token distributions, money (or "unpriced"), the
verification pass rate, knowledge reuse, repetition, pruning, recovery, failure reasons
and samples, and — when a case carries a hidden oracle — how often verification passed
what the oracle rejects.  Two strategies are said to differ only on evidence (Fisher's
exact test, non-overlapping ranges); fixtures are a mechanism check, never a quality
claim.

Strategies may only change what the whitelist allows; safety boundaries, budgets and the
deployment policy stay the plan's, and a run may only reach the local test service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import statistics
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import Budget
from ..contracts.models import sha256_hex
from ..governance.policies import DeploymentPolicy, snapshot_diff
from ..orchestrator.action_commits import parse_action_criterion
from ..orchestrator.commit_service import MissionSpec
from ..runtime.assembly import OrchestratorConfig
from ..runtime.connectors import TestConfigService
from ..storage.store import Store
from .evidence import write_evidence
from .metrics import metrics
from .replay import library_copy
from .secrets import redact_text
from .traces import attribution

EVALUATION_VERSION = "evaluation-v1"
EVALUATION_TENANT = "evaluation"
# plan D8-7': what a strategy may change — nothing else
STRATEGY_OVERRIDES = frozenset(
    {
        "ablations",
        "model",
        "candidates_per_task",
        "manager_after_failures",
        "no_progress_limit",
        "max_manager_rounds",
        "max_concurrency",
    }
)
# what the plan (the operator, the same for every strategy) may set
PLAN_CONFIG = frozenset(
    {
        "model",  # the provider's model name, the same for every strategy
        "test_timeout_seconds",
        "max_concurrency",
        "max_concurrent_model_calls",
        "lease_seconds",
        "stall_seconds",
        "turn_deadline_seconds",
        "default_max_output_tokens",
        "max_output_tokens_ceiling",
        "planner_reserve_tokens",
        "critic_reserve_tokens",
        "attempt_reserve_tokens",
        "manager_reserve_tokens",
        "global_budget",
        "price_table",
        "hard_cap_micros",
    }
)
FIXTURE_NOTE = "机制验证（fixture），不代表质量"


# ------------------------------------------------------------------ the plan
@dataclass(frozen=True)
class Oracle:
    """Hidden checks the evaluator runs on the final integrated tree; Agents never see
    them (plan D8-6': verification misjudgment = PASS the oracle rejects)."""

    files: Mapping[str, str]
    target: str


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    spec: Callable[[str, str], MissionSpec]  # (tenant_id, idempotency_key) -> the charter
    provider: Callable[[], Any]  # a fresh provider for every trial
    kind: str = "fixtures"  # fixtures | env
    connectors: Callable[[Path], Mapping[str, Any]] | None = None  # fresh test services per run
    oracle: Oracle | None = None
    derived_from: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Strategy:
    name: str
    overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationPlan:
    name: str
    cases: tuple[EvaluationCase, ...]
    strategies: tuple[Strategy, ...]
    trials: int = 1
    config: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 300.0
    min_samples: int = 3

    def validate(self) -> None:
        if self.trials < 1 or not self.cases or not self.strategies:
            raise ValueError("an evaluation needs cases, strategies and at least one trial")
        for group, names in (
            ("case", [c.name for c in self.cases]),
            ("strategy", [s.name for s in self.strategies]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"{group} names must be unique: {names}")
        refused_plan = sorted(set(self.config) - PLAN_CONFIG)
        if refused_plan:
            raise ValueError(f"the plan may not set {refused_plan} (plan D8-7')")
        for strategy in self.strategies:
            refused = sorted(set(strategy.overrides) - STRATEGY_OVERRIDES)
            if refused:
                raise ValueError(
                    f"strategy {strategy.name!r} may not change {refused}: budgets, the "
                    "deployment policy and safety boundaries are the plan's (plan D8-7')"
                )
        probe = "probe"
        for case in self.cases:
            spec = case.spec(EVALUATION_TENANT, probe)
            free_text = [
                c
                for c in spec.success_criteria
                if not c.startswith(("pytest:", "file:", "action:"))
            ]
            for strategy in self.strategies:
                if "critic" in tuple(strategy.overrides.get("ablations", ())) and free_text:
                    raise ValueError(
                        f"case {case.name!r} has free-text criteria {free_text}: without a "
                        "judge their outcome is decided in advance (plan D8-7'); use pytest: / file:"
                    )
            if any(parse_action_criterion(c) for c in spec.success_criteria):
                if case.connectors is None:
                    raise ValueError(f"case {case.name!r} names actions but brings no test service")
                with tempfile.TemporaryDirectory() as scratch:
                    services = case.connectors(Path(scratch))
                    wrong = sorted(
                        n for n, s in services.items() if not isinstance(s, TestConfigService)
                    )
                if wrong:
                    raise ValueError(
                        f"an evaluation may only reach the local test service, not {wrong}"
                    )


# ------------------------------------------------------------------ statistics (plan §6.1)
def wilson(successes: int, samples: int, z: float = 1.96) -> list[float] | None:
    if samples <= 0:
        return None
    p = successes / samples
    denominator = 1 + z * z / samples
    centre = (p + z * z / (2 * samples)) / denominator
    half = z * math.sqrt(p * (1 - p) / samples + z * z / (4 * samples * samples)) / denominator
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided p of the 2×2 table [[a, b], [c, d]] (successes / failures of two groups)."""

    row, col, total = a + b, a + c, a + b + c + d
    if total == 0:
        return 1.0

    def probability(x: int) -> float:
        return math.comb(col, x) * math.comb(total - col, row - x) / math.comb(total, row)

    observed = probability(a)
    low, high = max(0, row - (total - col)), min(row, col)
    return min(
        1.0, sum(p for x in range(low, high + 1) if (p := probability(x)) <= observed * (1 + 1e-9))
    )


def _spread(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "median": None, "max": None}
    return {
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


def _overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if a["min"] is None or b["min"] is None:
        return True
    return not (a["max"] < b["min"] or b["max"] < a["min"])


# ------------------------------------------------------------------ one run
def _spec_hash_of(spec: MissionSpec) -> str:
    return sha256_hex(spec.to_json())


async def _oracle_check(
    store: Store, mission_id: str, oracle: Oracle, timeout: float
) -> dict[str, Any]:
    from ..artifacts.versioning import merge_accepted
    from ..runtime.tool_gateway import run_pytest

    mission = store.get_mission(mission_id)
    tasks = [t for t in store.list_tasks(mission_id) if str(t.status) == "COMPLETED"]
    by_id = {t.id: t for t in store.list_tasks(mission_id)}
    artifacts = {
        t.id: [a for aid in t.accepted_artifacts if (a := store.get_artifact(aid))] for t in tasks
    }
    with tempfile.TemporaryDirectory(prefix="orch-oracle-") as scratch:
        root = Path(scratch)
        for path, content in dict(
            (mission.final_report or {}).get("workspace_seed", {}) if mission else {}
        ).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(str(content), encoding="utf-8")
        for item in merge_accepted(tasks, artifacts, tasks_by_id=by_id):
            artifact = store.get_artifact(item.artifact_id)
            if artifact is not None and Path(artifact.storage_uri).is_file():
                (root / item.path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact.storage_uri, root / item.path)
        for path, content in oracle.files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(content, encoding="utf-8")
        run = await run_pytest(str(root), path=oracle.target, timeout=timeout)
    return {"passed": run.passed, "tail": run.stdout[-400:]}


def _record(store: Store, mission_id: str) -> dict[str, Any]:
    """Per-run facts, read from the run's own library (plan §6.1 definitions)."""

    snapshot = store.snapshot(mission_id)
    mission = snapshot["mission"]
    measured = metrics(store, mission_id, unpriced=True)
    cost = attribution(store, mission_id)["cost"]["total"]
    tasks, attempts = snapshot["tasks"], snapshot["attempts"]
    hashes = [a["content_hash"] for a in snapshot["artifacts"]]
    pruned_tasks = [
        t
        for t in tasks
        if t["status"] == "CANCELLED"
        and t.get("failure_reason") not in {"mission_stopped", "not_needed_paused"}
    ]
    claims = snapshot["claims"]
    refuted = [c for c in claims if c.get("status") == "DISPUTED" and c.get("resolved_by")]
    recovered = [
        t
        for t in tasks
        if t["status"] == "COMPLETED"
        and any(a["task_id"] == t["id"] and a["status"] == "RETRY_WAIT" for a in attempts)
    ]
    failure = None
    events = store.iter_events(mission_id)
    failed = [e for e in events if e.type == "VerificationFailed"]
    if str(mission["status"]) != "COMPLETED":
        last = dict(failed[-1].payload) if failed else {}
        first = (last.get("failures") or [{}])[0]
        failure = {
            "stop_reason": mission.get("stop_reason"),
            "failing_layer": first.get("layer"),
            "summary": str(first.get("summary") or "")[:300],
        }
    ablated = any(
        (v.get("detail") or {}).get("ablated")
        for r in snapshot["results"]
        for v in r.get("verifications", [])
    )
    return {
        "mission_status": mission["status"],
        "stop_reason": mission.get("stop_reason"),
        "duration_seconds": measured["mission"]["duration_seconds"],
        "tokens": cost["tokens"],
        "cost_micros": cost["cost_micros"],
        "cost_note": cost["cost_note"],
        "attempts": len(attempts),
        "verification_pass_rate": measured["verification"]["pass_rate"],
        "knowledge_reuse": measured["knowledge"]["reuse_events"],
        "repeat_rate": None
        if not hashes
        else round((len(hashes) - len(set(hashes))) / len(hashes), 4),
        "task_pruning_rate": None if not tasks else round(len(pruned_tasks) / len(tasks), 4),
        "candidate_pruning_rate": None
        if not attempts
        else round(sum(1 for a in attempts if a["status"] == "SUPERSEDED") / len(attempts), 4),
        "pollution_rate": None if not claims else round(len(refuted) / len(claims), 4),
        "recoveries": len(recovered),
        "new_ideas": None,  # plan §6.1: no reliable record yet
        "ablated_policy_pass": bool(ablated and str(mission["status"]) == "COMPLETED"),
        "failure": failure,
    }


async def _run_once(
    plan: EvaluationPlan, case: EvaluationCase, strategy: Strategy, trial: int, run_dir: Path
) -> dict[str, Any]:
    from ..orchestrator.event_handler import Orchestrator

    key = f"eval:{plan.name}:{strategy.name}:{case.name}:{trial}"
    spec = case.spec(EVALUATION_TENANT, key)
    services = dict(case.connectors(run_dir)) if case.connectors is not None else {}
    config = OrchestratorConfig(
        evidence_root=run_dir,
        deployment_policy=DeploymentPolicy(enabled_connectors=tuple(sorted(services))),
        **{"max_concurrency": 1, **dict(plan.config), **dict(strategy.overrides)},
    )
    base: dict[str, Any] = {
        "strategy": strategy.name,
        "case": case.name,
        "trial": trial,
        "kind": case.kind,
        "idempotency_key": key,
        "run_dir": str(run_dir),
        "budget": spec.budget.to_json(),
        "derived_from": None if case.derived_from is None else dict(case.derived_from),
    }
    started = time.monotonic()
    async with Orchestrator(config, case.provider(), connectors=services) as orchestrator:
        start_snapshot = orchestrator.policy_snapshot()
        mission = await orchestrator.submit_mission(spec)
        await orchestrator.run()
        store = orchestrator.store
        final = store.get_mission(mission.id)
        assert final is not None
        waiting = store.waiting_on(mission.id)
        status = str(final.status)
        category = (
            "success"
            if status == "COMPLETED"
            else "waiting_for_human"
            if status == "ACTIVE" and waiting
            else "failure"
            if status in {"FAILED", "CANCELLED"}
            else "harness_error"
        )
        record = {
            **base,
            "mission_id": mission.id,
            "category": category,
            "wall_seconds": round(time.monotonic() - started, 3),
            **_record(store, mission.id),
            "snapshot_hash": start_snapshot["hash"],
            "oracle": None,
        }
        if case.oracle is not None and category == "success":
            record["oracle"] = await _oracle_check(
                store, mission.id, case.oracle, config.test_timeout_seconds
            )
        write_evidence(
            directory=run_dir,
            store=store,
            commit=orchestrator.commit,
            mission_id=mission.id,
            baseline={
                "evaluation": base,
                "spec": spec.to_json(),
                "policy_snapshot": start_snapshot,
                "config": config.to_json(),
            },
            workspaces_root=config.workspaces_root,
            test_report=record,
            policy_snapshot=orchestrator.policy_snapshot(),
        )
        record["snapshot"] = start_snapshot
        return record


# ------------------------------------------------------------------ the whole plan
def _summary(records: Sequence[Mapping[str, Any]], min_samples: int) -> dict[str, Any]:
    counted = [r for r in records if r["category"] != "harness_error"]
    successes = sum(1 for r in counted if r["category"] == "success")
    oracle_checked = [r for r in counted if r.get("oracle") is not None]
    misjudged = sum(1 for r in oracle_checked if not r["oracle"]["passed"])
    priced = [r["cost_micros"] for r in counted if r["cost_micros"] is not None]
    rates = [
        r["verification_pass_rate"] for r in counted if r["verification_pass_rate"] is not None
    ]
    repeat = [r["repeat_rate"] for r in counted if r["repeat_rate"] is not None]
    return {
        "samples": len(counted),
        "harness_errors": len(records) - len(counted),
        "successes": successes,
        "waiting_for_human": sum(1 for r in counted if r["category"] == "waiting_for_human"),
        "success_rate": None if not counted else round(successes / len(counted), 4),
        "wilson95": wilson(successes, len(counted)),
        "enough_samples": len(counted) >= min_samples,
        "duration_seconds": _spread(
            [r["duration_seconds"] for r in counted if r["duration_seconds"] is not None]
        ),
        "tokens": _spread([r["tokens"] for r in counted]),
        "cost_micros": sum(priced) if priced and len(priced) == len(counted) else None,
        "cost_note": None
        if priced and len(priced) == len(counted)
        else "unpriced: money not recorded (null, never zero)",
        "verification_pass_rate": None if not rates else round(statistics.mean(rates), 4),
        "knowledge_reuse": sum(r["knowledge_reuse"] for r in counted),
        "repeat_rate": None if not repeat else round(statistics.mean(repeat), 4),
        "recoveries": sum(r["recoveries"] for r in counted),
        "ablated_policy_passes": sum(1 for r in counted if r["ablated_policy_pass"]),
        "verification_misjudgment": {
            "checked": len(oracle_checked),
            "misjudged": misjudged,
            "rate": None if not oracle_checked else round(misjudged / len(oracle_checked), 4),
        },
        "failure_reasons": dict(
            Counter(str(r["stop_reason"]) for r in counted if r["category"] == "failure")
        ),
        "failure_samples": [
            {k: r[k] for k in ("case", "trial", "mission_id", "failure", "run_dir")}
            for r in counted
            if r["category"] == "failure"
        ][:3],
        "snapshot_hashes": sorted({r["snapshot_hash"] for r in records if r.get("snapshot_hash")}),
    }


def _compare(
    plan: EvaluationPlan, left: str, right: str, a: Mapping[str, Any], b: Mapping[str, Any]
) -> dict[str, Any]:
    fixture = all(case.kind == "fixtures" for case in plan.cases)
    p = fisher_exact(
        a["successes"], a["samples"] - a["successes"], b["successes"], b["samples"] - b["successes"]
    )
    if fixture:
        verdict = f"不适用（fixture）：{FIXTURE_NOTE}"
    elif not (a["enough_samples"] and b["enough_samples"]):
        verdict = f"证据不足：样本量 {a['samples']} / {b['samples']} 低于 {plan.min_samples}"
    elif p < 0.05:
        verdict = "成功率有差异（Fisher 精确检验 p < 0.05）"
    else:
        verdict = "证据不足：成功率差异在样本波动内"
    return {
        "a": left,
        "b": right,
        "success": {
            "a": [a["successes"], a["samples"]],
            "b": [b["successes"], b["samples"]],
            "fisher_p": round(p, 4),
        },
        "verdict": verdict,
        "duration": "有差异（区间不重叠）"
        if not _overlap(a["duration_seconds"], b["duration_seconds"])
        else "证据不足（区间重叠）",
        "tokens": "有差异（区间不重叠）"
        if not _overlap(a["tokens"], b["tokens"])
        else "证据不足（区间重叠）",
    }


async def run_plan_async(plan: EvaluationPlan, directory: Path) -> dict[str, Any]:
    plan.validate()
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(f"{directory} is not empty: an evaluation always runs in a new directory")
    directory.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for strategy in plan.strategies:
        for case in plan.cases:
            for trial in range(1, plan.trials + 1):
                run_dir = directory / "runs" / strategy.name / case.name / f"trial-{trial}"
                run_dir.mkdir(parents=True, exist_ok=False)
                try:
                    record = await asyncio.wait_for(
                        _run_once(plan, case, strategy, trial, run_dir),
                        timeout=plan.timeout_seconds,
                    )
                except TimeoutError:
                    record = {
                        "strategy": strategy.name,
                        "case": case.name,
                        "trial": trial,
                        "category": "harness_error",
                        "reason": f"timeout after {plan.timeout_seconds} s",
                        "run_dir": str(run_dir),
                    }
                except Exception as error:  # noqa: BLE001 - a broken harness is reported, never counted
                    record = {
                        "strategy": strategy.name,
                        "case": case.name,
                        "trial": trial,
                        "category": "harness_error",
                        "reason": f"{type(error).__name__}: {error}",
                        "run_dir": str(run_dir),
                    }
                records.append(record)
    snapshots = {r["strategy"]: r["snapshot"] for r in records if r.get("snapshot")}
    for record in records:
        record.pop("snapshot", None)
    summaries = {
        s.name: _summary([r for r in records if r["strategy"] == s.name], plan.min_samples)
        for s in plan.strategies
    }
    names = [s.name for s in plan.strategies]
    comparisons = [
        {
            **_compare(plan, names[0], other, summaries[names[0]], summaries[other]),
            "policy_differences": snapshot_diff(snapshots[names[0]], snapshots[other])
            if names[0] in snapshots and other in snapshots
            else None,
        }
        for other in names[1:]
    ]
    report = {
        "version": EVALUATION_VERSION,
        "plan": plan.name,
        "kind": sorted({case.kind for case in plan.cases}),
        "note": FIXTURE_NOTE
        if all(c.kind == "fixtures" for c in plan.cases)
        else "真实模型试验（与确定性回归分开报告）",
        "cases": [
            {
                "name": c.name,
                "kind": c.kind,
                "oracle": c.oracle is not None,
                "derived_from": c.derived_from,
            }
            for c in plan.cases
        ],
        "strategies": [{"name": s.name, "overrides": dict(s.overrides)} for s in plan.strategies],
        "trials": plan.trials,
        "config": {
            k: (v.to_json() if hasattr(v, "to_json") else v) for k, v in plan.config.items()
        },
        "runs": records,
        "summary": summaries,
        "comparisons": comparisons,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    text, _found = redact_text(text)
    (directory / "evaluation.json").write_text(text, encoding="utf-8")
    markdown, _found = redact_text(render_markdown(report))
    (directory / "evaluation.md").write_text(markdown, encoding="utf-8")
    return report


def run_plan(plan: EvaluationPlan, directory: Path) -> dict[str, Any]:
    return asyncio.run(run_plan_async(plan, directory))


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# 评测报告：{report['plan']}",
        "",
        f"- 性质：{report['note']}",
        f"- 策略：{', '.join(s['name'] for s in report['strategies'])}；每组试验 {report['trials']} 次；运行共 {len(report['runs'])} 次",
        "- 口径：成功率带样本量与 Wilson 95% 区间；比较用 Fisher 精确检验，耗时 / tokens 区间不重叠才写差异；未定价时金额为 null",
        "",
        "## 各策略",
        "",
        "| 策略 | 样本 | 成功 | 成功率（Wilson 95%） | 等待人工 | 脚手架错误 | 耗时 s（min / 中位 / max） | tokens（min / 中位 / max） | 金额 | 验证通过率 | 知识复用 | 重复率 | 恢复 | 验证误判 | 消融政策下的 PASS |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, s in report["summary"].items():
        d, t, m = s["duration_seconds"], s["tokens"], s["verification_misjudgment"]
        lines.append(
            f"| {name} | {s['samples']} | {s['successes']} | {s['success_rate']}（{s['wilson95']}） | {s['waiting_for_human']} | {s['harness_errors']} "
            f"| {d['min']} / {d['median']} / {d['max']} | {t['min']} / {t['median']} / {t['max']} | {s['cost_micros'] if s['cost_micros'] is not None else '未定价'} "
            f"| {s['verification_pass_rate']} | {s['knowledge_reuse']} | {s['repeat_rate']} | {s['recoveries']} | {m['misjudged']}/{m['checked']} | {s['ablated_policy_passes']} |"
        )
    lines += ["", "## 比较", ""]
    for c in report["comparisons"]:
        lines.append(
            f"- {c['a']} vs {c['b']}：{c['verdict']}；成功 {c['success']['a']} vs {c['success']['b']}（p = {c['success']['fisher_p']}）；耗时 {c['duration']}；tokens {c['tokens']}"
        )
        for diff in (c.get("policy_differences") or [])[:12]:
            lines.append(
                f"  - 配置差异 `{diff['key']}`：{diff['a']} → {diff['b']}（来源 {diff['source']}）"
            )
    lines += ["", "## 失败原因与样例", ""]
    for name, s in report["summary"].items():
        lines.append(f"- {name}：{s['failure_reasons'] or '无'}")
        for sample in s["failure_samples"]:
            lines.append(
                f"  - {sample['case']} #{sample['trial']} `{sample['mission_id']}`：{sample['failure']}"
            )
    errors = [r for r in report["runs"] if r["category"] == "harness_error"]
    if errors:
        lines += ["", "## 脚手架错误（不计入成功率分母）", ""]
        lines += [
            f"- {r['strategy']} / {r['case']} #{r['trial']}：{r.get('reason')}" for r in errors
        ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ derived cases (D8-8')
def _spec_from_json(data: Mapping[str, Any]) -> MissionSpec:
    return MissionSpec(
        goal=str(data["goal"]),
        success_criteria=tuple(data["success_criteria"]),
        tenant_id=str(data["tenant_id"]),
        idempotency_key=str(data["idempotency_key"]),
        stop_conditions=tuple(
            data.get("stop_conditions", ("verification_passed", "budget_exhausted"))
        ),
        allowed_tools=tuple(data.get("allowed_tools", ())),
        risk_level=str(data.get("risk_level", "sandbox")),
        budget=Budget.from_json(data.get("budget", {})),
        task_kind=str(data.get("task_kind", "code")),
        workspace_seed=dict(data.get("workspace_seed", {})),
        untrusted_sources=tuple(data.get("untrusted_sources", ())),
        synthesis=data.get("synthesis"),
        conflict_reserve_tokens=int(data.get("conflict_reserve_tokens", 0)),
    )


def library_digest(library: Path) -> str:
    digest = hashlib.sha256()
    for suffix in ("", "-wal"):
        path = library.with_name(library.name + suffix)
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def case_from_evidence(
    evidence: Path,
    *,
    name: str,
    provider: Callable[[], Any],
    kind: str = "fixtures",
    oracle: Oracle | None = None,
    connectors: Callable[[Path], Mapping[str, Any]] | None = None,
) -> EvaluationCase:
    """An old Mission's charter as a new case (S8-06): its spec must hash to what the old
    library recorded, the new runs get their own keys, and the old library is only read."""

    evidence = Path(evidence)
    baseline = json.loads((evidence / "baseline.json").read_text(encoding="utf-8"))
    original = _spec_from_json(baseline["spec"])
    with tempfile.TemporaryDirectory(prefix="orch-derive-") as scratch:
        store = Store.open_readonly(library_copy(evidence / "orchestrator.db", Path(scratch)))
        try:
            missions = store.list_missions()
            created = [
                e
                for m in missions
                for e in store.iter_events(m.id)
                if e.type == "MissionCreated"
                and m.tenant_id == original.tenant_id
                and m.idempotency_key == original.idempotency_key
            ]
        finally:
            store.close()
    if not created:
        raise ValueError("the evidence holds no Mission with this charter")
    recorded = str(created[0].payload.get("spec_hash"))
    if recorded != _spec_hash_of(original):
        raise ValueError(
            "the charter in baseline.json does not hash to the recorded spec (changed or redacted)"
        )
    old_snapshot = baseline.get("policy_snapshot") or {}

    def build(tenant: str, key: str) -> MissionSpec:
        return _spec_from_json({**original.to_json(), "tenant_id": tenant, "idempotency_key": key})

    return EvaluationCase(
        name=name,
        spec=build,
        provider=provider,
        kind=kind,
        oracle=oracle,
        connectors=connectors,
        derived_from={
            "mission_id": created[0].mission_id,
            "tenant_id": original.tenant_id,
            "idempotency_key": original.idempotency_key,
            "spec_hash": recorded,
            "policy_snapshot_hash": old_snapshot.get("hash"),
            "library": str(evidence / "orchestrator.db"),
            "library_sha256": library_digest(evidence / "orchestrator.db"),
        },
    )


__all__ = (
    "EVALUATION_VERSION",
    "FIXTURE_NOTE",
    "PLAN_CONFIG",
    "STRATEGY_OVERRIDES",
    "EvaluationCase",
    "EvaluationPlan",
    "Oracle",
    "Strategy",
    "case_from_evidence",
    "fisher_exact",
    "library_digest",
    "render_markdown",
    "run_plan",
    "run_plan_async",
    "wilson",
)
