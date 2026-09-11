# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · slice C (D4-6', D4-7', D4-20): two contradictory claims are both kept and
marked DISPUTED (conflict precedes grading, nothing contested is projected), a
system-defined Conflict Task is opened at the end of the topological order, the
Arbiter's opinion is refused and its external check resolves the conflict — never a
vote; S4-03 end to end on fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

from knowledge_helpers import (
    claim,
    drive_to_running,
    envelope,
    passed_layers,
    submit,
    two_branch_service,
)

from agent_orchestrator.contracts import (
    AttemptStatus,
    Budget,
    ClaimStatus,
    MissionStatus,
    TaskStatus,
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec, Reservation
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.manager import arbitration_dir
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    COMPARE_ARBITRATION_DIR,
    COMPARE_SEED,
    COMPARE_SPEC,
    compare_script_arbiter,
    demo_knowledge_sharing_provider,
)
from agent_orchestrator.verification.deterministic_checks import check_arbitration

KEY = "impl_a.empty_input"
PROBE = f"{arbitration_dir(KEY)}/test_probe.py"


def _dispute(tmp_path, *, reserve=20_000, b_verified=True):
    """A's claim is VERIFIED knowledge; B's opposite claim arrives with its own evidence."""

    service, mission, (task_a, task_b) = two_branch_service(
        tmp_path, conflict_reserve_tokens=reserve
    )
    a1 = drive_to_running(service, task_a)
    sa = submit(
        service,
        a1,
        envelope(
            a1,
            claims=[
                claim(
                    "impl_a 对空输入抛 ValueError",
                    key=KEY,
                    stance="refutes",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                )
            ],
        ),
    )
    service.accept_result(
        sa.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    knowledge = service.store.list_knowledge(mission.id)[0]
    b1 = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    evidence = (
        ["pytest:tests/probe/test_impl_b.py"] if b_verified else ["tests/probe/test_impl_b.py"]
    )
    sb = submit(
        service,
        b1,
        envelope(
            b1,
            claims=[claim("impl_a 对空输入返回 {}", key=KEY, stance="affirms", evidence=evidence)],
            artifacts=("tests/probe/test_impl_b.py",),
        ),
        artifact_paths=("tests/probe/test_impl_b.py",),
        turn="turn-2",
    )
    completed = service.accept_result(
        sb.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_b.py")
    )
    return service, mission, (task_a, task_b), knowledge, sb, completed


def _arbitrate(
    service, task, *, stance="refutes", opinion_only=False, agent="agent-3", turn="turn-3"
):
    attempt, intent = service.create_attempt(
        task.id,
        role="arbiter",
        model="agent-model",
        prompt_version="arbiter-v1",
        context_version="ctx",
        reservation=Reservation(tokens=4_000, cost_micros=0),
        intent_config={"agent_config": {}, "message": "arbitrate"},
        input_hash="h",
    )
    service.claim_intent(intent.intent_id, owner="orch-1", lease_seconds=60)
    service.record_agent_created(intent.intent_id, agent_id=agent, expected_turn_id=turn)
    service.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
    attempt = service.store.get_attempt(attempt.id)
    evidence = [f"{arbitration_dir(KEY)}/verdict.md"] if opinion_only else [f"pytest:{PROBE}"]
    stored = submit(
        service,
        attempt,
        envelope(
            attempt,
            claims=[claim("仲裁结论", key=KEY, stance=stance, evidence=evidence)],
            artifacts=(PROBE,),
        ),
        artifact_paths=(PROBE,),
        turn=turn,
    )
    return attempt, stored


# ------------------------------------------------------------------ D4-6' / D4-7' / D4-20
def test_contradiction_keeps_both_claims_disputed_and_opens_a_conflict_task(tmp_path):
    service, mission, (task_a, task_b), knowledge, sb, completed = _dispute(tmp_path)
    assert completed.status is TaskStatus.COMPLETED  # B's result is accepted: its tests passed
    disputed = service.store.list_claims(sb.envelope.id)[0]
    assert disputed.status is ClaimStatus.DISPUTED
    assert (
        disputed.confidence_metadata["grade"] == "disputed"
        and disputed.confidence_metadata["grade_before_dispute"] == "verified"
    )
    assert service.store.get_knowledge(disputed.id) is None  # contested → never projected
    fresh = service.store.get_knowledge(knowledge.id)
    assert fresh.status == "VERIFIED" and fresh.disputed_by == (
        disputed.id,
    )  # no VERIFIED→DISPUTED edge
    assert service.store.get_claim(knowledge.id).status is ClaimStatus.VERIFIED
    conflicts = service.store.list_conflicts(mission.id)
    assert len(conflicts) == 1 and conflicts[0]["state"] == "OPEN" and conflicts[0]["key"] == KEY
    assert set(conflicts[0]["claim_ids"]) == {knowledge.id, disputed.id}
    tasks = service.store.list_tasks(mission.id)
    assert len(tasks) == 3
    conflict = tasks[-1]
    assert conflict.kind == "conflict" and conflict.id.endswith(
        ":task-3"
    )  # a leaf at the end of the order
    assert set(conflict.dependency_ids) == {task_a.id, task_b.id}
    assert (
        conflict.status is TaskStatus.READY
    )  # BLOCKED at insert, unblocked in the same transaction
    assert conflict.verification_policy == (
        "format_check",
        "rule_check",
        "critic_review",
        "code_test",
    )
    assert conflict.success_criteria == (f"arbitration:{KEY}", f"pytest:{PROBE}")
    assert conflict.outputs == (f"{arbitration_dir(KEY)}/",) and conflict.budget.max_attempts == 2
    assert conflict.budget.max_tokens == 20_000 and conflict.context["key"] == KEY
    assert not any(t.dependency_ids and conflict.id in t.dependency_ids for t in tasks)
    types = [e.type for e in service.store.list_events(mission.id)]
    assert types.count("ClaimDisputed") == 1 and types.count("ConflictOpened") == 1
    committed = [
        e
        for e in service.store.list_events(mission.id)
        if e.type == "TaskCommitted" and e.task_id == conflict.id
    ]
    assert committed and committed[0].payload["source"]["template"] == "conflict"
    assert any(
        e.type == "TaskUnblocked" and e.task_id == conflict.id
        for e in service.store.list_events(mission.id)
    )
    report = service.store.get_mission(mission.id).final_report
    assert report["graph_version"] == 2 and report["conflict_reserve_remaining"] == 0


def test_arbitration_resolves_on_the_external_check_not_on_a_count(tmp_path):
    service, mission, (task_a, task_b), knowledge, sb, _ = _dispute(tmp_path)
    conflict = service.store.list_tasks(mission.id)[-1]
    disputed = service.store.list_claims(sb.envelope.id)[0]
    from knowledge_helpers import envelope as make_envelope

    attempt, stored = _arbitrate(service, conflict)
    problems = check_arbitration(
        make_envelope(
            attempt,
            claims=[
                claim("意见", key=KEY, stance="refutes", evidence=["arbitration/x/verdict.md"])
            ],
            artifacts=(PROBE,),
        ),
        conflict,
    )
    assert problems and "external check" in problems[0]
    assert check_arbitration(stored.envelope, conflict) == []
    service.accept_result(stored.envelope.id, verifier_results=passed_layers(PROBE))
    resolution = service.store.list_claims(stored.envelope.id)[0]
    assert resolution.status is ClaimStatus.VERIFIED
    record = service.store.get_knowledge(resolution.id)
    assert set(record.resolves) == {knowledge.id, disputed.id}
    assert (
        service.store.get_claim(disputed.id).status is ClaimStatus.DISPUTED
    )  # §25.3: no edge out of DISPUTED
    assert service.store.get_claim(disputed.id).resolved_by == resolution.id
    assert service.store.get_knowledge(knowledge.id).status == "VERIFIED"
    assert service.store.get_knowledge(knowledge.id).confirmed_by == (resolution.id,)
    conflict_record = service.store.list_conflicts(mission.id)[0]
    assert (
        conflict_record["state"] == "RESOLVED"
        and conflict_record["resolution_knowledge_id"] == resolution.id
    )
    resolved = [e for e in service.store.list_events(mission.id) if e.type == "ConflictResolved"]
    assert len(resolved) == 1
    assert (
        resolved[0].payload["basis"]["layer"] == "code_test"
        and resolved[0].payload["basis"]["target"] == PROBE
    )
    assert "votes" not in resolved[0].payload and "majority" not in str(resolved[0].payload)
    assert service.store.get_task(conflict.id).status is TaskStatus.COMPLETED


def test_an_arbitration_that_contradicts_the_knowledge_supersedes_it(tmp_path):
    service, mission, _, knowledge, sb, _ = _dispute(tmp_path)
    conflict = service.store.list_tasks(mission.id)[-1]
    attempt, stored = _arbitrate(service, conflict, stance="affirms")
    service.accept_result(stored.envelope.id, verifier_results=passed_layers(PROBE))
    resolution = service.store.list_claims(stored.envelope.id)[0]
    old = service.store.get_knowledge(knowledge.id)
    assert old.status == "SUPERSEDED" and old.superseded_by == resolution.id
    assert service.store.get_claim(knowledge.id).status is ClaimStatus.SUPERSEDED
    assert [k.id for k in service.store.list_knowledge(mission.id, status="VERIFIED")] == [
        resolution.id
    ]
    resolved = [e for e in service.store.list_events(mission.id) if e.type == "ConflictResolved"][0]
    assert resolved.payload["superseded"] == [knowledge.id]


def test_without_a_reserve_the_conflict_is_deferred_and_the_accept_still_lands(tmp_path):
    service, mission, (task_a, task_b), knowledge, sb, completed = _dispute(tmp_path, reserve=0)
    assert completed.status is TaskStatus.COMPLETED
    assert len(service.store.list_tasks(mission.id)) == 2  # no Conflict Task
    conflict = service.store.list_conflicts(mission.id)[0]
    assert conflict["state"] == "DEFERRED" and conflict["deferred_reason"] == "no_reserve"
    assert service.store.list_claims(sb.envelope.id)[0].status is ClaimStatus.DISPUTED
    assert service.store.count_events(mission.id, "ConflictOpenDeferred") == 1
    assert service.store.count_events(mission.id, "ConflictOpened") == 0


def test_two_candidates_that_contradict_are_both_disputed(tmp_path):
    service, mission, (task_a, task_b) = two_branch_service(tmp_path, key="k-cand")
    a1 = drive_to_running(service, task_a)
    sa = submit(
        service, a1, envelope(a1, claims=[claim("impl_a 抛错（未测）", key=KEY, stance="refutes")])
    )
    service.accept_result(sa.envelope.id, verifier_results=passed_layers())
    first = service.store.list_claims(sa.envelope.id)[0]
    assert first.status is ClaimStatus.SUPPORTED
    b1 = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    sb = submit(
        service,
        b1,
        envelope(
            b1,
            claims=[claim("impl_a 返回 {}（未测）", key=KEY, stance="affirms")],
            artifacts=("notes/b.md",),
        ),
        artifact_paths=("notes/b.md",),
        turn="turn-2",
    )
    service.accept_result(sb.envelope.id, verifier_results=passed_layers())
    assert service.store.get_claim(first.id).status is ClaimStatus.DISPUTED
    assert service.store.list_claims(sb.envelope.id)[0].status is ClaimStatus.DISPUTED
    assert service.store.list_knowledge(mission.id) == []


# ------------------------------------------------------------------ S4-03 (closure)
def spec(key, **overrides):
    base = dict(
        goal=COMPARE_SPEC["goal"],
        success_criteria=("file:contract/CONTRACT.md",),
        tenant_id="tenant-4",
        idempotency_key=key,
        allowed_tools=tuple(COMPARE_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=200_000, max_attempts=12),
        workspace_seed=COMPARE_SEED,
        untrusted_sources=("docs/",),
        conflict_reserve_tokens=20_000,
    )
    base.update(overrides)
    return MissionSpec(**base)


def test_s4_03_opposite_claims_are_arbitrated_by_an_external_check(tmp_path):
    provider = demo_knowledge_sharing_provider(
        per_attempt={"K": [compare_script_arbiter(opinion_only=True), compare_script_arbiter()]},
    )

    async def case():
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
        )
        async with Orchestrator(config, provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-03"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            tasks = store.list_tasks(mission.id)
            conflict = next(t for t in tasks if t.kind == "conflict")
            assert conflict is tasks[-1] and conflict.status is TaskStatus.COMPLETED
            attempts = store.list_attempts(conflict.id)
            assert [a.status for a in attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert attempts[0].role == "arbiter"
            refused = attempts[0].failure["failures"][0]
            assert refused["layer"] == "rule_check" and "external check" in refused["summary"]
            layers = {
                v["layer"]: v["status"]
                for v in store.list_verifications(conflict.accepted_result_id)
            }
            assert layers["critic_review"] == "PASS" and layers["code_test"] == "PASS"
            conflicts = store.list_conflicts(mission.id)
            assert len(conflicts) == 1 and conflicts[0]["state"] == "RESOLVED"
            resolution = store.get_knowledge(conflicts[0]["resolution_knowledge_id"])
            assert (
                resolution.status == "VERIFIED"
                and resolution.verifier["target"] == f"{COMPARE_ARBITRATION_DIR}/test_probe.py"
            )
            # both sides are kept: A's knowledge confirmed, C's claim DISPUTED and resolved
            sides = {
                store.get_claim(cid).source_task: store.get_claim(cid)
                for cid in conflicts[0]["claim_ids"]
            }
            statuses = sorted(str(c.status) for c in sides.values())
            assert statuses == ["DISPUTED", "VERIFIED"]
            disputed = next(c for c in sides.values() if c.status is ClaimStatus.DISPUTED)
            assert disputed.resolved_by == resolution.id and disputed.confidence_metadata[
                "evidence_trust"
            ] == ["untrusted_external"]
            resolved = [e for e in store.list_events(mission.id) if e.type == "ConflictResolved"][0]
            assert (
                resolved.payload["basis"]["layer"] == "code_test"
                and "votes" not in resolved.payload
            )
            assert final.final_report["unresolved_conflicts"] == []
            # the arbitration artifacts joined the integrated tree without clashing
            assert any(
                a.path.startswith("arbitration/") for a in store.list_mission_artifacts(mission.id)
            )

    asyncio.run(case())
