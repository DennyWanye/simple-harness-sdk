# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-E3a: the orchestrator assembly runs a pool on the native runtime plane.

A legacy pool and a native pool are assembled side by side.  The native pool is built
by ``build_arp_runtime`` under the deployment's real authorization port, and its bridge
creates Agents only from a claimed dispatch intent (the authenticated creation caller
is derived from it).  AllowAll on a native pool is refused at assembly time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from arp_fixture import HASH, ExactWordTokenizer, RecordingAuthorization, activation_receipt, meter_binding, standalone_profile
from provider_fixture import ScriptedProvider

from agent_orchestrator.runtime.assembly import OrchestratorConfig, assemble_orchestrator_runtime
from agent_orchestrator.runtime.model_router import RuntimeProfile as PoolProfile
from agent_orchestrator.runtime.native_plane import NativePlaneAssembly, intent_caller
from simple_harness.agents import AgentConfig
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.arp import store
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import ArpPorts, bootstrap_root
from simple_harness.agents.ports import AllowAllAuthorization

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
OWNER_CONTRACT = Pin("policy", "owner-native-pool", 1, HASH)


def _intent(key: str) -> SimpleNamespace:
    return SimpleNamespace(intent_id=f"intent-{key}", mission_id="mission-1", kind="attempt", creation_key=key, input_hash="a" * 64)


def _native(tokenizer: ExactWordTokenizer, *, authorization) -> NativePlaneAssembly:  # type: ignore[no-untyped-def]
    def arp_ports(execution_db: Path) -> ArpPorts:
        root = bootstrap_root(execution_db.parent / "arp-root", root_id="root-test")
        return ArpPorts(root_dir=root.directory, profile=standalone_profile(), activation_receipt=activation_receipt(), meter=meter_binding(tokenizer))

    return NativePlaneAssembly(
        arp_ports=arp_ports,
        authorization=authorization,
        caller_for=lambda intent: intent_caller(intent, principal_id="host:user", owner_contract_ref=OWNER_CONTRACT),
    )


def test_native_pool_is_assembled_beside_the_legacy_pool_and_creates_only_from_intents(tmp_path) -> None:
    async def case() -> None:
        tokenizer = ExactWordTokenizer()
        provider = ScriptedProvider(["好的。"] * 4)
        profiles = {
            "default": PoolProfile("default", provider, "agent-model"),
            "native": PoolProfile("native", provider, "agent-model", context_policy=ContextPolicy(), tokenizer=tokenizer, native_plane=_native(tokenizer, authorization=RecordingAuthorization())),
        }
        assembled = assemble_orchestrator_runtime(OrchestratorConfig(evidence_root=tmp_path / "root"), profiles=profiles, default_profile="default")
        legacy, native = assembled.pool("default"), assembled.pool("native")
        assert getattr(legacy.runtime, "arp", None) is None and legacy.bridge.native_plane is False
        assert native.runtime.arp.protocol == "ARP_V1_1_1" and native.bridge.native_plane is True
        assert native.execution_db != legacy.execution_db
        async with native.runtime:
            with pytest.raises(ValueError):
                await native.bridge.create(creation_key="k1", config_json=CONFIG.to_json())
            agent_id, run_id, _ = await native.bridge.create(creation_key="k1", config_json=CONFIG.to_json(), intent=_intent("k1"))
            session = store.read_live_session(native.runtime.uow.database.connection, agent_id)
            assert session is not None and session.state == "ACTIVE" and run_id == agent_id
            # Replay of the same intent: the same Agent, no second session.
            again, _, _ = await native.bridge.create(creation_key="k1", config_json=CONFIG.to_json(), intent=_intent("k1"))
            assert again == agent_id
            assert native.runtime.uow.database.connection.execute("SELECT COUNT(*) FROM arp_agent_sessions").fetchone()[0] == 1
            # The creation was recorded under the intent-derived caller, not a bootstrap principal.
            row = native.runtime.uow.database.connection.execute(
                "SELECT original_receipt_ref_json, state FROM arp_creation_intents WHERE creation_key='k1'"
            ).fetchone()
            assert row is not None and row[1] == "BOUND" and "dispatch-intent:intent-k1" in row[0]
        async with legacy.runtime:
            legacy_id, _, _ = await legacy.bridge.create(creation_key="k1", config_json=CONFIG.to_json())
            assert legacy_id

    asyncio.run(case())


