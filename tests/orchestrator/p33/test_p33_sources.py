# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P3.3 B · D2/D7/D9 来源命令的决定性 oracle（先于实现写入）。

1. 真实 Facade/CommitService 登记 UTF-8 原文到 CAS；SQLite/事件/receipt 只存 hash
   与元数据。重复同命令返回原 receipt；不同正文冲突，不能借登记绕过更替审批。
2. 身份来自 Facade 构造；跨租户、未知 Mission 与来源根外路径均不泄露对象。
3. 更替/撤销只创建既有 source_change 审批；ApprovalApi 决定与源版本切换同事务。
   拒绝、过期、CAS 损坏、并发旧 head、ABA 及注入故障均不能留下假批准或半次切换。
4. 已消费的 grant 不再过期；旧版本仍可读，回到历史 hash 时仍只有一个 active。
5. 来源事件可独立折叠，与快照完全相等，unknown 为空；旧 schema 无 sources 可读。
   模型四种入口不获得来源命令，来源撤销不改变已有冲突。
6. 人可以拒绝 head 已变、CAS 丢失或已过期的来源审批；只记拒绝，不修改来源，
   不允许随后 grant。只有 grant 依赖来源仍可应用，不能把失效请求锁死在待办中。
7. 活动来源不能有 casefold 别名或双向父子文件冲突；新登记、历史版本重新激活
   的请求及 grant 事务均检查。拒绝冲突不能写 receipt/决定/来源，合法兄弟路径不受限。
8. 路径每个共享组件按 NFC + casefold 检查别名，包含不同叶文件的共同父目录；
   只允许一种原始拼写，不改写登记 key。同一种父目录拼写下的兄弟文件仍可登记。
9. reject 放宽的是当前可应用性，不放宽审批完整性：binding 被篡改或 receipt
   被换接时，grant/reject 都必须拒绝，不能留下任何 decision 或来源修改。

这里使用确定时钟和真实数据库/CAS/审批路径，不启动 provider 或 pytest 子进程。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.artifacts.store import ArtifactStore
from agent_orchestrator.contracts import ContractError, TaskStatus
from agent_orchestrator.governance.domains import CODE_DOMAIN, DOC_DOMAIN, DOC_PROFILE
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.observability.replay import (
    OPTIONAL_FIELDS,
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
)
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.storage import schema
from agent_orchestrator.storage.store import Store

PATH = "sources/report.md"
TEXT = "来源原文：我们不建议删除前提。\n"


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def env(tmp_path):
    now = [1_000.0]
    store = Store.open(tmp_path / "orchestrator.db", clock=lambda: now[0])
    cas = ArtifactStore(tmp_path / "cas")
    commit = CommitService(
        store, artifact_store=cas, deployed_layers=frozenset(DOC_PROFILE.runs_layers)
    )
    deployment = DeploymentPolicy(approval_ttl_seconds=10)
    host = SimpleNamespace(
        store=store, commit=commit, config=SimpleNamespace(deployment_policy=deployment)
    )
    host.validate_source_storage = lambda mission_id: None
    mission, _ = commit.create_mission(
        MissionSpec(
            goal="核对来源",
            success_criteria=("file:REPORT.md",),
            tenant_id="one",
            idempotency_key="source-mission",
            domain=DOC_DOMAIN,
        )
    )
    api = MissionControlV1(host, tenant_id="one", principal=Principal("person-one"))
    try:
        yield SimpleNamespace(
            store=store,
            cas=cas,
            commit=commit,
            api=api,
            host=host,
            mission=mission,
            now=now,
            tmp_path=tmp_path,
        )
    finally:
        store.close()


def _register(e, **overrides):
    return e.api.register_source(
        {
            "mission_id": e.mission.id,
            "path": PATH,
            "content": TEXT,
            "kind": "markdown",
            "idempotency_key": "register",
            **overrides,
        }
    )


def _supersede(e, *, old=TEXT, new="新版原文。\n", key="replace"):
    return e.api.supersede_source(
        {
            "mission_id": e.mission.id,
            "path": PATH,
            "content": new,
            "kind": "markdown",
            "expected_version_hash": _hash(old),
            "idempotency_key": key,
        }
    )


