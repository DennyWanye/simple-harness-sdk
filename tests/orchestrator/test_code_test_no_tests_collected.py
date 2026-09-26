# SPDX-License-Identifier: Apache-2.0
"""Assurance 1.1 default-ON (2026-09-23): a courtesy pytest run that collects nothing.

Host real-model run 1 (mission-65b8fedd9e75c45b): a documentation leaf on a
code-execution deployment carries ``code_test`` by system default, pytest on a
workspace without tests exits 5 and the layer graded FAIL, so the Critic never
ran and the Mission stalled. The default run that no ``pytest:`` criterion asked
for attests nothing and fails nothing; a *named* target that collects nothing is
still a criterion nobody checks.
"""

from __future__ import annotations

import asyncio

from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.assurance.checks import Grade
from agent_orchestrator.assurance.executor_checks import executor_run_facts
from agent_orchestrator.contracts import Budget, Task, TaskStatus
from agent_orchestrator.memory.claims import ran_test_targets
from agent_orchestrator.verification.deterministic_checks import code_test


def _task(criteria: tuple[str, ...]) -> Task:
    return Task(
        id="task-1", mission_id="mission-1", parent_task_ids=(), dependency_ids=(),
        goal="写一份 NOTES.md", rationale="test", success_criteria=criteria,
        verification_policy=("format_check", "rule_check", "code_test", "critic_review"),
        allowed_tools=("workspace_write_file",), budget=Budget(max_tokens=1000, max_attempts=1),
        priority=1.0, status=TaskStatus.ACTIVE, version=1,
    )


def test_default_run_that_collects_no_tests_is_not_a_failure(tmp_path):
    workspace = Workspace(tmp_path / "verify", "task-1:attempt-1", True)
    workspace.root.mkdir()
    workspace.write_text("NOTES.md", "- 一\n- 二\n- 三\n")
    layer = asyncio.run(code_test(_task(("file:NOTES.md",)), verification_copy=workspace, timeout=120))
    assert layer.status == "PASS", layer
    [run] = layer.detail["runs"]
    assert run["target"] is None and run["returncode"] == 5 and run["no_tests_collected"] is True
    assert run["passed"] is True
    assert "not applicable, counts as satisfied" in layer.summary
    # The audited claim grader (a4aae8c bytes, never edited) keys the default run
    # under "": a claim naming a real ``pytest:<target>`` is still unverified by it.
    assert ran_test_targets([layer.to_json()]) == {"": True}


def test_bare_pytest_criterion_that_collects_no_tests_fails(tmp_path):
    # 2026-09-25 主流程优化条目 3: ``pytest:`` with no path still *asks* for tests, so a
    # workspace with none is an unchecked criterion, not "nothing to attest".
    workspace = Workspace(tmp_path / "verify", "task-1:attempt-1", True)
    workspace.root.mkdir()
    workspace.write_text("NOTES.md", "- 一\n")
    layer = asyncio.run(code_test(_task(("pytest:",)), verification_copy=workspace, timeout=120))
    assert layer.status == "FAIL", layer
    [run] = layer.detail["runs"]
    assert run["target"] is None and run["returncode"] == 5 and "no_tests_collected" not in run
    assert run["passed"] is False


def test_named_target_that_collects_no_tests_still_fails(tmp_path):
    workspace = Workspace(tmp_path / "verify", "task-1:attempt-1", True)
    workspace.root.mkdir()
    (workspace.root / "tests").mkdir()
    workspace.write_text("tests/__init__.py", "")
    layer = asyncio.run(code_test(_task(("pytest:tests",)), verification_copy=workspace, timeout=120))
    assert layer.status == "FAIL", layer
    [run] = layer.detail["runs"]
    assert run["target"] == "tests" and run["returncode"] == 5 and "no_tests_collected" not in run
    assert layer.summary.startswith("pytest failed: ")


def _facts(*, flagged: bool, scope: bool = True) -> dict:
    run = {
        "target": None, "returncode": 5, "timed_out": False, "passed": flagged,
        "stdout": "no tests ran in 0.01s", "command": ["python", "-c", "..."],
        "receipt": {"execution_id": "exec-1", "kind": "process", "isolated": True,
                    "environment_digest": "e" * 64, "effective_limits": {}, "exit_code": 5,
                    "truncated": False, "timed_out": False, "limit_exceeded": False,
                    "tree_killed": True, "status": "ok", "residual_pids": []},
    }
    if flagged:
        run["no_tests_collected"] = True
    detail = {"runs": [run]}
    if scope:
        detail["observation_scope"] = {"schema": "pytest-observation-v2", "workspace_hash": "a" * 64}
    return {"layer": "code_test", "status": "PASS" if flagged else "FAIL", "summary": "", "detail": detail}


def test_executor_facts_grade_the_vacuous_run_pass_only_when_bound():
    state, grade, document = executor_run_facts(_facts(flagged=True), layer="code_test")
    assert (state, grade) == ("SUCCEEDED", Grade.PASS)
    assert "NOT APPLICABLE, counts as satisfied" in document["reason"] and "INCONCLUSIVE" in document["reason"]
    assert document["runs"][0]["no_tests_collected"] is True
    state, grade, document = executor_run_facts(_facts(flagged=True, scope=False), layer="code_test")
    assert (state, grade) == ("SUCCEEDED", Grade.UNKNOWN)
    assert "not bound" in document["reason"]


def test_executor_facts_keep_an_unflagged_exit_5_as_fail():
    state, grade, document = executor_run_facts(_facts(flagged=False), layer="code_test")
    assert (state, grade) == ("SUCCEEDED", Grade.FAIL)
    assert document["runs"][0]["no_tests_collected"] is False
