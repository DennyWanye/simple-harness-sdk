# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Obligations and the in-memory obligation ledger (§6.1, §6.4, ADR-08).

An :class:`Obligation` is the duty that survives re-planning.  Splitting a task,
swapping a method, renaming a goal or handing the work to a different agent are
all changes of *how* the duty is discharged; none of them mints a fresh retry
allowance.  The ledger therefore keys failure counts, spend and recursion fuel on
``obligation_id`` alone, and every "the work changed shape" operation is recorded
as history that leaves the counters where they were.

Recursion fuel follows the same rule (§6.4 v1.2): it is counted per obligation,
not per ``goal signature + parameters``, and running out is ``BOUND_REACHED`` —
a statement about this deployment's bounds, never ``UNSOLVABLE``, which would be
a claim about the world.  The generic ``achieve_outcome`` capability may not take
over once fuel is gone; :func:`achieve_outcome_admission` states that as a pure
rule so no caller has to remember it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .htn import ObligationId, Requiredness, obligation_id
from .models import ContractError
from .semantic_base import (
    MAX_LIST,
    enum_of,
    fields_of,
    identifier,
    identifiers,
    index,
    json_object,
    optional_identifier,
    text,
)


class ObligationLifecycle(StrEnum):
    """§6.1: unmet, met, cancelled, or replaced by an authorised substitute."""

    UNSATISFIED = "UNSATISFIED"
    SATISFIED = "SATISFIED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class FuelStatus(StrEnum):
    """ADR-08: exceeding a bound is reported as a bound, not as impossibility."""

    GRANTED = "GRANTED"
    BOUND_REACHED = "BOUND_REACHED"
    REPEATED_EXPANSION = "REPEATED_EXPANSION"


class ShapeChange(StrEnum):
    """The re-planning events that must *not* reset an obligation's counters."""

    TASK_RENAMED = "task_renamed"
    METHOD_SWITCHED = "method_switched"
    AGENT_REASSIGNED = "agent_reassigned"
    PARAMETERS_REBOUND = "parameters_rebound"
    SUCCESSOR_TASK = "successor_task"


class AchieveOutcomeAdmission(StrEnum):
    """Why the generic ``achieve_outcome`` capability may or may not be used here."""

    ADMISSIBLE = "ADMISSIBLE"
    FUEL_EXHAUSTED_NO_ESCAPE = "FUEL_EXHAUSTED_NO_ESCAPE"
    NOT_EXPLICITLY_SELECTED = "NOT_EXPLICITLY_SELECTED"
    OBLIGATION_NOT_ACTIVE = "OBLIGATION_NOT_ACTIVE"


class Selector(StrEnum):
    PLANNER_EXPLICIT = "planner_explicit"
    AUTOMATIC_FALLBACK = "automatic_fallback"


