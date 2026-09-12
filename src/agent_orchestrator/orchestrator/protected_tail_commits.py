# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Narrow FIRST/system tail hooks for the existing Commit transactions.

The caller freezes actual routed-profile amounts, calculates the actual next
Attempt ID, and retains all existing dispatch/lease/Task acceptance gates. These
hooks perform no routing, price estimation, submission, acceptance or await.
They must share the caller's transaction with the corresponding real writes.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..contracts import TERMINAL_ATTEMPT, TERMINAL_MISSION, TERMINAL_TASK, ids
from ..governance.budgets import BudgetError
from ..governance.tail_budget import TailAllocation, TailBudgetLedger, TailReserve
from ..planning.candidate_selection import selection_revision


class ProtectedTailCommitsMixin:
    if TYPE_CHECKING:
        from ..governance.budgets import BudgetLedger
        from ..storage.store import Store

        _store: Store
        _ledger: BudgetLedger

    @staticmethod
    def critic_tail_id(attempt_id: str) -> str:
        return "first-critic:" + attempt_id

    @staticmethod
    def system_tail_id(task_id: str) -> str:
        return "system:" + task_id

    @staticmethod
    def _protected_critic_subject(attempt_id: str, subject_id: str) -> None:
        if (
            not isinstance(subject_id, str)
            or re.fullmatch(re.escape(attempt_id) + r":critic:[1-9][0-9]*", subject_id) is None
        ):
            raise BudgetError(
                "Critic subject must belong to its actual Attempt and positive ordinal"
            )

    def protected_tail_revision(self, task_id: str) -> str:
        task = self._store.get_task(task_id)
        if task is None:
            raise BudgetError("protected tail Task does not exist")
        # Semantic contract/policy/domain/constraints; ordinary lease/status
        # updates do not invalidate the protected original Task budget.
        return selection_revision(self._store, task)

    def _protected_tail_task(self, task_id: str, semantic_revision: str):
        if not self._store.connection.in_transaction:
            raise BudgetError("protected tail hook requires its caller's Commit transaction")
        task = self._store.get_task(task_id)
        if task is None:
            raise BudgetError("protected tail Task does not exist")
        mission = self._store.get_mission(task.mission_id)
        if mission is None or mission.status in TERMINAL_MISSION or task.status in TERMINAL_TASK:
            raise BudgetError("protected tail Task or Mission is terminal")
        if self.protected_tail_revision(task_id) != semantic_revision:
            raise BudgetError("protected tail semantic revision changed")
        return task

    def _protected_attempt(self, task_id: str, attempt_id: str, *, allow_next: bool):
        attempt = self._store.get_attempt(attempt_id)
        if attempt is None:
            next_id = ids.attempt_id(task_id, len(self._store.list_attempts(task_id)) + 1)
            if not allow_next or attempt_id != next_id:
                raise BudgetError("protected tail requires the actual Attempt identity")
        elif attempt.task_id != task_id or attempt.status in TERMINAL_ATTEMPT:
            raise BudgetError("protected tail Attempt is foreign or terminal")
        return attempt

    def protected_tail_hold(self, hold_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT * FROM budget_tail_holds WHERE hold_id=?", (hold_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def reserve_critic_tail(
        self,
        *,
        attempt_id: str,
        task_id: str,
        reserve: TailReserve,
        semantic_revision: str,
    ) -> dict[str, Any]:
        """Before Worker reserve, alongside its actual Attempt creation.

        ``reserve`` is the caller's actual routed Critic allowance (both money
        and tokens). A Critic is a service, so it consumes no Attempt count.
        """
        task = self._protected_tail_task(task_id, semantic_revision)
        self._protected_attempt(task_id, attempt_id, allow_next=True)
        if reserve.attempts or reserve.tokens <= 0:
            raise BudgetError("Critic tail requires positive tokens and no Attempt allocation")
        return TailBudgetLedger(self._ledger).reserve_tail(
            self.critic_tail_id(attempt_id),
            f"budget:{task_id}",
            reserve,
            mission_id=task.mission_id,
            task_revision=semantic_revision,
            purpose="critic",
        )

    def consume_critic_tail(
        self,
        *,
        attempt_id: str,
        task_id: str,
        subject_id: str,
        account_id: str,
        reservation: Any,
        semantic_revision: str,
    ) -> dict[str, Any]:
        """After existing-intent replay check, before writing the actual Critic intent.

        Routing may select a different frozen profile for the real call. Its
        exact allowance must fit this original hold; this hook never expands it
        or rewrites prior rates. A retry uses its real new subject identity.
        """
        self._protected_tail_task(task_id, semantic_revision)
        self._protected_attempt(task_id, attempt_id, allow_next=False)
        if account_id != f"budget:{task_id}":
            raise BudgetError("Critic tail account differs from its original Task")
        self._protected_critic_subject(attempt_id, subject_id)
        hold_id = self.critic_tail_id(attempt_id)
        hold = self.protected_tail_hold(hold_id)
        if hold is None or hold["account_id"] != account_id:
            raise BudgetError("Critic has no original same-Task tail hold")
        return TailBudgetLedger(self._ledger).transfer_tail(
            hold_id,
            subject_id,
            [
                TailAllocation(
                    subject_id,
                    account_id,
                    "critic",
                    reservation.tokens,
                    reservation.cost_micros,
                    reservation.tool_calls,
                    counts_attempt=False,
                )
            ],
            task_revision=semantic_revision,
        )

    def reserve_system_tail(
        self,
        *,
        task_id: str,
        reserve: TailReserve,
        semantic_revision: str,
    ) -> dict[str, Any]:
        """After real synthesis/conflict Task and account writes, before graph commit.

        The explicit allowance includes the bounded system Attempt plus its
        required Critic. No imaginary future Task/account is created here.
        """
        task = self._protected_tail_task(task_id, semantic_revision)
        if task.kind not in {"conflict", "synthesis"} or reserve.attempts < 1:
            raise BudgetError("system tail needs an actual system Task and protected Attempt")
        return TailBudgetLedger(self._ledger).reserve_tail(
            self.system_tail_id(task_id),
            f"budget:{task_id}",
            reserve,
            mission_id=task.mission_id,
            task_revision=semantic_revision,
            purpose=task.kind,
        )

    def consume_system_tail(
        self,
        *,
        task_id: str,
        subject_id: str,
        reservation: Any,
        semantic_revision: str,
        attempt_id: str,
        critic: bool = False,
    ) -> dict[str, Any]:
        """Transfer to one actual system Attempt, or later to its actual Critic.

        The caller creates the Attempt/intent in the same transaction and skips
        its ordinary reserve. Actual system Attempts still consume the original
        max_attempts; Critic allocation leaves remaining protected counts alone.
        """
        task = self._protected_tail_task(task_id, semantic_revision)
        if task.kind not in {"conflict", "synthesis"} or type(critic) is not bool:
            raise BudgetError("system tail needs an actual system Task and role")
        self._protected_attempt(task_id, attempt_id, allow_next=not critic)
        if critic:
            self._protected_critic_subject(attempt_id, subject_id)
        if not critic and subject_id != attempt_id:
            raise BudgetError("system Attempt subject differs from actual Attempt ID")
        return TailBudgetLedger(self._ledger).transfer_tail(
            self.system_tail_id(task_id),
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

    def release_terminal_tail_holds(
        self,
        *,
        mission_id: str,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> list[str]:
        """Release only unused FIRST/system holds whose actual subject stopped.

        This is safe after Mission/Task cancellation and actual Attempt terminal
        transitions. It does not settle or release any transferred reservation;
        those real subjects retain their UNKNOWN/settlement obligations. COMPARE
        round holds remain owned by the selection lifecycle.
        """
        if not self._store.connection.in_transaction:
            raise BudgetError("terminal tail release requires its Commit transaction")
        mission = self._store.get_mission(mission_id)
        if mission is None:
            raise BudgetError("terminal tail Mission does not exist")
        if task_id is not None:
            task = self._store.get_task(task_id)
            if task is None or task.mission_id != mission_id:
                raise BudgetError("terminal tail Task differs from Mission")
        if attempt_id is not None:
            attempt = self._store.get_attempt(attempt_id)
            if (
                attempt is None
                or attempt.mission_id != mission_id
                or (task_id is not None and attempt.task_id != task_id)
            ):
                raise BudgetError("terminal tail Attempt differs from Task/Mission")
        released = []
        for row in self._store.connection.execute(
            "SELECT * FROM budget_tail_holds WHERE mission_id=? AND state='HELD' ORDER BY hold_id",
            (mission_id,),
        ).fetchall():
            origin_id = row["account_id"].removeprefix("budget:")
            if task_id is not None and origin_id != task_id:
                continue
            origin = self._store.get_task(origin_id)
            if origin is None or origin.mission_id != mission_id:
                raise BudgetError("terminal tail has no original Task")
            parent = None
            if row["hold_id"].startswith("first-critic:") and row["purpose"] == "critic":
                parent_id = row["hold_id"].removeprefix("first-critic:")
                parent = self._store.get_attempt(parent_id)
                if parent is None or parent.task_id != origin_id:
                    raise BudgetError("terminal Critic tail has no actual parent Attempt")
                if attempt_id is not None and parent.id != attempt_id:
                    continue
            elif row["hold_id"] == self.system_tail_id(origin_id) and row["purpose"] == origin.kind:
                if attempt_id is not None:
                    continue  # another system Attempt may still need this Task hold
            else:
                continue  # not owned by FIRST/system hooks (e.g. COMPARE)
            stopped = (
                mission.status in TERMINAL_MISSION
                or origin.status in TERMINAL_TASK
                or (parent is not None and parent.status in TERMINAL_ATTEMPT)
            )
            if not stopped:
                continue
            # A hold must never be a real usage subject. Preserve even malformed
            # data for diagnosis rather than writing a fabricated zero settlement.
            has_fact = self._store.connection.execute(
                "SELECT 1 FROM imported_usage WHERE subject_id=? LIMIT 1",
                (row["subject_id"],),
            ).fetchone()
            has_grant = self._store.connection.execute(
                "SELECT 1 FROM provider_token_grants WHERE subject_id=? LIMIT 1",
                (row["subject_id"],),
            ).fetchone()
            if has_fact is not None or has_grant is not None:
                raise BudgetError("terminal tail contains actual usage; cannot release as unused")
            TailBudgetLedger(self._ledger).release_tail(
                row["hold_id"],
                task_revision=row["task_revision"],
                reason="protected_subject_terminal",
            )
            released.append(row["hold_id"])
        return released


__all__ = ("ProtectedTailCommitsMixin",)
