# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Strategy versions and their controlled promotion (original §28 stage four "收集 Trace
→ 离线训练或规则改进 → 生成新策略版本 → Offline Evaluation → A/B Test → 审批后上线",
§29.3 "参数应通过 Evaluation 调整"; ORCH-BUILD §11; plan D9-1' / D9-2').

A *policy* is the promotable layer over the deployment configuration: the §29.3 allocator
weights, a few scheduling knobs, routing overrides and the prompt version of each role.
It is always stored **resolved** — every whitelisted item carries an explicit value taken
from the code constants and the deployment configuration at the time — so a version id
(the hash of the resolved parameters) always means the same behaviour.  Anything outside
the whitelist (safety boundaries, budgets, the deployment policy, ablations, timeouts,
layer switches) can never be part of a policy.

Pure functions only: the registry itself is written by the Commit Service
(``orchestrator/policy_commits.py``)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from simple_harness.contracts import canonical_json

if TYPE_CHECKING:
    from ..storage.store import Store

PROMOTION_VERSION = "promotion-v1"
DEPLOYMENT_TIMELINE = "deployment"  # the sentinel event timeline of deployment-level facts
LEGACY_VERSION_ID = "policy-legacy"

WEIGHT_RANGES: dict[str, tuple[float, float]] = {
    "mission_importance": (0.0, 0.5),
    "unlock_value": (0.0, 0.5),
    "progress_signal": (0.0, 0.5),
    "uncertainty": (0.0, 0.5),
    "waiting_age": (0.0, 0.5),
    "estimated_cost": (-0.2, 0.0),
    "duplication_score": (-0.2, 0.0),
}
INT_RANGES: dict[str, tuple[int, int | None]] = {
    "candidates_per_task": (1, 3),
    "exploration_slots": (0, 2),
    "mission_concurrency": (1, None),  # the upper bound is the deployment's max_concurrency
    "manager_after_failures": (1, 5),
    "no_progress_limit": (1, 5),
    "max_manager_rounds": (1, 8),
}
FLOAT_RANGES: dict[str, tuple[float, float]] = {"aging_window_seconds": (60.0, 1800.0)}
ESCALATE_RANGE = (1, 3)
EXPANDING = ("candidates_per_task", "mission_concurrency", "exploration_slots")
MAX_WEIGHT_STEP = 0.10
MAX_INT_STEP = 1
MAX_AGING_FACTOR = 2.0
PROMOTABLE = frozenset(
    {"allocator_weights", *INT_RANGES, *FLOAT_RANGES, "routing", "prompt_versions"}
)
# named so a refusal can say *why*: core safety, budget and deployment rules (plan D9-1)
NON_PROMOTABLE = frozenset(
    {
        "permissions",
        "tool_gateway",
        "idempotency",
        "approvals",
        "deployment_policy",
        "secret_checks",
        "format_check",
        "rule_check",
        "code_test",
        "human_review",
        "budgets",
        "budget",
        "global_budget",
        "hard_cap_micros",
        "price_table",
        "planner_reserve_tokens",
        "critic_reserve_tokens",
        "attempt_reserve_tokens",
        "manager_reserve_tokens",
        "ablations",
        "knowledge_sharing",
        "dynamic_graph",
        "max_concurrency",
        "max_running_attempts",
        "lease_seconds",
        "stall_seconds",
        "test_timeout_seconds",
        "turn_deadline_seconds",
        "verification_policy",
        "enabled_connectors",
        "allowed_tools",
    }
)
PROPOSAL_STATES = (
    "PROPOSED",
    "PASSED",
    "FAILED",
    "INSUFFICIENT",
    "APPROVED",
    "REJECTED",
    "PROMOTED",
)
VERDICTS = ("PASSED", "FAILED", "INSUFFICIENT")
# plan D9-2': from → allowed next states (re-evaluation voids an approval)
PROPOSAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "PROPOSED": frozenset(VERDICTS),
    "PASSED": frozenset({*VERDICTS, "APPROVED", "REJECTED"}),
    "FAILED": frozenset(VERDICTS),
    "INSUFFICIENT": frozenset(VERDICTS),
    "APPROVED": frozenset({*VERDICTS, "PROMOTED"}),
    "REJECTED": frozenset(),
    "PROMOTED": frozenset(),
}