@dataclass(frozen=True, slots=True)
class SatisfactionPolicy:
    """§6.1: what evidence, and in what combination, discharges this duty."""

    required_criterion_ids: tuple[str, ...]
    independent_review_required: bool = True
    delivery_stage_required: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_criterion_ids",
            identifiers(self.required_criterion_ids, "satisfaction_policy.required_criterion_ids"),
        )
        if not isinstance(self.independent_review_required, bool):
            raise ContractError("satisfaction_policy.independent_review_required must be a boolean")
        object.__setattr__(
            self,
            "delivery_stage_required",
            optional_identifier(
                self.delivery_stage_required, "satisfaction_policy.delivery_stage_required"
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "required_criterion_ids": list(self.required_criterion_ids),
            "independent_review_required": self.independent_review_required,
            "delivery_stage_required": self.delivery_stage_required,
        }

    @classmethod
    def from_json(cls, value: object, name: str = "satisfaction_policy") -> SatisfactionPolicy:
        data = fields_of(
            value,
            name,
            required=("required_criterion_ids",),
            optional=("independent_review_required", "delivery_stage_required"),
        )
        return cls(
            required_criterion_ids=tuple(data["required_criterion_ids"]),
            independent_review_required=data.get("independent_review_required", True),
            delivery_stage_required=data.get("delivery_stage_required"),
        )


@dataclass(frozen=True, slots=True)
class Obligation:
    """§6.1: a duty, its authority scope, its funding lineage and its lifecycle.

    ``scope`` / ``authority_ref`` describe *where* work is allowed, and are not a
    self-issued credential: holding the obligation does not grant the capability.
    """

    obligation_id: ObligationId
    mission_id: str
    requirement_refs: tuple[str, ...]
    goal_signature_id: str
    parameters: dict[str, Any] = field(default_factory=dict)
    scope: str = "mission"
    authority_ref: str | None = None
    requiredness: Requiredness = Requiredness.REQUIRED
    budget_lineage_ref: str | None = None
    satisfaction_policy: SatisfactionPolicy | None = None
    lifecycle: ObligationLifecycle = ObligationLifecycle.UNSATISFIED
    resolution_ref: str | None = None
    parent_obligation_id: ObligationId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "obligation_id", obligation_id(self.obligation_id, "obligation.obligation_id")
        )
        object.__setattr__(self, "mission_id", identifier(self.mission_id, "obligation.mission_id"))
        object.__setattr__(
            self,
            "requirement_refs",
            identifiers(self.requirement_refs, "obligation.requirement_refs"),
        )
        object.__setattr__(
            self,
            "goal_signature_id",
            identifier(self.goal_signature_id, "obligation.goal_signature_id"),
        )
        object.__setattr__(
            self, "parameters", json_object(self.parameters, "obligation.parameters")
        )
        object.__setattr__(self, "scope", identifier(self.scope, "obligation.scope"))
        object.__setattr__(
            self,
            "authority_ref",
            optional_identifier(self.authority_ref, "obligation.authority_ref"),
        )
        object.__setattr__(
            self,
            "requiredness",
            enum_of(Requiredness, self.requiredness, "obligation.requiredness"),
        )
        object.__setattr__(
            self,
            "budget_lineage_ref",
            optional_identifier(self.budget_lineage_ref, "obligation.budget_lineage_ref"),
        )
        if self.satisfaction_policy is not None and not isinstance(
            self.satisfaction_policy, SatisfactionPolicy
        ):
            raise ContractError("obligation.satisfaction_policy must be a SatisfactionPolicy")
        object.__setattr__(
            self, "lifecycle", enum_of(ObligationLifecycle, self.lifecycle, "obligation.lifecycle")
        )
        object.__setattr__(
            self,
            "resolution_ref",
            optional_identifier(self.resolution_ref, "obligation.resolution_ref"),
        )
        if self.parent_obligation_id is not None:
            object.__setattr__(
                self,
                "parent_obligation_id",
                obligation_id(self.parent_obligation_id, "obligation.parent_obligation_id"),
            )
            if self.parent_obligation_id == self.obligation_id:
                raise ContractError("an obligation may not be its own parent")
        if self.lifecycle is ObligationLifecycle.SATISFIED and self.resolution_ref is None:
            raise ContractError("a SATISFIED obligation needs its current resolution_ref (§6.1)")

    def to_json(self) -> dict[str, Any]:
        return {
            "obligation_id": str(self.obligation_id),
            "mission_id": self.mission_id,
            "requirement_refs": list(self.requirement_refs),
            "goal_signature_id": self.goal_signature_id,
            "parameters": dict(self.parameters),
            "scope": self.scope,
            "authority_ref": self.authority_ref,
            "requiredness": str(self.requiredness),
            "budget_lineage_ref": self.budget_lineage_ref,
            "satisfaction_policy": (
                None if self.satisfaction_policy is None else self.satisfaction_policy.to_json()
            ),
            "lifecycle": str(self.lifecycle),
            "resolution_ref": self.resolution_ref,
            "parent_obligation_id": (
                None if self.parent_obligation_id is None else str(self.parent_obligation_id)
            ),
        }

    @classmethod
    def from_json(cls, value: object, name: str = "obligation") -> Obligation:
        data = fields_of(
            value,
            name,
            required=("obligation_id", "mission_id", "requirement_refs", "goal_signature_id"),
            optional=(
                "parameters",
                "scope",
                "authority_ref",
                "requiredness",
                "budget_lineage_ref",
                "satisfaction_policy",
                "lifecycle",
                "resolution_ref",
                "parent_obligation_id",
            ),
        )
        raw_policy = data.get("satisfaction_policy")
        raw_parent = data.get("parent_obligation_id")
        return cls(
            obligation_id=ObligationId(data["obligation_id"]),
            mission_id=data["mission_id"],
            requirement_refs=tuple(data["requirement_refs"]),
            goal_signature_id=data["goal_signature_id"],
            parameters=dict(data.get("parameters", {})),
            scope=data.get("scope", "mission"),
            authority_ref=data.get("authority_ref"),
            requiredness=data.get("requiredness", Requiredness.REQUIRED),
            budget_lineage_ref=data.get("budget_lineage_ref"),
            satisfaction_policy=(
                None
                if raw_policy is None
                else SatisfactionPolicy.from_json(raw_policy, f"{name}.satisfaction_policy")
            ),
            lifecycle=data.get("lifecycle", ObligationLifecycle.UNSATISFIED),
            resolution_ref=data.get("resolution_ref"),
            parent_obligation_id=None if raw_parent is None else ObligationId(raw_parent),
        )


