# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Shared ARP test assembly: managed root, activated standalone profile, trusted caller."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from provider_fixture import MODEL, ScriptedProvider

from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import ArpPorts, TrustedCaller, bootstrap_root
from simple_harness.agents.arp.profile import ProfileRefs, RuntimeProfile, default_policy
from simple_harness.agents.arp.runtime import build_arp_runtime
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.ports import AgentRuntimePorts
from simple_harness.runtime.ports import AuthorizationRequest, AuthorizationResult

HASH = "c" * 64


class RecordingAuthorization:
    """A consumer authorization port with an identifiable policy source (not AllowAll)."""

    policy_id = "tool-policy-test"

    def __init__(self) -> None:
        self.requests: list[AuthorizationRequest] = []

    async def request_authorization(self, request: AuthorizationRequest) -> AuthorizationResult:
        self.requests.append(request)
        return AuthorizationResult.allow()


def fixture_refs() -> ProfileRefs:
    return ProfileRefs(
        retention_policy_ref=Pin("policy", "retention-default", 1, HASH),
        capability_registry_ref=Pin("catalogue", "realm/test/project", 1, HASH),
        activation_receipt_ref=Pin("receipt", "deploy:activate:1", 0, HASH),
        default_skill_policy_ref=Pin("policy", "skill-default", 1, HASH),
        embedding_deployment_ref=Pin("deployment", "bge-m3-local", 1, HASH),
        catalogue_namespace_id="realm/test/project",
    )


def standalone_profile(*, embedding_required: bool = False, degrade: bool = True) -> RuntimeProfile:
    policy = default_policy("native-context-test")
    policy["embedding_required_for_activation"] = embedding_required
    return RuntimeProfile(
        profile_id="standalone-test",
        profile_revision=1,
        owner_mode="STANDALONE_CHAT",
        allow_lexical_degradation=degrade,
        context_policy=policy,
        refs=fixture_refs(),
    )


def trusted_caller(command_id: str = "create-1") -> TrustedCaller:
    return TrustedCaller(
        principal_ref=Pin("principal", "user:test", 1, HASH),
        owner_contract_ref=Pin("policy", "owner-standalone", 1, HASH),
        command_receipt_ref=Pin("receipt", f"host:command:{command_id}", 0, digest({"command_id": command_id})),
    )


def activation_receipt() -> dict[str, Any]:
    return {"kind": "deployment_activation", "profile_id": "standalone-test", "revision": 1}


def build(
    tmp_path: Path,
    provider: ScriptedProvider | None = None,
    *,
    profile: RuntimeProfile | None = None,
    embedding_available: bool = False,
    fault: Callable[[str], None] | None = None,
    authorization: object | None = None,
    clock: Callable[[], float] | None = None,
    owner_id: str = "arp-test-owner",
):
    root = bootstrap_root(tmp_path / "root", root_id="root-test")
    ports = AgentRuntimePorts(
        provider=provider or ScriptedProvider(["ok"]),
        authorization=authorization or RecordingAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id=owner_id,
        **({} if clock is None else {"clock": clock}),
    )
    arp = ArpPorts(
        root_dir=root.directory,
        profile=profile or standalone_profile(),
        activation_receipt=activation_receipt(),
        embedding_available=embedding_available,
        fault=fault,
    )
    return build_arp_runtime(ports, arp)
