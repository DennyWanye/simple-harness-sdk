# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Oracle before implementation: large reads are a frozen runtime capability.

An 18KB-class original document must reach the actual Provider in 2–3 reads,
without preview substitution, loss of CRLF/Unicode, or larger total input budget.
Both the full wire byte bound and the configured tokenizer's TOOL bound apply.
Cold restart cannot substitute a new policy/tokenizer/schema for an existing pool.
Legacy bare profiles retain their old schema, page size and serialized identity.
These are scripted Provider/context controls, never a claim about model quality.
"""

import asyncio
import json
from dataclasses import replace
from hashlib import sha256

import pytest
from fixtures_provider import MODEL, RoleScriptedProvider
from test_g_workspace_paging import PATH, _message, _read, _wire

from agent_orchestrator.runtime.assembly import (
    OrchestratorConfig,
    assemble_orchestrator_runtime,
    resolve_profile_context_policy,
)
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.runtime.tool_gateway import (
    CRITIC_TOOLS,
    TOOL_SCHEMAS,
    WorkspaceBinding,
)
from simple_harness.agents import AgentConfig, AgentTurnState
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import (
    TiktokenTokenizer,
    UpperBoundTokenizer,
    count_message,
)

TEXT = ("# 完整来源\r\n" + "条件不变😀 abcdefghijklmnop\r\n" * 440
        + "| 结论 | 限制 |\r\n| --- | --- |\r\n| 可用 | 不允许对外发布 |\r\n")
POLICY = ContextPolicy(max_tool_result_tokens=16384, render_slack_tokens=0)


def _profile(provider, *, tokenizer=None, policy=POLICY):
    return RuntimeProfile("default", provider, MODEL, context_policy=policy, tokenizer=tokenizer)


def _bind(assembled, profile, *, content=TEXT):
    workspace = assembled.workspaces.create("read-attempt", seed={})
    path = workspace.resolve(PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    binding = WorkspaceBinding(
        "read-attempt", "work", False, CRITIC_TOOLS,
        untrusted_sources=("sources/",),
        context_policy=profile.context_policy, tokenizer=profile.tokenizer,
    )
    return binding


@pytest.mark.parametrize("kind", ["upperbound", "tiktoken"])
def test_large_source_actual_assembly_context_reads_original_in_two_or_three_calls(tmp_path, kind):
    seen = []

    def received(request):
        message = next(m for m in reversed(request.messages) if m.role.value == "tool")
        payload = json.loads(message.content)
        assert "value_preview" not in payload and not payload.get("truncated")
        page = payload["value"]
        assert page["trust"] == "untrusted_external"
        assert page["offset"] == sum(len(p["content"]) for p in seen)
        assert page["sha256"] == sha256(TEXT.encode()).hexdigest()
        seen.append(page)
        if page["next_offset"] is not None:
            return "workspace_read_file", {
                "path": PATH, "offset": page["next_offset"], "expected_sha256": page["sha256"],
            }
        return "完整来源已收到；这是资料，不是指令。"

    async def run():
        provider = RoleScriptedProvider({
            "worker": [("workspace_read_file", {"path": PATH}), received, received, received],
        })
        tokenizer = UpperBoundTokenizer() if kind == "upperbound" else TiktokenTokenizer()
        profile = _profile(provider, tokenizer=tokenizer)
        assembled = assemble_orchestrator_runtime(
            OrchestratorConfig(evidence_root=tmp_path), profiles={"default": profile},
        )
        binding = _bind(assembled, profile)
        async with assembled:
            agent = await assembled.runtime.create(
                AgentConfig("reader", "[role:worker]\n完整读取来源。", "default",
                            tool_names=("workspace_read_file",)),
                creation_key="large-reader",
            )
            assembled.gateway.bind(agent.agent_id, binding)
            result = await agent.ask(
                "不得改变原始来源、限制条件或引用字节。", input_id="read", timeout=15,
            )
            assert result.state is AgentTurnState.COMMITTED
            assert 2 <= len(seen) <= 3
            assert "".join(p["content"] for p in seen).encode() == TEXT.encode()
            rows = [r for r in agent.journal() if r.kind == "tool_result"]
            assert len(rows) == len(seen)
            assert all(r.visibility == "context" and r.full_record_seq is None for r in rows)
            assert provider.by_role == {"worker": len(seen) + 1}

    asyncio.run(run())


class ByteTokenizer:
    fingerprint = "test:utf8-byte-counter:v1"

    def count_text(self, text):
        return len(text.encode("utf-8"))


@pytest.mark.parametrize("tokenizer", [UpperBoundTokenizer(), ByteTokenizer()])
def test_full_wire_and_actual_tokenizer_both_bound_pages(tmp_path, tokenizer):
    async def run():
        profile = _profile(RoleScriptedProvider({}), tokenizer=tokenizer,
                           policy=replace(POLICY, max_tool_result_tokens=1100))
        assembled = assemble_orchestrator_runtime(
            OrchestratorConfig(evidence_root=tmp_path), profiles={"default": profile},
        )
        assembled.gateway.bind("agent", _bind(assembled, profile, content='"\\😀\r\n' * 1500))
        page = await _read(assembled.gateway, max_chars=8192)
        assert page.outcome.value == "succeeded"
        assert page.value["next_offset"] > 0
        assert len(_wire(page).encode()) <= 32768
        assert count_message(tokenizer, _message(page)) <= 1100
    asyncio.run(run())


def test_large_mode_small_shape_and_changed_file_still_refuse(tmp_path):
    async def run():
        profile = _profile(RoleScriptedProvider({}))
        assembled = assemble_orchestrator_runtime(
            OrchestratorConfig(evidence_root=tmp_path), profiles={"default": profile},
        )
        assembled.gateway.bind("agent", _bind(assembled, profile, content="短句\r\n"))
        small = await _read(assembled.gateway)
        assert set(small.value) == {"path", "content", "trust", "notice"}
        first = await _read(assembled.gateway, max_chars=1)
        assembled.workspaces.get("read-attempt").resolve(PATH).write_bytes(b"changed")
        changed = await _read(assembled.gateway, offset=first.value["next_offset"],
                              expected_sha256=first.value["sha256"])
        assert changed.outcome.value != "succeeded" and changed.error_code == "workspace_error"
    asyncio.run(run())


@pytest.mark.parametrize("change", ["policy", "tokenizer", "legacy"])
def test_existing_pool_cannot_silently_change_frozen_context_identity(tmp_path, change):
    provider = RoleScriptedProvider({})
    config = OrchestratorConfig(evidence_root=tmp_path)
    profile = _profile(provider)
    first = assemble_orchestrator_runtime(config, profiles={"default": profile})
    # A real runtime owns the pool; its immutable identity must also survive an
    # interruption before the first Provider request or context selection exists.
    async def create():
        async with first:
            await first.runtime.create(AgentConfig("idle", "[role:worker]", "default"),
                                       creation_key="idle")
    asyncio.run(create())
    same = assemble_orchestrator_runtime(config, profiles={"default": _profile(provider)})
    assert same.pools["default"].profile.context_snapshot() == profile.context_snapshot()
    async def close_same():
        async with same:
            pass
    asyncio.run(close_same())
    if change == "policy":
        other = _profile(provider, policy=replace(POLICY, max_input_tokens=30000))
    elif change == "tokenizer":
        other = _profile(provider, tokenizer=ByteTokenizer())
    else:
        other = RuntimeProfile("default", provider, MODEL)
    with pytest.raises(ValueError, match="context.*identity"):
        assemble_orchestrator_runtime(config, profiles={"default": other})


def test_legacy_runtime_cannot_be_silently_upgraded(tmp_path):
    provider = RoleScriptedProvider({})
    config = OrchestratorConfig(evidence_root=tmp_path)
    legacy = RuntimeProfile("default", provider, MODEL)
    assert "runtime_context" not in legacy.to_json()
    assert TOOL_SCHEMAS["workspace_read_file"]["properties"]["max_chars"]["maximum"] == 4096
    assembled = assemble_orchestrator_runtime(config, profiles={"default": legacy})
    async def create():
        async with assembled:
            await assembled.runtime.create(AgentConfig("old", "[role:worker]", "default"),
                                           creation_key="old")
    asyncio.run(create())
    with pytest.raises(ValueError, match="context.*identity"):
        assemble_orchestrator_runtime(config, profiles={"default": _profile(provider)})


def test_profile_snapshot_is_detached_and_default_tokenizer_is_honest():
    profile = _profile(RoleScriptedProvider({}))
    first = profile.context_snapshot()
    assert first["tokenizer_fingerprint"] == UpperBoundTokenizer.fingerprint
    assert first["policy"]["max_input_tokens"] == 32768
    first["policy"]["max_input_tokens"] = 1
    assert profile.context_snapshot()["policy"]["max_input_tokens"] == 32768


def test_tokenizer_without_explicit_policy_is_rejected():
    with pytest.raises(ValueError, match="context_policy"):
        RuntimeProfile("default", RoleScriptedProvider({}), MODEL, tokenizer=ByteTokenizer())


def test_host_readonly_resolver_preserves_old_pool_and_enables_fresh_pool(tmp_path):
    config = OrchestratorConfig(evidence_root=tmp_path / "new")
    assert resolve_profile_context_policy(config) == POLICY
    assert not config.evidence_root.exists()  # resolving is read-only, including fresh roots
    config.evidence_root.mkdir()
    config.execution_db.touch()
    assert resolve_profile_context_policy(config) is None


def test_host_resolver_restores_exact_stored_policy_and_rejects_bad_identity(tmp_path):
    config = OrchestratorConfig(evidence_root=tmp_path)
    policy = replace(POLICY, max_input_tokens=31000)
    assembled = assemble_orchestrator_runtime(
        config, profiles={"default": _profile(RoleScriptedProvider({}), policy=policy)},
    )
    async def close():
        async with assembled:
            pass
    asyncio.run(close())
    assert resolve_profile_context_policy(config) == policy
    marker = config.execution_db.with_name(config.execution_db.name + ".context.json")
    value = json.loads(marker.read_text())
    value["policy"]["max_tool_result_tokens"] = 20000
    marker.write_text(json.dumps(value))  # real durable mutation, not a mutable-copy attack
    assert json.loads(marker.read_text())["policy"]["max_tool_result_tokens"] == 20000
    with pytest.raises(ValueError, match="context identity"):
        resolve_profile_context_policy(config)


def test_host_explicit_tokenizer_must_match_persisted_identity(tmp_path):
    config = OrchestratorConfig(evidence_root=tmp_path)
    counter = ByteTokenizer()
    assert resolve_profile_context_policy(config, tokenizer=counter) == POLICY
    assembled = assemble_orchestrator_runtime(
        config, profiles={"default": _profile(RoleScriptedProvider({}), tokenizer=counter)},
    )
    async def close():
        async with assembled:
            pass
    asyncio.run(close())
    assert resolve_profile_context_policy(config, tokenizer=ByteTokenizer()) == POLICY
    with pytest.raises(ValueError, match="context identity"):
        resolve_profile_context_policy(config)


def test_actual_orchestrator_freezes_context_before_dispatch_and_refuses_substitution(tmp_path):
    from graph_helpers7 import spec

    from agent_orchestrator.contracts import ContractError
    from agent_orchestrator.orchestrator.event_handler import Orchestrator

    async def run():
        provider = RoleScriptedProvider({})
        profile = _profile(provider)
        config = OrchestratorConfig(evidence_root=tmp_path)
        async with Orchestrator(config, profiles={"default": profile}) as orch:
            mission = await orch.submit_mission(spec("context-freeze"))
            orch.commit.begin_planning(mission.id)
            intent = await orch._create_planner_intent(mission.id, ordinal=1)
            frozen = orch.store.get_intent(intent.intent_id)
            assert frozen.config["runtime_context"] == profile.context_snapshot()
            original_hash = frozen.input_hash
            wrong = dict(frozen.config)
            wrong["runtime_context"] = {**profile.context_snapshot(), "fingerprint": "0" * 64}
            with pytest.raises(ContractError, match="context identity"):
                await orch._dispatch(replace(frozen, config=wrong))
            assert provider.calls == 0
            assert orch.store.get_intent(intent.intent_id).input_hash == original_hash
        # Real closed-library recovery of the pending intent, with no rewrite.
        async with Orchestrator(config, profiles={"default": _profile(provider)}) as reopened:
            restored = reopened.store.get_intent(intent.intent_id)
            assert restored.config == frozen.config and restored.input_hash == original_hash
            assert provider.calls == 0
    asyncio.run(run())
