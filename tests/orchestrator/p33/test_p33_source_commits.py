# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""E01/02 and projection-boundary oracle, written before E production changes.

Real Facade source approvals race real recorded integrity PASS and Commit accept.
Direct old citations fail; unused changes and inherited stale knowledge do not.
Accepted historical rows never change on source replacement or acceptance replay.
Projection inherits every upstream version, including two hashes of the same path.
The companion arbitration suite reuses these real graph/result/CAS drivers.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.contracts import (
    Artifact,
    AttemptStatus,
    Budget,
    ClaimProposal,
    ClaimStatus,
    ResultEnvelope,
    SourceCitation,
    TaskStatus,
    ids,
)
from agent_orchestrator.contracts.models import canonical_json
from agent_orchestrator.governance.domains import CODE_DOMAIN, CODE_PROFILE, DOC_DOMAIN, DOC_PROFILE
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.memory.verified_knowledge import KnowledgeIndex
from agent_orchestrator.observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
)
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec, Reservation
from agent_orchestrator.storage.store import Store
from agent_orchestrator.verification.assessments import assessment_binding_for, citation_integrity
from agent_orchestrator.verification.critics import CriticVerdict
from agent_orchestrator.verification.deterministic_checks import rule_check
from agent_orchestrator.verification.evidence_resolver import EvidenceResolver
from agent_orchestrator.verification.verifier_router import VerifierRouter

PATH = "sources/a.md"
TEXT = "甲条件下可以使用。\n"
NEW_TEXT = "乙条件下需要复核。\n"


def attach(s, store):
    """Only the deployment guard is outside this Commit/Facade fixture's scope."""
    commit = CommitService(
        store,
        artifact_store=s.cas,
        deployed_layers=frozenset(s.profile.runs_layers),
    )
    host = SimpleNamespace(
        store=store,
        commit=commit,
        config=SimpleNamespace(deployment_policy=DeploymentPolicy()),
        validate_source_storage=lambda mission_id: None,
    )
    return SimpleNamespace(
        **{
            **vars(s),
            "store": store,
            "commit": commit,
            "host": host,
            "api": MissionControlV1(host, tenant_id="tenant", principal=Principal("reviewer")),
        }
    )


@pytest.fixture
def e_scenes(tmp_path):
    opened = []

    def make(*, paths=(PATH, PATH, PATH), sources=None, domain=DOC_DOMAIN):
        root = tmp_path / str(len(opened))
        root.mkdir()
        store = Store.open(root / "orchestrator.db", clock=lambda: 1000.0)
        opened.append(store)
        s = attach(
            SimpleNamespace(
                root=root,
                cas=ArtifactStore(root / "artifacts"),
                profile=CODE_PROFILE if domain == CODE_DOMAIN else DOC_PROFILE,
            ),
            store,
        )
        mission, _ = s.commit.create_mission(
            MissionSpec(
                goal="来源与争议的实际提交",
                success_criteria=("file:REPORT.md",),
                tenant_id="tenant",
                idempotency_key="e-oracle",
                domain=domain,
                budget=Budget(max_tokens=120_000, max_attempts=40),
                conflict_reserve_tokens=15_000,
            )
        )
        s.mission = mission
        if domain == DOC_DOMAIN:
            for path, content in (sources or {PATH: TEXT}).items():
                s.api.register_source(
                    dict(
                        mission_id=mission.id,
                        path=path,
                        content=content,
                        kind="markdown",
                        idempotency_key="register:" + path,
                    )
                )
        planning = s.commit.begin_planning(mission.id)
        nodes = [
            dict(
                key=f"T{n}",
                goal=f"核对分支{n}",
                rationale="保留证据",
                dependencies=[],
                success_criteria=["file:REPORT.md"]
                + (["cite:" + path] if domain == DOC_DOMAIN else []),
                verification_policy=["format_check", "rule_check", "critic_review"],
                allowed_tools=[],
                budget={"max_tokens": 10_000, "max_attempts": 4},
            )
            for n, path in enumerate(paths)
        ]
        s.tasks, _ = s.commit.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json({"tasks": nodes}),
            base_version=planning.version,
            source={"planner": "fixture"},
        )
        s.mission = store.get_mission(mission.id)
        return s

    yield make
    for store in opened:
        store.close()


