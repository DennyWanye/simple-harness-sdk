# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The gates between a candidate policy and the ACTIVE version (original §28 "Offline
Evaluation → A/B Test → 审批后上线"; theory 12 §8 Offline Evaluation, §10 A/B Test;
ORCH-BUILD §11.2 / §11.4 S9-02 / S9-03 / S9-07; plan D9-6' / D9-7').

A candidate is evaluated against the ACTIVE version on the same held-out cases, the same
budgets and the same deployment configuration — each side pins its policy into every
run's own new *evaluation* library, so a candidate never touches a production Mission
before it is approved and promoted ("shadow / 测试" of S9-03).  The verdict uses the
step-8 statistics: samples per case and per side, a harness error on either side makes
the result INSUFFICIENT (never FAILED), cost fails only when clearly worse, and PASSED
means *non-inferior within the samples* — never "better".

Before anything runs, the leakage check refuses a case that is the same task as one in
the training set (task identity, not ``spec_hash``: tenant and key are rewritten by every
evaluation), a case derived from a training Mission, or a derived case older than the
end of the training window."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..observability.evaluation import (
    EvaluationPlan,
    EvaluationRefused,
    Strategy,
    run_plan_async,
)
from ..observability.secrets import redact_text
from .promotion import code_versions, spec_task_identity

GATE_VERSION = "gates-v1"
PASS_NOTE = "非劣：样本内未见退化（不是'提升'的证据）"
FIXTURE_NOTE = "fixture 评测只证明流程，不证明质量"


@dataclass(frozen=True)
class GateSpec:
    min_samples_real: int = 3  # per case and per side (plan D9-7')
    min_samples_fixture: int = 1  # fixtures are deterministic: they prove the mechanism

    def to_json(self) -> dict[str, Any]:
        return {
            "min_samples_real": self.min_samples_real,
            "min_samples_fixture": self.min_samples_fixture,
            "cost_rule": "candidate tokens min > baseline tokens max → FAILED",
            "quality_rule": "per case candidate success rate ≥ baseline; misjudgments not more",
            "risk_rule": "harness errors → INSUFFICIENT; waiting for a person not more; "
            "no snapshot difference outside the whitelist",
        }


def evidence_kind(cases: Sequence[Any]) -> str:
    return "fixture" if all(case.kind == "fixtures" for case in cases) else "real"


def leakage(proposal: Mapping[str, Any], cases: Sequence[Any]) -> list[str]:
    """Plan D9-6': the same task in training and evaluation, a case derived from a
    training Mission, or a derived case not newer than the training window."""

    manifest = dict(proposal.get("manifest") or {})
    training = list(manifest.get("training") or [])
    identities = {str(t["task_identity"]) for t in training if t.get("task_identity")}
    missions = {str(t["mission_id"]) for t in training if t.get("mission_id")}
    window_end = dict(manifest.get("window") or {}).get("end")
    problems = []
    for case in cases:
        spec = case.spec("evaluation", f"leakage-probe:{case.name}")
        if spec_task_identity(spec) in identities:
            problems.append(f"case {case.name}: 与训练集是同一任务（任务身份相同）")
        derived = dict(case.derived_from or {})
        if derived.get("mission_id") in missions:
            problems.append(f"case {case.name}: 派生自训练集里的 Mission {derived['mission_id']}")
        created = derived.get("created_at")
        if created is not None and window_end is not None and float(created) <= float(window_end):
            problems.append(f"case {case.name}: 来源时间 {created} 不晚于训练窗口末端 {window_end}")
    return problems


def gate_verdict(report: Mapping[str, Any], *, gate: GateSpec, kind: str) -> tuple[str, list[str]]:
    summary = report["summary"]
    active, candidate = summary["active"], summary["candidate"]
    [comparison] = report["comparisons"]
    if active["harness_errors"] or candidate["harness_errors"]:
        return "INSUFFICIENT", [
            f"脚手架错误：active {active['harness_errors']} / candidate "
            f"{candidate['harness_errors']}，样本不可比（不判 FAILED）"
        ]
    minimum = gate.min_samples_fixture if kind == "fixture" else gate.min_samples_real
    thin = [
        row["case"] for row in comparison["per_case"] if min(row["a"][1], row["b"][1]) < minimum
    ]
    if thin:
        return "INSUFFICIENT", [f"每个 case 每一方至少 {minimum} 个样本；不足：{thin}"]
    reasons = []
    for row in comparison["per_case"]:
        (won_a, runs_a), (won_b, runs_b) = row["a"], row["b"]
        if won_b / runs_b < won_a / runs_a:
            reasons.append(
                f"质量：case {row['case']} 候选成功 {won_b}/{runs_b} 低于基线 {won_a}/{runs_a}"
            )
    if (
        candidate["verification_misjudgment"]["misjudged"]
        > active["verification_misjudgment"]["misjudged"]
    ):
        reasons.append("质量：候选的验证误判多于基线")
    tokens_a, tokens_b = active["tokens"], candidate["tokens"]
    if (
        tokens_b["min"] is not None
        and tokens_a["max"] is not None
        and tokens_b["min"] > tokens_a["max"]
    ):
        reasons.append(
            f"成本：候选 tokens 最小值 {tokens_b['min']} "
            f"高于基线最大值 {tokens_a['max']}（明显更贵）"
        )
    if candidate["waiting_for_human"] > active["waiting_for_human"]:
        reasons.append("风险：候选等待人工多于基线")
    outside = [d["key"] for d in (comparison.get("policy_differences") or [])]
    if outside:
        reasons.append(f"风险：两方的开始快照在策略白名单之外还有差异 {outside}")
    return ("FAILED", reasons) if reasons else ("PASSED", [])


