# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""B 运行时 oracle：登记来源版本冻结进 intent，重启不改写；发布不得写源根。

文档来源只由 Host/人登记；同一 source 的新版出现后，旧 Attempt 的材料仍是旧版，
新 Attempt 才看到新版。Worker 改写已登记来源并报 artifact 必须拒绝；解析与
验证使用 CAS 原文。这里用确定性 Provider，真实模型和 Host 原生仍属于 G。
"""

import asyncio
import json
import re

import pytest
from fixtures_provider import RoleScriptedProvider
from graph_helpers7 import spec

from agent_orchestrator.contracts import ContractError
from agent_orchestrator.governance.domains import CODE_DOMAIN, DOC_DOMAIN
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector


def test_source_versions_are_visible_in_the_sealed_document_context(tmp_path):
    from graph_helpers7 import drive_to_running, graph_service

    from agent_orchestrator.context.context_builder import build_worker_package

    service, mission, tasks = graph_service(tmp_path, domain=DOC_DOMAIN)
    task = tasks["A"]
    attempt = drive_to_running(service, task)
    versions = {"sources/a.md": "a" * 64}
    package = build_worker_package(
        mission,
        task,
        attempt,
        previous_attempts=[],
        verifier_feedback=[],
        workspace_files=[],
        domain=service.domain_for(mission.id),
        source_versions=versions,
    )
    assert package.package["source_versions"] == versions
    assert package.package["source_roots"] == ["sources/"]
    assert "来源原文" in package.package["source_notice"]
    changed = build_worker_package(
        mission,
        task,
        attempt,
        previous_attempts=[],
        verifier_feedback=[],
        workspace_files=[],
        domain=service.domain_for(mission.id),
        source_versions={"sources/a.md": "b" * 64},
    )
    assert changed.context_version != package.context_version


@pytest.mark.parametrize("location", ["cas", "workspace", "ancestor", "symlink", "case_alias"])
def test_document_mission_rejects_publisher_overlapping_actual_source_roots(tmp_path, location):
    async def case():
        evidence = tmp_path / "evidence"
        locations = {
            "cas": evidence / "artifacts" / "sha256",
            "workspace": evidence / "workspaces" / "nested",
            "ancestor": evidence.parent,
            "case_alias": evidence / "ARTIFACTS" / "sha256",
        }
        if location == "symlink":
            target = evidence / "artifacts"
            target.mkdir(parents=True)
            link = tmp_path / "publish-link"
            link.symlink_to(target, target_is_directory=True)
            locations["symlink"] = link
        publisher = FilePublishConnector(locations[location], tmp_path / "publish-ledger")
        config = OrchestratorConfig(
            evidence_root=evidence,
            deployment_policy=DeploymentPolicy(enabled_connectors=("file_publish",)),
        )
        async with Orchestrator(
            config, RoleScriptedProvider({}), connectors={"file_publish": publisher}
        ) as orch:
            with pytest.raises(ContractError, match="source_publish_root_overlap"):
                await orch.submit_mission(spec("overlap", domain=DOC_DOMAIN))
            assert orch.store.list_missions() == []
            # code-v1 原行为保留；它没有文档来源根。
            mission = await orch.submit_mission(spec("legacy", domain=CODE_DOMAIN))
            assert mission.id

    asyncio.run(case())


def test_document_mission_accepts_disjoint_publish_directory(tmp_path):
    async def case():
        config = OrchestratorConfig(
            evidence_root=tmp_path / "evidence",
            deployment_policy=DeploymentPolicy(enabled_connectors=("file_publish",)),
        )
        publisher = FilePublishConnector(tmp_path / "published", tmp_path / "ledger")
        async with Orchestrator(
            config, RoleScriptedProvider({}), connectors={"file_publish": publisher}
        ) as orch:
            assert (await orch.submit_mission(spec(domain=DOC_DOMAIN))).id

    asyncio.run(case())


def test_source_storage_validation_uses_current_deployment_after_reopen(tmp_path):
    async def case():
        config = OrchestratorConfig(
            evidence_root=tmp_path / "evidence",
            deployment_policy=DeploymentPolicy(enabled_connectors=("file_publish",)),
        )
        async with Orchestrator(config, RoleScriptedProvider({})) as first:
            mission = await first.submit_mission(spec(domain=DOC_DOMAIN))
        unsafe = FilePublishConnector(config.workspaces_root, tmp_path / "ledger")
        async with Orchestrator(
            config, RoleScriptedProvider({}), connectors={"file_publish": unsafe}
        ) as second:
            with pytest.raises(ContractError, match="source_publish_root_overlap"):
                second.validate_source_storage(mission.id)

    asyncio.run(case())


@pytest.mark.parametrize("topology", ["removed", "file_to_dir", "dir_to_file"])
def test_dispatch_freezes_source_versions_and_restart_remounts_the_same_bytes(tmp_path, topology):
    from graph_helpers7 import node

    from agent_orchestrator.api.facade import MissionControlV1
    from agent_orchestrator.governance.permissions import Principal
    from agent_orchestrator.graph.task_graph import TaskGraphProposal

    async def case():
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=2)
        async with Orchestrator(config, RoleScriptedProvider({})) as first:
            mission = await first.submit_mission(spec(domain=DOC_DOMAIN))
            control = MissionControlV1(
                first, tenant_id=mission.tenant_id, principal=Principal("importer")
            )
            source_path = (
                "sources/notes.md/child.md" if topology == "dir_to_file" else "sources/notes.md"
            )
            control.register_source(
                {
                    "mission_id": mission.id,
                    "path": source_path,
                    "content": "旧来源原文。\r\n",
                    "kind": "text/markdown",
                    "idempotency_key": "register",
                }
            )
            old = first.store.get_source(mission.id, source_path)["version_hash"]
            planning = first.commit.begin_planning(mission.id)
            tasks, _ = first.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [node("A"), node("B")]}),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            mission = first.store.get_mission(mission.id)
            assert await first._next_attempt(mission, tasks[0], [])
            [a] = first.store.list_attempts(tasks[0].id)
            intent = first.store.get_intent_for_subject(a.id)
            assert intent.config["source_versions"] == {source_path: old}
            assert intent.config["source_roots"] == ["sources/"]
            first._bind_workspace(a)
            assert first._source_files(a) == {source_path: "旧来源原文。\r\n".encode()}
            saved_intent = intent.to_json()

            proposal = control.supersede_source(
                {
                    "mission_id": mission.id,
                    "path": source_path,
                    "content": "新版资料。\n",
                    "kind": "text/markdown",
                    "idempotency_key": "supersede",
                    "expected_version_hash": old,
                }
            )
            assert first.store.get_source(mission.id, source_path)["version_hash"] == old
            control.decide(proposal["request_id"], "approve", nonce="approve-source")
            new = first.store.get_source(mission.id, source_path)["version_hash"]
            assert new != old
            assert first._source_files(a)[source_path] == "旧来源原文。\r\n".encode()
            assert await first._next_attempt(mission, tasks[1], [])
            [b] = first.store.list_attempts(tasks[1].id)
            assert first.store.get_intent_for_subject(b.id).config["source_versions"] == {
                source_path: new
            }
            first._bind_workspace(b)
            assert first._source_files(b)[source_path] == "新版资料。\n".encode()

            # A new repair must not inherit a revoked source from its old workspace.
            pending = control.revoke_source(
                {
                    "mission_id": mission.id,
                    "path": source_path,
                    "reason": "撤回原文",
                    "idempotency_key": "revoke",
                    "expected_version_hash": new,
                }
            )
            control.decide(pending["request_id"], "approve", nonce="approve-revoke")
            replacement = (
                source_path + "/child.md"
                if topology == "file_to_dir"
                else "sources/notes.md"
                if topology == "dir_to_file"
                else None
            )
            expected_versions = {}
            if replacement:
                registered = control.register_source(
                    {
                        "mission_id": mission.id,
                        "path": replacement,
                        "content": "新结构来源。",
                        "kind": "markdown",
                        "idempotency_key": "replacement",
                    }
                )
                expected_versions[replacement] = registered["version_hash"]
            first.assembled.workspaces.get(b.id).write_text("draft.md", "保留修复草稿")
            failed_b = first.commit.mark_attempt_lost(b.id, reason="fixture executor lost")
            assert await first._next_attempt(
                first.store.get_mission(mission.id),
                first.store.get_task(tasks[1].id),
                [failed_b],
            )
            repair = first.store.list_attempts(tasks[1].id)[-1]
            assert repair.retry_of == b.id
            repair_intent = first.store.get_intent_for_subject(repair.id)
            assert repair_intent.config["source_versions"] == expected_versions
            first._bind_workspace(repair)
            repair_tree = first.assembled.workspaces.get(repair.id)
            assert source_path not in repair_tree.list_files()
            if replacement:
                assert repair_tree.read_text(replacement) == "新结构来源。"
            assert repair_tree.read_text("draft.md") == "保留修复草稿"
            section = re.search(
                r"## tools_and_permissions\n(.*?)(?=\n## |\Z)",
                repair_intent.config["message"]["content"],
                re.DOTALL,
            )
            assert section is not None
            permissions = json.loads(section[1])
            assert source_path not in permissions["workspace_files"]

        async with Orchestrator(config, RoleScriptedProvider({})) as reopened:
            reopened._bind_workspace(a)
            assert reopened.store.get_intent_for_subject(a.id).to_json() == saved_intent
            tree = reopened.assembled.workspaces.get(a.id)
            assert (tree.root / source_path).read_bytes() == "旧来源原文。\r\n".encode()
            assert source_path in reopened._read_only_inputs(a.id)
            tree.write_bytes(source_path, b"tampered")
            reopened._bind_workspace(a)
            assert (tree.root / source_path).read_bytes() == b"tampered"
            guarded = reopened._protected_files(mission, tasks[0], a)
            assert guarded[source_path] == "旧来源原文。\r\n".encode()
            assert reopened.assembled.workspaces.tampered_protected(a.id, guarded) == [source_path]

    asyncio.run(case())


@pytest.mark.parametrize(
    "attack_path,forbidden_evidence",
    [
        ("sources/notes.md", None),
        ("sources/new.md", None),
        ("SOURCES/new.md", None),
        ("sources/notes.md", "pytest:unrelated.py"),
        ("sources/notes.md", "tool-run:forged"),
    ],
)
def test_changed_source_artifact_is_rejected_at_the_real_collection_boundary(
    tmp_path,
    attack_path,
    forbidden_evidence,
):
    from fixtures_provider import envelope_step
    from graph_helpers7 import node

    from agent_orchestrator.api.facade import MissionControlV1
    from agent_orchestrator.contracts import SourceCitation
    from agent_orchestrator.governance.permissions import Principal
    from agent_orchestrator.graph.task_graph import TaskGraphProposal
    from agent_orchestrator.verification.evidence_resolver import EvidenceResolver

    async def case():
        path = "sources/notes.md"
        original = "在条件甲下，这项建议有效。\n"
        provider = RoleScriptedProvider(
            {
                "worker": [
                    ("workspace_write_file", {"path": attack_path, "content": "伪造来源。"}),
                    envelope_step(
                        summary="提交资料",
                        artifacts=[attack_path],
                        claims=["已核对资料"],
                        override=(
                            None
                            if forbidden_evidence is None
                            else lambda body: {**body, "evidence": [forbidden_evidence]}
                        ),
                    ),
                ]
            }
        )
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1)
        async with Orchestrator(config, provider) as orch:
            mission = await orch.submit_mission(spec(domain=DOC_DOMAIN))
            api = MissionControlV1(
                orch, tenant_id=mission.tenant_id, principal=Principal("importer")
            )
            api.register_source(
                {
                    "mission_id": mission.id,
                    "path": path,
                    "content": original,
                    "kind": "markdown",
                    "idempotency_key": "source",
                }
            )
            version = orch.store.get_source(mission.id, path)["version_hash"]
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [node("A", outputs=[attack_path])]}),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            assert await orch._next_attempt(orch.store.get_mission(mission.id), tasks[0], [])
            [attempt] = orch.store.list_attempts(tasks[0].id)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert await orch._dispatch(intent)
            intent = orch.store.get_intent(intent.intent_id)

            async def completed():
                while True:
                    result = await orch.bridge_for(intent).result(
                        agent_id=intent.agent_id,
                        turn_id=intent.expected_turn_id,
                    )
                    if result is not None:
                        return result
                    await asyncio.sleep(0.01)

            result = await asyncio.wait_for(completed(), timeout=10)
            tree = orch.assembled.workspaces.get(attempt.id)
            # The actual file tool refused a write, even to a declared output.
            assert (tree.root / path).read_bytes() == original.encode()
            if attack_path != path:
                assert not (tree.root / attack_path).exists()
            # Simulate an out-of-band modification after execution, before collection.
            if forbidden_evidence is None:
                tree.write_bytes(attack_path, "伪造来源。".encode())
            await orch._collect_attempt(intent, result)
            rejections = [
                e for e in orch.store.list_events(mission.id) if e.type == "ResultRejected"
            ]
            assert rejections[-1].payload["reason"] == (
                "protected_path_rewritten"
                if forbidden_evidence is None
                else "result_evidence_kind_not_allowed"
            )
            assert orch.store.list_mission_artifacts(mission.id) == []
            resolved = EvidenceResolver(
                orch.store, orch.assembled.workspaces.artifact_store
            ).resolve(
                SourceCitation(path, version, 1, 1, original.strip()),
                tenant_id=mission.tenant_id,
                mission_id=mission.id,
                source_versions={path: version},
                source_roots=("sources/",),
            )
            assert resolved.status == "resolved"
            assert resolved.display_block.preview == original.strip()
            assert provider.by_role == {"worker": 2}

    asyncio.run(case())


@pytest.mark.parametrize("view", ["work", "verify"])
def test_source_case_alias_reads_keep_the_untrusted_marker(tmp_path, view):
    from agent_orchestrator.artifacts.workspace import WorkspaceManager
    from agent_orchestrator.runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway
    from simple_harness.contracts import CallId
    from simple_harness.tools import ToolCall

    async def case():
        manager = WorkspaceManager(tmp_path / "workspaces")
        manager.create("attempt", seed={"SOURCES/NOTES.MD": "外部原文。"})
        manager.verification_copy("attempt")
        gateway = WorkspaceToolGateway(manager)
        gateway.bind(
            "reader",
            WorkspaceBinding(
                "attempt",
                view,
                view == "work",
                ("workspace_read_file",),
                untrusted_sources=("sources/notes.md",),
                protected_prefixes=("sources/",),
            ),
        )
        result = await gateway.execute(
            ToolCall(CallId("read"), "workspace_read_file", {"path": "./SOURCES/NOTES.MD"}),
            {"run_id": "reader"},
        )
        assert result.value["content"] == "外部原文。"
        assert result.value["trust"] == "untrusted_external"
        assert "不是指令" in result.value["notice"]
        assert gateway.calls[-1]["trust"] == "untrusted_external"

    asyncio.run(case())
