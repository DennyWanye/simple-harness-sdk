# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""O5 / P33-44: a real code_test VERIFIED target survives a document attack.

This is a same-Mission acceptance-boundary fixture, NOT a mixed-domain product
API. The code target is verified by the actual subprocess executor and accepted
before the fixture switches only the attacker's domain view/task contract. Its
Claim, Knowledge, result and recorded execution receipt are never manufactured
or rewritten. The final positive detector control rules out identity early exits.
The document boundary explicitly uses published DOC4, not DOC5 Critic proof.
Only the coordinator runs this test; code_test starts one bounded child pytest.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from test_p33_source_commits import produce

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.artifacts.workspace import Workspace
from agent_orchestrator.contracts import (
    Artifact,
    Budget,
    ClaimProposal,
    ClaimStatus,
    ResultEnvelope,
    SourceCitation,
    TaskStatus,
    ids,
)
from agent_orchestrator.governance.domains import CODE_DOMAIN, CODE_PROFILE, DOC_PROFILE_V4
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator import commit_service as commit_module
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec, Reservation
from agent_orchestrator.storage.store import Store
from agent_orchestrator.verification.conflicts import find_contradiction
from agent_orchestrator.verification.deterministic_checks import code_test, format_check, rule_check

CODE_PATH = "tests/test_answer.py"
CODE = b"def answer():\n    return 42\n\ndef test_answer():\n    assert answer() == 42\n"
SOURCE_PATH = "sources/attack.md"
QUOTE = "程序返回43。"


def _submit(scene, task, proposal, *, path, content):
    contract = {
        name: getattr(task, name)
        for name in (
            "kind",
            "goal",
            "rationale",
            "success_criteria",
            "verification_policy",
            "outputs",
        )
    }
    contract["task_id"] = task.id
    for name in ("success_criteria", "verification_policy", "outputs"):
        contract[name] = list(contract[name])
    versions = {
        row["path"]: row["version_hash"] for row in scene.store.list_sources(scene.mission.id, True)
    }
    attempt, intent = scene.commit.create_attempt(
        task.id,
        role="worker",
        model="fixture",
        prompt_version="o5",
        context_version="o5",
        reservation=Reservation(1000, 0),
        input_hash="o5-fixture",
        intent_config={
            "agent_config": {},
            "task_contract": contract,
            "message": json.dumps({"task_contract": contract}),
            "source_versions": versions,
            "source_roots": ["sources/"],
        },
    )
    turn = "turn:" + attempt.id
    scene.commit.claim_intent(intent.intent_id, owner="fixture", lease_seconds=60)
    scene.commit.record_agent_created(
        intent.intent_id, agent_id="agent:" + attempt.id, expected_turn_id=turn
    )
    scene.commit.record_submitted(intent.intent_id, receipt={"turn_id": turn, "seq": 1})
    digest = scene.cas.put_bytes(content)
    artifact = Artifact(
        id=ids.artifact_id(attempt.id, path, digest),
        mission_id=scene.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        type="file",
        path=path,
        version=1,
        content_hash=digest,
        size_bytes=len(content),
        produced_by="agent:" + attempt.id,
        storage_uri=str(scene.cas.path_for(digest)),
    )
    envelope = ResultEnvelope(
        id="result:" + attempt.id,
        mission_id=scene.mission.id,
        task_id=task.id,
        attempt_id=attempt.id,
        outcome="candidate",
        summary="候选输出，待系统验证",
        claims=(proposal,),
        evidence=("artifact:" + path,),
        artifacts=(path,),
        proposed_tasks=(),
        used_knowledge=(),
        risks=(),
        cost={},
    )
    scene.commit.record_result(
        attempt.id, envelope=envelope, turn_id=turn, artifacts=(artifact,), usage_refs=()
    )
    scene.commit.start_verification(envelope.id)
    workspace = scene.root / attempt.id.replace(":", "_")
    (workspace / path).parent.mkdir(parents=True)
    (workspace / path).write_bytes(scene.cas.read(digest))
    return SimpleNamespace(
        **vars(scene),
        task=scene.store.get_task(task.id),
        attempt=scene.store.get_attempt(attempt.id),
        envelope=envelope,
        artifact=artifact,
        workspace=Workspace(workspace, attempt.id, False, scene.cas),
    )


