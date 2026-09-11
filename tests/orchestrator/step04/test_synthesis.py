# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · slice D (D4-8', D4-20): the fixed synthesis Task is appended at graph commit
and depends on every Planner leaf, its budget is part of the §18.2 sum, an open
conflict gates it (no rewiring, no state change) and is re-checked inside the accept
Commit, and its product must pass verification again — sources passing is not the
synthesis passing (S4-05)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from knowledge_helpers import (
    claim,
    drive_to_running,
    envelope,
    node,
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
from agent_orchestrator.graph.task_graph import GraphRejected, TaskGraphProposal, validate_graph
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    COMPARE_SEED,
    COMPARE_SPEC,
    COMPARE_SYNTHESIS,
    COMPARE_TASKS,
    compare_script_synthesizer,
    demo_knowledge_sharing_provider,
)

SYNTH = {
    **COMPARE_SYNTHESIS,
    "success_criteria": ["file:COMPARISON.md"],
    "verification_policy": ["format_check", "rule_check"],
}


# ------------------------------------------------------------------ D4-8 / D4-20 (graph)
def test_synthesis_task_is_appended_at_graph_commit_and_budgeted_in_the_sum(tmp_path):
    service, mission, tasks = two_branch_service(
        tmp_path, synthesis=SYNTH, conflict_reserve_tokens=10_000
    )
    assert [t.kind for t in tasks] == ["work", "work", "synthesis"]
    synthesis = tasks[-1]
    assert synthesis.status is TaskStatus.BLOCKED and set(synthesis.dependency_ids) == {
        tasks[0].id,
        tasks[1].id,
    }
    assert synthesis.budget.max_tokens == 30_000 and synthesis.outputs == (
        "comparison.json",
        "COMPARISON.md",
    )
    receipt = service.store.get_receipt(
        next(
            e.payload["commit_id"]
            for e in service.store.list_events(mission.id)
            if e.type == "TaskGraphCommitted"
        )
    )
    assert receipt["terminal_task_id"] == synthesis.id
    with service.store.transaction():
        assert service.ledger.account(f"budget:{synthesis.id}").limits.max_tokens == 30_000
    # the Planner's Σ plus the synthesis budget plus the conflict reserve may not exceed the Mission
    over = TaskGraphProposal.from_json(
        {
            "tasks": [
                node("A", budget={"max_tokens": 40_000, "max_attempts": 3}),
                node("B", budget={"max_tokens": 40_000, "max_attempts": 3}),
            ]
        }
    )
    with pytest.raises(GraphRejected) as rejected:
        validate_graph(service.store.get_mission(mission.id), over)
    assert "system reserve (40000)" in str(rejected.value) and "remaining=60000" in str(
        rejected.value
    )
    # unset task budgets are normalised over the pool that remains after the reserve
    free = TaskGraphProposal.from_json(
        {
            "tasks": [
                {**node("A"), "budget": {"max_attempts": 2}},
                {**node("B"), "budget": {"max_attempts": 2}},
            ]
        }
    )
    validated = validate_graph(service.store.get_mission(mission.id), free)
    assert [n.budget.max_tokens for n in validated.proposal.tasks] == [30_000, 30_000]


def test_synthesis_accept_is_refused_while_a_conflict_is_open_and_needs_knowledge(tmp_path):
    service, mission, (task_a, task_b, synthesis) = two_branch_service(
        tmp_path, synthesis=SYNTH, conflict_reserve_tokens=20_000
    )
    # A: VERIFIED knowledge; B: the opposite → a conflict opens (task-4) and S becomes READY (its deps are done)
    a1 = drive_to_running(service, task_a)
    sa = submit(
        service,
        a1,
        envelope(
            a1,
            claims=[
                claim(
                    "抛错",
                    key="impl_a.empty_input",
                    stance="refutes",
                    evidence=["pytest:tests/probe/test_impl_a.py"],
                )
            ],
        ),
    )
    service.accept_result(
        sa.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_a.py")
    )
    knowledge_id = service.store.list_knowledge(mission.id)[0].id
    b1 = drive_to_running(service, task_b, agent="agent-2", turn="turn-2")
    sb = submit(
        service,
        b1,
        envelope(
            b1,
            claims=[
                claim(
                    "返回 {}",
                    key="impl_a.empty_input",
                    stance="affirms",
                    evidence=["pytest:tests/probe/test_impl_b.py"],
                )
            ],
            artifacts=("tests/probe/test_impl_b.py",),
        ),
        artifact_paths=("tests/probe/test_impl_b.py",),
        turn="turn-2",
    )
    service.accept_result(
        sb.envelope.id, verifier_results=passed_layers("tests/probe/test_impl_b.py")
    )
    tasks = service.store.list_tasks(mission.id)
    assert [t.kind for t in tasks] == ["work", "work", "synthesis", "conflict"]
    assert (
        service.store.get_task(synthesis.id).status is TaskStatus.READY
    )  # never rewired, never BLOCKED again
    assert service.store.get_task(synthesis.id).dependency_ids == synthesis.dependency_ids
    # a synthesis result that somehow got verified while the conflict is OPEN is refused in the Commit
    s1 = drive_to_running(
        service, service.store.get_task(synthesis.id), agent="agent-3", turn="turn-3"
    )
    ss = submit(
        service,
        s1,
        envelope(
            s1,
            claims=[claim("综合完成", evidence=["COMPARISON.md"])],
            artifacts=("COMPARISON.md",),
            used_knowledge=[knowledge_id],
        ),
        artifact_paths=("COMPARISON.md",),
        turn="turn-3",
    )
    returned = service.accept_result(ss.envelope.id, verifier_results=passed_layers())
    assert returned.status is TaskStatus.ACTIVE
    failure = service.store.get_attempt(s1.id).failure["failures"][0]
    assert failure["detail"]["reason"] == "synthesis_blocked_by_open_conflict"
    assert all(c.status is ClaimStatus.REJECTED for c in service.store.list_claims(ss.envelope.id))


