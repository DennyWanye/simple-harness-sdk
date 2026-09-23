"""Cold recovery restores source-library state without resuming provider calls."""
from pathlib import Path
from test_htn_end_to_end import build_world
from agent_orchestrator.contracts.semantic_base import content_hash_of
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.evaluation.htn_method_source import SEED_SNAPSHOT, recover_source_library


def test_seed_restoration_uses_original_store_snapshot_and_is_idempotent(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    world = build_world(runtime, key="h6-source-recovery")
    reference = world.contract.method_ref()
    original = world.env.registry.registration(reference)
    snapshot = {"mission_id": world.mission.id, "manifest_hash": "a" * 64,
                "registrations": [original.to_json()]}
    with world.store.transaction():
        world.store.insert_receipt(commit_id=SEED_SNAPSHOT, kind="h6_source_seed_snapshot",
            subject_id=world.mission.id, base_version=None,
            proposal_hash=content_hash_of(snapshot), receipt=snapshot)
        HtnStore(world.store).set_method_registration(world.env.registry.suspend(
            reference, reason="H6 cold-library source experiment"))
    world.store.close()
    first = recover_source_library(tmp_path)
    assert first["state"] == "LIBRARY_RESTORED" and first["source_ready"] is False
    assert recover_source_library(tmp_path) == first
    from agent_orchestrator.storage.store import Store
    store = Store.open(runtime / "orchestrator.db")
    assert HtnStore(store).get_method(reference.method_id, reference.version).registration == original
    assert store.get_receipt("h6-source-completion-v1") is None
    store.close()
