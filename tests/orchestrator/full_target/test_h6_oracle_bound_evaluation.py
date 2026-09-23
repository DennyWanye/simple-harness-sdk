"""Focused H6 oracle receipt coverage; no model or fabricated Mission completion."""
from __future__ import annotations

import hashlib

import pytest

import test_htn_end_to_end as e2e

from agent_orchestrator.contracts.htn import (
    MethodRegistryStatus,
    RegistryAuthor,
    admit_method,
)
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.planning.htn.method_lifecycle import (
    EvaluationSetV1,
    MethodLifecyclePolicyV1,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.method_evaluation_store import MethodEvaluationStore
from agent_orchestrator.storage.store import Store, StoreConflict


def test_baseline_must_be_frozen_before_work_and_exact_replay_stays_valid(tmp_path):
    world = e2e.build_world(tmp_path, key="h6-baseline-freeze", name="baseline.db")
    candidate = e2e._outer(method_id="h6.baseline.freeze")
    reference = candidate.method_ref()
    HtnStore(world.store).register_method(
        candidate,
        admit_method(reference, MethodRegistryStatus.TRIAL_ADMITTED,
                     author=RegistryAuthor.SYSTEM, trial_scope_mission=world.mission.id),
    )
    service = MethodEvaluationStore(world.store)
    evaluation = EvaluationSetV1.build("code-v1", ("future-trial",), ("future-heldout",))
    policy = MethodLifecyclePolicyV1(min_trials=1, min_heldout=1)
    baseline = (world.mission.id,)
    world.service.import_usage(
        "baseline-call", world.mission.id,
        (UsageFact("baseline-call", 80, 20, None, unknown=False),),
    )
    with pytest.raises(StoreConflict, match="before scheduling"):
        service.freeze(reference, evaluation, baseline_mission_ids=baseline, policy=policy)
    assert world.store.connection.execute("SELECT count(*) FROM method_evaluations").fetchone()[0] == 0

    # A genuinely prospective baseline is valid. Work after freeze must not
    # prevent an exact replay of the original frozen document during restart.
    future, _ = world.service.create_mission(e2e._spec("future-baseline", mode=e2e.HIERARCHICAL_SEMANTICS))
    frozen = service.freeze(reference, evaluation, baseline_mission_ids=(future.id,), policy=policy)
    world.service.import_usage(
        "frozen-baseline-call", future.id,
        (UsageFact("frozen-baseline-call", 80, 20, None, unknown=False),),
    )
    world.store.close()
    reopened = Store.open(tmp_path / "baseline.db")
    try:
        assert MethodEvaluationStore(reopened).freeze(
            reference, evaluation, baseline_mission_ids=(future.id,), policy=policy,
        ) == frozen
    finally:
        reopened.close()


def _frozen_real_trial(tmp_path, *, key: str):
    """Freeze before work, then finish one Mission through the original real chain."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    world = e2e.build_world(tmp_path, key=key, name="oracle-real.db")
    candidate = e2e._outer(method_id=key + ".candidate")
    reference = candidate.method_ref()
    HtnStore(world.store).register_method(
        candidate,
        admit_method(
            reference,
            MethodRegistryStatus.TRIAL_ADMITTED,
            author=RegistryAuthor.SYSTEM,
            trial_scope_mission=world.mission.id,
        ),
    )
    heldout_id = "mission-heldout-" + key
    baseline_id = "mission-baseline-" + key
    oracle_hashes = {
        world.mission.id: hashlib.sha256(b"trial-oracle").hexdigest(),
        heldout_id: hashlib.sha256(b"heldout-oracle").hexdigest(),
        baseline_id: hashlib.sha256(b"baseline-oracle").hexdigest(),
    }
    service = MethodEvaluationStore(world.store)
    service.freeze(
        reference,
        EvaluationSetV1.build("code-v1", (world.mission.id,), (heldout_id,)),
        baseline_mission_ids=(baseline_id,),
        policy=MethodLifecyclePolicyV1(min_trials=1, min_heldout=1),
        oracle_hashes=oracle_hashes,
    )

    outcome = world.plan()
    assert outcome.committed, outcome.last_reason
    world.admit_demand()
    e2e._ready_for_root(world)
    resolution = e2e._offer_root(world)
    assert resolution.committed, (resolution.reason, resolution.detail)
    completed = world.service.judge_mission(
        world.mission.id,
        judgments=[{"criterion": item, "met": True} for item in world.mission.success_criteria],
        summary="real terminal H6 oracle fixture",
    )
    assert str(completed.status) == "COMPLETED"
    world.service.import_usage(
        "real-h6-call",
        world.mission.id,
        (UsageFact("real-h6-call", 80, 20, None, unknown=False),),
    )
    return world, service, reference, oracle_hashes[world.mission.id]


def test_real_terminal_nonadopting_mission_oracle_persists_and_legacy_run_stays_strict(
    tmp_path,
) -> None:
    world, service, reference, oracle_hash = _frozen_real_trial(
        tmp_path, key="h6-real-oracle"
    )
    assert all(
        instance.method_ref != reference
        for instance in HtnStore(world.store).list_method_instances(world.mission.id)
    )
    receipt = service.record_oracle(
        reference,
        world.mission.id,
        oracle_hash=oracle_hash,
        passed=True,
        evidence_sha256=hashlib.sha256(b"independent-real-oracle-evidence").hexdigest(),
    )
    # Oracle mode counts this formally accepted Mission in the denominator but
    # cannot turn a non-adopted candidate into accepted candidate evidence.
    assert receipt["passed"] is True
    assert service._run(world.mission.id, reference, require_exercised=False)["accepted"] is False
    with pytest.raises(StoreConflict, match="does not match"):
        service.record_oracle(reference, world.mission.id, oracle_hash="f" * 64,
                              passed=True, evidence_sha256="1" * 64)
    with pytest.raises(StoreConflict, match="did not exercise"):
        service._run(world.mission.id, reference)

    world.store.close()
    reopened = Store.open(tmp_path / "oracle-real.db")
    try:
        replay = MethodEvaluationStore(reopened).record_oracle(
            reference,
            world.mission.id,
            oracle_hash=oracle_hash,
            passed=True,
            evidence_sha256=hashlib.sha256(b"independent-real-oracle-evidence").hexdigest(),
        )
        assert replay == receipt
    finally:
        reopened.close()


def test_real_oracle_rejects_unknown_usage_and_post_receipt_usage_change(tmp_path) -> None:
    world, service, reference, oracle_hash = _frozen_real_trial(
        tmp_path / "unknown", key="h6-real-unknown"
    )
    world.service.import_usage(
        "unknown-h6-call",
        world.mission.id,
        (UsageFact("unknown-h6-call", 0, 0, None, unknown=True),),
    )
    with pytest.raises(StoreConflict, match="absent or unresolved"):
        service.record_oracle(
            reference,
            world.mission.id,
            oracle_hash=oracle_hash,
            passed=True,
            evidence_sha256="1" * 64,
        )
    assert service._row(reference)["state"] == "FROZEN"
    assert world.store.get_receipt(service._oracle_key(reference, world.mission.id)) is None

    changed, changed_service, changed_ref, changed_hash = _frozen_real_trial(
        tmp_path / "changed", key="h6-real-changed"
    )
    changed_service.record_oracle(
        changed_ref,
        changed.mission.id,
        oracle_hash=changed_hash,
        passed=True,
        evidence_sha256="2" * 64,
    )
    changed.service.import_usage(
        "late-known-call",
        changed.mission.id,
        (UsageFact("late-known-call", 1, 1, None, unknown=False),),
    )
    with pytest.raises(StoreConflict, match="evidence changed"):
        changed_service.record_oracle(
            changed_ref,
            changed.mission.id,
            oracle_hash=changed_hash,
            passed=True,
            evidence_sha256="2" * 64,
        )
    assert changed_service._row(changed_ref)["state"] == "FROZEN"


@pytest.mark.parametrize(
    ("failed", "expected_state", "rate"),
    ((1, "EVALUATED", 24 / 25), (4, "REJECTED", 21 / 25)),
)
def test_aggregation_only_oracle_thresholds_use_all_25_candidate_receipts(
    tmp_path, monkeypatch, failed, expected_state, rate
) -> None:
    """Aggregation-only: Mission evidence shape is synthetic and labelled as such."""

    world = e2e.build_world(tmp_path, key=f"h6-aggregate-{failed}", name="aggregate.db")
    candidate = e2e._outer(method_id=f"h6.aggregate.{failed}")
    reference = candidate.method_ref()
    HtnStore(world.store).register_method(
        candidate,
        admit_method(
            reference,
            MethodRegistryStatus.TRIAL_ADMITTED,
            author=RegistryAuthor.SYSTEM,
            trial_scope_mission=world.mission.id,
        ),
    )
    trials = tuple(f"trial-{failed}-{index}" for index in range(20))
    heldout = tuple(f"heldout-{failed}-{index}" for index in range(5))
    baselines = tuple(f"baseline-{failed}-{index}" for index in range(25))
    all_ids = (*trials, *heldout, *baselines)
    hashes = {item: hashlib.sha256(("oracle:" + item).encode()).hexdigest() for item in all_ids}
    service = MethodEvaluationStore(world.store)
    service.freeze(
        reference,
        EvaluationSetV1.build("code-v1", trials, heldout),
        baseline_mission_ids=baselines,
        oracle_hashes=hashes,
    )

    candidate_ids = set((*trials, *heldout))

    def aggregate_run(mission_id, method, *, require_exercised=True):
        return {
            "mission_id": mission_id,
            "tenant_id": "aggregation-only",
            "mission": {"fixture": "aggregation-only"},
            "accepted": True,
            "resolutions": [],
            "instances": ([{"method_ref": reference.to_json()}] if mission_id in candidate_ids else []),
            "usage": [{"usage_ref": mission_id, "input_tokens": 80, "output_tokens": 20, "unknown": 0}],
            "actions": [],
            "unresolved": 0,
            "critical": 0,
            "cost_tokens": 100,
        }

    monkeypatch.setattr(service, "_run", aggregate_run)
    with pytest.raises(StoreConflict, match="oracle is absent"):
        service.evaluate(reference)
    for mission_id in all_ids:
        service.record_oracle(reference, mission_id, oracle_hash=hashes[mission_id],
            passed=mission_id not in set((*trials, *heldout)[:failed]),
            evidence_sha256=hashlib.sha256(("evidence:" + mission_id).encode()).hexdigest())
    result = service.evaluate(reference)
    assert result["state"] == expected_state
    assert result["record"]["acceptance_rate"] == pytest.approx(rate)
    if expected_state == "REJECTED":
        with pytest.raises(StoreConflict, match="passing evaluation"):
            service.promote(reference)
