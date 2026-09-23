# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``RuntimeProfile`` / ``Policy`` objects for the ``ARP_V1_1_1`` creation protocol.

The default policy candidate (``contracts/default-policy.candidate.json``) is the
documented default *value*; it becomes authoritative only through the activation
receipt an authenticated deployment command produces (resources/DEPENDENCIES.md).
512K is the configured ceiling, not proof that a deployment supports it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any, Mapping

from . import PROTOCOL, codec
from .errors import ArpError
from .pins import Pin, pin_for
from .rules import embedding_mode

# Hard ceilings the contract fixes for a submitted Policy (CONTEXT-SEARCH C7 / R5).
POLICY_HARD_LIMITS: Mapping[str, int] = {
    "max_scan_rows_per_page": 256,
    "max_search_cursors": 16,
    "max_retrieval_ms": 500,
    "max_query_total_ms": 30000,
    "max_query_candidates": 512,
    "max_candidates": 512,
    "max_recall_items": 128,
    "max_group_count": 8192,
}


def default_policy(policy_id: str | None = None) -> dict[str, Any]:
    raw = resources.files(__package__).joinpath("contracts/default-policy.candidate.json")
    policy = json.loads(raw.read_bytes().decode("utf-8"))
    if policy_id is not None:
        policy["policy_id"] = policy_id
    return codec.check("Policy", policy)


def check_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Structural + semantic + hard-ceiling validation of a submitted Policy."""

    value = codec.check("Policy", dict(policy))
    for field, ceiling in POLICY_HARD_LIMITS.items():
        if value[field] > ceiling:
            raise ArpError("POLICY_CONFLICT", f"{field} exceeds hard limit {ceiling}", field_path=field)
    if value["output_reserve_tokens"] + value["safety_reserve_tokens"] >= value["max_context_tokens"]:
        raise ArpError("NO_INPUT_BUDGET", field_path="max_context_tokens")
    return value


def policy_pin(policy: Mapping[str, Any], revision: int) -> Pin:
    return pin_for("policy", str(policy["policy_id"]), revision, check_policy(policy))


@dataclass(frozen=True, slots=True)
class ProfileRefs:
    """Approved references an authenticated bootstrap supplies; never model-provided."""

    retention_policy_ref: Pin
    capability_registry_ref: Pin
    activation_receipt_ref: Pin
    default_skill_policy_ref: Pin
    embedding_deployment_ref: Pin
    catalogue_namespace_id: str

    def __post_init__(self) -> None:
        self.retention_policy_ref.require_kind("policy")
        self.capability_registry_ref.require_kind("catalogue")
        self.activation_receipt_ref.require_kind("receipt")
        self.default_skill_policy_ref.require_kind("policy")
        self.embedding_deployment_ref.require_kind("deployment")
        if not isinstance(self.catalogue_namespace_id, str) or not self.catalogue_namespace_id:
            raise ArpError("MISSING_FIELD", field_path="catalogue_namespace_id")


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    profile_id: str
    profile_revision: int
    owner_mode: str
    allow_lexical_degradation: bool
    context_policy: Mapping[str, Any]
    refs: ProfileRefs

    def __post_init__(self) -> None:
        codec.check("RuntimeProfile", self.to_json())

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "context_policy": dict(self.context_policy),
            "owner_mode": self.owner_mode,
            "retention_policy_ref": self.refs.retention_policy_ref.to_json(),
            "capability_registry_ref": self.refs.capability_registry_ref.to_json(),
            "activation_receipt_ref": self.refs.activation_receipt_ref.to_json(),
            "default_skill_policy_ref": self.refs.default_skill_policy_ref.to_json(),
            "parallel_model_per_agent": 1,
            "allow_lexical_degradation": self.allow_lexical_degradation,
            "protocol": PROTOCOL,
            "embedding_deployment_ref": self.refs.embedding_deployment_ref.to_json(),
            "catalogue_namespace_id": self.refs.catalogue_namespace_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RuntimeProfile:
        body = codec.check("RuntimeProfile", dict(value))
        refs = ProfileRefs(
            Pin.from_json(body["retention_policy_ref"]),
            Pin.from_json(body["capability_registry_ref"]),
            Pin.from_json(body["activation_receipt_ref"]),
            Pin.from_json(body["default_skill_policy_ref"]),
            Pin.from_json(body["embedding_deployment_ref"]),
            body["catalogue_namespace_id"],
        )
        return cls(
            body["profile_id"],
            body["profile_revision"],
            body["owner_mode"],
            body["allow_lexical_degradation"],
            body["context_policy"],
            refs,
        )

    @property
    def pin(self) -> Pin:
        return pin_for("profile", self.profile_id, self.profile_revision, self.to_json())

    @property
    def embedding_required(self) -> bool:
        return bool(self.context_policy["embedding_required_for_activation"])

    def creation_mode(self, *, embedding_available: bool) -> str:
        """HYBRID / LEXICAL_ONLY, or raise EMBEDDING_RESOURCE_MISSING (§6 truth table)."""

        mode = embedding_mode(
            self.embedding_required,
            self.allow_lexical_degradation,
            creating=True,
            available=embedding_available,
        )
        if mode == "REJECT_CREATION":
            raise ArpError("EMBEDDING_RESOURCE_MISSING", "profile requires embedding for activation")
        return mode


__all__ = (
    "POLICY_HARD_LIMITS",
    "ProfileRefs",
    "RuntimeProfile",
    "check_policy",
    "default_policy",
    "policy_pin",
)