class PolicyError(ValueError):
    """A policy that may not exist: an item outside the whitelist or out of range."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def builtin_weights() -> dict[str, float]:
    from ..scheduling.allocator import WEIGHTS

    return {name: float(value) for name, value in WEIGHTS.items()}


def builtin_prompt_versions() -> dict[str, str]:
    from ..runtime.role_templates import ROLES

    return {name: template.prompt_version for name, template in sorted(ROLES.items())}


def resolve_params(
    config: Any, partial: Mapping[str, Any] | None = None, *, routing: Any = None
) -> dict[str, Any]:
    """The complete parameter set: the deployment's values for every whitelisted item
    (plan D9-1'), then ``partial`` on top.  An item outside the whitelist is refused."""

    resolved: dict[str, Any] = {
        "allocator_weights": builtin_weights(),
        "candidates_per_task": int(config.candidates_per_task),
        "exploration_slots": int(config.exploration_slots),
        "mission_concurrency": int(config.max_concurrency),
        "manager_after_failures": int(config.manager_after_failures),
        "no_progress_limit": int(config.no_progress_limit),
        "max_manager_rounds": int(config.max_manager_rounds),
        "aging_window_seconds": float(config.aging_window_seconds),
        "routing": {
            "escalate_after_failures": int(getattr(routing, "escalate_after_failures", 1) or 1),
            "by_task_kind": {
                str(k): str(v) for k, v in dict(getattr(routing, "by_task_kind", {}) or {}).items()
            },
        },
        "prompt_versions": builtin_prompt_versions(),
    }
    return overlay(resolved, partial or {})


def overlay(base: Mapping[str, Any], partial: Mapping[str, Any]) -> dict[str, Any]:
    refused = sorted(set(partial) - PROMOTABLE)
    if refused:
        core = [k for k in refused if k in NON_PROMOTABLE]
        raise PolicyError(
            f"not promotable: {refused}"
            + (f" ({core} are core safety / budget / deployment rules)" if core else "")
        )
    merged = {key: value for key, value in base.items()}
    for key, value in partial.items():
        if key == "allocator_weights":
            merged[key] = {**dict(base[key]), **{str(k): float(v) for k, v in dict(value).items()}}
        elif key == "routing":
            routing = dict(value)
            merged[key] = {
                "escalate_after_failures": int(
                    routing.get("escalate_after_failures", base[key]["escalate_after_failures"])
                ),
                "by_task_kind": {
                    str(k): str(v)
                    for k, v in dict(routing.get("by_task_kind", base[key]["by_task_kind"])).items()
                },
            }
        elif key == "prompt_versions":
            merged[key] = {**dict(base[key]), **{str(k): str(v) for k, v in dict(value).items()}}
        elif key in FLOAT_RANGES:
            merged[key] = float(value)
        else:
            merged[key] = int(value)
    return merged


def validate_params(
    params: Mapping[str, Any],
    *,
    base: Mapping[str, Any],
    max_concurrency: int,
    profiles: Iterable[str] | None = None,
) -> list[str]:
    """What is wrong with ``params`` as a successor of ``base``: missing or unknown
    items, and — for every item that changed — its allowed range, a routing target the
    deployment does not have, or a prompt version no template registers."""

    from ..runtime.role_templates import registered_versions

    problems: list[str] = []
    keys = set(params)
    if keys != set(PROMOTABLE):
        problems.append(
            f"a policy carries exactly {sorted(PROMOTABLE)}; "
            f"missing {sorted(PROMOTABLE - keys)}, unknown {sorted(keys - PROMOTABLE)}"
        )
        return problems
    weights = dict(params["allocator_weights"])
    if set(weights) != set(WEIGHT_RANGES):
        problems.append(f"allocator_weights must name exactly {sorted(WEIGHT_RANGES)}")
    for name, (low, high) in WEIGHT_RANGES.items():
        value = float(weights.get(name, 0.0))
        if value != float(dict(base["allocator_weights"]).get(name, value)) and not (
            low <= value <= high
        ):
            problems.append(f"allocator_weights.{name}={value} outside [{low}, {high}]")
    for name, (ilow, ihigh) in INT_RANGES.items():
        value = int(params[name])
        top = max_concurrency if name == "mission_concurrency" else ihigh
        if name == "mission_concurrency" and value > max_concurrency:
            problems.append(f"mission_concurrency={value} above the deployment's {max_concurrency}")
        elif value != int(base[name]) and not (
            ilow <= value <= (top if top is not None else value)
        ):
            problems.append(f"{name}={value} outside [{ilow}, {top}]")
    for name, (flow, fhigh) in FLOAT_RANGES.items():
        fvalue = float(params[name])
        if fvalue != float(base[name]) and not (flow <= fvalue <= fhigh):
            problems.append(f"{name}={fvalue} outside [{flow}, {fhigh}]")
    routing, old_routing = dict(params["routing"]), dict(base["routing"])
    escalate = int(routing.get("escalate_after_failures", 1))
    if escalate != int(old_routing.get("escalate_after_failures", 1)) and not (
        ESCALATE_RANGE[0] <= escalate <= ESCALATE_RANGE[1]
    ):
        problems.append(
            f"routing.escalate_after_failures={escalate} outside {list(ESCALATE_RANGE)}"
        )
    known = None if profiles is None else set(profiles)
    for kind, target in dict(routing.get("by_task_kind", {})).items():
        if (
            known is not None
            and target not in known
            and target != dict(old_routing.get("by_task_kind", {})).get(kind)
        ):
            problems.append(f"routing.by_task_kind[{kind}]={target!r} is not a deployment profile")
    registered = registered_versions()
    for role, version in dict(params["prompt_versions"]).items():
        if role not in registered:
            problems.append(f"prompt_versions names an unknown role {role!r}")
        elif version != dict(base["prompt_versions"]).get(role) and version not in registered[role]:
            problems.append(f"prompt_versions[{role}]={version!r} is not a registered template")
    return problems


def params_hash(params: Mapping[str, Any]) -> str:
    return _hash(dict(params))


def version_id(params: Mapping[str, Any]) -> str:
    """Content-addressed: the same resolved parameters are the same version (D9-2')."""

    return "policy-" + params_hash(params)[:16]


def weights_hash(weights: Mapping[str, float]) -> str:
    return _hash({k: round(float(v), 6) for k, v in weights.items()})[:16]


def proposal_id(version: str, manifest: Mapping[str, Any]) -> str:
    """One proposal = one parameter version + where it came from (D9-2')."""

    return "proposal-" + _hash({"version_id": version, "manifest": dict(manifest)})[:16]


def diff_params(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[dict[str, Any]]:
    def flat(params: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in params.items():
            if isinstance(value, Mapping):
                for sub, item in value.items():
                    if isinstance(item, Mapping):
                        for leaf, leaf_value in item.items():
                            out[f"{key}.{sub}.{leaf}"] = leaf_value
                    else:
                        out[f"{key}.{sub}"] = item
            else:
                out[key] = value
        return out

    fa, fb = flat(a), flat(b)
    return [
        {"key": key, "a": fa.get(key), "b": fb.get(key)}
        for key in sorted(set(fa) | set(fb))
        if fa.get(key) != fb.get(key)
    ]


def step_problems(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    """Plan D9-8' / D9-9: how far one promotion may move each item."""

    problems = []
    for name in WEIGHT_RANGES:
        delta = abs(
            float(dict(candidate["allocator_weights"])[name])
            - float(dict(current["allocator_weights"])[name])
        )
        if delta > MAX_WEIGHT_STEP + 1e-9:
            problems.append(f"allocator_weights.{name} moves {round(delta, 4)} > {MAX_WEIGHT_STEP}")
    for name in INT_RANGES:
        delta_int = abs(int(candidate[name]) - int(current[name]))
        if delta_int > MAX_INT_STEP:
            problems.append(f"{name} moves {delta_int} > {MAX_INT_STEP}")
    old_aging, new_aging = (
        float(current["aging_window_seconds"]),
        float(candidate["aging_window_seconds"]),
    )
    if (
        old_aging > 0
        and new_aging > 0
        and max(new_aging / old_aging, old_aging / new_aging) > (MAX_AGING_FACTOR + 1e-9)
    ):
        problems.append(f"aging_window_seconds changes more than ×{MAX_AGING_FACTOR}")
    old_routing, new_routing = dict(current["routing"]), dict(candidate["routing"])
    if (
        abs(
            int(new_routing["escalate_after_failures"])
            - int(old_routing["escalate_after_failures"])
        )
        > MAX_INT_STEP
    ):
        problems.append(f"routing.escalate_after_failures moves more than {MAX_INT_STEP}")
    old_kinds, new_kinds = dict(old_routing["by_task_kind"]), dict(new_routing["by_task_kind"])
    changed_kinds = [
        k for k in set(old_kinds) | set(new_kinds) if old_kinds.get(k) != new_kinds.get(k)
    ]
    if len(changed_kinds) > 1:
        problems.append(f"routing changes {len(changed_kinds)} task kinds at once (at most 1)")
    old_prompts, new_prompts = dict(current["prompt_versions"]), dict(candidate["prompt_versions"])
    changed_roles = [
        r for r in set(old_prompts) | set(new_prompts) if old_prompts.get(r) != new_prompts.get(r)
    ]
    if len(changed_roles) > 1:
        problems.append(f"prompt_versions changes {len(changed_roles)} roles at once (at most 1)")
    return problems


def expands(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    """The items a promotion would raise that widen concurrency (refused under pressure)."""

    return [name for name in EXPANDING if int(candidate[name]) > int(current[name])]


def code_versions() -> dict[str, str]:
    from ..version import __version__ as orchestrator_version

    try:
        from simple_harness.version import __version__ as sdk_version
    except ImportError:  # pragma: no cover - the SDK always ships its version module
        sdk_version = "unknown"
    return {"agent_orchestrator": str(orchestrator_version), "simple_harness": str(sdk_version)}


def interpreter_versions() -> dict[str, str]:
    """The code that reads a policy (plan D9-1' / D9-4'): a version records them so a
    Mission resumed under other code can say so instead of changing silently."""

    from ..context.context_builder import CONTEXT_BUILDER_VERSION
    from ..context.retrieval import RETRIEVAL_VERSION
    from ..runtime.model_router import ROUTER_VERSION
    from ..scheduling.allocator import ALLOCATOR_VERSION
    from ..verification.verifier_router import VERIFIER_VERSION

    return {
        "allocator": ALLOCATOR_VERSION,
        "model_router": ROUTER_VERSION,
        "retrieval": RETRIEVAL_VERSION,
        "context_builder": CONTEXT_BUILDER_VERSION,
        "verifier": VERIFIER_VERSION,
        "promotion": PROMOTION_VERSION,
        **code_versions(),
    }


def task_identity_hash(spec: Mapping[str, Any]) -> str:
    """The task a charter asks for, without who asked and under which key (plan D9-6'):
    the same task submitted under another tenant or idempotency key has the same
    identity — the leakage check compares these, not ``spec_hash``."""

    body = {k: v for k, v in dict(spec).items() if k not in {"tenant_id", "idempotency_key"}}
    return _hash(body)


def spec_task_identity(spec: Any) -> str:
    """The task identity of a MissionSpec — the same fields the learner reads from a
    Mission (``governance/learning.task_identity``), so both sides of the leakage check
    hash the same thing (plan D9-6')."""

    return task_identity_hash(
        {
            "goal": spec.goal,
            "success_criteria": list(spec.success_criteria),
            "allowed_tools": list(spec.allowed_tools),
            "task_kind": str(spec.task_kind or "code"),
            "workspace_seed": dict(spec.workspace_seed or {}),
        }
    )


# ------------------------------------------------------------------ registry projection
def registry_projection(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold the deployment timeline into registry states (step 8 lesson: every event
    that changes formal state is projected, and the projection is compared with the
    tables)."""

    versions: dict[str, str] = {}
    proposals: dict[str, str] = {}
    active: str | None = None
    for event in sorted(events, key=lambda e: int(e.get("seq") or 0)):
        kind, p = str(event.get("type")), dict(event.get("payload") or {})
        if kind == "PolicySeeded":
            active = str(p["version_id"])
            versions[active] = "ACTIVE"
        elif kind == "PolicyProposed":
            versions.setdefault(str(p["version_id"]), "NEVER_ACTIVE")
            proposals[str(p["proposal_id"])] = "PROPOSED"
        elif kind == "PolicyEvaluated":
            proposals[str(p["proposal_id"])] = str(p["verdict"])
        elif kind == "PolicyApproved":
            proposals[str(p["proposal_id"])] = "APPROVED"
        elif kind == "PolicyRejected":
            proposals[str(p["proposal_id"])] = "REJECTED"
        elif kind == "PolicyPromoted":
            if active is not None:
                versions[active] = "RETIRED"
            active = str(p["version_id"])
            versions[active] = "ACTIVE"
            proposals[str(p["proposal_id"])] = "PROMOTED"
        elif kind == "PolicyRolledBack":
            versions[str(p["from_version_id"])] = "ROLLED_BACK"
            active = str(p["version_id"])
            versions[active] = "ACTIVE"
    return {"versions": versions, "proposals": proposals, "active": active}


def registry_consistency(store: Store) -> list[dict[str, Any]]:
    """Every difference between the folded deployment timeline and the registry tables."""

    events = [e.to_json() | {"seq": e.seq} for e in store.iter_events(DEPLOYMENT_TIMELINE)]
    folded = registry_projection(events)
    problems: list[dict[str, Any]] = []
    for version in store.list_policy_versions():
        vid, status = version["version_id"], version["status"]
        if status == "LEGACY":
            continue
        if folded["versions"].get(vid) != status:
            problems.append(
                {
                    "object": "version",
                    "id": vid,
                    "table": status,
                    "events": folded["versions"].get(vid),
                }
            )
    for proposal in store.list_policy_proposals():
        pid, state = proposal["proposal_id"], proposal["state"]
        if folded["proposals"].get(pid) != state:
            problems.append(
                {
                    "object": "proposal",
                    "id": pid,
                    "table": state,
                    "events": folded["proposals"].get(pid),
                }
            )
    active = store.active_policy()
    if (None if active is None else active["version_id"]) != folded["active"]:
        problems.append(
            {
                "object": "active",
                "table": active and active["version_id"],
                "events": folded["active"],
            }
        )
    return problems


__all__ = (
    "DEPLOYMENT_TIMELINE",
    "EXPANDING",
    "LEGACY_VERSION_ID",
    "NON_PROMOTABLE",
    "PROMOTABLE",
    "PROMOTION_VERSION",
    "PROPOSAL_STATES",
    "PROPOSAL_TRANSITIONS",
    "PolicyError",
    "code_versions",
    "diff_params",
    "expands",
    "interpreter_versions",
    "overlay",
    "params_hash",
    "proposal_id",
    "registry_consistency",
    "registry_projection",
    "resolve_params",
    "step_problems",
    "task_identity_hash",
    "validate_params",
    "version_id",
    "weights_hash",
)
