# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Frozen doc4 runtime owner change: one root intent and one reserved judgment.
Worker and Critic use the real SDK, SQLite and workspace tool gateway; model scripted.
Only Mission creation uses the published profile; recovery runs under today's registry.
"""

import asyncio
import json
import shutil

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step
from graph_helpers7 import node, spec

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import ContractError, MissionStatus, TaskStatus
from agent_orchestrator.governance import domains
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig


@pytest.mark.parametrize("missing_tree", [False, True])
@pytest.mark.parametrize(
    "stage,damage",
    [
        ("pending", None),
        ("created", None),
        ("created", "missing_catalog"),
        ("created", "missing_trust"),
        ("created", "tree_hash"),
        ("created", "request_hash"),
        ("created", "catalog_cleared"),
    ],
)
def test_g_root_judge_reopen_uses_original_view_and_one_intent(
    tmp_path, missing_tree, stage, damage, monkeypatch
):
    path, quote = "sources/a.md", "甲方案需保留前提。"
    version = ""

    def output(body):
        return {
            **body,
            "claims": [
                {
                    "content": quote,
                    "confidence": 1,
                    "citations": [
                        dict(path=path, version=version, start_line=1, end_line=1, quote=quote)
                    ],
                }
            ],
            "evidence": [],
        }

    provider = RoleScriptedProvider(
        {
            "worker": [
                ("workspace_write_file", {"path": "report.md", "content": "分析"}),
                envelope_step(
                    summary="submitted",
                    artifacts=["report.md"],
                    claims=["pending"],
                    override=output,
                ),
            ],
            "critic": [
                "<critic_verdict>"
                + json.dumps(
                    dict(
                        verdict="PASS",
                        findings=[],
                        mission_criteria=[
                            dict(
                                criterion="arbitration:topic",
                                met=True,
                                reason="actual separate critic",
                            )
                        ],
                    )
                )
                + "</critic_verdict>"
            ],
        }
    )

    class Restart(Exception):
        pass

    async def run():
        nonlocal version
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, provider, owner="original") as orch:
            with monkeypatch.context() as patch:
                patch.setattr(
                    domains, "DOMAINS", {**domains.DOMAINS, DOC_DOMAIN: domains.DOC_PROFILE_V4}
                )
                mission = await orch.submit_mission(
                    spec(domain=DOC_DOMAIN, success_criteria=("arbitration:topic",))
                )
            assert domains.resolve_domain(DOC_DOMAIN) is domains.DOC_PROFILE
            assert orch.commit.domain_for(mission.id).to_json() == domains.DOC_PROFILE_V4.to_json()
            api = MissionControlV1(orch, tenant_id=mission.tenant_id, principal=Principal("human"))
            api.register_source(
                dict(
                    mission_id=mission.id,
                    path=path,
                    content=quote + "\n",
                    kind="markdown",
                    idempotency_key="source",
                )
            )
            version = orch.store.get_source(mission.id, path)["version_hash"]
            provider.scripts["critic"].insert(
                0, ("workspace_read_file", {"path": f"mission-sources/{version}/{path}"})
            )
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {"tasks": [node("A", outputs=["report.md"], success_criteria=["cite:" + path])]}
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            await orch._next_attempt(orch.store.get_mission(mission.id), tasks[0], [])
            [attempt] = orch.store.list_attempts(tasks[0].id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            await orch._dispatch(intent)
            intent = orch.store.get_intent(intent.intent_id)

            async def collect():
                while True:
                    value = await orch.bridge_for(intent).result(
                        agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                    )
                    if value is not None:
                        return value
                    await asyncio.sleep(0.01)

            await orch._collect_attempt(intent, await asyncio.wait_for(collect(), 10))
            stored = orch.store.find_result_for_attempt(attempt.id)
            await asyncio.wait_for(orch._verify(stored.envelope.id), 10)
            assert orch.store.get_task(tasks[0].id).status is TaskStatus.COMPLETED
            assert provider.by_role == {"worker": 2}
            original_dispatch = orch._dispatch

            async def stop_root(intent):
                if intent.subject_id.startswith(mission.id + ":judge:"):
                    if stage == "created":
                        claimed = orch.commit.claim_intent(
                            intent.intent_id, owner=orch.owner, lease_seconds=0.05
                        )
                        agent_id, _, _ = await orch.bridge_for(claimed).create(
                            creation_key=claimed.creation_key,
                            config_json=claimed.config["agent_config"],
                        )
                        turn = await orch.bridge_for(claimed).expected_turn_id(
                            agent_id=agent_id, input_id=claimed.input_id
                        )
                        orch.commit.record_agent_created(
                            claimed.intent_id, agent_id=agent_id, expected_turn_id=turn
                        )
                    raise Restart()
                return await original_dispatch(intent)

            orch._dispatch = stop_root
            with pytest.raises(Restart):
                await orch._decide(orch.store.get_mission(mission.id))
            frozen = orch.store.get_intent_for_subject(mission.id + ":judge:1")
            assert frozen is not None and frozen.state == (
                "PENDING" if stage == "pending" else "AGENT_CREATED"
            )
            frozen_config = dict(frozen.config)
            view = orch.assembled.workspaces.verification_view(frozen.config["attempt_id"])
            catalog = frozen.config["mission_source_catalog"]
            assert len(catalog["entries"]) == 1
            entry = catalog["entries"][0]
            assert entry["version"] == version
            assert view.resolve(entry["mounted_path"]).read_bytes() == (quote + "\n").encode()
            assert "mission_source_catalog" in frozen.config["message"]["content"]
            reserved = orch.store.count_events(mission.id, "BudgetReserved")
            old_owner = orch._owner
            if missing_tree:
                shutil.rmtree(view.root)
            if damage:
                damaged = json.loads(json.dumps(frozen_config))
                if damage == "missing_catalog":
                    damaged.pop("mission_source_catalog")
                elif damage == "missing_trust":
                    damaged["untrusted_sources"] = []
                elif damage == "tree_hash":
                    damaged["mission_judge_tree"]["hash"] = "0" * 64
                elif damage == "catalog_cleared":
                    from agent_orchestrator.contracts.models import sha256_hex

                    catalog = {"schema": 1, "entries": []}
                    tree = {
                        "schema": 1,
                        "files": {
                            p: h
                            for p, h in damaged["mission_judge_tree"]["files"].items()
                            if not p.startswith("mission-sources/")
                        },
                    }
                    damaged["mission_source_catalog"] = {**catalog, "hash": sha256_hex(catalog)}
                    damaged["mission_judge_tree"] = {**tree, "hash": sha256_hex(tree)}
                    damaged["untrusted_sources"] = []
                else:
                    damaged["message"]["content"] += "tampered"
                with orch.store.transaction() as db:
                    db.execute(
                        "UPDATE dispatch_intents SET config_json=? WHERE intent_id=?",
                        (json.dumps(damaged), frozen.intent_id),
                    )
        async with Orchestrator(config, provider, owner="resumed") as resumed:
            assert domains.resolve_domain(DOC_DOMAIN) is domains.DOC_PROFILE
            assert (
                resumed.commit.domain_for(mission.id).to_json() == domains.DOC_PROFILE_V4.to_json()
            )
            assert resumed._owner != old_owner
            if damage:
                with pytest.raises(ContractError):
                    await asyncio.wait_for(resumed.run(), 15)
                failed = resumed.store.get_mission(mission.id)
                assert failed.status is MissionStatus.FAILED
                assert failed.stop_reason == "verifier_unavailable"
                assert provider.by_role == {"worker": 2}
                assert resumed.store.count_events(mission.id, "BudgetReserved") == reserved
                return
            await asyncio.wait_for(resumed.run(), 15)
            final = resumed.store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, resumed.progress_log
            current = resumed.store.get_intent_for_subject(frozen.subject_id)
            assert current.intent_id == frozen.intent_id and current.config == frozen_config
            assert resumed.store.get_intent_for_subject(mission.id + ":judge:2") is None
            assert resumed.store.count_events(mission.id, "BudgetReserved") == reserved
            assert provider.by_role == {"worker": 2, "critic": 2}
            from fixtures_provider import role_of

            requests = [r for r in provider.requests if role_of(r) == "critic"]
            assert quote in str(requests[-1].messages)
            assert "untrusted" in str(requests[-1].messages).lower()
            assert final.final_report["success_criteria"][0]["source"] == "independent"
            assert (
                resumed.assembled.workspaces.verification_view(frozen.config["attempt_id"])
                .resolve(entry["mounted_path"])
                .read_bytes()
                == (quote + "\n").encode()
            )

    asyncio.run(run())
