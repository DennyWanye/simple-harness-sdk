# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Opt-in P34 *strict* real-provider experiment, not a deterministic regression.

Main may run (with SH_BASEURL/SH_APIKEY securely injected, SH_MODEL=deepseek-flash,
and SH_TOKENIZER_PATH set to the pinned official V4.1 tokenizer):
    pytest --run-real-provider tests/orchestrator/p34/test_real_search_value.py -q

Exactly one FIRST run followed by one COMPARE run, even if FIRST fails. No reroll,
criteria repair, graph injection, seeded knowledge, or scripted model response.
Policy evaluation below is explicitly FIXTURE evidence for approval plumbing only.
The real Planner, Manager, Workers, Critic and Synthesizer own every work result.
Both arms require actual verified C and final S. Only COMPARE additionally requires
Manager graph repair, failed-fragment reuse and same-Task candidate synthesis;
FIRST need not demonstrate fragment reuse or candidate synthesis; both arms audit A.

``native_ui_materials()`` exports the identical charter, seed files and budgets;
it does not call a provider or resolve credentials. The runner/Host must provision
those files and the public policy binding through their normal input surfaces.
This SDK run does NOT prove native UI, priced cost, cold replay, or general model
superiority. Its official DeepSeek profile uses the pinned V4.1 counter for
both context and per-request budget admission, as in the Host source runtime.

The immutable legacy implementation fails its baseline conformance suite for a
message containing spaces. A audits that actual implementation and submits its
analysis for verification; it must not repair the audit subject. Failure is a
property of the supplied baseline, not a request to make the model get it wrong.
Manager decisions and successful repair remain real model work, not guarantees.
Missing behavior FAILS the COMPARE gate; no retries to select a fortunate run.
The paired budget is the existing real_dynamic_dag_closure's 2,000,000/24,
not the smaller 300,000/16 scripted recorder demo budget. It is frozen before
either run; this is not the separate Host original-document N1 experiment.

Public fragment projection refuses a file output plus a pytest criterion because
it cannot relocate arbitrary test code. We do NOT project a pytest criterion.
Instead, the immutable pytest.ini declares the independent analysis suite as the
default. A explicitly runs analysis AND baseline conformance; F projects only
file:analysis.md and retains all layers, including code_test and Critic. Its
default suite checks actual original/projected analysis bytes against an execution
of the frozen baseline. C/S explicitly run final conformance (and C's analysis
suite). F's PASS does not certify baseline conformance or a projected pytest claim.
No test/config is rewritten, skipped, xfailed, or given a canned analysis to pass.
The oracle requires A's nonzero baseline exit and a passing analysis run in actual
Verifier evidence, preserving that failed result after reuse. Main reviews before
any paid invocation; the original unguarded pair failed and remains archived.
The corrected production-profile pair has not yet been executed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from agent_orchestrator.contracts import Budget, MissionStatus, TaskStatus
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.promotion import code_versions
from agent_orchestrator.observability.evidence import write_evidence
from agent_orchestrator.observability.secrets import redact_text
from agent_orchestrator.orchestrator.commit_service import MissionSpec, mission_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.candidate_selection import candidate_path
from agent_orchestrator.runtime.assembly import OrchestratorConfig, resolve_profile_context_policy
from agent_orchestrator.runtime.deepseek_tokens import TOKENIZER_SHA256, DeepSeekV41TokenEstimator
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import RECORDER_SEED, RECORDER_SPEC, package_of, role_of

pytestmark = pytest.mark.real_provider

MODEL = "deepseek-flash"
OFFICIAL_HOST = "api.deepseek.com"
MISSION_BUDGET = Budget(max_tokens=2_000_000, max_attempts=24)
CONSUMER_BUDGET = Budget(max_tokens=400_000, max_attempts=4)
WALL_SECONDS_PER_ARM = 1800
CONSUMER_GOAL = "C：读取已验证分析与独立文档检查，实现并验证记录器"
AUDIT_GOAL = "A：审计不可修改的旧版记录器并验证其符合性"
AUDIT_CRITERIA = (
    "file:analysis.md",
    "pytest:tests/test_analysis.py",
    "pytest:tests/test_baseline.py",
)
AUDIT_BUDGET = Budget(max_tokens=240_000, max_attempts=4)
DOCS_BUDGET = Budget(max_tokens=240_000, max_attempts=3)
CONSUMER_CRITERIA = (
    "file:analysis.md", "pytest:tests/test_analysis.py", "file:recorder.py",
    "pytest:tests/test_recorder.py", "file:VERIFY.md",
)
SYNTHESIS_GOAL = "S：读取C实际产物与验证知识，综合最终交付说明FINAL.md"
CRITERIA = (*tuple(RECORDER_SPEC["success_criteria"]), "file:FINAL.md")

