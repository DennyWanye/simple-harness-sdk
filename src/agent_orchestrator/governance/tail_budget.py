# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real tail reservations on the original account chain, not budget snapshots.

CommitService owns semantic revision/round/owner validation and must call these
ports inside its transaction, together with the actual Task/Attempt/intent write.
Transfers never accept an existing subject, and never touch its unknown usage.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts import TERMINAL_MISSION, TERMINAL_TASK
from .budgets import BudgetError, BudgetExhausted, BudgetLedger


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BudgetError(f"tail {name} must be nonempty")


@dataclass(frozen=True, slots=True)
class TailReserve:
    tokens: int
    cost_micros: int
    tool_calls: int = 0
    attempts: int = 0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value < 0:
                raise BudgetError(f"tail {name} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class TailAllocation:
    subject_id: str
    account_id: str
    role: str
    tokens: int
    cost_micros: int
    tool_calls: int = 0
    counts_attempt: bool = False

    def __post_init__(self) -> None:
        _text(self.subject_id, "subject")
        _text(self.account_id, "account")
        if self.role not in {"critic", "conflict", "synthesis"}:
            raise BudgetError("tail allocation role is not a protected role")
        TailReserve(self.tokens, self.cost_micros, self.tool_calls)
        if type(self.counts_attempt) is not bool or self.counts_attempt != (self.role != "critic"):
            raise BudgetError("every conflict/synthesis Attempt must count; Critic is a service")


class TailBudgetLedger:
    def __init__(self, ledger: BudgetLedger) -> None:
        self.ledger = ledger
        self.store = ledger._store

    def _transaction_required(self) -> None:
        if not self.store.connection.in_transaction:
            raise BudgetError("tail mutation requires the caller's Commit transaction")

    def _account(self, account_id: str, mission_id: str):
        row = self.store.connection.execute(
            "SELECT mission_id FROM budget_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        account = self.ledger.account(account_id)
        if row is None or row[0] != mission_id or account.scope != "task":
            raise BudgetError("tail must stay on a real Task in the original Mission")
        task_id = account_id.removeprefix("budget:")
        task = self.store.get_task(task_id)
        if task is None or task.mission_id != mission_id:
            raise BudgetError("tail Task account has no matching Task")
        mission = self.store.get_mission(mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            raise BudgetError("tail Mission is terminal")
        if task.status in TERMINAL_TASK:
            raise BudgetError("tail Task is terminal")
        return task

    def reserve_tail(
        self,
        hold_id: str,
        task_account: str,
        reserve: TailReserve,
        *,
        mission_id: str,
        task_revision: str,
        purpose: str,
    ) -> dict[str, Any]:
        self._transaction_required()
        _text(hold_id, "identity")
        _text(task_revision, "Task revision")
        if purpose not in {"selection", "critic", "conflict", "synthesis"}:
            raise BudgetError("unknown tail purpose")
        request = canonical_json(
            dict(
                hold_id=hold_id,
                account_id=task_account,
                mission_id=mission_id,
                task_revision=task_revision,
                purpose=purpose,
                reserve=asdict(reserve),
            )
        )
        prior = self.store.connection.execute(
            "SELECT * FROM budget_tail_holds WHERE hold_id=?", (hold_id,)
        ).fetchone()
        if prior is not None:
            if prior["request_json"] != request:
                raise BudgetError("tail reserve replay changed its frozen contract")
            return dict(prior)
        self._account(task_account, mission_id)
        for snapshot in self.ledger._chain(task_account):
            remaining = snapshot.remaining_attempts()
            if remaining is not None and reserve.attempts > remaining:
                raise BudgetExhausted(snapshot.account_id, "attempts", reserve.attempts, remaining)
        subject = "tail:" + hold_id
        if self.ledger.reservation(subject) is not None:
            raise BudgetError("tail subject already belongs to another reservation")
        self.ledger.reserve(
            account_id=task_account,
            subject_id=subject,
            mission_id=mission_id,
            tokens=reserve.tokens,
            cost_micros=reserve.cost_micros,
            tool_calls=reserve.tool_calls,
            counts_attempt=False,
        )
        for snapshot in self.ledger._chain(task_account):
            if reserve.attempts:
                self.ledger._apply(snapshot.account_id, reserved_attempts=reserve.attempts)
        self.store.connection.execute(
            "INSERT INTO budget_tail_holds VALUES(?,?,?,?,?,?,?,?,'HELD',NULL,?,?)",
            (
                hold_id,
                mission_id,
                task_account,
                subject,
                task_revision,
                purpose,
                request,
                reserve.attempts,
                self.store.now,
                self.store.now,
            ),
        )
        return dict(
            self.store.connection.execute(
                "SELECT * FROM budget_tail_holds WHERE hold_id=?", (hold_id,)
            ).fetchone()
        )

    def reserve_selection_tail(
        self,
        round_id: str,
        task_account: str,
        reserve: TailReserve,
        *,
        mission_id: str,
        task_revision: str,
    ) -> dict[str, Any]:
        if reserve.attempts < 1:
            raise BudgetError("selection tail must protect at least one real synthesis Attempt")
        return self.reserve_tail(
            round_id,
            task_account,
            reserve,
            mission_id=mission_id,
            task_revision=task_revision,
            purpose="selection",
        )

    def transfer_selection_reserve(
        self,
        round_id: str,
        attempt_subject: str,
        allocations: Sequence[TailAllocation],
        *,
        task_revision: str,
    ) -> dict[str, Any]:
        if not any(a.subject_id == attempt_subject and a.counts_attempt for a in allocations):
            raise BudgetError("selection transfer must include its real synthesis Attempt")
        return self.transfer_tail(
            round_id, attempt_subject, allocations, task_revision=task_revision
        )

    def transfer_tail(
        self,
        hold_id: str,
        transfer_id: str,
        allocations: Sequence[TailAllocation],
        *,
        task_revision: str,
    ) -> dict[str, Any]:
        self._transaction_required()
        _text(transfer_id, "transfer identity")
        if not allocations or len({a.subject_id for a in allocations}) != len(allocations):
            raise BudgetError("tail transfer requires distinct actual subjects")
        ordered = sorted(allocations, key=lambda a: a.subject_id)
        request = canonical_json(
            dict(
                hold_id=hold_id,
                transfer_id=transfer_id,
                task_revision=task_revision,
                allocations=[asdict(a) for a in ordered],
            )
        )
        prior = self.store.connection.execute(
            "SELECT request_json,receipt_json FROM budget_tail_transfers"
            " WHERE hold_id=? AND transfer_id=?",
            (hold_id, transfer_id),
        ).fetchone()
        if prior is not None:
            if prior[0] != request:
                raise BudgetError("tail transfer replay changed allocations")
            return json.loads(prior[1])
        hold = self.store.connection.execute(
            "SELECT * FROM budget_tail_holds WHERE hold_id=?", (hold_id,)
        ).fetchone()
        if hold is None or hold["state"] != "HELD" or hold["task_revision"] != task_revision:
            raise BudgetError("tail hold missing, released or bound to another Task revision")
        origin = self._account(hold["account_id"], hold["mission_id"])
        reserve = self.ledger.reservation(hold["subject_id"])
        if reserve is None or reserve["state"] != "RESERVED":
            raise BudgetError("tail reservation is not live")
        if (
            self.ledger.has_unknown_usage(hold["subject_id"])
            or self.ledger.usage_for(hold["subject_id"])[0]
        ):
            raise BudgetError("tail is not an unused hold")
        totals = {
            name: sum(getattr(a, name) for a in ordered)
            for name in ("tokens", "cost_micros", "tool_calls")
        }
        attempts = sum(int(a.counts_attempt) for a in ordered)
        if attempts > hold["remaining_attempts"]:
            raise BudgetError("tail transfer exceeds its protected Attempt count")
        for dimension, amount in totals.items():
            if amount > reserve["reserved_" + dimension]:
                raise BudgetExhausted(
                    hold["account_id"], dimension, amount, reserve["reserved_" + dimension]
                )
        # Preflight every chain including summed Attempt counts BEFORE any write.
        deltas: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for snapshot in self.ledger._chain(hold["account_id"]):
            for dimension, amount in totals.items():
                deltas[snapshot.account_id][dimension] -= amount
            deltas[snapshot.account_id]["attempts"] -= attempts
        for a in ordered:
            if self.ledger.reservation(a.subject_id) is not None:
                raise BudgetError("tail destination already has a reservation; no double reserve")
            target = self._account(a.account_id, hold["mission_id"])
            if a.account_id != hold["account_id"] and (
                target.kind not in {"conflict", "synthesis"}
                or (a.role != "critic" and target.kind != a.role)
                or origin.id not in (*target.parent_task_ids, *target.dependency_ids)
            ):
                raise BudgetError("tail cannot be diverted to an unrelated Task")
            allowed_roles = {
                "selection": {"synthesis", "critic"},
                "critic": {"critic"},
                "conflict": {"conflict", "critic"},
                "synthesis": {"synthesis", "critic"},
            }
            if a.role not in allowed_roles[hold["purpose"]]:
                raise BudgetError("tail allocation changed its protected purpose")
            for snapshot in self.ledger._chain(a.account_id):
                for dimension in totals:
                    deltas[snapshot.account_id][dimension] += getattr(a, dimension)
                deltas[snapshot.account_id]["attempts"] += int(a.counts_attempt)
        for account_id, amounts in deltas.items():
            snapshot = self.ledger.account(account_id)
            for dimension, amount in amounts.items():
                remaining = getattr(snapshot, "remaining_" + dimension)()
                if remaining is not None and amount > remaining:
                    raise BudgetExhausted(account_id, dimension, amount, remaining)
        # All validations precede these writes. Caller commits the actual subjects
        # in this same transaction; there is no release/await/re-reserve window.
        for snapshot in self.ledger._chain(hold["account_id"]):
            self.ledger._apply(
                snapshot.account_id,
                **{"reserved_" + dimension: -amount for dimension, amount in totals.items()},
                reserved_attempts=-attempts,
            )
        self.store.connection.execute(
            "UPDATE budget_tail_holds SET remaining_attempts=remaining_attempts-?,updated_at=?"
            " WHERE hold_id=?",
            (attempts, self.store.now, hold_id),
        )
        self.store.connection.execute(
            "UPDATE budget_reservations SET reserved_tokens=reserved_tokens-?,"
            "reserved_cost_micros=reserved_cost_micros-?,"
            "reserved_tool_calls=reserved_tool_calls-?,updated_at=? WHERE subject_id=?",
            (
                totals["tokens"],
                totals["cost_micros"],
                totals["tool_calls"],
                self.store.now,
                hold["subject_id"],
            ),
        )
        reservations = []
        for a in ordered:
            reservations.append(
                self.ledger.reserve(
                    account_id=a.account_id,
                    subject_id=a.subject_id,
                    mission_id=hold["mission_id"],
                    tokens=a.tokens,
                    cost_micros=a.cost_micros,
                    tool_calls=a.tool_calls,
                    counts_attempt=a.counts_attempt,
                )
            )
        receipt: dict[str, Any] = dict(
            hold_id=hold_id,
            transfer_id=transfer_id,
            task_revision=task_revision,
            allocations=[asdict(a) for a in ordered],
            reservations=reservations,
        )
        self.store.connection.execute(
            "INSERT INTO budget_tail_transfers VALUES(?,?,?,?,?)",
            (hold_id, transfer_id, request, canonical_json(receipt), self.store.now),
        )
        return receipt

    def release_tail(self, hold_id: str, *, task_revision: str, reason: str) -> dict[str, Any]:
        self._transaction_required()
        _text(reason, "release reason")
        hold = self.store.connection.execute(
            "SELECT * FROM budget_tail_holds WHERE hold_id=?",
            (hold_id,),
        ).fetchone()
        if hold is None or hold["task_revision"] != task_revision:
            raise BudgetError("tail release identity differs")
        if hold["state"] == "RELEASED":
            if hold["release_reason"] != reason:
                raise BudgetError("tail release replay changed reason")
            result = self.ledger.reservation(hold["subject_id"])
            assert result is not None
            return result
        result = self.ledger.settle(subject_id=hold["subject_id"])
        for snapshot in self.ledger._chain(hold["account_id"]):
            if hold["remaining_attempts"]:
                self.ledger._apply(
                    snapshot.account_id, reserved_attempts=-hold["remaining_attempts"]
                )
        self.store.connection.execute(
            "UPDATE budget_tail_holds SET state='RELEASED',remaining_attempts=0,"
            "release_reason=?,updated_at=?"
            " WHERE hold_id=?",
            (reason, self.store.now, hold_id),
        )
        return result


__all__ = ("TailReserve", "TailAllocation", "TailBudgetLedger")
