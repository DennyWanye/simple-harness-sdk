"""Default system pool hooks; all mutations share the enclosing Commit transaction."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ..contracts import TERMINAL_MISSION, TERMINAL_TASK
from ..governance.budgets import BudgetError
from ..governance.mission_system_tail import MissionSystemTailLedger
from ..governance.tail_budget import TailReserve
from ..planning.candidate_selection import selection_revision
from ..verification.assessments import mission_contract_revision


def system_cost_upper(tokens, *, rate, physical_calls):
    """Sum of up to 2*C integer ceilings <= ceil(T*max_rate/M)+2*C-1.

    C is the durable AgentLimits call bound, including output-cap retries.
    This bounds SDK-visible priced invocations, not hidden retries inside an
    arbitrary external Provider implementation. No user money cap is enlarged.
    """
    if (
        type(physical_calls) is not int
        or physical_calls <= 0
        or type(tokens) is not int
        or tokens < 0
        or type(rate) is not int
        or rate < 0
    ):
        raise BudgetError("priced system allowance needs a provable physical call bound")
    return 0 if rate == 0 else (tokens * rate + 999_999) // 1_000_000 + 2 * physical_calls - 1


class MissionTailCommitsMixin:
    def _reserve_mission_system_pools(self, mission):
        if self._system_tail_factory is None:
            return  # Bare Commit/old Missions retain their original allowance semantics.
        for purpose in ("conflict", "synthesis"):
            plan = self._system_tail_factory(mission, purpose)
            if plan is None:
                continue
            reserve, binding = plan
            MissionSystemTailLedger(self._ledger).reserve_pool(
                f"{mission.id}:{purpose}",
                mission_id=mission.id,
                purpose=purpose,
                reserve=reserve,
                mission_revision=mission_contract_revision(mission),
                route_binding=binding,
            )

    def system_task_hold(self, task_id):
        if not self._store.has_table("mission_system_tail_tasks"):
            return None
        row = self._store.connection.execute(
            "SELECT * FROM mission_system_tail_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def _transfer_mission_system_pool(self, task):
        if not self._store.has_table("mission_system_tail_pools"):
            return
        pools = MissionSystemTailLedger(self._ledger)
        pool = pools.pool(f"{task.mission_id}:{task.kind}")
        if pool is None:
            return
        if self._system_tail_factory is None:
            raise BudgetError("system pool needs the frozen runtime routing authority")
        mission = self._require_mission(task.mission_id)
        plan = self._system_tail_factory(mission, task.kind)
        if plan is None:
            raise BudgetError("system pool runtime allowance is unavailable")
        _, binding = plan
        original = json.loads(pool["request_json"])["reserve"]
        pools.transfer_to_task_hold(
            pool["pool_id"],
            task_id=task.id,
            reserve=TailReserve(**original),
            semantic_revision=selection_revision(self._store, task),
            route_binding=binding,
        )

    def _consume_mission_system_hold(
        self,
        task,
        *,
        attempt_id,
        subject_id,
        reservation,
        profile_id,
        model,
        agent_config,
        critic=False,
    ):
        row = self.system_task_hold(task.id)
        if row is None:
            return False
        if self._system_tail_factory is None:
            raise BudgetError("system Task needs the frozen runtime routing authority")
        if not isinstance(agent_config, Mapping):
            raise BudgetError("system Task requires the actual frozen Agent limits")
        try:
            critic_ordinal = int(subject_id.rsplit(":", 1)[-1]) if critic else None
        except ValueError as error:
            raise BudgetError("system Critic ordinal is invalid") from error
        plan = self._system_tail_factory(
            self._require_mission(task.mission_id),
            task.kind,
            agent_config=agent_config,
            critic=critic,
            critic_ordinal=critic_ordinal,
        )
        if plan is None:
            raise BudgetError("system Task runtime allowance is unavailable")
        _, binding = plan
        route = binding.critic if critic else binding.worker
        if route is None or route.profile_id != profile_id or route.model != model:
            raise BudgetError("system Task actual route differs from its protected route")
        MissionSystemTailLedger(self._ledger).consume_task_hold(
            task_id=task.id,
            attempt_id=attempt_id,
            subject_id=subject_id,
            reservation=reservation,
            semantic_revision=selection_revision(self._store, task),
            route_binding=binding,
            critic=critic,
        )
        return True

    def _release_terminal_mission_pools(self, mission_id):
        if not self._store.has_table("mission_system_tail_pools"):
            return
        mission = self._require_mission(mission_id)
        if mission.status not in TERMINAL_MISSION:
            return
        pools = MissionSystemTailLedger(self._ledger)
        for row in self._store.connection.execute(
            "SELECT * FROM mission_system_tail_pools WHERE mission_id=? AND state='HELD'",
            (mission_id,),
        ).fetchall():
            # Only the unconsumed pool subject. Transferred UNKNOWN reservations
            # stay owned by their actual Attempt/service and original guard.
            pools.release_unused_pool(
                row["pool_id"], mission_revision=row["mission_revision"], reason="mission_terminal"
            )

    def _return_system_unused_allowance(self, settled):
        """Return only unused original transfers after a FIRST known settlement.

        The caller checks the subject was not already SETTLED. Neither the
        transfer receipt nor actual counters are rewritten; UNKNOWN never reaches
        this hook. Retry can reuse unspent money, never an already-used Attempt.
        """
        task_id = settled["account_id"].removeprefix("budget:")
        row = self.system_task_hold(task_id)
        if row is None:
            return
        task = self._require_task(task_id)
        if (
            task.status in TERMINAL_TASK
            or self._require_mission(task.mission_id).status in TERMINAL_MISSION
        ):
            return
        hold = self.protected_tail_hold(row["hold_id"])
        if hold is None or hold["state"] != "HELD":
            return
        transfer = self._store.connection.execute(
            "SELECT request_json FROM budget_tail_transfers WHERE hold_id=? AND transfer_id=?",
            (row["hold_id"], settled["subject_id"]),
        ).fetchone()
        if transfer is None:
            return
        allocation = next(
            a
            for a in json.loads(transfer[0])["allocations"]
            if a["subject_id"] == settled["subject_id"]
        )
        amounts = {}
        chain = self._ledger._chain(settled["account_id"])
        for dimension in ("tokens", "cost_micros", "tool_calls"):
            amount = max(0, allocation[dimension] - int(settled["settled_" + dimension] or 0))
            for account in chain:
                remaining = getattr(account, "remaining_" + dimension)()
                if remaining is not None:
                    amount = min(amount, max(0, remaining))
            amounts["reserved_" + dimension] = amount
        for account in chain:
            self._ledger._apply(account.account_id, **amounts)
        self._store.connection.execute(
            "UPDATE budget_reservations SET reserved_tokens=reserved_tokens+?,"
            "reserved_cost_micros=reserved_cost_micros+?,"
            "reserved_tool_calls=reserved_tool_calls+?,updated_at=? "
            "WHERE subject_id=? AND state='RESERVED'",
            (
                amounts["reserved_tokens"],
                amounts["reserved_cost_micros"],
                amounts["reserved_tool_calls"],
                self._store.now,
                hold["subject_id"],
            ),
        )
