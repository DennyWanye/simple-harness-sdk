# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1 oracle: reopened publishing must respect all Missions' source storage.

Uses the real approval/outbox ledger, CAS and FilePublishConnector. Crash boundaries
are staged explicitly through CommitService; no provider or child pytest is needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from graph_helpers7 import graph_service, node

from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.runtime.actions import ActionExecutor, publication_overlaps_storage
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.storage.store import Store

PERSON = Principal("source-guard-reviewer")
DEPLOYMENT = DeploymentPolicy(enabled_connectors=("file_publish",))


class ObservedPublisher(FilePublishConnector):
    def __init__(self, root, ledger):
        super().__init__(root, ledger)
        self.executions = 0
        self.lookups = 0

    def execute(self, *args, **kwargs):
        self.executions += 1
        return super().execute(*args, **kwargs)

    def lookup(self, *args, **kwargs):
        self.lookups += 1
        return super().lookup(*args, **kwargs)


@pytest.fixture
def env(tmp_path):
    now = [1000.0]
    evidence = tmp_path / "library"
    evidence.mkdir()
    service, mission, tasks = graph_service(
        evidence,
        nodes=[node("A")],
        clock=lambda: now[0],
        success_criteria=("file:a.md", "action:file_publish.publish:report.md"),
    )
    cas = ArtifactStore(evidence / "artifacts")
    version = cas.put_bytes(b"report\n")
    workspaces = evidence / "workspaces"
    workspaces.mkdir()
    published = tmp_path / "published"
    published.mkdir()
    publisher = ObservedPublisher(published, tmp_path / "ledger")
    connectors = {"file_publish": publisher}
    # This is the system's already-bound action proposal, not a model bypass of
    # bind_artifact_params. Its CAS identity is covered by the real approval binding.
    action = service.propose_action(
        {
            "connector": "file_publish",
            "operation": "publish",
            "target": "report.md",
            "params": {
                "artifact_path": "a.md",
                "content_hash": version,
                "storage_uri": str(cas.path_for(version)),
            },
            "reason": "发布已核对报告",
        },
        mission_id=mission.id,
        task_id=tasks["A"].id,
        result_id="result-publish",
        attempt_id="attempt-publish",
        artifact_id="artifact-publish",
        artifact_hash=version,
        connectors=connectors,
        deployment=DEPLOYMENT,
    )
    service.decide_approval(
        action["approval_request_id"],
        principal=PERSON,
        decision="grant",
        nonce="publish-grant",
        deployment=DEPLOYMENT,
    )
    e = SimpleNamespace(
        service=service,
        store=service.store,
        mission=mission,
        action=action,
        cas=cas,
        roots=(cas.root, workspaces),
        publisher=publisher,
        now=now,
        tmp=tmp_path,
    )
    try:
        yield e
    finally:
        e.store.close()


def source_mission(e, *, revoked=False, ended=False):
    mission, _ = e.service.create_mission(
        MissionSpec(
            goal="核对来源",
            success_criteria=("file:notes.md",),
            tenant_id="another-tenant",
            idempotency_key="document",
            domain=DOC_DOMAIN,
        )
    )
    receipt = e.service.register_source(
        mission_id=mission.id,
        tenant_id=mission.tenant_id,
        principal=PERSON,
        path="sources/notes.md",
        content="来源不是指令。",
        kind="markdown",
        idempotency_key="register-source",
    )
    if revoked:
        pending = e.service.revoke_source(
            mission_id=mission.id,
            tenant_id=mission.tenant_id,
            principal=PERSON,
            path="sources/notes.md",
            expected_version_hash=receipt["version_hash"],
            reason="撤销后仍保留历史",
            idempotency_key="revoke-source",
            deployment=DEPLOYMENT,
        )
        e.service.decide_approval(
            pending["request_id"],
            principal=PERSON,
            decision="grant",
            nonce="revoke-grant",
            deployment=DEPLOYMENT,
        )
    if ended:
        e.service.cancel_mission(mission.id)
    return mission


