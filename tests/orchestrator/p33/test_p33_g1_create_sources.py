# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""G1 oracle before implementation: a Host never publishes a partial source batch.

The real deployment door and Facade create the Mission. Faults after the first
source, during events, and at the batch receipt must roll back all SQLite state.
Only immutable unreferenced CAS blobs may survive. Concurrent commits and reordered
retries return the original receipt; an ordinary create cannot be adopted later.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy

import pytest

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.api.missions import spec_from_request
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
)
from agent_orchestrator.orchestrator.commit_service import CommitService
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

PERSON = Principal("g1-host")


def command():
    return {
        "mission": {
            "goal": "比较原始资料",
            "success_criteria": ["file:REPORT.md"],
            "domain": DOC_DOMAIN,
            "idempotency_key": "g1-batch",
            "budget": {"max_tokens": 30000, "max_attempts": 4},
        },
        "sources": [
            {"path": "sources/a.md", "content": "甲材料。\r\n", "kind": "markdown"},
            {"path": "sources/b.md", "content": "乙材料。\n", "kind": "markdown"},
        ],
    }


def run(tmp_path, body, *, publish=False):
    async def case():
        root = tmp_path / "evidence"
        deployment = DeploymentPolicy(enabled_connectors=("file_publish",)) if publish else None
        kwargs = {} if deployment is None else {"deployment_policy": deployment}
        config = OrchestratorConfig(evidence_root=root, **kwargs)
        connectors = (
            {"file_publish": FilePublishConnector(root / "artifacts", tmp_path / "publish-ledger")}
            if publish
            else {}
        )
        async with Orchestrator(config, RoleScriptedProvider({}), connectors=connectors) as orch:
            api = MissionControlV1(orch, tenant_id="tenant", principal=PERSON)
            return body(orch, api)

    return asyncio.run(case())


def database_state(store):
    with store.read_view() as connection:
        return tuple(connection.iterdump())


def test_batch_reordering_reopen_returns_original_receipt_and_replays(tmp_path):
    def body(orch, api):
        value = command()
        receipt = api.create_with_sources(value)
        mid = receipt["mission_id"]
        assert receipt["created"] is True and receipt["status"] == "CREATED"
        assert receipt["spec_hash"] == orch.store.find_mission("tenant", "g1-batch")[1]
        assert set(receipt["source_versions"]) == {s["path"] for s in value["sources"]}
        assert len(receipt["sources"]) == 2 and len(receipt["batch_hash"]) == 64
        for item in value["sources"]:
            row = orch.store.get_source(mid, item["path"])
            assert row["trust"] == "untrusted_external"
            assert (
                orch.assembled.workspaces.artifact_store.read(row["version_hash"])
                == item["content"].encode()
            )
        before = database_state(orch.store)
        assert api.create_with_sources({**value, "sources": value["sources"][::-1]}) == receipt
        assert database_state(orch.store) == before
        # A fresh connection exercises persisted receipt identity, not facade memory.
        with closing(Store.open(orch.store.path)) as reopened:
            service = CommitService(
                reopened, artifact_store=orch.assembled.workspaces.artifact_store
            )
            spec = spec_from_request(
                "tenant",
                value["mission"],
                default_tools=orch.config.deployment_policy.allowed_tools,
            )
            assert service.create_mission_with_sources(
                spec, sources=value["sources"], principal=PERSON
            ) == {k: v for k, v in receipt.items() if k != "facade"}
        projection = Projection().feed(events_from_store(orch.store, mid))
        assert dict(projection.unknown) == {}
        assert (
            compare(projection.objects, formal_from_snapshot(orch.store.snapshot(mid)))[
                "mismatches"
            ]
            == []
        )

    run(tmp_path, body)


@pytest.mark.parametrize("damage", ["content", "kind", "goal"])
def test_batch_changed_body_conflicts_without_mutation(tmp_path, damage):
    def body(orch, api):
        value = command()
        api.create_with_sources(value)
        changed = deepcopy(value)
        if damage == "goal":
            changed["mission"]["goal"] += "不同目标"
        else:
            changed["sources"][0][damage] += "不同"
        before = database_state(orch.store)
        with pytest.raises(FacadeError) as raised:
            api.create_with_sources(changed)
        assert raised.value.code == "conflict"
        assert database_state(orch.store) == before

    run(tmp_path, body)


def test_ordinary_create_cannot_be_adopted_as_an_atomic_batch(tmp_path):
    def body(orch, api):
        api.create(command()["mission"])
        before = database_state(orch.store)
        with pytest.raises(FacadeError) as raised:
            api.create_with_sources(command())
        assert raised.value.code == "conflict"
        assert database_state(orch.store) == before

    run(tmp_path, body)


