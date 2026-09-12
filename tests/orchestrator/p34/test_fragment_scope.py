# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P34 A03/A05 oracle: independent partial verification, never a repaired history.

Uses a real Store, graph, CAS and deterministic verifier. This is software-boundary
evidence; the separately queued runtime case exercises actual Worker/Critic calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.contracts import (
    Artifact,
    Budget,
    ClaimProposal,
    ContractError,
    ResultEnvelope,
    ids,
)
from agent_orchestrator.contracts.fragments import FragmentProposalV1
from agent_orchestrator.graph.changes import TaskGraphChange
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
)
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionSpec,
    Reservation,
)
from agent_orchestrator.planning.fragments import (
    current_task_revision,
    fragment_validation_layout,
    freeze_fragment_execution,
    revision_for_result,
)
from agent_orchestrator.storage.store import Store
from agent_orchestrator.verification.verifier_router import VerifierRouter


def contract(task):
    return {
        "task_id": task.id,
        **{
            name: getattr(task, name)
            for name in (
                "kind",
                "goal",
                "rationale",
            )
        },
        "success_criteria": list(task.success_criteria),
        "verification_policy": list(task.verification_policy),
        "outputs": list(task.outputs),
    }


def submit(s, task, *, accept=False, output_path=None):
    task = s.store.get_task(task.id)
    if output_path is None:
        output_path = task.outputs[0] if "fragment_validation" in task.context else "good.md"
    config = {"task_contract": contract(task), "message": {"content": "verify"}, "agent_config": {}}
    config["fragment_execution"] = freeze_fragment_execution(
        s.store, s.cas, task=task, intent_config=config, inputs=(), retry_of=None
    )
    attempt, intent = s.commit.create_attempt(
        task.id,
        role="worker",
        model="oracle",
        prompt_version="worker-v2",
        context_version="oracle",
        reservation=Reservation(1000, 0),
        intent_config=config,
        input_hash="fragment-oracle",
    )
    assert s.store.get_intent(intent.intent_id).config["task_contract"] == contract(task)
    s.commit.claim_intent(intent.intent_id, owner="oracle", lease_seconds=60)
    s.commit.record_agent_created(
        intent.intent_id, agent_id="agent:" + attempt.id, expected_turn_id="turn:" + attempt.id
    )
    s.commit.record_submitted(intent.intent_id, receipt={"turn_id": "turn:" + attempt.id})
    data = "完整记录。\n".encode()
    digest = s.cas.put_bytes(data)
    artifact = Artifact(
        id=ids.artifact_id(attempt.id, output_path, digest),
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=output_path,
        version=1,
        content_hash=digest,
        size_bytes=len(data),
        produced_by="agent:" + attempt.id,
        storage_uri=str(s.cas.path_for(digest)),
    )
    envelope = ResultEnvelope(
        id="result:" + attempt.id,
        mission_id=task.mission_id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="recorded",
        claims=(ClaimProposal(content="完整记录。", confidence=0.8),),
        evidence=(output_path,),
        artifacts=(output_path,),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    s.commit.record_result(
        attempt.id,
        envelope=envelope,
        turn_id="turn:" + attempt.id,
        artifacts=[artifact],
        usage_refs=(),
    )
    s.commit.start_verification(envelope.id)
    workspace = Workspace(s.root / attempt.id, attempt_id=attempt.id, writable=True)
    workspace.root.mkdir(parents=True)
    workspace.write_text(output_path, data.decode())

    async def critic(_):
        raise AssertionError("code file-only oracle must not invent a Critic verdict")

    verdict = asyncio.run(
        VerifierRouter().verify(
            mission=s.store.get_mission(task.mission_id),
            task=task,
            envelope=envelope,
            artifacts=[artifact],
            verification_copy=workspace,
            client_result_id=None,
            run_critic=critic,
        )
    )
    for layer in verdict.layers:
        s.commit.record_verification_layer(
            envelope.id, layer=layer.layer, status=layer.status, detail=layer.detail
        )
    if accept:
        assert verdict.passed
        s.commit.accept_result(
            envelope.id, verifier_results=[x.to_json() for x in verdict.layers], owner="oracle"
        )
    else:
        assert not verdict.passed
        assert verdict.short_circuited_at == "rule_check"
        s.commit.fail_result(envelope.id, failures=verdict.failures, owner="oracle")
    return envelope, artifact


@pytest.fixture
def scene(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db")
    cas = ArtifactStore(tmp_path / "cas")
    commit = CommitService(store, artifact_store=cas)
    mission, _ = commit.create_mission(
        MissionSpec(
            goal="完整记录与独立验证",
            tenant_id="tenant",
            success_criteria=("file:good.md", "file:missing.md"),
            idempotency_key="fragment",
            budget=Budget(max_tokens=200_000, max_attempts=20),
        )
    )
    planning = commit.begin_planning(mission.id)
    tasks, _ = commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    {
                        "key": "A",
                        "goal": mission.goal,
                        "rationale": "全部原准则",
                        "success_criteria": list(mission.success_criteria),
                        "dependencies": [],
                        "verification_policy": ["format_check", "rule_check"],
                        "outputs": ["good.md"],
                        "budget": {"max_tokens": 10_000, "max_attempts": 3},
                    }
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "oracle"},
    )
    s = SimpleNamespace(
        store=store, cas=cas, commit=commit, root=tmp_path, task=tasks[0], mission=mission
    )
    s.envelope, s.artifact = submit(s, s.task)
    revision = revision_for_result(store, cas, s.envelope.id)
    s.proposal = FragmentProposalV1.from_json(
        {
            "schema_version": 1,
            "origin": {
                "mission_id": mission.id,
                "task_id": s.task.id,
                "attempt_id": s.envelope.attempt_id,
                "result_id": s.envelope.id,
                "task_revision_id": revision.revision_id,
            },
            "criterion_ids": [revision.criteria[0]["id"]],
            "claim_refs": [],
            "material_refs": [
                {
                    "kind": "artifact",
                    "artifact_id": s.artifact.id,
                    "content_hash": s.artifact.content_hash,
                    "byte_start": 0,
                    "byte_end_exclusive": s.artifact.size_bytes,
                }
            ],
            "rationale": "只验证存在性",
        }
    )
    yield s
    store.close()


