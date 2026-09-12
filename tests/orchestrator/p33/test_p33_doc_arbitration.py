# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""E05/06/07/08 oracle, written before production implementation.

Two supported source-backed statements reach the actual accept conflict entrance.
The resulting system Conflict Task runs the actual verifier to human suspension.
Only one arbitration may exist for its frozen scope; no ordinary review can accept
the Task. Real facade keep/contextual/unresolved rulings preserve the original
claim grades and never mint knowledge. Membership changes invalidate old consent;
source revocation alone cannot erase a historical dispute. All decisions replay.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_p33_source_commits import (
    PATH,
    TEXT,
    accept,
    attach,
    change_source,
    produce,
    replay,
    submit,
    verify,
)
from test_p33_source_commits import (
    e_scenes as source_scenes,
)

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.contracts import AttemptStatus, ClaimStatus, MissionStatus, TaskStatus, ids
from agent_orchestrator.contracts.models import canonical_json
from agent_orchestrator.governance.domains import CODE_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.action_commits import ActionCommitError
from agent_orchestrator.orchestrator.commit_service import CommitRejected
from agent_orchestrator.storage.store import Store
from agent_orchestrator.verification.human_review import human_layer, review_request_id

KEY = "world.applicability"
SECOND = "sources/b.md"
THIRD = "sources/c.md"
SOURCES = {PATH: TEXT, SECOND: "乙条件下禁止使用。\n", THIRD: "丙条件下尚待核实。\n"}
e_scenes = source_scenes


def conflicted(factory, *, domain=None):
    s = factory(
        paths=(PATH, SECOND, THIRD), sources=SOURCES, **({"domain": domain} if domain else {})
    )
    a = submit(s, index=0, path=PATH, content="该方案适用", key=KEY, stance="affirms")
    produce(a)
    assert accept(a).status is TaskStatus.COMPLETED
    assert s.store.get_claim(ids.claim_id(a.envelope.id, 1)).status is ClaimStatus.SUPPORTED
    b = submit(s, index=1, path=SECOND, content="该方案不适用", key=KEY, stance="refutes")
    produce(b)
    assert accept(b).status is TaskStatus.COMPLETED
    [conflict] = s.store.list_conflicts(s.mission.id)
    assert conflict["state"] == "OPEN"  # real reserve and deployed human layer, no DEFERRED
    assert s.store.get_task(conflict["task_id"]).status is TaskStatus.READY
    return s, a, b, conflict


def dossier(s, conflict):
    task = s.store.get_task(conflict["task_id"])
    assert "human_review" in task.verification_policy
    assert "code_test" not in task.verification_policy
    e = submit(s, task=task, citations=(), content="请按双方条件裁决，本文不证明世界结论", key=KEY)
    verdict = verify(e)
    assert verdict.suspended and not verdict.passed
    assert not verdict.failures
    assert next(row for row in verdict.layers if row.layer == "rule_check").status == "PASS"
    assert next(row for row in verdict.layers if row.layer == "human_review").status == "SUSPENDED"
    assert e.store.get_attempt(e.attempt.id).ordinal == 1
    return e, verdict


def suspend(e, verdict):
    return e.commit.suspend_verification(
        e.envelope.id, owner=None, reason="policy", layers=[row.to_json() for row in verdict.layers]
    )


def arbitrate(s, request, ruling="contextual", *, nonce="human-ruling"):
    return s.api.decide(
        request["request_id"],
        "arbitrate",
        ruling=ruling,
        basis="甲条件与乙条件不同；仅在各自所列条件下采用，不授予世界事实等级。",
        nonce=nonce,
    )


def members(s, conflict):
    return {cid: s.store.get_claim(cid).to_json() for cid in conflict["claim_ids"]}


def test_e05_nonoverlapping_source_scopes_are_annotations_not_conflict_exemptions(e_scenes):
    s, a, b, conflict = conflicted(e_scenes)
    assert {s.store.get_claim(cid).status for cid in conflict["claim_ids"]} == {
        ClaimStatus.DISPUTED
    }
    assert s.store.list_knowledge(s.mission.id) == []
    by_id = {side["claim_id"]: side for side in conflict["sides"]}
    for e in (a, b):
        cid = ids.claim_id(e.envelope.id, 1)
        side = by_id[cid]
        ref = e.envelope.claims[0].citations[0]
        assert side["source_versions"] == {ref.path: [ref.version]}
        assessments = s.store.list_criterion_assessments(s.mission.id, result_id=e.envelope.id)
        scopes = [row["checked_scope"] for row in assessments]
        assert scopes and side["checked_scope"] == scopes
        # Conditions in the actual quoted sentence survive, not just a model key.
        assert ref.quote in canonical_json(side)
    assert (
        by_id[ids.claim_id(a.envelope.id, 1)]["checked_scope"]
        != by_id[ids.claim_id(b.envelope.id, 1)]["checked_scope"]
    )
    replay(s)