def test_rule_check_requires_the_synthesis_to_cite_verified_knowledge(tmp_path):
    from agent_orchestrator.artifacts.workspace import Workspace
    from agent_orchestrator.contracts import ResultEnvelope
    from agent_orchestrator.memory.verified_knowledge import KnowledgeIndex
    from agent_orchestrator.verification.deterministic_checks import rule_check

    service, mission, (task_a, task_b, synthesis) = two_branch_service(tmp_path, synthesis=SYNTH)
    root = Path(tmp_path) / "ws"
    root.mkdir()
    (root / "COMPARISON.md").write_text("# x\n", encoding="utf-8")
    workspace = Workspace(root, "s", True)
    env = ResultEnvelope(
        id="r",
        mission_id=mission.id,
        task_id=synthesis.id,
        attempt_id="a",
        outcome="candidate",
        summary="s",
        claims=(),
        evidence=("COMPARISON.md",),
        artifacts=("COMPARISON.md",),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    result = rule_check(
        env,
        synthesis,
        artifacts=(),
        verification_copy=workspace,
        knowledge=KnowledgeIndex.empty(mission.id),
    )
    assert result.status == "FAIL" and any("used_knowledge" in p for p in result.detail["problems"])
    relaxed = rule_check(
        env,
        synthesis,
        artifacts=(),
        verification_copy=workspace,
        knowledge=KnowledgeIndex.empty(mission.id),
        require_synthesis_knowledge=False,
    )
    assert not any("must cite" in p for p in relaxed.detail["problems"])


# ------------------------------------------------------------------ S4-05 (closure)
def spec(key, **overrides):
    base = dict(
        goal=COMPARE_SPEC["goal"],
        success_criteria=tuple(COMPARE_SPEC["success_criteria"]),
        tenant_id="tenant-4",
        idempotency_key=key,
        allowed_tools=tuple(COMPARE_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=200_000, max_attempts=12),
        workspace_seed=COMPARE_SEED,
        untrusted_sources=("docs/",),
        synthesis=COMPARE_SYNTHESIS,
    )
    base.update(overrides)
    return MissionSpec(**base)


def only(keys):
    return [t for t in COMPARE_TASKS if t["key"] in keys]


def test_s4_05_a_synthesis_that_introduces_an_error_is_not_committed_until_it_passes_again(
    tmp_path,
):
    provider = demo_knowledge_sharing_provider(
        tasks=only("AB"),
        per_attempt={"S": [compare_script_synthesizer(wrong=True), compare_script_synthesizer()]},
    )

    async def case():
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
        )
        async with Orchestrator(config, provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-05"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            tasks = store.list_tasks(mission.id)
            synthesis = next(t for t in tasks if t.kind == "synthesis")
            assert final.final_report["terminal_task_id"] == synthesis.id
            attempts = store.list_attempts(synthesis.id)
            assert [a.status for a in attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert attempts[0].role == "synthesizer"
            wrong = attempts[0].failure["failures"][0]
            assert wrong["layer"] == "code_test"  # the sources passed; the synthesis did not
            # the Mission was not judged before the synthesis passed
            events = store.list_events(mission.id)
            failed_seq = next(
                e.seq
                for e in events
                if e.type == "VerificationFailed" and e.task_id == synthesis.id
            )
            judged_seq = next(e.seq for e in events if e.type == "MissionSuccessJudged")
            assert failed_seq < judged_seq
            accepted = store.get_result(synthesis.accepted_result_id)
            verified = {k.id for k in store.list_knowledge(mission.id, status="VERIFIED")}
            assert (
                accepted.envelope.used_knowledge
                and set(accepted.envelope.used_knowledge) <= verified
            )
            used = [e for e in events if e.type == "KnowledgeUsed" and e.task_id == synthesis.id]
            assert {e.payload["knowledge_id"] for e in used} == set(
                accepted.envelope.used_knowledge
            )
            judged = {j["criterion"]: j["met"] for j in final.final_report["success_criteria"]}
            assert judged == {"pytest:tests/test_comparison.py": True, "file:COMPARISON.md": True}

    asyncio.run(case())
