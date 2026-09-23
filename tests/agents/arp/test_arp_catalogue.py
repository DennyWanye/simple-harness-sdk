"""Unified catalogue (§8): builtin bootstrap, tool exposure frozen into the manifest,
revocation re-checked at prepare and at call time, health refresh vs category change,
capability resolution (hard filter before priority) and bounded catalogue pages."""

from __future__ import annotations

import asyncio
import sys

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import catalogue as cat
from simple_harness.agents.arp import store
from simple_harness.agents.arp.bootstrap import BUILTIN_CAPABILITY_ID, BUILTIN_PROVIDER_ID
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.arp.tools import READ_TOOL_NAME, SEARCH_TOOL_NAME
from simple_harness.agents.contracts import AgentTurnState

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p", tool_names=(SEARCH_TOOL_NAME, READ_TOOL_NAME))


def _tool_pin(runtime, name: str) -> Pin:  # type: ignore[no-untyped-def]
    service = runtime.arp.catalogue
    row = cat.latest_revision(service.connection, service.namespace_id, "TOOL", name)
    assert row is not None
    return row.pin


def _state(runtime, pin: Pin) -> str:  # type: ignore[no-untyped-def]
    service = runtime.arp.catalogue
    row = cat.resolve_pin(service.connection, service.namespace_id, pin)
    activation = cat.read_activation(service.connection, service.namespace_id, row.entry_kind, row.entry_id, row.revision)
    return activation.state


def _provider_body(service, *, provider_id: str, priority: int, capability: Pin) -> dict:  # type: ignore[no-untyped-def]
    schema = cat.latest_revision(service.connection, service.namespace_id, "SCHEMA", "sdk.tool.result").pin
    return {
        "schema_version": 1, "provider_id": provider_id, "version": 1, "kind": "TOOL", "capability_refs": [capability.to_json()],
        "input_schema_ref": schema.to_json(), "output_schema_ref": schema.to_json(),
        "implementation_ref": Pin("artifact", f"impl:{provider_id}", 0, digest(provider_id)).to_json(),
        "selection_priority": priority, "priority_policy_ref": Pin("policy", "prio", 0, digest("prio")).to_json(),
        "supported_semantics": ["tool.invoke"], "scope_ref": service.scope.pin.to_json(), "source_receipt_ref": trusted_caller().command_receipt_ref.to_json(),
    }


def _deployment_body(service, *, deployment_id: str, provider: Pin, capability: Pin, approval: Pin) -> dict:  # type: ignore[no-untyped-def]
    return {
        "schema_version": 1, "provider_ref": provider.to_json(), "implementation_digest": digest(deployment_id), "endpoint_namespace": "remote:test",
        "credential_ref_name": "TEST_CRED", "supported_capabilities": [capability.to_json()], "supported_effect_classes": ["READ_ONLY"],
        "platforms": [sys.platform], "version": 1, "scope_ref": service.scope.pin.to_json(), "approval_ref": approval.to_json(), "deployment_id": deployment_id,
    }


def _admit(service, pin: Pin, command: str) -> None:  # type: ignore[no-untyped-def]
    for state in ("TRIAL", "ADMITTED"):
        service.transition(pin, state=state, caller=trusted_caller(), command_id=f"{command}:{state}")