@pytest.mark.parametrize("point", ["second_cas", "source_event", "batch_receipt"])
def test_batch_fault_rolls_back_mission_budget_binding_sources_receipts_and_events(
    tmp_path, monkeypatch, point
):
    def body(orch, api):
        before = database_state(orch.store)
        cas = orch.assembled.workspaces.artifact_store
        if point == "second_cas":
            original = cas.put_bytes
            calls = []

            def fail(data):
                calls.append(data)
                if len(calls) == 2:
                    raise OSError("second source cannot persist")
                return original(data)

            monkeypatch.setattr(cas, "put_bytes", fail)
            # Commit may hold a distinct ArtifactStore object for the same real CAS root.
            monkeypatch.setattr(orch.commit._source_cas(), "put_bytes", fail)
        elif point == "source_event":
            original = orch.store.append_event

            def fail(event):
                if event.type == "SourceRegistered" and event.payload["sources"][0][
                    "path"
                ].endswith("b.md"):
                    raise RuntimeError("source event fault")
                return original(event)

            monkeypatch.setattr(orch.store, "append_event", fail)
        else:
            original = orch.store.insert_receipt

            def fail(**kwargs):
                if kwargs["kind"] == "mission_source_batch":
                    raise RuntimeError("batch receipt fault")
                return original(**kwargs)

            monkeypatch.setattr(orch.store, "insert_receipt", fail)
        with pytest.raises((FacadeError, RuntimeError)):
            api.create_with_sources(command())
        assert database_state(orch.store) == before
        with closing(Store.open(orch.store.path)) as reopened:
            assert database_state(reopened) == before

    run(tmp_path, body)


@pytest.mark.parametrize(
    "other",
    [
        "sources/A.md",
        "sources/a.md/child",
        "sources/a.md",
        "sources/Dir/b.md",
        "sources/e\u0301/b.md",
    ],
)
def test_batch_active_aliases_and_file_ancestors_refuse_atomically(tmp_path, other):
    def body(orch, api):
        value = command()
        if "Dir" in other:
            value["sources"][0]["path"] = "sources/dir/a.md"
        if "e\u0301" in other:
            value["sources"][0]["path"] = "sources/é/a.md"
        value["sources"][1]["path"] = other
        before = database_state(orch.store)
        with pytest.raises(FacadeError):
            api.create_with_sources(value)
        assert database_state(orch.store) == before

    run(tmp_path, body)


@pytest.mark.parametrize("field", ["principal", "tenant_id", "trust", "origin", "idempotency_key"])
def test_batch_source_items_reject_caller_authority_fields(tmp_path, field):
    def body(orch, api):
        value = command()
        value["sources"][0][field] = "caller"
        before = database_state(orch.store)
        with pytest.raises(FacadeError) as raised:
            api.create_with_sources(value)
        assert raised.value.code == "invalid_request"
        assert database_state(orch.store) == before

    run(tmp_path, body)


def test_batch_runs_real_source_publish_storage_guard(tmp_path):
    def body(orch, api):
        before = database_state(orch.store)
        with pytest.raises(FacadeError):
            api.create_with_sources(command())
        assert database_state(orch.store) == before

    run(tmp_path, body, publish=True)


def test_two_connections_commit_one_source_batch(tmp_path):
    path = tmp_path / "orchestrator.db"
    with closing(Store.open(path)):
        pass
    spec = spec_from_request("tenant", command()["mission"])

    def submit():
        with closing(Store.open(path)) as store:
            commit = CommitService(store, artifact_store=ArtifactStore(tmp_path / "artifacts"))
            return commit.create_mission_with_sources(
                spec, sources=command()["sources"], principal=PERSON
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: submit(), range(2)))
    assert receipts[0] == receipts[1]
    with closing(Store.open(path)) as store:
        assert len(store.list_missions()) == 1
        assert len(store.list_sources(receipts[0]["mission_id"])) == 2


def test_same_batch_key_is_tenant_scoped(tmp_path):
    def body(orch, api):
        value = command()
        original = api.create_with_sources(value)
        foreign = MissionControlV1(orch, tenant_id="foreign", principal=Principal("other-host"))
        other = foreign.create_with_sources(value)
        assert original["mission_id"] != other["mission_id"]
        assert original["command_id"] != other["command_id"]
        assert api.create_with_sources(value) == original
        assert foreign.create_with_sources(value) == other
        for receipt, tenant in ((original, "tenant"), (other, "foreign")):
            rows = orch.store.list_sources(receipt["mission_id"])
            assert len(rows) == 2 and {row["tenant_id"] for row in rows} == {tenant}

    run(tmp_path, body)