def _revoke(e, *, old=TEXT, key="revoke"):
    return e.api.revoke_source(
        {
            "mission_id": e.mission.id,
            "path": PATH,
            "expected_version_hash": _hash(old),
            "reason": "来源已撤回",
            "idempotency_key": key,
        }
    )


def _approve(e, pending, nonce="approve"):
    return e.api.decide(pending["request_id"], "approve", nonce=nonce)


def _assert_replay(e):
    projection = Projection().feed(events_from_store(e.store, e.mission.id))
    assert dict(projection.unknown) == {}
    report = compare(projection.objects, formal_from_snapshot(e.store.snapshot(e.mission.id)))
    assert report["coverage"] == 1.0 and report["mismatches"] == [], report


def test_register_cas_metadata_idempotency_and_historical_read(env):
    e = env
    receipt = _register(e)
    source = e.store.get_source(e.mission.id, PATH)
    assert source["version_hash"] == _hash(TEXT)
    assert source["trust"] == "untrusted_external" and source["revoked"] is False
    assert e.cas.read(source["version_hash"]) == TEXT.encode("utf-8")
    before = e.store.snapshot(e.mission.id)
    assert _register(e) == receipt
    assert e.store.snapshot(e.mission.id) == before
    assert e.store.get_source(e.mission.id, PATH, _hash(TEXT)) == source
    assert e.store.list_sources(e.mission.id, active_only=True) == [source]
    assert TEXT not in json.dumps(before, ensure_ascii=False)
    with sqlite3.connect(e.store.path) as connection:
        assert TEXT not in "\n".join(connection.iterdump())
    with pytest.raises(FacadeError) as error:
        _register(e, content="different")
    assert error.value.code == "conflict"
    with pytest.raises(FacadeError):
        _register(e, content="different", idempotency_key="bypass-approval")
    assert e.store.snapshot(e.mission.id) == before
    _assert_replay(e)


@pytest.mark.parametrize("field", ["tenant_id", "principal", "principal_id", "trust", "actor_id"])
def test_source_commands_refuse_caller_supplied_authority(env, field):
    before = env.store.snapshot(env.mission.id)
    with pytest.raises(FacadeError) as error:
        _register(env, **{field: "trusted"})
    assert error.value.code == "invalid_request"
    assert env.store.snapshot(env.mission.id) == before


@pytest.mark.parametrize(
    "path",
    [
        "../sources/a.md",
        "/sources/a.md",
        "sources/../a.md",
        "sources//a.md",
        "./sources/a.md",
        "sources\\a.md",
        "notes/a.md",
    ],
)
def test_source_paths_are_canonical_and_inside_frozen_domain_roots(env, path):
    with pytest.raises(FacadeError):
        _register(env, path=path)
    assert env.store.list_sources(env.mission.id) == []


def test_two_tenants_cannot_register_or_decide_each_others_sources(env):
    e = env
    other = MissionControlV1(e.host, tenant_id="two", principal=Principal("person-two"))
    command = {
        "mission_id": e.mission.id,
        "path": PATH,
        "content": TEXT,
        "kind": "markdown",
        "idempotency_key": "other",
    }
    errors = []
    for api, mid, path in [
        (other, e.mission.id, PATH),
        (e.api, "missing", PATH),
        (e.api, e.mission.id, "outside/a.md"),
    ]:
        with pytest.raises(FacadeError) as error:
            api.register_source({**command, "mission_id": mid, "path": path})
        errors.append((error.value.code, str(error.value)))
    assert errors[0] == errors[1] == errors[2]
    _register(e)
    pending = _supersede(e)
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(FacadeError) as error:
        other.decide(pending["request_id"], "approve", nonce="foreign")
    assert error.value.code == "not_found"
    assert e.store.snapshot(e.mission.id) == before