def test_bootstrap_admits_builtin_tools_once_and_freezes_exposure_into_the_manifest(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["好的。", "好的。"])
        runtime = build(tmp_path, provider)
        async with runtime:
            service = runtime.arp.catalogue
            report = runtime.arp.bootstrap
            assert {SEARCH_TOOL_NAME, READ_TOOL_NAME, "agent_delegate"} <= set(report.tools)
            for pin in (report.provider_ref, report.deployment_ref, report.capability_ref, _tool_pin(runtime, SEARCH_TOOL_NAME)):
                assert _state(runtime, pin) == "ADMITTED"
            epoch = service.epoch()
            assert epoch > 0
            mount = cat.read_mount(service.connection, service.namespace_id)
            assert mount is not None and mount.catalogue_owner_root_id == runtime.arp.root.root_id
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            receipt = await agent.submit("你好", input_id="i0")
            assert (await agent.wait_turn(receipt.turn_id, timeout=10)).state is AgentTurnState.COMMITTED
            connection = runtime.uow.database.connection
            manifest = store.read_context_by_request_key(connection, provider.requests[-1].request_id.value).manifest
            assert manifest["registry_epoch"] == epoch
            snapshot = cat.read_tool_snapshot(connection, manifest["tool_snapshot_ref"]["id"])
            assert snapshot is not None and {t["model_name"] for t in snapshot["tools"]} == {SEARCH_TOOL_NAME, READ_TOOL_NAME}
            assert {t.name for t in provider.requests[-1].tools} == {SEARCH_TOOL_NAME, READ_TOOL_NAME}
            assert len(manifest["catalogue_witness_refs"]) == 2
        # A second runtime on the same store replays the bootstrap: nothing changes, no epoch step.
        runtime2 = build(tmp_path, ScriptedProvider([]))
        async with runtime2:
            assert runtime2.arp.catalogue.epoch() == epoch

    asyncio.run(case())


def test_suspending_a_tool_removes_it_from_the_next_request_and_from_dispatch(tmp_path) -> None:
    async def case() -> None:
        provider = ScriptedProvider(["好的。", "好的。"])
        runtime = build(tmp_path, provider)
        async with runtime:
            service = runtime.arp.catalogue
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            receipt = await agent.submit("第一句", input_id="i0")
            assert (await agent.wait_turn(receipt.turn_id, timeout=10)).state is AgentTurnState.COMMITTED
            assert SEARCH_TOOL_NAME in {t.name for t in provider.requests[-1].tools}
            before = service.epoch()
            pin = _tool_pin(runtime, SEARCH_TOOL_NAME)
            row = service.transition(pin, state="SUSPENDED", caller=trusted_caller(), command_id="suspend-1")
            assert row.state == "SUSPENDED" and service.epoch() == before + 1
            # Re-sent command: same outcome, no second epoch step.
            service.transition(pin, state="SUSPENDED", caller=trusted_caller(), command_id="suspend-1")
            assert service.epoch() == before + 1
            # Dispatch re-checks the catalogue: the suspended tool is no longer exposed to the Run.
            registry = runtime._assembled.registry
            assert registry.exposed_tools(agent.run_id) == frozenset({READ_TOOL_NAME})
            receipt = await agent.submit("第二句", input_id="i1")
            assert (await agent.wait_turn(receipt.turn_id, timeout=10)).state is AgentTurnState.COMMITTED
            assert {t.name for t in provider.requests[-1].tools} == {READ_TOOL_NAME}
            manifest = store.read_context_by_request_key(runtime.uow.database.connection, provider.requests[-1].request_id.value).manifest
            assert manifest["registry_epoch"] == before + 1
            # The model view of the catalogue says why.
            page = service.page({"namespace_id": service.namespace_id, "kind": "TOOL", "cursor": None, "limit": 64}, access_view="MODEL")
            by_name = {i["name"]: i for i in page["items"]}
            assert by_name[SEARCH_TOOL_NAME]["current_usable"] is False and "STATE_SUSPENDED" in by_name[SEARCH_TOOL_NAME]["reason_codes"]
            assert by_name[READ_TOOL_NAME]["current_usable"] is True
            # Illegal lifecycle steps are named; RETIRED is terminal.
            with pytest.raises(ArpError) as refused:
                service.transition(pin, state="QUARANTINED", caller=trusted_caller(), command_id="bad-1")
            assert refused.value.code == "STATE_COMBINATION_INVALID"
            service.transition(pin, state="RETIRED", caller=trusted_caller(), command_id="retire-1")
            with pytest.raises(ArpError):
                service.transition(pin, state="ADMITTED", caller=trusted_caller(), command_id="revive-1")

    asyncio.run(case())


