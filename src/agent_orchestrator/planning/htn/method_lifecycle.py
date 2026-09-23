"""H6 method evaluation and promotion contract.

Evaluation is a registry-side decision.  A planner may propose a method, but it
cannot manufacture an evaluation record or a promotion transition.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from ...contracts.models import ContractError
from ...contracts.semantic_base import content_hash_of


@dataclass(frozen=True, slots=True)
class EvaluationSetV1:
    domain_id: str
    trial_ids: tuple[str, ...]
    heldout_ids: tuple[str, ...]
    content_hash: str

    @classmethod
    def build(
        cls, domain_id: str, trial_ids: Sequence[str], heldout_ids: Sequence[str]
    ) -> EvaluationSetV1:
        trials = tuple(sorted(set(str(item) for item in trial_ids)))
        heldout = tuple(sorted(set(str(item) for item in heldout_ids)))
        if not domain_id.strip() or not trials or not heldout:
            raise ContractError("evaluation set fields must not be empty")
        if (
            len(trials) != len(trial_ids)
            or len(heldout) != len(heldout_ids)
            or set(trials) & set(heldout)
        ):
            raise ContractError("evaluation trials and heldout runs must be unique and disjoint")
        body = {
            "schema_version": "evaluation-set-v1",
            "domain_id": domain_id,
            "trial_ids": list(trials),
            "heldout_ids": list(heldout),
        }
        return cls(domain_id, trials, heldout, content_hash_of(body))

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": "evaluation-set-v1",
            "domain_id": self.domain_id,
            "trial_ids": list(self.trial_ids),
            "heldout_ids": list(self.heldout_ids),
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> EvaluationSetV1:
        if value.get("schema_version") != "evaluation-set-v1":
            raise ContractError("unsupported evaluation set schema")
        built = cls.build(
            str(value["domain_id"]),
            tuple(value["trial_ids"]),
            tuple(value["heldout_ids"]),
        )
        if value.get("content_hash") not in (None, built.content_hash):
            raise ContractError("evaluation set content_hash does not match")
        return built


@dataclass(frozen=True, slots=True)
class MethodLifecyclePolicyV1:
    min_trials: int = 20
    min_heldout: int = 5
    acceptance_threshold: float = 0.85
    max_critical_side_effect_failures: int = 0
    max_unresolved_operations: int = 0
    max_cost_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if (
            self.min_trials < 1
            or self.min_heldout < 1
            or not 0.0 <= self.acceptance_threshold <= 1.0
        ):
            raise ContractError("invalid lifecycle policy thresholds")
        if (
            self.max_critical_side_effect_failures < 0
            or self.max_unresolved_operations < 0
            or not math.isfinite(self.max_cost_multiplier)
            or self.max_cost_multiplier <= 0
        ):
            raise ContractError("invalid lifecycle policy limits")


@dataclass(frozen=True, slots=True)
class MethodEvaluationRecordV1:
    method_ref: str
    evaluation_set: EvaluationSetV1
    trial_count: int
    heldout_count: int
    acceptance_rate: float
    critical_side_effect_failures: int
    unresolved_operations: int
    costs: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.method_ref.strip() or not 0 <= self.acceptance_rate <= 1:
            raise ContractError("invalid method evaluation identity or acceptance rate")
        if self.trial_count != len(self.evaluation_set.trial_ids) or self.heldout_count != len(
            self.evaluation_set.heldout_ids
        ):
            raise ContractError("evaluation counts must match the frozen run identities")
        if (
            self.critical_side_effect_failures < 0
            or self.unresolved_operations < 0
            or len(self.costs) != self.trial_count + self.heldout_count
            or any(not math.isfinite(cost) or cost < 0 for cost in self.costs)
        ):
            raise ContractError(
                "evaluation needs one finite cost per run and non-negative failures"
            )

    @property
    def median_cost(self) -> float:
        if not self.costs or any(item < 0 for item in self.costs):
            raise ContractError("evaluation costs must be non-negative and non-empty")
        return float(median(self.costs))

    def meets(self, policy: MethodLifecyclePolicyV1, baseline_cost: float = 1.0) -> bool:
        return (
            self.trial_count >= policy.min_trials
            and self.heldout_count >= policy.min_heldout
            and self.acceptance_rate >= policy.acceptance_threshold
            and self.critical_side_effect_failures <= policy.max_critical_side_effect_failures
            and self.unresolved_operations <= policy.max_unresolved_operations
            and self.median_cost <= baseline_cost * policy.max_cost_multiplier
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "method_ref": self.method_ref,
            "evaluation_set": self.evaluation_set.to_json(),
            "trial_count": self.trial_count,
            "heldout_count": self.heldout_count,
            "acceptance_rate": self.acceptance_rate,
            "critical_side_effect_failures": self.critical_side_effect_failures,
            "unresolved_operations": self.unresolved_operations,
            "costs": list(self.costs),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> MethodEvaluationRecordV1:
        return cls(
            method_ref=str(value["method_ref"]),
            evaluation_set=EvaluationSetV1.from_json(value["evaluation_set"]),
            trial_count=int(value["trial_count"]),
            heldout_count=int(value["heldout_count"]),
            acceptance_rate=float(value["acceptance_rate"]),
            critical_side_effect_failures=int(value["critical_side_effect_failures"]),
            unresolved_operations=int(value["unresolved_operations"]),
            costs=tuple(float(item) for item in value["costs"]),
        )


@dataclass(frozen=True, slots=True)
class MethodLifecycleReceipt:
    method_ref: str
    status: str
    evaluation_set_hash: str
    reason: str = ""


class MethodLifecycleService:
    def __init__(self, policy: MethodLifecyclePolicyV1 | None = None) -> None:
        self.policy = policy or MethodLifecyclePolicyV1()
        self._records: dict[str, MethodEvaluationRecordV1] = {}
        self._statuses: dict[str, str] = {}

    def evaluate(self, record: MethodEvaluationRecordV1) -> MethodLifecycleReceipt:
        if not record.method_ref.strip():
            raise ContractError("method_ref must not be empty")
        existing = self._records.get(record.method_ref)
        if (
            existing is not None
            and existing.evaluation_set.content_hash != record.evaluation_set.content_hash
        ):
            raise ContractError("evaluation set is frozen for this method")
        if existing is not None and existing != record:
            raise ContractError("evaluation record replay differs")
        if existing is None:
            self._records[record.method_ref] = record
        passed = record.meets(self.policy)
        status = (
            "ADMITTED"
            if self._statuses.get(record.method_ref) == "ADMITTED" and passed
            else "EVALUATED"
            if passed
            else "REJECTED"
        )
        self._statuses[record.method_ref] = status
        return MethodLifecycleReceipt(
            record.method_ref,
            status,
            record.evaluation_set.content_hash,
            "" if passed else "evaluation thresholds not met",
        )

    def promote(self, record: MethodEvaluationRecordV1) -> MethodLifecycleReceipt:
        status = self._statuses.get(record.method_ref)
        if self._records.get(record.method_ref) != record:
            raise ContractError("promotion record differs from the frozen evaluation")
        if status not in {"EVALUATED", "ADMITTED"}:
            raise ContractError("method is not evaluated")
        self._statuses[record.method_ref] = "ADMITTED"
        return MethodLifecycleReceipt(
            record.method_ref, "ADMITTED", record.evaluation_set.content_hash
        )

    def status(self, method_ref: str) -> str | None:
        return self._statuses.get(method_ref)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": "method-lifecycle-v1",
            "statuses": dict(sorted(self._statuses.items())),
            "records": [self._records[key].to_json() for key in sorted(self._records)],
        }

    @classmethod
    def from_json(
        cls,
        value: Mapping[str, Any],
        *,
        policy: MethodLifecyclePolicyV1 | None = None,
    ) -> MethodLifecycleService:
        if value.get("schema_version") != "method-lifecycle-v1":
            raise ContractError("unsupported method lifecycle schema")
        service = cls(policy)
        for raw in value.get("records", ()):
            record = MethodEvaluationRecordV1.from_json(raw)
            service.evaluate(record)
        saved = value.get("statuses", {})
        if not isinstance(saved, Mapping):
            raise ContractError("method lifecycle statuses must be an object")
        for method_ref, status in saved.items():
            if method_ref not in service._records:
                raise ContractError("lifecycle status has no evaluation record")
            if status not in {"REJECTED", "EVALUATED", "ADMITTED"}:
                raise ContractError("unknown method lifecycle status")
            computed = service._statuses.get(method_ref)
            if status == "ADMITTED" and computed != "EVALUATED":
                raise ContractError("ADMITTED status does not satisfy lifecycle thresholds")
            if status != "ADMITTED" and status != computed:
                raise ContractError("method lifecycle status does not match evaluation")
            service._statuses[method_ref] = status
        return service


__all__ = (
    "EvaluationSetV1",
    "MethodEvaluationRecordV1",
    "MethodLifecyclePolicyV1",
    "MethodLifecycleReceipt",
    "MethodLifecycleService",
)
