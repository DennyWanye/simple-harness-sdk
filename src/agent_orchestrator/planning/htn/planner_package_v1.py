# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""The V1.4 §48 planner package.

``planner_package`` is deliberately kept as the legacy collector.  This module is
the stable, nine-view envelope used by new callers; the assembler accepts the legacy
mapping so rollout does not change the bytes of an existing request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, ClassVar

from ...contracts.models import ContractError

PACKAGE_VERSION = "planner-package-hierarchical-v9"
PROTOCOL_VERSION = "planner-package-v1"
MAX_PACKAGE_BYTES = 96 * 1024
MAX_METHODS_PER_SIGNATURE = 12
MAX_FACTS = 24
MAX_ACCEPTED_RESULTS = 24
MAX_FAILURES = 16
MAX_VISIBLE_REFS = 128

_VIEW_NAMES = (
    "goals", "obligations", "plans", "methods", "facts", "accepted_results",
    "failures", "capabilities", "planning_budgets",
)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, set):
        return sorted(_plain(v) for v in value)
    return value


class PlannerPackageCodecError(ContractError):
    """A malformed or over-sized H2 package."""


class _View:
    _required: ClassVar[frozenset[str]] = frozenset()

    def to_json(self) -> dict[str, Any]:
        return {item.name: _plain(getattr(self, item.name)) for item in fields(self)}  # type: ignore[arg-type]

    @classmethod
    def from_json(cls, value: Mapping[str, Any], path: str = "view"):
        if not isinstance(value, Mapping):
            raise PlannerPackageCodecError(f"{path} must be an object")
        names = {item.name for item in fields(cls)}  # type: ignore[arg-type]
        unknown = sorted(set(value) - names)
        missing = sorted(cls._required - set(value))
        if unknown or missing:
            raise PlannerPackageCodecError(
                f"{path} fields invalid; unknown={unknown}, missing={missing}"
            )
        data = {name: value[name] for name in names}
        return cls(**data)


@dataclass(frozen=True, slots=True)
class GoalView(_View):
    subject_key: str
    occurrence_id: str
    task_id: str
    obligation_id: str
    signature: Mapping[str, Any]
    statement: str
    params: Mapping[str, Any]
    requirement_refs: tuple[Any, ...]
    contract_revision: int
    requiredness: str
    _required: ClassVar[frozenset[str]] = frozenset(
        {"subject_key", "occurrence_id", "task_id", "obligation_id", "signature",
         "statement", "params", "requirement_refs", "contract_revision", "requiredness"}
    )


@dataclass(frozen=True, slots=True)
class ObligationView(_View):
    parent: str | None
    relation: str
    demand: Mapping[str, Any]
    fuel: Mapping[str, Any]
    failures: tuple[Any, ...]
    attempts: int
    budget: Mapping[str, Any]
    _required: ClassVar[frozenset[str]] = frozenset(
        {"parent", "relation", "demand", "fuel", "failures", "attempts", "budget"}
    )


@dataclass(frozen=True, slots=True)
class PlanView(_View):
    plan_revision: int
    adopted_methods: tuple[Any, ...]
    open_compounds: tuple[Any, ...]
    pending_primitives: tuple[Any, ...]
    order_summary: Mapping[str, Any]
    data_summary: Mapping[str, Any]
    rejected_refinements: tuple[Any, ...]
    _required: ClassVar[frozenset[str]] = frozenset(
        {"plan_revision", "adopted_methods", "open_compounds", "pending_primitives",
         "order_summary", "data_summary", "rejected_refinements"}
    )


@dataclass(frozen=True, slots=True)
class MethodView(_View):
    method_ref: Mapping[str, Any]
    goal_signature: Mapping[str, Any]
    registry_status: str
    applicability: Mapping[str, Any]
    precondition_summary: Mapping[str, Any]
    capabilities: tuple[str, ...]
    schema: Mapping[str, Any]
    steps: tuple[Any, ...]
    rejected_reasons: tuple[Any, ...]
    _required: ClassVar[frozenset[str]] = frozenset(
        {"method_ref", "goal_signature", "registry_status", "applicability",
         "precondition_summary", "capabilities", "schema", "steps", "rejected_reasons"}
    )


