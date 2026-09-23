# SPDX-License-Identifier: Apache-2.0
"""Execute an H8 hierarchical arm through actual Mission, Planner and Commit APIs."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ..api.planning_authorization import PlanningAuthorizationApi
from ..contracts import Budget
from ..contracts.htn import ContractRevision, ObligationId, TaskForm, TaskRef, TaskSemanticBindingV1
from ..contracts.models import ContractError
from ..contracts.obligations import Obligation
from ..contracts.semantic_base import VersionedRef, content_hash_of
from ..governance.permissions import Principal
from ..orchestrator.commit_service import MissionSpec
from ..orchestrator.event_handler import Orchestrator
from ..orchestrator.plan_commits import HIERARCHICAL_SEMANTICS
from ..planning.htn.cross_domain_acceptance import FourArm
from ..runtime.assembly import OrchestratorConfig
from ..runtime.model_router import RuntimeProfile
from ..runtime.planning_operations import StoreOperationReader, build_operation_snapshot
from ..storage.htn_store import HtnStore
from ..storage.obligation_store import ObligationStore
from ..storage.planning_admission_store import PlanningAdmissionStore
from ..storage.planning_decision_store import PlanningDecisionStore
from ..storage.store import Store
from .htn_matrix import H8Manifest, H8Run
from .htn_meter import DurableMeteredProvider


def read_formal_root_completion(store: Store, mission_id: str) -> dict[str, Any] | None:
    """Read the original completed root proof, including after review is resolved.

    RootReviewState deliberately drops the pending review on ALREADY_RESOLVED.
    That UI/scheduling state is not the durable completion identity. Resolve the
    exact active root and recheck its official record and current completion.
    """
    from ..orchestrator.completion_status import read_occurrence_completion
    from ..orchestrator.scoped_content_review import uses_completion_protocol

    with store.read_view():
        mission = store.get_mission(mission_id)
        if mission is None or str(mission.status) != "COMPLETED":
            return None
        htn = HtnStore(store)
        active = htn.active_plan_revision(mission_id)
        requirements = htn.latest_requirements_revision(mission_id)
        if active is None or requirements is None:
            return None
        children = {str(child.occurrence_id)
                    for instance in htn.list_method_instances(mission_id, state="ADOPTED")
                    for child in instance.child_bindings}
        roots = [item for item in htn.list_plan_memberships(mission_id, active.revision)
                 if str(item.occurrence_id) not in children]
        if len(roots) != 1:
            return None
        root = roots[0]
        binding = htn.task_semantics_of(mission_id, str(root.task_id))
        if binding is None:
            return None
        candidates = [item for item in htn.list_goal_resolutions(mission_id)
                      if item.goal_task_id == str(root.task_id)
                      and item.obligation_id == str(binding.obligation_id)
                      and item.contract_revision == int(binding.contract_revision)
                      and item.requirements_version == requirements.revision
                      and str(item.validity) == "CURRENT" and str(item.verdict) == "ACCEPT"]
        if len(candidates) != 1:
            return None
        resolution = candidates[0]
        if any(item.subject_id in {str(root.task_id), str(resolution.resolution_id)}
               for item in htn.list_dirty(mission_id)):
            return None
        stored = htn.get_review_record(resolution.review_receipt_id)
        record = stored.record
        package = htn.get_review_package(str(record.package_id))
        if (not stored.official or htn.official_review_record(str(record.package_id)) != record
                or str(record.verdict) != "ACCEPT" or str(record.purpose) != "MISSION_FINAL"
                or package.purpose != record.purpose
                or package.binding.mission_id != mission_id
                or package.binding.subject_ref.id != str(root.task_id)
                or package.binding.obligation_id != str(binding.obligation_id)
                or package.binding.requirements_revision != requirements.revision
                or package.requirements_content_hash != requirements.content_hash()):
            return None
        scope = None
        if uses_completion_protocol(store, mission_id):
            completion = read_occurrence_completion(store, mission_id, str(root.occurrence_id))
            if not completion.complete:
                return None
            scope = completion.scope.to_json()
        return {"kind": "root-goal-resolution", "resolution": resolution.to_json(),
                "review_record_id": str(record.record_id), "completion_scope": scope}


@dataclass(frozen=True, slots=True)
class HierarchicalExecution:
    mission_id: str
    terminal_status: str
    receipt: Mapping[str, Any]
    accepted_artifacts: tuple[Mapping[str, Any], ...]
    formal_completion_id: str | None
    unresolved_operations: int
    critical_effect_failures: int
    solver_statuses: tuple[str, ...]


def prepare_root(
    loop: Orchestrator,
    mission: Any,
    world: Any,
    *,
    task_type: VersionedRef,
    parameters: Mapping[str, Any],
    principal: Principal,
    recursion_fuel: int,
    completion_contract: Mapping[str, Any],
) -> None:
    """Freeze the supplied domain contract; a restart never writes a new root."""
    if type(recursion_fuel) is not int or recursion_fuel < 1 or world.mission_id != mission.id:
        raise ContractError("root requires a mission-bound world and positive recursion fuel")
    definition = world.catalog.require(task_type)
    if definition.form != TaskForm.COMPOUND:
        raise ContractError("hierarchical episode root must be a declared compound")
    if not world.schemas.require(definition.parameter_schema_ref).check(parameters).ok:
        raise ContractError("episode parameters do not satisfy the installed root schema")
    task_id, duty_id = "h8-root-" + mission.id, "h8-duty-" + mission.id
    binding = TaskSemanticBindingV1(
        task_id=TaskRef(task_id),
        obligation_id=ObligationId(duty_id),
        contract_revision=ContractRevision(1),
        contract_hash=content_hash_of(
            {"task_type": definition.to_json(), "parameters": dict(parameters)}
        ),
        form=definition.form,
        goal_signature=definition.goal_signature,
        typed_parameters=dict(parameters),
        requirement_refs=tuple(definition.goal_signature.coverage_criteria),
        semantic_scope="mission",
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
    )
    from ..api.operation_completion import OperationCompletionApi
    from ..contracts.semantic_base import TypedRef
    from ..orchestrator.operation_completion import OperationCompletionReader
    from ..orchestrator.root_review import root_requirements

    requirements = root_requirements(mission.id, binding, revision=1)
    pin = {
        "id": str(requirements.revision_id),
        "revision": requirements.revision,
        "content_hash": requirements.content_hash(),
    }
    if set(completion_contract) != {"mode", "content_criterion_ids", "effects"}:
        raise ContractError("H8 requires an explicitly frozen completion contract")
    content_ids = completion_contract["content_criterion_ids"]
    effects = []
    for effect in completion_contract["effects"]:
        if not isinstance(effect, Mapping) or effect.get("obligation_id") != "$ROOT_OBLIGATION":
            raise ContractError("H8 effect contract must explicitly bind its root obligation")
        effects.append({**effect, "obligation_id": duty_id})
    if content_ids == "ALL_ROOT_CONTENT":
        if completion_contract["mode"] != "CONTENT_ONLY" or completion_contract["effects"]:
            raise ContractError("all-root-content cannot conceal required effects")
        content_ids = list(requirements.required_criterion_ids())
    proposal = {
        "schema_version": 1,
        "mission_id": mission.id,
        "requirements_ref": pin,
        "mode": completion_contract["mode"],
        "content_criterion_ids": content_ids,
        "effects": effects,
    }
    htn = HtnStore(loop.store)
    with loop.store.transaction():
        original = htn.latest_task_semantics(task_id)
        if original is not None:
            frozen = htn.get_task_semantics(task_id, 1)
            if frozen.to_json() != binding.to_json():
                raise ContractError("episode root identity changed during recovery")
            approved = OperationCompletionReader(loop.store).read_requirements(
                mission.id, TypedRef.from_json({"kind": "requirements", **pin})
            )
            if approved.to_json() != proposal:
                raise ContractError("episode completion contract changed during recovery")
            return
        ObligationStore(loop.store).register(
            Obligation(
                obligation_id=ObligationId(duty_id),
                mission_id=mission.id,
                requirement_refs=binding.requirement_refs,
                goal_signature_id=definition.goal_signature.signature_id,
            ),
            recursion_fuel=recursion_fuel,
        )
        loop.commit.admit_obligation_demand(
            mission.id,
            ObligationId(duty_id),
            principal=principal.principal_id,
            requester={"kind": "mission_root"},
            evidence={"mission_id": mission.id, "requirement_refs": list(binding.requirement_refs)},
        )
        htn.put_task_semantics(mission.id, binding)
        htn.insert_requirements_revision(requirements)
        # Explicit experiment issuer using the authenticated production API.
        OperationCompletionApi(
            loop.commit, tenant_id=mission.tenant_id, principal=principal
        ).approve(
            {
                "mission_id": mission.id,
                "command_id": "h8-completion:" + mission.id,
                "expected_requirements_ref": {"kind": "requirements", **pin},
                "proposal": proposal,
            }
        )
        # Leave CREATED intact: the original _start_planning transaction opens
        # both PLANNING and its first durable Planner intent.


async def execute_hierarchical(
    *,
    manifest: H8Manifest,
    run: H8Run,
    config: OrchestratorConfig,
    provider: DurableMeteredProvider,
    root: Path,
    seed: Mapping[str, str],
    allowed_tools: tuple[str, ...],
    success_criteria: tuple[str, ...],
    principal: Principal,
    prepare: Callable[[Orchestrator, Any], None],
    on_cycle: Callable[[Orchestrator, Any], Awaitable[None]] | None = None,
    resume: bool = False,
    runtime_profile: RuntimeProfile | None = None,
    connectors: Mapping[str, Any] | None = None,
    operation_profiles: Any = None,
) -> HierarchicalExecution:
    if run.arm == FourArm.STRONG_SINGLE_AGENT:
        raise ContractError("hierarchical executor cannot stand in for a single Agent")
    if (
        config.model != manifest.model
        or config.max_concurrent_model_calls != manifest.physical_slots
    ):
        raise ContractError("hierarchical deployment differs from frozen model/concurrency")
    if config.hierarchical_repair_enabled != (run.arm != FourArm.H_NATIVE_NO_REPAIR):
        raise ContractError("no-repair ablation must be frozen before Mission creation")
    if (config.planning_backend is not None) != (run.arm == FourArm.H_SOLVER):
        raise ContractError("solver arm must use its actual planning backend")
    if config.planning_backend is not None and (
        config.planning_backend.backend_id != manifest.solver_identity["backend_id"]
        or config.planning_backend_limits is None
        or config.planning_backend_limits.to_json() != manifest.solver_identity["limits"]
    ):
        raise ContractError("solver deployment differs from frozen manifest")
    config = replace(config, evidence_root=root)
    if config.orchestrator_db.exists() and not resume:
        raise ContractError("an existing Mission runtime requires explicit recovery")
    spec = MissionSpec(
        goal=str(run.scenario.fixture["goal"]),
        success_criteria=success_criteria,
        tenant_id="h8:" + manifest.experiment_id,
        idempotency_key=run.run_id,
        allowed_tools=allowed_tools,
        workspace_seed=dict(seed),
        domain=run.scenario.domain,
        budget=Budget(
            max_tokens=manifest.budget.total_tokens,
            max_attempts=manifest.budget.calls,
            max_tool_calls=config.max_tool_calls_per_turn * manifest.budget.calls,
        ),
        orchestration_semantics_version=HIERARCHICAL_SEMANTICS,
        planning_protocol_version="planning-decision-v1",
    )
    if runtime_profile is not None and (
        runtime_profile.provider is not provider
        or runtime_profile.model != manifest.model
        or runtime_profile.profile_id != "default"
    ):
        raise ContractError("all hierarchical roles must use the one metered provider/profile")
    async with Orchestrator(
        config,
        provider,
        critic_wait_seconds=min(manifest.budget.seconds, config.turn_deadline_seconds),
        profiles=None if runtime_profile is None else {"default": runtime_profile},
        connectors=connectors,
        operation_profiles=operation_profiles,
    ) as loop:
        mission = await loop.submit_mission(spec)
        prepare(loop, mission)
        api = PlanningAuthorizationApi(
            loop.commit, tenant_id=mission.tenant_id, principal=principal
        )
        decisions, authorities = (
            PlanningDecisionStore(loop.store),
            PlanningAdmissionStore(loop.store),
        )
        timed_out = False
        try:
            # The one physical meter's original deadline survives recovery. Reopening
            # the Mission cannot buy another wall-clock or token allowance.
            async with asyncio.timeout_at(provider._deadline):
                while True:
                    if provider.unknown_usage_calls:
                        from .metered_provider import UnknownProviderUsage

                        raise UnknownProviderUsage(
                            "physical model usage is unresolved; preserve the original run"
                        )
                    current = loop.store.get_mission(mission.id)
                    assert current is not None
                    mission = current
                    if on_cycle is not None:
                        await on_cycle(loop, mission)
                    if str(mission.status) in {"COMPLETED", "FAILED", "CANCELLED"}:
                        break
                    for intent in loop.store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED"):
                        if intent.mission_id != mission.id:
                            continue
                        request = decisions.get_planning_request_for_intent(intent.intent_id)
                        if (
                            request is not None
                            and authorities.get_request_binding(request.request_id) is None
                        ):
                            api.issue(
                                mission.id,
                                command_id="h8-grant:" + request.request_id,
                                request_id=request.request_id,
                            )
                    await loop.run(max_cycles=1, until_idle=False)
                    await asyncio.sleep(0.01)
        except TimeoutError:
            timed_out = True
        current = loop.store.get_mission(mission.id)
        assert current is not None
        mission = current
        dispatch = loop._new_mode(mission)
        if dispatch is None:
            raise ContractError("hierarchical Mission lost its installed planning world")
        # Planning can fail before the first Plan exists. Record that real failure
        # instead of crashing while trying to evaluate non-existent completion scopes.
        review = (
            None
            if HtnStore(loop.store).active_plan_revision(mission.id) is None
            else loop._root_review(mission, dispatch).state(mission.id)
        )
        formal_root = read_formal_root_completion(loop.store, mission.id)
        completion = None if formal_root is None else formal_root["review_record_id"]
        operations = build_operation_snapshot(
            mission.id, reader=StoreOperationReader(loop.store), now_ms=int(loop.store.now * 1000)
        )
        from ..orchestrator.completion_support import read_completion_support

        artifacts: list[Mapping[str, Any]] = []
        artifact_ids: set[str] = set()
        active_tasks = {str(o.task_id) for o in dispatch.network(mission.id).occurrences}
        for acceptance in HtnStore(loop.store).list_acceptances(mission.id):
            if str(acceptance.task_id) not in active_tasks or str(acceptance.validity) != "CURRENT":
                continue
            if (
                read_completion_support(loop.store, mission.id, str(acceptance.acceptance_id))
                is None
            ):
                continue
            for reference in acceptance.artifact_refs:
                artifact = loop.store.get_artifact(reference.id)
                if (
                    artifact is None
                    or artifact.mission_id != mission.id
                    or artifact.task_id != str(acceptance.task_id)
                    or artifact.content_hash != reference.content_hash
                ):
                    raise ContractError("formal Acceptance artifact identity is unavailable")
                if artifact.id not in artifact_ids:
                    artifacts.append(artifact.to_json())
                    artifact_ids.add(artifact.id)
        statuses = tuple(
            str(e.payload["status"])
            for e in loop.store.iter_events(mission.id)
            if e.type == "PlanningBackendReturned"
        )
        actions = loop.store.list_actions(mission.id)
        critical = sum(
            1 for a in actions if a.get("state") == "FAILED" and int(a.get("handoffs", 0)) > 0
        )
        receipt = {
            "mission": mission.to_json(),
            "root_review": (
                {"status": "UNAVAILABLE", "reason": "no_committed_plan"}
                if review is None
                else review.to_json()
            ),
            "budget_usage": loop.store.mission_budget_usage(mission.id),
            "timed_out": timed_out,
            "planning_decisions": [
                dict(e.payload)
                for e in loop.store.iter_events(mission.id)
                if e.type == "PlanningDecisionEvaluated"
            ],
            "operations": asdict(operations),
            "solver_statuses": statuses,
            "formal_root_completion": formal_root,
        }
        import json

        receipt = json.loads(json.dumps(receipt, allow_nan=False))
        return HierarchicalExecution(
            mission.id,
            "TIMEOUT" if timed_out else str(mission.status),
            receipt,
            tuple(artifacts),
            completion,
            len(operations.unresolved),
            critical,
            statuses,
        )
