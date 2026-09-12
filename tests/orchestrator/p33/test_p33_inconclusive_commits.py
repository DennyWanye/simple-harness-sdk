# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""D commit oracle, authored before implementation; the main task runs pytest.

Real source registration, dispatch, recorded producer and acceptance establish facts.
Full limitations accept immediately; only missing limitations on an otherwise sound
INCONCLUSIVE consumes the bounded rework allowance. Human permission must bind the
same result. Mission coverage uses the original catalogue and is reread in the stop
transaction. No hand-authored assessment PASS/receipt and no alternate state machine.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.contracts import (
    Artifact,
    AttemptStatus,
    Budget,
    ClaimProposal,
    ClaimStatus,
    MissionStatus,
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
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionSpec,
    Reservation,
)
from agent_orchestrator.storage.store import Store
from agent_orchestrator.verification.assessments import (
    assessment_binding_for,
    citation_integrity,
    criterion_id,
    task_contract_revision,
)
from agent_orchestrator.verification.deterministic_checks import rule_check
from agent_orchestrator.verification.evidence_resolver import EvidenceResolver
from agent_orchestrator.verification.human_review import human_layer, review_request_id

PATH = "sources/observations.md"
QUOTE = "资料仅记录隔离环境中的观察。"
CRITERION = "确认方案适用于生产"


def contract_for(task):
    return {
        "task_id": task.id,
        "kind": task.kind,
        "goal": task.goal,
        "rationale": task.rationale,
        "success_criteria": list(task.success_criteria),
        "verification_policy": list(task.verification_policy),
        "outputs": list(task.outputs),
    }


@pytest.fixture
def scenes(tmp_path):
    opened = []

    def make(
        *,
        mission_criteria=(CRITERION,),
        task_criteria=(CRITERION,),
        tasks=1,
        domain=DOC_DOMAIN,
        profile=None,
    ):
        root = tmp_path / str(len(opened))
        root.mkdir()
        store = Store.open(root / "orchestrator.db", clock=lambda: 1_000.0)
        opened.append(store)
        cas = ArtifactStore(root / "artifacts")
        commit = CommitService(
            store, artifact_store=cas, deployed_layers=frozenset(DOC_PROFILE.runs_layers)
        )
        mission, _ = commit.create_mission(
            MissionSpec(
                goal="来源完整但结论仍不确定",
                success_criteria=mission_criteria,
                tenant_id="tenant",
                idempotency_key="d-oracle",
                domain=domain,
                budget=Budget(max_tokens=200_000, max_attempts=20),
            )
        )
        if profile is not None:
            # Explicit deployment-history boundary fixture: replace the newly bound
            # profile before any Task/Attempt exists, never migrate an old intent.
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE mission_domains SET domain_version=?, json=? WHERE mission_id=?",
                    (profile.version, json.dumps(profile.to_json()), mission.id),
                )
        version = cas.put_bytes((QUOTE + "\n").encode())
        if domain == DOC_DOMAIN:
            commit.register_source(
                mission_id=mission.id,
                tenant_id=mission.tenant_id,
                principal=Principal("human"),
                path=PATH,
                content=QUOTE + "\n",
                kind="markdown",
                idempotency_key="source",
            )
        planning = commit.begin_planning(mission.id)
        nodes = [
            {
                "key": f"T{n}",
                "goal": f"核对候选{n}",
                "rationale": f"独立候选{n}",
                "success_criteria": list(task_criteria),
                "verification_policy": ["format_check", "rule_check", "critic_review"],
                "allowed_tools": [],
                "dependencies": [],
                "budget": {"max_tokens": 20_000, "max_attempts": 10},
            }
            for n in range(tasks)
        ]
        actual_tasks, _ = commit.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json({"tasks": nodes}),
            base_version=planning.version,
            source={"planner": "oracle"},
        )
        return SimpleNamespace(
            root=root,
            store=store,
            cas=cas,
            commit=commit,
            mission=store.get_mission(mission.id),
            tasks=actual_tasks,
            version=version,
        )

    yield make
    for store in opened:
        store.close()