def test_projection_preserves_origin_criterion_identity_and_conditions(scene):
    s = scene
    before = s.store.get_result(s.envelope.id).to_json()
    projection = s.commit.project_fragment(s.proposal)
    assert [item["text"] for item in projection.criteria] == ["file:good.md"]
    assert [item["text"] for item in projection.outside_scope] == ["file:missing.md"]
    assert (
        s.commit.project_fragment(replace(s.proposal, rationale="换个说法")).fragment_id
        == projection.fragment_id
    )
    assert s.store.get_result(s.envelope.id).to_json() == before
    assert s.store.get_result(s.envelope.id).verdict == "FAIL"


def test_independent_validation_maps_new_ids_and_does_not_accept_origin(scene):
    s = scene
    receipt = s.commit.commit_fragment_validation(
        s.proposal, command_id="cmd:1", base_graph_version=1, source={"manager": "oracle"}
    )
    new = s.store.get_task(receipt["validation_task_id"])
    assert new.id != s.task.id and new.dependency_ids == ()
    assert new.parent_task_ids == (s.task.id,)
    mapped = receipt["output_path_mapping"]["good.md"]
    assert (
        mapped == "fragment-output/" + receipt["fragment_id"].removeprefix("fragment-") + "/good.md"
    )
    assert new.success_criteria == ("file:" + mapped,)
    assert new.outputs == (mapped,) and not set(new.outputs) & set(s.task.outputs)
    assert new.verification_policy == s.task.verification_policy
    assert new.allowed_tools == s.task.allowed_tools and new.budget == s.task.budget
    mapping = receipt["criterion_mapping"]
    assert mapping[0]["origin_criterion_id"] == s.proposal.criterion_ids[0]
    assert mapping[0]["criterion_id"] != s.proposal.criterion_ids[0]
    assert mapping[0]["origin_text"] == "file:good.md"
    assert mapping[0]["text"] == "file:" + mapped
    assert s.commit.fragment_validation_inputs(new.id)["good.md"].decode() == "完整记录。\n"
    accepted, _ = submit(s, new, accept=True)
    assert s.store.get_task(new.id).accepted_result_id == accepted.id
    assert s.store.get_result(s.envelope.id).verdict == "FAIL"
    assert s.store.get_task(s.task.id).accepted_result_id is None
    assert (
        s.commit.commit_fragment_validation(
            replace(s.proposal, rationale="reworded"),
            command_id="cmd:2",
            base_graph_version=2,
            source={"manager": "oracle"},
        )
        == receipt
    )
    assert len(s.store.list_tasks(s.mission.id)) == 2


