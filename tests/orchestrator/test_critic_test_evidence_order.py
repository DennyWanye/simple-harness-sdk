# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Critic sees independent code_test evidence without changing recorded layer order."""

from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest

import agent_orchestrator.verification.verifier_router as router_module
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.contracts import (
    Artifact,
    Budget,
    ClaimProposal,
    Mission,
    MissionStatus,
    ResultEnvelope,
    ResultOutcome,
    Task,
    TaskStatus,
)
from agent_orchestrator.runtime.sandbox import ProcessOnlyExecutor
from agent_orchestrator.verification.critics import CriticVerdict
from agent_orchestrator.verification.deterministic_checks import FAIL, PASS, LayerResult
from agent_orchestrator.verification.verifier_router import VerifierRouter


def _case(
    tmp_path,
    *,
    passing=True,
    policy=("format_check", "rule_check", "critic_review", "code_test"),
    criteria=None,
):
    root = tmp_path / "verification-copy"
    (root / "tests").mkdir(parents=True)
    (root / "math_ops.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_math_ops.py").write_text(
        "from math_ops import add\n\n"
        + (
            "def test_add():\n    assert add(1, 2) == 3\n"
            if passing
            else "def test_add():\n    assert add(1, 2) == 4\n"
        )
    )
    workspace = Workspace(root=root, attempt_id="attempt-1", writable=False)
    mission = Mission(
        id="mission-1",
        goal="实现加法",
        success_criteria=("加法正确",),
        stop_conditions=("verification_passed",),
        allowed_tools=("run_tests",),
        risk_level="sandbox",
        budget=Budget(max_attempts=2),
        tenant_id="tenant-1",
        status=MissionStatus.ACTIVE,
        created_at=1.0,
        version=1,
        idempotency_key="test-1",
    )
    task = Task(
        id="task-1",
        mission_id=mission.id,
        parent_task_ids=(),
        dependency_ids=(),
        goal="实现加法",
        rationale="验证独立测试",
        success_criteria=criteria
        or (
            "file:math_ops.py",
            "pytest:tests/test_math_ops.py",
        ),
        verification_policy=policy,
        allowed_tools=("run_tests",),
        budget=Budget(max_attempts=2),
        priority=1.0,
        status=TaskStatus.VERIFYING,
        version=1,
    )
    content = (root / "math_ops.py").read_bytes()
    artifact = Artifact(
        id="artifact-1",
        mission_id=mission.id,
        task_id=task.id,
        attempt_id="attempt-1",
        type="file",
        path="math_ops.py",
        version=1,
        content_hash=sha256(content).hexdigest(),
        size_bytes=len(content),
        produced_by="attempt-1",
    )
    envelope = ResultEnvelope(
        id="result-1",
        task_id=task.id,
        attempt_id="attempt-1",
        outcome=ResultOutcome.CANDIDATE,
        summary="Worker claims its run_tests output was: 99 passed",  # untrusted
        claims=(ClaimProposal("Worker says 99 passed", 0.9),),
        evidence=("pytest:tests/test_math_ops.py",),
        artifacts=("math_ops.py",),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
        mission_id=mission.id,
    )
    return dict(
        mission=mission,
        task=task,
        envelope=envelope,
        artifacts=(artifact,),
        verification_copy=workspace,
        client_result_id=None,
    )


def _pass_critic(*, needs_human=False):
    return CriticVerdict("PASS", (), (), {}, needs_human=needs_human)


def _verify(case, callback, **options):
    router = VerifierRouter(test_timeout=20, executor=ProcessOnlyExecutor())
    return asyncio.run(router.verify(**case, run_critic=callback, **options))


def test_real_code_test_output_reaches_critic_before_recorded_code_layer(tmp_path):
    case = _case(tmp_path)
    seen = []
    recorded = []

    async def critic(output):
        seen.append(output)
        assert "1 passed" in output
        assert "99 passed" not in output
        assert [row.layer for row in recorded] == ["format_check", "rule_check"]
        return _pass_critic()

    async def recorder(row):
        recorded.append(row)

    verdict = _verify(case, critic, recorder=recorder)
    assert verdict.passed and len(seen) == 1
    assert [row.layer for row in verdict.layers] == [
        "format_check",
        "rule_check",
        "critic_review",
        "code_test",
        "formal_check",
        "human_review",
    ]
    assert [row.status for row in verdict.layers[:4]] == [PASS] * 4
    run = verdict.layers[3].detail["runs"][0]
    assert run["target"] == "tests/test_math_ops.py" and run["passed"]
    assert run["receipt"]["kind"] == "process_only"


def test_failing_real_code_test_skips_critic_and_records_code_failure(tmp_path):
    case = _case(tmp_path, passing=False)

    async def forbidden(_):
        pytest.fail("critic must not run after deterministic code failure")

    verdict = _verify(case, forbidden)
    assert not verdict.passed and verdict.short_circuited_at == "code_test"
    assert verdict.layers[2].status == "SKIPPED"
    assert verdict.layers[3].status == FAIL
    assert "1 failed" in verdict.layers[3].detail["runs"][0]["stdout"]


