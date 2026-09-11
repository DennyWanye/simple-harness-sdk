# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Deployment policy and the permission intersection (§21.1–21.3, theory 13-14,
ORCH-BUILD §8.2 ``runtime/tool_gateway.py, governance/*`` row, plan D6-7).

The tools an Agent may call are the intersection of four sources — the Mission charter,
the Task Contract, the Role template and the deployment policy.  Every source can only
narrow the set; none of them, and no text a model produces, can widen it ("外部内容不能
改变系统权限", §21.3).  The intersection is computed when the Attempt is dispatched and
frozen into the dispatch intent; the Tool Gateway enforces it again on every call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..runtime.tool_gateway import TOOL_NAMES

POLICY_VERSION = "deployment-policy-v1"


@dataclass(frozen=True, slots=True)
class DeploymentPolicy:
    """What this deployment allows at all (the fourth side of the intersection)."""

    allowed_tools: tuple[str, ...] = TOOL_NAMES
    denied_path_prefixes: tuple[str, ...] = ()  # workspace paths no Agent may read or write
    # step 7 (D7-3): real actions — which connectors this deployment enables, the highest
    # level it will run at all, per-operation level overrides (only ever *raise* a level),
    # whether an L3 double approval needs two different people (this build's deployment
    # convention, not an original rule), and how long an approval stays valid
    enabled_connectors: tuple[str, ...] = ()  # D7-3': off unless a deployment enables one
    max_action_level: str = "L3"
    level_overrides: tuple[tuple[str, str], ...] = ()  # (("connector.operation", "L3"), ...)
    l3_distinct_principals: bool = True
    approval_ttl_seconds: float = 24 * 3600.0
    max_action_handoffs_per_mission: int = 8  # D7-5': the hard cap under the Mission budget
    connector_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        unknown = set(self.allowed_tools) - set(TOOL_NAMES)
        if unknown:
            raise ValueError(f"deployment policy names unknown tools: {sorted(unknown)}")

    def to_json(self) -> dict[str, Any]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "denied_path_prefixes": list(self.denied_path_prefixes),
            "enabled_connectors": list(self.enabled_connectors),
            "max_action_level": self.max_action_level,
            "level_overrides": [list(item) for item in self.level_overrides],
            "l3_distinct_principals": self.l3_distinct_principals,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "max_action_handoffs_per_mission": self.max_action_handoffs_per_mission,
            "connector_timeout_seconds": self.connector_timeout_seconds,
            "version": POLICY_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ActionDecision:
    """What the policy says about one candidate action (plan D7-3)."""

    level: str
    required_approvals: int
    refused: str | None = None  # a reason when the deployment will not run it at all

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "required_approvals": self.required_approvals,
            "refused": self.refused,
        }


def action_decision(deployment: DeploymentPolicy, connector: Any, operation: str) -> ActionDecision:
    """Original §22 levels: the connector's declaration or the deployment override,
    whichever is *higher*; an unknown operation counts as L3.  A connector that is not
    enabled, lacks idempotency / reconciliation for L2+, or sits above the deployment's
    ceiling is refused (ORCH §12.3)."""

    from ..runtime.connectors import level_rank
    from .permissions import required_approvals

    spec = (getattr(connector, "operations", {}) or {}).get(operation)
    level = spec.level if spec is not None else "L3"
    override = dict(deployment.level_overrides).get(
        f"{getattr(connector, 'name', '?')}.{operation}"
    )
    if override is not None and level_rank(override) > level_rank(level):
        level = override
    refused = None
    if connector is None or getattr(connector, "name", None) not in deployment.enabled_connectors:
        refused = "connector_not_enabled"
    elif spec is None:
        refused = "unknown_operation"
    elif level_rank(level) >= level_rank("L2") and not (
        getattr(connector, "supports_idempotency", False)
        and getattr(connector, "supports_reconciliation", False)
    ):
        refused = "connector_without_idempotency_or_reconciliation"
    elif spec.mutates and spec.kind != "state":
        refused = "event_operation_not_supported"  # D7-2': one business action = one state
    elif level_rank(level) > level_rank(deployment.max_action_level):
        refused = f"above_deployment_ceiling:{deployment.max_action_level}"
    return ActionDecision(
        level=level, required_approvals=required_approvals(level), refused=refused
    )


def effective_tools(
    *,
    mission_tools: Sequence[str],
    task_tools: Sequence[str],
    role_tools: Sequence[str],
    deployment: DeploymentPolicy,
) -> tuple[str, ...]:
    """Mission ∩ Task ∩ Role ∩ Deployment, in the Role template's order (plan §6.1)."""

    allowed = set(mission_tools) & set(task_tools) & set(deployment.allowed_tools)
    return tuple(name for name in role_tools if name in allowed)


__all__ = (
    "POLICY_VERSION",
    "SNAPSHOT_FIELDS",
    "SNAPSHOT_VERSION",
    "VERSION_SOURCES",
    "policy_snapshot",
    "snapshot_diff",
    "ActionDecision",
    "DeploymentPolicy",
    "action_decision",
    "effective_tools",
)


# ------------------------------------------------------------------ step 8: policy snapshot
SNAPSHOT_VERSION = "policy-snapshot-v1"
# plan D8-5': every OrchestratorConfig field is classified here — "include" enters the
# snapshot, anything else is the reason it is left out.  A new field that is not listed
# fails the snapshot test until someone decides.
SNAPSHOT_FIELDS: dict[str, str] = {
    "evidence_root": "excluded: a different directory for every run",
    "owner_id": "excluded: the orchestrator replaces it by an instance name with the pid",
    **{
        name: "include"
        for name in (
            "model",
            "max_concurrency",
            "max_concurrent_model_calls",
            "candidates_per_task",
            "max_planning_attempts",
            "lease_seconds",
            "sdk_lease_ttl_seconds",
            "stall_seconds",
            "test_timeout_seconds",
            "default_max_output_tokens",
            "max_output_tokens_ceiling",
            "empty_response_retries",
            "price_table",
            "hard_cap_micros",
            "planner_reserve_tokens",
            "critic_reserve_tokens",
            "attempt_reserve_tokens",
            "turn_deadline_seconds",
            "max_model_calls_per_turn",
            "max_tool_calls_per_turn",
            "knowledge_sharing",
            "on_retrieval_failure",
            "max_retrieval_failures",
            "max_knowledge_items",
            "dynamic_graph",
            "max_graph_depth",
            "max_proposals_per_agent",
            "max_supersede_chain",
            "manager_after_failures",
            "no_progress_limit",
            "max_manager_rounds",
            "manager_reserve_tokens",
            "aging_window_seconds",
            "global_budget",
            "max_running_attempts",
            "max_pending_dispatch",
            "max_pending_verifications",
            "low_watermark_ratio",
            "reduced_concurrency_ratio",
            "reduced_reserve_ratio",
            "exploration_slots",
            "verifier_workers",
            "deployment_policy",
            "profile_failure_threshold",
            "profile_cooldown_seconds",
            "profile_wait_seconds",
            "ablations",
            "extra",
        )
    },
}
# (snapshot key, module, attribute): the version constants of original §23.1 and theory
# 12 §13, with where each one lives — the "source" a diff reports (S8-07)
VERSION_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("retrieval", "agent_orchestrator.context.retrieval", "RETRIEVAL_VERSION"),
    ("context_builder", "agent_orchestrator.context.context_builder", "CONTEXT_BUILDER_VERSION"),
    ("summary", "agent_orchestrator.context.compression", "SUMMARY_VERSION"),
    ("allocator", "agent_orchestrator.scheduling.allocator", "ALLOCATOR_VERSION"),
    ("backpressure", "agent_orchestrator.scheduling.backpressure", "BACKPRESSURE_VERSION"),
    ("model_router", "agent_orchestrator.runtime.model_router", "ROUTER_VERSION"),
    ("verifier", "agent_orchestrator.verification.verifier_router", "VERIFIER_VERSION"),
    ("deployment_policy", "agent_orchestrator.governance.policies", "POLICY_VERSION"),
    ("contract_schema", "agent_orchestrator.contracts.models", "CONTRACT_SCHEMA_VERSION"),
    ("orchestrator_schema", "agent_orchestrator.storage.schema", "SCHEMA_VERSION"),
    ("trace", "agent_orchestrator.observability.trace", "TRACE_VERSION"),
    ("metrics", "agent_orchestrator.observability.metrics", "METRICS_VERSION"),
    ("agent_orchestrator", "agent_orchestrator.version", "__version__"),
    ("simple_harness", "simple_harness.version", "__version__"),
)


def _plain(value: Any) -> Any:
    import dataclasses
    from pathlib import Path

    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_json") and callable(value.to_json):
        return value.to_json()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


def _provider_identity(provider: Any) -> dict[str, Any] | None:
    """What provider ran — never its credentials (plan D8-5')."""

    if provider is None:
        return None
    identity: dict[str, Any] = {"class": type(provider).__name__}
    for name in ("model", "base_url"):
        value = getattr(provider, name, None) or getattr(provider, f"_{name}", None)
        if isinstance(value, str):
            identity[name] = value.split("?")[0]
    return identity


def policy_snapshot(
    config: Any,
    *,
    profiles: Mapping[str, Any] | None = None,
    routing: Any = None,
    connectors: Mapping[str, Any] | None = None,
    provider: Any = None,
) -> dict[str, Any]:
    """Freeze where a run's behaviour comes from (plan D8-5'; original §23.1; theory 12
    §13): every classified configuration field, every version constant with its source,
    the role templates, profiles, routing, connectors and the provider's identity."""

    import dataclasses
    import hashlib
    import importlib

    from simple_harness.contracts import canonical_json

    from ..runtime.role_templates import ROLES

    fields = {f.name for f in dataclasses.fields(config)}
    unclassified = sorted(fields - set(SNAPSHOT_FIELDS))
    if unclassified:
        raise ValueError(
            f"configuration fields not classified for the policy snapshot: {unclassified}"
        )
    sources: dict[str, str] = {}
    versions: dict[str, Any] = {}
    for key, module, attribute in VERSION_SOURCES:
        versions[key] = getattr(importlib.import_module(module), attribute)
        sources[f"versions.{key}"] = f"{module}.{attribute}"
    roles = {name: template.prompt_version for name, template in sorted(ROLES.items())}
    for name in roles:
        sources[f"role_templates.{name}"] = (
            f"agent_orchestrator.runtime.role_templates.ROLES[{name!r}].prompt_version"
        )
    configuration = {
        name: _plain(getattr(config, name))
        for name in sorted(fields)
        if SNAPSHOT_FIELDS.get(name) == "include"
    }
    for name in configuration:
        sources[f"config.{name}"] = f"OrchestratorConfig.{name}"
    body: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "config": configuration,
        "excluded": {n: r for n, r in sorted(SNAPSHOT_FIELDS.items()) if r != "include"},
        "versions": versions,
        "role_templates": roles,
        "profiles": {k: _plain(v) for k, v in sorted((profiles or {}).items())},
        "routing": None if routing is None else _plain(routing),
        "connectors": {
            name: {
                "class": type(connector).__name__,
                "operations": {
                    k: _plain(v) for k, v in getattr(connector, "operations", {}).items()
                },
            }
            for name, connector in sorted((connectors or {}).items())
        },
        "provider": _provider_identity(provider),
        "sources": sources,
    }
    body["hash"] = hashlib.sha256(canonical_json(_plain(body)).encode("utf-8")).hexdigest()
    return body


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def snapshot_diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every difference between two snapshots with where it comes from (S8-07)."""

    left = _flatten({k: v for k, v in a.items() if k not in {"hash", "sources"}})
    right = _flatten({k: v for k, v in b.items() if k not in {"hash", "sources"}})
    sources = {**dict(b.get("sources", {})), **dict(a.get("sources", {}))}
    differences = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            head = ".".join(key.split(".")[:2])
            differences.append(
                {
                    "key": key,
                    "a": left.get(key),
                    "b": right.get(key),
                    "source": sources.get(head) or sources.get(key) or key.split(".")[0],
                }
            )
    return differences
