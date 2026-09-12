# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""C01/03/04/05/06/07：先于实现的真实评估提交 oracle。

公共 Mission/Task/Attempt/result 入口建立前置；真实 CAS、rule_check 与
citation_integrity 产生评估，不手填成功 receipt。只有反例篡改已产生的记录。
accept 必须消费 recorded layer，复核合同/结果/claim/来源全部绑定；评估与知识
同事务。失败仅存层 detail，doc claim 停 UNDER_REVIEW/unsupported。已 accepted
记录完全相同重放无写入，改变层或追补评估一律拒绝；旧库查询有 has_table 守卫。
结构准则有 verdict，但不捏造 claim assessment，更不能给无依据的 claim 晋级。
本组显式保留 C 的 DOC2/citation_integrity@v1 路径；DOC3 由 D 套件覆盖。
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.contracts import (
    Artifact,
    Budget,
    ClaimProposal,
    ClaimStatus,
    CriterionAssessmentV1,
    ResultEnvelope,
    SourceCitation,
    TaskStatus,
    ids,
)
from agent_orchestrator.governance.domains import CODE_DOMAIN, DOC_DOMAIN, DOC_PROFILE
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
)
from agent_orchestrator.orchestrator import commit_service as commit_module
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionSpec,
    Reservation,
    TaskProposal,
)
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import Store, StoreConflict
from agent_orchestrator.verification.assessments import (
    assessment_binding_for,
    citation_integrity,
)
from agent_orchestrator.verification.deterministic_checks import rule_check
from agent_orchestrator.verification.evidence_resolver import EvidenceResolver

PATH = "sources/notes.md"
QUOTE = "方案仅在隔离环境有效。"
SOURCE = "# 原始说明\n\n" + QUOTE + "\n"