async def evaluate_candidate_async(
    commit: Any,
    proposal_id: str,
    *,
    cases: Sequence[Any],
    directory: Path,
    trials: int = 1,
    gate: GateSpec | None = None,
    config: Mapping[str, Any] | None = None,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Run the candidate and the ACTIVE version side by side and record the verdict on
    the proposal (plan D9-7').  A leaking case refuses the evaluation before it runs."""

    gate = gate or GateSpec()
    store = commit.store
    proposal = store.get_policy_proposal(proposal_id)
    if proposal is None:
        raise EvaluationRefused(f"unknown policy proposal {proposal_id}")
    if proposal["state"] in {"PROMOTED", "REJECTED"}:  # review P2-8: before any run
        raise EvaluationRefused(
            f"proposal {proposal_id} is {proposal['state']}; it is not evaluated again"
        )
    version = store.get_policy_version(str(proposal["version_id"]))
    active = store.active_policy()
    if active is None or not active.get("params") or version is None:
        raise EvaluationRefused(
            "an evaluation needs an ACTIVE policy with parameters to compare against"
        )
    problems = leakage(proposal, cases)
    if problems:
        raise EvaluationRefused("训练 / 评测泄漏，拒绝评测：" + "；".join(problems))
    kind = evidence_kind(cases)
    plan = EvaluationPlan(
        name=f"gate-{proposal_id}",
        cases=tuple(cases),
        strategies=(
            Strategy("active", policy_pin=active["params"]),
            Strategy("candidate", policy_pin=version["params"]),
        ),
        trials=trials,
        config=dict(config or {}),
        timeout_seconds=timeout_seconds,
        min_samples=gate.min_samples_fixture if kind == "fixture" else gate.min_samples_real,
    )
    directory = Path(directory)
    report = await run_plan_async(plan, directory)
    verdict, reasons = gate_verdict(report, gate=gate, kind=kind)
    report_hash = hashlib.sha256((directory / "evaluation.json").read_bytes()).hexdigest()
    [comparison] = report["comparisons"]
    record = commit.record_policy_evaluation(
        proposal_id,
        verdict=verdict,
        reasons=reasons,
        report_hash=report_hash,
        baseline_version_id=active["version_id"],
        code_versions=code_versions(),
        evidence_kind=kind,
        summary={
            "gate_version": GATE_VERSION,
            "gate": gate.to_json(),
            "directory": str(directory),
            "per_case": comparison["per_case"],
            "note": (PASS_NOTE if verdict == "PASSED" else None),
            "fixture_note": FIXTURE_NOTE if kind == "fixture" else None,
        },
    )
    document = {
        "gate_version": GATE_VERSION,
        "proposal_id": proposal_id,
        "candidate_version_id": proposal["version_id"],
        "baseline_version_id": active["version_id"],
        "verdict": verdict,
        "reasons": reasons,
        "evidence_kind": kind,
        "note": PASS_NOTE if verdict == "PASSED" else None,
        "fixture_note": FIXTURE_NOTE if kind == "fixture" else None,
        "report_hash": report_hash,
        "evaluation_id": record["evaluation_id"],
        "gate": gate.to_json(),
    }
    text, _found = redact_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    )
    (directory / "gate.json").write_text(text, encoding="utf-8")
    return {
        "verdict": verdict,
        "reasons": reasons,
        "evaluation": record,
        "evidence_kind": kind,
        "report": report,
    }


def evaluate_candidate(commit: Any, proposal_id: str, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(evaluate_candidate_async(commit, proposal_id, **kwargs))


__all__ = (
    "GATE_VERSION",
    "GateSpec",
    "evaluate_candidate",
    "evaluate_candidate_async",
    "evidence_kind",
    "gate_verdict",
    "leakage",
)