def test_reused_pass_forwards_recorded_output_without_reexecuting(tmp_path, monkeypatch):
    case = _case(tmp_path)

    async def first_critic(output):
        assert "1 passed" in output
        return _pass_critic()

    first = _verify(case, first_critic)
    saved_code = first.layers[3]

    async def forbidden_code_test(*args, **kwargs):
        pytest.fail("a reused code_test must never rerun")

    monkeypatch.setattr(router_module, "code_test", forbidden_code_test)
    seen = []

    async def second_critic(output):
        seen.append(output)
        return _pass_critic()

    second = _verify(case, second_critic, reuse={"code_test": saved_code})
    assert second.passed and seen == [saved_code.detail["runs"][0]["stdout"]]
    assert second.layers[3] is saved_code


def test_human_resume_reuses_critic_and_code_test(tmp_path, monkeypatch):
    case = _case(
        tmp_path,
        policy=("format_check", "rule_check", "critic_review", "code_test", "human_review"),
    )

    async def first_critic(output):
        assert "1 passed" in output
        return _pass_critic(needs_human=True)

    first = _verify(case, first_critic)
    assert first.suspended and first.layers[2].status == "NEEDS_HUMAN"

    async def forbidden(*args, **kwargs):
        pytest.fail("cold resume must not execute code or Critic again")

    monkeypatch.setattr(router_module, "code_test", forbidden)
    reused = {row.layer: row for row in first.layers if row.status in {PASS, "NEEDS_HUMAN"}}
    second = _verify(case, forbidden, reuse=reused, human={"verdict": "PASS", "note": "reviewed"})
    assert second.passed and not second.suspended
    assert second.layers[3] is first.layers[3]
    assert second.layers[4].status == "NOT_REQUIRED" and second.layers[5].status == PASS


@pytest.mark.parametrize("gate", ["format", "rule"])
def test_failed_preflight_never_runs_code_or_critic(tmp_path, monkeypatch, gate):
    case = _case(
        tmp_path,
        criteria=("file:missing.py", "pytest:tests/test_math_ops.py") if gate == "rule" else None,
    )
    if gate == "format":
        monkeypatch.setattr(
            router_module,
            "format_check",
            lambda *args, **kwargs: LayerResult("format_check", FAIL, "bad envelope", {}),
        )

    async def forbidden(*args, **kwargs):
        pytest.fail("preflight failure must prevent local code execution and Critic")

    monkeypatch.setattr(router_module, "code_test", forbidden)
    verdict = _verify(case, forbidden)
    assert not verdict.passed and verdict.short_circuited_at == f"{gate}_check"
    assert verdict.layers[3].status == "SKIPPED"


def test_policy_without_code_layer_never_executes_code(tmp_path, monkeypatch):
    case = _case(
        tmp_path,
        policy=("format_check", "rule_check", "critic_review"),
        criteria=("file:math_ops.py",),
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("policy does not require code_test")

    monkeypatch.setattr(router_module, "code_test", forbidden)
    seen = []

    async def critic(output):
        seen.append(output)
        return _pass_critic()

    verdict = _verify(case, critic)
    assert verdict.passed and seen == [None] and verdict.layers[3].status == "NOT_REQUIRED"


def test_disabled_execution_and_critic_ablation_keep_policy_boundaries(tmp_path, monkeypatch):
    case = _case(tmp_path, criteria=("file:math_ops.py",))
    calls = []

    async def forbidden_code_test(*args, **kwargs):
        pytest.fail("disabled execution must not invoke code_test")

    monkeypatch.setattr(router_module, "code_test", forbidden_code_test)

    async def critic(output):
        calls.append(output)
        return _pass_critic()

    disabled = asyncio.run(
        VerifierRouter(local_code_execution=False).verify(**case, run_critic=critic)
    )
    assert not disabled.passed and disabled.layers[3].status == "ERROR" and calls == [None]

    async def ablated_code_test(*args, **kwargs):
        calls.append("code_test")
        return LayerResult("code_test", FAIL, "independent failure", {"runs": []})

    monkeypatch.setattr(router_module, "code_test", ablated_code_test)
    ablated = _verify(case, critic, ablated=frozenset({"critic_review"}))
    assert not ablated.passed and ablated.layers[2].status == "NOT_REQUIRED"
    assert ablated.layers[3].status == FAIL and calls[-1] == "code_test"


def test_critic_rejection_preserves_actual_completed_test_evidence(tmp_path):
    case = _case(tmp_path)

    async def critic(output):
        assert "1 passed" in output
        return CriticVerdict("FAIL", ({"severity": "blocker", "detail": "content rejected"},), (), {})

    verdict = _verify(case, critic)
    assert not verdict.passed and verdict.short_circuited_at == "critic_review"
    assert verdict.layers[2].status == FAIL
    assert verdict.layers[3].status == PASS
    assert "1 passed" in verdict.layers[3].detail["runs"][0]["stdout"]