BASELINE_CODE = dedent('''\
    def parse_line(line: str) -> dict:
        ts, level, message = line.split(" ")
        return {"ts": ts, "level": level, "message": message}
''')
BASELINE_TEST = dedent('''\
    import importlib.util
    from pathlib import Path

    def load_baseline():
        path = Path(__file__).resolve().parents[1] / "baseline/recorder.py"
        spec = importlib.util.spec_from_file_location("legacy_recorder", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_baseline_single_word():
        assert load_baseline().parse_line("2026-09-11T10:00:00Z INFO started") == {
            "ts": "2026-09-11T10:00:00Z", "level": "INFO", "message": "started",
        }

    def test_baseline_message_with_spaces():
        assert load_baseline().parse_line(
            "2026-09-11T10:00:01Z ERROR disk full on /data"
        ) == {
            "ts": "2026-09-11T10:00:01Z", "level": "ERROR",
            "message": "disk full on /data",
        }
''')
ANALYSIS_TEST = dedent('''\
    import hashlib
    import importlib.util
    import json
    import re
    from pathlib import Path

    ROOT = Path(__file__).resolve().parents[1]
    SAMPLES = (
        "2026-09-11T10:00:00Z INFO started",
        "2026-09-11T10:00:01Z ERROR disk full on /data",
    )

    def test_analysis_records_actual_immutable_baseline_behavior():
        source = ROOT / "baseline/recorder.py"
        spec = importlib.util.spec_from_file_location("audit_subject", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        observations = []
        for sample in SAMPLES:
            try:
                value = module.parse_line(sample)
            except Exception as error:
                observations.append({
                    "input": sample, "outcome": "error", "error_type": type(error).__name__,
                })
            else:
                observations.append({"input": sample, "outcome": "returned", "value": value})
        expected = {
            "baseline_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "cases": observations,
        }
        # Check both the real original and every projected copy. F's file rule
        # separately requires its exact fresh output; an old input cannot satisfy it.
        paths = [ROOT / "analysis.md", *sorted(ROOT.glob("fragment-output/*/analysis.md"))]
        for path in paths:
            assert path.is_file(), f"missing actual analysis: {path}"
            body = path.read_text(encoding="utf-8")
            blocks = re.findall(r"```json\\s*\\n(.*?)\\n```", body, re.S)
            assert len(blocks) == 1, "one audit JSON block is required"
            assert json.loads(blocks[0]) == expected, "audit differs from actual baseline behavior"
            assert len(re.sub(r"```json.*?```", "", body, flags=re.S).strip()) >= 40, (
                "include a substantive explanation for independent Critic review"
            )
''')
MATERIALS = {
    **RECORDER_SEED,
    "baseline/recorder.py": BASELINE_CODE,
    "tests/test_baseline.py": BASELINE_TEST,
    "tests/test_analysis.py": ANALYSIS_TEST,
    "pytest.ini": "[pytest]\ntestpaths = tests/test_analysis.py\n",
    "spec/AUDIT.md": dedent("""\
        # Immutable baseline audit and separate repair
        baseline/recorder.py is the legacy subject, not the final implementation.
        Do not change any seed file, pytest configuration, test, or baseline bytes.
        Audit both samples listed in tests/test_analysis.py using actual tool execution.
        analysis.md must contain explanation plus exactly one fenced json block:
        {"baseline_sha256": "actual hash", "cases": [actual observations in sample order]}.
        Each case has input and outcome. Returned cases add value; error cases add error_type.
        Derive these values from real execution, not from this schema or assumed success.
        The default suite is ONLY the analysis contract, including projected analysis copies.
        A explicitly also runs tests/test_baseline.py: it checks legacy conformance, not whether
        the auditor did useful work. A failed baseline cannot be accepted as a conforming one.
        A's useful analysis can be independently validated while that failure remains unchanged.
        C repairs into root recorder.py; tests/test_recorder.py is its fixed conformance suite.
        Passing repaired conformance does not make legacy conformance pass.
        No test skips, xfails, monkeypatches, conftest hooks, or added pytest configuration.
        Fragment scope is file:analysis.md ONLY: do not request unsupported pytest relocation.
        Its inherited code_test runs the immutable default analysis suite; Critic still reviews
        the explanation. This is not a certificate for legacy conformance or a projected test.
    """),
}
GOAL = """
审计旧记录器的真实符合性，保存可复用分析，并交付独立修复的 recorder.py 及最终说明。
完整材料和阶段测试范围见 spec/AUDIT.md。全部种子文件不可改，不扩大预算。
请用三个初始工作 Task，精确保留以下 goal、criteria、budget；由真正 Planner 提交图。
A goal「A：审计不可修改的旧版记录器并验证其符合性」，无依赖，outputs仅analysis.md，
criteria按序：file:analysis.md、pytest:tests/test_analysis.py、pytest:tests/test_baseline.py，
预算240000 tokens/4 attempts，priority=1。只审计baseline，不修复它。
运行两套实际测试，写准确的分析和限制，提交真实产物候选让系统验证完整合同；
即使baseline不符合要求也保留分析，不谎报测试通过，不为让审计通过修改被审计对象。
B goal「B：独立文档检查」，无依赖，outputs=DOCS.md，criteria=file:DOCS.md，
预算240000 tokens/3 attempts，priority=2；核查输入、固定测试和原版/修复版的边界。
C goal「C：读取已验证分析与独立文档检查，实现并验证记录器」，初始依赖A、B，
outputs=analysis.md、recorder.py、VERIFY.md；criteria按序：file:analysis.md、
pytest:tests/test_analysis.py、file:recorder.py、pytest:tests/test_recorder.py、file:VERIFY.md；
预算400000 tokens/4 attempts，priority=1。修复只能写root recorder.py，不写baseline。
A/C验证层必须是format_check、rule_check、code_test、critic_review；
B仅用format_check、rule_check、critic_review，独立核查文档，不执行A的analysis套件。
实际baseline审计失败时，Manager可从真实失败结果选择仅file:analysis.md的独立片段验证；
F通过后再调整尚未开始的C依赖为F、B，取消不再采用的A，保留A失败与B成果。
不得把baseline pytest纳入F的file范围，不移除继承的验证层，不让F声称旧实现符合要求。
C实际读取F新验证的analysis和DOCS.md，保留观察事实，独立修复并运行最终固定测试。
COMPARE有两份同Task候选时，分别考虑有界分段和显式词法分隔；完整合同相同，
综合者须读两份实际候选后形成新C，不复制PASS。S由Mission模板创建，不另建S。
S实际读取C的recorder.py、VERIFY.md与已验证知识，写FINAL.md，区分旧版失败和修复版结果。
保存所有失败、未采用候选、实际费用、采用理由与局限；不要为了展示改图制造模型错误。
""".strip()