def test_supersede_uses_existing_approval_and_atomically_keeps_both_versions(env):
    e = env
    _register(e)
    pending = _supersede(e)
    assert _supersede(e) == pending
    request = e.store.get_approval(pending["request_id"])
    assert (request["kind"], request["level"], request["required_count"], request["state"]) == (
        "source_change",
        "L2",
        1,
        "PENDING",
    )
    assert e.store.get_source(e.mission.id, PATH)["version_hash"] == _hash(TEXT)
    assert e.store.get_source(e.mission.id, PATH, _hash("新版原文。\n")) is None
    assert e.store.list_actions(e.mission.id) == []
    result = _approve(e, pending)
    assert result["request_state"] == "GRANTED"
    active = e.store.get_source(e.mission.id, PATH)
    historical = e.store.get_source(e.mission.id, PATH, _hash(TEXT))
    assert active["version_hash"] == _hash("新版原文。\n")
    assert historical["superseded_by"] == active["version_hash"]
    assert historical["revoked"] is False
    assert len(e.store.list_sources(e.mission.id)) == 2
    assert e.store.list_sources(e.mission.id, True) == [active]
    before = e.store.snapshot(e.mission.id)
    e.now[0] += 100
    e.commit.expire_approvals(e.mission.id)
    assert _approve(e, pending) == result
    assert e.store.snapshot(e.mission.id) == before
    assert _supersede(e) == pending  # original command receipt, even after consumption
    _assert_replay(e)


@pytest.mark.parametrize("operation", ["supersede", "revoke"])
@pytest.mark.parametrize("decision", ["reject", "expire"])
def test_reject_or_expire_never_changes_a_source(env, operation, decision):
    e = env
    _register(e)
    pending = _supersede(e) if operation == "supersede" else _revoke(e)
    before = e.store.list_sources(e.mission.id)
    if decision == "reject":
        result = e.api.decide(pending["request_id"], "reject", reason="不允许", nonce="reject")
        assert result["request_state"] == "REJECTED"
    else:
        e.now[0] += 11
        with pytest.raises(FacadeError):
            _approve(e, pending)
        assert e.store.get_approval(pending["request_id"])["state"] == "EXPIRED"
        assert e.store.list_decisions(pending["request_id"]) == []
    assert e.store.list_sources(e.mission.id) == before
    _assert_replay(e)


def test_parallel_approvals_recheck_old_head_before_writing_a_decision(env):
    e = env
    _register(e)
    winner = _supersede(e)
    loser = _revoke(e)
    _approve(e, winner)
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(FacadeError):
        _approve(e, loser)
    assert e.store.list_decisions(loser["request_id"]) == []
    assert e.store.snapshot(e.mission.id) == before


@pytest.mark.parametrize(
    "operation,invalidated_by",
    [
        (operation, invalidated_by)
        for operation in ("supersede", "revoke")
        for invalidated_by in ("head_changed", "missing_old", "expired", "already_expired")
    ]
    + [("supersede", "missing_new")],
)
def test_invalidated_source_approval_can_be_rejected_without_touching_sources(
    env, operation, invalidated_by
):
    e = env
    _register(e)
    pending = _supersede(e) if operation == "supersede" else _revoke(e)
    if invalidated_by == "head_changed":
        _approve(e, _supersede(e, new="另一个版本。", key="winner"))
    elif invalidated_by.startswith("missing_"):
        text = TEXT if invalidated_by == "missing_old" else "新版原文。\n"
        e.cas.path_for(_hash(text)).unlink()
    else:
        e.now[0] += 11
        if invalidated_by == "already_expired":
            e.commit.expire_approvals(e.mission.id)
            assert e.store.get_approval(pending["request_id"])["state"] == "EXPIRED"
    before = e.store.list_sources(e.mission.id)
    source_events = [
        event
        for event in events_from_store(e.store, e.mission.id)
        if event["type"].startswith("Source")
    ]
    result = e.api.decide(pending["request_id"], "reject", reason="已失效", nonce="dismiss")
    assert result["request_state"] == "REJECTED"
    assert e.store.list_sources(e.mission.id) == before
    assert [
        event
        for event in events_from_store(e.store, e.mission.id)
        if event["type"].startswith("Source")
    ] == source_events
    assert [d["decision"] for d in e.store.list_decisions(pending["request_id"])] == ["reject"]
    after = e.store.snapshot(e.mission.id)
    assert e.api.decide(pending["request_id"], "reject", reason="已失效", nonce="dismiss") == result
    with pytest.raises(FacadeError):
        _approve(e, pending)
    assert e.store.snapshot(e.mission.id) == after
    _assert_replay(e)


