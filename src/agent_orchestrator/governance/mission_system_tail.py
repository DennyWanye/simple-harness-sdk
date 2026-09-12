# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Mission system allowance before a real synthesis/conflict Task exists.

No Work Task is debited. When the graph commit creates the real system Task,
Mission/global reservations move to that Task's tail in the SAME transaction.
The runtime supplies certified allowance amounts and immutable routing/prices;
this module does not infer a price, a future Task or a larger Mission budget.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from simple_harness.contracts import canonical_json
from simple_harness.execution.budget import FrozenPriceEstimator

from ..contracts import TERMINAL_ATTEMPT, TERMINAL_MISSION, TERMINAL_TASK, ids
from ..planning.candidate_selection import selection_revision
from ..verification.assessments import mission_contract_revision
from .budgets import BudgetError, BudgetExhausted, BudgetLedger
from .tail_budget import TailAllocation, TailBudgetLedger, TailReserve


@dataclass(frozen=True, slots=True)
class SystemTailRoute:
    profile_id: str
    model: str
    profile_fingerprint: str
    price: FrozenPriceEstimator | None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.profile_id,
                self.model,
                self.profile_fingerprint,
            )
        ):
            raise BudgetError("system tail routing must be explicitly frozen")
        if self.price is not None and not isinstance(self.price, FrozenPriceEstimator):
            raise BudgetError(
                "system tail price must be a frozen SDK estimator or explicit unpriced"
            )

    def to_json(self) -> dict[str, Any]:
        return dict(
            profile_id=self.profile_id,
            model=self.model,
            profile_fingerprint=self.profile_fingerprint,
            pricing_mode="unpriced_local" if self.price is None else "consumer_supplied",
            price=None if self.price is None else self.price.snapshot_json(),
        )


@dataclass(frozen=True, slots=True)
class SystemTailBinding:
    worker: SystemTailRoute
    critic: SystemTailRoute | None
    cost_bound_protocol: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker, SystemTailRoute)
            or (self.critic is not None and not isinstance(self.critic, SystemTailRoute))
            or not isinstance(self.cost_bound_protocol, str)
            or not self.cost_bound_protocol.strip()
        ):
            raise BudgetError("system tail needs explicit role routes and allowance protocol")

    def to_json(self) -> dict[str, Any]:
        return dict(
            schema_version=1,
            worker=self.worker.to_json(),
            critic=None if self.critic is None else self.critic.to_json(),
            cost_bound_protocol=self.cost_bound_protocol,
        )


