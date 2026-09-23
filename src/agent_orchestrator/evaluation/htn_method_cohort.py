# SPDX-License-Identifier: Apache-2.0
"""H6 real Mission cohort execution in the candidate method's original Store.

The source method must already have passed the production admission chain. This
runner freezes identities before scheduling and never imports fabricated runs.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, replace
from collections import Counter
import json
from pathlib import Path
from typing import Any

from ..contracts import TERMINAL_MISSION, MissionStatus
from ..contracts.htn import MethodRef
from ..contracts.ids import mission_id
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..governance.permissions import Principal
from ..planning.htn.cross_domain_acceptance import FourArm, ScenarioKind
from ..planning.htn.method_lifecycle import EvaluationSetV1, MethodLifecyclePolicyV1
from ..runtime.model_router import RuntimeProfile
from ..storage.method_evaluation_store import MethodEvaluationStore
from ..storage.store import Store
from .htn_domains import EpisodeDomain
from .htn_executor import RuntimeEpisodeExecutor, source_fingerprint
from .htn_hierarchical import execute_hierarchical
from .htn_matrix import EvidenceFile, H8Manifest, H8MeterContext, H8Run, ScenarioDefinition
from .htn_meter import DurableMeteredProvider
from .htn_oracles import write_evidence


def validate_code_cohort(scenarios: list[dict[str, Any]]) -> None:
    """Reject unusable fixed tests before freezing IDs or spending model budget."""
    for item in scenarios:
        fixture, oracle = item["fixture"], item["oracle"]
        public = fixture.get("files", {})
        immutable = oracle.get("immutable_files", {})
        hidden = oracle.get("hidden_files", {})
        if (not public or not immutable or set(hidden) & set(public)
                or any(public.get(path) != body for path, body in immutable.items())):
            raise ContractError("H6 public/hidden oracle inputs are not isolated and pinned")
        for path, body in {**public, **hidden}.items():
            if path.endswith(".py"):
                try:
                    ast.parse(body, filename=path)
                except (SyntaxError, TypeError, ValueError) as error:
                    raise ContractError(
                        f"H6 fixture {item['idempotency_key']} has invalid Python: {path}"
                    ) from error


async def execute_method_cohort(*, executor: RuntimeEpisodeExecutor, manifest: H8Manifest,
        method: MethodRef, runtime_root: Path, evidence_root: Path,
        scenarios: list[dict[str, Any]], promote: bool = False,
        source_manifest: H8Manifest | None = None) -> dict[str, Any]:
    """Run explicit code tasks for >=20 trial, >=5 heldout and separate baselines.

    Each entry has cohort, idempotency_key, fixture, oracle. Its goal should name
    the method under evaluation for trial/heldout; actual adoption is subsequently
    checked by MethodEvaluationStore rather than assumed from that instruction.
    """
    validate_code_cohort(scenarios)
    config = replace(executor.config, evidence_root=runtime_root,
                     planning_backend=None, planning_backend_limits=None, hierarchical_repair_enabled=True)
    if not config.orchestrator_db.is_file():
        raise ContractError("H6 must use the existing candidate admission runtime")
    if source_fingerprint(executor.checkout) != manifest.source_fingerprint:
        raise ContractError("H6 source differs from frozen deployment")
    source_manifest = manifest if source_manifest is None else source_manifest
    if source_manifest.experiment_id != manifest.experiment_id:
        raise ContractError("H6 source and evaluation must belong to the same campaign")
    tenant = "h8:" + manifest.experiment_id
    cohorts: dict[str, list[str]] = {"baseline": [], "trial": [], "heldout": []}
    runs = []
    for index, item in enumerate(scenarios):
        if set(item) != {"cohort", "idempotency_key", "fixture", "oracle"} or item["cohort"] not in cohorts:
            raise ContractError("H6 requires explicit cohort, idempotency_key, fixture and oracle")
        key = item["idempotency_key"]
        if not isinstance(key, str) or not key:
            raise ContractError("H6 Mission idempotency key required")
        cohorts[item["cohort"]].append(mission_id(tenant, key))
        scenario = ScenarioDefinition(key, "code-v1", ScenarioKind.NORMAL, item["fixture"], item["oracle"])
        runs.append((item["cohort"], H8Run(key, scenario, FourArm.H_NATIVE, index + 1, manifest.seed + index)))
    ids = [value for group in cohorts.values() for value in group]
    paired: dict[str, Counter[str]] = {"baseline": Counter(), "candidate": Counter()}
    for item in scenarios:
        # The only prompt difference allowed by this cohort is the explicit
        # instruction to consider the trial method. Task and tests remain paired.
        fixture = dict(item["fixture"])
        fixture["goal"] = fixture["goal"].replace(
            " Evaluate and use the trial-admitted candidate method when it is applicable.", "")
        pair = content_hash_of([fixture, item["oracle"]])
        paired["baseline" if item["cohort"] == "baseline" else "candidate"][pair] += 1
    if (paired["baseline"] != paired["candidate"]
            or any(count != 1 for count in paired["candidate"].values())):
        raise ContractError("H6 requires one matching baseline per distinct candidate scenario")
    policy = MethodLifecyclePolicyV1()
    if (len(ids) != len(set(ids)) or not cohorts["baseline"] or len(cohorts["trial"]) < policy.min_trials
            or len(cohorts["heldout"]) < policy.min_heldout):
        raise ContractError("H6 requires disjoint >=20 trial, >=5 heldout and nonempty baseline")
    frozen = {"manifest": manifest.to_json(), "source_manifest": source_manifest.to_json(), "method": method.to_json(), "scenarios": scenarios,
              "runtime_root": str(runtime_root.resolve()), "cohorts": cohorts}
    write_evidence(evidence_root, "cohort.json", frozen)
    store = Store.open(config.orchestrator_db)
    try:
        registered = MethodEvaluationStore(store).htn.get_method(method.method_id, method.version)
        if registered.contract.method_ref() != method:
            raise ContractError("H6 candidate content differs from its original admission")
        scope = registered.registration.trial_scope_mission
        source = None if scope is None else store.get_mission(str(scope))
        if (source is None or source.tenant_id != tenant or source.status is not MissionStatus.COMPLETED
                or str(scope) in ids):
            raise ContractError("H6 candidate requires a completed source Mission outside all cohorts in this tenant")
        original = store.get_receipt("h6-source-completion-v1")
        source_root = runtime_root.parent
        source_path = source_root / "source-receipt.json"
        if (original is None or not source_path.is_file()
                or json.loads(source_path.read_text()) != original
                or original.get("state") != "CANDIDATES_AVAILABLE"
                or original.get("manifest_hash") != source_manifest.fingerprint
                or original.get("source_mission_id") != source.id
                or method.to_json() not in original.get("candidates", [])
                or original.get("domain_success") is not True
                or original.get("unknown_usage_calls") != 0):
            raise ContractError("H6 original candidate source evidence is unavailable or failed")
        EvidenceFile("physical-meter.json", original["meter_sha256"]).verify(source_root)
        EvidenceFile(original["oracle"]["path"], original["oracle"]["sha256"]).verify(source_root)
        # The source run and this comparative cohort have separate frozen code
        # identities. Resolve the source run from its original Mission identity,
        # then verify the already hash-pinned meter against that original manifest.
        source_runs = [run for run in source_manifest.runs()
                       if mission_id(tenant, run.run_id) == source.id]
        if len(source_runs) != 1:
            raise ContractError("H6 source Mission is not in its original manifest")
        source_run = source_runs[0]
        meter = json.loads((source_root / "physical-meter.json").read_text())
        if meter.get("sha256") != content_hash_of(meter.get("state")):
            raise ContractError("H6 source meter state hash differs")
        expected_meter = {
            "budget": asdict(source_manifest.budget),
            "provider": source_manifest.provider, "model": source_manifest.model,
            "physical_slots": source_manifest.physical_slots,
            "run_identity": content_hash_of([
                source_manifest.fingerprint, source_run.run_id, "H6-source-v1"]),
        }
        if meter["state"].get("binding") != expected_meter:
            raise ContractError("H6 source meter deployment differs")
        snapshot = store.get_receipt("h6-source-seed-snapshot-v1")
        if (snapshot is None or snapshot.get("mission_id") != source.id
                or snapshot.get("manifest_hash") != source_manifest.fingerprint):
            raise ContractError("H6 source registry snapshot differs")
        write_evidence(evidence_root, "source-manifest.json", source_manifest.to_json())
        MethodEvaluationStore(store).freeze(method,
            EvaluationSetV1.build("code-v1", cohorts["trial"], cohorts["heldout"]),
            baseline_mission_ids=tuple(cohorts["baseline"]), policy=policy,
            oracle_hashes={mission_id(tenant, run.run_id): content_hash_of(dict(run.scenario.oracle))
                           for _, run in runs})
    finally:
        store.close()
    principal = Principal("h6-frozen-cohort-issuer")
    for cohort, run in sorted(runs, key=lambda entry: entry[0] != "baseline"):
        # Hash arbitrary idempotency keys into paths rather than treating them as paths.
        run_root = evidence_root / content_hash_of([run.run_id])[:24]
        receipt_path = run_root / "receipt.json"
        store = Store.open(config.orchestrator_db)
        try:
            expected_id = mission_id(tenant, run.run_id)
            existing = store.get_mission(expected_id)
            if any(m.status not in TERMINAL_MISSION and m.id != expected_id for m in store.list_missions()):
                raise ContractError("H6 cannot dispatch while another Mission is active in the shared runtime")
            if receipt_path.exists():
                envelope = json.loads(receipt_path.read_text())
                previous = envelope["body"]
                if (envelope["sha256"] != content_hash_of(previous)
                        or previous["run_id"] != run.run_id or previous["cohort"] != cohort
                        or previous["cohort_hash"] != content_hash_of(frozen)
                        or previous["status"] != "RECORDED"
                        or type(previous["oracle_passed"]) is not bool
                        or existing is None or existing.status not in TERMINAL_MISSION):
                    raise ContractError("H6 recorded receipt identity differs")
                EvidenceFile(previous["oracle"]["path"], previous["oracle"]["sha256"]).verify(run_root)
                oracle_body = json.loads((run_root / previous["oracle"]["path"]).read_text())
                if (oracle_body.get("passed") is not previous["oracle_passed"]
                        or oracle_body.get("oracle_hash") != content_hash_of(dict(run.scenario.oracle))):
                    raise ContractError("H6 independent oracle differs from the recorded scenario verdict")
                EvidenceFile("physical-meter.json", previous["meter_sha256"]).verify(run_root)
                service = MethodEvaluationStore(store)
                original_outcome = store.get_receipt(service._oracle_key(method, expected_id))
                if original_outcome is None or original_outcome != previous["store_outcome"]:
                    raise ContractError("H6 original Store lost its immutable oracle outcome")
                service.record_oracle(method, expected_id,
                    oracle_hash=content_hash_of(dict(run.scenario.oracle)),
                    passed=previous["oracle_passed"], evidence_sha256=previous["oracle"]["sha256"])
                continue
            if existing is not None:
                if existing.status in TERMINAL_MISSION:
                    raise ContractError("terminal H6 Mission lost its receipt; explicit receipt recovery required")
                if not (run_root / "physical-meter.json").is_file():
                    raise ContractError("H6 active Mission lost its original physical accounting")
        finally:
            store.close()
        checkpoint = run_root / "physical-meter.json"
        context = H8MeterContext(manifest, run, lambda counts: None)
        meter = DurableMeteredProvider(executor.provider, context,
            estimate_input_tokens=executor.estimate, extra_input_reserve=executor.extra,
            checkpoint=checkpoint, run_identity=content_hash_of([frozen, run.run_id]), resume=checkpoint.exists())
        domain = EpisodeDomain(manifest, run, run_root, config,
                               large_context=executor.context_policy is not None, resume=(run_root / "source-repository").exists())
        try:
            profile = RuntimeProfile("default", meter, manifest.model, provider_kind="real",
                tokenizer=executor.tokenizer, context_policy=executor.context_policy,
                max_concurrent_model_calls=manifest.physical_slots,
                default_max_output_tokens=config.default_max_output_tokens,
                max_output_tokens_ceiling=config.max_output_tokens_ceiling)
            result = await execute_hierarchical(manifest=manifest, run=run, config=config,
                provider=meter, root=runtime_root, seed=domain.seed, allowed_tools=domain.tools,
                success_criteria=domain.criteria, principal=principal, resume=True, runtime_profile=profile,
                prepare=lambda loop, mission: domain.prepare(loop, mission, principal=principal))
            workspace = domain.accepted_workspace(result)
            passed, oracle = await domain.grade(workspace=workspace, mission_id=result.mission_id)
            from hashlib import sha256
            if meter.unknown_usage_calls:
                from .metered_provider import UnknownProviderUsage
                raise UnknownProviderUsage("H6 unknown physical usage cannot enter an evaluation")
            # The production reader requires a terminal Mission, settled usage,
            # and actual candidate execution. Honest failures remain in the
            # frozen denominator; external oracle failure can never be hidden
            # behind an internally COMPLETED Mission.
            store = Store.open(config.orchestrator_db)
            try:
                outcome = MethodEvaluationStore(store).record_oracle(method, result.mission_id,
                    oracle_hash=content_hash_of(dict(run.scenario.oracle)),
                    passed=passed, evidence_sha256=oracle.sha256)
            finally:
                store.close()
            body = {"run_id": run.run_id, "cohort": cohort, "cohort_hash": content_hash_of(frozen),
                "status": "RECORDED", "runtime": dict(result.receipt), "store_outcome": outcome,
                "oracle_passed": passed, "oracle": {"path": oracle.relative_path, "sha256": oracle.sha256},
                "meter_sha256": sha256(checkpoint.read_bytes()).hexdigest()}
            write_evidence(run_root, "receipt.json", {"body": body, "sha256": content_hash_of(body)})
            print({"cohort": cohort, "mission_id": result.mission_id, "status": result.terminal_status,
                   "oracle_passed": passed}, flush=True)

        finally:
            domain.close()
    store = Store.open(config.orchestrator_db)
    try:
        service = MethodEvaluationStore(store)
        evaluation = service.evaluate(method)
        if promote and evaluation["state"] == "EVALUATED":
            service.promote(method)
            evaluation = {**evaluation, "state": "ADMITTED"}
        write_evidence(evidence_root, "evaluation.json", evaluation)
        return evaluation
    finally:
        store.close()
