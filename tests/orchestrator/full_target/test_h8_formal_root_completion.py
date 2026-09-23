"""H8 reads original completed-root evidence after the pending review is gone."""
from dataclasses import replace
from types import SimpleNamespace

from agent_orchestrator.evaluation.experiment import ExecutionCounters, ExperimentBudget
from agent_orchestrator.evaluation.htn_matrix import EpisodeReceipt, EvidenceFile
from agent_orchestrator.planning.htn.cross_domain_acceptance import FourArm, ScenarioKind
from agent_orchestrator.evaluation.htn_hierarchical import read_formal_root_completion
from agent_orchestrator.storage.store import Store
from test_h6_oracle_bound_evaluation import _frozen_real_trial


def test_completed_root_identity_survives_cold_read_but_dirty_proof_is_refused(tmp_path):
    world, _, _, _ = _frozen_real_trial(tmp_path, key="h8-formal-root")
    before = world.store.connection.total_changes
    proof = read_formal_root_completion(world.store, world.mission.id)
    assert proof is not None
    assert proof["review_record_id"] == proof["resolution"]["review_receipt_id"]
    assert proof["resolution"]["goal_task_id"] == "task-root"
    assert world.store.connection.total_changes == before
    world.store.close()
    reopened = Store.open(tmp_path / "oracle-real.db")
    try:
        assert read_formal_root_completion(reopened, world.mission.id) == proof
        from agent_orchestrator.storage.htn_store import HtnStore
        htn = HtnStore(reopened)
        htn.mark_dirty(world.mission.id, subject_kind="resolution",
                       subject_id=proof["resolution"]["resolution_id"], scope_id="mission",
                       epoch=htn.epoch(world.mission.id, "mission"), reason="source_invalidated")
        assert read_formal_root_completion(reopened, world.mission.id) is None
    finally:
        reopened.close()


def test_hierarchical_receipt_requires_formal_root_oracle_and_settled_usage():
    """Receipt predicate only; no model Mission or file-verification claim."""
    manifest = SimpleNamespace(budget=ExperimentBudget(20, 10, 30, 3, 30), physical_slots=1)
    run = SimpleNamespace(scenario=SimpleNamespace(kind=ScenarioKind.NORMAL), arm=FourArm.H_NATIVE)
    receipt = EpisodeReceipt(
        "run", "manifest", "COMPLETED", True,
        EvidenceFile("oracle.json", "a" * 64), EvidenceFile("runtime.json", "b" * 64),
        "fixture", "model", "tools", ExecutionCounters("fixture", "model", 4, 2, 6, 1, 1),
        0, 0, 0, 1.0, "original-formal-root-review",
    )
    assert receipt.passed(manifest, run)
    assert not replace(receipt, formal_completion_id=None).passed(manifest, run)
    assert not replace(receipt, domain_success=False).passed(manifest, run)
    assert not replace(receipt, unknown_usage_calls=1).passed(manifest, run)