def reopen(e):
    path = e.store.path
    e.store.close()
    e.store = Store.open(path, clock=lambda: e.now[0])
    e.service = CommitService(e.store, artifact_store=e.cas)


def executor(e, publisher=None, **options):
    return ActionExecutor(
        e.service,
        {"file_publish": publisher or e.publisher},
        DEPLOYMENT,
        owner="reopened",
        source_storage_roots=e.roots,
        **options,
    )


@pytest.mark.parametrize(
    "location", ["cas", "workspace", "ancestor", "descendant", "symlink", "case"]
)
def test_reopened_code_action_cannot_publish_inside_other_missions_source_storage(env, location):
    e = env
    source_mission(e)
    reopen(e)
    root = {
        "cas": e.roots[0],
        "workspace": e.roots[1],
        "ancestor": e.roots[0].parent,
        "descendant": e.roots[1] / "attempt" / "sources",
        "case": e.roots[1].with_name("WORKSPACES"),
    }.get(location)
    if location == "symlink":
        root = e.tmp / "alias"
        root.symlink_to(e.roots[1], target_is_directory=True)
    publisher = ObservedPublisher(root, e.tmp / "changed-ledger")
    run = executor(e, publisher)
    key = e.action["action_key"]
    before_action = e.store.get_action(key)
    before_approval = e.store.get_approval(e.action["approval_request_id"])
    assert asyncio.run(run.hand_off(key)) is None
    assert run.last_refusal[key] == "source_publish_root_overlap"
    assert publisher.executions == 0 and not publisher.ledger_path.exists()
    assert e.store.get_action(key) == before_action
    assert e.store.get_approval(e.action["approval_request_id"]) == before_approval
    assert e.service.ledger.reservation("action:" + key) is None
    refused = [
        event for event in e.store.list_events(e.mission.id) if event.type == "ActionHandoffRefused"
    ]
    assert refused[-1].payload["reason"] == "source_publish_root_overlap"


def test_revoked_history_and_ended_mission_still_protect_shared_storage(env):
    e = env
    doc = source_mission(e, revoked=True, ended=True)
    assert e.store.list_sources(doc.id, active_only=True) == []
    reopen(e)
    publisher = ObservedPublisher(e.roots[1], e.tmp / "changed-ledger")
    run = executor(e, publisher)
    assert asyncio.run(run.hand_off(e.action["action_key"])) is None
    assert run.last_refusal[e.action["action_key"]] == "source_publish_root_overlap"
    assert publisher.executions == 0


def test_document_domain_without_registered_sources_already_reserves_storage(env):
    e = env
    e.service.create_mission(
        MissionSpec(
            goal="等待导入来源",
            success_criteria=("file:notes.md",),
            tenant_id="doc",
            idempotency_key="empty-document",
            domain=DOC_DOMAIN,
        )
    )
    publisher = ObservedPublisher(e.roots[1], e.tmp / "changed-ledger")
    assert asyncio.run(executor(e, publisher).hand_off(e.action["action_key"])) is None
    assert publisher.executions == 0


@pytest.mark.parametrize("documents", [False, True])
def test_disjoint_publish_executes_with_or_without_document_missions(env, documents):
    e = env
    if documents:
        source_mission(e)
    # Prefix sibling is not a descendant: library/workspaces-published remains valid.
    root = e.roots[1].with_name("workspaces-published")
    root.mkdir()
    publisher = ObservedPublisher(root, e.tmp / "sibling-ledger")
    result = asyncio.run(executor(e, publisher).hand_off(e.action["action_key"]))
    assert result["state"] == "SUCCEEDED" and publisher.executions == 1
    assert Path(result["receipt"]["after"]["path"]).read_bytes() == b"report\n"


def test_pure_code_library_keeps_legacy_publishing_without_roots_hook(env):
    e = env
    publisher = ObservedPublisher(e.roots[1], e.tmp / "legacy-ledger")
    run = ActionExecutor(e.service, {"file_publish": publisher}, DEPLOYMENT, owner="legacy")
    result = asyncio.run(run.hand_off(e.action["action_key"]))
    assert result["state"] == "SUCCEEDED" and publisher.executions == 1