def test_original_path_does_not_satisfy_relocated_file_predicate(scene):
    s = scene
    receipt = s.commit.commit_fragment_validation(
        s.proposal, command_id="namespace", base_graph_version=1, source={"manager": "oracle"}
    )
    task = s.store.get_task(receipt["validation_task_id"])
    before = s.store.get_result(s.envelope.id).to_json()
    rejected, _ = submit(s, task, output_path="good.md")
    assert s.store.get_result(rejected.id).verdict == "FAIL"
    assert s.store.get_task(task.id).accepted_result_id is None
    accepted, artifact = submit(s, task, accept=True)
    assert artifact.path == receipt["output_path_mapping"]["good.md"]
    assert s.store.get_task(task.id).accepted_result_id == accepted.id
    assert s.store.get_result(s.envelope.id).to_json() == before


def test_namespace_does_not_disable_independent_graph_collision(scene):
    s = scene
    s.commit.commit_fragment_validation(
        s.proposal, command_id="namespace", base_graph_version=1, source={"manager": "oracle"}
    )
    with pytest.raises(CommitRejected, match="artifact_conflict"):
        s.commit.commit_graph_change(
            s.mission.id,
            TaskGraphChange.from_json(
                {
                    "base_graph_version": 2,
                    "basis": {"trigger": "unrelated_duplicate"},
                    "rationale": "独立任务不能覆盖原输出",
                    "operations": [
                        {
                            "op": "add_task",
                            "key": "duplicate",
                            "goal": s.task.goal,
                            "rationale": "duplicate",
                            "dependencies": [],
                            "success_criteria": ["file:good.md"],
                            "verification_policy": ["format_check", "rule_check"],
                            "outputs": ["good.md"],
                            "budget": s.task.budget.to_json(),
                        }
                    ],
                }
            ),
            source={"manager": "oracle"},
        )
    assert len(s.store.list_tasks(s.mission.id)) == 2


def test_executable_predicate_cannot_validate_relocated_output_against_old_input(scene):
    projection = scene.commit.project_fragment(scene.proposal).to_json()
    projection["criteria"][0].update(kind="pytest", text="pytest:test_original.py")
    with pytest.raises(ContractError, match="pytest output relocation"):
        fragment_validation_layout(projection)


@pytest.mark.parametrize(
    "field,value",
    [
        ("criterion_ids", ["foreign"]),
        ("claim_refs", [{"claim_id": "foreign", "claim_revision": 1}]),
    ],
)
def test_foreign_scope_or_claim_is_rejected_without_graph_write(scene, field, value):
    raw = scene.proposal.to_json()
    raw[field] = value
    with pytest.raises(ContractError):
        scene.commit.commit_fragment_validation(
            FragmentProposalV1.from_json(raw),
            command_id="bad",
            base_graph_version=1,
            source={"manager": "oracle"},
        )
    assert len(scene.store.list_tasks(scene.mission.id)) == 1


def test_corrupt_material_fails_before_any_task_or_receipt(scene):
    scene.cas.path_for(scene.artifact.content_hash).chmod(0o600)
    scene.cas.path_for(scene.artifact.content_hash).write_bytes(b"changed")
    with pytest.raises(ContractError, match="material_unavailable"):
        scene.commit.project_fragment(scene.proposal)
    assert len(scene.store.list_tasks(scene.mission.id)) == 1


@pytest.mark.parametrize("value", [True, -1, 0.5])
def test_strict_material_byte_offsets_reject_bool_negative_float(scene, value):
    raw = scene.proposal.to_json()
    raw["material_refs"][0]["byte_start"] = value
    with pytest.raises(ContractError):
        FragmentProposalV1.from_json(raw)


def test_utf8_subrange_cannot_cut_a_codepoint(scene):
    raw = scene.proposal.to_json()
    raw["material_refs"][0]["byte_start"] = 1
    with pytest.raises(ContractError, match="UTF-8"):
        scene.commit.project_fragment(FragmentProposalV1.from_json(raw))


def test_namespaced_input_requires_separate_receipt_validated_path_map(scene):
    s = scene
    mounted = "candidate-inputs/original/good.md"
    item = {"artifact_id": s.artifact.id, "content_hash": s.artifact.content_hash, "path": mounted}
    config = {"task_contract": contract(s.task), "validated_input_paths": {s.artifact.id: mounted}}
    # A model/frozen config field cannot authorize a path rename.
    with pytest.raises(ContractError, match="input identity"):
        freeze_fragment_execution(
            s.store, s.cas, task=s.task, intent_config=config, inputs=(item,), retry_of=None
        )
    frozen = freeze_fragment_execution(
        s.store,
        s.cas,
        task=s.task,
        intent_config=config,
        inputs=(item,),
        retry_of=None,
        validated_input_paths={s.artifact.id: mounted},
    )
    assert frozen["files"][mounted]["original_path"] == "good.md"
    assert frozen["files"][mounted]["artifact_id"] == s.artifact.id
    assert "good.md" not in frozen["files"]
    with pytest.raises(ContractError, match="input identity"):
        freeze_fragment_execution(
            s.store,
            s.cas,
            task=s.task,
            intent_config=config,
            inputs=({**item, "content_hash": "0" * 64},),
            retry_of=None,
            validated_input_paths={s.artifact.id: mounted},
        )


