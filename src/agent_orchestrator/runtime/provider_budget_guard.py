# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Per-handoff token admission against the original Task/Mission reservation.

The Commit adapter uses the orchestration transaction as the cancellation fence.
Only synchronous SDK handoff is inside that fence (Orch -> SDK lock order).
Transport and slot waiting are outside both databases. An uncertain request keeps
both its allowance and slot until actual SDK reconciliation, never a TTL guess.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import contextmanager
from hashlib import sha256
from typing import Any

from simple_harness.contracts import canonical_json
from simple_harness.execution.provider_admission import (
    ProviderAdmissionDenied,
    ProviderAdmissionTicket,
    TokenEstimatorPort,
)
from simple_harness.execution.provider_invocations import provider_request_fingerprint

from ..contracts import TERMINAL_ATTEMPT, TERMINAL_MISSION, TERMINAL_TASK
from ..governance.budgets import BudgetExhausted

HELD = ("RESERVED", "HANDED_OFF", "UNKNOWN")


def _deny(reason: str) -> ProviderAdmissionDenied:
    return ProviderAdmissionDenied(public_message=reason)


def _tokens(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < int(positive):
        raise _deny(f"invalid {name} for provider admission")
    return value


def _usage(record):
    usage = record.usage_json
    if not isinstance(usage, Mapping) or not isinstance(usage.get("usage"), Mapping):
        return None
    values = usage["usage"]
    return (
        _tokens(values.get("input_tokens"), "actual input"),
        _tokens(values.get("output_tokens"), "actual output"),
    )


class ProviderBudgetCommitAdapter:
    """Narrow accounting writer; shares CommitService's Store and BudgetLedger."""

    def __init__(self, commit, *, owner: str, fingerprint: str) -> None:
        self.commit = commit
        self.store = commit.store
        self.owner = owner
        self.fingerprint = fingerprint

    def authority(self, *, agent_id: str, turn_id: str):
        rows = self.store.connection.execute(
            "SELECT intent_id FROM dispatch_intents WHERE agent_id=? AND expected_turn_id=?",
            (agent_id, turn_id),
        ).fetchall()
        if len(rows) != 1:
            raise _deny("provider request has no unique durable dispatch intent")
        intent = self.store.get_intent(rows[0][0])
        mission = self.store.get_mission(intent.mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            raise _deny("provider subject Mission is terminal")
        if intent.state not in {"AGENT_CREATED", "SUBMITTED"}:
            raise _deny("provider dispatch intent is stopped")
        if intent.config.get("provider_admission_fingerprint") != self.fingerprint:
            raise _deny("provider admission differs from frozen intent")
        task_id = intent.config.get("task_id")
        lease = intent
        if intent.kind == "attempt":
            attempt = self.store.get_attempt(intent.subject_id)
            if attempt is None or attempt.status in TERMINAL_ATTEMPT:
                raise _deny("provider Attempt is terminal")
            task_id = attempt.task_id
            lease = attempt
        elif intent.config.get("attempt_id"):
            parent_attempt = self.store.get_attempt(str(intent.config["attempt_id"]))
            if parent_attempt is not None:
                if parent_attempt.status in TERMINAL_ATTEMPT:
                    raise _deny("provider parent Attempt is terminal")
                task_id = parent_attempt.task_id
        if task_id:
            task = self.store.get_task(str(task_id))
            if task is None or task.mission_id != mission.id or task.status in TERMINAL_TASK:
                raise _deny("provider Task is terminal or differs from Mission")
        # Service intent leases govern pre-submit claiming, not an ongoing SDK
        # turn. SUBMITTED service authority additionally uses the SDK Run lease
        # CAS inside handoff. Attempts have an independently renewed live lease.
        require_live_lease = intent.kind == "attempt" or intent.state != "SUBMITTED"
        if require_live_lease and (
            lease.lease_owner != self.owner
            or (lease.lease_expires_at is None or lease.lease_expires_at <= self.store.now)
        ):
            raise _deny("provider subject lease is expired or owned by another executor")
        reservation = self.commit.ledger.reservation(intent.subject_id)
        if reservation is None or reservation["state"] != "RESERVED":
            raise _deny("provider subject has no live budget reservation")
        return intent, reservation

    def grow(self, reservation, *, required: int) -> None:
        delta = max(0, required - int(reservation["reserved_tokens"]))
        if not delta:
            return
        chain = self.commit.ledger._chain(reservation["account_id"])
        for account in chain:
            remaining = account.remaining_tokens()
            if remaining is not None and delta > remaining:
                raise BudgetExhausted(account.account_id, "tokens", delta, remaining)
        for account in chain:
            self.commit.ledger._apply(account.account_id, reserved_tokens=delta)
        self.store.connection.execute(
            "UPDATE budget_reservations SET reserved_tokens=reserved_tokens+?,updated_at=?"
            " WHERE reservation_id=? AND state='RESERVED'",
            (delta, self.store.now, reservation["reservation_id"]),
        )


class ProviderBudgetGuard:
    supports_priced_budgets = False

    def __init__(
        self,
        commit,
        *,
        owner: str,
        estimator: TokenEstimatorPort,
        max_slots: int,
        poll_seconds: float = 0.01,
        priced: bool = False,
    ) -> None:
        if priced:
            raise ValueError(
                "priced shared provider admission is not supported by this token guard"
            )
        if not owner or not callable(getattr(estimator, "estimate_input_tokens", None)):
            raise ValueError("provider admission requires owner and an explicit estimator")
        for name in ("fingerprint", "bound_protocol"):
            if not isinstance(getattr(estimator, name, None), str) or not getattr(estimator, name):
                raise ValueError(f"provider estimator requires {name}")
        if type(getattr(estimator, "requires_prior_output_reserve", None)) is not bool:
            raise ValueError("estimator must declare its prior-output protocol")
        self.max_slots = _tokens(max_slots, "physical slots", positive=True)
        if not 0 < poll_seconds <= 1:
            raise ValueError("provider admission polling must be in (0,1]")
        self.estimator = estimator
        self.fingerprint = (
            "provider-token-admission-v1:"
            + sha256(
                canonical_json(
                    {
                        "estimator": estimator.fingerprint,
                        "protocol": estimator.bound_protocol,
                        "prior_output": estimator.requires_prior_output_reserve,
                        "max_slots": max_slots,
                        "version": 1,
                    }
                ).encode()
            ).hexdigest()
        )
        self.adapter = ProviderBudgetCommitAdapter(
            commit, owner=owner, fingerprint=self.fingerprint
        )
        self.store = commit.store
        self.poll_seconds = poll_seconds
        self._waiting: dict[str, tuple[str, str]] = {}
        self._clock = time.time

    async def acquire(
        self, *, request, record, cancel, uow, execution_lease
    ) -> ProviderAdmissionTicket:
        try:
            return await self._acquire(
                request=request,
                record=record,
                cancel=cancel,
                uow=uow,
                execution_lease=execution_lease,
            )
        finally:
            self._waiting.pop(record.invocation_id, None)

    def waiting_for_slot(self, *, agent_id: str, turn_id: str) -> bool:
        """Actual local waiters only; not a synthetic SDK progress increment."""
        return (agent_id, turn_id) in self._waiting.values()

    async def _acquire(
        self, *, request, record, cancel, uow, execution_lease
    ) -> ProviderAdmissionTicket:
        snapshot = record.estimator_snapshot
        if isinstance(snapshot, Mapping) and any(
            snapshot.get(name, 0) != 0
            for name in (
                "input_micros_per_million_tokens",
                "output_micros_per_million_tokens",
            )
        ):
            raise _deny("priced provider admission is not supported by this token guard")
        # Resolve identities from SDK records, never provider-message metadata.
        binding = uow.read_agent_binding_for_run(record.run_id.value)
        turn = uow.read_open_agent_turn(record.run_id.value)
        if binding is None or turn is None or binding.agent_id != turn.agent_id:
            raise _deny("provider admission requires a live SDK Agent turn")
        limits = binding.config_json.get("limits", {})
        seconds = limits.get("turn_deadline_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise _deny("provider admission requires a finite SDK turn deadline")
        deadline = turn.created_at + seconds
        self.recover(uow)
        try:
            public_input = _tokens(self.estimator.estimate_input_tokens(request), "input allowance")
        except ProviderAdmissionDenied:
            raise
        except Exception as exc:
            raise _deny("provider input estimator unavailable for this request") from exc
        output = _tokens(request.max_output_tokens, "maximum output", positive=True)
        wire_hash = provider_request_fingerprint(request)
        ticket = ProviderAdmissionTicket(
            record.invocation_id, record.handoff_attempt + 1, wire_hash, self.fingerprint
        )
        while True:
            if self._clock() >= deadline:
                raise _deny("provider slot wait exceeded the SDK turn deadline")
            if cancel.is_cancelled:
                raise _deny("provider cancelled before admission")
            with self.store.transaction():
                actual_lease = uow.read_provider_runtime_lease(record.run_id.value)
                if (
                    actual_lease is None
                    or actual_lease.owner_id != execution_lease.owner_id
                    or actual_lease.epoch != execution_lease.epoch
                    or actual_lease.expires_at <= self._clock()
                ):
                    raise _deny("provider SDK runtime lease is no longer current")
                intent, reservation = self.adapter.authority(
                    agent_id=binding.agent_id, turn_id=turn.turn_id
                )
                if uow.read_agent_turn_cancel(turn.turn_id) is not None:
                    raise _deny("SDK turn cancelled before admission")
                prior_output = 0
                for previous in uow.list_provider_invocations(record.run_id):
                    if previous.invocation_id == record.invocation_id:
                        continue
                    if str(previous.state) == "claimed":
                        continue  # a proven never-handed-off request has no usage
                    actual = _usage(previous)
                    if str(previous.state) not in {"succeeded", "failed"} or actual is None:
                        raise _deny("prior provider usage is unresolved; allowance held")
                    prior_output += actual[1]
                extra = prior_output if self.estimator.requires_prior_output_reserve else 0
                upper = public_input + extra + output
                row = self._row(ticket)
                if row is not None and row["state"] != "RELEASED":
                    raise _deny("provider invocation already owns an admission grant")
                if row is not None:
                    expected = {
                        "mission_id": intent.mission_id,
                        "subject_id": intent.subject_id,
                        "agent_id": binding.agent_id,
                        "turn_id": turn.turn_id,
                        "intent_id": intent.intent_id,
                        "fingerprint": self.fingerprint,
                        "request_hash": record.request_fingerprint,
                        "wire_hash": wire_hash,
                        "public_input_upper": public_input,
                        "prior_output_upper": extra,
                        "output_ceiling": output,
                        "total_upper": upper,
                    }
                    if any(row[key] != value for key, value in expected.items()):
                        raise _deny("released provider grant identity or allowance changed")
                if self.store.connection.execute(
                    "SELECT 1 FROM provider_token_grants"
                    " WHERE mission_id=? AND state='OVERRUN' LIMIT 1",
                    (intent.mission_id,),
                ).fetchone():
                    raise _deny("observed provider usage exceeded its bound protocol")
                active = self.store.connection.execute(
                    "SELECT COUNT(*) FROM provider_token_grants"
                    " WHERE state IN ('RESERVED','HANDED_OFF','UNKNOWN')"
                ).fetchone()[0]
                if active < self.max_slots:
                    spent = self.store.connection.execute(
                        "SELECT COALESCE(SUM(CASE WHEN actual_tokens IS NOT NULL THEN actual_tokens"
                        " ELSE total_upper END),0) FROM provider_token_grants"
                        " WHERE subject_id=? AND state!='RELEASED'",
                        (intent.subject_id,),
                    ).fetchone()[0]
                    # Previous unguarded history cannot be silently omitted from the envelope.
                    known_ids = {
                        r[0]
                        for r in self.store.connection.execute(
                            "SELECT invocation_id FROM provider_token_grants WHERE agent_id=?",
                            (binding.agent_id,),
                        )
                    }
                    if any(
                        p.handoff_attempt and p.invocation_id not in known_ids
                        for p in uow.list_provider_invocations(record.run_id)
                        if p.invocation_id != record.invocation_id
                    ):
                        raise _deny(
                            "existing Agent history predates the provider admission contract"
                        )
                    try:
                        self.adapter.grow(reservation, required=int(spent) + upper)
                    except BudgetExhausted as exc:
                        raise _deny(str(exc)) from exc
                    self.store.connection.execute(
                        "INSERT INTO provider_token_grants("
                        "invocation_id,handoff_ordinal,mission_id,"
                        "subject_id,agent_id,turn_id,intent_id,owner,sdk_owner,sdk_epoch,"
                        "fingerprint,request_hash,wire_hash,"
                        "public_input_upper,prior_output_upper,output_ceiling,total_upper,"
                        "state,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RESERVED',?,?)"
                        " ON CONFLICT(invocation_id,handoff_ordinal) DO UPDATE SET "
                        "owner=excluded.owner,"
                        "sdk_owner=excluded.sdk_owner,sdk_epoch=excluded.sdk_epoch,"
                        "state='RESERVED',updated_at=excluded.updated_at,version=version+1"
                        " WHERE provider_token_grants.state='RELEASED'",
                        (
                            ticket.invocation_id,
                            ticket.handoff_ordinal,
                            intent.mission_id,
                            intent.subject_id,
                            binding.agent_id,
                            turn.turn_id,
                            intent.intent_id,
                            self.adapter.owner,
                            execution_lease.owner_id,
                            execution_lease.epoch,
                            self.fingerprint,
                            record.request_fingerprint,
                            wire_hash,
                            public_input,
                            extra,
                            output,
                            upper,
                            self.store.now,
                            self.store.now,
                        ),
                    )
                    return ticket
            self._waiting[record.invocation_id] = (binding.agent_id, turn.turn_id)
            await asyncio.sleep(self.poll_seconds)

    def _row(self, ticket):
        return self.store.connection.execute(
            "SELECT * FROM provider_token_grants WHERE invocation_id=? AND handoff_ordinal=?",
            (ticket.invocation_id, ticket.handoff_ordinal),
        ).fetchone()

    def _update(self, ticket, state: str, **values: Any) -> None:
        assignments = ",".join(f"{key}=?" for key in values)
        self.store.connection.execute(
            "UPDATE provider_token_grants SET state=?,version=version+1,updated_at=?"
            + ("," + assignments if assignments else "")
            + " WHERE invocation_id=? AND handoff_ordinal=?",
            (state, self.store.now, *values.values(), ticket.invocation_id, ticket.handoff_ordinal),
        )

    @contextmanager
    def handoff(self, ticket, *, request, cancel):
        with self.store.transaction():
            row = self._row(ticket)
            if row is None or row["state"] != "RESERVED" or row["owner"] != self.adapter.owner:
                raise _deny("provider grant is not owned and reserved")
            if cancel.is_cancelled:
                raise _deny("provider cancelled before handoff")
            if (
                ticket.authority_fingerprint != self.fingerprint
                or row["fingerprint"] != self.fingerprint
                or row["wire_hash"] != provider_request_fingerprint(request)
            ):
                raise _deny("provider wire changed after admission")
            self.adapter.authority(agent_id=row["agent_id"], turn_id=row["turn_id"])
            # Only synchronous SDK CAS is allowed inside this cancellation fence.
            yield
            self._update(ticket, "HANDED_OFF")

    def observe(self, ticket, *, record) -> None:
        with self.store.transaction():
            overrun = self._observe_in_transaction(ticket, record=record)
        if overrun:
            raise _deny(
                "actual provider usage exceeded the admitted bound; Mission admission stopped"
            )

    def _observe_in_transaction(self, ticket, *, record) -> bool:
        """Record actual usage; the caller raises only after its transaction commits."""
        row = self._row(ticket)
        if row is None or row["state"] in {"SETTLED", "OVERRUN", "RELEASED"}:
            return False
        if record is None:
            return False  # no evidence that a different pool's call never started
        if record.request_fingerprint != row["request_hash"]:
            raise _deny("SDK record differs from admission identity")
        state = str(record.state)
        if record.handoff_attempt < ticket.handoff_ordinal and state == "claimed":
            # This callback runs after the synchronous SDK CAS returned/failed.
            if row["state"] == "RESERVED" and row["owner"] == self.adapter.owner:
                self._update(ticket, "RELEASED")
            return False
        if record.handoff_attempt != ticket.handoff_ordinal:
            return False
        actual = _usage(record) if state in {"succeeded", "failed"} else None
        if actual is None:
            if row["state"] != "UNKNOWN":
                self._update(ticket, "UNKNOWN")
            return False
        total = sum(actual)
        overrun = total > row["total_upper"] or actual[1] > row["output_ceiling"]
        self._update(
            ticket,
            "OVERRUN" if overrun else "SETTLED",
            actual_tokens=total,
            actual_output_tokens=actual[1],
        )
        return overrun

    def recover(self, uow) -> None:
        # Read and fence in Orch -> SDK order; no remote reconciliation under this lock.
        overrun = False
        with self.store.transaction():
            rows = self.store.connection.execute(
                "SELECT * FROM provider_token_grants"
                " WHERE state IN ('RESERVED','HANDED_OFF','UNKNOWN')"
            ).fetchall()
            for row in rows:
                binding = uow.read_agent_binding(row["agent_id"])
                if binding is None:
                    continue  # another pool's database is not negative evidence
                record = uow.read_provider_invocation(row["invocation_id"])
                if record is not None:
                    turn = uow.read_agent_turn(row["turn_id"])
                    intent = self.store.get_intent(row["intent_id"])
                    if (
                        record.invocation_id != row["invocation_id"]
                        or record.run_id.value != binding.run_id
                        or record.request_fingerprint != row["request_hash"]
                        or binding.agent_id != row["agent_id"]
                        or turn is None
                        or turn.agent_id != binding.agent_id
                        or intent is None
                        or intent.expected_turn_id != turn.turn_id
                        or intent.agent_id != binding.agent_id
                        or intent.input_id != turn.input_id
                        or intent.subject_id != row["subject_id"]
                        or intent.mission_id != row["mission_id"]
                    ):
                        raise _deny("recovery SDK/intent/grant identities differ")
                ticket = ProviderAdmissionTicket(
                    row["invocation_id"],
                    row["handoff_ordinal"],
                    row["wire_hash"],
                    row["fingerprint"],
                )
                if record is not None and str(record.state) in {"succeeded", "failed"}:
                    overrun = self._observe_in_transaction(ticket, record=record) or overrun
                elif record is not None and record.handoff_attempt >= row["handoff_ordinal"]:
                    resolution = uow.read_reconciliation_resolution(
                        kind="provider",
                        ledger_identity=record.invocation_id,
                        handoff_attempt=row["handoff_ordinal"],
                    )
                    if (
                        resolution is not None
                        and str(resolution.outcome) == "confirmed_not_started"
                    ):
                        self._update(ticket, "RELEASED")
                    elif row["state"] != "UNKNOWN":
                        self._update(ticket, "UNKNOWN")
                elif row["state"] == "RESERVED":
                    # Do not release a live competing owner's pre-handoff grant.
                    intent = self.store.get_intent(row["intent_id"])
                    attempt = self.store.get_attempt(row["subject_id"])
                    mission = self.store.get_mission(row["mission_id"])
                    sdk_lease = uow.read_provider_runtime_lease(binding.run_id)
                    parent_task = None if attempt is None else self.store.get_task(attempt.task_id)
                    if (
                        mission.status in TERMINAL_MISSION
                        or intent is None
                        or intent.state not in {"AGENT_CREATED", "SUBMITTED"}
                        or (
                            attempt is not None
                            and (
                                attempt.status in TERMINAL_ATTEMPT
                                or parent_task is None
                                or parent_task.status in TERMINAL_TASK
                            )
                        )
                        or sdk_lease is None
                        or sdk_lease.owner_id != row["sdk_owner"]
                        or sdk_lease.epoch != row["sdk_epoch"]
                        or sdk_lease.expires_at <= self._clock()
                    ):
                        self._update(ticket, "RELEASED")

        if overrun:
            raise _deny(
                "actual provider usage exceeded the admitted bound; Mission admission stopped"
            )


__all__ = ("ProviderBudgetGuard", "ProviderBudgetCommitAdapter")
