"""A deferred new-protocol repair survives a cold Store reopen and resumes locally."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import pytest
from dataclasses import replace
from pathlib import Path

_FULL_TARGET = Path(__file__).resolve().parent
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_h1i_production_entry import (
    real_smoke,
    _config,
    _open_planner_round,
    _refine_reply,
    _seed_new_protocol,
)

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts import AttemptStatus, TaskStatus
from agent_orchestrator.contracts.htn import parse_conditions
from agent_orchestrator.contracts.planning_decisions import (
    PlanningDecisionEnvelopeV1,
    PlanningDecisionStatus,
)
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import deployed_layers
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.event_handler import ROOT_REVIEW_REPAIR_REASON, Orchestrator
from agent_orchestrator.planning.decision_codec import serialize_planning_decision
from agent_orchestrator.planning.htn import evidence_round
from agent_orchestrator.planning.htn.observers.code import code_observers
from agent_orchestrator.planning.htn.world import build_planning_world
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.planning_repair_store import PlanningRepairStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def _repair_reply(package: dict, *, instance_id: str) -> str:
    rejected = next(
        item for item in package["rejected_refinements"]
        if item["rejected_method_instance_id"] == instance_id
    )
    alternatives = [
        item for item in package["applicability"]
        if item["verdict"] == "APPLICABLE"
        and item["goal_occurrence_id"] == rejected["occurrence_id"]
        and item["method_ref"]["method_id"] == "code.fix-by-revert"
    ]
    assert alternatives
    method_ref = alternatives[0]["method_ref"]
    instance_ref = next(
        item for item in package["visible_refs"]
        if item["kind"] == "method_instance" and item["id"] == instance_id
    )
    subject = next(item["subject_key"] for item in package["planning_subjects"]
                   if item["occurrence_id"] == rejected["occurrence_id"])
    return serialize_planning_decision(
        PlanningDecisionEnvelopeV1.from_json(
            {
                "schema_version": 1,
                "decision_type": "REPAIR",
                "subject_key": subject,
                "rationale": "replace the rejected method with the registered applicable alternative",
                "reason_refs": [],
                "assumptions": [],
                "payload": {
                    "repair_kind": "REPLACE_METHOD",
                    "rejected_method_instance": instance_ref,
                    "replacement_method_ref": {
                        "kind": "method",
                        "id": method_ref["method_id"],
                        "semantic_revision": method_ref["version"],
                        "content_hash": method_ref["content_hash"],
                    },
                    "bindings": rejected["typed_parameters"],
                },
                "uncertainties": [],
                "alternatives": [],
                "replan_triggers": [],
            }
        )
    )


@pytest.mark.parametrize("invalid_replacement", ["identity", "bindings"])
def test_deferred_replace_method_cold_resume_reuses_frozen_decision_without_llm(tmp_path: Path, invalid_replacement: str) -> None:
    async def case() -> None:
        provider = RoleScriptedProvider({"planner": []})
        repository = tmp_path / "repo"
        async with Orchestrator(_config(tmp_path), provider) as first:
            mission, _env, _binding, dispatch = _seed_new_protocol(
                first, tmp_path, key="h1i-deferred-repair-resume"
            )
            opener = await _open_planner_round(first, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                first.store, tenant_id=mission.tenant_id, principal=Principal(first._owner)
            ).issue(
                mission.id,
                command_id="grant-h1i-deferred-initial",
                request_id=opener.intent_id,
            )
            await first._collect_plan_decision(
                opener, object(), mission, _refine_reply(opener.config["planning_package"]), dispatch
            )
            network = dispatch.network(mission.id)
            root = network.root_occurrence_ids[0]
            adopted = network.adopted_instance_for(root)
            assert adopted is not None
            instance_id = str(adopted.instance_id)

            # The seed library's real alternative is ``code.fix-by-revert``.  Its
            # second precondition is intentionally false until a bisect identifies
            # the regression commit.  Materialize that actual repository fact, then
            # let the shipped history observer record it through the normal evidence
            # round; the repair package remains the authority on applicability.
            subprocess.run(
                ["git", "update-ref", "refs/bisect/bad", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            )
            regression_condition = parse_conditions(
                [
                    {
                        "op": "predicate",
                        "predicate_ref": real_smoke._vref(
                            "code.regression-commit-known"
                        ).to_json(),
                        "arguments": {
                            "repository": {"op": "parameter", "name": "repository"}
                        },
                    }
                ],
                "preconditions",
            )
            asks = evidence_round.pending_asks(
                regression_condition,
                parameters={"repository": str(repository)},
                registry=_env.predicates,
                snapshot=_env.snapshot(),
            )
            observed = evidence_round.run_round(
                _env.observer_index,
                HtnStore(first.store),
                mission.id,
                asks,
                now_ms=2_000,
            )
            assert [item.observation.predicate_id for item in observed.recorded] == [
                "code.regression-commit-known"
            ]
            assert not observed.unavailable

            completion = OperationCompletionStore(first.store)
            assert completion.get_scope_exact(mission.id, 1, str(root)) is not None
            requirements = HtnStore(first.store).latest_requirements_revision(mission.id)
            assert requirements is not None
            assert completion.get_spec_exact(
                mission.id, requirements.revision, requirements.content_hash()
            ) is not None

            first.commit.record_planning_rejected(
                mission.id,
                ordinal=2,
                reason=ROOT_REVIEW_REPAIR_REASON,
                key=f"{mission.id}:root-review-repair:1",
                detail={
                    "plan_revision": 1,
                    "package_id": "pkg-deferred-repair",
                    "repair_round": 1,
                    "max_root_review_repairs": 1,
                    "findings": [{"severity": "blocker", "criterion_id": "root", "detail": "replace method"}],
                    "occurrence_id": str(root),
                    "method_instance_id": instance_id,
                    "method_ref": adopted.method_ref.to_json(),
                },
            )

            children = [
                first.store.get_task(str(spec.task_id))
                for spec in network.occurrences
                if str(spec.occurrence_id) != str(root)
            ]
            # READY is only a scheduling state.  The completion protocol also
            # requires the dispatch InputManifest to have been frozen from the
            # current plan/data edges before CommitService may open an Attempt.
            dispatch.issue_input_witnesses(
                mission.id, network, now_ms=int(first.store.now * 1_000)
            )
            ready = next(
                task
                for task in children
                if task is not None
                and task.status is TaskStatus.READY
                and dispatch.resolved_inputs(
                    mission.id,
                    network,
                    next(spec for spec in network.occurrences if str(spec.task_id) == task.id),
                ).manifest is not None
                and dispatch.resolved_inputs(
                    mission.id,
                    network,
                    next(spec for spec in network.occurrences if str(spec.task_id) == task.id),
                ).manifest.is_frozen
            )
            unrelated = first.store.get_task(str(root))
            assert unrelated is not None and unrelated.id != ready.id
            frozen_inputs = dispatch.attempt_inputs(mission.id, ready.id)
            attempt, worker = first.commit.create_attempt(
                ready.id,
                role="worker",
                model="fixture-worker",
                prompt_version="worker-hierarchical-v2",
                context_version="deferred-repair-v1",
                reservation=Reservation(tokens=500, cost_micros=0),
                intent_config={"message": "hold a live sibling lease"},
                input_hash="a" * 64,
                inputs=tuple(item.to_json() for item in frozen_inputs),
            )
            first.commit.claim_intent(worker.intent_id, owner="foreign-worker", lease_seconds=60)
            first.commit.record_agent_created(
                worker.intent_id, agent_id="foreign-worker-agent", expected_turn_id="foreign-turn"
            )
            first.commit.record_submitted(worker.intent_id, receipt={"turn_id": "foreign-turn", "seq": 1})
            live = first.store.get_attempt(attempt.id)
            assert live is not None and live.status is AttemptStatus.RUNNING
            first.store.update_attempt(
                replace(live, lease_owner="foreign-worker", lease_expires_at=first.store.now + 60),
                expected_version=live.version,
            )
            unrelated_before = unrelated.to_json()

            # P06: a syntactically valid REPAIR whose replacement identity was
            # not the identity exposed by the frozen package must be rejected
            # before the retirement/stop path can touch live work.  Keep the
            # real foreign lease and every durable work row as the before image.
            invalid_intent = await _open_planner_round(first, mission, dispatch, ordinal=2)
            PlanningAuthorizationApi(
                first.store, tenant_id=mission.tenant_id, principal=Principal(first._owner)
            ).issue(
                mission.id,
                command_id="grant-h1i-invalid-replacement",
                request_id=invalid_intent.intent_id,
            )
            invalid_package = invalid_intent.config["planning_package"]
            invalid_raw = _repair_reply(invalid_package, instance_id=instance_id)
            invalid_body = json.loads(
                invalid_raw.removeprefix("<planning_decision>").removesuffix(
                    "</planning_decision>"
                )
            )
            if invalid_replacement == "identity":
                invalid_body["payload"]["replacement_method_ref"]["content_hash"] = "0" * 64
            else:
                # This keeps the real replacement identity but violates its
                # registered schema; the real compiler must refuse before retiring work.
                invalid_body["payload"]["bindings"]["repository"] = 42
            before_tasks = tuple(task.to_json() for task in first.store.list_tasks(mission.id))
            before_attempt = first.store.get_attempt(attempt.id)
            before_intent = first.store.get_intent_for_subject(attempt.id)
            before_actions = tuple(first.store.list_actions(mission.id))
            await first._collect_plan_decision(
                invalid_intent,
                object(),
                mission,
                serialize_planning_decision(PlanningDecisionEnvelopeV1.from_json(invalid_body)),
                dispatch,
            )
            invalid = PlanningDecisionStore(first.store).get_planning_decision_by_attempt(
                invalid_intent.intent_id, 0
            )
            assert invalid is not None
            assert invalid["status"] == str(PlanningDecisionStatus.REJECTED)
            if invalid_replacement == "bindings":
                assert "STRUCTURE_INVALID" in invalid["rejection_codes"]
            assert tuple(task.to_json() for task in first.store.list_tasks(mission.id)) == before_tasks
            assert first.store.get_attempt(attempt.id) == before_attempt
            assert first.store.get_intent_for_subject(attempt.id) == before_intent
            assert tuple(first.store.list_actions(mission.id)) == before_actions

            # A fresh, separately authorized planner round may then submit the
            # actual registered/applicable replacement.  Only this legal
            # decision is allowed to enter the durable deferred-stop protocol.
            repair_intent = first.store.get_intent_for_subject(f"{mission.id}:planner:3")
            assert repair_intent is not None
            PlanningAuthorizationApi(
                first.store, tenant_id=mission.tenant_id, principal=Principal(first._owner)
            ).issue(
                mission.id,
                command_id="grant-h1i-deferred-repair",
                request_id=repair_intent.intent_id,
            )
            repair_package = repair_intent.config["planning_package"]
            applicable_alternatives = [
                item
                for item in repair_package["applicability"]
                if item["verdict"] == "APPLICABLE"
                and item["method_ref"]["method_id"] == "code.fix-by-revert"
            ]
            assert len(applicable_alternatives) == 1
            raw = _repair_reply(repair_package, instance_id=instance_id)
            await first._collect_plan_decision(repair_intent, object(), mission, raw, dispatch)

            decisions = PlanningDecisionStore(first.store)
            deferred = decisions.get_planning_decision_by_attempt(repair_intent.intent_id, 0)
            assert deferred is not None
            assert deferred["status"] == str(PlanningDecisionStatus.COMPILED)
            continuation = PlanningRepairStore(first.store).get_by_decision(deferred["decision_id"])
            assert continuation is not None and continuation.state == "WAITING"
            frozen = {
                "mission_id": mission.id,
                "intent_id": repair_intent.intent_id,
                "decision_id": deferred["decision_id"],
                "raw_hash": deferred["raw_output_hash"],
                "raw_ref": deferred["raw_artifact_ref"],
                "canonical_hash": deferred["canonical_hash"],
                "grant": dict(continuation.authority_binding),
                "unrelated_task_id": unrelated.id,
                "unrelated": unrelated_before,
                "lease_expires_at": first.store.get_attempt(attempt.id).lease_expires_at,
            }
            assert provider.calls == 0

        resumed_provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), resumed_provider) as resumed:
            mission = resumed.store.get_mission(frozen["mission_id"])
            assert mission is not None
            world = build_planning_world(
                mission.id,
                domains=("code",),
                semantics=HtnStore(resumed.store),
                deployed_layers=deployed_layers(resumed._config.deployment_policy),
                observers=code_observers(repository, allow_test_execution=True),
            )
            resumed.install_hierarchical(planning=world)
            resumed.store._clock = lambda: float(frozen["lease_expires_at"]) + 2.0
            # First pass closes the expired Attempt. The ordinary collector then
            # settles its abandoned dispatch before the next durable retry.
            assert await resumed._retry_deferred_repair() is False
            retired_intent = resumed.store.get_intent_for_subject(attempt.id)
            assert retired_intent is not None
            assert await resumed._collect(retired_intent) is True
            resumed.store._clock = lambda: float(frozen["lease_expires_at"]) + 4.0
            assert await resumed._retry_deferred_repair() is True

            decision = PlanningDecisionStore(resumed.store).get_planning_decision(
                frozen["decision_id"]
            )
            assert decision is not None
            assert decision["status"] == str(PlanningDecisionStatus.COMMITTED)
            assert decision["raw_output_hash"] == frozen["raw_hash"]
            assert decision["raw_artifact_ref"] == frozen["raw_ref"]
            assert decision["canonical_hash"] == frozen["canonical_hash"]
            continuation = PlanningRepairStore(resumed.store).get_by_decision(frozen["decision_id"])
            assert continuation is not None and continuation.state == "APPLIED"
            assert dict(continuation.authority_binding) == frozen["grant"]
            assert resumed.hierarchical.network(mission.id).plan_revision == 2
            unrelated = resumed.store.get_task(frozen["unrelated_task_id"])
            assert unrelated is not None and unrelated.to_json() == frozen["unrelated"]
            assert resumed_provider.calls == 0

    asyncio.run(case())