class MissionSystemTailLedger:
    def __init__(self, ledger: BudgetLedger) -> None:
        self.ledger = ledger
        self.store = ledger._store

    def _transaction(self) -> None:
        if not self.store.connection.in_transaction:
            raise BudgetError("system pool mutation must share the actual Commit transaction")

    def _mission(self, mission_id: str, revision: str):
        mission = self.store.get_mission(mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            raise BudgetError("system pool requires a live actual Mission")
        if mission_contract_revision(mission) != revision:
            raise BudgetError("system pool Mission contract changed")
        account = self.ledger.account(f"budget:{mission_id}")
        if account.scope != "mission":
            raise BudgetError("system pool requires the original Mission account")
        return mission

    def pool(self, pool_id: str) -> dict[str, Any] | None:
        row = self.store.connection.execute(
            "SELECT * FROM mission_system_tail_pools WHERE pool_id=?",
            (pool_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def reserve_pool(
        self,
        pool_id: str,
        *,
        mission_id: str,
        purpose: str,
        reserve: TailReserve,
        mission_revision: str,
        route_binding: SystemTailBinding,
    ) -> dict[str, Any]:
        self._transaction()
        if not isinstance(pool_id, str) or not pool_id or purpose not in {"conflict", "synthesis"}:
            raise BudgetError("system pool requires an explicit identity and system purpose")
        if reserve.attempts < 1 or reserve.tokens <= 0:
            raise BudgetError("system pool must protect a real future Attempt and token allowance")
        binding = canonical_json(route_binding.to_json())
        request = canonical_json(
            dict(
                pool_id=pool_id,
                mission_id=mission_id,
                purpose=purpose,
                mission_revision=mission_revision,
                reserve=asdict(reserve),
                route_binding=route_binding.to_json(),
            )
        )
        prior = self.pool(pool_id)
        if prior is not None:
            if prior["request_json"] != request:
                raise BudgetError("system pool replay cannot change allowance, routing or price")
            return prior
        self._mission(mission_id, mission_revision)
        account_id = f"budget:{mission_id}"
        chain = self.ledger._chain(account_id)
        for account in chain:
            remaining = account.remaining_attempts()
            if remaining is not None and reserve.attempts > remaining:
                raise BudgetExhausted(account.account_id, "attempts", reserve.attempts, remaining)
        subject = "mission-system:" + pool_id
        if self.ledger.reservation(subject) is not None:
            raise BudgetError("system pool subject already has a reservation")
        self.ledger.reserve(
            account_id=account_id,
            subject_id=subject,
            mission_id=mission_id,
            tokens=reserve.tokens,
            cost_micros=reserve.cost_micros,
            tool_calls=reserve.tool_calls,
            counts_attempt=False,
        )
        for account in chain:
            self.ledger._apply(account.account_id, reserved_attempts=reserve.attempts)
        self.store.connection.execute(
            "INSERT INTO mission_system_tail_pools VALUES(?,?,?,?,?,?,?,?, 'HELD',NULL,?,?)",
            (
                pool_id,
                mission_id,
                subject,
                purpose,
                mission_revision,
                binding,
                request,
                reserve.attempts,
                self.store.now,
                self.store.now,
            ),
        )
        result = self.pool(pool_id)
        assert result is not None
        return result

    def transfer_to_task_hold(
        self,
        pool_id: str,
        *,
        task_id: str,
        reserve: TailReserve,
        semantic_revision: str,
        route_binding: SystemTailBinding,
    ) -> dict[str, Any]:
        self._transaction()
        pool = self.pool(pool_id)
        if pool is None:
            raise BudgetError("system pool is missing")
        binding = canonical_json(route_binding.to_json())
        if binding != pool["binding_json"]:
            raise BudgetError("system pool routing/price differs; new allowance authority required")
        request = canonical_json(
            dict(
                pool_id=pool_id,
                task_id=task_id,
                reserve=asdict(reserve),
                semantic_revision=semantic_revision,
                route_binding=route_binding.to_json(),
            )
        )
        prior = self.store.connection.execute(
            "SELECT * FROM mission_system_tail_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if prior is not None:
            if prior["request_json"] != request:
                raise BudgetError("system Task transfer replay changed")
            return dict(prior)
        if pool["state"] != "HELD":
            raise BudgetError("system pool is released")
        self._mission(pool["mission_id"], pool["mission_revision"])
        task = self.store.get_task(task_id)
        if task is None or task.mission_id != pool["mission_id"] or task.kind != pool["purpose"]:
            raise BudgetError("system transfer needs the actual same-Mission system Task")
        if (
            task.status in TERMINAL_TASK
            or selection_revision(self.store, task) != semantic_revision
        ):
            raise BudgetError("system Task is terminal or its semantic revision changed")
        if reserve.attempts < 1 or reserve.attempts > pool["remaining_attempts"]:
            raise BudgetError("system transfer exceeds protected Attempt count")
        original = self.ledger.reservation(pool["subject_id"])
        if original is None or original["state"] != "RESERVED":
            raise BudgetError("system pool has no live original reservation")
        self._unused(pool["subject_id"])
        destination = self.ledger.account(f"budget:{task_id}")
        if destination.scope != "task" or destination.parent_id != original["account_id"]:
            raise BudgetError("system Task account does not belong to the original Mission")
        amounts = dict(
            tokens=reserve.tokens, cost_micros=reserve.cost_micros, tool_calls=reserve.tool_calls
        )
        for dimension, amount in amounts.items():
            held = original["reserved_" + dimension]
            remaining = getattr(destination, "remaining_" + dimension)()
            if amount > held:
                raise BudgetExhausted(original["account_id"], dimension, amount, held)
            if remaining is not None and amount > remaining:
                raise BudgetExhausted(destination.account_id, dimension, amount, remaining)
        remaining_attempts = destination.remaining_attempts()
        if remaining_attempts is not None and reserve.attempts > remaining_attempts:
            raise BudgetExhausted(
                destination.account_id, "attempts", reserve.attempts, remaining_attempts
            )
        # A historical actual overrun can leave an ancestor negative. Preflight
        # that case BEFORE reducing the original pool: Store nested transactions
        # join their caller, so a caught exception must not leave half a transfer.
        for account in self.ledger._chain(original["account_id"]):
            for dimension in (*amounts, "attempts"):
                remaining = getattr(account, "remaining_" + dimension)()
                if remaining is not None and remaining < 0:
                    raise BudgetExhausted(account.account_id, dimension, 0, remaining)
        hold_id = f"system:{task_id}"
        if (
            self.ledger.reservation("tail:" + hold_id) is not None
            or self.store.connection.execute(
                "SELECT 1 FROM budget_tail_holds WHERE hold_id=?",
                (hold_id,),
            ).fetchone()
        ):
            raise BudgetError("system Task already has a separate hold; cannot double fund it")
        # Only Mission/global lose the pool share; the destination reserve adds
        # exactly that share back plus the Task-level protection. No await/gap.
        for account in self.ledger._chain(original["account_id"]):
            self.ledger._apply(
                account.account_id,
                **{"reserved_" + dimension: -amount for dimension, amount in amounts.items()},
                reserved_attempts=-reserve.attempts,
            )
        self.store.connection.execute(
            "UPDATE budget_reservations SET reserved_tokens=reserved_tokens-?,"
            "reserved_cost_micros=reserved_cost_micros-?,reserved_tool_calls=reserved_tool_calls-?,"
            "updated_at=? WHERE subject_id=?",
            (
                reserve.tokens,
                reserve.cost_micros,
                reserve.tool_calls,
                self.store.now,
                pool["subject_id"],
            ),
        )
        self.store.connection.execute(
            "UPDATE mission_system_tail_pools SET remaining_attempts=remaining_attempts-?,"
            "updated_at=?"
            " WHERE pool_id=?",
            (reserve.attempts, self.store.now, pool_id),
        )
        TailBudgetLedger(self.ledger).reserve_tail(
            hold_id,
            destination.account_id,
            reserve,
            mission_id=pool["mission_id"],
            task_revision=semantic_revision,
            purpose=task.kind,
        )
        self.store.connection.execute(
            "INSERT INTO mission_system_tail_tasks VALUES(?,?,?,?,?,?)",
            (task_id, pool_id, hold_id, request, binding, self.store.now),
        )
        return dict(
            self.store.connection.execute(
                "SELECT * FROM mission_system_tail_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        )

    def consume_task_hold(
        self,
        *,
        task_id: str,
        attempt_id: str,
        subject_id: str,
        reservation: Any,
        semantic_revision: str,
        route_binding: SystemTailBinding,
        critic: bool = False,
    ) -> dict[str, Any]:
        self._transaction()
        row = self.store.connection.execute(
            "SELECT * FROM mission_system_tail_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None or row["binding_json"] != canonical_json(route_binding.to_json()):
            raise BudgetError("system Task has no matching frozen routing/price authority")
        task = self.store.get_task(task_id)
        if task is None or selection_revision(self.store, task) != semantic_revision:
            raise BudgetError("system Task semantic revision changed")
        if type(critic) is not bool or (critic and route_binding.critic is None):
            raise BudgetError("system Critic route was not reserved")
        # Delegate actual identity/terminal/positive-ordinal checks to the owned
        # Commit hook; it does not perform any routing or price estimation.
        from ..orchestrator.protected_tail_commits import ProtectedTailCommitsMixin

        existing = self.store.get_attempt(attempt_id)
        if existing is None:
            if critic or attempt_id != ids.attempt_id(
                task_id, len(self.store.list_attempts(task_id)) + 1
            ):
                raise BudgetError("system consumption needs an actual Attempt identity")
        elif existing.task_id != task_id or existing.status in TERMINAL_ATTEMPT:
            raise BudgetError("system Attempt belongs to another Task or is terminal")
        if critic:
            ProtectedTailCommitsMixin._protected_critic_subject(attempt_id, subject_id)
        elif subject_id != attempt_id:
            raise BudgetError("system Attempt subject differs")
        return TailBudgetLedger(self.ledger).transfer_tail(
            row["hold_id"],
            subject_id,
            [
                TailAllocation(
                    subject_id,
                    f"budget:{task_id}",
                    "critic" if critic else task.kind,
                    reservation.tokens,
                    reservation.cost_micros,
                    reservation.tool_calls,
                    counts_attempt=not critic,
                )
            ],
            task_revision=semantic_revision,
        )

    def _unused(self, subject_id: str) -> None:
        if (
            self.store.connection.execute(
                "SELECT 1 FROM imported_usage WHERE subject_id=? LIMIT 1",
                (subject_id,),
            ).fetchone()
            or self.store.connection.execute(
                "SELECT 1 FROM provider_token_grants WHERE subject_id=? LIMIT 1",
                (subject_id,),
            ).fetchone()
        ):
            raise BudgetError("system pool contains actual usage; cannot treat it as unused")

    def release_unused_pool(
        self, pool_id: str, *, mission_revision: str, reason: str
    ) -> dict[str, Any]:
        self._transaction()
        pool = self.pool(pool_id)
        if (
            pool is None
            or pool["mission_revision"] != mission_revision
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise BudgetError("system pool release identity/reason differs")
        if pool["state"] == "RELEASED":
            if pool["release_reason"] != reason:
                raise BudgetError("system pool release replay changed reason")
            return pool
        self._unused(pool["subject_id"])
        original = self.ledger.reservation(pool["subject_id"])
        assert original is not None
        self.ledger.settle(subject_id=pool["subject_id"])
        for account in self.ledger._chain(original["account_id"]):
            self.ledger._apply(account.account_id, reserved_attempts=-pool["remaining_attempts"])
        self.store.connection.execute(
            "UPDATE mission_system_tail_pools SET state='RELEASED',remaining_attempts=0,"
            "release_reason=?,updated_at=? WHERE pool_id=?",
            (reason, self.store.now, pool_id),
        )
        result = self.pool(pool_id)
        assert result is not None
        return result


__all__ = ("SystemTailRoute", "SystemTailBinding", "MissionSystemTailLedger")
