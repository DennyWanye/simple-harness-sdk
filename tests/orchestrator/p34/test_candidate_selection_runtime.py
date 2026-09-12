# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""COMPARE first oracle: real SDK writes/collects/verifies before any acceptance.

Two complete candidates stay non-final, a distinct Synthesizer writes C, and C
must meet the original complete contract. The real policy registry records a
fixture evaluation explicitly as fixture evidence, then authenticated approval
and promotion. This proves software transactions, not model comparison quality.
No state/receipt PASS is supplied to the result verification or acceptance gate.
"""

import asyncio
from dataclasses import replace

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, envelope_step, package_of

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import Budget, TaskStatus
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.promotion import code_versions
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    MissionSpec,
    Reservation,
    task_account,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig


def selection_policy():
    return {
        "schema_version": 1,
        "mode": "COMPARE_THEN_SYNTHESIZE",
        "max_candidates": 2,
        "deadline_seconds": 120.0,
        "tie_break": "verified_rank_then_result_id-v1",
        "synthesis_limit": 1,
        "on_deadline": "best_complete_else_stop",
        "synthesis_reserve": {"tokens": 16000, "cost_micros": 0, "tool_calls": 0},
        "synthesis_attempts_reserved": 1,
    }


def approve(orch):
    base = orch.store.active_policy()
    params = {**base["params"], "schema_version": 2, "search_selection": selection_policy()}
    proposal = orch.commit.propose_policy(
        params,
        manifest={"oracle": "selection-policy-mechanism"},
        source="fixture",
        principal=Principal("test-host"),
    )
    orch.commit.record_policy_evaluation(
        proposal["proposal_id"],
        verdict="PASSED",
        reasons=["fixture approval path control"],
        report_hash=sha256_hex({"fixture": "selection-policy-mechanism"}),
        baseline_version_id=base["version_id"],
        code_versions=code_versions(),
        evidence_kind="fixture",
    )
    orch.commit.decide_policy(
        proposal["proposal_id"],
        principal=Principal("test-host"),
        decision="approve",
        nonce="selection-approval",
    )
    orch.commit.promote_policy(
        proposal["proposal_id"],
        principal=Principal("test-host"),
        cooldown_seconds=0,
        accept_fixture_evidence=True,
    )
    return proposal["version_id"]


def script(label, *, missing=False):
    files = ["a.txt"] if missing else ["a.txt", "b.txt"]
    return [
        *(
            ("workspace_write_file", {"path": path, "content": f"{label}:{path}\n"})
            for path in files
        ),
        envelope_step(summary=label, artifacts=files, claims=[f"candidate {label}"]),
    ]


async def setup(tmp_path, *, compare=True, missing=False, document=False, on_synthesis=None):
    seen_inputs = []
    quote = "本次观察仅适用于离线实验。"
    versions = {}

    def candidate_script(label, *, missing=False):
        steps = script(label, missing=missing)
        if not document:
            return steps
        path = "sources/b.md" if label == "B" else "sources/a.md"

        def citation(body):
            body["claims"][0]["citations"] = [
                {
                    "path": path,
                    "version": versions[path],
                    "start_line": 1,
                    "end_line": 1,
                    "quote": quote,
                }
            ]
            return body

        return [
            *steps[:-1],
            envelope_step(
                summary=label,
                artifacts=["a.txt"] if missing else ["a.txt", "b.txt"],
                claims=[quote],
                override=citation,
            ),
        ]

    def read_candidate(request):
        package = package_of(request)
        inputs = package["selection_inputs"]["inputs"]
        assert len(inputs) == 4
        assert len({i["path"] for i in inputs}) == 4
        seen_inputs.extend(inputs)
        if on_synthesis is not None:
            on_synthesis(orch, task_id=package["task_contract"]["task_id"])
        return ("workspace_read_file", {"path": inputs[0]["path"]})

    provider = RoleScriptedProvider(
        {
            "worker": candidate_script("A") + candidate_script("B"),
            "synthesizer": [read_candidate, *candidate_script("C", missing=missing)],
            "critic": [
                ("workspace_read_file", {"path": "a.txt"}),
                critic_step(verdict="PASS", criteria_met=True),
            ]
            * 4,
        }
    )
    orch = Orchestrator(
        OrchestratorConfig(
            evidence_root=tmp_path,
            max_concurrency=1,
            candidates_per_task=2,
            attempt_reserve_tokens=4000,
        ),
        provider,
    )
    await orch.__aenter__()
    version = approve(orch)
    spec = MissionSpec(
        "deliver both complete files",
        ("file:a.txt", "file:b.txt", *((quote,) if document else ())),
        "test",
        "compare",
        allowed_tools=("workspace_read_file", "workspace_write_file"),
        budget=Budget(max_tokens=100000, max_attempts=12),
    )
    if compare:
        spec = replace(spec, search_policy_version_id=version)
    if document:
        spec = replace(spec, domain=DOC_DOMAIN)
    mission = await orch.submit_mission(spec)
    if document:
        api = MissionControlV1(orch, tenant_id="test", principal=Principal("source-host"))
        for path in ("sources/a.md", "sources/b.md"):
            api.register_source(
                {
                    "mission_id": mission.id,
                    "path": path,
                    "content": quote + "\n",
                    "kind": "markdown",
                    "idempotency_key": "source:" + path,
                }
            )
            versions[path] = orch.store.get_source(mission.id, path)["version_hash"]
    planning = orch.commit.begin_planning(mission.id)
    tasks, _ = orch.commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    {
                        "key": "one",
                        "goal": "deliver both complete files",
                        "rationale": "full contract",
                        "dependencies": [],
                        "success_criteria": list(spec.success_criteria),
                        "verification_policy": ["format_check", "rule_check", "critic_review"],
                        "allowed_tools": ["workspace_read_file", "workspace_write_file"],
                        "budget": {"max_tokens": 60000, "max_attempts": 4},
                    }
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "fixture"},
    )
    return orch, provider, tasks[0]


async def until(orch, predicate):
    async def drive():
        for _ in range(500):
            await orch._cycle()
            if predicate():
                return
            await asyncio.sleep(0.002)
        raise AssertionError("selection did not converge in bounded cycles")

    await asyncio.wait_for(drive(), 12)


@pytest.mark.parametrize("compare,missing", [(False, False), (True, False), (True, True)])
def test_actual_candidates_and_c_use_one_original_contract(tmp_path, compare, missing):
    async def run():
        orch, provider, task = await setup(tmp_path, compare=compare, missing=missing)
        try:
            await until(
                orch,
                lambda: (
                    orch.store.get_task(task.id).status
                    in {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                    }
                ),
            )
            saved = orch.store.get_task(task.id)
            attempts = orch.store.list_attempts(task.id)
            assert saved.status is TaskStatus.COMPLETED
            if not compare:
                assert saved.status is TaskStatus.COMPLETED
                assert all(a.role != "synthesizer" for a in attempts)
                assert orch.commit.selection_round(task.id) is None
            else:
                combined = [a for a in attempts if a.role == "synthesizer"]
                assert len(combined) == 1 and len(attempts) == 3
                c = orch.store.find_result_for_attempt(combined[0].id)
                rows = orch.store.list_verifications(c.envelope.id)
                assert any(r["layer"] == "rule_check" for r in rows)
                if missing:
                    assert any(r["layer"] == "rule_check" and r["status"] == "FAIL" for r in rows)
                    assert saved.accepted_result_id != c.envelope.id
                else:
                    assert c.verdict == "PASS" and saved.accepted_result_id == c.envelope.id
                assert saved.success_criteria == task.success_criteria
            completed_events = [
                e
                for e in orch.store.list_events(task.mission_id)
                if e.type == "TaskCompleted" and e.task_id == task.id
            ]
            assert len(completed_events) == 1
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(run())


def test_ready_is_not_pass_and_direct_accept_cannot_bypass_selection(tmp_path):
    async def run():
        orch, _, task = await setup(tmp_path)
        try:
            await until(orch, lambda: bool(orch.commit.selection_candidates(task.id)))
            candidates = orch.commit.selection_candidates(task.id)
            candidate = candidates[0]
            result = orch.store.get_result(candidate["result_id"])
            assert result.verification_state == "RUNNING" and result.verdict is None
            assert orch.store.get_task(task.id).accepted_result_id is None
            assert orch.store.list_knowledge(task.mission_id) == []
            for owner in (None, "", "old-owner"):
                with pytest.raises(CommitRejected):
                    orch.commit.accept_result(result.envelope.id, verifier_results=(), owner=owner)
            assert orch.store.get_result(result.envelope.id) == result
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(run())


def test_ready_reopen_keeps_receipt_deadline_and_does_not_rerun_a(tmp_path):
    async def run():
        orch, provider, task = await setup(tmp_path)
        await until(orch, lambda: bool(orch.commit.selection_candidates(task.id)))
        candidate = orch.commit.selection_candidates(task.id)[0]
        ready = orch.store.get_receipt(candidate["receipt_id"])
        rows = orch.store.list_verifications(candidate["result_id"])
        deadline = orch.commit.selection_round(task.id)["deadline_at"]
        first_intent = orch.store.get_intent_for_subject(candidate["attempt_id"])
        config, owner = orch._config, orch._owner
        await orch.__aexit__(None, None, None)
        async with Orchestrator(config, provider, owner=owner) as reopened:
            await reopened.recover()
            assert reopened.commit.selection_round(task.id)["deadline_at"] == deadline
            assert reopened.store.get_receipt(candidate["receipt_id"]) == ready
            await until(
                reopened, lambda: reopened.store.get_task(task.id).status is TaskStatus.COMPLETED
            )
            assert reopened.store.list_verifications(candidate["result_id"]) == rows
            assert (
                reopened.store.get_intent(first_intent.intent_id).input_hash
                == first_intent.input_hash
            )
            assert reopened.commit.selection_round(task.id)["deadline_at"] == deadline
            assert len(reopened.store.list_attempts(task.id)) == 3
            assert provider.by_role["worker"] == 6  # two real write/write/envelope candidates
            assert provider.by_role["synthesizer"] == 4  # read + new write/write/envelope

    asyncio.run(run())


def test_ready_cold_after_deadline_takes_over_for_final_accept_only(tmp_path, monkeypatch):
    async def run():
        orch, provider, task = await setup(tmp_path)
        await until(orch, lambda: bool(orch.commit.selection_candidates(task.id)))
        candidate = orch.commit.selection_candidates(task.id)[0]
        deadline = orch.commit.selection_round(task.id)["deadline_at"]
        old_calls = provider.calls
        config = orch._config
        await orch.__aexit__(None, None, None)
        async with Orchestrator(config, provider, owner="new-selection-owner") as cold:
            monkeypatch.setattr(cold.store, "_clock", lambda: deadline + 120)
            await cold.recover()
            await until(
                cold,
                lambda: (
                    cold.store.get_task(task.id).status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
                ),
            )
            saved = cold.store.get_task(task.id)
            assert saved.status is TaskStatus.COMPLETED
            assert saved.accepted_result_id == candidate["result_id"]
            round_ = cold.commit.selection_round(task.id)
            assert round_["deadline_at"] == deadline
            assert round_["synthesis_attempt_id"] is None
            decision = cold.store.get_receipt(round_["decision_id"])
            assert decision["action"] == "accept_complete" and decision["reason"] == "deadline"
            assert provider.calls == old_calls  # no pending B/C handoff after the deadline

    asyncio.run(run())


@pytest.mark.parametrize("attack", ["manager", "wrong_subject", "other_attempt", "wrong_account"])
def test_c_tail_refuses_service_identity_diversion(tmp_path, attack):
    async def run():
        orch, _, task = await setup(tmp_path)
        try:
            await until(
                orch,
                lambda: bool(
                    (orch.commit.selection_round(task.id) or {}).get("synthesis_attempt_id")
                ),
            )
            round_ = orch.commit.selection_round(task.id)
            c_id = round_["synthesis_attempt_id"]
            subject = c_id + ":critic:1"
            attempt_id, account_id, kind = c_id, task_account(task.id), "critic"
            if attack == "manager":
                kind = "manager"
            elif attack == "wrong_subject":
                subject = c_id + ":other:1"
            elif attack == "other_attempt":
                attempt_id = round_["attempt_ids"][0]
            else:
                account_id = "budget:" + task.mission_id
            transfers = list(orch.store.connection.execute("SELECT * FROM budget_tail_transfers"))
            with pytest.raises(CommitRejected, match="selection.*service|selection.*Critic"):
                orch.commit.create_service_intent(
                    kind=kind,
                    subject_id=subject,
                    mission_id=task.mission_id,
                    task_id=task.id,
                    attempt_id=attempt_id,
                    account_id=account_id,
                    creation_key=subject,
                    input_id=subject,
                    input_hash="adversarial",
                    config={},
                    reservation=Reservation(100, 0),
                )
            assert orch.store.get_intent_for_subject(subject) is None
            assert (
                list(orch.store.connection.execute("SELECT * FROM budget_tail_transfers"))
                == transfers
            )
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(run())


def test_ready_changed_record_is_invalidated_without_rewriting_original_receipt(tmp_path):
    async def run():
        orch, _, task = await setup(tmp_path)
        try:
            await until(orch, lambda: bool(orch.commit.selection_candidates(task.id)))
            candidate = orch.commit.selection_candidates(task.id)[0]
            original = orch.store.get_receipt(candidate["receipt_id"])
            row = next(
                r
                for r in orch.store.list_verifications(candidate["result_id"])
                if r["layer"] == "rule_check"
            )
            orch.commit.record_verification_layer(
                candidate["result_id"],
                layer="rule_check",
                status="PASS",
                detail={**row["detail"], "post_ready_change": "actual different row"},
            )
            assert (
                next(
                    r
                    for r in orch.store.list_verifications(candidate["result_id"])
                    if r["layer"] == "rule_check"
                )["detail"]["post_ready_change"]
                == "actual different row"
            )
            orch.commit.decide_selection(task.id, owner=orch._owner, command_id="changed-row")
            saved = next(
                c
                for c in orch.commit.selection_candidates(task.id)
                if c["result_id"] == candidate["result_id"]
            )
            assert saved["state"] == "INVALIDATED"
            assert orch.store.get_receipt(candidate["receipt_id"]) == original
            assert orch.store.get_result(candidate["result_id"]).verdict == "FAIL"
            assert orch.store.get_task(task.id).accepted_result_id is None
            assert not orch.store.list_knowledge(task.mission_id)
            await until(orch, lambda: orch.store.get_task(task.id).status is TaskStatus.COMPLETED)
            decision = orch.store.get_receipt(orch.commit.selection_round(task.id)["decision_id"])
            omitted = next(
                item
                for item in decision["considered"]
                if item["result_id"] == candidate["result_id"]
            )
            assert omitted["eligible"] is False and omitted["state"] == "INVALIDATED"
            assert "no longer matches" in omitted["reason"]
            assert decision in orch.store.snapshot(task.mission_id)["search"]["decisions"]
            event = next(
                e
                for e in orch.store.list_events(task.mission_id)
                if e.type == "SelectionDecisionRecorded"
            )
            assert dict(event.payload) == dict(decision)
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(run())


def test_actual_ready_cancel_releases_only_unused_selection_tail(tmp_path):
    async def run():
        orch, _, task = await setup(tmp_path)
        try:
            await until(orch, lambda: bool(orch.commit.selection_candidates(task.id)))
            round_ = orch.commit.selection_round(task.id)
            hold = orch.store.connection.execute(
                "SELECT * FROM budget_tail_holds WHERE hold_id=?", (round_["round_id"],)
            ).fetchone()
            assert hold["state"] == "HELD" and hold["remaining_attempts"] == 1
            orch.commit.cancel_mission(task.mission_id)
            hold = orch.store.connection.execute(
                "SELECT * FROM budget_tail_holds WHERE hold_id=?", (round_["round_id"],)
            ).fetchone()
            assert hold["state"] == "RELEASED" and hold["remaining_attempts"] == 0
            assert orch.commit.selection_round(task.id)["state"] == "INVALIDATED"
            before = orch.store.list_events(task.mission_id)
            orch.commit.cancel_mission(task.mission_id)
            assert orch.store.list_events(task.mission_id) == before
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(run())


def test_ready_decision_requires_nonempty_live_owner(tmp_path, monkeypatch):
    async def run():
        orch, _, task = await setup(tmp_path)
        try:
            await until(orch, lambda: bool(orch.commit.selection_candidates(task.id)))
            candidate = orch.commit.selection_candidates(task.id)[0]
            before = orch.commit.selection_round(task.id)
            for owner in (None, "", " ", "another-owner"):
                with pytest.raises(CommitRejected):
                    orch.commit.decide_selection(task.id, owner=owner, command_id="bad-owner")
            attempt = orch.store.get_attempt(candidate["attempt_id"])
            with monkeypatch.context() as clock:
                clock.setattr(orch.store, "_clock", lambda: attempt.lease_expires_at + 1)
                with pytest.raises(CommitRejected, match="lease"):
                    orch.commit.decide_selection(task.id, owner=orch._owner, command_id="expired")
            assert orch.commit.selection_round(task.id) == before
            assert orch.store.get_result(candidate["result_id"]).verdict is None
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(run())


def revoke_b(orch, *, task_id):
    task = orch.store.get_task(task_id)
    api = MissionControlV1(orch, tenant_id="test", principal=Principal("source-host"))
    old = orch.store.get_source(task.mission_id, "sources/b.md")
    pending = api.revoke_source(
        {
            "mission_id": task.mission_id,
            "path": "sources/b.md",
            "expected_version_hash": old["version_hash"],
            "reason": "撤回第二份原材料",
            "idempotency_key": "revoke-b",
        }
    )
    api.decide(pending["request_id"], "approve", nonce="approve-b-revoke")


@pytest.mark.parametrize("change_during_c", [False, True])
def test_doc_c_inherits_selected_sources_even_without_model_used_knowledge(
    tmp_path, change_during_c
):
    """C cites A only; its real B input must remain a system lineage dependency."""

    async def run():
        orch, _, task = await setup(
            tmp_path, document=True, on_synthesis=revoke_b if change_during_c else None
        )
        try:
            await until(
                orch,
                lambda: (
                    orch.store.get_task(task.id).status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
                ),
            )
            current = orch.store.get_task(task.id)
            c_attempt = next(
                a for a in orch.store.list_attempts(task.id) if a.role == "synthesizer"
            )
            c = orch.store.find_result_for_attempt(c_attempt.id)
            assert c is not None and not c.envelope.used_knowledge
            assert {ref.path for claim in c.envelope.claims for ref in claim.citations} == {
                "sources/a.md"
            }
            assert current.status is TaskStatus.COMPLETED
            if change_during_c:
                assert current.accepted_result_id != c.envelope.id
                assert c.verdict == "FAIL"
                assert any(
                    item["state"] == "INVALIDATED"
                    for item in orch.commit.selection_candidates(task.id)
                )
            else:
                assert current.accepted_result_id == c.envelope.id
                records = orch.store.list_knowledge(task.mission_id)
                assert len(records) == 1
                assert records[0].to_json()["source_versions"] == {
                    path: [orch.store.get_source(task.mission_id, path)["version_hash"]]
                    for path in ("sources/a.md", "sources/b.md")
                }
                original = records[0].to_json()
                revoke_b(orch, task_id=task.id)
                assert orch.store.list_knowledge(task.mission_id)[0].to_json() == original
                mission = orch.store.get_mission(task.mission_id)
                context = orch._gather_knowledge(mission, current, {task.id: current})
                assert records[0].id not in {item["id"] for item in context.verified}
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(run())
