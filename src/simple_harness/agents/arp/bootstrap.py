# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Builtin catalogue bootstrap (SKILL-CATALOGUE §3 "首次 builtin bootstrap").

The SDK's own tools (session history, delegate) and the consumer tools registered
through ``AgentRuntimePorts`` are static, in-process implementations. They enter the
namespace catalogue like any other entry — definition revision, QUARANTINED → TRIAL →
ADMITTED under the deployment profile's activation receipt, one builtin provider +
local deployment with a real health probe — never through an AllowAll shortcut. The
bootstrap is idempotent: unchanged definitions replay, admitted rows stay, the epoch
moves only when something actually changed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Mapping, Sequence

from simple_harness.tools import ToolSpec

from .catalogue import CatalogueService, latest_health, read_activation, resolve_pin
from .errors import ArpError
from .pins import Pin
from .ports import TrustedCaller
from .strict import digest, plain

BUILTIN_PROVIDER_ID = "sdk-builtin"
BUILTIN_DEPLOYMENT_ID = "sdk-builtin-local"
BUILTIN_CAPABILITY_ID = "sdk.tool.invoke"
RESULT_SCHEMA_ID = "sdk.tool.result"
HEALTH_TTL_MS = 24 * 3600 * 1000
HEALTH_REFRESH_MS = 60 * 60 * 1000  # re-probe when less than an hour of validity is left
DEFAULT_EFFECT_CLASSES = {
    "session_history_search": "READ_ONLY",
    "session_history_read": "READ_ONLY",
    "agent_delegate": "SANDBOX_WRITE",
}


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    namespace_id: str
    registry_epoch: int
    tools: tuple[str, ...]
    provider_ref: Pin
    deployment_ref: Pin
    capability_ref: Pin


def runtime_caller(profile_activation_ref: Pin, owner_contract_ref: Pin) -> TrustedCaller:
    """The runtime's own bootstrap principal, authorised by the profile activation receipt."""

    return TrustedCaller(
        principal_ref=Pin("principal", "runtime:bootstrap", 0, digest("runtime:bootstrap")),
        owner_contract_ref=owner_contract_ref,
        command_receipt_ref=profile_activation_ref,
    )


def refresh_builtin_health(catalogue: CatalogueService, deployment_pin: Pin, *, caller: TrustedCaller, run_id: str | None = None, force: bool = False) -> bool:
    """Re-probe the in-process deployment when its health line is missing, not READY,
    expired or inside the refresh window. A READY→READY refresh extends the line without
    an epoch step (§8); the call site (bootstrap and the runtime tick) keeps a long-lived
    process usable past the TTL."""

    revision = resolve_pin(catalogue.connection, catalogue.namespace_id, deployment_pin)
    health = latest_health(catalogue.connection, catalogue.namespace_id, revision.entry_id, revision.revision)
    now = catalogue.clock_ms()
    stale = health is None or health.body["health"] != "READY" or int(health.body["expires_at_ms"]) - now < HEALTH_REFRESH_MS
    if not stale and not force:
        return False
    if not stale and force and health is not None and int(health.body["expires_at_ms"]) - now >= HEALTH_TTL_MS // 2:
        return False  # bootstrap replay with plenty of time left: no new observation
    catalogue.probe(
        deployment_pin, health="READY", registered=True, configured=True, compatible=True, ttl_ms=HEALTH_TTL_MS,
        caller=caller, command_id=f"probe:{catalogue.namespace_id}:{revision.entry_id}:{now}", run_id=run_id,
    )
    return True


