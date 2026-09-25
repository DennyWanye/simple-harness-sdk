# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""H2 production views: read system records before sealing a Planner request."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..contracts.htn import MethodRef
from ..contracts.semantic_base import content_hash_of
from ..planning.htn.planner_package import _sorted_unique_refs, _network_authorities, _merge_authorities
from ..planning.htn.planner_package_v1 import (
    MAX_PACKAGE_BYTES, AcceptedResultView, CapabilityView, FactView, GoalView, FailureView,
    ObligationView, PlanningBudgetView, PlannerPackageAssemblerV1,
    PlannerPackageCodecError, collect_planner_views,
)
from ..storage.obligation_store import ObligationStore
from .completion_support import read_completion_support

# Integer 6 / prompt 9 is a new pair; historical integer 4/5 requests stay frozen.
FORMAL_PACKAGE_LABEL = "planner-package-hierarchical-v7"


def assemble_runtime_views(*, store: Any, mission: Any, network: Any, world: Any,
                           htn: Any, package: dict[str, Any], authorities: list[dict[str, Any]],
                           budget: PlanningBudgetView, selection_reports: Any = (), dispatch: Any = None,
                           selection_policy: str = "MODEL_ON_MULTIPLE") -> dict[str, Any]:
    # Select visible methods from the complete applicability read. A registry's
    # lexicographic first twelve must not hide the sole applicable method.
    from ..planning.htn.planner_package import method_library, _methods_for
    choices: list[dict[str, Any]] = []
    if dispatch is not None:
        from .planning_selection import selection_context
        choices = selection_context(dispatch, mission.id, selection_reports, policy=selection_policy)
    signatures = {row["goal_signature_id"] for row in package["method_library"]}
    priority = {c["method_id"] for choice in choices for c in choice["applicable"]}
    selected_ids = {choice["selected_method_id"] for choice in choices if choice["selected_method_id"]}
    library: list[dict[str, Any]] = []
    original = {row["method_id"]: row for row in package["method_library"]}
    for signature in sorted(signatures):
        all_rows = method_library(world.registry, (signature,),
            limit=len(_methods_for(world.registry, signature)), mission_id=mission.id)
        ordered = sorted(all_rows, key=lambda row: (row["method_id"] not in selected_ids,
                                                   row["method_id"] not in priority, row["method_id"]))
        library.extend(original.get(row["method_id"], row) for row in ordered[:12])
    package = {**package, "method_library": library}
    views = collect_planner_views(package)
    subjects = {row["occurrence_id"]: row for row in package["planning_subjects"]}
    goals = []
    for occurrence in network.occurrences:
        subject = subjects.get(str(occurrence.occurrence_id))
        if subject is None:
            continue
        binding = network.binding_for_occurrence(occurrence.occurrence_id)
        goals.append(GoalView(
            subject_key=subject["subject_key"], occurrence_id=str(occurrence.occurrence_id),
            task_id=str(occurrence.task_id), obligation_id=str(occurrence.obligation_id),
            signature=binding.goal_signature.to_json(), statement=binding.goal_signature.statement,
            params=dict(binding.typed_parameters), requirement_refs=tuple(binding.requirement_refs),
            contract_revision=binding.contract_revision, requiredness=str(occurrence.requiredness),
        ))
    views["goals"] = tuple(goals)
    duties = ObligationStore(store)
    obligations = []
    for duty in duties.list_obligations(mission.id):
        account = duties.account(mission.id, duty.obligation_id)
        relations = duties.list_relations(mission.id, child=duty.obligation_id)
        obligations.append(ObligationView(
            parent=None if duty.parent_obligation_id is None else str(duty.parent_obligation_id),
            relation=",".join(sorted({str(row.kind) for row in relations})) or "ROOT",
            demand={"obligation_id": str(duty.obligation_id), "admitted": account.has_admitted_demand,
                    "lifecycle": str(account.lifecycle), "requiredness": str(duty.requiredness)},
            fuel={"limit": account.fuel_limit, "used": account.fuel_used, "remaining": account.remaining_fuel},
            failures=({"count": account.failure_count},), attempts=account.consumed_attempts,
            budget={"lineage_ref": duty.budget_lineage_ref, "spent_tokens": account.consumed_tokens,
                    "spent_cost_micros": account.consumed_cost_micros},
        ))
    views["obligations"] = tuple(obligations)
    views["plans"] = (replace(views["plans"][0],
        adopted_methods=tuple(i.to_json() for i in network.method_instances
                              if i.instance_id in network.adopted_instance_ids),
        order_summary={"constraints": [i.to_json() for i in network.order_constraints]},
        data_summary={"requirements": [i.to_json() for i in network.data_requirements]}),)
    methods = []
    for view, row in zip(views["methods"], package["method_library"], strict=True):
        ref = MethodRef.from_json(row["method_ref"])
        contract = world.registry.definition(ref)
        if contract is None:
            raise PlannerPackageCodecError("method disappeared while assembling views")
        from ..planning.htn.planner_package import applicability_reports
        reports = tuple(r for r in applicability_reports(selection_reports, limit=len(selection_reports))
                        if r.get("method_ref") == row["method_ref"])
        schema = world.schemas.resolve(contract.parameter_schema_ref)
        if schema is None:
            raise PlannerPackageCodecError("method parameter schema is unavailable")
        methods.append(replace(view, applicability={"reports": reports},
            precondition_summary={"applicable_when": [p.to_json() for p in contract.applicable_when]},
            schema=schema.to_json()))
    views["methods"] = tuple(methods)
    snapshot = world.snapshot()
    observations = {o.observation_id: o for o in htn.list_observations(mission.id)}
    facts = []
    for row in package["facts"]:
        observation = observations[row["read_set_entry"]["id"]]
        entry = snapshot.lookup(observation.proposition_key)
        if entry is None:
            raise PlannerPackageCodecError("observation is missing from the evidence snapshot")
        facts.append(FactView(
            observation_ref=dict(row["read_set_entry"]), proposition_key=observation.proposition_key,
            polarity=observation.polarity, availability=str(entry.availability),
            truth=str(entry.truth(now_ms=int(store.now * 1000))), coverage=str(observation.coverage),
            observer=observation.observer_id, times={
                "observed_at_ms": observation.observed_at_ms, "recorded_at_ms": observation.recorded_at_ms,
                "valid_from_ms": observation.valid_from_ms, "valid_until_ms": observation.valid_until_ms,
                "query_watermark_ms": observation.query_watermark_ms},
        ))
    views["facts"] = tuple(facts)
    outputs = htn.list_acceptance_outputs(mission.id)
    accepted = []
    for acceptance in htn.list_acceptances(mission.id):
        support = read_completion_support(store, mission.id, str(acceptance.acceptance_id)) if str(acceptance.validity) == "CURRENT" else None
        if support is None:
            continue
        matching = [o for o in outputs if o.get("acceptance_id") == str(acceptance.acceptance_id)]
        for occurrence in network.occurrences:
            if occurrence.task_id != acceptance.task_id:
                continue
            accepted.append(AcceptedResultView(
                acceptance_ref={"kind": "acceptance", "id": str(acceptance.acceptance_id),
                    "semantic_revision": 1, "content_hash": content_hash_of(acceptance.to_json())},
                producer_occurrence=str(occurrence.occurrence_id), ports=tuple(matching),
                artifacts=tuple(a.to_json() for a in acceptance.artifact_refs),
                currentness=str(acceptance.validity), support_revision=acceptance.contract_revision,
                permitted_uses=("DATA",) if matching else (),
            ))
    views["accepted_results"] = tuple(accepted)
    views["capabilities"] = tuple(CapabilityView(
        r.capability_id, r.registered, r.configured, r.reachable, r.healthy, r.authorized, r.compatible
    ) for r in world.capabilities().records)
    failures = list(views["failures"])
    for task in store.list_tasks(mission.id):
        for attempt in store.list_attempts(task.id):
            if attempt.failure is not None or attempt.feedback:
                failures.append(FailureView(source="attempt", reason=str(attempt.status),
                    attempt_review_ref={"kind": "attempt", "id": attempt.id, "version": attempt.version,
                        "content_hash": content_hash_of(attempt.to_json())},
                    repeat_count=1, last_seen=str(attempt.created_at),
                    findings=tuple(attempt.feedback) + ((dict(attempt.failure),) if attempt.failure else ())))
    views["failures"] = tuple(reversed(failures))
    views["planning_budgets"] = (budget,)
    # Rebuild the uncapped set; the legacy collector's lexicographic cap can drop
    # the current subject or method instance. No required reference is discarded.
    duty_refs = [{"kind": "obligation", "id": str(d.obligation_id), "semantic_revision": 1,
                  "content_hash": content_hash_of(d.to_json())} for d in duties.list_obligations(mission.id)]
    from .planning_protocol_binding import planning_protocol_for_mission
    from .planning_graph_repairs import graph_repair_sources
    protocol = planning_protocol_for_mission(store, mission.id)
    h4 = protocol is not None and int(protocol["package_version"]) >= 7
    sharing_candidates = graph_repair_sources(store, network) if h4 else ()
    refs = _sorted_unique_refs(package, _merge_authorities(_network_authorities(network), [*authorities, *duty_refs]))
    if h4:
        extra_refs = [ref for row in sharing_candidates for ref in (row["task_ref"], row["resolution_ref"]) if ref is not None]
        # H4 can retry/cancel/share within an adopted method that was never
        # rejected. The legacy ref collector only exposes rejected refinements.
        extra_refs.extend(ref for ref in authorities if ref["kind"] == "method_instance")
        refs = sorted({(ref["kind"], ref["id"], ref["semantic_revision"], ref["content_hash"]): ref
                       for ref in (*refs, *extra_refs)}.values(), key=lambda ref: (ref["kind"], ref["id"]))
    formal = PlannerPackageAssemblerV1.assemble(package, visible_refs=refs, views=views)
    result = dict(package)
    from .planning_repair_requests import pending_requests
    result["repair_requests"] = pending_requests(store, mission.id)
    if dispatch is not None:
        result["method_selection"] = choices
    if (choices and choices[0]["route"] == "MODEL_REFINE"
            and not result["repair_requests"] and not result.get("rejected_refinements")):
        # One model selection spends one occurrence's frozen identity. Other
        # goals remain visible context, but cannot consume this call's authority.
        result["planning_subjects"] = [subject for subject in result["planning_subjects"]
            if subject["occurrence_id"] == choices[0]["occurrence_id"]]
    from ..contracts.planning_decisions import H4_DECISION_ENABLEMENT, V14_DECISION_ENABLEMENT
    from .planning_protocol_binding import planning_protocol_for_mission
    protocol = planning_protocol_for_mission(store, mission.id)
    h4 = protocol is not None and int(protocol["package_version"]) >= 7
    enablement = H4_DECISION_ENABLEMENT if h4 else V14_DECISION_ENABLEMENT
    if h4:
        result["active_method_instances"] = [
            {"method_instance_ref": ref,
             "child_bindings": [child.to_json() for child in instance.child_bindings]}
            for instance in network.method_instances if instance.instance_id in network.adopted_instance_ids
            for ref in authorities if ref["kind"] == "method_instance" and ref["id"] == str(instance.instance_id)
        ]
        result["sharing_candidates"] = list(sharing_candidates)
        result["data_rebind_candidates"] = [{"requirement": edge.to_json(), "expected_requirement_hash": content_hash_of(edge.to_json())}
                                             for edge in network.data_requirements]
        result["successor_types"] = [spec.to_json() for spec in world.catalog.task_types()]
        result["compensation_candidates"] = [
            {"action_key": action["action_key"], "action_hash": content_hash_of(action),
             "connector": action["connector"], "operation": action["operation"], "target": action["target"]}
            for action in store.list_actions(mission.id) if action["state"] == "SUCCEEDED"
        ][-16:]
    # 2026-09-25: the model sees legal decision types and legal repair kinds, never the
    # internal ``REPAIR/<kind>`` enablement keys (they are not decision types).
    from ..contracts.planning_decisions import exposed_enablement
    decision_types, repair_kinds = exposed_enablement(enablement)
    result["planning_protocol"] = {**package["planning_protocol"],
                                   "enabled_decision_types": decision_types,
                                   "enabled_repair_kinds": repair_kinds}
    from ..storage.planning_human_store import PlanningHumanStore
    result["human_answers"] = [
        {"subject_key": row["subject_key"], "question": row["request"]["payload"]["question"], "answer": row["answer"]}
        for row in PlanningHumanStore(store).list(mission.id) if row["state"] == "ANSWERED"
        and PlanningHumanStore(store).binding_current(row)]
    from ..planning.htn.synthesis import build_request
    result["method_proposal_contexts"] = [
        {"subject_key": goal.subject_key, "request": build_request(network.binding_for_task(goal.task_id), world.capabilities(), world.registry,
            catalog=world.catalog, mission_id=mission.id).to_json()}
        for goal in goals if any(choice["occurrence_id"] == goal.occurrence_id
            and choice["route"] == "EVIDENCE_OR_SYNTHESIS" for choice in result.get("method_selection", ()))
    ]
    result["evidence_predicates"] = [s.to_json() for s in world.predicates.signatures()
        if getattr(world, "observers", None) is not None
        and world.observers.observer_for(s.predicate_ref.id) is not None]
    from ..runtime.role_templates import PLANNING_DECISION_PACKAGE_LABEL
    result.update(package_version=PLANNING_DECISION_PACKAGE_LABEL if h4 else FORMAL_PACKAGE_LABEL, views=formal.views_json(),
                  visible_refs=list(formal.visible_refs), truncated=formal.truncated,
                  omitted_counts=dict(formal.omitted_counts or {}))
    # Count loss that already occurred in the compatibility collector as well as
    # loss in the formal view assembler. Hidden candidates/facts are not absence.
    from ..planning.htn.planner_package import _methods_for
    signatures = {row["goal_signature_id"] for row in package["method_library"]}
    prior_omissions = {
        "methods": max(0, sum(len(tuple(_methods_for(world.registry, signature))) for signature in signatures) - len(package["method_library"])),
        "facts": max(0, len({o.proposition_key for o in observations.values()}) - len(package["facts"])),
        "applicability": max(0, len(selection_reports) - len(package["applicability"])),
    }
    for key, count in prior_omissions.items():
        if count:
            result["omitted_counts"][key] = result["omitted_counts"].get(key, 0) + count
            result["truncated"] = True
    if len(result.get("planning_rejected", ())) > 16:
        result["omitted_counts"]["planning_rejected"] = len(result["planning_rejected"]) - 16
        result["planning_rejected"] = result["planning_rejected"][-16:]
        result["truncated"] = True
    # The bound applies to the entire provider envelope, including compatibility
    # fields and decision controls, not just to the nine views in isolation.
    from simple_harness.contracts import canonical_json
    for name in ("accepted_results", "failures", "facts", "methods"):
        while len(canonical_json(result).encode("utf-8")) > MAX_PACKAGE_BYTES and result["views"][name]:
            result["views"][name].pop()
            result["omitted_counts"][name] = result["omitted_counts"].get(name, 0) + 1
            result["truncated"] = True
    if len(canonical_json(result).encode("utf-8")) > MAX_PACKAGE_BYTES:
        raise PlannerPackageCodecError("mandatory request exceeds 96 KiB; narrow the planning subject")
    return result