def test_allow_all_authorization_is_refused_for_a_native_pool(tmp_path) -> None:
    tokenizer = ExactWordTokenizer()
    provider = ScriptedProvider([])
    profiles = {"native": PoolProfile("native", provider, "agent-model", context_policy=ContextPolicy(), tokenizer=tokenizer, native_plane=_native(tokenizer, authorization=AllowAllAuthorization()))}
    with pytest.raises(ArpError) as refused:
        assemble_orchestrator_runtime(OrchestratorConfig(evidence_root=tmp_path / "root"), profiles=profiles, default_profile="native")
    assert refused.value.code == "AUTHORITY_SOURCE_MISSING"


# ---- prior reserve reader (RP-E3b) ------------------------------------------------------------


def _invocation(invocation_id: str, state: str, output: int | None) -> SimpleNamespace:
    # Real records expose ``usage_json`` as a read-only mapping (not a dict); the fake does too.
    usage = None if output is None else MappingProxyType({"usage": MappingProxyType({"input_tokens": 3, "output_tokens": output})})
    return SimpleNamespace(invocation_id=invocation_id, state=state, usage_json=usage)


class _FakeUow:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    def list_provider_invocations(self, run_id):  # type: ignore[no-untyped-def]
        assert run_id.value == "run-1"
        return tuple(self.rows)

    def read_effective_provider_invocation(self, invocation_id):  # type: ignore[no-untyped-def]
        return next(r for r in self.rows if r.invocation_id == invocation_id)


def test_the_prior_reserve_is_the_recorded_output_of_the_run_and_never_a_guess() -> None:
    from agent_orchestrator.runtime.native_plane import RunPriorReserve

    reader = RunPriorReserve()
    with pytest.raises(ArpError) as unbound:
        reader("run-1")
    assert unbound.value.code == "PRIOR_RESERVE_UNAVAILABLE"
    reader.bind(SimpleNamespace(uow=_FakeUow([])))
    assert reader("run-1").tokens == 0 and reader("run-1").basis_ref is None
    reader.bind(SimpleNamespace(uow=_FakeUow([_invocation("i1", "succeeded", 40), _invocation("i2", "claimed", None), _invocation("i3", "failed", 2)])))
    basis = reader("run-1")
    assert basis.tokens == 42 and basis.basis_ref is not None and basis.basis_ref.id == "prior-output:run-1"
    same = reader("run-1")
    assert same.basis_ref == basis.basis_ref
    # An invocation whose usage is unresolved makes the reserve unavailable by name.
    reader.bind(SimpleNamespace(uow=_FakeUow([_invocation("i1", "succeeded", 40), _invocation("i4", "handed_off", None)])))
    with pytest.raises(ArpError) as unresolved:
        reader("run-1")
    assert unresolved.value.code == "PRIOR_RESERVE_UNAVAILABLE" and unresolved.value.detail["invocation_id"] == "i4"
    reader.bind(SimpleNamespace(uow=_FakeUow([_invocation("i5", "succeeded", None)])))
    with pytest.raises(ArpError):
        reader("run-1")


def test_after_build_binds_the_reader_to_the_assembled_native_pool(tmp_path) -> None:
    from agent_orchestrator.runtime.native_plane import RunPriorReserve

    tokenizer = ExactWordTokenizer()
    reader = RunPriorReserve()
    base = _native(tokenizer, authorization=RecordingAuthorization())
    native = NativePlaneAssembly(arp_ports=base.arp_ports, authorization=base.authorization, caller_for=base.caller_for, after_build=reader.bind)
    profiles = {"native": PoolProfile("native", ScriptedProvider([]), "agent-model", context_policy=ContextPolicy(), tokenizer=tokenizer, native_plane=native)}
    assembled = assemble_orchestrator_runtime(OrchestratorConfig(evidence_root=tmp_path / "root"), profiles=profiles, default_profile="native")
    assert reader._uow is assembled.pool("native").runtime.uow
    assert reader("run-never").tokens == 0