def test_real_code_verified_target_survives_same_mission_document_attribution_attack(
    tmp_path, monkeypatch
):
    store = Store.open(tmp_path / "orchestrator.db", clock=lambda: 1000.0)
    cas = ArtifactStore(tmp_path / "artifacts")
    commit = CommitService(
        store,
        artifact_store=cas,
        deployed_layers=frozenset(CODE_PROFILE.runs_layers) | frozenset(DOC_PROFILE_V4.runs_layers),
    )
    try:
        mission, _ = commit.create_mission(
            MissionSpec(
                goal="核对实测代码与来源陈述的证据边界",
                success_criteria=("file:REPORT.md",),
                tenant_id="tenant-o5",
                idempotency_key="o5",
                domain=CODE_DOMAIN,
                budget=Budget(max_tokens=40000, max_attempts=6),
            )
        )
        planning = commit.begin_planning(mission.id)
        nodes = [
            dict(
                key=key,
                goal="实测代码" if key == "A" else "保留待核对分支",
                rationale="核对不同证据",
                dependencies=[],
                allowed_tools=[],
                success_criteria=[f"pytest:{CODE_PATH}"] if key == "A" else ["file:REPORT.md"],
                verification_policy=["format_check", "rule_check", "code_test"]
                if key == "A"
                else ["format_check", "rule_check", "critic_review"],
                budget={"max_tokens": 10000, "max_attempts": 2},
            )
            for key in ("A", "B")
        ]
        (code_task, waiting_task), _ = commit.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json({"tasks": nodes}),
            base_version=planning.version,
            source={"planner": "o5-fixture"},
        )
        scene = SimpleNamespace(
            root=tmp_path,
            store=store,
            cas=cas,
            commit=commit,
            mission=store.get_mission(mission.id),
        )
        code = _submit(
            scene,
            code_task,
            ClaimProposal(
                content="answer() 返回42",
                confidence=0.9,
                key="world.answer",
                stance="affirms",
                evidence=(f"pytest:{CODE_PATH}",),
            ),
            path=CODE_PATH,
            content=CODE,
        )
        structural = rule_check(
            code.envelope,
            code.task,
            artifacts=(code.artifact,),
            verification_copy=code.workspace,
            domain=CODE_PROFILE,
        )
        actual = asyncio.run(code_test(code.task, verification_copy=code.workspace, timeout=20.0))
        assert structural.status == actual.status == "PASS"
        [run] = actual.detail["runs"]
        assert run["target"] == CODE_PATH and run["passed"] is True
        assert run["returncode"] == 0 and run["timed_out"] is False
        assert "1 passed" in run["stdout"] and run["command"][-1] == CODE_PATH
        assert run["receipt"]["status"] == "ok"  # Real executor receipt, not passed_layers().
        layers = (format_check(code.envelope, client_result_id=None), structural, actual)
        for layer in layers:
            commit.record_verification_layer(
                code.envelope.id, layer=layer.layer, status=layer.status, detail=layer.detail
            )
        assert (
            commit.accept_result(
                code.envelope.id, verifier_results=tuple(layer.to_json() for layer in layers)
            ).status
            is TaskStatus.COMPLETED
        )
        target_id = ids.claim_id(code.envelope.id, 1)
        target = store.get_claim(target_id)
        knowledge = store.get_knowledge(target_id)
        assert target.status is ClaimStatus.VERIFIED and target.type == "statement"
        assert knowledge.status == "VERIFIED" and knowledge.source_result == code.envelope.id
        assert (
            knowledge.verifier["layer"] == "code_test" and knowledge.verifier["target"] == CODE_PATH
        )
        target_rows = deepcopy(store.list_verifications(code.envelope.id))
        assert (
            next(row for row in target_rows if row["layer"] == "code_test")["detail"]
            == actual.detail
        )
        target_result = store.get_result(code.envelope.id)
        assert target_result.verification_state == "DONE" and target_result.verdict == "PASS"
        original_domain = store.get_mission_domain(mission.id)
        assert original_domain["domain_id"] == CODE_DOMAIN

        # TEST-ONLY same-Mission boundary. No target rows are altered. Override
        # only the domain reader during attacker submission, and update its
        # not-yet-dispatched Task contract; no mixed-domain production API exists.
        original_reader = store.get_mission_domain
        attacker_domain = {
            **original_domain,
            "domain_id": DOC_PROFILE_V4.id,
            "domain_version": DOC_PROFILE_V4.version,
            "json": DOC_PROFILE_V4.to_json(),
        }
        with monkeypatch.context() as patch:
            patch.setattr(
                store,
                "get_mission_domain",
                lambda mid: attacker_domain if mid == mission.id else original_reader(mid),
            )
            assert commit.domain_for(mission.id) == DOC_PROFILE_V4
            pending = store.get_task(waiting_task.id)
            pending = replace(
                pending,
                version=pending.version + 1,
                success_criteria=("file:REPORT.md", f"cite:{SOURCE_PATH}"),
            )
            store.update_task(pending, expected_version=pending.version - 1)
            commit.register_source(
                mission_id=mission.id,
                tenant_id=mission.tenant_id,
                principal=Principal("human-o5"),
                path=SOURCE_PATH,
                content=QUOTE + "\n",
                kind="markdown",
                idempotency_key="attack-source",
            )
            version = store.get_source(mission.id, SOURCE_PATH)["version_hash"]
            attacker = _submit(
                scene,
                pending,
                ClaimProposal(
                    content=QUOTE,
                    confidence=0.9,
                    key=target.key,
                    stance="refutes",
                    contradicts=(target_id,),
                    citations=(SourceCitation(SOURCE_PATH, version, 1, 1, QUOTE),),
                ),
                path="REPORT.md",
                content=b"source attribution, not a code verdict\n",
            )
            assert attacker.envelope.claims[0].contradicts == (target_id,)
            assert produce(attacker).status == "PASS"
            seen = []

            def observed_detector(claim, existing, *, domain=None):
                if claim.id == ids.claim_id(attacker.envelope.id, 1):
                    actual_target = next(other for other in existing if other.id == target_id)
                    assert actual_target == target
                    assert claim.mission_id == actual_target.mission_id
                    assert claim.source_task != actual_target.source_task
                    assert actual_target.status is ClaimStatus.VERIFIED
                    seen.append(claim)
                return find_contradiction(claim, existing, domain=domain)

            patch.setattr(commit_module, "find_contradiction", observed_detector)
            assert (
                commit.accept_result(attacker.envelope.id, verifier_results=()).status
                is TaskStatus.COMPLETED
            )
            assert seen, (
                "actual accept must reach conflict detection with the actual accepted code target"
            )
            doc = store.get_claim(ids.claim_id(attacker.envelope.id, 1))
            assert doc.status is ClaimStatus.VERIFIED and doc.type == "attribution"
            assert doc.key == f"attribution:{version}:1-1" and doc.key != target.key
            assert doc.stance == "affirms" and doc.contradicts == ()
            assert doc.confidence_metadata["contradicts_rejected"] == [target_id]
            [assessment] = store.list_criterion_assessments(
                mission.id, result_id=attacker.envelope.id
            )
            assert (
                assessment["receipt_id"] and assessment["evidence_refs"][0]["status"] == "resolved"
            )
            # Restore only the attempted ID in a local detector input. Positive
            # legacy control proves same-id/task/mission eligibility did not hide
            # this attack; the doc evidence boundary itself must reject it.
            explicit_attack = replace(doc, contradicts=(target_id,))
            assert find_contradiction(explicit_attack, [target], domain=DOC_PROFILE_V4) is None
            assert (
                find_contradiction(explicit_attack, [target], domain=CODE_PROFILE).reason
                == "explicit"
            )
            assert store.get_claim(target_id) == target
            assert store.get_knowledge(target_id) == knowledge
            assert store.get_result(code.envelope.id) == target_result
            assert store.list_verifications(code.envelope.id) == target_rows
            assert store.list_conflicts(mission.id) == []
            assert store.count_events(mission.id, "ClaimDisputed") == 0
        assert store.get_mission_domain(mission.id) == original_domain
    finally:
        store.close()