def selection_policy():
    return {
        "schema_version": 1, "mode": "COMPARE_THEN_SYNTHESIZE", "max_candidates": 2,
        "deadline_seconds": 900.0, "tie_break": "verified_rank_then_result_id-v1",
        "synthesis_limit": 1, "on_deadline": "best_complete_else_stop",
        "synthesis_reserve": {"tokens": 120_000, "cost_micros": 0, "tool_calls": 0},
        "synthesis_attempts_reserved": 1,
    }


def mission_spec():
    return MissionSpec(
        goal=GOAL, success_criteria=CRITERIA, tenant_id="real-p34",
        idempotency_key="real-search-value-v2", budget=MISSION_BUDGET,
        workspace_seed=dict(MATERIALS), allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
        synthesis={
            "goal": SYNTHESIS_GOAL, "success_criteria": list(CRITERIA),
            "verification_policy": ["format_check", "rule_check", "code_test", "critic_review"],
            "outputs": ["FINAL.md"],
            "budget": {"max_tokens": 120_000, "max_attempts": 2},
        },
    )


def native_ui_materials():
    """Pure text/data export: no file writes, credential reads, or model calls."""
    spec = mission_spec().to_json()
    return {
        "scenario": "p34-real-search-value-v2", "mission_spec": spec,
        "contract_hash": sha256_hex(spec), "compare_policy": selection_policy(),
        "material_sha256": {
            path: hashlib.sha256(content.encode()).hexdigest()
            for path, content in MATERIALS.items()
        },
        "audit_goal": AUDIT_GOAL, "audit_criteria": list(AUDIT_CRITERIA),
        "audit_budget": AUDIT_BUDGET.to_json(), "docs_budget": DOCS_BUDGET.to_json(),
        "consumer_goal": CONSUMER_GOAL, "consumer_criteria": list(CONSUMER_CRITERIA),
        "consumer_budget": CONSUMER_BUDGET.to_json(),
        "shared_reservations": {
            "attempt_tokens": 60_000, "critic_tokens": 30_000,
            "manager_tokens": 30_000,
        },
        "model": MODEL, "wall_seconds_per_arm": WALL_SECONDS_PER_ARM,
        "max_runs": 2, "order": ["FIRST_VERIFIED", "COMPARE_THEN_SYNTHESIZE"],
        "budget_source": "tests/orchestrator/step05/test_real_provider_dynamic_dag.py",
        "test_scopes": {
            "A": ["tests/test_analysis.py", "tests/test_baseline.py"],
            "F": {"projected_criteria": ["file:analysis.md"],
                  "default_pytest_target": "tests/test_analysis.py",
                  "pytest_criterion_projection": False},
            "C": ["tests/test_analysis.py", "tests/test_recorder.py"],
            "S": ["tests/test_recorder.py"],
        },
        "limits": "Baseline failure is deterministic; model repair/patch/reuse are not guaranteed.",
    }


def _approve_fixture_policy(orch):
    """Public policy lifecycle only; never used as a work-result quality verdict."""
    base = orch.store.active_policy()
    label = {"oracle": "real-search-value-policy-plumbing", "evidence_kind": "fixture"}
    proposal = orch.commit.propose_policy(
        {**base["params"], "schema_version": 2, "search_selection": selection_policy()},
        manifest=label, source="fixture", principal=Principal("real-p34-host"),
    )
    orch.commit.record_policy_evaluation(
        proposal["proposal_id"], verdict="PASSED",
        reasons=["FIXTURE approval plumbing only; no model quality evaluation"],
        report_hash=sha256_hex(label), baseline_version_id=base["version_id"],
        code_versions=code_versions(), evidence_kind="fixture",
    )
    orch.commit.decide_policy(
        proposal["proposal_id"], principal=Principal("real-p34-host"),
        decision="approve", nonce="real-search-value-v2",
    )
    orch.commit.promote_policy(
        proposal["proposal_id"], principal=Principal("real-p34-host"),
        cooldown_seconds=0, accept_fixture_evidence=True,
    )
    return proposal["version_id"]