@pytest.mark.parametrize(
    "existing,candidate",
    [
        (PATH, "sources/REPORT.md"),
        ("sources/straße.md", "sources/STRASSE.md"),
        (PATH, PATH + "/child.md"),
        (PATH + "/child.md", PATH),
        (PATH, "sources/REPORT.md/child.md"),
        ("sources/REPORT.md/child.md", PATH),
        ("sources/caf\u00e9.md", "sources/cafe\u0301.md"),
        ("sources/cafe\u0301.md", "sources/caf\u00e9.md"),
        ("sources/Dir/a.md", "sources/dir/b.md"),
        ("sources/caf\u00e9/a.md", "sources/cafe\u0301/b.md"),
        ("sources/cafe\u0301/a.md", "sources/caf\u00e9/b.md"),
    ],
)
def test_register_refuses_active_aliases_and_file_ancestor_collisions(env, existing, candidate):
    e = env
    _register(e, path=existing)
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(FacadeError) as error:
        _register(e, path=candidate, idempotency_key="collision")
    assert error.value.code == "conflict"
    assert e.store.snapshot(e.mission.id) == before
    # Same-prefix siblings are distinct files; a refused key wrote no receipt.
    _register(e, path="sources/report.md-extra", idempotency_key="collision")
    assert len(e.store.list_sources(e.mission.id, active_only=True)) == 2
    _assert_replay(e)


@pytest.mark.parametrize("parent", ["sources/Dir", "sources/caf\u00e9", "sources/cafe\u0301"])
def test_source_siblings_preserve_the_same_original_parent_spelling(env, parent):
    e = env
    first = _register(e, path=parent + "/a.md")
    second = _register(e, path=parent + "/b.md", idempotency_key="sibling")
    assert [s["path"] for s in e.store.list_sources(e.mission.id, True)] == [
        parent + "/a.md",
        parent + "/b.md",
    ]
    before = e.store.snapshot(e.mission.id)
    assert _register(e, path=parent + "/a.md") == first
    assert _register(e, path=parent + "/b.md", idempotency_key="sibling") == second
    assert e.store.snapshot(e.mission.id) == before
    _assert_replay(e)


@pytest.mark.parametrize("timing", ["before_request", "before_grant"])
@pytest.mark.parametrize(
    "original,conflicting",
    [
        (PATH, "sources/REPORT.md"),
        (PATH, "sources/REPORT.md/child.md"),
        (PATH + "/child.md", "sources/REPORT.md"),
        ("sources/caf\u00e9.md", "sources/cafe\u0301.md"),
        ("sources/Dir/a.md", "sources/dir/b.md"),
        ("sources/cafe\u0301/a.md", "sources/caf\u00e9/b.md"),
    ],
)
def test_historical_reactivation_rechecks_active_path_collisions(
    env, timing, original, conflicting
):
    e = env
    _register(e, path=original)
    revoke = e.api.revoke_source(
        {
            "mission_id": e.mission.id,
            "path": original,
            "expected_version_hash": _hash(TEXT),
            "reason": "撤回",
            "idempotency_key": "revoke-original",
        }
    )
    _approve(e, revoke)
    command = {
        "mission_id": e.mission.id,
        "path": original,
        "expected_version_hash": _hash(TEXT),
        "content": TEXT,
        "kind": "markdown",
        "idempotency_key": "reactivate",
    }
    pending = e.api.supersede_source(command) if timing == "before_grant" else None
    _register(e, path=conflicting, idempotency_key="conflicting")
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(FacadeError, match="source path conflicts"):
        if pending is None:
            e.api.supersede_source(command)
        else:
            _approve(e, pending)
    assert e.store.snapshot(e.mission.id) == before
    assert e.store.get_source(e.mission.id, original, _hash(TEXT))["revoked"] is True
    assert [s["path"] for s in e.store.list_sources(e.mission.id, True)] == [conflicting]
    if pending is not None:
        assert e.store.list_decisions(pending["request_id"]) == []
        assert (
            e.api.decide(pending["request_id"], "reject", reason="路径已占用", nonce="dismiss")[
                "request_state"
            ]
            == "REJECTED"
        )
    _assert_replay(e)