def dispatch(s, *, task_index=0, config_overrides=None, candidates=1):
    task = s.store.get_task(s.tasks[task_index].id)
    contract = contract_for(task)
    config = {
        "agent_config": {},
        "task_contract": contract,
        "message": {"content": json.dumps({"task_contract": contract})},
        "source_versions": {PATH: s.version},
        "source_roots": ["sources/"],
        **(config_overrides or {}),
    }
    return s.commit.create_attempt(
        task.id,
        role="worker",
        model="fixture",
        prompt_version="fixture-v1",
        context_version="fixture-v1",
        reservation=Reservation(1_000, 0),
        intent_config=config,
        input_hash="fixture",
        candidates_per_task=candidates,
    )


def submitted(
    s,
    *,
    task_index=0,
    full=True,
    bad=None,
    attempt_pair=None,
    content="这些资料尚不足以确定生产适用性",
    key=None,
    stance="affirms",
    candidates=True,
    mission_candidates=True,
):
    from agent_orchestrator.contracts import LimitationV1
    from agent_orchestrator.verification.assessments import mission_criterion_catalog

    task = s.store.get_task(s.tasks[task_index].id)
    revision = task_contract_revision(contract_for(task))
    task_ids = tuple(
        criterion_id(revision, n, text)
        for n, text in enumerate(task.success_criteria, 1)
        if not text.startswith("file:")
    )
    mission_ids = tuple(
        row["criterion_id"]
        for row in mission_criterion_catalog(s.mission)
        if row["kind"] not in {"file", "action", "arbitration"}
    )
    attempt, intent = attempt_pair or dispatch(s, task_index=task_index)
    turn = "turn:" + attempt.id
    s.commit.claim_intent(intent.intent_id, owner="fixture", lease_seconds=60)
    s.commit.record_agent_created(
        intent.intent_id, agent_id="agent:" + attempt.id, expected_turn_id=turn
    )
    s.commit.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
    citation = SourceCitation(PATH, s.version, 1, 1, QUOTE)
    citations = () if bad == "missing" else (citation,)
    if bad == "quote":
        citations = (replace(citation, quote="并不存在的完整句。"),)
    if bad == "mixed":
        citations = (citation, replace(citation, quote="并不存在的完整句。"))
    proposal = ClaimProposal(
        content=content,
        confidence=0.9,
        citations=citations,
        key=key,
        stance=stance,
        criterion_ids=task_ids if candidates else (),
        mission_criterion_ids=mission_ids if mission_candidates else (),
    )
    limits = tuple(LimitationV1(cid, "claim:1", "缺少生产环境的独立证据") for cid in task_ids)
    data = b"analysis\n"
    digest = s.cas.put_bytes(data)
    artifact = Artifact(
        id=ids.artifact_id(attempt.id, "REPORT.md", digest),
        mission_id=s.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path="REPORT.md",
        version=1,
        content_hash=digest,
        size_bytes=len(data),
        produced_by="agent:" + attempt.id,
        storage_uri=str(s.cas.path_for(digest)),
    )
    envelope = ResultEnvelope(
        id="result:" + attempt.id,
        mission_id=s.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="有据的局限",
        claims=(proposal,),
        evidence=("artifact:REPORT.md",),
        artifacts=("REPORT.md",),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
        limitations=limits if full else (),
    )
    s.commit.record_result(
        attempt.id, envelope=envelope, turn_id=turn, artifacts=(artifact,), usage_refs=()
    )
    s.commit.start_verification(envelope.id)
    root = s.root / attempt.id.replace(":", "_")
    root.mkdir()
    (root / "REPORT.md").write_bytes(data)
    return SimpleNamespace(
        **vars(s),
        task=task,
        attempt=attempt,
        envelope=envelope,
        artifact=s.store.get_artifact(artifact.id),
        workspace=Workspace(root, attempt.id, False, s.cas),
    )


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


def accept(e, *, caller=()):
    return e.commit.accept_result(e.envelope.id, verifier_results=caller)


def fail(e, layer):
    e.commit.fail_result(e.envelope.id, failures=(layer.to_json(),))


def replay(s):
    folded = Projection().feed(events_from_store(s.store, s.mission.id))
    assert dict(folded.unknown) == {}
    report = compare(folded.objects, formal_from_snapshot(s.store.snapshot(s.mission.id)))
    assert report["mismatches"] == [] and report["coverage"] == 1.0