@dataclass(frozen=True, slots=True)
class FactView(_View):
    observation_ref: Mapping[str, Any]
    proposition_key: str
    polarity: bool
    availability: str
    truth: str
    coverage: str
    observer: str | None
    times: Mapping[str, Any]
    _required: ClassVar[frozenset[str]] = frozenset(
        {"observation_ref", "proposition_key", "polarity", "availability", "truth",
         "coverage", "observer", "times"}
    )


@dataclass(frozen=True, slots=True)
class AcceptedResultView(_View):
    acceptance_ref: Mapping[str, Any]
    producer_occurrence: str
    ports: tuple[Any, ...]
    artifacts: tuple[Any, ...]
    currentness: str
    support_revision: int
    permitted_uses: tuple[str, ...]
    _required: ClassVar[frozenset[str]] = frozenset(
        {"acceptance_ref", "producer_occurrence", "ports", "artifacts", "currentness",
         "support_revision", "permitted_uses"}
    )


@dataclass(frozen=True, slots=True)
class FailureView(_View):
    source: str
    reason: str
    attempt_review_ref: Mapping[str, Any]
    repeat_count: int
    last_seen: str | None
    findings: tuple[Any, ...]
    _required: ClassVar[frozenset[str]] = frozenset(
        {"source", "reason", "attempt_review_ref", "repeat_count", "last_seen", "findings"}
    )


@dataclass(frozen=True, slots=True)
class CapabilityView(_View):
    capability: str
    registered: bool
    configured: bool
    reachable: bool
    healthy: bool
    authorized: bool
    compatible: bool
    _required: ClassVar[frozenset[str]] = frozenset(
        {"capability", "registered", "configured", "reachable", "healthy", "authorized", "compatible"}
    )


@dataclass(frozen=True, slots=True)
class PlanningBudgetView(_View):
    planning_remaining: int
    synthesis_remaining: int
    root_repair_remaining: int
    max_method_candidates: int
    max_new_steps: int
    max_repair_actions: int
    token_budget: int
    _required: ClassVar[frozenset[str]] = frozenset(
        {"planning_remaining", "synthesis_remaining", "root_repair_remaining",
         "max_method_candidates", "max_new_steps", "max_repair_actions", "token_budget"}
    )