@dataclass(frozen=True, slots=True)
class ExpansionRecord:
    """One refinement of an obligation, identified for repeat detection (§6.4)."""

    method_id: str
    parameters_digest: str
    task_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method_id", identifier(self.method_id, "expansion.method_id"))
        object.__setattr__(
            self,
            "parameters_digest",
            identifier(self.parameters_digest, "expansion.parameters_digest"),
        )
        object.__setattr__(self, "task_id", optional_identifier(self.task_id, "expansion.task_id"))

    @property
    def key(self) -> tuple[str, str]:
        """Repeat detection ignores which task carried the expansion."""

        return (self.method_id, self.parameters_digest)


@dataclass(frozen=True, slots=True)
class ObligationAccountView:
    """An immutable read of one obligation's accumulated cost and remaining fuel."""

    obligation_id: ObligationId
    failure_count: int
    consumed_cost_micros: int
    consumed_attempts: int
    fuel_limit: int
    fuel_used: int
    expansions: int
    shape_changes: int
    lifecycle: ObligationLifecycle
    resolution_ref: str | None = None

    @property
    def remaining_fuel(self) -> int:
        return max(self.fuel_limit - self.fuel_used, 0)

    def to_json(self) -> dict[str, Any]:
        return {
            "obligation_id": str(self.obligation_id),
            "failure_count": self.failure_count,
            "consumed_cost_micros": self.consumed_cost_micros,
            "consumed_attempts": self.consumed_attempts,
            "fuel_limit": self.fuel_limit,
            "fuel_used": self.fuel_used,
            "remaining_fuel": self.remaining_fuel,
            "expansions": self.expansions,
            "shape_changes": self.shape_changes,
            "lifecycle": str(self.lifecycle),
            "resolution_ref": self.resolution_ref,
        }


@dataclass(frozen=True, slots=True)
class FuelDecision:
    status: FuelStatus
    remaining_fuel: int
    reason: str

    @property
    def granted(self) -> bool:
        return self.status is FuelStatus.GRANTED


@dataclass(frozen=True, slots=True)
class AchieveOutcomeDecision:
    admission: AchieveOutcomeAdmission
    reason: str

    @property
    def allowed(self) -> bool:
        return self.admission is AchieveOutcomeAdmission.ADMISSIBLE


def achieve_outcome_admission(
    account: ObligationAccountView, *, selected_by: Selector
) -> AchieveOutcomeDecision:
    """§6.4: the generic atomic capability is a planner choice, not an escape hatch.

    It is admissible only while fuel remains *and* only when a planner picked it
    deliberately.  Once the obligation has reached ``BOUND_REACHED`` it cannot
    quietly absorb the undecomposed compound goal, because that would convert a
    reported bound into an unverified claim of completion.
    """

    if account.lifecycle is not ObligationLifecycle.UNSATISFIED:
        return AchieveOutcomeDecision(
            AchieveOutcomeAdmission.OBLIGATION_NOT_ACTIVE,
            "the obligation is no longer open",
        )
    if account.remaining_fuel <= 0:
        return AchieveOutcomeDecision(
            AchieveOutcomeAdmission.FUEL_EXHAUSTED_NO_ESCAPE,
            "recursion fuel is exhausted; report BOUND_REACHED instead of taking over",
        )
    if selected_by is not Selector.PLANNER_EXPLICIT:
        return AchieveOutcomeDecision(
            AchieveOutcomeAdmission.NOT_EXPLICITLY_SELECTED,
            "achieve_outcome must be chosen explicitly by the planner",
        )
    return AchieveOutcomeDecision(AchieveOutcomeAdmission.ADMISSIBLE, "explicitly selected")