def change_source(s, *, mode="supersede", path=PATH, content=NEW_TEXT, key="change"):
    current = s.store.get_source(s.mission.id, path)
    body = dict(
        mission_id=s.mission.id,
        path=path,
        expected_version_hash=current["version_hash"],
        idempotency_key=key,
    )
    if mode == "supersede":
        pending = s.api.supersede_source({**body, "content": content, "kind": "markdown"})
    else:
        pending = s.api.revoke_source({**body, "reason": "原始材料撤回"})
    assert s.store.get_source(s.mission.id, path) == current
    assert s.store.get_approval(pending["request_id"])["kind"] == "source_change"
    s.api.decide(pending["request_id"], "approve", nonce="approve:" + key)
    assert s.store.get_approval(pending["request_id"])["state"] == "GRANTED"
    return pending


def submit(
    s,
    *,
    index=0,
    task=None,
    path=PATH,
    content=None,
    key=None,
    stance="affirms",
    used=(),
    citations=None,
):
    task = s.store.get_task((task or s.tasks[index]).id)
    versions = {
        row["path"]: row["version_hash"] for row in s.store.list_sources(s.mission.id, True)
    }
    if citations is None:
        citations = (
            ()
            if s.profile.id == CODE_DOMAIN
            else (
                SourceCitation(
                    path, versions[path], 1, 1, s.cas.read(versions[path]).decode().strip()
                ),
            )
        )
    contract = {
        name: getattr(task, name)
        for name in (
            "kind",
            "goal",
            "rationale",
            "success_criteria",
            "verification_policy",
            "outputs",
        )
    }
    contract["task_id"] = task.id
    for name in ("success_criteria", "verification_policy", "outputs"):
        contract[name] = list(contract[name])
    attempt, intent = s.commit.create_attempt(
        task.id,
        role="arbiter" if task.kind == "conflict" else "worker",
        model="fixture",
        prompt_version="fixture-v1",
        context_version="fixture-v1",
        reservation=Reservation(1000, 0),
        intent_config={
            "agent_config": {},
            "task_contract": contract,
            "message": json.dumps({"task_contract": contract}),
            "source_versions": versions,
            "source_roots": ["sources/"],
        },
        input_hash="e-fixture",
    )
    turn = "turn:" + attempt.id
    s.commit.claim_intent(intent.intent_id, owner="fixture", lease_seconds=60)
    s.commit.record_agent_created(
        intent.intent_id, agent_id="agent:" + attempt.id, expected_turn_id=turn
    )
    s.commit.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
    data = "真实候选报告，不代表自动裁决。\n".encode()
    digest = s.cas.put_bytes(data)
    output = "REPORT.md" if task.kind != "conflict" else task.context["artifact_dir"] + "/REPORT.md"
    artifact = Artifact(
        id=ids.artifact_id(attempt.id, output, digest),
        mission_id=s.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=output,
        version=1,
        content_hash=digest,
        size_bytes=len(data),
        produced_by="agent:" + attempt.id,
        storage_uri=str(s.cas.path_for(digest)),
    )
    proposal = ClaimProposal(
        content=content or (citations[0].quote if citations else "人工审查候选"),
        confidence=0.8,
        citations=citations,
        key=key,
        stance=stance,
    )
    envelope = ResultEnvelope(
        id="result:" + attempt.id,
        mission_id=s.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="候选报告",
        claims=(proposal,),
        evidence=("artifact:" + output,),
        artifacts=(output,),
        proposed_tasks=(),
        used_knowledge=tuple(used),
        risks=(),
        cost={},
    )
    s.commit.record_result(
        attempt.id, envelope=envelope, turn_id=turn, artifacts=(artifact,), usage_refs=()
    )
    s.commit.start_verification(envelope.id)
    root = s.root / attempt.id.replace(":", "_")
    (root / output).parent.mkdir(parents=True, exist_ok=True)
    (root / output).write_bytes(data)
    return SimpleNamespace(
        **vars(s),
        task=task,
        attempt=s.store.get_attempt(attempt.id),
        envelope=envelope,
        artifact=artifact,
        workspace=Workspace(root, attempt.id, False, s.cas),
    )


