# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Doc5 final acceptance oracle, written before the gate implementation.

A policy declaration or caller PASS is not a Critic execution. Real Worker and
Critic turns produce the result, rule receipt, submitted/settled intent and layer.
Stop immediately before acceptance, then attack only the persisted proof. Missing,
skipped, invented or cross-result proof must leave all formal state unchanged.
Real PASS and NEEDS_HUMAN plus the matching human approval remain acceptable after
reopen. Old frozen doc4/code keep their existing direct-commit semantics.
These are deterministic Provider wiring tests, not model quality evidence.
"""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, package_of
from graph_helpers7 import node, spec
from helpers_step07 import ALICE

from agent_orchestrator.contracts import TaskStatus
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.governance import domains
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitRejected
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import InjectedCrash


class AtAcceptance(Exception):
    """Stop after real verification, before any acceptance writes."""


def _provider(*, human=False, copies=1, failed_first=False, critic_fail=False):
    def verdict(request):
        reads = [
            json.loads(message.content)
            for message in request.messages
            if str(message.role) == "tool"
        ]
        assert reads[-1]["value"]["content"] == "# 报告\n风险：材料有限。\n"
        body = {
            "verdict": "FAIL" if critic_fail else "PASS",
            "findings": [{"severity": "blocker", "detail": "实际审核不合格"}]
            if critic_fail
            else [],
            "needs_human": human,
            "mission_criteria": [
                {"criterion": c, "met": True, "reason": "已读取实际产物"}
                for c in package_of(request)["mission_success_criteria"]
            ],
        }
        return "<critic_verdict>" + json.dumps(body, ensure_ascii=False) + "</critic_verdict>"

    return RoleScriptedProvider(
        {
            "worker": [
                (
                    "workspace_write_file",
                    {"path": "report.md", "content": "# 报告\n风险：材料有限。\n"},
                ),
                envelope_step(
                    summary="报告已写入", artifacts=["report.md"], claims=["报告已写入 report.md。"]
                ),
            ]
            * copies,
            "critic": (["invalid verdict"] if failed_first else [])
            + [("workspace_read_file", {"path": "report.md"}), verdict] * copies,
        }
    )


async def _prepared(
    orch,
    monkeypatch,
    *,
    label="gate",
    profile=None,
    human=False,
    grant=True,
    critic_fail=False,
    before_human_request=False,
):
    with monkeypatch.context() as patch:
        if profile is not None:
            patch.setattr(domains, "DOMAINS", {**domains.DOMAINS, profile.id: profile})
        mission = await orch.submit_mission(
            spec(
                label,
                domain=(profile or domains.DOC_PROFILE).id,
                success_criteria=("file:report.md",),
            )
        )
    planning = orch.commit.begin_planning(mission.id)
    [task], _ = orch.commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    node(
                        "A",
                        tokens=60_000,
                        outputs=["report.md"],
                        success_criteria=["file:report.md"],
                        verification_policy=["format_check", "rule_check", "critic_review"],
                    )
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "acceptance-oracle"},
    )
    assert await orch._next_attempt(orch.store.get_mission(mission.id), task, [])
    [attempt] = orch.store.list_attempts(task.id)
    intent = orch.store.get_intent_for_subject(attempt.id)
    assert await orch._dispatch(intent)
    intent = orch.store.get_intent(intent.intent_id)

    async def completed():
        while True:
            result = await orch.bridge_for(intent).result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
            if result is not None:
                return result
            await asyncio.sleep(0.01)

    result = await asyncio.wait_for(completed(), 15)
    await orch._collect_attempt(intent, result)
    stored = orch.store.find_result_for_attempt(attempt.id)
    assert stored is not None, orch.progress_log[-12:]

    def stop(*args, **kwargs):
        raise AtAcceptance

    with monkeypatch.context() as patch:
        patch.setattr(orch.commit, "accept_result", stop)
        if critic_fail:
            patch.setattr(orch.commit, "fail_result", stop)
        if before_human_request:
            patch.setattr(orch.commit, "suspend_verification", stop)
        if human and not before_human_request:
            assert await asyncio.wait_for(orch._verify(stored.envelope.id), 15)
            assert orch.store.get_result(stored.envelope.id).verification_state == "SUSPENDED"
            [request] = orch.store.list_approvals(mission.id)
            if grant:
                orch.commit.review_result(
                    request["request_id"],
                    principal=ALICE,
                    verdict="pass",
                    note="已审阅实际报告",
                    nonce="doc5-human",
                )
        if not human or grant or before_human_request:
            with pytest.raises(AtAcceptance):
                await asyncio.wait_for(orch._verify(stored.envelope.id), 15)
    rows = orch.store.list_verifications(stored.envelope.id)
    assert next(row for row in rows if row["layer"] == "rule_check")["status"] == "PASS"
    critic = next(row for row in rows if row["layer"] == "critic_review")
    assert critic["status"] == ("FAIL" if critic_fail else "NEEDS_HUMAN" if human else "PASS")
    actual = orch.store.get_intent(critic["detail"]["critic_intent_id"])
    assert actual.state == "SETTLED" and actual.receipt
    return mission, task, stored, critic, actual


@pytest.mark.parametrize("original", ["FAIL", "NEEDS_HUMAN"])
def test_doc5_cannot_rewrite_the_actual_critic_verdict_to_pass(tmp_path, monkeypatch, original):
    """Real settled FAIL/human verdicts cannot be laundered through a mutable row.

    NEEDS_HUMAN stops before approval creation: no pending request masks the
    missing original-verdict check. These use the real layer writer for tampering.
    """

    async def run():
        human = original == "NEEDS_HUMAN"
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path),
            _provider(human=human, critic_fail=not human),
        ) as orch:
            mission, task, stored, row, intent = await _prepared(
                orch,
                monkeypatch,
                human=human,
                critic_fail=not human,
                before_human_request=human,
            )
            assert row["status"] == original and intent.state == "SETTLED"
            assert not orch.store.list_approvals(mission.id)
            proof = orch.store.get_receipt("critic-verdict:" + intent.intent_id)
            assert proof is not None
            assert proof["verdict"]["verdict"] == ("PASS" if human else "FAIL")
            assert proof["verdict"]["needs_human"] is human
            forged = {**row["detail"], "verdict": "PASS", "findings": [], "needs_human": False}
            orch.commit.record_verification_layer(
                stored.envelope.id,
                layer="critic_review",
                status="PASS",
                detail=forged,
            )
            changed = next(
                r
                for r in orch.store.list_verifications(stored.envelope.id)
                if r["layer"] == "critic_review"
            )
            assert changed["status"] == "PASS" and changed["detail"] == forged
            before = orch.store.snapshot(mission.id)
            with pytest.raises(CommitRejected, match="[Cc]ritic"):
                orch.commit.accept_result(
                    stored.envelope.id, verifier_results=[], owner=orch._owner
                )
            assert orch.store.snapshot(mission.id) == before
            assert orch.store.get_task(task.id).status != TaskStatus.COMPLETED
            assert orch.store.get_receipt("critic-verdict:" + intent.intent_id) == proof

    asyncio.run(run())


def test_doc5_critic_proof_replay_cannot_overwrite_original_output(tmp_path, monkeypatch):
    """Same SDK output is idempotent; another parsed output cannot replace it."""

    async def run():
        async with Orchestrator(OrchestratorConfig(evidence_root=tmp_path), _provider()) as orch:
            mission, _, stored, row, intent = await _prepared(orch, monkeypatch)
            result = await orch.bridge_for(intent).result(
                agent_id=intent.agent_id,
                turn_id=intent.expected_turn_id,
            )
            assert result is not None
            key = "critic-verdict:" + intent.intent_id
            proof = orch.store.get_receipt(key)
            assert proof is not None
            assert proof["result_id"] == stored.envelope.id
            assert proof["verdict"]["verdict"] == "PASS"
            assert proof["input_hash"] == intent.input_hash
            assert (proof["agent_id"], proof["turn_id"]) == (result.agent_id, result.turn_id)
            assert (
                proof["output_hash"]
                == sha256(result.public_output.content.encode("utf-8")).hexdigest()
            )
            before = orch.store.snapshot(mission.id)
            orch.commit.settle_critic_verdict(intent.intent_id, result=result)
            assert orch.store.get_receipt(key) == proof
            assert orch.store.snapshot(mission.id) == before
            body = {
                **proof["verdict"],
                "verdict": "FAIL",
                "findings": [{"severity": "blocker", "detail": "replacement"}],
            }
            different = replace(
                result,
                public_output=replace(
                    result.public_output,
                    content="<critic_verdict>" + json.dumps(body) + "</critic_verdict>",
                    metadata=dict(result.public_output.metadata),
                ),
            )
            with pytest.raises(CommitRejected, match="[Cc]ritic"):
                orch.commit.settle_critic_verdict(intent.intent_id, result=different)
            assert orch.store.get_receipt(key) == proof
            assert orch.store.get_intent(intent.intent_id).to_json() == intent.to_json()
            assert orch.store.snapshot(mission.id) == before
            assert (
                next(
                    r
                    for r in orch.store.list_verifications(stored.envelope.id)
                    if r["layer"] == "critic_review"
                )
                == row
            )

    asyncio.run(run())


def test_doc5_critic_proof_and_settlement_are_one_transaction(tmp_path, monkeypatch):
    async def run():
        provider = _provider()
        async with Orchestrator(OrchestratorConfig(evidence_root=tmp_path), provider) as orch:
            settle = orch.commit._settle_intent

            def crash(intent, state):
                if intent.kind == "critic" and state == "SETTLED":
                    raise InjectedCrash("critic-proof-before-settle")
                return settle(intent, state)

            with monkeypatch.context() as patch:
                patch.setattr(orch.commit, "_settle_intent", crash)
                with pytest.raises(InjectedCrash, match="critic-proof-before-settle"):
                    await _prepared(orch, monkeypatch)
            [intent] = [i for i in orch.store.list_intents("SUBMITTED") if i.kind == "critic"]
            assert intent.state == "SUBMITTED"
            assert orch.store.get_receipt("critic-verdict:" + intent.intent_id) is None
            stored = orch.store.find_result_for_attempt(intent.config["attempt_id"])
            calls = dict(provider.by_role)
            assert await orch._verify(stored.envelope.id)
            assert orch.store.get_intent(intent.intent_id).state == "SETTLED"
            assert orch.store.get_receipt("critic-verdict:" + intent.intent_id) is not None
            assert orch.store.get_result(stored.envelope.id).verdict == "PASS"
            assert provider.by_role == calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "attack",
    [
        "missing",
        "not_required",
        "skipped",
        "invented_pass",
        "other_result",
        "unsettled",
        "wrong_turn",
        "wrong_agent",
        "wrong_input_hash",
        "wrong_prompt",
        "wrong_artifacts",
        "wrong_task",
        "invalid_verdict",
        "hidden_human",
    ],
)
def test_doc5_direct_accept_requires_matching_executed_critic(tmp_path, monkeypatch, attack):
    async def run():
        provider = _provider(copies=2 if attack == "other_result" else 1)
        async with Orchestrator(OrchestratorConfig(evidence_root=tmp_path), provider) as orch:
            mission, task, stored, row, intent = await _prepared(orch, monkeypatch)
            detail = deepcopy(row["detail"])
            status = row["status"]
            if attack == "missing":
                with orch.store.transaction() as connection:
                    connection.execute(
                        "DELETE FROM verifications WHERE result_id=? AND layer='critic_review'",
                        (stored.envelope.id,),
                    )
            elif attack in {
                "not_required",
                "skipped",
                "invented_pass",
                "other_result",
                "wrong_prompt",
                "invalid_verdict",
                "hidden_human",
            }:
                if attack == "not_required":
                    status = "NOT_REQUIRED"
                elif attack == "skipped":
                    status = "SKIPPED"
                elif attack == "invented_pass":
                    detail["critic_intent_id"] = "invented-settled-critic"
                elif attack == "other_result":
                    _, _, other, other_row, _ = await _prepared(orch, monkeypatch, label="other")
                    assert other.envelope.id != stored.envelope.id
                    detail = deepcopy(other_row["detail"])
                elif attack == "wrong_prompt":
                    detail["verifier_version"] = "critic-doc-research-v1"
                elif attack == "invalid_verdict":
                    detail["verdict"] = "FAIL"
                else:
                    detail["needs_human"] = True  # PASS status may not hide escalation.
                orch.store.upsert_verification(
                    result_id=stored.envelope.id,
                    attempt_id=stored.envelope.attempt_id,
                    layer="critic_review",
                    status=status,
                    detail=detail,
                )
            else:
                updates = {}
                config = deepcopy(dict(intent.config))
                if attack == "unsettled":
                    updates["state"] = "SUBMITTED"
                elif attack == "wrong_turn":
                    updates["receipt"] = {**intent.receipt, "turn_id": "other-turn"}
                elif attack == "wrong_agent":
                    updates["receipt"] = {**intent.receipt, "agent_id": "other-agent"}
                elif attack == "wrong_input_hash":
                    updates["input_hash"] = "0" * 64
                else:
                    text = config["message"]["content"]
                    if attack == "wrong_artifacts":
                        [artifact_id] = stored.artifacts
                        original = orch.store.get_artifact(artifact_id).content_hash
                        assert original in text
                        text = text.replace(original, "0" * 64)
                    else:
                        assert task.id in text
                        text = text.replace(task.id, "other-task")
                    config["message"]["content"] = text
                    # Self-consistent hashes cannot replace the actual result binding.
                    updates.update(config=config, input_hash=sha256_hex(config["message"]))
                if attack in {"wrong_input_hash", "wrong_artifacts", "wrong_task"}:
                    # update_intent deliberately updates only mutable columns;
                    # it cannot change frozen config/input_hash. These corruption
                    # oracles must use SQL, then prove the attack actually landed.
                    with orch.store.transaction() as connection:
                        connection.execute(
                            "UPDATE dispatch_intents SET input_hash=?, config_json=?"
                            " WHERE intent_id=?",
                            (
                                updates["input_hash"],
                                json.dumps(config, ensure_ascii=False),
                                intent.intent_id,
                            ),
                        )
                    changed = orch.store.get_intent(intent.intent_id)
                    assert changed.input_hash == updates["input_hash"] != intent.input_hash
                    assert changed.config == config
                    if attack == "wrong_input_hash":
                        assert changed.config == intent.config
                        assert changed.input_hash != sha256_hex(changed.config["message"])
                    else:
                        assert changed.config != intent.config
                        assert changed.input_hash == sha256_hex(changed.config["message"])
                        assert changed.config["message"]["content"] == text
                else:
                    orch.store.update_intent(
                        replace(intent, version=intent.version + 1, **updates),
                        expected_version=intent.version,
                    )
            before = orch.store.snapshot(mission.id)
            with pytest.raises(CommitRejected, match="[Cc]ritic"):
                orch.commit.accept_result(
                    stored.envelope.id,
                    owner=orch._owner,
                    verifier_results=[{"layer": "critic_review", "status": "PASS"}],
                )
            assert orch.store.snapshot(mission.id) == before
            assert orch.store.get_task(task.id).status != TaskStatus.COMPLETED

    asyncio.run(run())


@pytest.mark.parametrize("human,failed_first", [(False, False), (True, False), (True, True)])
def test_doc5_real_critic_and_matching_human_survive_reopen(
    tmp_path,
    monkeypatch,
    human,
    failed_first,
):
    async def run():
        config = OrchestratorConfig(evidence_root=tmp_path)
        provider = _provider(human=human, failed_first=failed_first)
        async with Orchestrator(config, provider, owner="doc5-accept") as first:
            mission, task, stored, row, intent = await _prepared(first, monkeypatch, human=human)
            frozen = intent.to_json()
            original_proof = first.store.get_receipt("critic-verdict:" + intent.intent_id)
            assert original_proof is not None
            calls = dict(provider.by_role)
        async with Orchestrator(config, provider, owner="doc5-accept") as second:
            assert second.store.get_intent(intent.intent_id).to_json() == frozen
            assert second.store.get_receipt("critic-verdict:" + intent.intent_id) == original_proof
            accepted = second.commit.accept_result(
                stored.envelope.id, verifier_results=[], owner="doc5-accept"
            )
            assert accepted.status == TaskStatus.COMPLETED
            before = second.store.snapshot(mission.id)
            second.commit.accept_result(stored.envelope.id, verifier_results=[])
            assert second.store.snapshot(mission.id) == before
            assert provider.by_role == calls
            assert (
                next(
                    r
                    for r in second.store.list_verifications(stored.envelope.id)
                    if r["layer"] == "critic_review"
                )
                == row
            )

    asyncio.run(run())


@pytest.mark.parametrize("field", ["request_id", "principal_id", "ungranted"])
def test_doc5_critic_escalation_cannot_use_a_different_human_proof(tmp_path, monkeypatch, field):
    async def run():
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), _provider(human=True)
        ) as orch:
            mission, _, stored, _, _ = await _prepared(
                orch,
                monkeypatch,
                human=True,
                grant=field != "ungranted",
            )
            human = next(
                row
                for row in orch.store.list_verifications(stored.envelope.id)
                if row["layer"] == "human_review"
            )
            detail = {**human["detail"], field: "other"}
            if field == "ungranted":
                [request] = orch.store.list_approvals(mission.id)
                assert request["state"] != "GRANTED"
                detail = {"request_id": request["request_id"], "principal_id": ALICE.principal_id}
            orch.store.upsert_verification(
                result_id=stored.envelope.id,
                attempt_id=stored.envelope.attempt_id,
                layer="human_review",
                status="PASS",
                detail=detail,
            )
            before = orch.store.snapshot(mission.id)
            with pytest.raises(CommitRejected, match="human PASS"):
                orch.commit.accept_result(
                    stored.envelope.id, verifier_results=[], owner=orch._owner
                )
            assert orch.store.snapshot(mission.id) == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "profile", [domains.DOC_PROFILE_V4, domains.CODE_PROFILE], ids=["frozen-doc4", "code"]
)
def test_legacy_direct_accept_does_not_acquire_doc5_critic_gate(tmp_path, monkeypatch, profile):
    async def run():
        async with Orchestrator(OrchestratorConfig(evidence_root=tmp_path), _provider()) as orch:
            _, _, stored, _, _ = await _prepared(orch, monkeypatch, profile=profile)
            with orch.store.transaction() as connection:
                connection.execute(
                    "DELETE FROM verifications WHERE result_id=? AND layer='critic_review'",
                    (stored.envelope.id,),
                )
            assert orch.commit.domain_for(stored.envelope.mission_id).version == profile.version
            accepted = orch.commit.accept_result(
                stored.envelope.id,
                verifier_results=[{"layer": "rule_check", "status": "PASS"}],
                owner=orch._owner,
            )
            assert accepted.status == TaskStatus.COMPLETED

    asyncio.run(run())