def test_full_limitations_accept_first_result_without_rework_or_human(scenes):
    e = submitted(scenes())
    layer = produce(e)
    assert layer.status == "PASS"  # acceptance gate, not a claim proof
    assert {a["verdict"] for a in layer.detail["criterion_assessments"]} == {"INCONCLUSIVE"}
    assert accept(e).status is TaskStatus.COMPLETED
    claim = e.store.get_claim(ids.claim_id(e.envelope.id, 1))
    assert claim.status is ClaimStatus.UNDER_REVIEW
    assert claim.confidence_metadata["grade"] == "insufficient_evidence"
    assert e.store.list_knowledge(e.mission.id) == []
    assert len(e.store.list_attempts(e.task.id)) == 1
    assert e.store.list_approvals(e.mission.id) == []
    [row] = e.store.list_criterion_assessments(e.mission.id)
    assert row["verdict"] == "INCONCLUSIVE" and row["version"] == "2"
    replay(e)


def test_missing_limitations_count_actual_failures_allow_one_retry_then_stop(scenes, monkeypatch):
    from agent_orchestrator.orchestrator.commit_service import InconclusiveRetryExhausted

    s = scenes()
    for expected in (1, 2):
        e = submitted(s, full=False)
        layer = produce(e)
        assert layer.status == "FAIL"
        with pytest.raises(CommitRejected):
            accept(e)
        fail(e, layer)
        assert e.store.get_attempt(e.attempt.id).failure["reason"] == "inconclusive"
        assert e.commit.inconclusive_failure_count(e.task.id) == expected
        before = e.store.snapshot(e.mission.id)
        fail(e, layer)
        assert e.store.snapshot(e.mission.id) == before
        reopened = Store.open(s.store.path)
        try:
            assert (
                CommitService(reopened, artifact_store=s.cas).inconclusive_failure_count(e.task.id)
                == expected
            )
        finally:
            reopened.close()
    before = s.store.snapshot(s.mission.id)
    with monkeypatch.context() as patch:
        patch.setattr(
            s.commit.ledger, "reserve", lambda **kwargs: pytest.fail("budget touched after cap")
        )
        with pytest.raises(InconclusiveRetryExhausted) as caught:
            dispatch(s)  # deliberately omits retry_of; cannot evade historical count
    assert caught.value.failure_count == 2 and caught.value.retry_limit == 1
    assert caught.value.task_id == e.task.id
    assert s.store.snapshot(s.mission.id) == before
    assert s.commit.stop_inconclusive_task(e.task.id)
    mission = s.store.get_mission(s.mission.id)
    assert mission.status is MissionStatus.FAILED and mission.stop_reason == "insufficient_evidence"
    assert mission.final_report["result"] == "INSUFFICIENT"
    assert s.store.get_task(e.task.id).status is TaskStatus.FAILED
    assert s.store.count_events(s.mission.id, "ManagementRequested") == 0
    before = s.store.snapshot(s.mission.id)
    assert not s.commit.stop_inconclusive_task(e.task.id)
    assert s.store.snapshot(s.mission.id) == before
    replay(s)


def test_missing_limitations_rework_can_finish_with_full_limitations(scenes):
    s = scenes()
    first = submitted(s, full=False)
    fail(first, produce(first))
    second = submitted(s)
    produce(second)
    assert accept(second).status is TaskStatus.COMPLETED
    assert second.commit.inconclusive_failure_count(second.task.id) == 1
    assert not second.commit.stop_inconclusive_task(second.task.id)


@pytest.mark.parametrize("bad", ["missing", "quote", "mixed", "no_candidate"])
def test_hard_citation_or_binding_failures_never_consume_uncertainty_allowance(scenes, bad):
    e = submitted(scenes(), full=False, bad=bad, candidates=bad != "no_candidate")
    layer = produce(e)
    assert layer.status == "FAIL"
    fail(e, layer)
    assert e.store.get_attempt(e.attempt.id).failure["reason"] == "verification_failed"
    assert e.commit.inconclusive_failure_count(e.task.id) == 0
    assert not e.commit.stop_inconclusive_task(e.task.id)


def test_exhausted_rework_does_not_cancel_an_existing_sibling(scenes):
    s = scenes()
    first = submitted(s, full=False)
    fail(first, produce(first))
    second_pair = dispatch(s, candidates=2)
    sibling_pair = dispatch(s, candidates=2)
    second = submitted(s, full=False, attempt_pair=second_pair)
    fail(second, produce(second))
    assert s.commit.inconclusive_failure_count(second.task.id) == 2
    assert not s.commit.stop_inconclusive_task(second.task.id)
    assert s.store.get_attempt(sibling_pair[0].id).status is AttemptStatus.PENDING
    sibling = submitted(s, attempt_pair=sibling_pair)
    produce(sibling)
    assert accept(sibling).status is TaskStatus.COMPLETED
    assert not s.commit.stop_inconclusive_task(sibling.task.id)