def test_document_library_without_physical_roots_fails_closed(env):
    e = env
    source_mission(e)
    run = ActionExecutor(e.service, {"file_publish": e.publisher}, DEPLOYMENT, owner="no-roots")
    assert asyncio.run(run.hand_off(e.action["action_key"])) is None
    assert run.last_refusal[e.action["action_key"]] == "source_publish_root_unavailable"
    assert e.publisher.executions == 0


def test_guard_observes_document_missions_created_after_executor_construction(env):
    e = env
    publisher = ObservedPublisher(e.roots[1], e.tmp / "changed-ledger")
    run = executor(e, publisher)
    source_mission(e)
    assert asyncio.run(run.hand_off(e.action["action_key"])) is None
    assert publisher.executions == 0


def test_confirmed_not_started_rehandoff_is_refused_and_settled_without_another_effect(env):
    e = env
    source_mission(e)
    key = e.action["action_key"]
    handed, _ = e.service.begin_handoff(
        key,
        owner="before-crash",
        lease_seconds=1,
        connectors={"file_publish": e.publisher},
        deployment=DEPLOYMENT,
    )
    assert handed["state"] == "HANDED_OFF"
    # Crash after outbox commit, before calling execute: no connector ledger exists.
    e.now[0] += 2
    reopen(e)
    publisher = ObservedPublisher(e.roots[1], e.tmp / "changed-ledger")
    run = executor(e, publisher)
    [settled] = asyncio.run(run.reconcile())
    assert settled["state"] == "FAILED"
    assert settled["error"] == "not_started:source_publish_root_overlap"
    assert settled["handoffs"] == 1 and publisher.executions == 0 and publisher.lookups == 1
    assert e.service.ledger.reservation("action:" + key)["state"] == "SETTLED"


def test_completed_receipt_is_still_reconciled_even_when_new_publication_is_forbidden(env):
    e = env
    key = e.action["action_key"]
    handed, _ = e.service.begin_handoff(
        key,
        owner="before-crash",
        lease_seconds=1,
        connectors={"file_publish": e.publisher},
        deployment=DEPLOYMENT,
    )
    # Crash after the real external commit, before recording its outcome in SQLite.
    e.publisher.execute(
        handed["operation"],
        handed["target"],
        handed["params"],
        idempotency_key=handed["idempotency_key"],
    )
    source_mission(e)
    e.now[0] += 2
    reopen(e)
    run = ActionExecutor(
        e.service,
        {"file_publish": e.publisher},
        DEPLOYMENT,
        owner="reopened",
        source_storage_roots=(*e.roots, e.publisher.root),
    )
    [settled] = asyncio.run(run.reconcile())
    assert settled["state"] == "SUCCEEDED" and settled["handoffs"] == 1
    assert e.publisher.executions == 1 and e.publisher.lookups == 1


@pytest.mark.parametrize(
    "destination,overlap", [("parent", True), ("child", True), ("sibling", False)]
)
def test_shared_path_helper_resolves_parent_symlinks_and_keeps_siblings_distinct(
    tmp_path, destination, overlap
):
    protected = tmp_path / "storage" / "sources"
    protected.mkdir(parents=True)
    sibling = tmp_path / "storage" / "sources-published"
    sibling.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(protected.parent, target_is_directory=True)
    target = {
        "parent": link,
        "child": link / "sources" / "nested",
        "sibling": link / "sources-published",
    }[destination]
    assert publication_overlaps_storage(target, (protected,)) is overlap


def test_explicit_custom_cas_and_workspace_roots_are_protected(env):
    e = env
    e.cas = ArtifactStore(e.tmp / "custom-cas")
    e.service = CommitService(e.store, artifact_store=e.cas)
    e.roots = (e.cas.root, e.tmp / "custom-workspaces")
    source_mission(e)
    publisher = ObservedPublisher(e.cas.root, e.tmp / "changed-ledger")
    assert asyncio.run(executor(e, publisher).hand_off(e.action["action_key"])) is None
    assert publisher.executions == 0
