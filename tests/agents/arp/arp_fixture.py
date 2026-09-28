# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Shared ARP test assembly: managed root, activated standalone profile, trusted caller."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from provider_fixture import MODEL, ScriptedProvider

from simple_harness.agents.arp.meter import MeterBinding, model_limits
from simple_harness.agents.arp.pins import Pin
from simple_harness.agents.arp.ports import ArpPorts, TrustedCaller, bootstrap_root
from simple_harness.agents.arp.profile import ProfileRefs, RuntimeProfile, default_policy
from simple_harness.agents.arp.runtime import build_arp_runtime
from simple_harness.agents.arp.strict import digest
from simple_harness.agents.ports import AgentRuntimePorts
from simple_harness.runtime.ports import AuthorizationRequest, AuthorizationResult

HASH = "c" * 64


class ExactWordTokenizer:
    """The scripted provider's real counter: it bills one token per whitespace word.

    ``ScriptedProvider`` has no tokenizer of its own, so for this deployment the
    word count *is* the exact wire count; the binding declares it EXACT and the
    certification receipt names this rule.  Never use it for a real model.
    """

    fingerprint = "test-exact-words:v1"
    count_mode = "EXACT"

    def count_text(self, text: str) -> int:
        return len(text.split())


def meter_binding(
    tokenizer: ExactWordTokenizer, *, input_limit: int = 8192, max_output: int = 8192, combined: int | None = None
) -> MeterBinding:
    limits = model_limits(
        model=MODEL,
        tokenizer=tokenizer,
        input_limit_tokens=input_limit,
        max_output_tokens=max_output,
        combined_limit_tokens=combined,
        provider_id="scripted-test",
    )
    return MeterBinding(
        tokenizer=tokenizer,
        model_limits=limits,
        certification_ref=Pin("receipt", "meter-certification:test-exact-words", 0, digest({"rule": "one token per word"})),
    )


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
    # A test deployment: the scripted provider's window is small, so the budget
    # terms are scaled down from the 512K default candidate (same shape, same rules).
    policy.update(
        max_context_tokens=8192,
        output_reserve_tokens=1024,
        safety_reserve_tokens=64,
        tool_headroom_tokens=128,
        recent_min_tokens=1024,
        recall_max_tokens=1024,
        fixed_soft_max_tokens=4096,
        section_soft_caps={"A": 1024, "B": 1024, "C": 1024, "D": 1024, "E": 1024},
        embedding_chunk_tokens=64,
        embedding_overlap_tokens=8,
        max_scan_rows_per_page=4,
        max_recall_items=8,
    )
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
    embedding: object | None = None,
    policy_overrides: dict[str, Any] | None = None,
    input_limit: int = 8192,
    max_output: int = 1024,
    clock_ms: Callable[[], int] | None = None,
    acceptance: object | None = None,
    script_runner: object | None = None,
    artifacts: object | None = None,
    mission_sources: object | None = None,
    catalogue_authority: object | None = None,
    **port_overrides: Any,
):
    root = bootstrap_root(tmp_path / "root", root_id="root-test")
    tokenizer = ExactWordTokenizer()
    profile = profile or standalone_profile()
    if policy_overrides:
        profile.context_policy.update(policy_overrides)
    ports = AgentRuntimePorts(
        provider=provider or ScriptedProvider(["ok"]),
        authorization=authorization or RecordingAuthorization(),
        database_path=str(tmp_path / "runtime.db"),
        model=MODEL,
        owner_id=owner_id,
        tokenizer=tokenizer,
        default_max_output_tokens=min(max_output, 256),
        max_output_tokens_ceiling=max_output,
        **({} if clock is None else {"clock": clock}),
        **port_overrides,
    )
    arp = ArpPorts(
        root_dir=root.directory,
        profile=profile,
        activation_receipt=activation_receipt(),
        embedding_available=embedding is not None or embedding_available,
        fault=fault,
        meter=meter_binding(tokenizer, input_limit=input_limit, max_output=max_output),
        embedding=embedding,
        embedding_resource_ref=None if embedding is None else Pin("deployment", "embedding-test", 1, digest(str(getattr(embedding, "fingerprint", "?")))),
        acceptance=acceptance,
        script_runner=script_runner,
        artifacts=artifacts,
        mission_sources=mission_sources,
        catalogue_authority=catalogue_authority,
        **({} if clock_ms is None else {"clock_ms": clock_ms}),
    )
    return build_arp_runtime(ports, arp)