def test_reenter_historical_hash_has_one_active_and_rejects_aba_approval(env):
    e = env
    _register(e)
    stale = _revoke(e)
    _approve(e, _supersede(e))
    _approve(e, _supersede(e, old="新版原文。\n", new=TEXT, key="back"))
    assert len(e.store.list_sources(e.mission.id)) == 2
    assert [s["version_hash"] for s in e.store.list_sources(e.mission.id, True)] == [_hash(TEXT)]
    with pytest.raises(FacadeError):
        _approve(e, stale)
    assert e.store.list_decisions(stale["request_id"]) == []
    _assert_replay(e)


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("damage", ["binding", "receipt_link"])
def test_tampered_source_approval_cannot_record_grant_or_reject(env, decision, damage):
    e = env
    _register(e)
    pending = _supersede(e)
    request = e.store.get_approval(pending["request_id"])
    if damage == "binding":
        request["binding"]["path"] = "sources/forged.md"
    else:
        other = _revoke(e)
        request["subject_key"] = other["command_id"]
    e.store.put_approval(request)
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(FacadeError, match="source approval binding changed"):
        e.api.decide(pending["request_id"], decision, reason="不允许", nonce="decision")
    assert e.store.list_decisions(pending["request_id"]) == []
    assert e.store.snapshot(e.mission.id) == before


@pytest.mark.parametrize("which", ["old", "new"])
def test_approval_rehashes_cas_and_refuses_tampered_bytes_without_decision(env, which):
    e = env
    _register(e)
    pending = _supersede(e)
    address = _hash(TEXT if which == "old" else "新版原文。\n")
    path = e.cas.path_for(address)
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    before = e.store.snapshot(e.mission.id)
    with pytest.raises(FacadeError):
        _approve(e, pending)
    assert e.store.list_decisions(pending["request_id"]) == []
    assert e.store.snapshot(e.mission.id) == before


def test_approval_event_failure_rolls_back_source_decision_and_grant(env, monkeypatch):
    e = env
    _register(e)
    pending = _supersede(e)
    before = e.store.snapshot(e.mission.id)
    append = e.store.append_event

    def fail(event):
        if event.type == "SourceSuperseded":
            raise RuntimeError("injected source event failure")
        return append(event)

    with monkeypatch.context() as patch:
        patch.setattr(e.store, "append_event", fail)
        with pytest.raises(RuntimeError, match="injected source event"):
            _approve(e, pending)
    assert e.store.snapshot(e.mission.id) == before
    assert e.store.list_decisions(pending["request_id"]) == []
    _approve(e, pending)
    _assert_replay(e)


def test_revoke_keeps_history_and_requires_approval_to_reactivate(env):
    e = env
    _register(e)
    _approve(e, _revoke(e))
    assert e.store.get_source(e.mission.id, PATH) is None
    historical = e.store.get_source(e.mission.id, PATH, _hash(TEXT))
    assert historical["revoked"] is True
    with pytest.raises(FacadeError):
        _register(e, idempotency_key="cannot-reactivate")
    _approve(e, _supersede(e, new=TEXT, key="reactivate"))
    assert e.store.get_source(e.mission.id, PATH)["revoked"] is False
    assert len(e.store.list_sources(e.mission.id)) == 1
    _assert_replay(e)


