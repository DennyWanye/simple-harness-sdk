# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 4 · the whole ORCH §6.3 scenario end to end on the deterministic fixture provider:
A probes impl_a and its verified claim becomes team knowledge (S4-01: B reuses it in its
next context), C contradicts A on the strength of an untrusted document (S4-08) and the
system arbitrates with an external check (S4-03), the synthesis Task is gated by the
open conflict and its product is verified again (S4-05); the final report carries the
lineage of the result (30-27)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_orchestrator.contracts import (
    AttemptStatus,
    Budget,
    ClaimStatus,
    MissionStatus,
    TaskStatus,
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    COMPARE_SEED,
    COMPARE_SPEC,
    COMPARE_SYNTHESIS,
    COMPARE_TASKS,
    compare_script_arbiter,
    compare_script_synthesizer,
    demo_knowledge_sharing_provider,
)


def spec(key):
    return MissionSpec(
        goal=COMPARE_SPEC["goal"],
        success_criteria=tuple(COMPARE_SPEC["success_criteria"]),
        tenant_id="tenant-4",
        idempotency_key=key,
        allowed_tools=tuple(COMPARE_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=400_000, max_attempts=16),
        workspace_seed=COMPARE_SEED,
        untrusted_sources=("docs/",),
        synthesis=COMPARE_SYNTHESIS,
        conflict_reserve_tokens=20_000,
    )


def by_key(store, mission_id):
    goals = {t["goal"]: t["key"] for t in COMPARE_TASKS}
    found = {}
    for task in store.list_tasks(mission_id):
        if task.goal in goals:
            found[goals[task.goal]] = task
        elif task.kind == "conflict":
            found["K"] = task
        elif task.kind == "synthesis":
            found["S"] = task
    return found


def seq(events, event_type, **match):
    for event in events:
        if event.type == event_type and all(
            getattr(event, k, event.payload.get(k)) == v for k, v in match.items()
        ):
            return event.seq
    return None