def test_doc3_dispatch_freezes_authoritative_checks_and_mission_catalogue(scenes):
    from agent_orchestrator.verification.assessments import (
        mission_contract_revision,
        mission_criterion_catalog,
    )

    s = scenes()
    _, intent = dispatch(s)
    assert intent.config["check_spec_ids"] == sorted(
        s.commit.domain_for(s.mission.id).adapters.values()
    )
    assert intent.config["mission_contract_revision"] == mission_contract_revision(s.mission)
    assert intent.config["mission_criteria"] == list(mission_criterion_catalog(s.mission))


@pytest.mark.parametrize(
    "field,value",
    [
        ("check_spec_ids", []),
        ("mission_contract_revision", "0" * 64),
        ("mission_criteria", []),
    ],
)
def test_conflicting_dispatch_freeze_rejected_before_budget_or_intent(
    scenes, monkeypatch, field, value
):
    s = scenes()
    before = s.store.snapshot(s.mission.id)
    with monkeypatch.context() as patch:
        patch.setattr(
            s.commit.ledger, "reserve", lambda **kwargs: pytest.fail("budget touched on conflict")
        )
        with pytest.raises(CommitRejected):
            dispatch(s, config_overrides={field: value})
    assert s.store.snapshot(s.mission.id) == before


@pytest.mark.parametrize("version", ["code", "1", "2"])
def test_legacy_dispatch_does_not_backfill_new_freeze_fields(scenes, version):
    profile = None if version == "code" else replace(DOC_PROFILE, version=version, adapters={})
    s = scenes(domain=CODE_DOMAIN if version == "code" else DOC_DOMAIN, profile=profile)
    _, intent = dispatch(s)
    assert not {"check_spec_ids", "mission_contract_revision", "mission_criteria"} & set(
        intent.config
    )
    assert intent.config["task_contract"] == contract_for(s.tasks[0])


def human_rule(e, monkeypatch):
    from agent_orchestrator.verification import adapters

    with monkeypatch.context() as patch:
        patch.setattr(adapters, "coverage_verdict", lambda **kwargs: "NEEDS_HUMAN")
        layer = produce(e)
    assert layer.status == "NEEDS_HUMAN"
    return layer


def approve_review(e, layer, *, record=True):
    request = e.commit.suspend_verification(
        e.envelope.id,
        owner=None,
        reason="needs_human",
        layers=(layer.to_json(),),
    )
    decided, _ = e.commit.review_result(
        request["request_id"],
        principal=Principal("human"),
        verdict="pass",
        note="接受标明局限的结果",
        nonce="human-decision",
    )
    actual = human_layer(
        {
            "verdict": "PASS",
            "note": decided["note"],
            "principal": decided["decided_by"],
            "request_id": decided["request_id"],
        }
    )
    if record:
        e.commit.record_verification_layer(
            e.envelope.id,
            layer=actual.layer,
            status=actual.status,
            detail=actual.detail,
        )
    return actual


def test_needs_human_accept_requires_real_same_result_approval_after_reopen(scenes, monkeypatch):
    e = submitted(scenes())
    layer = human_rule(e, monkeypatch)
    approve_review(e, layer)
    reopened = Store.open(e.store.path)
    try:
        commit = CommitService(reopened, artifact_store=e.cas)
        task = commit.accept_result(e.envelope.id, verifier_results=())
        assert task.status is TaskStatus.COMPLETED
        claim = reopened.get_claim(ids.claim_id(e.envelope.id, 1))
        assert claim.status is ClaimStatus.UNDER_REVIEW
        assert claim.confidence_metadata["grade"] == "insufficient_evidence"
        assert reopened.list_knowledge(e.mission.id) == []
        before = reopened.snapshot(e.mission.id)
        commit.accept_result(e.envelope.id, verifier_results=())
        assert reopened.snapshot(e.mission.id) == before
    finally:
        reopened.close()
    replay(e)


