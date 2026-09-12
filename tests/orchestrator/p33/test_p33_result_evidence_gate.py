# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P33-08/09 · 先写 oracle，再接实际 record_result 的集中领域闸门。

合法 Mission/Task/Attempt 经公共 commit 入口推进至 RUNNING，产物来自真实 CAS。
文档信封顶层或任意 claim 的 pytest:/tool-run: 都须在写入前拒绝，包括被 claim
自己的 evidence 遮蔽的顶层证据。拒绝后完整快照不变；同一 Attempt 改为合法证据
可提交，重复投递幂等。code-v1 的旧证据字符串不作全域收紧，source/knowledge 的
种类放行不冒称来源解析或 KnowledgeIndex 检查已经通过。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import (
    Artifact,
    AttemptStatus,
    Budget,
    ClaimProposal,
    ResultEnvelope,
)
from agent_orchestrator.governance.domains import CODE_DOMAIN, DOC_DOMAIN, DOC_PROFILE
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionSpec,
    Reservation,
    TaskProposal,
)
from agent_orchestrator.storage.store import Store


@pytest.fixture
def running(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db", clock=lambda: 1_000.0)
    cas = ArtifactStore(tmp_path / "artifacts")
    service = CommitService(
        store,
        artifact_store=cas,
        deployed_layers=frozenset(DOC_PROFILE.runs_layers) | {"code_test"},
    )

    def make(domain):
        mission, _ = service.create_mission(
            MissionSpec(
                goal="核对报告",
                success_criteria=("file:REPORT.md",),
                tenant_id="tenant",
                idempotency_key=domain,
                domain=domain,
                budget=Budget(max_tokens=20_000, max_attempts=3),
            )
        )
        planning = service.begin_planning(mission.id)
        task, _ = service.commit_task_proposal(
            mission.id,
            TaskProposal(
                goal="核对报告",
                rationale="完成报告",
                success_criteria=("file:REPORT.md",),
                verification_policy=("format_check", "rule_check", "critic_review"),
                allowed_tools=(),
                budget=Budget(max_tokens=10_000, max_attempts=2),
            ),
            base_version=planning.version,
            source={"planner": "fixture"},
        )
        attempt, intent = service.create_attempt(
            task.id,
            role="worker",
            model="fixture-model",
            prompt_version="worker-v2",
            context_version="ctx",
            reservation=Reservation(1_000, 0),
            intent_config={"agent_config": {}, "message": "核对报告"},
            input_hash="fixture",
        )
        turn_id = "turn:" + attempt.id
        service.claim_intent(intent.intent_id, owner="fixture", lease_seconds=60)
        service.record_agent_created(
            intent.intent_id, agent_id="agent:" + attempt.id, expected_turn_id=turn_id
        )
        service.record_submitted(intent.intent_id, receipt={"turn_id": turn_id, "seq": 1})
        data = b"report\n"
        digest = cas.put_bytes(data)
        artifact = Artifact(
            id="artifact:" + attempt.id,
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
        envelope = ResultEnvelope(
            id="result:" + attempt.id,
            mission_id=mission.id,
            task_id=task.id,
            attempt_id=attempt.id,
            outcome="candidate",
            summary="报告候选",
            claims=(ClaimProposal(content="资料中有该陈述", confidence=0.8),),
            evidence=("artifact:REPORT.md",),
            artifacts=("REPORT.md",),
            proposed_tasks=(),
            used_knowledge=(),
            risks=(),
            cost={},
        )
        assert store.get_attempt(attempt.id).status is AttemptStatus.RUNNING
        return mission, attempt, turn_id, artifact, envelope

    try:
        yield service, make
    finally:
        store.close()


def _record(service, attempt, turn_id, artifact, envelope):
    return service.record_result(
        attempt.id, envelope=envelope, turn_id=turn_id, artifacts=(artifact,), usage_refs=()
    )


@pytest.mark.parametrize("reference", ["pytest:tests/test_unrelated.py", "tool-run:made-up-call"])
@pytest.mark.parametrize("location", ["envelope", "claim", "overridden_envelope"])
def test_doc_record_result_refuses_forbidden_evidence_before_any_state_write(
    running, reference, location
):
    service, make = running
    mission, attempt, turn_id, artifact, valid = make(DOC_DOMAIN)
    if location == "claim":
        # The second claim must be inspected too; first/top-level evidence is legal.
        poisoned = replace(
            valid,
            claims=(
                *valid.claims,
                ClaimProposal(content="第二条陈述", confidence=0.8, evidence=(reference,)),
            ),
        )
    else:
        poisoned = replace(valid, evidence=(reference,))
        if location == "overridden_envelope":
            poisoned = replace(
                poisoned, claims=(replace(valid.claims[0], evidence=valid.evidence),)
            )
    before = service.store.snapshot(mission.id)
    with pytest.raises(CommitRejected) as error:
        _record(service, attempt, turn_id, artifact, poisoned)
    assert "evidence" in str(error.value) and DOC_DOMAIN in str(error.value)
    assert reference.split(":", 1)[0] in str(error.value)
    assert service.store.snapshot(mission.id) == before
    assert service.store.get_result(valid.id) is None
    assert service.store.get_artifact(artifact.id) is None
    accepted = _record(service, attempt, turn_id, artifact, valid)
    assert accepted.envelope == valid
    after = service.store.snapshot(mission.id)
    assert _record(service, attempt, turn_id, artifact, valid) == accepted
    assert service.store.snapshot(mission.id) == after


@pytest.mark.parametrize(
    "reference", ["pytest:tests/test_legacy.py", "tool-run:legacy-call", "legacy-custom:opaque"]
)
def test_code_record_result_keeps_legacy_evidence_admission(running, reference):
    service, make = running
    mission, attempt, turn_id, artifact, envelope = make(CODE_DOMAIN)
    envelope = replace(
        envelope,
        evidence=(reference,),
        claims=(replace(envelope.claims[0], evidence=(reference,)),),
    )
    stored = _record(service, attempt, turn_id, artifact, envelope)
    assert stored.envelope.evidence == (reference,)
    assert service.store.list_claims(envelope.id)[0].evidence == (reference,)
    assert service.store.get_attempt(attempt.id).status is AttemptStatus.SUBMITTED


@pytest.mark.parametrize(
    "reference",
    [
        "REPORT.md",
        "file:REPORT.md",
        "artifact:REPORT.md",
        "source:sources/input.md",
        "knowledge:existing-id",
    ],
)
def test_doc_allowed_kind_is_admitted_without_claiming_its_evidence_was_verified(
    running, reference
):
    service, make = running
    _, attempt, turn_id, artifact, envelope = make(DOC_DOMAIN)
    envelope = replace(envelope, evidence=(reference,))
    stored = _record(service, attempt, turn_id, artifact, envelope)
    assert stored.verification_state == "PENDING" and stored.verdict is None
    assert service.store.list_claims(envelope.id)[0].evidence == (reference,)
