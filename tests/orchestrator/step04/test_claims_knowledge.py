# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · slice A (D4-1…D4-5): Claims are graded by the system from the evidence the
verification actually produced, only VERIFIED claims are projected into the separately
stored Verified Knowledge (with provenance), ``used_knowledge`` is a checked reference that
records the reuse chain, supersession is explicit and traceable, and knowledge never
crosses a Mission boundary."""

from __future__ import annotations

import pytest
from knowledge_helpers import (
    claim,
    drive_to_running,
    envelope,
    passed_layers,
    submit,
    two_branch_service,
)

from agent_orchestrator.contracts import ClaimProposal, ClaimStatus, ContractError, TaskStatus
from agent_orchestrator.memory.blackboard import Blackboard
from agent_orchestrator.memory.verified_knowledge import KnowledgeIndex
from agent_orchestrator.verification.deterministic_checks import check_used_knowledge


# ------------------------------------------------------------------ D4-1
def test_claim_proposal_carries_typed_fields_and_rejects_more_than_proposed():
    proposal = ClaimProposal.from_json(
        {
            "content": "impl_a 对空输入抛 ValueError",
            "confidence": 0.9,
            "key": "impl_a.empty_input",
            "stance": "refutes",
            "evidence": ["pytest:tests/probe/test_impl_a.py"],
            "supersedes": None,
            "contradicts": [],
        }
    )
    assert proposal.key == "impl_a.empty_input" and proposal.stance == "refutes"
    assert proposal.evidence == ("pytest:tests/probe/test_impl_a.py",)
    assert ClaimProposal.from_json({"content": "x", "confidence": 0.5}).stance == "affirms"
    with pytest.raises(ContractError):  # 原则三: nothing above PROPOSED may be proposed
        ClaimProposal.from_json({"content": "x", "confidence": 0.5, "status": "VERIFIED"})
    with pytest.raises(ContractError):
        ClaimProposal.from_json({"content": "x", "confidence": 0.5, "stance": "maybe"})
    with pytest.raises(ContractError):
        ClaimProposal.from_json({"content": "x", "confidence": 0.5, "verified": True})


# ------------------------------------------------------------------ D4-2 / D4-3
def test_claims_are_graded_from_actual_verification_and_only_verified_is_knowledge(tmp_path):
    service, mission, (task_a, _task_b) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task_a)
    stored = submit(
        service,
        attempt,
        envelope(
            attempt,
            claims=[
                claim(
                    "impl_a 对空输入抛 ValueError",
                    key="impl_a.empty_input",
                    stance="refutes",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                ),
                claim(
                    "impl_a 的实现风格良好", evidence=["tests/probe/test_impl_a.py"]
                ),  # cited artifact, no test
                claim(
                    "impl_a 完全满足合同", evidence=["docs/vendor_notes.md"]
                ),  # untrusted source only
                claim(
                    "impl_a 的性能优于 impl_b"
                ),  # inherits the envelope evidence: an artifact → SUPPORTED at most
            ],
        ),
    )
    completed = service.accept_result(
        stored.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    assert completed.status is TaskStatus.COMPLETED
    claims = service.store.list_claims(stored.envelope.id)
    by_content = {c.content: c for c in claims}
    verified = by_content["impl_a 对空输入抛 ValueError"]
    assert verified.status is ClaimStatus.VERIFIED
    assert verified.verifier_results[-1]["layer"] == "code_test"
    assert verified.verifier_results[-1]["target"] == "tests/probe/test_impl_a.py"
    assert by_content["impl_a 的实现风格良好"].status is ClaimStatus.SUPPORTED
    assert by_content["impl_a 的性能优于 impl_b"].status is ClaimStatus.SUPPORTED
    unsupported = by_content["impl_a 完全满足合同"]
    assert unsupported.status is ClaimStatus.UNDER_REVIEW
    assert unsupported.confidence_metadata["grade"] == "unsupported"
    assert unsupported.confidence_metadata["evidence_trust"] == ["untrusted_external"]
    # Verified Knowledge is stored separately (30-07) and carries the provenance (30-08)
    knowledge = service.store.list_knowledge(mission.id)
    assert [k.id for k in knowledge] == [verified.id]
    record = knowledge[0]
    assert record.status == "VERIFIED" and record.version == 1
    assert record.key == "impl_a.empty_input" and record.stance == "refutes"
    assert record.source_task == task_a.id and record.source_attempt == attempt.id
    assert record.source_result == stored.envelope.id and record.proposed_by == "agent-1"
    assert (
        record.verifier["layer"] == "code_test"
        and record.verifier["target"] == "tests/probe/test_impl_a.py"
    )
    assert record.evidence == ("pytest:tests/probe/test_impl_a.py",)
    assert record.used_by == () and record.supersedes is None and record.superseded_by is None
    assert service.store.count_events(mission.id, "KnowledgeCommitted") == 1
    # the Blackboard facade is read-only and keeps the layers apart (§11)
    board = Blackboard(service.store)
    assert [k.id for k in board.verified_knowledge(mission.id)] == [verified.id]
    assert {c.id for c in board.candidate_claims(mission.id)} == {
        c.id for c in claims if c.status is not ClaimStatus.VERIFIED
    }
    assert not hasattr(board, "upsert_knowledge") and not hasattr(board, "write")


def test_a_directory_test_run_covers_a_cited_file_but_not_an_unrun_one(tmp_path):
    service, mission, (task_a, _) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task_a)
    stored = submit(
        service,
        attempt,
        envelope(
            attempt,
            claims=[
                claim("覆盖", key="k.covered", evidence=["pytest:tests/probe/test_impl_a.py"]),
                claim("未跑", key="k.unrun", evidence=["pytest:tests/other/test_x.py"]),
            ],
        ),
    )
    service.accept_result(stored.envelope.id, verifier_results=passed_layers("tests/probe"))
    statuses = {c.key: c.status for c in service.store.list_claims(stored.envelope.id)}
    assert statuses == {"k.covered": ClaimStatus.VERIFIED, "k.unrun": ClaimStatus.SUPPORTED}


def test_failed_verification_rejects_every_claim_and_projects_nothing(tmp_path):
    service, mission, (task_a, _) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task_a)
    stored = submit(
        service,
        attempt,
        envelope(
            attempt, claims=[claim("x", key="k", evidence=["pytest:tests/probe/test_impl_a.py"])]
        ),
    )
    service.fail_result(
        stored.envelope.id,
        failures=[{"layer": "code_test", "status": "FAIL", "summary": "1 failed"}],
    )
    assert all(
        c.status is ClaimStatus.REJECTED for c in service.store.list_claims(stored.envelope.id)
    )
    assert service.store.list_knowledge(mission.id) == []


# ------------------------------------------------------------------ D4-4
def test_used_knowledge_is_a_checked_reference_and_records_the_reuse_chain(tmp_path):
    service, mission, (task_a, task_b) = two_branch_service(tmp_path)
    attempt_a = drive_to_running(service, task_a)
    stored_a = submit(
        service,
        attempt_a,
        envelope(
            attempt_a,
            claims=[
                claim(
                    "A 已验证",
                    key="impl_a.empty_input",
                    stance="refutes",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                ),
                claim("A 只有证据", key="impl_a.style"),
            ],
        ),
    )
    service.accept_result(
        stored_a.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    verified_id, supported_id = (c.id for c in service.store.list_claims(stored_a.envelope.id))
    index = KnowledgeIndex.load(service.store, mission.id)
    assert check_used_knowledge((verified_id,), index) == []
    assert any("not VERIFIED" in p for p in check_used_knowledge((supported_id,), index))
    assert any("unknown" in p for p in check_used_knowledge(("K-nope",), index))
    attempt_b = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    stored_b = submit(
        service,
        attempt_b,
        envelope(
            attempt_b,
            claims=[
                claim(
                    "B 依据 A",
                    key="impl_b.empty_input",
                    evidence=["pytest:tests/probe/test_impl_b.py"],
                )
            ],
            artifacts=("tests/probe/test_impl_b.py",),
            used_knowledge=[verified_id],
        ),
        artifact_paths=("tests/probe/test_impl_b.py",),
        turn="turn-2",
    )
    service.accept_result(
        stored_b.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_b.py")
    )
    record = service.store.get_knowledge(verified_id)
    assert record.used_by == (task_b.id,)
    used = [e for e in service.store.list_events(mission.id) if e.type == "KnowledgeUsed"]
    assert len(used) == 1 and used[0].payload["knowledge_id"] == verified_id
    assert used[0].payload["version"] == 1 and used[0].task_id == task_b.id
    b_knowledge = service.store.get_knowledge(
        next(c.id for c in service.store.list_claims(stored_b.envelope.id))
    )
    assert b_knowledge.dependencies == (verified_id,)  # provenance: 基于哪些已有知识


# ------------------------------------------------------------------ D4-5 / S4-04
def test_s4_04_a_supported_claim_cannot_supersede_verified_knowledge(tmp_path):
    service, mission, (task_a, task_b) = two_branch_service(tmp_path)
    attempt_a = drive_to_running(service, task_a)
    stored_a = submit(
        service,
        attempt_a,
        envelope(
            attempt_a,
            claims=[
                claim(
                    "impl_a 忽略尾分隔符",
                    key="impl_a.trailing",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                )
            ],
        ),
    )
    service.accept_result(
        stored_a.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    k1 = service.store.list_knowledge(mission.id)[0]
    attempt_b = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    stored_b = submit(
        service,
        attempt_b,
        envelope(
            attempt_b,
            claims=[claim("impl_a 尾分隔符抛错（未测）", key="impl_a.trailing", supersedes=k1.id)],
            artifacts=("notes/b.md",),
        ),
        artifact_paths=("notes/b.md",),
        turn="turn-2",
    )
    service.accept_result(stored_b.envelope.id, verifier_results=passed_layers())
    weak = service.store.list_claims(stored_b.envelope.id)[0]
    assert weak.status is ClaimStatus.SUPPORTED
    assert "supersedes_rejected" in weak.confidence_metadata
    assert service.store.get_knowledge(k1.id).status == "VERIFIED"
    assert service.store.get_claim(k1.id).status is ClaimStatus.VERIFIED
    assert service.store.count_events(mission.id, "KnowledgeSuperseded") == 0


def test_s4_04_a_verified_claim_supersedes_and_the_lineage_stays_traceable(tmp_path):
    service, mission, (t1, t2) = two_branch_service(tmp_path)
    a1 = drive_to_running(service, t1)
    s1 = submit(
        service,
        a1,
        envelope(
            a1,
            claims=[
                claim("v1", key="impl_a.trailing", evidence=["pytest:tests/probe/test_impl_a.py"])
            ],
        ),
    )
    service.accept_result(
        s1.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    old = service.store.list_knowledge(mission.id)[0]
    a2 = drive_to_running(service, t2, agent="agent-2", turn="turn-2")
    s2 = submit(
        service,
        a2,
        envelope(
            a2,
            claims=[
                claim(
                    "v2 更准确",
                    key="impl_a.trailing",
                    supersedes=old.id,
                    evidence=["pytest:tests/probe/test_impl_b.py"],
                )
            ],
            artifacts=("tests/probe/test_impl_b.py",),
        ),
        artifact_paths=("tests/probe/test_impl_b.py",),
        turn="turn-2",
    )
    service.accept_result(
        s2.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_b.py")
    )
    new = service.store.list_claims(s2.envelope.id)[0]
    assert service.store.get_claim(old.id).status is ClaimStatus.SUPERSEDED
    assert service.store.get_knowledge(old.id).status == "SUPERSEDED"
    assert service.store.get_knowledge(old.id).superseded_by == new.id
    assert service.store.get_knowledge(new.id).supersedes == old.id
    events = [e for e in service.store.list_events(mission.id) if e.type == "KnowledgeSuperseded"]
    assert len(events) == 1 and events[0].payload == {
        "knowledge_id": old.id,
        "superseded_by": new.id,
        "key": "impl_a.trailing",
    }
    assert [k.id for k in service.store.list_knowledge(mission.id, status="VERIFIED")] == [new.id]
    problems = check_used_knowledge((old.id,), KnowledgeIndex.load(service.store, mission.id))
    assert problems and "SUPERSEDED" in problems[0] and new.id in problems[0]


# ------------------------------------------------------------------ S4-06 (storage side)
def test_knowledge_never_crosses_the_mission_boundary(tmp_path):
    service, mission, (task_a, _) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task_a)
    stored = submit(
        service,
        attempt,
        envelope(
            attempt, claims=[claim("A", key="k", evidence=["pytest:tests/probe/test_impl_a.py"])]
        ),
    )
    service.accept_result(
        stored.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    foreign = service.store.list_knowledge(mission.id)[0]
    other, other_mission, _ = two_branch_service(tmp_path / "other", key="k-other")
    assert other.store.list_knowledge(other_mission.id) == []
    # same library, second Mission: still invisible
    from knowledge_helpers import spec

    planning = service.begin_planning(service.create_mission(spec("k-3"))[0].id)
    assert service.store.list_knowledge(planning.id) == []
    problems = check_used_knowledge((foreign.id,), KnowledgeIndex.load(service.store, planning.id))
    assert problems and "not in this Mission" in problems[0]


# ------------------------------------------------------------------ D4-2' (R6)
def test_a_whole_tree_run_covers_no_claim(tmp_path):
    service, mission, (task_a, _) = two_branch_service(tmp_path)
    attempt = drive_to_running(service, task_a)
    stored = submit(
        service,
        attempt,
        envelope(
            attempt,
            claims=[
                claim("整树跑通", key="k.tree", evidence=["pytest:tests/probe/test_impl_a.py"])
            ],
        ),
    )
    whole_tree = passed_layers()
    whole_tree[-1]["detail"]["runs"] = [{"target": None, "passed": True, "stdout": "9 passed"}]
    service.accept_result(stored.envelope.id, verifier_results=whole_tree)
    only = service.store.list_claims(stored.envelope.id)[0]
    assert only.status is ClaimStatus.SUPPORTED
    assert service.store.list_knowledge(mission.id) == []


# ------------------------------------------------------------------ D4-4' (R5)
def test_accept_rechecks_used_knowledge_inside_the_commit(tmp_path):
    service, mission, (task_a, task_b) = two_branch_service(tmp_path)
    a1 = drive_to_running(service, task_a)
    s1 = submit(
        service,
        a1,
        envelope(a1, claims=[claim("v1", key="k", evidence=["pytest:tests/probe/test_impl_a.py"])]),
    )
    service.accept_result(
        s1.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    k1 = service.store.list_knowledge(mission.id)[0]
    # B verified its result while K1 was current…
    b1 = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    sb = submit(
        service,
        b1,
        envelope(
            b1,
            claims=[claim("uses K1", evidence=["pytest:tests/probe/test_impl_b.py"])],
            artifacts=("tests/probe/test_impl_b.py",),
            used_knowledge=[k1.id],
        ),
        artifact_paths=("tests/probe/test_impl_b.py",),
        turn="turn-2",
    )
    # …but K1 was superseded before B's accept landed (another branch's Commit)
    service._supersede_knowledge(k1.id, by="result-x:claim-9")
    returned = service.accept_result(
        sb.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_b.py")
    )
    assert returned.status is TaskStatus.ACTIVE
    attempt = service.store.get_attempt(b1.id)
    assert attempt.status.value == "RETRY_WAIT"
    assert attempt.failure["failures"][0]["detail"]["reason"] == "used_knowledge_stale"
    assert service.store.get_result(sb.envelope.id).verdict == "FAIL"
    assert all(c.status is ClaimStatus.REJECTED for c in service.store.list_claims(sb.envelope.id))
    assert service.store.count_events(mission.id, "VerificationFailed") == 1