@pytest.mark.parametrize(
    "failed_layer,status", [("format_check", "FAIL"), ("critic_review", "ERROR")]
)
@pytest.mark.parametrize("approved_human", [False, True])
def test_recorded_hard_failure_blocks_accept_even_with_real_human_pass(
    scenes, monkeypatch, failed_layer, status, approved_human
):
    # Oracle: a genuine rule assessment and actual same-result human permission
    # cannot erase a different layer's hard failure at the direct commit boundary.
    e = submitted(scenes())
    if approved_human:
        layer = human_rule(e, monkeypatch)
        approve_review(e, layer)
    else:
        assert produce(e).status == "PASS"
    e.commit.record_verification_layer(
        e.envelope.id,
        layer=failed_layer,
        status=status,
        detail={"summary": "authentic verifier hard failure", "reason": "oracle_hard_failure"},
    )
    result = accept(e, caller=({"layer": failed_layer, "status": "PASS", "detail": {}},))
    assert result.status is TaskStatus.ACTIVE
    assert result.accepted_result_id is None
    attempt = e.store.get_attempt(e.attempt.id)
    assert attempt.status is AttemptStatus.RETRY_WAIT
    assert attempt.failure["reason"] == "verification_failed"
    assert any(
        row["layer"] == failed_layer and row["status"] == status
        for row in attempt.failure["failures"]
    )
    assert e.store.get_result(e.envelope.id).verdict == "FAIL"
    assert e.store.get_claim(ids.claim_id(e.envelope.id, 1)).status is ClaimStatus.UNDER_REVIEW
    assert e.store.list_criterion_assessments(e.mission.id, result_id=e.envelope.id) == []
    assert e.store.list_knowledge(e.mission.id) == []
    assert e.commit.inconclusive_failure_count(e.task.id) == 0
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(CommitRejected):
        accept(e)
    assert e.store.snapshot(e.mission.id) == before
    replay(e)


@pytest.mark.parametrize(
    "missing", ["approval", "human_row", "principal", "request_binding", "other_result"]
)
def test_needs_human_caller_pass_or_wrong_approval_cannot_accept(scenes, monkeypatch, missing):
    s = scenes(tasks=2)
    e = submitted(s)
    layer = human_rule(e, monkeypatch)
    caller = ({"layer": "human_review", "status": "PASS", "detail": {}},)
    if missing == "approval":
        # A recorded layer alone is also not an authenticated decision.
        s.commit.record_verification_layer(
            e.envelope.id,
            layer="human_review",
            status="PASS",
            detail={"request_id": review_request_id(e.envelope.id), "principal_id": "human"},
        )
    elif missing == "other_result":
        other = submitted(s, task_index=1)
        other_rule = human_rule(other, monkeypatch)
        other_pass = approve_review(other, other_rule)
        s.commit.record_verification_layer(
            e.envelope.id, layer="human_review", status="PASS", detail=other_pass.detail
        )
    else:
        actual = approve_review(e, layer, record=missing != "human_row")
        if missing == "principal":
            s.commit.record_verification_layer(
                e.envelope.id,
                layer="human_review",
                status="PASS",
                detail={**actual.detail, "principal_id": "another-human"},
            )
        elif missing == "request_binding":
            request = s.store.get_approval(review_request_id(e.envelope.id))
            s.store.put_approval(
                {**request, "binding": {**request["binding"], "attempt_id": "another-attempt"}}
            )
    before = s.store.snapshot(s.mission.id)
    with pytest.raises(CommitRejected):
        accept(e, caller=caller)
    assert s.store.snapshot(s.mission.id) == before
    assert s.store.list_criterion_assessments(s.mission.id, result_id=e.envelope.id) == []


@pytest.mark.parametrize(
    "numerator,denominator,insufficient", [(1, 3, False), (1, 2, False), (2, 3, True)]
)
def test_mission_original_catalogue_threshold_and_atomic_stop(
    scenes, numerator, denominator, insufficient
):
    criteria = tuple(f"{CRITERION}：范围{n}" for n in range(numerator)) + tuple(
        f"file:REPORT-{n}.md" for n in range(denominator - numerator)
    )
    e = submitted(scenes(mission_criteria=criteria))
    assert e.commit.stop_insufficient_mission(e.mission.id) is None
    produce(e)
    accept(e)
    before = e.store.snapshot(e.mission.id)
    stopped = e.commit.stop_insufficient_mission(e.mission.id)
    if not insufficient:
        assert stopped is None
        assert e.store.snapshot(e.mission.id) == before
        return
    assert stopped.status is MissionStatus.FAILED
    assert stopped.stop_reason == "insufficient_evidence"
    assert stopped.final_report["result"] == "INSUFFICIENT"
    coverage = stopped.final_report["document_coverage"]
    assert coverage["numerator"] == numerator and coverage["denominator"] == denominator
    assert len(coverage["criteria"]) == denominator
    assert len({row["criterion_id"] for row in coverage["criteria"]}) == denominator
    assert e.store.get_task(e.task.id).status is TaskStatus.COMPLETED
    before = e.store.snapshot(e.mission.id)
    e.commit.stop_insufficient_mission(e.mission.id)
    assert e.store.snapshot(e.mission.id) == before
    replay(e)