class _Account:
    """Mutable ledger row.  Only :class:`ObligationLedger` touches it."""

    __slots__ = (
        "consumed_attempts",
        "consumed_cost_micros",
        "expansion_keys",
        "failure_count",
        "fuel_limit",
        "fuel_used",
        "lifecycle",
        "obligation",
        "resolution_ref",
        "shape_changes",
    )

    def __init__(self, obligation: Obligation, fuel_limit: int) -> None:
        self.obligation = obligation
        self.fuel_limit = fuel_limit
        self.fuel_used = 0
        self.failure_count = 0
        self.consumed_cost_micros = 0
        self.consumed_attempts = 0
        self.expansion_keys: list[tuple[str, str]] = []
        self.shape_changes: list[tuple[ShapeChange, str]] = []
        self.lifecycle = obligation.lifecycle
        self.resolution_ref = obligation.resolution_ref


class ObligationLedger:
    """A pure in-memory ledger of duties, their accumulated cost and their fuel.

    There is no store here and no transaction: P1.2 persists the same counters.
    What this class fixes is the *arithmetic* — in particular that nothing except
    an explicit new obligation ever resets a counter.
    """

    def __init__(self, *, default_fuel: int = 3) -> None:
        if isinstance(default_fuel, bool) or not isinstance(default_fuel, int) or default_fuel < 0:
            raise ContractError("default_fuel must be a non-negative integer")
        self._default_fuel = default_fuel
        self._accounts: dict[ObligationId, _Account] = {}

    # -- registration ---------------------------------------------------------------

    def register(self, obligation: Obligation, *, recursion_fuel: int | None = None) -> None:
        """Add a duty.  Re-registering the same id never re-opens its allowance."""

        if not isinstance(obligation, Obligation):
            raise ContractError("register expects an Obligation")
        if obligation.obligation_id in self._accounts:
            raise ContractError(
                f"obligation {obligation.obligation_id!s} is already registered; "
                "a genuinely new duty needs a new obligation_id (§6.1)"
            )
        fuel = self._default_fuel if recursion_fuel is None else index(recursion_fuel, "fuel")
        self._accounts[obligation.obligation_id] = _Account(obligation, fuel)

    def obligation(self, target: ObligationId) -> Obligation:
        return self._require(target).obligation

    def account(self, target: ObligationId) -> ObligationAccountView:
        account = self._require(target)
        return ObligationAccountView(
            obligation_id=account.obligation.obligation_id,
            failure_count=account.failure_count,
            consumed_cost_micros=account.consumed_cost_micros,
            consumed_attempts=account.consumed_attempts,
            fuel_limit=account.fuel_limit,
            fuel_used=account.fuel_used,
            expansions=len(account.expansion_keys),
            shape_changes=len(account.shape_changes),
            lifecycle=account.lifecycle,
            resolution_ref=account.resolution_ref,
        )

    def obligation_ids(self) -> tuple[ObligationId, ...]:
        return tuple(self._accounts)

    # -- accumulation ---------------------------------------------------------------

    def record_failure(self, target: ObligationId, *, count: int = 1) -> int:
        """§6.1: failures accrue against the duty, not against a task name."""

        account = self._require(target)
        account.failure_count += index(count, "count", minimum=1)
        return account.failure_count

    def record_spend(
        self, target: ObligationId, *, cost_micros: int = 0, attempts: int = 0
    ) -> ObligationAccountView:
        account = self._require(target)
        # Validate every argument before touching the row: a rejected call must
        # leave the ledger exactly as it was, or a caller that retries after a
        # validation error would double-count the half that did land.
        spent = index(cost_micros, "cost_micros")
        tries = index(attempts, "attempts")
        account.consumed_cost_micros += spent
        account.consumed_attempts += tries
        return self.account(target)

    def note_shape_change(
        self, target: ObligationId, change: ShapeChange, *, detail: str
    ) -> ObligationAccountView:
        """Record that the work changed shape.  Counters are deliberately untouched.

        This is the single entry point for "renamed the task", "swapped the
        method", "handed it to another agent": it exists so that such a change is
        *visible* in the ledger without being an opportunity to start over.
        """

        account = self._require(target)
        account.shape_changes.append(
            (enum_of(ShapeChange, change, "shape_change"), text(detail, "detail", limit=512))
        )
        return self.account(target)

    def set_lifecycle(
        self,
        target: ObligationId,
        lifecycle: ObligationLifecycle,
        *,
        resolution_ref: str | None = None,
    ) -> ObligationAccountView:
        account = self._require(target)
        target_lifecycle = enum_of(ObligationLifecycle, lifecycle, "lifecycle")
        reference = optional_identifier(resolution_ref, "resolution_ref")
        if target_lifecycle is ObligationLifecycle.SATISFIED and reference is None:
            raise ContractError("a SATISFIED obligation needs a resolution_ref")
        # Only now is anything written.  A refused transition must not leave the
        # duty parked in the state it was refused for — that would silently take
        # it out of achieve_outcome_admission's "still open" branch.
        account.lifecycle = target_lifecycle
        if reference is not None:
            account.resolution_ref = reference
        return self.account(target)

    # -- recursion fuel -------------------------------------------------------------

    def consume_fuel(self, target: ObligationId, *, expansion: ExpansionRecord) -> FuelDecision:
        """Spend one unit of the obligation's recursion fuel.

        Changing method, parameters or agent does not refill anything: the fuel
        belongs to the duty.  Exhaustion yields ``BOUND_REACHED`` together with the
        expansions already made, never ``UNSOLVABLE`` (ADR-08).
        """

        if not isinstance(expansion, ExpansionRecord):
            raise ContractError("consume_fuel expects an ExpansionRecord")
        account = self._require(target)
        if expansion.key in account.expansion_keys:
            return FuelDecision(
                FuelStatus.REPEATED_EXPANSION,
                max(account.fuel_limit - account.fuel_used, 0),
                "this obligation was already expanded with the same method and parameters",
            )
        if account.fuel_used >= account.fuel_limit:
            return FuelDecision(
                FuelStatus.BOUND_REACHED,
                0,
                "recursion fuel for this obligation is exhausted",
            )
        account.fuel_used += 1
        account.expansion_keys.append(expansion.key)
        return FuelDecision(
            FuelStatus.GRANTED,
            max(account.fuel_limit - account.fuel_used, 0),
            "expansion admitted",
        )

    def remaining_fuel(self, target: ObligationId) -> int:
        account = self._require(target)
        return max(account.fuel_limit - account.fuel_used, 0)

    def expansion_keys(self, target: ObligationId) -> tuple[tuple[str, str], ...]:
        return tuple(self._require(target).expansion_keys)

    def shape_changes(self, target: ObligationId) -> tuple[tuple[ShapeChange, str], ...]:
        return tuple(self._require(target).shape_changes)

    def _require(self, target: ObligationId) -> _Account:
        account = self._accounts.get(target)
        if account is None:
            raise ContractError(f"obligation {target!s} is not registered")
        return account