def test_e05_legacy_code_missing_scope_still_conflicts_without_new_doc_side_fields(e_scenes):
    s, _, _, conflict = conflicted(e_scenes, domain=CODE_DOMAIN)
    assert all(
        "checked_scope" not in side and "source_versions" not in side for side in conflict["sides"]
    )
    assert len(conflict["claim_ids"]) == 2
    assert {s.store.get_claim(cid).status for cid in conflict["claim_ids"]} == {
        ClaimStatus.DISPUTED
    }
    replay(s)


def test_e06_first_human_wait_is_one_arbitration_with_final_member_versions(e_scenes):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    before_members = members(s, conflict)
    request = suspend(e, verdict)
    assert request["kind"] == "arbitration" and request["topic"] == "conflict"
    assert request["subject_key"] == conflict["conflict_id"]
    assert request["task_id"] == e.task.id
    assert request["binding"]["result_id"] == e.envelope.id
    assert request["binding"]["attempt_id"] == e.attempt.id
    assert set(request["options"]) == {
        *("keep:" + cid for cid in conflict["claim_ids"]),
        "contextual",
        "unresolved",
    }
    assert s.store.get_result(e.envelope.id).verification_state == "SUSPENDED"
    assert [r["kind"] for r in s.store.list_approvals(s.mission.id)] == ["arbitration"]
    assert s.store.get_approval(review_request_id(e.envelope.id)) is None
    before = s.store.snapshot(s.mission.id)
    assert suspend(e, verdict) == request
    assert s.store.snapshot(s.mission.id) == before
    # A successful fresh ruling also proves binding captured final DISPUTED/conflict_id
    # increments, rather than the older assessment's original claim_revision.
    arbitrate(s, request)
    assert members(s, conflict) == before_members
    assert s.store.list_knowledge(s.mission.id) == []
    replay(s)


@pytest.mark.parametrize("layer,status", [("format_check", "FAIL"), ("critic_review", "ERROR")])
def test_e06_actual_hard_failure_cannot_be_hidden_by_caller_pass_when_suspending(
    e_scenes, layer, status
):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    s.commit.record_verification_layer(
        e.envelope.id, layer=layer, status=status, detail={"reason": "real_hard_failure"}
    )
    before = s.store.snapshot(s.mission.id)
    with pytest.raises((CommitRejected, ActionCommitError)):
        suspend(e, verdict)  # caller still supplies the earlier PASS; stored row controls
    assert s.store.snapshot(s.mission.id) == before
    assert s.store.list_approvals(s.mission.id) == []


def test_e06_suspension_and_arbitration_request_roll_back_together(e_scenes, monkeypatch):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    before = s.store.snapshot(s.mission.id)
    original = s.store.append_event

    def broken(event):
        if event.type == "ApprovalRequested":
            raise RuntimeError("injected approval write failure")
        return original(event)

    with monkeypatch.context() as patch:
        patch.setattr(s.store, "append_event", broken)
        with pytest.raises(RuntimeError, match="injected approval"):
            suspend(e, verdict)
    assert s.store.snapshot(s.mission.id) == before
    assert s.store.get_result(e.envelope.id).verification_state == "RUNNING"


@pytest.mark.parametrize("ordinary_review", [False, True])
def test_e06_direct_conflict_accept_cannot_use_caller_or_real_ordinary_review_pass(
    e_scenes, ordinary_review
):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    if ordinary_review:
        # Imported pending pre-E review fixture. The decision itself is real Facade
        # authorization; such a legacy grant still has no conflict-resolving scope.
        rid = review_request_id(e.envelope.id)
        s.store.set_result_verification(e.envelope.id, state="SUSPENDED", verdict=None)
        s.store.put_approval(
            {
                "request_id": rid,
                "kind": "review",
                "mission_id": s.mission.id,
                "task_id": e.task.id,
                "subject_key": e.envelope.id,
                "state": "PENDING",
                "version": 1,
                "binding": {
                    "mission_id": s.mission.id,
                    "task_id": e.task.id,
                    "result_id": e.envelope.id,
                    "attempt_id": e.attempt.id,
                    "artifacts": [e.artifact.id],
                },
                "reason": "policy",
                "required_count": 1,
                "grant_count": 0,
                "granted_by": [],
                "expires_at": None,
                "created_at": 1000.0,
            }
        )
        s.api.decide(rid, "review_pass", note="只审核报告", nonce="ordinary-review")
        actual = human_layer({"verdict": "PASS", "request_id": rid, "principal": "reviewer"})
        s.commit.record_verification_layer(
            e.envelope.id, layer=actual.layer, status=actual.status, detail=actual.detail
        )
    before = s.store.snapshot(s.mission.id)
    with pytest.raises(CommitRejected):
        s.commit.accept_result(
            e.envelope.id,
            verifier_results=[
                {"layer": row.layer, "status": "PASS", "detail": dict(row.detail)}
                for row in verdict.layers
            ],
        )
    assert s.store.snapshot(s.mission.id) == before
    assert s.store.get_conflict(conflict["conflict_id"])["state"] == "OPEN"
    assert s.store.list_knowledge(s.mission.id) == []