def test_s4_01_s4_03_s4_05_s4_08_knowledge_sharing_closure(tmp_path):
    provider = demo_knowledge_sharing_provider(
        per_attempt={
            "K": [compare_script_arbiter(opinion_only=True), compare_script_arbiter()],
            "S": [compare_script_synthesizer(wrong=True), compare_script_synthesizer()],
        },
    )

    async def case():
        config = OrchestratorConfig(
            evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
        )
        async with Orchestrator(config, provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("s4-closure"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            tasks = by_key(store, mission.id)
            assert set(tasks) == {"A", "B", "C", "K", "S"}
            events = store.list_events(mission.id)
            # ---- S4-01: A's verified claim is knowledge, B's context carried it and B cited it
            a_result = store.get_result(tasks["A"].accepted_result_id)
            a_claims = {c.key: c for c in store.list_claims(a_result.envelope.id)}
            assert a_claims["impl_a.empty_input"].status is ClaimStatus.VERIFIED
            assert a_claims["impl_a.trailing_separator"].status is ClaimStatus.VERIFIED
            empty_input = store.get_knowledge(a_claims["impl_a.empty_input"].id)
            b_attempt = store.list_attempts(tasks["B"].id)[0]
            b_intent = store.get_intent_for_subject(b_attempt.id)
            offered = {item["id"]: item["version"] for item in b_intent.config["knowledge"]}
            assert offered == {c.id: 1 for c in a_claims.values()}
            assert (
                b_intent.config["retrieval_version"] == "retrieval-v1"
                and b_intent.config["role"] == "worker"
            )
            assert empty_input.id in str(b_intent.config["message"]["content"])
            assert seq(events, "AttemptCreated", attempt_id=b_attempt.id) > seq(
                events, "KnowledgeCommitted", task_id=tasks["A"].id
            )
            b_result = store.get_result(tasks["B"].accepted_result_id)
            assert set(b_result.envelope.used_knowledge) == set(offered)
            assert tasks["B"].id in store.get_knowledge(empty_input.id).used_by
            assert any(
                e.type == "KnowledgeUsed"
                and e.task_id == tasks["B"].id
                and e.payload["knowledge_id"] == empty_input.id
                for e in events
            )
            # ---- S4-08: C read the untrusted document, was refused the tool it asked for, and its claim is unsupported
            reads = [
                c
                for c in orchestrator.assembled.gateway.calls
                if c["tool"] == "workspace_read_file"
                and c["arguments"]["path"] == "docs/vendor_notes.md"
            ]
            assert reads and all(c["trust"] == "untrusted_external" for c in reads)
            assert not [
                c
                for c in orchestrator.assembled.gateway.calls
                if c["tool"] == "run_tests"
                and c["run_id"] == store.list_attempts(tasks["C"].id)[0].agent_id
            ]
            c_claim = store.list_claims(
                store.get_result(tasks["C"].accepted_result_id).envelope.id
            )[0]
            assert c_claim.confidence_metadata["evidence_trust"] == ["untrusted_external"]
            # ---- S4-03: C's claim contradicts A's knowledge → DISPUTED, arbitrated by an external check
            assert (
                c_claim.status is ClaimStatus.DISPUTED
                and c_claim.confidence_metadata["grade"] == "disputed"
            )
            assert store.get_knowledge(c_claim.id) is None
            conflict = store.list_conflicts(mission.id)[0]
            assert conflict["state"] == "RESOLVED" and conflict["key"] == "impl_a.empty_input"
            k_attempts = store.list_attempts(tasks["K"].id)
            assert [a.status for a in k_attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert (
                tasks["K"].status is TaskStatus.COMPLETED
                and tasks["K"].id == store.list_tasks(mission.id)[-1].id
            )
            resolution = store.get_knowledge(conflict["resolution_knowledge_id"])
            assert resolution.status == "VERIFIED" and set(resolution.resolves) == {
                empty_input.id,
                c_claim.id,
            }
            assert store.get_knowledge(empty_input.id).confirmed_by == (resolution.id,)
            assert store.get_claim(c_claim.id).resolved_by == resolution.id
            layers = {
                v["layer"]: v["status"]
                for v in store.list_verifications(tasks["K"].accepted_result_id)
            }
            assert layers["critic_review"] == "PASS" and layers["code_test"] == "PASS"
            # ---- S4-05: the synthesis waited for the conflict, failed once on its own error, then passed
            assert set(tasks["S"].dependency_ids) == {
                tasks["A"].id,
                tasks["B"].id,
                tasks["C"].id,
            }  # never rewired
            gated = seq(events, "SynthesisGated", task_id=tasks["S"].id)
            k_done = seq(events, "TaskCompleted", task_id=tasks["K"].id)
            s_attempts = store.list_attempts(tasks["S"].id)
            s_created = seq(events, "AttemptCreated", attempt_id=s_attempts[0].id)
            assert gated is not None and gated < k_done < s_created
            assert [a.status for a in s_attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert s_attempts[0].failure["failures"][0]["layer"] == "code_test"
            s_result = store.get_result(tasks["S"].accepted_result_id)
            verified = {k.id for k in store.list_knowledge(mission.id, status="VERIFIED")}
            assert (
                s_result.envelope.used_knowledge
                and set(s_result.envelope.used_knowledge) <= verified
            )
            # the resolution confirms A's knowledge on the same subject: retrieval offers the
            # subject once (D4-9 dedupe), so the synthesis cites A's record, and the lineage
            # reaches the arbitration through confirmed_by
            assert empty_input.id in s_result.envelope.used_knowledge
            # ---- lineage (30-27): the final result depends on A, B and K's knowledge and Agents
            lineage = final.final_report["lineage"]
            assert lineage["terminal_task_id"] == tasks["S"].id
            assert set(lineage["tasks"]) >= {
                tasks["S"].id,
                tasks["A"].id,
                tasks["B"].id,
                tasks["K"].id,
            }
            assert {k["id"] for k in lineage["knowledge"]} >= set(
                s_result.envelope.used_knowledge
            ) | {resolution.id}
            assert [c["id"] for c in lineage["claims"]] == [
                c_claim.id
            ]  # the contested claim is history on the path
            assert any(e.get("confirmed_by") == resolution.id for e in lineage["edges"])
            assert k_attempts[1].agent_id in lineage["agents"]
            assert len(lineage["agents"]) >= 4 and all(
                a.startswith("agent-") for a in lineage["agents"]
            )
            assert final.final_report["unresolved_conflicts"] == []
            assert final.final_report["graph_version"] == 2
            assert final.stop_reason == "verification_passed"

    asyncio.run(case())