def test_consumer_requires_accepted_validation_and_does_not_promote_file_claims(scene):
    s = scene
    receipt = s.commit.commit_fragment_validation(
        s.proposal, command_id="validate", base_graph_version=1, source={"manager": "oracle"}
    )
    validation = s.store.get_task(receipt["validation_task_id"])
    submit(s, validation, accept=True)
    children, _ = s.commit.commit_graph_change(
        s.mission.id,
        TaskGraphChange.from_json(
            {
                "base_graph_version": 2,
                "basis": {"trigger": "use_validated_fragment"},
                "rationale": "独立验证后的范围复用",
                "operations": [
                    {
                        "op": "add_task",
                        "key": "consumer",
                        "goal": "完整记录与独立验证：读取已验证文件",
                        "rationale": "只复用文件存在性",
                        "dependencies": [validation.id],
                        "parent_task_ids": [validation.id],
                        "success_criteria": ["file:good.md"],
                        "verification_policy": ["format_check", "rule_check"],
                        "budget": {"max_tokens": 10000, "max_attempts": 3},
                        "outputs": ["consumer.md"],
                    }
                ],
            }
        ),
        source={"manager": "oracle"},
    )
    consumer = children[0]
    config = {
        "task_contract": contract(consumer),
        "agent_config": {},
        "message": {"content": "consume"},
    }
    config["fragment_execution"] = freeze_fragment_execution(
        s.store, s.cas, task=consumer, intent_config=config, inputs=(), retry_of=None
    )
    s.commit.create_attempt(
        consumer.id,
        role="worker",
        model="oracle",
        prompt_version="worker-v2",
        context_version="oracle",
        reservation=Reservation(1000, 0),
        intent_config=config,
        input_hash="consume",
    )
    revision = current_task_revision(s.store, s.store.get_task(consumer.id))
    value = s.commit.fragment_input(
        receipt["fragment_id"], consumer_task_revision_id=revision.revision_id
    )
    assert value["kind"] == "validated_fragment"
    assert value["validation_result_id"] == s.store.get_task(validation.id).accepted_result_id
    assert value["claims"] == []  # file structure cannot promote a content claim
    assert all(ref["artifact_id"] != s.artifact.id for ref in value["material_refs"])
    assert value["material_refs"][0]["original_path"] == "good.md"
    assert value["material_refs"][0]["path"] == receipt["output_path_mapping"]["good.md"]
    assert s.store.get_result(s.envelope.id).verdict == "FAIL"
    with pytest.raises(ContractError, match="consumer"):
        s.commit.fragment_input(receipt["fragment_id"], consumer_task_revision_id="0" * 64)
    # A dynamically added consumer of an already completed dependency starts
    # READY and becomes ACTIVE on its first attempt. Historical replay must
    # derive the same state without needing a synthetic TaskUnblocked event.
    projection = Projection().feed(events_from_store(s.store, s.mission.id))
    projection.check_structure()
    comparison = compare(projection.formal(), formal_from_snapshot(s.store.snapshot(s.mission.id)))
    assert not projection.unknown and not projection.gaps
    assert comparison["consistent"] and not comparison["not_covered"]


def test_command_collision_and_stale_graph_are_atomic(scene):
    s = scene
    with pytest.raises(Exception, match="stale_base"):
        s.commit.commit_fragment_validation(
            s.proposal, command_id="late", base_graph_version=7, source={"manager": "oracle"}
        )
    assert len(s.store.list_tasks(s.mission.id)) == 1
    assert s.commit.list_fragments(s.mission.id) == []
    s.commit.commit_fragment_validation(
        s.proposal, command_id="same", base_graph_version=1, source={"manager": "oracle"}
    )
    raw = s.proposal.to_json()
    raw["material_refs"][0]["byte_end_exclusive"] -= 1
    with pytest.raises(ContractError, match="command identity"):
        s.commit.commit_fragment_validation(
            FragmentProposalV1.from_json(raw),
            command_id="same",
            base_graph_version=2,
            source={"manager": "oracle"},
        )