_VIEW_TYPES: dict[str, type[_View]] = {
    "goals": GoalView,
    "obligations": ObligationView,
    "plans": PlanView,
    "methods": MethodView,
    "facts": FactView,
    "accepted_results": AcceptedResultView,
    "failures": FailureView,
    "capabilities": CapabilityView,
    "planning_budgets": PlanningBudgetView,
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PlannerPackageV1:
    goals: tuple[GoalView, ...]
    obligations: tuple[ObligationView, ...]
    plans: tuple[PlanView, ...]
    methods: tuple[MethodView, ...]
    facts: tuple[FactView, ...]
    accepted_results: tuple[AcceptedResultView, ...]
    failures: tuple[FailureView, ...]
    capabilities: tuple[CapabilityView, ...]
    planning_budgets: tuple[PlanningBudgetView, ...]
    visible_refs: tuple[Mapping[str, Any], ...] = ()
    truncated: bool = False
    omitted_counts: Mapping[str, int] | None = None
    package_version: str = PACKAGE_VERSION
    protocol: str = PROTOCOL_VERSION
    mission: Mapping[str, Any] | None = None

    def views_json(self) -> dict[str, list[dict[str, Any]]]:
        return {name: [item.to_json() for item in getattr(self, name)] for name in _VIEW_NAMES}

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "package_version": self.package_version,
            "protocol": self.protocol,
            "views": self.views_json(),
            "visible_refs": _plain(self.visible_refs),
            "truncated": bool(self.truncated),
            "omitted_counts": dict(self.omitted_counts or {}),
        }
        if self.mission is not None:
            result["mission"] = _plain(self.mission)
        return result

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_json())

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> PlannerPackageV1:
        if not isinstance(value, Mapping):
            raise PlannerPackageCodecError("planner package must be an object")
        allowed = {"package_version", "protocol", "views", "visible_refs", "truncated", "omitted_counts", "mission"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise PlannerPackageCodecError(f"unknown planner package fields: {unknown}")
        missing_top = sorted({"package_version", "protocol", "views", "visible_refs", "truncated", "omitted_counts"} - set(value))
        if missing_top:
            raise PlannerPackageCodecError(f"missing planner package fields: {missing_top}")
        if value.get("package_version") != PACKAGE_VERSION or value.get("protocol") != PROTOCOL_VERSION:
            raise PlannerPackageCodecError("unsupported planner package version/protocol")
        views = value.get("views")
        if not isinstance(views, Mapping) or set(views) != set(_VIEW_NAMES):
            raise PlannerPackageCodecError("views must contain exactly the nine H2 view names")
        parsed: dict[str, tuple[Any, ...]] = {}
        for name in _VIEW_NAMES:
            rows = views[name]
            if not isinstance(rows, list):
                raise PlannerPackageCodecError(f"views.{name} must be an array")
            typ = _VIEW_TYPES[name]
            parsed[name] = tuple(typ.from_json(row, f"views.{name}[{idx}]") for idx, row in enumerate(rows))
        if len(parsed["facts"]) > MAX_FACTS or len(parsed["accepted_results"]) > MAX_ACCEPTED_RESULTS or len(parsed["failures"]) > MAX_FAILURES:
            raise PlannerPackageCodecError("one or more H2 view limits are exceeded")
        method_counts: dict[str, int] = {}
        for row in parsed["methods"]:
            key = str(row.goal_signature.get("id", ""))
            method_counts[key] = method_counts.get(key, 0) + 1
            if method_counts[key] > MAX_METHODS_PER_SIGNATURE:
                raise PlannerPackageCodecError("methods exceed 12 per goal signature")
        refs = value.get("visible_refs", [])
        if not isinstance(refs, list) or len(refs) > MAX_VISIBLE_REFS:
            raise PlannerPackageCodecError("visible_refs exceeds 128 or is not an array")
        omitted = value.get("omitted_counts", {})
        invalid_omitted = not isinstance(omitted, Mapping) or any(
            type(v) is not int or v < 0 for v in omitted.values()
        )
        if invalid_omitted:
            raise PlannerPackageCodecError("omitted_counts must contain non-negative integers")
        if not isinstance(value.get("truncated"), bool):
            raise PlannerPackageCodecError("truncated must be boolean")
        result = cls(**parsed, visible_refs=tuple(refs), truncated=value["truncated"], omitted_counts=dict(omitted), mission=value.get("mission"))  # type: ignore[arg-type]
        if len(result.canonical_bytes()) > MAX_PACKAGE_BYTES:
            raise PlannerPackageCodecError("planner package exceeds 96 KiB")
        return result


def _goal(row: Mapping[str, Any], idx: int) -> GoalView:
    signature = row.get("signature") or {"id": row.get("goal_signature_id", "")}
    return GoalView(
        subject_key=str(row.get("subject_key", f"occurrence:{row.get('occurrence_id', idx)}")),
        occurrence_id=str(row.get("occurrence_id", "")), task_id=str(row.get("task_id", row.get("goal_id", ""))),
        obligation_id=str(row.get("obligation_id", "")), signature=dict(signature), statement=str(row.get("statement", "")),
        params=dict(row.get("params", row.get("typed_parameters", {})) or {}),
        requirement_refs=tuple(row.get("requirement_refs", ())), contract_revision=int(row.get("contract_revision", 0)),
        requiredness=str(row.get("requiredness", "REQUIRED")),
    )


def collect_planner_views(legacy: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
    """Collect nine views from the existing hierarchical package mapping."""
    plan = legacy.get("plan", {}) if isinstance(legacy.get("plan", {}), Mapping) else {}
    goals = tuple(_goal(row, idx) for idx, row in enumerate(plan.get("open_compound_goals", ())) if isinstance(row, Mapping))
    obligations = tuple(
        ObligationView(parent=None, relation="required", demand={"obligation_id": str(item)}, fuel={}, failures=(), attempts=0, budget={})
        for item in plan.get("required_obligations", ())
    )
    plan_view = PlanView(
        plan_revision=int(plan.get("plan_revision", 0)), adopted_methods=tuple(plan.get("adopted_methods", ())),
        open_compounds=tuple(plan.get("open_compound_goals", ())), pending_primitives=tuple(plan.get("committed_primitives", ())),
        order_summary={"root_occurrences": tuple(plan.get("root_occurrences", ()))}, data_summary={},
        rejected_refinements=tuple(legacy.get("rejected_refinements", ())),
    )
    methods = tuple(
        MethodView(
            method_ref=dict(row.get("refine_method_ref", row.get("method_ref", {})) or {}),
            goal_signature={"id": str(row.get("goal_signature_id", ""))}, registry_status=str(row.get("registry_status", "UNKNOWN")),
            applicability={}, precondition_summary={}, capabilities=tuple(str(x) for x in row.get("required_capabilities", ())),
            schema={"parameter_schema_ref": row.get("parameter_schema_ref")}, steps=tuple(row.get("steps", ())),
            rejected_reasons=tuple(x for x in ("root_review",) if row.get("rejected_by_root_review")) + tuple(x for x in ("read_only_leaf",) if row.get("rejected_by_read_only_leaf")),
        ) for row in legacy.get("method_library", ()) if isinstance(row, Mapping)
    )
    facts = tuple(
        FactView(observation_ref=dict(row.get("read_set_entry", {})), proposition_key=str(row.get("proposition_key", "")), polarity=bool(row.get("polarity", False)),
                 availability="recorded", truth="UNKNOWN", coverage=str(row.get("coverage", "")), observer=row.get("observer_id"),
                 times={"observed_at_ms": int(row.get("observed_at_ms", 0))})
        for row in legacy.get("facts", ()) if isinstance(row, Mapping)
    )
    accepted = tuple(
        AcceptedResultView(acceptance_ref=dict(row.get("acceptance_ref", {})), producer_occurrence=str(row.get("producer_occurrence", "")), ports=tuple(row.get("ports", ())),
                          artifacts=tuple(row.get("artifacts", ())), currentness=str(row.get("currentness", "UNKNOWN")), support_revision=int(row.get("support_revision", 0)), permitted_uses=tuple(row.get("permitted_uses", ())))
        for row in legacy.get("accepted_results", legacy.get("accepted_outputs", ())) if isinstance(row, Mapping)
    )
    failures = tuple(
        FailureView(source=str(row.get("source", "planning")), reason=str(row.get("reason", row.get("status", ""))), attempt_review_ref=dict(row.get("attempt_review_ref", {})), repeat_count=int(row.get("repeat_count", 1)), last_seen=row.get("last_seen"), findings=tuple(row.get("findings", ())))
        for row in (*legacy.get("planning_rejected", ()), *legacy.get("failures", ()), *legacy.get("rejected_refinements", ())) if isinstance(row, Mapping)
    )
    operators = legacy.get("operators", {}) if isinstance(legacy.get("operators", {}), Mapping) else {}
    available = {str(x) for x in operators.get("available_capabilities", ())}
    unavailable = {str(x) for x in operators.get("unavailable_capabilities", ())}
    capabilities = tuple(CapabilityView(x, x in available or x not in unavailable, x in available, x in available, x in available, True, x in available) for x in sorted(available | unavailable))
    budget = (legacy.get("mission", {}) or {}).get("budget", {}) if isinstance(legacy.get("mission", {}), Mapping) else {}
    budgets = (PlanningBudgetView(int(budget.get("planning_remaining", budget.get("remaining", 0))), int(budget.get("synthesis_remaining", 0)), int(budget.get("root_repair_remaining", 0)), MAX_METHODS_PER_SIGNATURE, int(budget.get("max_new_steps", 0)), int(budget.get("max_repair_actions", 0)), int(budget.get("token_budget", budget.get("max_tokens", 0)))),)
    return {"goals": goals, "obligations": obligations, "plans": (plan_view,), "methods": methods, "facts": facts, "accepted_results": accepted, "failures": failures, "capabilities": capabilities, "planning_budgets": budgets}


class PlannerPackageCollectorV1:
    """Named collector facade for callers that keep collection separate from assembly."""

    @staticmethod
    def collect(legacy: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
        return collect_planner_views(legacy)


class PlannerPackageCodec:
    """Stateless JSON codec facade used by restart/replay callers."""

    @staticmethod
    def encode(package: PlannerPackageV1) -> bytes:
        if not isinstance(package, PlannerPackageV1):
            raise PlannerPackageCodecError("encode expects PlannerPackageV1")
        data = package.canonical_bytes()
        if len(data) > MAX_PACKAGE_BYTES:
            raise PlannerPackageCodecError("planner package exceeds 96 KiB")
        return data

    @staticmethod
    def decode(data: bytes | bytearray | str) -> PlannerPackageV1:
        try:
            raw = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise PlannerPackageCodecError(f"invalid planner package JSON: {exc}") from exc
        return PlannerPackageV1.from_json(value)


def _cap(rows: Sequence[Any], limit: int, name: str, omitted: dict[str, int]) -> tuple[Any, ...]:
    if len(rows) <= limit:
        return tuple(rows)
    omitted[name] = len(rows) - limit
    return tuple(rows[:limit])


def _cap_methods(rows: Sequence[MethodView], omitted: dict[str, int]) -> tuple[MethodView, ...]:
    """Apply §48's 12-method bound independently for every goal signature."""
    kept: list[MethodView] = []
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.goal_signature.get("id", ""))
        if counts.get(key, 0) < MAX_METHODS_PER_SIGNATURE:
            kept.append(row)
            counts[key] = counts.get(key, 0) + 1
        else:
            omitted["methods"] = omitted.get("methods", 0) + 1
    return tuple(kept)


class PlannerPackageAssemblerV1:
    """Deterministically assemble and bound the formal package."""

    @classmethod
    def assemble(cls, legacy: Mapping[str, Any], *, visible_refs: Sequence[Mapping[str, Any]] | None = None, views: Mapping[str, Sequence[Any]] | None = None) -> PlannerPackageV1:
        collected = dict(views) if views is not None else collect_planner_views(legacy)
        if set(collected) != set(_VIEW_NAMES):
            raise PlannerPackageCodecError("assembler requires all nine views")
        omitted: dict[str, int] = {}
        limits = {"facts": MAX_FACTS, "accepted_results": MAX_ACCEPTED_RESULTS, "failures": MAX_FAILURES}
        selected = {name: _cap(collected[name], limits.get(name, len(collected[name])), name, omitted) for name in _VIEW_NAMES}
        selected["methods"] = _cap_methods(collected["methods"], omitted)
        refs = list(visible_refs if visible_refs is not None else legacy.get("visible_refs", ()))
        if len(refs) > MAX_VISIBLE_REFS:
            raise PlannerPackageCodecError("required visible references exceed 128; narrow the planning subject")
        def make() -> PlannerPackageV1:
            return PlannerPackageV1(**selected, visible_refs=tuple(refs), truncated=bool(omitted), omitted_counts=dict(omitted), mission=legacy.get("mission"))  # type: ignore[arg-type]

        package = make()
        # Size pressure drops optional evidence from the deterministic tail. Goals,
        # plan revision and budgets are mandatory and are never silently removed.
        for name in ("facts", "accepted_results", "failures", "methods"):
            while len(package.canonical_bytes()) > MAX_PACKAGE_BYTES and selected[name]:
                selected[name] = tuple(selected[name][:-1])
                omitted[name] = omitted.get(name, 0) + 1
                package = make()
            if len(package.canonical_bytes()) <= MAX_PACKAGE_BYTES:
                break
        if len(package.canonical_bytes()) > MAX_PACKAGE_BYTES:
            raise PlannerPackageCodecError("mandatory H2 planner package exceeds 96 KiB")
        return package


__all__ = [
    "AcceptedResultView", "CapabilityView", "FactView", "FailureView", "GoalView", "MethodView", "ObligationView", "PlanView", "PlanningBudgetView",
    "PlannerPackageAssemblerV1", "PlannerPackageCollectorV1", "PlannerPackageCodec", "PlannerPackageCodecError", "PlannerPackageV1", "collect_planner_views", "MAX_PACKAGE_BYTES", "MAX_METHODS_PER_SIGNATURE", "MAX_FACTS", "MAX_ACCEPTED_RESULTS", "MAX_FAILURES", "MAX_VISIBLE_REFS", "PACKAGE_VERSION", "PROTOCOL_VERSION",
]