def test_mission_stop_rolls_back_coverage_and_terminal_event_together(scenes, monkeypatch):
    e = submitted(scenes())
    produce(e)
    accept(e)
    before = e.store.snapshot(e.mission.id)
    original = e.store.append_event

    def fail_event(event):
        if event.type == "MissionFailed":
            raise RuntimeError("injected mission stop failure")
        return original(event)

    with monkeypatch.context() as patch:
        patch.setattr(e.store, "append_event", fail_event)
        with pytest.raises(RuntimeError, match="injected mission stop failure"):
            e.commit.stop_insufficient_mission(e.mission.id)
    assert e.store.snapshot(e.mission.id) == before


def test_live_uncertainty_conflict_is_recorded_fail_not_accept_rejection_loop(scenes):
    s = scenes(tasks=2, task_criteria=("cite:" + PATH, CRITERION))
    # A supported statement first: citation/path binding supplies a real PASS.
    first = submitted(s, task_index=0, content=CRITERION, key="deployment", stance="affirms")
    produce(first)
    accept(first)
    before_claim = s.store.get_claim(ids.claim_id(first.envelope.id, 1))
    assert before_claim.status is ClaimStatus.SUPPORTED
    second = submitted(s, task_index=1, key="deployment", stance="refutes")
    produce(second)  # a real recorded evaluation before the commit's live conflict check
    result = accept(second)
    assert result.status is not TaskStatus.COMPLETED
    assert s.store.get_result(second.envelope.id).verdict == "FAIL"
    assert s.store.get_attempt(second.attempt.id).failure["reason"] == "verification_failed"
    assert s.commit.inconclusive_failure_count(second.task.id) == 0
    assert s.store.get_claim(before_claim.id) == before_claim
    assert s.store.list_criterion_assessments(s.mission.id, result_id=second.envelope.id) == []


def test_direct_judge_cannot_bypass_insufficient_with_caller_true(scenes):
    e = submitted(scenes())
    produce(e)
    accept(e)
    judgments = [{"criterion": CRITERION, "met": True, "judge": "critic_review"}]
    e.commit.record_criteria_judgment(e.mission.id, "cached", judgments, summary="caller says pass")
    result = e.commit.judge_mission(e.mission.id, judgments=judgments, summary="caller says pass")
    assert result.status is MissionStatus.FAILED
    assert result.stop_reason == "insufficient_evidence"
    assert result.final_report["result"] == "INSUFFICIENT"
    assert result.final_report["document_coverage"]["insufficient"]
    assert e.store.count_events(e.mission.id, "MissionCompleted") == 0
    replay(e)


def test_direct_judge_cannot_claim_missing_mission_binding_is_met(scenes):
    e = submitted(scenes(), mission_candidates=False)
    produce(e)
    accept(e)
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(CommitRejected):
        e.commit.judge_mission(
            e.mission.id, judgments=[{"criterion": CRITERION, "met": True}], summary="forged"
        )
    assert e.store.snapshot(e.mission.id) == before


def test_direct_judge_accepts_within_limit_and_retains_explicit_uncertainty(scenes):
    e = submitted(scenes(mission_criteria=(CRITERION, "file:REPORT.md")))
    produce(e)
    accept(e)
    result = e.commit.judge_mission(
        e.mission.id,
        judgments=[
            {"criterion": CRITERION, "met": True, "verdict": "INCONCLUSIVE"},
            {"criterion": "file:REPORT.md", "met": True, "judge": "rule_check"},
        ],
        summary="完成明确局限报告",
    )
    assert result.status is MissionStatus.COMPLETED
    coverage = result.final_report["document_coverage"]
    assert coverage["share"] == 0.5 and not coverage["insufficient"]
    assert coverage["criteria"][0]["verdict"] == "INCONCLUSIVE"
    assert coverage["criteria"][0]["limitations"]
    assert e.store.list_knowledge(e.mission.id) == []
    replay(e)