@pytest.mark.parametrize("ruling", ["keep", "contextual", "unresolved"])
def test_e07_real_ruling_after_reopen_is_idempotent_and_never_promotes_claims(e_scenes, ruling):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    request = suspend(e, verdict)
    before_members = members(s, conflict)
    reopened = Store.open(s.store.path)
    try:
        restored = attach(s, reopened)
        value = "keep:" + conflict["claim_ids"][0] if ruling == "keep" else ruling
        arbitrate(restored, request, value)
        assert members(restored, conflict) == before_members
        assert restored.store.list_knowledge(s.mission.id) == []
        task = restored.store.get_task(e.task.id)
        if ruling == "unresolved":
            assert task.status is TaskStatus.FAILED
            assert restored.store.get_mission(s.mission.id).status is MissionStatus.FAILED
        else:
            assert task.status is TaskStatus.CANCELLED and task.accepted_result_id is None
            assert restored.store.get_attempt(e.attempt.id).status is AttemptStatus.CANCELLED
            assert restored.store.get_result(e.envelope.id).verification_state == "REJECTED"
            assert (
                restored.store.get_conflict(conflict["conflict_id"])["state"] == "RESOLVED_BY_HUMAN"
            )
            assert restored.store.count_events(s.mission.id, "ConflictResolvedByHuman") == 1
        before = restored.store.snapshot(s.mission.id)
        arbitrate(restored, request, value)
        assert restored.store.snapshot(s.mission.id) == before
        replay(restored)
    finally:
        reopened.close()


def test_e07_human_resolution_event_failure_rolls_back_decision_and_cancellation(
    e_scenes, monkeypatch
):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    request = suspend(e, verdict)
    before = s.store.snapshot(s.mission.id)
    original = s.store.append_event

    def broken(event):
        if event.type == "ConflictResolvedByHuman":
            raise RuntimeError("injected ruling event failure")
        return original(event)

    with monkeypatch.context() as patch:
        patch.setattr(s.store, "append_event", broken)
        with pytest.raises(RuntimeError, match="injected ruling"):
            arbitrate(s, request)
    assert s.store.snapshot(s.mission.id) == before
    assert s.store.list_decisions(request["request_id"]) == []


@pytest.mark.parametrize(
    "damage",
    [
        "member_revision",
        "artifact_bytes",
        "rule_receipt",
        "result_binding",
        "wrong_side",
        "cross_tenant",
    ],
)
def test_e08_stale_or_wrong_arbitration_binding_cannot_book_a_decision(e_scenes, damage):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    request = suspend(e, verdict)
    if damage == "member_revision":
        member = s.store.get_claim(conflict["claim_ids"][0])
        s.store.upsert_claim(replace(member, version=member.version + 1))
    elif damage == "artifact_bytes":
        path = s.cas.path_for(e.artifact.content_hash)
        path.chmod(0o600)
        path.write_bytes(b"changed after human saw it")
    elif damage == "rule_receipt":
        rule = next(
            r for r in s.store.list_verifications(e.envelope.id) if r["layer"] == "rule_check"
        )
        s.commit.record_verification_layer(
            e.envelope.id,
            layer="rule_check",
            status="PASS",
            detail={**rule["detail"], "criterion_verdicts": []},
        )
    elif damage == "result_binding":
        s.store.put_approval(
            {**request, "binding": {**request["binding"], "result_id": "foreign-result"}}
        )
    before = s.store.snapshot(s.mission.id)
    with pytest.raises(FacadeError):
        if damage == "cross_tenant":
            other = MissionControlV1(s.host, tenant_id="other", principal=Principal("other"))
            other.decide(
                request["request_id"],
                "arbitrate",
                ruling="contextual",
                basis="无权裁决",
                nonce="foreign",
            )
        else:
            arbitrate(s, request, "keep:foreign-claim" if damage == "wrong_side" else "contextual")
    assert s.store.snapshot(s.mission.id) == before
    assert s.store.list_decisions(request["request_id"]) == []


