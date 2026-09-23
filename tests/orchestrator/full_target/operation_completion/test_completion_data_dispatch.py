"""OC2: a real accepted upstream leaf is a DATA source under the new protocol.

The H1-I code-domain fixture approves a CONTENT_ONLY Spec.  It is intentionally
used here only to prove nonempty DATA freezing and the durable Acceptance pin;
MIXED preparation waiting is covered by the single-task scoped-content suite.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
import sys
from pathlib import Path

import pytest

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

import test_h1i_production_entry as h1i  # noqa: E402

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.artifacts.input_bindings import TargetRules
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.versioning import manifest_upstream_inputs
from agent_orchestrator.contracts import Artifact, ClaimProposal, ResultEnvelope
from agent_orchestrator.contracts.htn import TaskForm
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.contracts.state_machines import AttemptStatus, TaskStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.runtime.output_blocks import PortClaim
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

_OWNER = "oc2-data-dispatch-owner"


def _occurrence_for(dispatch, mission_id: str, signature_id: str):
    network = dispatch.network(mission_id)
    return next(
        item
        for item in network.occurrences
        if str(network.binding_for_occurrence(item.occurrence_id).goal_signature.signature_id)
        == signature_id
    )


def _produce_verified_preparation(service, mission, task, *, tmp_path: Path, extra_path: str | None = None):
    """Use the actual worker/result/verification/acceptance path for the upstream leaf."""

    attempt, intent = service.create_attempt(
        task.id,
        role="worker",
        model="fixture-worker",
        prompt_version="fixture-worker-v1",
        context_version="oc2-data-producer-v1",
        reservation=Reservation(tokens=1_000, cost_micros=0),
        intent_config={"message": "read repository facts"},
        input_hash="d" * 64,
        inputs=(),
    )
    service.claim_intent(intent.intent_id, owner=_OWNER, lease_seconds=60)
    service.record_agent_created(
        intent.intent_id, agent_id="fixture-worker", expected_turn_id="turn-data-producer"
    )
    service.record_submitted(intent.intent_id, receipt={"turn_id": "turn-data-producer", "seq": 1})

    body = b'{"failing_test":"tests/test_kv.py::test_parse_kv_strips_whitespace"}\n'
    artifact_path = tmp_path / "repository-facts.json"
    artifact_path.write_bytes(body)
    cas = ArtifactStore(tmp_path / "producer-cas")
    artifact_digest = cas.put_bytes(body)
    artifact = Artifact(
        id="artifact-data-" + hashlib.sha256(body).hexdigest()[:24],
        mission_id=mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=str(artifact_path),
        version=1,
        content_hash=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        produced_by="fixture-worker",
        storage_uri=str(cas.path_for(artifact_digest)),
    )
    artifacts = [artifact]
    if extra_path is not None:
        extra_body = b"# accepted upstream workspace file\n"
        extra_digest = cas.put_bytes(extra_body)
        artifacts.append(replace(artifact, id="artifact-overlay", path=extra_path,
            content_hash=extra_digest, size_bytes=len(extra_body),
            storage_uri=str(cas.path_for(extra_digest))))
    result = ResultEnvelope(
        id="result-data-producer",
        mission_id=mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="Repository facts were read from the real fixture repository.",
        claims=(ClaimProposal(content="repository facts prepared", confidence=0.9),),
        evidence=(str(artifact_path),),
        artifacts=tuple(item.path for item in artifacts),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    stored = service.record_result(
        attempt.id,
        envelope=result,
        turn_id="turn-data-producer",
        artifacts=tuple(artifacts),
        usage_refs=(),
        port_claims=(PortClaim(port_key="facts", path=artifact.path),),
    )
    service.start_verification(stored.envelope.id)
    for layer in ("schema_check", "rule_check", "critic_review"):
        service.record_verification_layer(
            stored.envelope.id,
            layer=layer,
            status="PASS",
            detail={"producer": "CommitService", "layer": layer},
        )
    completed = service.accept_result(stored.envelope.id, verifier_results=())
    assert completed.status is TaskStatus.COMPLETED
    assert completed.accepted_result_id == stored.envelope.id
    persisted_attempt = service.store.get_attempt(attempt.id)
    assert persisted_attempt is not None and persisted_attempt.status is AttemptStatus.COMPLETED
    return attempt, intent, stored, artifact


@pytest.mark.parametrize("overlay", ["none", "seed", "readonly-new-test"])
def test_oc2_data_dispatch_freezes_nonempty_accepted_input_and_never_uses_order_only(
    tmp_path: Path, overlay: str, monkeypatch,
) -> None:
    """OC2: DATA becomes readable from a real accepted source; an unbound edge stays open."""

    async def case() -> None:
        async with h1i.Orchestrator(
            h1i._config(tmp_path), RoleScriptedProvider({"planner": []})
        ) as loop:
            mission, _env, _binding, dispatch = h1i._seed_new_protocol(
                loop, tmp_path, key="oc2-data-dispatch"
            )
            if overlay == "seed":
                with loop.store.transaction():
                    current = loop.store.get_mission(mission.id)
                    loop.store.update_mission(replace(current,
                        final_report={**(current.final_report or {}),
                                      "workspace_seed": {"target.py": "# seed"}},
                        version=current.version + 1), expected_version=current.version)
            opener = await h1i._open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=Principal(loop._owner),
            ).issue(
                mission.id,
                command_id="grant-oc2-data-dispatch",
                request_id=opener.intent_id,
            )
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                h1i._refine_reply(opener.config["planning_package"]),
                dispatch,
            )

            network = dispatch.network(mission.id)
            producer_occurrence = _occurrence_for(
                dispatch, mission.id, "code.read-repository-facts"
            )
            consumer_occurrence = _occurrence_for(dispatch, mission.id, "code.reproduce-failure")
            assert producer_occurrence.form is TaskForm.PRIMITIVE
            assert consumer_occurrence.form is TaskForm.PRIMITIVE
            data_edge = next(
                item
                for item in network.data_requirements
                if item.producer_occurrence == producer_occurrence.occurrence_id
                and item.consumer_occurrence == consumer_occurrence.occurrence_id
            )
            assert data_edge.output_port == "facts"

            # The adopted plan's relation alone cannot materialise a source.  This
            # is the pre-preparation oracle for the pure ORDER / no-artifact case.
            before = dispatch.resolved_inputs(mission.id, network, consumer_occurrence)
            assert before.manifest is not None and not before.manifest.is_frozen
            assert before.manifest.bindings == ()
            assert before.manifest.pending

            producer_task = loop.store.get_task(str(producer_occurrence.task_id))
            consumer_task = loop.store.get_task(str(consumer_occurrence.task_id))
            assert producer_task is not None and consumer_task is not None
            _producer_attempt, _producer_intent, _producer_result, artifact = (
                _produce_verified_preparation(
                    loop.commit, mission, producer_task, tmp_path=tmp_path,
                    extra_path=("target.py" if overlay == "seed" else
                                "tests/test_extra.py" if overlay == "readonly-new-test" else None)
                )
            )

            # This shared H1-I fixture has a CONTENT_ONLY Spec, so its producer
            # correctly completes.  The oracle here is the nonempty DATA pin, not
            # MIXED preparation state.
            assert loop.store.get_task(producer_task.id).status is TaskStatus.COMPLETED
            dispatch.issue_input_witnesses(mission.id, network, now_ms=1_000_000)
            resolved = dispatch.resolved_inputs(mission.id, network, consumer_occurrence)
            assert resolved.manifest is not None and resolved.manifest.is_frozen
            assert len(resolved.manifest.bindings) == 1
            bound = resolved.manifest.bindings[0]
            assert bound.artifact_id == artifact.id
            assert bound.acceptance_id

            rules = dispatch.target_rules or TargetRules(namespace=f"workspace:{consumer_task.id}")
            upstream = manifest_upstream_inputs(resolved.manifest, rules, network=network)
            assert len(upstream) == 1
            assert upstream[0].artifact_id == artifact.id
            assert upstream[0].content_hash == artifact.content_hash

            upstream = dispatch.overlay_attempt_inputs(mission.id, upstream)
            assert len(upstream) == (2 if overlay == "seed" else 1)
            if overlay == "seed":
                assert next(item for item in upstream if item.path == "target.py").artifact_id == "artifact-overlay"
            assert all(item.path != "tests/test_extra.py" for item in upstream)

            # A caller cannot use the reviewed artifact at a mount the adopted
            # DATA manifest did not select. The refusal leaves no new Attempt.
            attempts_before = tuple(loop.store.list_attempts(consumer_task.id))
            forged = [{**upstream[0].to_json(), "path": "forged/report.json"}]
            with pytest.raises(Exception, match="dispatch inputs differ from frozen manifest"):
                loop.commit.create_attempt(
                    consumer_task.id,
                    role="worker",
                    model="fixture-worker",
                    prompt_version="fixture-worker-v1",
                    context_version="oc2-data-consumer-v1",
                    reservation=Reservation(tokens=1_000, cost_micros=0),
                    intent_config={"message": "reproduce the observed failure"},
                    input_hash=sha256_hex("oc2-data-consumer"),
                    inputs=forged,
                )
            assert tuple(loop.store.list_attempts(consumer_task.id)) == attempts_before

            if overlay == "seed":
                original_get = loop.store.get_artifact
                def wrong_owner(artifact_id):
                    found = original_get(artifact_id)
                    return (replace(found, mission_id="foreign-mission")
                            if artifact_id == "artifact-overlay" else found)
                with monkeypatch.context() as patch:
                    patch.setattr(loop.store, "get_artifact", wrong_owner)
                    with pytest.raises(Exception, match="artifact ownership"):
                        dispatch.overlay_attempt_inputs(mission.id, upstream)
                    with pytest.raises(Exception, match="artifact ownership"):
                        loop.commit.create_attempt(
                            consumer_task.id, role="worker", model="fixture-worker",
                            prompt_version="fixture-v1", context_version="fixture-v1",
                            reservation=Reservation(tokens=1000, cost_micros=0),
                            intent_config={}, input_hash="e" * 64,
                            inputs=[item.to_json() for item in upstream])
                assert tuple(loop.store.list_attempts(consumer_task.id)) == attempts_before

            consumer_attempt, consumer_intent = loop.commit.create_attempt(
                consumer_task.id,
                role="worker",
                model="fixture-worker",
                prompt_version="fixture-worker-v1",
                context_version="oc2-data-consumer-v1",
                reservation=Reservation(tokens=1_000, cost_micros=0),
                intent_config={"message": "reproduce the observed failure"},
                input_hash=sha256_hex("oc2-data-consumer"),
                inputs=[item.to_json() for item in upstream],
            )
            frozen = consumer_intent.config["completion_inputs"]
            assert frozen["manifest_hash"]
            assert frozen["manifest"]["bindings"]
            assert frozen["manifest"]["bindings"][0]["bound_input"]["acceptance_id"] == (
                bound.acceptance_id
            )
            assert (
                HtnStore(loop.store).get_input_manifest(frozen["manifest_hash"])
                == frozen["manifest"]
            )

            # A fresh dispatch instance sees precisely the same frozen source pins;
            # no in-memory acceptance cache is part of the downstream input claim.
            cold = HierarchicalDispatch(loop.store, loop.commit)
            cold_network = cold.network(mission.id)
            cold_consumer = next(
                item
                for item in cold_network.occurrences
                if item.occurrence_id == consumer_occurrence.occurrence_id
            )
            cold_resolved = cold.resolved_inputs(mission.id, cold_network, cold_consumer)
            assert cold_resolved.manifest is not None
            assert cold_resolved.manifest.to_json() == resolved.manifest.to_json()
            assert loop.store.get_attempt(consumer_attempt.id) is not None

    asyncio.run(case())