@pytest.fixture
def submitted(tmp_path, monkeypatch):
    store = Store.open(tmp_path / "orchestrator.db", clock=lambda: 1_000.0)
    cas = ArtifactStore(tmp_path / "artifacts")
    commit = CommitService(
        store, artifact_store=cas, deployed_layers=frozenset(DOC_PROFILE.runs_layers)
    )
    count = 0
    legacy_doc = replace(DOC_PROFILE, version="2", adapters={})
    resolve_domain = commit_module.resolve_domain

    def make(
        *,
        criteria=None,
        proposals=None,
        domain=DOC_DOMAIN,
        siblings=False,
        prior=None,
        source=SOURCE,
    ):
        nonlocal count
        count += 1
        criteria = criteria or ("file:REPORT.md", f"cite:{PATH}")
        # Simulate the published C deployment only at profile selection. The public
        # creation transaction persists the actual DOC2 binding/event; subsequent
        # dispatch, verification, acceptance and reopen read that frozen profile.
        with monkeypatch.context() as patch:
            patch.setattr(
                commit_module,
                "resolve_domain",
                lambda domain_id: (
                    legacy_doc if domain_id == DOC_DOMAIN else resolve_domain(domain_id)
                ),
            )
            mission, _ = (
                commit.create_mission(
                    MissionSpec(
                        goal="核对来源",
                        success_criteria=("file:REPORT.md",),
                        tenant_id="tenant",
                        idempotency_key=f"assessment-{count}",
                        domain=domain,
                        budget=Budget(max_tokens=20_000, max_attempts=3),
                    )
                )
                if prior is None
                else (prior.mission, False)
            )
        version = (
            hashlib.sha256(source.encode()).hexdigest() if prior is None else prior.citation.version
        )
        if domain == DOC_DOMAIN and prior is None:
            commit.register_source(
                mission_id=mission.id,
                tenant_id=mission.tenant_id,
                principal=Principal("human"),
                path=PATH,
                content=source,
                kind="markdown",
                idempotency_key="source",
            )
        peer = None
        if prior is not None:
            task = store.get_task(prior.peer.id)
        elif siblings:
            planning = commit.begin_planning(mission.id)
            nodes = [
                {
                    "key": key,
                    "goal": f"核对来源{key}",
                    "rationale": f"独立核对{key}",
                    "dependencies": [],
                    "success_criteria": list(criteria),
                    "verification_policy": ["format_check", "rule_check", "critic_review"],
                    "allowed_tools": [],
                    "budget": {"max_tokens": 10_000, "max_attempts": 2},
                }
                for key in ("A", "B")
            ]
            tasks, _ = commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": nodes}),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            task, peer = tasks
        else:
            planning = commit.begin_planning(mission.id)
            task, _ = commit.commit_task_proposal(
                mission.id,
                TaskProposal(
                    goal="核对来源",
                    rationale="核对原文归属",
                    success_criteria=criteria,
                    verification_policy=("format_check", "rule_check", "critic_review"),
                    allowed_tools=(),
                    budget=Budget(max_tokens=10_000, max_attempts=2),
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
        contract = {
            "task_id": task.id,
            "task_version": task.version,
            "kind": task.kind,
            "goal": task.goal,
            "rationale": task.rationale,
            "success_criteria": list(task.success_criteria),
            "verification_policy": list(task.verification_policy),
            "outputs": list(task.outputs),
        }
        attempt, intent = commit.create_attempt(
            task.id,
            role="worker",
            model="fixture",
            prompt_version="fixture-v1",
            context_version="fixture-v1",
            reservation=Reservation(1_000, 0),
            intent_config={
                "agent_config": {},
                "message": json.dumps({"task_contract": contract}),
                "task_contract": contract,
                "source_versions": {PATH: version} if domain == DOC_DOMAIN else {},
                "source_roots": ["sources/"],
            },
            input_hash="fixture",
        )
        turn = "turn:" + attempt.id
        commit.claim_intent(intent.intent_id, owner="fixture", lease_seconds=60)
        commit.record_agent_created(
            intent.intent_id, agent_id="agent:" + attempt.id, expected_turn_id=turn
        )
        commit.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
        data = b"report\n"
        digest = cas.put_bytes(data)
        artifact = Artifact(
            id=ids.artifact_id(attempt.id, "REPORT.md", digest),
            mission_id=mission.id,
            task_id=task.id,
            attempt_id=attempt.id,
            type="file",
            path="REPORT.md",
            version=1,
            content_hash=digest,
            size_bytes=len(data),
            produced_by="agent:" + attempt.id,
            storage_uri=str(cas.path_for(digest)),
        )
        citation = SourceCitation(PATH, version, 3, 3, QUOTE)
        claims = (
            proposals(citation)
            if proposals
            else (
                ClaimProposal(
                    content=QUOTE,
                    confidence=0.8,
                    citations=(citation,),
                    key="model-key",
                    stance="refutes",
                ),
            )
        )
        envelope = ResultEnvelope(
            id="result:" + attempt.id,
            mission_id=mission.id,
            task_id=task.id,
            attempt_id=attempt.id,
            outcome="candidate",
            summary="候选报告",
            claims=claims,
            evidence=("artifact:REPORT.md",),
            artifacts=("REPORT.md",),
            proposed_tasks=(),
            used_knowledge=(),
            risks=(),
            cost={},
        )
        commit.record_result(
            attempt.id, envelope=envelope, turn_id=turn, artifacts=(artifact,), usage_refs=()
        )
        commit.start_verification(envelope.id)
        root = tmp_path / f"verification-{count}"
        root.mkdir()
        (root / "REPORT.md").write_bytes(cas.read(digest))
        return SimpleNamespace(
            store=store,
            commit=commit,
            cas=cas,
            mission=mission,
            task=store.get_task(task.id),
            attempt=store.get_attempt(attempt.id),
            envelope=envelope,
            artifact=store.get_artifact(artifact.id),
            citation=citation,
            workspace=Workspace(root, attempt.id, False, cas),
            peer=peer,
        )

    try:
        yield make
    finally:
        store.close()


def produce(e, *, record=True):
    binding = assessment_binding_for(
        e.store,
        task=e.store.get_task(e.task.id),
        attempt=e.store.get_attempt(e.attempt.id),
        envelope=e.envelope,
        artifacts=(e.artifact,),
    )
    structural = rule_check(
        e.envelope,
        e.store.get_task(e.task.id),
        artifacts=(e.artifact,),
        verification_copy=e.workspace,
        domain=e.commit.domain_for(e.mission.id),
    )
    layer = citation_integrity(
        binding=binding,
        envelope=e.envelope,
        resolver=EvidenceResolver(e.store, e.cas),
        structural_result=structural,
    )
    if record:
        e.commit.record_verification_layer(
            e.envelope.id, layer=layer.layer, status=layer.status, detail=layer.detail
        )
    return layer


def accept(e):
    return e.commit.accept_result(e.envelope.id, verifier_results=())


def assert_replay(e):
    folded = Projection().feed(events_from_store(e.store, e.mission.id))
    assert dict(folded.unknown) == {}
    report = compare(folded.objects, formal_from_snapshot(e.store.snapshot(e.mission.id)))
    assert report["mismatches"] == [] and report["coverage"] == 1.0


def test_real_producer_record_accept_binds_every_field_and_projects_system_attribution(submitted):
    e = submitted()
    assert e.commit.domain_for(e.mission.id).version == "2"
    revision = e.store.get_claim(ids.claim_id(e.envelope.id, 1)).version
    layer = produce(e)
    assert "check_spec_ids" not in layer.detail["assessment_binding"]
    assert layer.status == "PASS"
    assert len(layer.detail["criterion_verdicts"]) == 2
    assert e.store.list_criterion_assessments(e.mission.id) == []
    assert accept(e).status is TaskStatus.COMPLETED
    [row] = e.store.list_criterion_assessments(e.mission.id, result_id=e.envelope.id)
    assert all(
        value is not None and value != "" and value != [] and value != {} for value in row.values()
    )
    assert row["claim_id"] == ids.claim_id(e.envelope.id, 1)
    assert row["claim_revision"] == revision
    assert row["output_ref"] == e.envelope.id
    assert row["source_versions"] == {PATH: e.citation.version}
    assert row["verifier_adapter_id"] == "citation_integrity" and row["version"] == "1"
    assert row["evidence_refs"][0]["locator"] == {"start_line": 3, "end_line": 3}
    claim = e.store.get_claim(row["claim_id"])
    assert claim.status is ClaimStatus.VERIFIED
    assert claim.content == f"《{PATH}》@{e.citation.version[:8]} #L3-L3 记载：「{QUOTE}」"
    assert claim.key == f"attribution:{e.citation.version}:3-3"
    assert claim.type == "attribution" and claim.stance == "affirms"
    assert e.store.get_knowledge(claim.id).content == claim.content
    assert_replay(e)


@pytest.mark.parametrize(
    "tamper",
    [
        "task_contract_revision",
        "claim_revision",
        "output_hash",
        "output_ref",
        "tenant_id",
        "attempt_id",
        "source_versions",
        "receipt_id",
        "criterion_verdicts",
    ],
)
def test_accept_refuses_tampered_recorded_evaluation_without_any_commit(submitted, tamper):
    e = submitted()
    layer = produce(e)
    detail = deepcopy(dict(layer.detail))
    assessment = detail["criterion_assessments"][0]
    if tamper == "criterion_verdicts":
        detail["criterion_verdicts"].pop()
    elif tamper in {"tenant_id", "attempt_id"}:
        assessment["provenance"][tamper] = "another"
    elif tamper == "source_versions":
        assessment[tamper] = {PATH: "f" * 64}
    else:
        assessment[tamper] = 99 if tamper == "claim_revision" else "forged"
    e.commit.record_verification_layer(
        e.envelope.id, layer="rule_check", status="PASS", detail=detail
    )
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(CommitRejected):
        accept(e)
    assert e.store.snapshot(e.mission.id) == before
    assert e.store.list_criterion_assessments(e.mission.id) == []
    assert e.store.list_knowledge(e.mission.id) == []


@pytest.mark.parametrize("recorded", [False, True])
def test_caller_pass_or_old_rule_pass_cannot_substitute_for_producer_receipt(submitted, recorded):
    e = submitted()
    if recorded:
        e.commit.record_verification_layer(
            e.envelope.id,
            layer="rule_check",
            status="PASS",
            detail={"verifier_version": "verifier-v1"},
        )
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(CommitRejected):
        e.commit.accept_result(
            e.envelope.id, verifier_results=({"layer": "rule_check", "status": "PASS"},)
        )
    assert e.store.snapshot(e.mission.id) == before
    # A pending legacy row can be replaced only by a real new check.
    assert produce(e).status == "PASS"
    assert accept(e).status is TaskStatus.COMPLETED


@pytest.mark.parametrize("damage", ["claim_revision", "artifact_hash", "task_contract"])
def test_accept_rechecks_live_binding_against_the_recorded_assessment(submitted, damage):
    e = submitted()
    produce(e)
    if damage == "claim_revision":
        claim = e.store.get_claim(ids.claim_id(e.envelope.id, 1))
        e.store.upsert_claim(replace(claim, version=claim.version + 1))
    elif damage == "artifact_hash":
        # Artifact IDs are immutable: upsert intentionally ignores an existing ID.
        # Explicitly corrupt the persisted row to test accept's fresh binding read.
        changed = replace(e.artifact, content_hash=e.cas.put_bytes(b"changed\n"))
        with e.store.transaction() as connection:
            connection.execute(
                "UPDATE artifacts SET content_hash = ?, json = ? WHERE artifact_id = ?",
                (changed.content_hash, json.dumps(changed.to_json()), changed.id),
            )
        assert e.store.get_artifact(e.artifact.id).content_hash == changed.content_hash
        assert changed.content_hash != e.artifact.content_hash
    else:
        task = e.store.get_task(e.task.id)
        e.store.update_task(
            replace(task, version=task.version + 1, goal="合同已变"), expected_version=task.version
        )
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(CommitRejected):
        accept(e)
    assert e.store.snapshot(e.mission.id) == before


def test_assessment_claim_and_knowledge_roll_back_together_on_event_failure(submitted, monkeypatch):
    e = submitted()
    produce(e)
    before = e.store.snapshot(e.mission.id)
    append = e.store.append_event

    def fail(event):
        if event.type == "KnowledgeCommitted":
            raise RuntimeError("injected projection failure")
        return append(event)

    with monkeypatch.context() as patch:
        patch.setattr(e.store, "append_event", fail)
        with pytest.raises(RuntimeError, match="injected projection failure"):
            accept(e)
    assert e.store.snapshot(e.mission.id) == before
    assert e.store.list_criterion_assessments(e.mission.id) == []
    accept(e)
    assert_replay(e)


@pytest.mark.parametrize("bad", ["missing", "quote", "mixed", "unbound"])
def test_failed_doc_citations_are_unsupported_not_rejected_and_have_no_assessment_rows(
    submitted, bad
):
    def proposals(citation):
        citations = () if bad == "missing" else (replace(citation, quote="不存在的原文。"),)
        if bad == "mixed":
            citations = (citation, replace(citation, quote="不存在的原文。"))
        if bad == "unbound":
            citations = (citation,)
        return (ClaimProposal(content=QUOTE, confidence=0.8, citations=citations),)

    e = submitted(
        criteria=("file:REPORT.md", "完全不关联的自由准则") if bad == "unbound" else None,
        proposals=proposals,
    )
    layer = produce(e)
    assert layer.status == "FAIL"
    with pytest.raises(CommitRejected):
        accept(e)
    e.commit.fail_result(e.envelope.id, failures=(layer.to_json(),))
    claim = e.store.get_claim(ids.claim_id(e.envelope.id, 1))
    assert claim.status is ClaimStatus.UNDER_REVIEW
    assert claim.confidence_metadata["grade"] == "unsupported"
    assert e.store.list_knowledge(e.mission.id) == []
    assert e.store.list_criterion_assessments(e.mission.id) == []
    assert e.store.list_verifications(e.envelope.id)[0]["detail"] == dict(layer.detail)
    assert_replay(e)


def test_accepted_history_is_immutable_across_duplicate_calls_and_reopen(submitted):
    e = submitted()
    layer = produce(e)
    accept(e)
    before = e.store.snapshot(e.mission.id)
    accept(e)
    e.commit.record_verification_layer(
        e.envelope.id, layer="rule_check", status="PASS", detail=layer.detail
    )
    for status, detail in [("FAIL", layer.detail), ("PASS", {"summary": "rewritten"})]:
        with pytest.raises(CommitRejected):
            e.commit.record_verification_layer(
                e.envelope.id, layer="rule_check", status=status, detail=detail
            )
    with pytest.raises(CommitRejected):
        e.commit.fail_result(e.envelope.id, failures=())
    other = Store.open(e.store.path)
    try:
        again = CommitService(other, artifact_store=e.cas)
        again.accept_result(e.envelope.id, verifier_results=())
        assert other.snapshot(e.mission.id) == before
    finally:
        other.close()
    assert e.store.snapshot(e.mission.id) == before


def test_schema9_readonly_and_upgrade_do_not_backfill_assessments(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:9])
        old = Store.open(path)
        mission, _ = CommitService(old).create_mission(
            MissionSpec(
                goal="old", success_criteria=("file:x",), tenant_id="t", idempotency_key="old"
            )
        )
        old.close()
    readonly = Store.open_readonly(path)
    try:
        assert readonly.list_criterion_assessments(mission.id) == []
        before = readonly.snapshot(mission.id)
    finally:
        readonly.close()
    upgraded = Store.open(path)
    try:
        assert upgraded.has_table("criterion_assessments")
        assert upgraded.snapshot(mission.id) == before
    finally:
        upgraded.close()


def test_code_accept_keeps_legacy_grading_and_requires_no_assessment(submitted):
    e = submitted(
        domain=CODE_DOMAIN,
        criteria=("file:REPORT.md",),
        proposals=lambda citation: (ClaimProposal(content="legacy", confidence=0.8),),
    )
    assert accept(e).status is TaskStatus.COMPLETED
    assert e.store.get_claim(ids.claim_id(e.envelope.id, 1)).status is ClaimStatus.SUPPORTED
    assert e.store.list_criterion_assessments(e.mission.id) == []


def test_store_assessment_receipt_is_idempotent_and_cannot_cross_scope(submitted):
    e = submitted()
    layer = produce(e)
    assessment = CriterionAssessmentV1.from_json(layer.detail["criterion_assessments"][0])
    accept(e)
    before = e.store.snapshot(e.mission.id)
    assert not e.store.insert_criterion_assessment(
        mission_id=e.mission.id, task_id=e.task.id, result_id=e.envelope.id, assessment=assessment
    )
    with pytest.raises(StoreConflict):
        e.store.insert_criterion_assessment(
            mission_id="another", task_id=e.task.id, result_id=e.envelope.id, assessment=assessment
        )
    assert e.store.snapshot(e.mission.id) == before


def test_attribution_cannot_explicitly_dispute_legacy_world_knowledge_at_accept(submitted):
    """Mixed-provenance boundary fixture, not a supported mixed-domain Mission API.

    Establish a real accepted target, then represent a historical code/world row.
    The new source claim must not gain authority over that row by naming its ID.
    """
    first = submitted(siblings=True)
    produce(first)
    accept(first)
    old_id = ids.claim_id(first.envelope.id, 1)
    old = first.store.get_claim(old_id)
    knowledge = first.store.get_knowledge(old_id)
    legacy_basis = {
        "layer": "code_test",
        "target": "tests/world.py",
        "evidence": "pytest:tests/world.py",
    }
    first.store.upsert_claim(
        replace(
            old,
            content="程序返回已通过代码验证的值",
            type="statement",
            key="world.result",
            confidence_metadata={"grade": "verified", "basis": legacy_basis},
        )
    )
    first.store.upsert_knowledge(
        replace(
            knowledge,
            content="程序返回已通过代码验证的值",
            type="statement",
            key="world.result",
            verifier=legacy_basis,
        )
    )
    before_claim = first.store.get_claim(old_id)
    before_knowledge = first.store.get_knowledge(old_id)
    before_conflicts = first.store.list_conflicts(first.mission.id)
    second = submitted(
        prior=first,
        proposals=lambda citation: (
            ClaimProposal(
                content=QUOTE,
                confidence=0.9,
                citations=(citation,),
                contradicts=(old_id,),
                key="world.result",
                stance="refutes",
            ),
        ),
    )
    assert produce(second).status == "PASS"
    accept(second)
    candidate = second.store.get_claim(ids.claim_id(second.envelope.id, 1))
    assert candidate.status is ClaimStatus.VERIFIED
    assert candidate.contradicts == ()
    assert candidate.confidence_metadata["contradicts_rejected"] == [old_id]
    assert second.store.get_claim(old_id) == before_claim
    assert second.store.get_knowledge(old_id) == before_knowledge
    assert second.store.get_claim(old_id).disputed_by == ()
    assert second.store.get_knowledge(old_id).disputed_by == ()
    assert second.store.list_conflicts(first.mission.id) == before_conflicts
    assert second.store.count_events(first.mission.id, "ClaimDisputed") == 0


@pytest.mark.parametrize("relation", ["same_identity", "same_key_other_sentence", "different_key"])
def test_source_supersession_requires_same_key_and_exact_sentence_identity(submitted, relation):
    # Two complete sentences on one physical line intentionally share the mandated key.
    first_quote, other_quote, next_line = "甲方案可用。", "乙方案受限。", "丙方案待定。"
    source = f"# 原始说明\n\n{first_quote}{other_quote}\n\n{next_line}\n"
    first = submitted(
        siblings=True,
        source=source,
        proposals=lambda citation: (
            ClaimProposal(
                content=first_quote,
                confidence=0.9,
                citations=(replace(citation, quote=first_quote),),
            ),
        ),
    )
    assert produce(first).status == "PASS"
    accept(first)
    old_id = ids.claim_id(first.envelope.id, 1)
    before_claim = first.store.get_claim(old_id)
    before_knowledge = first.store.get_knowledge(old_id)
    before_conflicts = first.store.list_conflicts(first.mission.id)
    quote = (
        first_quote
        if relation == "same_identity"
        else (other_quote if relation == "same_key_other_sentence" else next_line)
    )
    line = 5 if relation == "different_key" else 3
    second = submitted(
        prior=first,
        proposals=lambda citation: (
            ClaimProposal(
                content=quote,
                confidence=0.9,
                supersedes=old_id,
                citations=(replace(citation, start_line=line, end_line=line, quote=quote),),
            ),
        ),
    )
    assert produce(second).status == "PASS"
    accept(second)
    candidate = second.store.get_claim(ids.claim_id(second.envelope.id, 1))
    assert candidate.status is ClaimStatus.VERIFIED
    if relation == "same_identity":
        assert candidate.key == before_claim.key
        assert candidate.supersedes == old_id
        assert second.store.get_claim(old_id).status is ClaimStatus.SUPERSEDED
        assert second.store.get_knowledge(old_id).superseded_by == candidate.id
        assert second.store.count_events(first.mission.id, "KnowledgeSuperseded") == 1
    else:
        assert (candidate.key == before_claim.key) == (relation == "same_key_other_sentence")
        assert candidate.supersedes is None
        assert "supersedes_rejected" in candidate.confidence_metadata
        assert second.store.get_claim(old_id) == before_claim
        assert second.store.get_knowledge(old_id) == before_knowledge
        assert second.store.count_events(first.mission.id, "KnowledgeSuperseded") == 0
    assert second.store.list_conflicts(first.mission.id) == before_conflicts
    assert_replay(second)
