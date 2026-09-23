# SPDX-License-Identifier: Apache-2.0
"""H6 cold-library source Mission and export of actual admitted candidates."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contracts import TERMINAL_MISSION, MissionStatus
from ..contracts.htn import MethodRegistryStatus, MethodRegistration
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..governance.permissions import Principal
from ..planning.htn.cross_domain_acceptance import FourArm
from ..runtime.model_router import RuntimeProfile
from ..storage.htn_store import HtnStore
from ..storage.store import Store
from .htn_domains import EpisodeDomain
from .htn_executor import RuntimeEpisodeExecutor, source_fingerprint
from .htn_hierarchical import execute_hierarchical
from .htn_matrix import H8Manifest, H8MeterContext, H8Run
from .htn_meter import DurableMeteredProvider
from .htn_oracles import write_evidence


SEED_SNAPSHOT = "h6-source-seed-snapshot-v1"


def recover_source_library(root: Path) -> dict[str, Any]:
    """Restore the exact original seed registrations from the Store transaction.

    This repairs library state after an exception or process death. It does not
    resume model calls, reset accounting or turn an incomplete source into a pass.
    """
    database = root / "runtime" / "orchestrator.db"
    if not database.is_file():
        raise ContractError("H6 recovery requires the original runtime")
    store = Store.open(database)
    try:
        snapshot = store.get_receipt(SEED_SNAPSHOT)
        if snapshot is None:
            raise ContractError("H6 original seed snapshot is unavailable")
        mission = store.get_mission(snapshot["mission_id"])
        if mission is None:
            raise ContractError("H6 original source Mission is unavailable")
        htn = HtnStore(store)
        with store.transaction():
            for raw in snapshot["registrations"]:
                original = MethodRegistration.from_json(raw)
                current = htn.get_method(original.method_ref.method_id, original.method_ref.version)
                if current.registration == original:
                    continue
                if (current.contract.method_ref() != original.method_ref
                        or current.registration.status is not MethodRegistryStatus.SUSPENDED):
                    raise ContractError("H6 seed registration changed outside the source experiment")
                htn.set_method_registration(original)
        return {"state": "LIBRARY_RESTORED", "mission_id": mission.id,
                "mission_status": str(mission.status), "source_ready": False}
    finally:
        store.close()


async def execute_method_source(*, executor: RuntimeEpisodeExecutor, manifest: H8Manifest,
                                run: H8Run, root: Path) -> dict[str, Any]:
    """Temporarily suspend seed methods in a fresh experiment Store.

    Task types/operators stay installed. The original synthesizer, admission,
    planner, Commit, Worker and review paths must create the candidate. A method
    ref is exported only by reading its original trial registration in this Store.
    This cold-library source episode is not a normal H8 arm measurement.
    """
    if run.scenario.domain != "code-v1" or run.arm != FourArm.H_NATIVE:
        raise ContractError("H6 candidate source requires a code H-native scenario")
    if source_fingerprint(executor.checkout) != manifest.source_fingerprint:
        raise ContractError("H6 source differs from frozen deployment")
    runtime = root / "runtime"
    config = replace(executor.config, evidence_root=runtime, planning_backend=None,
                     planning_backend_limits=None, hierarchical_repair_enabled=True)
    if config.orchestrator_db.exists() or (root / "physical-meter.json").exists():
        raise ContractError("H6 source requires a fresh runtime; preserve prior evidence")
    write_evidence(root, "source-experiment.json", {
        "kind": "H6-cold-library-source-v1", "manifest_hash": manifest.fingerprint,
        "run_id": run.run_id, "fixture": dict(run.scenario.fixture),
        "oracle_hash": content_hash_of(dict(run.scenario.oracle)),
        "policy": "suspend-installed-seeds-for-source-only; restore-on-terminal"})
    meter = DurableMeteredProvider(executor.provider, H8MeterContext(manifest, run, lambda counts: None),
        estimate_input_tokens=executor.estimate, extra_input_reserve=executor.extra,
        checkpoint=root / "physical-meter.json",
        run_identity=content_hash_of([manifest.fingerprint, run.run_id, "H6-source-v1"]))
    domain = EpisodeDomain(manifest, run, root, config,
                           large_context=executor.context_policy is not None)
    original = []
    snapshot_written = False
    principal = Principal("h6-frozen-source-issuer")

    def prepare(loop, mission):
        nonlocal snapshot_written
        domain.prepare(loop, mission, principal=principal)
        world = loop._new_mode(mission)._world()
        htn = HtnStore(loop.store)
        with loop.store.transaction():
            for reference in world.registry.method_refs():
                registration = world.registry.registration(reference)
                if registration is None:
                    raise ContractError("installed source method lacks registration")
                original.append(registration)
                htn.set_method_registration(world.registry.suspend(reference,
                    reason="H6 cold-library source experiment"))
            snapshot = {"mission_id": mission.id, "manifest_hash": manifest.fingerprint,
                        "registrations": [r.to_json() for r in original]}
            loop.store.insert_receipt(commit_id=SEED_SNAPSHOT, kind="h6_source_seed_snapshot",
                subject_id=mission.id, base_version=None, proposal_hash=content_hash_of(snapshot), receipt=snapshot)
        snapshot_written = True
        write_evidence(root, "source-seed-registrations.json", [r.to_json() for r in original])

    async def check_source_failure(loop, mission):
        for event in loop.store.iter_events(mission.id):
            if (event.type == "PlanningRejected"
                    and event.payload.get("detail", {}).get("internal_contract_error")):
                raise ContractError("H6 source stopped on an internal planning contract failure")

    try:
        profile = RuntimeProfile("default", meter, manifest.model, provider_kind="real",
            tokenizer=executor.tokenizer, context_policy=executor.context_policy,
            max_concurrent_model_calls=manifest.physical_slots,
            default_max_output_tokens=config.default_max_output_tokens,
            max_output_tokens_ceiling=config.max_output_tokens_ceiling)
        result = await execute_hierarchical(manifest=manifest, run=run, config=config,
            provider=meter, root=runtime, seed=domain.seed, allowed_tools=domain.tools,
            success_criteria=domain.criteria, principal=principal, prepare=prepare,
            on_cycle=check_source_failure, runtime_profile=profile)
        workspace = domain.accepted_workspace(result)
        passed, oracle = await domain.grade(workspace=workspace, mission_id=result.mission_id)
        candidates = []
        store = Store.open(config.orchestrator_db)
        try:
            mission = store.get_mission(result.mission_id)
            if mission is None or mission.status not in TERMINAL_MISSION:
                raise ContractError("H6 source must reach an original terminal state")
            htn = HtnStore(store)
            # Restore only the exact registrations captured before this experiment;
            # never promote or rewrite the model-created candidate's admission.
            with store.transaction():
                for registration in original:
                    htn.set_method_registration(registration)
            for row in htn.list_methods(status=MethodRegistryStatus.TRIAL_ADMITTED):
                if (row.registration.trial_scope_mission == result.mission_id
                        and row.contract.method_ref() not in {r.method_ref for r in original}):
                    reference = row.contract.method_ref()
                    candidates.append(reference.to_json())
                    write_evidence(root, "candidates/" + content_hash_of(reference.to_json()) + ".json",
                                   reference.to_json())
        finally:
            store.close()
        ready = (bool(candidates) and mission.status is MissionStatus.COMPLETED
                 and passed and not meter.unknown_usage_calls)
        from hashlib import sha256
        body = {"state": "CANDIDATES_AVAILABLE" if ready else "SOURCE_NOT_READY",
                "manifest_hash": manifest.fingerprint,
                "meter_sha256": sha256((root / "physical-meter.json").read_bytes()).hexdigest(),
                "runtime_root": str(runtime.resolve()), "candidates": candidates,
                "source_mission_id": result.mission_id, "terminal_status": result.terminal_status,
                "domain_success": passed, "unknown_usage_calls": meter.unknown_usage_calls,
                "oracle": {"path": oracle.relative_path, "sha256": oracle.sha256},
                "runtime": dict(result.receipt)}
        store = Store.open(config.orchestrator_db)
        try:
            store.insert_receipt(commit_id="h6-source-completion-v1", kind="h6_source_completion",
                subject_id=result.mission_id, base_version=None,
                proposal_hash=content_hash_of(body), receipt=body)
        finally:
            store.close()
        write_evidence(root, "source-receipt.json", body)
        return body
    finally:
        try:
            if snapshot_written:
                recover_source_library(root)
        finally:
            domain.close()