class _ObservedProvider:
    """Transparent real-provider observer. No response edits, synthetic usage or retries.

    Requests stay in memory for exact input inspection; disk summaries contain only
    ids, hashes and reported usage. Normal SDK evidence owns persisted work products.
    """

    def __init__(self, inner):
        self.inner = inner
        self.requests = []
        self.calls = []
        self.read_calls = {}
        self.reads = []
        self.writes = []

    @property
    def target(self):
        return self.inner.target

    async def invoke(self, request, *, cancel):
        package = package_of(request)
        attempt = package.get("attempt", {}).get("attempt_id")
        self.requests.append(request)
        row = {
            "request_id": str(request.request_id), "role": role_of(request),
            "attempt_id": attempt,
            # This is a message hash at invoke, NOT a claimed Provider wire hash.
            "message_hash": sha256_hex([
                {"role": str(m.role), "content": str(m.content), "call_id": str(m.call_id)}
                for m in request.messages
            ]),
            "usage": None, "error_type": None,
        }
        self.calls.append(row)
        for message in request.messages:
            key = (attempt, str(message.call_id))
            if str(message.role) != "tool" or key not in self.read_calls:
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            # The gateway wraps a successful workspace payload in a ToolResult.
            value = payload.get("value", {}) if isinstance(payload, dict) else {}
            if not isinstance(payload, dict) or payload.get("outcome") != "succeeded":
                continue
            if isinstance(value, dict) and isinstance(value.get("content"), str):
                self.reads.append({
                    "attempt_id": attempt, "path": self.read_calls[key],
                    "content_hash": hashlib.sha256(value["content"].encode()).hexdigest(),
                    "full": value.get("offset", 0) == 0 and value.get("next_offset") is None,
                })
        try:
            response = await self.inner.invoke(request, cancel=cancel)
        except BaseException as error:
            row["error_type"] = type(error).__name__  # never store exception text/URL/key
            raise
        if response.usage is not None:
            row["usage"] = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
                "cache_tokens": response.usage.cache_tokens,
            }
        row["response_model"] = response.model
        for call in response.tool_calls:
            if call.name == "workspace_read_file":
                self.read_calls[(attempt, str(call.call_id))] = call.arguments.get("path")
            elif call.name == "workspace_write_file":
                self.writes.append({
                    "attempt_id": attempt,
                    "path": posixpath.normpath(str(call.arguments.get("path", ""))),
                })
        return response

    def saw_bytes(self, attempt_id, path, content_hash):
        return any(
            row["attempt_id"] == attempt_id and row["path"] == path
            and row["full"] and row["content_hash"] == content_hash
            for row in self.reads
        )


def _assert_verified(store, task):
    assert task.status is TaskStatus.COMPLETED, "task did not complete"
    assert task.accepted_result_id, "no formal result acceptance"
    result = store.get_result(task.accepted_result_id)
    assert result.verdict == "PASS", "accepted result lacks actual PASS"
    rows = store.list_verifications(result.envelope.id)
    for layer in {"format_check", "rule_check", *task.verification_policy}:
        assert any(r["layer"] == layer and r["status"] == "PASS" for r in rows), layer
    assert not any(r["status"] == "FAIL" for r in rows), "failed layer cannot be inherited"
    return result