def binding(e):
    return assessment_binding_for(
        e.store,
        task=e.store.get_task(e.task.id),
        attempt=e.store.get_attempt(e.attempt.id),
        envelope=e.envelope,
        artifacts=(e.store.get_artifact(e.artifact.id),),
    )


def produce(e):
    structural = rule_check(
        e.envelope,
        e.store.get_task(e.task.id),
        artifacts=(e.artifact,),
        verification_copy=e.workspace,
        knowledge=KnowledgeIndex.load(e.store, e.mission.id),
        domain=e.commit.domain_for(e.mission.id),
    )
    layer = citation_integrity(
        binding=binding(e),
        envelope=e.envelope,
        resolver=EvidenceResolver(e.store, e.cas),
        structural_result=structural,
    )
    e.commit.record_verification_layer(
        e.envelope.id, layer=layer.layer, status=layer.status, detail=layer.detail
    )
    return layer


def verify(e):
    """Actual six-layer router with only the model Critic verdict supplied by a fixture."""

    async def critic(_):
        return CriticVerdict.from_json({"verdict": "PASS", "findings": [], "mission_criteria": []})

    async def record(layer):
        e.commit.record_verification_layer(
            e.envelope.id, layer=layer.layer, status=layer.status, detail=layer.detail
        )

    return asyncio.run(
        VerifierRouter(domain=e.profile).verify(
            mission=e.mission,
            task=e.store.get_task(e.task.id),
            envelope=e.envelope,
            artifacts=(e.artifact,),
            verification_copy=e.workspace,
            client_result_id=None,
            run_critic=critic,
            recorder=record,
            knowledge=KnowledgeIndex.load(e.store, e.mission.id),
            assessment_binding=binding(e),
            evidence_resolver=EvidenceResolver(e.store, e.cas),
        )
    )


def accept(e):
    return e.commit.accept_result(e.envelope.id, verifier_results=())


def historical(s):
    return canonical_json(
        {
            "claims": [c.to_json() for c in s.store.list_mission_claims(s.mission.id)],
            "knowledge": [r.to_json() for r in s.store.list_knowledge(s.mission.id)],
            "assessments": s.store.list_criterion_assessments(s.mission.id),
        }
    )


def replay(s):
    folded = Projection().feed(events_from_store(s.store, s.mission.id))
    assert dict(folded.unknown) == {}
    report = compare(folded.objects, formal_from_snapshot(s.store.snapshot(s.mission.id)))
    assert report["mismatches"] == [] and report["coverage"] == 1.0, report


@pytest.mark.parametrize("mode", ["supersede", "revoke"])
@pytest.mark.parametrize("reopen", [False, True])
def test_e01_real_source_approval_after_rule_pass_blocks_new_accept(e_scenes, mode, reopen):
    s = e_scenes()
    e = submit(s)
    assert produce(e).status == "PASS"
    original_rule = s.store.list_verifications(e.envelope.id)[0]
    change_source(s, mode=mode)
    reopened = Store.open(s.store.path) if reopen else None
    try:
        current = attach(s, reopened) if reopened else s
        task = current.commit.accept_result(e.envelope.id, verifier_results=())
        assert task.status is not TaskStatus.COMPLETED and task.accepted_result_id is None
        attempt = current.store.get_attempt(e.attempt.id)
        assert attempt.status is AttemptStatus.RETRY_WAIT
        assert attempt.failure["reason"] == "verification_failed"
        assert "stale_source" in canonical_json(attempt.failure)
        assert (
            current.store.get_claim(ids.claim_id(e.envelope.id, 1)).status
            is ClaimStatus.UNDER_REVIEW
        )
        assert current.store.list_knowledge(s.mission.id) == []
        assert current.store.list_criterion_assessments(s.mission.id) == []
        # The prior frozen integrity check remains truthful history; currentness is new.
        assert current.store.list_verifications(e.envelope.id)[0] == original_rule
        replay(current)
    finally:
        if reopened:
            reopened.close()