def test_e08_third_member_invalidates_old_consent_and_can_open_a_new_request(e_scenes):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    old = suspend(e, verdict)
    third = submit(s, index=2, path=THIRD, content="另一个适用判断", key=KEY, stance="affirms")
    assert produce(third).status == "PASS"
    assert accept(third).status is TaskStatus.COMPLETED
    current = s.store.get_conflict(conflict["conflict_id"])
    assert len(current["claim_ids"]) == len(current["sides"]) == 3
    assert {side["claim_id"] for side in current["sides"]} == set(current["claim_ids"])
    assert current["version"] > conflict["version"]
    with pytest.raises(FacadeError):
        arbitrate(s, old)
    assert s.store.list_decisions(old["request_id"]) == []
    new = suspend(e, verdict)
    assert new["request_id"] != old["request_id"]
    assert new["subject_key"] == old["subject_key"] == conflict["conflict_id"]
    assert s.store.get_approval(old["request_id"])["state"] == "CANCELLED"
    assert [
        r["request_id"] for r in s.store.list_approvals(s.mission.id) if r["state"] == "PENDING"
    ] == [new["request_id"]]
    assert "keep:" + ids.claim_id(third.envelope.id, 1) in new["options"]
    arbitrate(s, new)
    assert s.store.get_conflict(conflict["conflict_id"])["state"] == "RESOLVED_BY_HUMAN"
    replay(s)


def test_e08_source_revocation_does_not_change_historical_scope_or_cancel_pending_ruling(e_scenes):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    request = suspend(e, verdict)
    before_members = members(s, conflict)
    before_conflict = s.store.get_conflict(conflict["conflict_id"])
    change_source(s, mode="revoke")
    assert s.store.get_conflict(conflict["conflict_id"]) == before_conflict
    assert members(s, conflict) == before_members
    assert s.store.get_approval(request["request_id"])["state"] == "PENDING"
    arbitrate(s, request)
    assert s.store.get_source(s.mission.id, PATH) is None
    assert members(s, conflict) == before_members
    assert s.store.list_knowledge(s.mission.id) == []
    replay(s)


def test_e08_cancelled_conflict_task_cancels_its_pending_arbitration(e_scenes):
    s, _, _, conflict = conflicted(e_scenes)
    e, verdict = dossier(s, conflict)
    request = suspend(e, verdict)
    s.api.cancel(s.mission.id)
    assert s.store.get_approval(request["request_id"])["state"] == "CANCELLED"
    with pytest.raises(FacadeError):
        arbitrate(s, request)
    assert s.store.list_decisions(request["request_id"]) == []
    replay(s)


@pytest.mark.parametrize("topic", ["conflict", "judgment"])
def test_document_conflict_cannot_request_unbound_legacy_arbitration(e_scenes, topic):
    # A real OPEN conflict alone is not a reviewable Arbiter result. Even a
    # mislabeled topic must not bypass the frozen result/receipt entrance.
    s, _, _, conflict = conflicted(e_scenes)
    before = s.store.snapshot(s.mission.id)
    with pytest.raises(ActionCommitError, match="result"):
        s.commit.request_arbitration(
            s.mission.id,
            subject=conflict["conflict_id"],
            topic=topic,
            options=["keep:" + conflict["claim_ids"][0], "unresolved"],
            context={"sides": conflict["sides"]},
            task_id=conflict["task_id"],
        )
    assert s.store.snapshot(s.mission.id) == before
    assert s.store.list_approvals(s.mission.id) == []


def test_code_arbitration_keeps_fixed_request_identity_and_no_contextual_option(e_scenes):
    from agent_orchestrator.verification.human_review import arbitration_request_id

    s, _, _, conflict = conflicted(e_scenes, domain=CODE_DOMAIN)
    request, _ = s.commit.request_arbitration(
        s.mission.id,
        subject=conflict["conflict_id"],
        topic="conflict",
        options=[*("keep:" + cid for cid in conflict["claim_ids"]), "unresolved"],
        context={"sides": conflict["sides"]},
        task_id=conflict["task_id"],
    )
    assert request["request_id"] == arbitration_request_id(conflict["conflict_id"])
    assert "contextual" not in request["options"]
    before = s.store.snapshot(s.mission.id)
    with pytest.raises(FacadeError):
        arbitrate(s, request)
    assert s.store.snapshot(s.mission.id) == before