def test_code_missions_and_commit_without_cas_keep_old_behavior(env):
    e = env
    mission, _ = CommitService(e.store).create_mission(
        MissionSpec(
            goal="code",
            success_criteria=("pytest:tests",),
            tenant_id="one",
            idempotency_key="code",
            domain=CODE_DOMAIN,
        )
    )
    before = e.store.snapshot(mission.id)
    with pytest.raises(FacadeError):
        _register(e, mission_id=mission.id)
    assert e.store.snapshot(mission.id) == before


def test_schema8_readonly_snapshot_has_no_sources_and_needs_no_optional_fields(
    tmp_path, monkeypatch
):
    path = tmp_path / "legacy.db"
    with monkeypatch.context() as patch:
        patch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:8])
        legacy = Store.open(path)
        mission, _ = CommitService(legacy).create_mission(
            MissionSpec(
                goal="legacy",
                success_criteria=("file:x",),
                tenant_id="one",
                idempotency_key="legacy",
            )
        )
        legacy.close()
    store = Store.open_readonly(path)
    try:
        assert store.get_source("missing", PATH) is None
        assert store.list_sources("missing") == []
        assert not any(kind == "source" for kind, _ in OPTIONAL_FIELDS)
        snapshot = store.snapshot(mission.id)
        assert snapshot["sources"] == []
        assert formal_from_snapshot(snapshot)["source"] == {}
    finally:
        store.close()
    upgraded = Store.open(path)
    try:
        assert upgraded.has_table("sources")
        assert upgraded.snapshot(mission.id) == snapshot
    finally:
        upgraded.close()


def test_reopened_store_returns_original_receipts_and_exact_history(env):
    e = env
    registered = _register(e)
    pending = _supersede(e)
    other = Store.open(e.store.path, clock=lambda: e.now[0])
    try:
        host = SimpleNamespace(
            store=other,
            commit=CommitService(other, artifact_store=e.cas),
            config=e.host.config,
            validate_source_storage=lambda mid: None,
        )
        api = MissionControlV1(host, tenant_id="one", principal=Principal("person-one"))
        api.decide(pending["request_id"], "approve", nonce="after-reopen")
        e.api = api
        assert _register(e) == registered
        assert _supersede(e) == pending
        assert other.get_source(e.mission.id, PATH, _hash(TEXT))["superseded_by"] == _hash(
            "新版原文。\n"
        )
        assert other.get_source(e.mission.id, PATH)["version_hash"] == _hash("新版原文。\n")
    finally:
        other.close()
    _assert_replay(e)


def test_facade_rechecks_physical_storage_on_new_commands_and_pending_approval(env):
    e = env
    _register(e)
    pending = _supersede(e)
    before = e.store.snapshot(e.mission.id)
    seen = []

    def reject(mid):
        seen.append(mid)
        raise ContractError("publish overlaps source storage after deployment change")

    e.host.validate_source_storage = reject
    with pytest.raises(FacadeError, match="publish overlaps"):
        _register(e, path="sources/another.md", idempotency_key="another")
    with pytest.raises(FacadeError, match="publish overlaps"):
        _approve(e, pending)
    assert seen == [e.mission.id, e.mission.id]
    assert e.store.snapshot(e.mission.id) == before
    assert e.store.list_decisions(pending["request_id"]) == []


def test_revocation_does_not_close_a_real_conflict_task_or_rewrite_claims(env):
    from test_p33_remaining_domain_gates import (
        DOC_PASSES,
        _document_result,
        _graph,
        _node,
        _planning,
    )

    e = env
    e.mission = _planning(e.commit, conflict_reserve_tokens=20_000)
    e.api = MissionControlV1(
        e.host, tenant_id=e.mission.tenant_id, principal=Principal("person-one")
    )
    tasks, _ = _graph(e.commit, e.mission, _node("A"), _node("B"))
    for task, stance in zip(tasks, ("affirms", "refutes"), strict=True):
        result = _document_result(e.commit, task, stance=stance)
        e.commit.accept_result(result.envelope.id, verifier_results=DOC_PASSES)
    [conflict] = e.store.list_conflicts(e.mission.id)
    assert conflict["state"] == "OPEN"
    conflict_task = e.store.get_task(conflict["task_id"])
    assert conflict_task.kind == "conflict" and conflict_task.status is TaskStatus.READY
    claims = e.store.list_mission_claims(e.mission.id)
    knowledge = e.store.list_knowledge(e.mission.id)
    _register(e)
    _approve(e, _revoke(e))
    assert e.store.list_conflicts(e.mission.id) == [conflict]
    assert e.store.get_task(conflict_task.id) == conflict_task
    assert e.store.list_mission_claims(e.mission.id) == claims
    assert e.store.list_knowledge(e.mission.id) == knowledge
    _assert_replay(e)