def _pytest_summary(stdout):
    """Parse only pytest's final statistics line, never names or traceback text."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    assert lines, "pytest terminal summary is missing"
    terminal = lines[-1].strip("=").strip()
    match = re.fullmatch(
        r"(?P<counts>[0-9]+ [a-z]+(?:, [0-9]+ [a-z]+)*)"
        r" in [0-9]+(?:\.[0-9]+)?s(?: \([0-9]+:[0-9]{2}:[0-9]{2}\))?",
        terminal,
    )
    assert match is not None, "pytest final line is not a complete terminal summary"
    aliases = {
        "passed": "passed", "failed": "failed", "error": "errors", "errors": "errors",
        "warning": "warnings", "warnings": "warnings", "skipped": "skipped",
        "xfail": "xfailed", "xfailed": "xfailed", "xpass": "xpassed", "xpassed": "xpassed",
        "deselected": "deselected",
    }
    counts = {}
    for item in match["counts"].split(", "):
        count, label = item.split(" ")
        assert label in aliases, "unknown pytest terminal outcome"
        key = aliases[label]
        assert key not in counts, "duplicate pytest terminal outcome"
        counts[key] = int(count)
    assert not any(counts.get(key, 0) > 0 for key in (
        "skipped", "xfailed", "xpassed", "deselected",
    )), "fixed acceptance cases cannot have positive skip/xfail/xpass/deselection counts"
    return {key: count for key, count in counts.items() if count > 0}


def _assert_pytest_summary(stdout, expected):
    counts = _pytest_summary(stdout)
    outcomes = {key: count for key, count in counts.items() if key != "warnings"}
    assert outcomes == expected, "pytest terminal counts differ from the fixed suite"


def _test_runs(store, result):
    rows = [row for row in store.list_verifications(result.envelope.id)
            if row["layer"] == "code_test"]
    assert len(rows) == 1, "actual independent code_test evidence is required"
    runs = rows[0]["detail"]["runs"]
    assert runs and all(run.get("receipt") for run in runs), "actual executor receipts required"
    assert not any(run["timed_out"] for run in runs), "timeout is not conformance evidence"
    for run in runs:
        _pytest_summary(run["stdout"])
    return rows[0], runs


def _assert_passing_tests(store, result, targets):
    row, runs = _test_runs(store, result)
    assert row["status"] == "PASS"
    assert {run["target"] for run in runs} == set(targets), "test target scope changed"
    assert all(run["passed"] and run["returncode"] == 0 for run in runs)
    expected_passes = {None: 1, "tests/test_analysis.py": 1, "tests/test_recorder.py": 2}
    for run in runs:
        _assert_pytest_summary(run["stdout"], {"passed": expected_passes[run["target"]]})


def _assert_failed_audit(store, result):
    task = store.get_task(result.envelope.task_id)
    assert task.goal == AUDIT_GOAL and task.success_criteria == AUDIT_CRITERIA
    assert task.budget == AUDIT_BUDGET and tuple(task.outputs) == ("analysis.md",)
    assert set(task.verification_policy) == {
        "format_check", "rule_check", "code_test", "critic_review",
    }
    assert result.verdict == "FAIL", "immutable baseline must remain nonconforming"
    row, runs = _test_runs(store, result)
    assert row["status"] == "FAIL"
    assert {run["target"] for run in runs} == {
        "tests/test_analysis.py", "tests/test_baseline.py",
    }
    by_target = {run["target"]: run for run in runs}
    analysis = by_target["tests/test_analysis.py"]
    baseline = by_target["tests/test_baseline.py"]
    assert analysis["passed"] and analysis["returncode"] == 0
    assert baseline["passed"] is False and baseline["returncode"] == 1
    assert "test_baseline_message_with_spaces" in baseline["stdout"]
    assert "ValueError" in baseline["stdout"], "baseline must fail for its actual known defect"
    _assert_pytest_summary(analysis["stdout"], {"passed": 1})
    _assert_pytest_summary(baseline["stdout"], {"failed": 1, "passed": 1})


def _assert_value(orch, provider, mission_id, compare):
    store = orch.store
    final = store.get_mission(mission_id)
    assert final.status is MissionStatus.COMPLETED, "Mission did not complete"
    assert final.success_criteria == CRITERIA and final.goal == GOAL, "Mission contract drift"
    budget = orch.commit.ledger.account(mission_account(mission_id))
    assert budget.limits == MISSION_BUDGET, "Mission budget was changed"
    assert 0 < budget.settled_tokens <= MISSION_BUDGET.max_tokens
    assert budget.reserved_tokens == 0, "unknown or outstanding usage cannot be called complete"
    assert budget.attempts_created <= MISSION_BUDGET.max_attempts
    assert provider.calls and all(c["usage"] is not None for c in provider.calls), "unknown usage"
    assert not any(c["error_type"] for c in provider.calls), "physical provider error retained"
    for write in provider.writes:
        path = write["path"]
        assert path not in MATERIALS, "model attempted to rewrite immutable audit/test input"
        assert path.rsplit("/", 1)[-1] not in {
            "conftest.py", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg",
        }, "model attempted to change acceptance collection/configuration"
    tasks = store.list_tasks(mission_id)
    for task in tasks:
        for attempt in store.list_attempts(task.id):
            workspace = orch._config.workspaces_root / attempt.id
            if not workspace.is_dir():
                continue  # a denied dispatch need not materialize a workspace
            for path, text in MATERIALS.items():
                source = workspace / path
                assert source.is_file() and source.read_bytes() == text.encode("utf-8"), (
                    "fixed original material/test changed in an actual workspace"
                )
    audits = [task for task in tasks if task.goal == AUDIT_GOAL]
    assert len(audits) == 1, "the immutable baseline audit must run in each arm"
    failed_audits = []
    other_audit_failures = []
    for attempt in store.list_attempts(audits[0].id):
        result = store.find_result_for_attempt(attempt.id)
        if result is not None and result.verdict == "FAIL":
            try:
                _assert_failed_audit(store, result)
            except AssertionError as error:
                # An imperfect earlier analysis remains a failure. Only an actual
                # independently checked partial success can be this gate's origin.
                other_audit_failures.append({"result_id": result.envelope.id, "reason": str(error)})
            else:
                failed_audits.append(result.envelope.id)
    assert failed_audits, "no verified failing baseline audit with valid analysis"
    b = [t for t in tasks if t.goal == "B：独立文档检查"]
    assert len(b) == 1 and b[0].budget == DOCS_BUDGET
    assert tuple(b[0].success_criteria) == ("file:DOCS.md",)
    assert set(b[0].verification_policy) == {"format_check", "rule_check", "critic_review"}
    _assert_verified(store, b[0])
    consumers = [t for t in tasks if t.goal == CONSUMER_GOAL]
    assert len(consumers) == 1, "Planner did not preserve the single shared C task"
    consumer = consumers[0]
    assert consumer.success_criteria == CONSUMER_CRITERIA
    assert consumer.budget == CONSUMER_BUDGET
    assert set(consumer.verification_policy) == {
        "format_check", "rule_check", "code_test", "critic_review",
    }, "C's verification contract was weakened"
    c = _assert_verified(store, consumer)
    _assert_passing_tests(store, c, ("tests/test_analysis.py", "tests/test_recorder.py"))
    # Every candidate for this same Task must receive the unchanged original contract.
    packages = [package_of(r) for r in provider.requests]
    c_packages = [p for p in packages if p.get("task_contract", {}).get("task_id") == consumer.id]
    assert c_packages
    for package in c_packages:
        assert tuple(package["task_contract"]["success_criteria"]) == CONSUMER_CRITERIA
    syntheses = [t for t in tasks if t.kind == "synthesis" and t.goal == SYNTHESIS_GOAL]
    assert len(syntheses) == 1, "actual final synthesis S missing"
    s = _assert_verified(store, syntheses[0])
    _assert_passing_tests(store, s, ("tests/test_recorder.py",))
    assert s.envelope.attempt_id != c.envelope.attempt_id
    assert s.envelope.used_knowledge, "S did not cite actual verified knowledge"
    assert any(
        (knowledge := store.get_knowledge(key)) is not None
        and knowledge.source_result == c.envelope.id and knowledge.status == "VERIFIED"
        for key in s.envelope.used_knowledge
    ), "S must cite knowledge actually verified from C"
    for path in ("recorder.py", "VERIFY.md"):
        artifacts = [a for a in store.list_artifacts(c.envelope.attempt_id) if a.path == path]
        assert len(artifacts) == 1, f"C artifact missing: {path}"
        assert provider.saw_bytes(s.envelope.attempt_id, path, artifacts[0].content_hash), (
            "S must receive actual C artifact bytes in a real Provider request"
        )
    if not compare:
        assert orch.commit.selection_round(consumer.id) is None
        return {"consumer_contract": CONSUMER_CRITERIA, "consumer_result": c.envelope.id,
                "failed_baseline_results": failed_audits,
                "other_audit_failures": other_audit_failures}

    round_ = orch.commit.selection_round(consumer.id)
    assert round_ and round_["decision_id"], "COMPARE did not make a durable decision"
    decision = store.get_receipt(round_["decision_id"])
    assert decision["action"] == "synthesize", "best-only fallback does not prove combined C"
    chosen = decision["selected_inputs"]
    assert len({item["result_id"] for item in chosen}) == 2, "two actual candidates required"
    assert c.envelope.id not in {item["result_id"] for item in chosen}
    assert any(
        a.id == c.envelope.attempt_id and a.role == "synthesizer"
        for a in store.list_attempts(consumer.id)
    ), "C was not a new actual synthesizer Attempt"
    for item in chosen:
        assert item["task_id"] == consumer.id, "different DAG nodes are not same-task candidates"
        assert any(
            provider.saw_bytes(c.envelope.attempt_id,
                               candidate_path(item["result_id"], ref["path"]), ref["hash"])
            for ref in item["artifact_refs"]
        ), "C did not actually read this candidate"
    assert len({
        ref["hash"] for item in chosen for ref in item["artifact_refs"]
        if ref["path"] == "recorder.py"
    }) == 2, "identical implementations do not demonstrate two different candidates"

    manager_intents = [
        i
        for i in store.list_intents("SETTLED")
        if i.kind == "manager" and i.mission_id == mission_id
    ]
    assert manager_intents and any(r["role"] == "manager" for r in provider.calls)
    changes = store.list_graph_changes(mission_id)
    assert changes, "real Manager graph change is mandatory, not optional"
    repairs = [change for change in changes
               if change["source"].get("intent_id") in {i.intent_id for i in manager_intents}
               and any(op.get("op") == "retarget_dependencies" and op.get("task_id") == consumer.id
                       for op in change["operations"])]
    assert repairs, "C dependency repair must originate in an actual Manager intent"
    fragments = store.list_fragment_validations(mission_id)
    reused = []
    for row in fragments:
        receipt = store.get_receipt(row["projection_receipt_id"])
        validation = store.get_task(receipt["validation_task_id"])
        if validation.id not in consumer.dependency_ids:
            continue
        f = _assert_verified(store, validation)
        assert set(validation.verification_policy) == set(audits[0].verification_policy)
        assert [m["origin_text"] for m in receipt["criterion_mapping"]] == ["file:analysis.md"]
        assert not any(c.startswith("pytest:") for c in validation.success_criteria)
        _assert_passing_tests(store, f, (None,))
        assert receipt["source"]["intent_id"] in {i.intent_id for i in manager_intents}
        assert store.get_receipt(receipt["graph_change_id"]) is not None
        inputs = [
            p["validated_fragment_input"]
            for p in c_packages
            if "validated_fragment_input" in p
        ]
        matching = [p for p in inputs if p["validation_result_id"] == f.envelope.id]
        assert matching, "no frozen validated-fragment lineage into C"
        for package in matching:
            assert any(
                provider.saw_bytes(p["attempt"]["attempt_id"], ref["path"],
                                   store.get_artifact(ref["artifact_id"]).content_hash)
                for p in c_packages if p.get("validated_fragment_input") == package
                for ref in package["material_refs"]
            ), "C did not receive independently validated fragment bytes"
        # The origin catalog is frozen by the real Manager, not reconstructed as PASS.
        origins = [i.config["fragment_origin"]["origin"] for i in manager_intents
                   if i.config.get("fragment_origin", {}).get("available")]
        origin = next(
            (
                o for o in origins
                if o["result_id"] == receipt["proposal"]["origin"]["result_id"]
            ),
            None,
        )
        assert origin is not None, "fragment origin not bound to actual Manager input"
        failed = store.get_result(origin["result_id"])
        _assert_failed_audit(store, failed)
        assert failed.envelope.id in failed_audits, (
            "F must come from the real failed baseline audit"
        )
        assert failed.envelope.attempt_id != f.envelope.attempt_id
        reservation = orch.commit.ledger.reservation(failed.envelope.attempt_id)
        assert reservation["state"] == "SETTLED" and reservation["settled_tokens"] > 0
        reused.append(f.envelope.id)
    assert reused, "no failed-fragment cross-branch reuse: real-search-value remains OPEN"
    assert b[0].id in consumer.dependency_ids
    events = store.list_events(mission_id)
    completed = [e for e in events if e.type == "TaskCompleted" and e.task_id == b[0].id]
    assert len(completed) == 1
    assert not any(e.type == "AttemptCreated" and e.task_id == b[0].id
                   and e.seq > completed[0].seq for e in events), "completed B was rerun"
    assert any(e.type == "TaskGraphChanged" and e.seq > completed[0].seq
               and e.payload.get("source", {}).get("intent_id") in
               {change["source"]["intent_id"] for change in repairs}
               for e in events), "repair did not preserve an already completed independent B"
    b_result = store.get_result(b[0].accepted_result_id)
    docs = [a for a in store.list_artifacts(b_result.envelope.attempt_id) if a.path == "DOCS.md"]
    assert len(docs) == 1
    assert any(provider.saw_bytes(p["attempt"]["attempt_id"], "DOCS.md", docs[0].content_hash)
               for p in c_packages if "attempt" in p), "C never read independent B's actual output"
    return {"consumer_contract": CONSUMER_CRITERIA, "consumer_result": c.envelope.id,
            "selection_decision": decision["receipt_id"], "reused_fragments": reused,
            "failed_baseline_results": failed_audits,
            "other_audit_failures": other_audit_failures}


def _dump(path, value):
    body, _ = redact_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    path.write_text(body + "\n", encoding="utf-8")


def _official_runtime_options(config, provider, *, base_url, model, tokenizer_path):
    """Mirror Host source profile wiring without importing Host or reading a key."""
    parsed = urlparse(base_url)
    if (parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("P34 requires the official HTTPS DeepSeek endpoint")
    if model != MODEL:
        raise ValueError("P34 requires deepseek-flash")
    if not tokenizer_path or not Path(tokenizer_path).is_absolute():
        raise ValueError("P34 requires absolute SH_TOKENIZER_PATH")
    path = Path(tokenizer_path)
    if not path.is_file():
        raise ValueError("P34 SH_TOKENIZER_PATH file is missing")
    if hashlib.sha256(path.read_bytes()).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("P34 tokenizer SHA-256 differs from pinned V4.1 bytes")
    counter = DeepSeekV41TokenEstimator(path, model=model)
    policy = resolve_profile_context_policy(config, tokenizer=counter)
    if policy is None:
        raise RuntimeError("P34 requires a fresh source runtime context pool")
    profile = RuntimeProfile(
        "default", provider, model, price_table=config.price_table,
        provider_kind="env", context_policy=policy, tokenizer=counter,
    )
    return {"profiles": {"default": profile}, "provider_token_estimator": counter}


def _admission_identity(profile, counter, *, grants):
    snapshot = profile.context_snapshot()
    assert snapshot is not None and profile.tokenizer is counter
    return {
        "endpoint_host": OFFICIAL_HOST, "model": profile.model,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "estimator_fingerprint": counter.fingerprint,
        "estimator_bound_protocol": counter.bound_protocol,
        "requires_prior_output_reserve": counter.requires_prior_output_reserve,
        "runtime_context_fingerprint": snapshot["fingerprint"],
        "runtime_context_tokenizer_fingerprint": snapshot["tokenizer_fingerprint"],
        "provider_grants": grants,
        "state": "EXERCISED" if grants > 0 else "ZERO_GRANTS",
    }


async def _arm(root, provider, *, compare, base_url, tokenizer_path):
    mode = "COMPARE_THEN_SYNTHESIZE" if compare else "FIRST_VERIFIED"
    directory = root / mode
    config = OrchestratorConfig(
        evidence_root=directory, model=MODEL, max_concurrency=1,
        max_concurrent_model_calls=1, candidates_per_task=2,
        default_max_output_tokens=8192, max_output_tokens_ceiling=32768,
        test_timeout_seconds=120, turn_deadline_seconds=900, lease_seconds=120,
        stall_seconds=300, attempt_reserve_tokens=60_000, critic_reserve_tokens=30_000,
        manager_reserve_tokens=30_000, max_planning_attempts=3,
        manager_after_failures=1, max_manager_rounds=4, max_graph_depth=6,
    )
    report = {"mode": mode, "gate": "FAIL", "checks": {}, "error_type": None}
    start = time.monotonic()
    mission = None
    runtime = _official_runtime_options(
        config, provider, base_url=base_url, model=MODEL, tokenizer_path=tokenizer_path,
    )
    profile = runtime["profiles"]["default"]
    counter = runtime["provider_token_estimator"]
    async with Orchestrator(
        config, provider, profiles=runtime["profiles"],
        provider_token_estimator=counter, critic_wait_seconds=900,
    ) as orch:
        # Both arms install the same fixture-approved active policy. Only the
        # explicit Mission search binding differs; default FIRST stays untouched.
        version = _approve_fixture_policy(orch)
        spec = mission_spec()
        if compare:
            spec = replace(spec, search_policy_version_id=version)
        try:
            mission = await orch.submit_mission(spec)
            await asyncio.wait_for(orch.run(), WALL_SECONDS_PER_ARM)
            report["checks"] = _assert_value(orch, provider, mission.id, compare)
            assert orch.store.connection.execute(
                "SELECT COUNT(*) FROM provider_token_grants"
            ).fetchone()[0] > 0, "production provider admission recorded no grants"
            report["gate"] = "PASS"
        except Exception as error:
            report["error_type"] = type(error).__name__
            # Only our assertion text is safe to include; transport errors may contain URLs.
            if isinstance(error, AssertionError):
                report["assertion"] = str(error)
        finally:
            grants = orch.store.connection.execute(
                "SELECT COUNT(*) FROM provider_token_grants"
            ).fetchone()[0]
            report["runtime_admission"] = _admission_identity(profile, counter, grants=grants)
            report["elapsed_seconds"] = time.monotonic() - start
            report["provider_calls"] = provider.calls
            report["successful_reads"] = provider.reads
            report["write_paths"] = provider.writes
            report["reported_tokens"] = sum(
                c["usage"]["total_tokens"] for c in provider.calls if c["usage"] is not None
            )
            report["policy_evidence_kind"] = "fixture; not model quality"
            if mission is not None:
                final = orch.store.get_mission(mission.id)
                report.update(
                    mission_id=mission.id,
                    status=str(final.status),
                    stop_reason=final.stop_reason,
                )
                write_evidence(
                    directory=directory / "evidence", store=orch.store, commit=orch.commit,
                    mission_id=mission.id, baseline={"materials": native_ui_materials(),
                        "spec": spec.to_json(), "config": config.to_json()},
                    workspaces_root=config.workspaces_root, test_report=report,
                )
            directory.mkdir(parents=True, exist_ok=True)
            _dump(directory / "verdict.json", report)
    return report


def test_real_first_vs_approved_compare_search_value():
    # pytest's existing --run-real-provider marker gates collection. No fallback
    # to Host .env: main must explicitly inject the existing SH_* environment.
    assert os.environ.get("SH_MODEL") == MODEL, "set SH_MODEL=deepseek-flash explicitly"
    assert os.environ.get("SH_BASEURL") and os.environ.get("SH_APIKEY"), (
        "missing SH_* provider configuration"
    )
    assert os.environ.get("SH_TOKENIZER_PATH"), "set SH_TOKENIZER_PATH explicitly"
    import httpx
    from real_provider_config import RealProviderConfig

    from simple_harness.providers import OpenAICompatibleProvider, Secret

    real = RealProviderConfig(os.environ["SH_BASEURL"], os.environ["SH_APIKEY"], MODEL)
    evidence_base = Path(__file__).resolve().parents[3] / ".local-test-evidence"
    root = evidence_base / datetime.now(UTC).strftime("%Y-%m-%d") / (
        "p34-real-search-value-" + uuid4().hex
    )
    root.mkdir(parents=True, exist_ok=False)
    _dump(root / "native-ui-materials.json", native_ui_materials())

    async def paired():
        reports = []
        for compare in (False, True):
            mode = "COMPARE_THEN_SYNTHESIZE" if compare else "FIRST_VERIFIED"
            try:
                async with httpx.AsyncClient() as client:
                    actual = OpenAICompatibleProvider(
                        client, real.base_url, real.model, Secret(real.api_key), timeout=300.0,
                    )
                    report = await _arm(
                        root, _ObservedProvider(actual), compare=compare,
                        base_url=real.base_url, tokenizer_path=os.environ["SH_TOKENIZER_PATH"],
                    )
            except Exception as error:
                # Startup/export/cleanup errors also cannot erase a failed arm or
                # prevent the one predeclared comparison; never retry either arm.
                report = {"mode": mode, "gate": "FAIL", "reported_tokens": 0,
                          "error_type": type(error).__name__, "usage_known": False,
                          "runtime_admission": {
                              "state": "UNKNOWN", "provider_grants": None,
                          }}
                _dump(root / (mode + "-runner-failure.json"), report)
            reports.append(report)
        return reports

    reports = asyncio.run(paired())
    admission = [r["runtime_admission"] for r in reports]
    # Do not infer superiority from node count or a successful mechanism trace.
    summary = {
        "arms": reports, "same_contract_hash": native_ui_materials()["contract_hash"],
        "model_quality_benefit": "NOT_PROVEN: one pair is not a superiority evaluation",
        "token_delta_compare_minus_first": (
            reports[1]["reported_tokens"] - reports[0]["reported_tokens"]
            if all(r.get("usage_known", True) and all(c["usage"] is not None
                       for c in r.get("provider_calls", [])) for r in reports) else None
        ),
        "native_ui": "NOT_RUN", "exact_tokenizer": "NOT_PROVEN", "priced_cost": "UNPRICED",
        "runtime_admission": admission,
        "production_admission_status": (
            "UNKNOWN" if any(a["state"] == "UNKNOWN" for a in admission)
            else "ZERO_GRANTS" if any(a["provider_grants"] == 0 for a in admission)
            else "EXERCISED_BOTH"
        ),
    }
    _dump(root / "comparison.json", summary)
    assert all(r["gate"] == "PASS" for r in reports), (
        f"P34 real-search-value remains OPEN; both runs and failures retained in {root}"
    )