@pytest.mark.parametrize("mode", ["supersede", "revoke"])
def test_e01_unreferenced_frozen_source_change_does_not_reject(e_scenes, mode):
    s = e_scenes(sources={PATH: TEXT, "sources/unused.md": "未被引用。\n"})
    e = submit(s)
    assert produce(e).status == "PASS"
    change_source(s, mode=mode, path="sources/unused.md")
    assert accept(e).status is TaskStatus.COMPLETED
    assert s.store.get_claim(ids.claim_id(e.envelope.id, 1)).status is ClaimStatus.VERIFIED


@pytest.mark.parametrize("damage", ["missing", "bytes"])
def test_e01_cas_error_after_rule_pass_is_error_not_uncertainty(e_scenes, damage):
    e = submit(e_scenes())
    assert produce(e).status == "PASS"
    source = e.cas.path_for(e.envelope.claims[0].citations[0].version)
    if damage == "missing":
        source.unlink()
    else:
        source.chmod(0o600)
        source.write_bytes(b"invalid CAS replacement")
    assert accept(e).status is not TaskStatus.COMPLETED
    attempt = e.store.get_attempt(e.attempt.id)
    assert attempt.failure["reason"] == "verification_failed"
    assert any(f["status"] == "ERROR" for f in attempt.failure["failures"])
    assert e.store.list_knowledge(e.mission.id) == []
    assert e.store.list_criterion_assessments(e.mission.id) == []


@pytest.mark.parametrize("mode", ["supersede", "revoke"])
def test_e02_source_change_and_accept_replay_never_rewrite_accepted_history(e_scenes, mode):
    s = e_scenes()
    e = submit(s)
    assert produce(e).status == "PASS"
    accept(e)
    old = historical(s)
    change_source(s, mode=mode)
    assert historical(s) == old
    source = e.envelope.claims[0].citations[0]
    read = EvidenceResolver(s.store, s.cas).read_source(
        tenant_id="tenant",
        mission_id=s.mission.id,
        path=source.path,
        version=source.version,
        source_roots=("sources/",),
    )
    assert read.status == "resolved" and read.text == TEXT
    reopened = Store.open(s.store.path)
    try:
        restored = attach(s, reopened)
        before = restored.store.snapshot(s.mission.id)
        restored.commit.accept_result(e.envelope.id, verifier_results=())
        assert restored.store.snapshot(s.mission.id) == before
        assert historical(restored) == old
        replay(restored)
    finally:
        reopened.close()


def test_projection_inherits_two_versions_of_one_path_without_stale_used_knowledge_failure(
    e_scenes,
):
    s = e_scenes()
    first = submit(s)
    produce(first)
    accept(first)
    first_id = ids.claim_id(first.envelope.id, 1)
    old_hash = first.envelope.claims[0].citations[0].version
    change_source(s)
    index = KnowledgeIndex.load(s.store, s.mission.id)
    assert index.check((first_id,)) == []  # required negative oracle: check() is unchanged
    second = submit(s, index=1, used=(first_id,))
    assert produce(second).status == "PASS"
    assert accept(second).status is TaskStatus.COMPLETED
    second_id = ids.claim_id(second.envelope.id, 1)
    new_hash = second.envelope.claims[0].citations[0].version
    assert s.store.get_knowledge(second_id).source_versions == {
        PATH: tuple(sorted((old_hash, new_hash)))
    }
    stale = KnowledgeIndex.load(s.store, s.mission.id).stale((second_id,))
    assert second_id in stale and "stale_source" in canonical_json(stale)
    third = submit(s, index=2, used=(second_id,))
    assert produce(third).status == "PASS"
    assert accept(third).status is TaskStatus.COMPLETED
    third_id = ids.claim_id(third.envelope.id, 1)
    assert s.store.get_knowledge(third_id).source_versions == {
        PATH: tuple(sorted((old_hash, new_hash)))
    }
    assert third_id in KnowledgeIndex.load(s.store, s.mission.id).stale((third_id,))
    replay(s)