def test_memory_store_requires_explicit_cas_instead_of_writing_cwd(tmp_path, monkeypatch):
    from agent_orchestrator.orchestrator.source_commits import SourceCommitError

    monkeypatch.chdir(tmp_path)
    store = Store.open(":memory:")
    try:
        commit = CommitService(store)
        mission, _ = commit.create_mission(
            MissionSpec(
                goal="memory",
                success_criteria=("file:x",),
                tenant_id="one",
                idempotency_key="memory",
                domain=DOC_DOMAIN,
            )
        )
        with pytest.raises(SourceCommitError, match="explicit CAS"):
            commit.register_source(
                mission_id=mission.id,
                tenant_id="one",
                principal=Principal("p"),
                path=PATH,
                content=TEXT,
                kind="markdown",
                idempotency_key="register",
            )
        assert not (tmp_path / "artifacts").exists()
        assert store.list_sources(mission.id) == []
    finally:
        store.close()


@pytest.mark.parametrize("entry", ["task_proposal", "graph_change", "tool", "connector_callback"])
def test_model_entries_cannot_execute_a_source_command_or_consume_its_approval(env, entry):
    from agent_orchestrator.artifacts.workspace import WorkspaceManager
    from agent_orchestrator.graph.changes import TaskGraphChange
    from agent_orchestrator.orchestrator.action_commits import ActionCommitError
    from agent_orchestrator.orchestrator.commit_service import TaskProposal
    from agent_orchestrator.runtime.connectors import Receipt
    from agent_orchestrator.runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway
    from simple_harness.contracts import CallId
    from simple_harness.tools import ToolCall

    e = env
    _register(e)
    pending = _revoke(e)
    before = e.store.snapshot(e.mission.id)
    command = {
        "mission_id": e.mission.id,
        "path": PATH,
        "content": "model-written",
        "kind": "markdown",
        "idempotency_key": "model",
    }
    for operation in ("register_source", "supersede_source", "revoke_source"):
        if entry == "task_proposal":
            with pytest.raises(ContractError, match="unknown fields"):
                TaskProposal.from_json(
                    {
                        "goal": "g",
                        "rationale": "r",
                        "success_criteria": [],
                        "verification_policy": [],
                        operation: command,
                    }
                )
        elif entry == "graph_change":
            with pytest.raises(ContractError, match="unknown operation"):
                TaskGraphChange.from_json(
                    {
                        "base_graph_version": 1,
                        "basis": {},
                        "rationale": "r",
                        "operations": [{"op": operation, **command}],
                    }
                )
        elif entry == "tool":
            gateway = WorkspaceToolGateway(WorkspaceManager(e.tmp_path / "tools"))
            # Even a forged allowed-tools binding cannot manufacture a registered tool.
            gateway.bind("agent", WorkspaceBinding("attempt", "work", True, (operation,)))
            outcome = asyncio.run(
                gateway.execute(ToolCall(CallId("call"), operation, command), {"run_id": "agent"})
            )
            assert outcome.error_code == "invalid_arguments"
        else:
            receipt = Receipt(
                idempotency_key=pending["command_id"],
                connector="sources",
                operation=operation,
                target=PATH,
                params_hash="f" * 64,
                applied=True,
            )
            with pytest.raises(ActionCommitError, match="unknown action"):
                e.commit.record_action_outcome(
                    pending["command_id"], owner="connector", outcome="succeeded", receipt=receipt
                )
    assert e.store.snapshot(e.mission.id) == before
    assert e.store.list_decisions(pending["request_id"]) == []