def test_claim_references_are_part_of_dedup_identity(scene):
    claim = scene.store.list_claims(scene.envelope.id)[0]
    referenced = replace(
        scene.proposal, claim_refs=({"claim_id": claim.id, "claim_revision": claim.version},)
    )
    assert (
        scene.commit.project_fragment(referenced).fragment_id
        != scene.commit.project_fragment(scene.proposal).fragment_id
    )
    with pytest.raises(ContractError, match="claim binding"):
        scene.commit.project_fragment(
            replace(
                referenced,
                claim_refs=({"claim_id": claim.id, "claim_revision": claim.version + 1},),
            )
        )


def test_frozen_proposal_replace_preserves_wire_and_rejects_duplicate_material(scene):
    proposal = scene.proposal
    assert replace(proposal).to_json() == proposal.to_json()
    assert FragmentProposalV1.from_json(proposal.to_json()).to_json() == proposal.to_json()
    with pytest.raises(ContractError, match="duplicate"):
        replace(proposal, material_refs=(*proposal.material_refs, proposal.material_refs[0]))


def test_declared_knowledge_dependency_is_not_silently_ignored(scene):
    from agent_orchestrator.planning.fragments import _current_sources

    revision = revision_for_result(scene.store, scene.cas, scene.envelope.id)
    envelope = replace(scene.envelope, used_knowledge=("knowledge-unavailable",))
    with pytest.raises(ContractError, match="source lineage"):
        _current_sources(scene.store, scene.cas, revision, envelope)


def test_cancelled_origin_remains_a_historical_parent_not_a_dependency(scene):
    s = scene
    s.commit.commit_graph_change(
        s.mission.id,
        TaskGraphChange.from_json(
            {
                "base_graph_version": 1,
                "basis": {"trigger": "replace_failed_origin"},
                "rationale": "原整体结果失败，保留历史材料",
                "operations": [
                    {"op": "cancel_task", "task_id": s.task.id, "reason": "另建独立验证"}
                ],
            }
        ),
        source={"manager": "oracle"},
    )
    before = s.store.get_task(s.task.id).to_json()
    receipt = s.commit.commit_fragment_validation(
        s.proposal, command_id="historical", base_graph_version=2, source={"manager": "oracle"}
    )
    validation = s.store.get_task(receipt["validation_task_id"])
    assert validation.dependency_ids == ()
    assert s.store.get_task(s.task.id).to_json() == before
    assert s.store.get_result(s.envelope.id).verdict == "FAIL"


@pytest.mark.parametrize("damage", ["revoke", "corrupt"])
def test_source_currentness_is_separate_from_historical_receipt(e_scenes, damage):
    from test_p33_source_commits import change_source, produce
    from test_p33_source_commits import submit as source_submit

    s = e_scenes()
    e = source_submit(s)
    layer = produce(e)
    assert layer.status == "PASS"
    # Independent rule receipt is real; the origin need not have been accepted.
    revision = revision_for_result(s.store, s.cas, e.envelope.id)
    row = next(item for item in revision.criteria if item["kind"] == "cite")
    proposal = FragmentProposalV1.from_json(
        {
            "schema_version": 1,
            "origin": {
                "mission_id": s.mission.id,
                "task_id": e.task.id,
                "attempt_id": e.attempt.id,
                "result_id": e.envelope.id,
                "task_revision_id": revision.revision_id,
            },
            "criterion_ids": [row["id"]],
            "claim_refs": [],
            "material_refs": [
                {
                    "kind": "artifact",
                    "artifact_id": e.artifact.id,
                    "content_hash": e.artifact.content_hash,
                    "byte_start": 0,
                    "byte_end_exclusive": e.artifact.size_bytes,
                }
            ],
            "rationale": "保留来源条件",
        }
    )
    before = s.store.get_result(e.envelope.id).to_json()
    assert s.commit.project_fragment(proposal).criteria[0]["text"] == row["text"]
    path = row["text"].removeprefix("cite:")
    if damage == "revoke":
        change_source(s, mode="revoke", path=path)
    else:
        physical = s.cas.path_for(s.store.get_source(s.mission.id, path)["version_hash"])
        physical.chmod(0o600)
        physical.write_bytes(b"broken")
    with pytest.raises(ContractError, match="stale_source" if damage == "revoke" else "ERROR"):
        s.commit.project_fragment(proposal)
    assert s.store.get_result(e.envelope.id).to_json() == before