def bootstrap_builtin_tools(
    catalogue: CatalogueService,
    *,
    specs: Sequence[ToolSpec],
    caller: TrustedCaller,
    approval_ref: Pin,
    effect_classes: Mapping[str, str] | None = None,
    sdk_version: str,
    run_id: str | None = None,
) -> BootstrapReport:
    if approval_ref.kind != "authority":
        raise ArpError("REF_KIND_MISMATCH", field_path="approval_ref")
    effects = {**DEFAULT_EFFECT_CLASSES, **dict(effect_classes or {})}
    scope = catalogue.scope
    command = f"bootstrap:{scope.namespace_id}:{sdk_version}"
    policy = lambda name: Pin("policy", f"builtin:{name}", 0, digest({"policy": name, "sdk": sdk_version}))  # noqa: E731

    def admit(pin: Pin) -> None:
        revision = resolve_pin(catalogue.connection, scope.namespace_id, pin)
        current = read_activation(catalogue.connection, scope.namespace_id, revision.entry_kind, revision.entry_id, revision.revision)
        if current is None or current.state != "QUARANTINED":
            # Only a fresh (QUARANTINED) row is driven here. ADMITTED replays; SUSPENDED
            # stays suspended (an operator's revocation survives restarts, §9.7);
            # TRIAL waits for its evaluation; RETIRED is terminal.
            return
        for state in ("TRIAL", "ADMITTED"):
            catalogue.transition(pin, state=state, caller=caller, command_id=f"{command}:{pin.kind}:{pin.id}:{state}", run_id=run_id)

    # 1. Schemas (input schema per tool + one shared result schema).
    result_schema, _ = catalogue.register("SCHEMA", {"type": "object"}, entry_id=RESULT_SCHEMA_ID, caller=caller, command_id=f"{command}:schema:result", run_id=run_id)
    admit(result_schema.pin)
    # 2. Builtin capability, then the provider that declares it (pins are hashes: the
    #    provider → capability direction is the acyclic one).
    capability, _ = catalogue.register(
        "CAPABILITY",
        {
            "schema_version": 1,
            "capability_id": BUILTIN_CAPABILITY_ID,
            "version": 1,
            "input_schema_ref": result_schema.pin.to_json(),
            "output_schema_ref": result_schema.pin.to_json(),
            "description": "Invoke an in-process SDK tool with a JSON argument object.",
            "required_semantics": ["tool.invoke"],
            "verification_policy_ref": policy("verification").to_json(),
            "provider_refs": [],
        },
        entry_id=BUILTIN_CAPABILITY_ID, caller=caller, command_id=f"{command}:capability", run_id=run_id,
    )
    admit(capability.pin)
    provider, _ = catalogue.register(
        "PROVIDER",
        {
            "schema_version": 1,
            "provider_id": BUILTIN_PROVIDER_ID,
            "version": 1,
            "kind": "TOOL",
            "capability_refs": [capability.pin.to_json()],
            "input_schema_ref": result_schema.pin.to_json(),
            "output_schema_ref": result_schema.pin.to_json(),
            "implementation_ref": Pin("artifact", "sdk:simple_harness", 0, digest({"sdk": sdk_version})).to_json(),
            "selection_priority": 0,
            "priority_policy_ref": policy("priority").to_json(),
            "supported_semantics": ["tool.invoke"],
            "scope_ref": scope.pin.to_json(),
            "source_receipt_ref": caller.command_receipt_ref.to_json(),
        },
        entry_id=BUILTIN_PROVIDER_ID, caller=caller, command_id=f"{command}:provider", run_id=run_id,
    )
    admit(provider.pin)
    # 3. Local deployment of that provider + a real probe (READY: the process itself).
    deployment, _ = catalogue.register(
        "DEPLOYMENT",
        {
            "schema_version": 1,
            "provider_ref": provider.pin.to_json(),
            "implementation_digest": digest({"sdk": sdk_version, "platform": sys.platform}),
            "endpoint_namespace": "local",
            "credential_ref_name": None,
            "supported_capabilities": [capability.pin.to_json()],
            "supported_effect_classes": ["READ_ONLY", "SANDBOX_WRITE", "EXTERNAL_EFFECT"],
            "platforms": [sys.platform],
            "version": 1,
            "scope_ref": scope.pin.to_json(),
            "approval_ref": approval_ref.to_json(),
            "deployment_id": BUILTIN_DEPLOYMENT_ID,
        },
        entry_id=BUILTIN_DEPLOYMENT_ID, caller=caller, command_id=f"{command}:deployment", run_id=run_id,
    )
    admit(deployment.pin)
    refresh_builtin_health(catalogue, deployment.pin, caller=caller, run_id=run_id, force=True)
    # 4. One TOOL entry per registered spec, each with its exact input schema.
    names: list[str] = []
    for spec in sorted(specs, key=lambda s: s.name):
        parameters = plain(spec.input_schema) or {"type": "object"}
        schema, _ = catalogue.register("SCHEMA", parameters, entry_id=f"tool.{spec.name}.input", caller=caller, command_id=f"{command}:schema:{spec.name}", run_id=run_id)
        admit(schema.pin)
        tool, _ = catalogue.register(
            "TOOL",
            {
                "schema_version": 1,
                "tool_id": spec.name,
                "version": 1,
                "description": spec.description[:16000],
                "input_schema_ref": schema.pin.to_json(),
                "output_schema_ref": result_schema.pin.to_json(),
                "provider_ref": provider.pin.to_json(),
                "effect_class": effects.get(spec.name, "EXTERNAL_EFFECT"),
                "required_capabilities": [capability.pin.to_json()],
                "scope_policy_ref": policy("tool-scope").to_json(),
                "timeout_policy_ref": policy("tool-timeout").to_json(),
                "retry_policy_ref": policy("tool-retry").to_json(),
                "result_bytes_limit": 65536,
                "inline_result_tokens": 4096,
                "implementation_digest": digest({"tool": spec.name, "parameters": parameters, "sdk": sdk_version}),
            },
            entry_id=spec.name, caller=caller, command_id=f"{command}:tool:{spec.name}", run_id=run_id,
        )
        admit(tool.pin)
        names.append(spec.name)
    return BootstrapReport(scope.namespace_id, catalogue.epoch(), tuple(names), provider.pin, deployment.pin, capability.pin)


__all__ = ("BUILTIN_CAPABILITY_ID", "BUILTIN_DEPLOYMENT_ID", "BUILTIN_PROVIDER_ID", "BootstrapReport", "bootstrap_builtin_tools", "refresh_builtin_health", "runtime_caller")