@dataclass(frozen=True, slots=True)
class BoundReachedReport:
    """ADR-08: what is reported when a bound stops the expansion."""

    obligation_id: ObligationId
    expansions: tuple[tuple[str, str], ...]
    open_child_obligation_ids: tuple[ObligationId, ...]
    reason: str = "recursion fuel exhausted"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "obligation_id", obligation_id(self.obligation_id, "report.obligation_id")
        )
        object.__setattr__(
            self,
            "open_child_obligation_ids",
            tuple(
                ObligationId(item)
                for item in identifiers(
                    self.open_child_obligation_ids, "report.open_child_obligation_ids"
                )
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "status": str(FuelStatus.BOUND_REACHED),
            "obligation_id": str(self.obligation_id),
            "expansions": [list(item) for item in self.expansions],
            "open_child_obligation_ids": [str(item) for item in self.open_child_obligation_ids],
            "reason": self.reason,
        }


def funding_owner_conflicts(
    obligations: tuple[Obligation, ...],
) -> tuple[ObligationId, ...]:
    """§6.1: a shared sub-goal has exactly one funding owner.

    Two duties that name the same ``budget_lineage_ref`` for the same goal
    signature would both reserve the full price; the ledger reports them instead
    of silently double-counting.
    """

    seen: dict[tuple[str, str], ObligationId] = {}
    conflicts: list[ObligationId] = []
    for duty in obligations:
        if duty.budget_lineage_ref is None:
            continue
        key = (duty.goal_signature_id, duty.budget_lineage_ref)
        if key in seen:
            conflicts.append(duty.obligation_id)
        else:
            seen[key] = duty.obligation_id
    return tuple(conflicts)


def obligation_refs(value: object, name: str) -> tuple[ObligationId, ...]:
    return tuple(ObligationId(item) for item in identifiers(value, name, limit=MAX_LIST))


__all__ = (
    "AchieveOutcomeAdmission",
    "AchieveOutcomeDecision",
    "BoundReachedReport",
    "ExpansionRecord",
    "FuelDecision",
    "FuelStatus",
    "Obligation",
    "ObligationAccountView",
    "ObligationLedger",
    "ObligationLifecycle",
    "SatisfactionPolicy",
    "Selector",
    "ShapeChange",
    "achieve_outcome_admission",
    "funding_owner_conflicts",
    "obligation_refs",
)
