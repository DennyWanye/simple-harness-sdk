"""The audit oracle enforces the declared experiment budget, not a stale constant."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_real_search_value import (
    AUDIT_BUDGET,
    AUDIT_CRITERIA,
    AUDIT_GOAL,
    _assert_failed_audit,
    _experiment_budgets,
)


def evidence(budget, *, baseline_pass=False):
    task = SimpleNamespace(goal=AUDIT_GOAL, success_criteria=AUDIT_CRITERIA,
                           budget=budget, outputs=("analysis.md",),
                           verification_policy=("format_check", "rule_check", "code_test",
                                                "critic_review"))
    result = SimpleNamespace(verdict="FAIL", envelope=SimpleNamespace(task_id="A", id="result-A"))
    runs = [
        {"target": "tests/test_analysis.py", "passed": True, "returncode": 0,
         "stdout": "1 passed in 0.01s\n", "receipt": {"id": "analysis"}, "timed_out": False},
        {"target": "tests/test_baseline.py", "passed": baseline_pass,
         "returncode": 0 if baseline_pass else 1,
         "stdout": "test_baseline_message_with_spaces ValueError\n1 failed, 1 passed in 0.01s\n",
         "receipt": {"id": "baseline"}, "timed_out": False},
    ]
    store = SimpleNamespace(get_task=lambda _: task,
                            list_verifications=lambda _: [{"layer": "code_test", "status": "FAIL",
                                                           "detail": {"runs": runs}}])
    return store, result


def test_legacy_default_remains_exact_and_rejects_larger_budget():
    _assert_failed_audit(*evidence(AUDIT_BUDGET))
    with pytest.raises(AssertionError):
        _assert_failed_audit(*evidence(replace(AUDIT_BUDGET, max_tokens=320_000)))


@pytest.mark.parametrize("profile", ["original-v2", "docs480-s240-v3",
                                     "audit320-docs480-s240-v4", "audit480-docs480-s240-v5"])
def test_each_declared_audit_budget_accepts_exact_and_rejects_drift(profile):
    budget, _, _ = _experiment_budgets(profile)
    _assert_failed_audit(*evidence(budget), audit_budget=budget)
    for changed in (replace(budget, max_tokens=budget.max_tokens + 1),
                    replace(budget, max_attempts=budget.max_attempts + 1)):
        with pytest.raises(AssertionError):
            _assert_failed_audit(*evidence(changed), audit_budget=budget)
    with pytest.raises(AssertionError):
        _assert_failed_audit(*evidence(budget, baseline_pass=True), audit_budget=budget)
