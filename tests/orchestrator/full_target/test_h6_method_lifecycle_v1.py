from __future__ import annotations

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.planning.htn.method_lifecycle import (
    EvaluationSetV1,
    MethodEvaluationRecordV1,
    MethodLifecyclePolicyV1,
    MethodLifecycleService,
)


def test_evaluation_set_is_frozen_and_order_independent() -> None:
    left = EvaluationSetV1.build("code-v1", ("s2", "s1"), ("h2", "h1"))
    right = EvaluationSetV1.build("code-v1", ("s1", "s2"), ("h1", "h2"))
    assert left == right
    with pytest.raises(ContractError, match="must not be empty"):
        EvaluationSetV1.build("code-v1", (), ())


def test_policy_requires_trials_and_acceptance_thresholds() -> None:
    policy = MethodLifecyclePolicyV1()
    assert policy.min_trials == 20
    assert policy.min_heldout == 5
    with pytest.raises(ContractError):
        MethodLifecyclePolicyV1(min_trials=0)
    with pytest.raises(ContractError):
        MethodLifecyclePolicyV1(acceptance_threshold=1.1)


def test_evaluation_contract_accepts_complete_run_evidence_and_rejects_failed_gate() -> None:
    service = MethodLifecycleService()
    evaluation_set = EvaluationSetV1.build(
        "code-v1", tuple(f"t{i}" for i in range(20)), tuple(f"h{i}" for i in range(5))
    )
    good = MethodEvaluationRecordV1(
        method_ref="m@1#abc",
        evaluation_set=evaluation_set,
        trial_count=20,
        heldout_count=5,
        acceptance_rate=0.95,
        critical_side_effect_failures=0,
        unresolved_operations=0,
        costs=tuple(1.0 + index / 100 for index in range(25)),
    )
    assert service.evaluate(good).status == "EVALUATED"
    bad = MethodEvaluationRecordV1(
        method_ref="m@2#abc",
        evaluation_set=evaluation_set,
        trial_count=20,
        heldout_count=5,
        acceptance_rate=0.95,
        critical_side_effect_failures=1,
        unresolved_operations=0,
        costs=(1.0,) * 25,
    )
    assert service.evaluate(bad).status == "REJECTED"
    with pytest.raises(ContractError, match="not evaluated"):
        service.promote(bad)


def test_evaluation_set_mismatch_is_rejected_on_replay() -> None:
    service = MethodLifecycleService()
    first = EvaluationSetV1.build("appworld-v1", ("a",), ("b",))
    record = MethodEvaluationRecordV1("m@1#abc", first, 1, 1, 0.9, 0, 0, (1.0, 1.0))
    service.evaluate(record)
    changed = EvaluationSetV1.build("appworld-v1", ("a", "new"), ("b",))
    replay = MethodEvaluationRecordV1(
        "m@1#abc", changed, 2, 1, 0.9, 0, 0, (1.0, 1.0, 1.0)
    )
    with pytest.raises(ContractError, match="frozen"):
        service.evaluate(replay)


def test_evaluated_status_and_frozen_evidence_survive_restart() -> None:
    service = MethodLifecycleService()
    evaluation_set = EvaluationSetV1.build(
        "code-v1", tuple(f"t{i}" for i in range(20)), tuple(f"h{i}" for i in range(5))
    )
    record = MethodEvaluationRecordV1(
        "m@restart#abc", evaluation_set, 20, 5, 0.95, 0, 0, (1.0,) * 25
    )
    service.evaluate(record)
    restored = MethodLifecycleService.from_json(service.to_json())
    assert restored.status(record.method_ref) == "EVALUATED"
    assert restored.to_json() == service.to_json()