def test_health_refresh_keeps_the_epoch_and_a_category_change_moves_it(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            service = runtime.arp.catalogue
            deployment = runtime.arp.bootstrap.deployment_ref
            epoch = service.epoch()
            first = service.probe(deployment, health="READY", registered=True, configured=True, compatible=True, ttl_ms=60_000, caller=trusted_caller(), command_id="p1")
            assert service.epoch() == epoch and first.health_revision >= 2  # healthy → healthy: no semantic change
            down = service.probe(deployment, health="UNAVAILABLE", registered=True, configured=True, compatible=True, ttl_ms=60_000, caller=trusted_caller(), command_id="p2")
            assert service.epoch() == epoch + 1 and down.health_revision == first.health_revision + 1
            exposed, refused = runtime.arp.exposure.exposed([SEARCH_TOOL_NAME], now_ms=service.clock_ms())
            assert exposed == () and "NO_USABLE_DEPLOYMENT" in refused[SEARCH_TOOL_NAME]
            service.probe(deployment, health="READY", registered=True, configured=True, compatible=True, ttl_ms=60_000, caller=trusted_caller(), command_id="p3")
            assert service.epoch() == epoch + 2
            exposed, _ = runtime.arp.exposure.exposed([SEARCH_TOOL_NAME], now_ms=service.clock_ms())
            assert [e.model_name for e in exposed] == [SEARCH_TOOL_NAME]

    asyncio.run(case())


def test_capability_resolution_filters_before_priority_and_binds_once(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            service = runtime.arp.catalogue
            capability = runtime.arp.bootstrap.capability_ref
            approval = runtime.arp.policy.approval_ref
            # A higher-priority provider whose only deployment is not healthy.
            fast, _ = service.register("PROVIDER", _provider_body(service, provider_id="fast", priority=100, capability=capability), entry_id="fast", caller=trusted_caller(), command_id="reg-fast")
            _admit(service, fast.pin, "adm-fast")
            fast_dep, _ = service.register("DEPLOYMENT", _deployment_body(service, deployment_id="fast-dep", provider=fast.pin, capability=capability, approval=approval), entry_id="fast-dep", caller=trusted_caller(), command_id="reg-fast-dep")
            _admit(service, fast_dep.pin, "adm-fast-dep")
            service.probe(fast_dep.pin, health="UNAVAILABLE", registered=True, configured=False, compatible=True, ttl_ms=60_000, caller=trusted_caller(), command_id="probe-fast")
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            owner = Pin("policy", "owner", 0, digest("owner"))
            manifest_ref = Pin("input_manifest", "m1", 0, digest("m1"))
            binding = runtime.arp.resolver.resolve(
                session, owner_contract_ref=owner, requirement_key="tool.invoke", capability_ref=capability, input_manifest_ref=manifest_ref,
                invocation_mode="TOOL", authority_refs=[approval], platform=sys.platform,
            )
            assert binding["provider_ref"]["id"] == BUILTIN_PROVIDER_ID, "the unhealthy high-priority provider is filtered before priority"
            assert binding["capability_ref"]["id"] == BUILTIN_CAPABILITY_ID
            again = runtime.arp.resolver.resolve(
                session, owner_contract_ref=owner, requirement_key="tool.invoke", capability_ref=capability, input_manifest_ref=manifest_ref,
                invocation_mode="TOOL", authority_refs=[approval], platform=sys.platform,
            )
            assert again["binding_id"] == binding["binding_id"]
            rows = runtime.uow.database.connection.execute("SELECT COUNT(*) FROM arp_capability_bindings WHERE session_id=?", (session.session_id,)).fetchone()[0]
            assert rows == 1
            # Once healthy, the approved priority decides.
            service.probe(fast_dep.pin, health="READY", registered=True, configured=True, compatible=True, ttl_ms=60_000, caller=trusted_caller(), command_id="probe-fast-2")
            preferred = runtime.arp.resolver.resolve(
                session, owner_contract_ref=owner, requirement_key="tool.invoke", capability_ref=capability, input_manifest_ref=manifest_ref,
                invocation_mode="TOOL", authority_refs=[approval], platform=sys.platform,
            )
            assert preferred["provider_ref"]["id"] == "fast" and preferred["registry_epoch"] > binding["registry_epoch"]
            # A mode no provider serves: a named refusal, never a Runtime switch.
            with pytest.raises(ArpError) as refused:
                runtime.arp.resolver.resolve(
                    session, owner_contract_ref=owner, requirement_key="wf", capability_ref=capability, input_manifest_ref=manifest_ref,
                    invocation_mode="WORKFLOW", authority_refs=[approval],
                )
            assert refused.value.code == "CAPABILITY_UNAVAILABLE"

    asyncio.run(case())


def test_catalogue_pages_are_bounded_and_epoch_bound(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            service = runtime.arp.catalogue
            command = {"namespace_id": service.namespace_id, "kind": "TOOL", "cursor": None, "limit": 2}
            first = service.page(command, access_view="MANAGEMENT")
            assert len(first["items"]) == 2 and first["has_more"] and first["next_cursor"]
            assert first["body_bytes"] <= 65536 and all(i["access_view"] == "MANAGEMENT" for i in first["items"])
            second = service.page({**command, "cursor": first["next_cursor"]}, access_view="MANAGEMENT")
            names = [i["name"] for i in first["items"] + second["items"]]
            assert names == sorted(names) and len(set(names)) == len(names)
            for item in first["items"] + second["items"]:
                assert "credential_ref_name" not in item and "endpoint_namespace" not in item
            # The catalogue changes → the old cursor is stale.
            service.transition(_tool_pin(runtime, READ_TOOL_NAME), state="SUSPENDED", caller=trusted_caller(), command_id="s1")
            with pytest.raises(ArpError) as refused:
                service.page({**command, "cursor": first["next_cursor"]}, access_view="MANAGEMENT")
            assert refused.value.code == "CATALOGUE_STALE"
            with pytest.raises(ArpError) as other:
                service.page({**command, "namespace_id": "other/ns/-"}, access_view="MODEL")
            assert other.value.code == "REF_OUTSIDE_SCOPE"

    asyncio.run(case())


def test_operator_revocations_survive_a_restart_and_dependencies_revoke_tools(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            service = runtime.arp.catalogue
            tool = _tool_pin(runtime, SEARCH_TOOL_NAME)
            deployment = runtime.arp.bootstrap.deployment_ref
            service.transition(tool, state="SUSPENDED", caller=trusted_caller(), command_id="s-tool")
            service.transition(deployment, state="SUSPENDED", caller=trusted_caller(), command_id="s-dep")
            epoch = service.epoch()
        # A restart replays the bootstrap but never re-admits what an operator suspended.
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            service = runtime.arp.catalogue
            assert _state(runtime, tool) == "SUSPENDED" and _state(runtime, deployment) == "SUSPENDED"
            assert service.epoch() == epoch
            # Resume both; then suspending the capability alone revokes every tool that needs it.
            service.transition(deployment, state="ADMITTED", caller=trusted_caller(), command_id="r-dep")
            service.transition(tool, state="ADMITTED", caller=trusted_caller(), command_id="r-tool")
            exposed, _ = runtime.arp.exposure.exposed([SEARCH_TOOL_NAME, READ_TOOL_NAME], now_ms=service.clock_ms())
            assert {e.model_name for e in exposed} == {SEARCH_TOOL_NAME, READ_TOOL_NAME}
            service.transition(runtime.arp.bootstrap.capability_ref, state="SUSPENDED", caller=trusted_caller(), command_id="s-cap")
            exposed, refused = runtime.arp.exposure.exposed([SEARCH_TOOL_NAME, READ_TOOL_NAME], now_ms=service.clock_ms())
            assert exposed == () and any(r.startswith("CAPABILITY_NOT_ADMITTED") for r in refused[SEARCH_TOOL_NAME])
            # Same for the shared result schema.
            service.transition(runtime.arp.bootstrap.capability_ref, state="ADMITTED", caller=trusted_caller(), command_id="r-cap")
            schema = cat.latest_revision(service.connection, service.namespace_id, "SCHEMA", "sdk.tool.result").pin
            service.transition(schema, state="SUSPENDED", caller=trusted_caller(), command_id="s-schema")
            exposed, refused = runtime.arp.exposure.exposed([READ_TOOL_NAME], now_ms=service.clock_ms())
            assert exposed == () and any(r.startswith("SCHEMA_NOT_ADMITTED") for r in refused[READ_TOOL_NAME])

    asyncio.run(case())


def test_builtin_health_is_refreshed_by_the_tick_before_it_expires(tmp_path) -> None:
    async def case() -> None:
        now = {"ms": 1_700_000_000_000}
        runtime = build(tmp_path, ScriptedProvider([]), clock_ms=lambda: now["ms"])
        async with runtime:
            service = runtime.arp.catalogue
            deployment = runtime.arp.bootstrap.deployment_ref
            epoch = service.epoch()
            first = cat.latest_health(service.connection, service.namespace_id, deployment.id, deployment.revision)
            # Well inside the TTL: the tick does not probe.
            now["ms"] += 6 * 3600 * 1000
            assert runtime.arp.tick()["health_probed"] == 0
            # Inside the refresh window (less than an hour left): the tick re-probes, READY→READY, no epoch step.
            now["ms"] += 17 * 3600 * 1000 + 30 * 60 * 1000
            assert runtime.arp.tick()["health_probed"] == 1
            latest = cat.latest_health(service.connection, service.namespace_id, deployment.id, deployment.revision)
            assert latest.health_revision == first.health_revision + 1 and service.epoch() == epoch
            # A day later (past the first line's expiry) the tools are still usable.
            now["ms"] += 24 * 3600 * 1000 - 60 * 1000
            runtime.arp.tick()
            exposed, refused = runtime.arp.exposure.exposed([SEARCH_TOOL_NAME], now_ms=service.clock_ms())
            assert [e.model_name for e in exposed] == [SEARCH_TOOL_NAME], refused

    asyncio.run(case())


def test_resolution_requires_the_capability_schemas(tmp_path) -> None:
    async def case() -> None:
        runtime = build(tmp_path, ScriptedProvider([]))
        async with runtime:
            service = runtime.arp.catalogue
            capability = runtime.arp.bootstrap.capability_ref
            approval = runtime.arp.policy.approval_ref
            other_schema, _ = service.register("SCHEMA", {"type": "object", "properties": {"x": {"type": "string"}}}, entry_id="other.schema", caller=trusted_caller(), command_id="reg-schema")
            _admit(service, other_schema.pin, "adm-schema")
            body = _provider_body(service, provider_id="mismatch", priority=1000, capability=capability)
            body["input_schema_ref"] = other_schema.pin.to_json()
            mismatch, _ = service.register("PROVIDER", body, entry_id="mismatch", caller=trusted_caller(), command_id="reg-mismatch")
            _admit(service, mismatch.pin, "adm-mismatch")
            dep, _ = service.register("DEPLOYMENT", _deployment_body(service, deployment_id="mismatch-dep", provider=mismatch.pin, capability=capability, approval=approval), entry_id="mismatch-dep", caller=trusted_caller(), command_id="reg-mismatch-dep")
            _admit(service, dep.pin, "adm-mismatch-dep")
            service.probe(dep.pin, health="READY", registered=True, configured=True, compatible=True, ttl_ms=60_000, caller=trusted_caller(), command_id="probe-mismatch")
            agent = await runtime.create(CONFIG, creation_key="k1", caller=trusted_caller())
            session = store.read_live_session(runtime.uow.database.connection, agent.agent_id)
            binding = runtime.arp.resolver.resolve(
                session, owner_contract_ref=Pin("policy", "owner", 0, digest("owner")), requirement_key="tool.invoke", capability_ref=capability,
                input_manifest_ref=Pin("input_manifest", "m1", 0, digest("m1")), invocation_mode="TOOL", authority_refs=[approval], platform=sys.platform,
            )
            assert binding["provider_ref"]["id"] == BUILTIN_PROVIDER_ID, "a schema-incompatible provider is filtered even at the highest priority"

    asyncio.run(case())
