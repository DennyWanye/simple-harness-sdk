# SPDX-License-Identifier: Apache-2.0
"""Bounded durable inbox consumption inside the original Orchestrator tick.

No tasks, pools or scheduler are created here. Each adapter must call the
original business writer; this coordinator owns only claim/prepare/ACK order.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..assurance.codec import AssuranceError, integer, text
from ..assurance.expiry import AssuranceExpiry
from ..assurance.refs import AssuranceRef
from ..contracts import Event
from ..governance.budgets import BudgetError
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import CONSUMERS, AssuranceWorkStore, WorkClaim, WorkTarget
from ..storage.store import StoreConflict
from .assurance_clock import observe_assurance_clock


@dataclass(frozen=True, slots=True)
class PreparedAssuranceWork:
    # Synchronous final-lock validator + ORIGINAL effect/receipt writer.
    commit: Callable[[], AssuranceRef]
    rejected: bool = False

    def __post_init__(self) -> None:
        if not callable(self.commit) or inspect.iscoroutinefunction(self.commit):
            raise AssuranceError("ASSURANCE_SYNC_COMMIT_REQUIRED")
        if type(self.rejected) is not bool:
            raise AssuranceError("ASSURANCE_PREPARED_RESULT_INVALID")


@dataclass(frozen=True, slots=True)
class AssuranceWait:
    reason: str
    not_before_ms: int

    def __post_init__(self) -> None:
        text(self.reason)
        integer(self.not_before_ms)


class AssuranceConsumer(Protocol):
    def classify(self, event: Event) -> Sequence[WorkTarget]:
        """Pure bounded metadata classification, under the cursor transaction."""
        ...

    async def prepare(self, claim: WorkClaim) -> PreparedAssuranceWork | AssuranceWait:
        """Bounded CAS/check/closure preparation outside the Store transaction.

        Never await a Provider call here: the original dispatch_intent submits
        it, a later actual runtime receipt wakes REVIEW. Preparing that intent
        and collecting a finished turn are separate durable work items.
        """
        ...


class AssuranceTick:
    def __init__(
        self,
        orchestrator: Any,
        *,
        consumers: Mapping[str, AssuranceConsumer],
        root_incarnation_id: str,
        require_execution_root: Callable[[], None],
        tenant_id: str,
        mission_limit: int = 8,
        prepare_timeout_ms: int = 20_000,
        prepare_lease_ms: int = 30_000,
    ) -> None:
        if set(consumers) != CONSUMERS or any(
            not callable(getattr(adapter, "classify", None))
            or not callable(getattr(adapter, "prepare", None))
            for adapter in consumers.values()
        ):
            raise AssuranceError("ASSURANCE_CONSUMERS_UNBOUND")
        if not callable(require_execution_root):
            raise AssuranceError("ASSURANCE_ROOT_GATE_UNBOUND")
        self.orchestrator = orchestrator
        self.store = orchestrator.store
        if orchestrator.commit.store is not self.store:
            raise AssuranceError("ASSURANCE_STORE_MISMATCH")
        self.consumers = dict(consumers)
        self.root_incarnation_id = text(root_incarnation_id)
        self.require_execution_root = require_execution_root
        self.tenant_id = text(tenant_id)
        self.mission_limit = integer(mission_limit, minimum=1, maximum=8)
        self.prepare_timeout_ms = integer(prepare_timeout_ms, minimum=1, maximum=299_000)
        self.prepare_lease_ms = integer(
            prepare_lease_ms, minimum=self.prepare_timeout_ms + 1_000, maximum=300_000
        )
        self.work = AssuranceWorkStore(self.store)
        self.expiry = AssuranceExpiry(self.store)
        self.owner = text(orchestrator._owner)
        self._after_mission = ""

    def _missions(self) -> tuple[str, ...]:
        # Include terminal Missions: expiry, late accounting and NOTIFY survive
        # business completion. Rotate within this deployment's authenticated tenant.
        rows = self.store.connection.execute(
            "SELECT b.mission_id FROM assurance_mission_bindings b "
            "JOIN missions m ON m.mission_id=b.mission_id WHERE m.tenant_id=? "
            "ORDER BY (b.mission_id<=?),b.mission_id LIMIT ?",
            (self.tenant_id, self._after_mission, self.mission_limit),
        ).fetchall()
        missions = tuple(row[0] for row in rows)
        if missions:
            self._after_mission = missions[-1]
        return missions

    def has_pending(self) -> bool:
        # Future deadlines are durable. MANUAL_REQUIRED alone does not spin the
        # run-until-idle loop; a real new event can reopen the same logical work.
        return (
            self.store.connection.execute(
                "SELECT 1 FROM assurance_pending_work w JOIN missions m USING(mission_id) "
                "WHERE m.tenant_id=? AND w.state IN ('PENDING','RUNNING','WAITING') "
                "AND (w.wait_reason IS NULL OR w.wait_reason<>'MANUAL_REQUIRED') LIMIT 1",
                (self.tenant_id,),
            ).fetchone()
            is not None
        )

    def _settlement_time(self, claim: WorkClaim) -> int | None:
        """Re-observe after every await, before committing or moving deadlines."""
        now_ms = int(self.store.now * 1000)
        clock = observe_assurance_clock(self.orchestrator.commit, now_ms=now_ms)
        if clock.state == "STABLE":
            return now_ms
        try:
            self.work.wait(
                claim,
                now_ms=clock.wall_high_ms,
                reason="TIME_DISCONTINUITY",
                not_before_ms=clock.wall_high_ms,
            )
        except StoreConflict:
            pass  # expired/superseded ownership is not repaired by moving time
        return None

    async def tick(self) -> bool:
        self.require_execution_root()
        if self.store.connection.in_transaction:
            raise AssuranceError("ASSURANCE_PREPARATION_INSIDE_TRANSACTION")
        missions = self._missions()
        if not missions:
            return False
        now_ms = int(self.store.now * 1000)
        clock = observe_assurance_clock(self.orchestrator.commit, now_ms=now_ms)
        coordination_ms = max(now_ms, clock.wall_high_ms)
        progressed = False
        # Ingest every consumer before expensive preparation. One budget-blocked
        # review cannot prevent later revocation from entering the durable inbox.
        for mission_id in missions:
            if AssuranceStore(self.store).lane(mission_id) != "ASSURANCE_1_1":
                raise AssuranceError("ASSURANCE_CREATION_LANE_MISMATCH")
            progressed = (
                bool(
                    self.expiry.emit_due(
                        mission_id,
                        root_incarnation_id=self.root_incarnation_id,
                        now_ms=now_ms,
                    )
                )
                or progressed
            )
            for consumer in sorted(CONSUMERS):
                cursor = self.store.connection.execute(
                    "SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer=?",
                    (mission_id, consumer),
                ).fetchone()
                if cursor is None:
                    # Recovery from a lost cursor replays original durable Events.
                    # It never guesses a latest seq or resets pending budgets.
                    self.work.initialize_cursor(
                        mission_id, consumer, activation_seq=0, now_ms=coordination_ms
                    )
                    cursor = self.store.connection.execute(
                        "SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer=?",
                        (mission_id, consumer),
                    ).fetchone()
                adapter = self.consumers[consumer]
                try:
                    seq = self.work.ingest(
                        mission_id,
                        consumer,
                        expected_version=cursor["row_version"],
                        classify=lambda event, _consumer, a=adapter: a.classify(event),
                        now_ms=coordination_ms,
                    )
                except StoreConflict:
                    continue
                progressed = seq != cursor["last_event_seq"] or progressed
        if clock.state != "STABLE":
            # Raw/accounting stays on the original collectors. Ingest new facts
            # without acquiring use claims against a rolled-back clock.
            return progressed
        # At most eight work items per consumer across this whole tick; claim
        # one at a time so asynchronous preparation never expires a queued batch.
        for consumer in sorted(CONSUMERS):
            remaining = 8
            for mission_id in missions:
                if remaining == 0:
                    break
                self.require_execution_root()
                claim_now = int(self.store.now * 1000)
                if (
                    observe_assurance_clock(self.orchestrator.commit, now_ms=claim_now).state
                    != "STABLE"
                ):
                    return progressed
                claims = self.work.claim_due(
                    mission_id,
                    consumer,
                    owner=self.owner,
                    now_ms=claim_now,
                    lease_ms=self.prepare_lease_ms,
                    limit=1,
                )
                for claim in claims:
                    remaining -= 1
                    try:
                        async with asyncio.timeout(self.prepare_timeout_ms / 1000):
                            prepared = await self.consumers[consumer].prepare(claim)
                        self.require_execution_root()
                        now_ms = self._settlement_time(claim)
                        if now_ms is None:
                            return progressed
                        if isinstance(prepared, AssuranceWait):
                            if prepared.reason == "RECHECK_REQUIRED":
                                self.work.recheck(claim, now_ms=now_ms)
                            else:
                                self.work.wait(
                                    claim,
                                    now_ms=now_ms,
                                    reason=prepared.reason,
                                    not_before_ms=max(now_ms, prepared.not_before_ms),
                                )
                        elif isinstance(prepared, PreparedAssuranceWork):

                            def effect() -> Any:
                                self.require_execution_root()
                                value = prepared.commit()
                                if inspect.isawaitable(value):
                                    if inspect.iscoroutine(value):
                                        value.close()
                                    raise AssuranceError("ASSURANCE_ASYNC_COMMIT_FORBIDDEN")
                                if not isinstance(value, AssuranceRef):
                                    raise AssuranceError("WORK_COMMIT_RECEIPT_REQUIRED")
                                receipt = AssuranceStore(self.store)._receipt(value)
                                if receipt.get("mission_id") != claim.mission_id:
                                    raise AssuranceError("WORK_COMMIT_RECEIPT_SCOPE_MISMATCH")
                                return value

                            self.work.commit(
                                claim, now_ms=now_ms, effect=effect, rejected=prepared.rejected
                            )
                        else:
                            raise AssuranceError("ASSURANCE_PREPARED_RESULT_INVALID")
                        progressed = True
                    except StoreConflict:
                        # A new target or elapsed lease owns the work now. Never
                        # ACK it with a stale computation or duplicate reservation.
                        continue
                    except TimeoutError:
                        # Repeated incomplete preparation shares the durable
                        # 32/300s cap. Never reset the budget by creating a job.
                        try:
                            now_ms = self._settlement_time(claim)
                            if now_ms is not None:
                                self.work.recheck(claim, now_ms=now_ms)
                        except StoreConflict:
                            pass
                    except (AssuranceError, BudgetError) as error:
                        # One unavailable source/reservation must not starve the
                        # other consumers or prevent later revocation ingestion.
                        # The same persistent work budget bounds all retries.
                        reason = (
                            error.code
                            if isinstance(error, AssuranceError)
                            else "BUDGET_UNAVAILABLE"
                        )
                        try:
                            now_ms = self._settlement_time(claim)
                            if now_ms is not None:
                                self.work.recheck(claim, now_ms=now_ms, reason=reason)
                        except StoreConflict:
                            pass
        return progressed
