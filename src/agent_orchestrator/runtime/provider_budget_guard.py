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
import json
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from simple_harness.contracts import canonical_json
from simple_harness.execution.budget import FrozenPriceEstimator
from simple_harness.execution.provider_admission import (
    ProviderAdmissionDenied,
    ProviderAdmissionFailure,
    ProviderAdmissionTicket,
    TokenEstimatorPort,
)
from simple_harness.execution.provider_invocations import provider_request_fingerprint

from ..contracts import TERMINAL_ATTEMPT, TERMINAL_MISSION, TERMINAL_TASK, TaskStatus
from ..governance.budgets import BudgetError, BudgetExhausted
from ..governance.provider_prices import ProviderPrice
from .first_request_budget import (
    INPUT_CAP_PROTOCOL,
    FirstRequestBudgetUnknown,
    FirstRequestInputCapExceeded,
    ProviderInputCap,
    enforce_final_wire_input_cap,
    frozen_provider_input_cap,
)

HELD = ("RESERVED", "HANDED_OFF", "UNKNOWN")


def _deny(
    reason: str, *, reason_code: str = "authority_rejected", **detail
) -> ProviderAdmissionDenied:
    return ProviderAdmissionDenied(
        public_message=reason,
        admission_detail=ProviderAdmissionFailure(reason_code=reason_code, **detail),
    )


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

    def _accepted_fragment_manager(self, intent, task) -> bool:
        # A verified fragment completes before Manager can reconnect consumers.
        # This completed Task is evidence, not Manager's live service authority.
        # Require the original projection and accepted Result, never just a label.
        summary = intent.config.get("validated_fragment")
        if (
            intent.kind != "manager"
            or task.status is not TaskStatus.COMPLETED
            or not isinstance(summary, Mapping)
            or summary.get("available") is not True
            or summary.get("validation_task_id") != task.id
            or not task.accepted_result_id
            or summary.get("validation_result_id") != task.accepted_result_id
            or intent.config.get("result_id") != task.accepted_result_id
        ):
            return False
        result = self.store.get_result(task.accepted_result_id)
        if (
            result is None
            or result.verdict != "PASS"
            or result.verification_state != "DONE"
            or result.envelope.attempt_id != intent.config.get("attempt_id")
        ):
            return False
        return any(
            receipt.get("validation_task_id") == task.id
            and receipt.get("fragment_id") == summary.get("fragment_id")
            and receipt.get("projection_receipt_id") == summary.get("projection_receipt_id")
            for receipt in self.store.list_fragment_validations(intent.mission_id)
        )

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
        elif intent.kind == "manager":
            # Manager owns a separate Mission-funded service turn. Its Attempt
            # reference is historical evidence (often RETRY_WAIT), not the live
            # parent authority required by a Critic. Resolve exact durable IDs;
            # never infer ownership from the composite subject's spelling.
            if not isinstance(task_id, str) or not task_id:
                raise _deny("provider Manager has no explicit Task identity")
            evidence_attempt_id = intent.config.get("attempt_id")
            if evidence_attempt_id is not None:
                if not isinstance(evidence_attempt_id, str) or not evidence_attempt_id:
                    raise _deny("provider Manager Attempt reference is invalid")
                evidence_attempt = self.store.get_attempt(evidence_attempt_id)
                if (
                    evidence_attempt is None
                    or evidence_attempt.mission_id != mission.id
                    or evidence_attempt.task_id != task_id
                ):
                    raise _deny("provider Manager Attempt differs from its Task or Mission")
        elif intent.config.get("attempt_id"):
            parent_attempt = self.store.get_attempt(str(intent.config["attempt_id"]))
            if parent_attempt is not None:
                if parent_attempt.status in TERMINAL_ATTEMPT:
                    raise _deny("provider parent Attempt is terminal")
                task_id = parent_attempt.task_id
        if task_id:
            task = self.store.get_task(str(task_id))
            if (
                task is None
                or task.mission_id != mission.id
                or (
                    task.status in TERMINAL_TASK
                    and not self._accepted_fragment_manager(intent, task)
                )
            ):
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

    def grow(self, reservation, *, required: int, required_cost_micros: int = 0) -> None:
        if (required <= reservation["reserved_tokens"]
                and required_cost_micros <= reservation["reserved_cost_micros"]):
            return
        try:
            if self.commit.grow_system_worker_allowance(
                reservation["subject_id"], tokens=required, cost_micros=required_cost_micros,
            ):
                return
        except BudgetExhausted:
            raise
        except BudgetError as exc:
            raise _deny(str(exc)) from exc
        self.commit.ledger.grow(
            subject_id=reservation["subject_id"],
            tokens=required,
            cost_micros=required_cost_micros,
        )


class ProviderBudgetGuard:
    supports_priced_budgets = True
    input_cap_protocol = INPUT_CAP_PROTOCOL

    def __init__(
        self,
        commit,
        *,
        owner: str,
        estimator: TokenEstimatorPort,
        max_slots: int,
        poll_seconds: float = 0.01,
        priced: bool = False,
        price_tables: Mapping[str, FrozenPriceEstimator | None] | None = None,
        profile_slots: Mapping[str, int] | None = None,
    ) -> None:
        if type(priced) is not bool:
            raise ValueError("priced must be an explicit boolean")
        self.requires_price = priced
        self._price_tables = None if price_tables is None else MappingProxyType(dict(price_tables))
        if self.price_tables is not None and any(
            not isinstance(key, str)
            or not key
            or (value is not None and not isinstance(value, FrozenPriceEstimator))
            for key, value in self.price_tables.items()
        ):
            raise ValueError("price tables must be explicitly frozen per profile")
        if priced and not self.price_tables:
            raise ValueError("priced admission requires frozen profile price tables")
        if not owner or not callable(getattr(estimator, "estimate_input_tokens", None)):
            raise ValueError("provider admission requires owner and an explicit estimator")
        for name in ("fingerprint", "bound_protocol"):
            if not isinstance(getattr(estimator, name, None), str) or not getattr(estimator, name):
                raise ValueError(f"provider estimator requires {name}")
        if type(getattr(estimator, "requires_prior_output_reserve", None)) is not bool:
            raise ValueError("estimator must declare its prior-output protocol")
        self.max_slots = _tokens(max_slots, "physical slots", positive=True)
        self.profile_slots = (
            None if profile_slots is None else MappingProxyType(dict(profile_slots))
        )
        if self.profile_slots is not None and (
            not self.profile_slots
            or any(
                not isinstance(key, str) or not key or type(value) is not int or value < 1
                for key, value in self.profile_slots.items()
            )
        ):
            raise ValueError("profile physical slots require named positive integer limits")
        if not 0 < poll_seconds <= 1:
            raise ValueError("provider admission polling must be in (0,1]")
        self.estimator = estimator
        self.fingerprint = (
            "provider-budget-admission-v2:"
            + sha256(
                canonical_json(
                    {
                        "estimator": estimator.fingerprint,
                        "protocol": estimator.bound_protocol,
                        "prior_output": estimator.requires_prior_output_reserve,
                        "max_slots": max_slots,
                        **(
                            {"profile_slots": dict(self.profile_slots)}
                            if self.profile_slots is not None
                            else {}
                        ),
                        "version": 2,
                        "requires_price": priced,
                        "prices": None
                        if self.price_tables is None
                        else {
                            key: None if value is None else value.snapshot_json()
                            for key, value in self.price_tables.items()
                        },
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

    @property
    def price_tables(self) -> Mapping[str, FrozenPriceEstimator | None] | None:
        return self._price_tables

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
        except ProviderAdmissionDenied as exc:
            exc.admission_detail = replace(
                exc.admission_detail,
                invocation_id=record.invocation_id,
                handoff_ordinal=record.handoff_attempt + 1,
                bound_protocol=self.estimator.bound_protocol,
            )
            exc.detail = exc.admission_detail.to_json()
            raise
        finally:
            self._waiting.pop(record.invocation_id, None)

    def waiting_for_slot(self, *, agent_id: str, turn_id: str) -> bool:
        """Actual local waiters only; not a synthetic SDK progress increment."""
        return (agent_id, turn_id) in self._waiting.values()

    async def _acquire(
        self, *, request, record, cancel, uow, execution_lease
    ) -> ProviderAdmissionTicket:
        try:
            sdk_price = ProviderPrice.from_record(record)
        except BudgetError as exc:
            raise _deny(str(exc)) from exc
        price_json = None if sdk_price is None else sdk_price.json
        price_digest = None if sdk_price is None else sdk_price.digest
        # Resolve identities from SDK records, never provider-message metadata.
        binding = uow.read_agent_binding_for_run(record.run_id.value)
        turn = uow.read_open_agent_turn(record.run_id.value)
        if binding is None or turn is None or binding.agent_id != turn.agent_id:
            raise _deny("provider admission requires a live SDK Agent turn")
        profile = binding.config_json.get("model_profile_ref")
        if self.profile_slots is not None and profile not in self.profile_slots:
            raise _deny("provider profile has no declared physical slot limit")
        if self.price_tables is not None and profile not in self.price_tables:
            raise _deny("provider profile has no declared price contract")
        expected_price = None if self.price_tables is None else self.price_tables[profile]
        if expected_price is None:
            sentinel = FrozenPriceEstimator("consumer-v1", "consumer", 0, 0)
            if sdk_price is not None and sdk_price.digest != sentinel.snapshot_digest:
                raise _deny("unpriced profile received a different frozen provider price")
            if self.requires_price:
                raise _deny("priced admission requires a priced profile")
            price = None
        else:
            if sdk_price is None or sdk_price.digest != expected_price.snapshot_digest:
                raise _deny(
                    "provider price differs from the frozen profile price",
                    reason_code="price_mismatch",
                )
            price = sdk_price
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
            raise _deny(
                "provider input estimator unavailable for this request",
                reason_code="estimator_unavailable",
            ) from exc
        output = _tokens(request.max_output_tokens, "maximum output", positive=True)
        wire_hash = provider_request_fingerprint(request)
        ticket = ProviderAdmissionTicket(
            record.invocation_id, record.handoff_attempt + 1, wire_hash, self.fingerprint
        )
        while True:
            if self._clock() >= deadline:
                raise _deny(
                    "provider slot wait exceeded the SDK turn deadline", reason_code="deadline"
                )
            if cancel.is_cancelled:
                raise _deny("provider cancelled before admission", reason_code="cancelled")
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
                if (
                    self.profile_slots is not None
                    and intent.config.get("runtime_profile_id") != profile
                ):
                    raise _deny("provider slot profile differs from original dispatch identity")
                if uow.read_agent_turn_cancel(turn.turn_id) is not None:
                    raise _deny("SDK turn cancelled before admission", reason_code="cancelled")
                if intent.kind == "critic" and isinstance(intent.config.get("attempt_id"), str):
                    attempt_id = intent.config["attempt_id"]
                    if intent.subject_id == f"{attempt_id}:critic:1":
                        worker = self.store.get_intent_for_subject(attempt_id)
                        frozen_first = (
                            None if worker is None else worker.config.get("first_critic_budget")
                        )
                        if frozen_first is not None and (
                            not isinstance(frozen_first, Mapping)
                            or intent.config.get("provider_input_cap")
                            != frozen_first.get("provider_input_cap")
                            or intent.config.get("provider_output_ceiling")
                            != frozen_first.get("output_ceiling")
                            or intent.config.get("provider_first_cost_micros")
                            != frozen_first.get("cost_micros")
                        ):
                            raise _deny(
                                "FIRST Critic cap differs from original protected tail",
                                reason_code="input_cap_identity",
                            )
                frozen_cap = intent.config.get("provider_input_cap")
                if frozen_cap is not None:
                    try:
                        cap = ProviderInputCap.from_json(frozen_cap)
                    except (TypeError, ValueError) as exc:
                        raise _deny(
                            "invalid frozen provider input cap", reason_code="input_cap_identity"
                        ) from exc
                    if (
                        intent.kind != "critic"
                        or binding.config_json.get("model_profile_ref") != cap.profile_id
                        or intent.config.get("runtime_profile_id") != cap.profile_id
                        or intent.config.get("model") != cap.model
                        or cap.estimator_fingerprint != self.estimator.fingerprint
                    ):
                        raise _deny(
                            "provider input cap identity differs from frozen intent",
                            reason_code="input_cap_identity",
                        )
                    expected_cap = frozen_provider_input_cap(
                        profile_id=cap.profile_id,
                        model=cap.model,
                        runtime_context=intent.config.get("runtime_context"),
                        estimator_fingerprint=self.estimator.fingerprint,
                    )
                    if isinstance(expected_cap, FirstRequestBudgetUnknown) or expected_cap != cap:
                        raise _deny(
                            "provider input cap differs from frozen context",
                            reason_code="input_cap_identity",
                        )
                    ceiling = intent.config.get("provider_output_ceiling")
                    if type(ceiling) is not int or ceiling < 1 or output > ceiling:
                        raise _deny(
                            "provider output exceeds frozen FIRST Critic ceiling",
                            reason_code="input_cap_identity",
                        )
                    expected_cost = (
                        0 if price is None else price.cost(cap.max_input_tokens, ceiling)
                    )
                    if intent.config.get("provider_first_cost_micros") != expected_cost:
                        raise _deny(
                            "FIRST Critic cost differs from frozen provider price",
                            reason_code="input_cap_identity",
                        )
                    if public_input < 1:
                        raise _deny(
                            "provider estimator returned no input count",
                            reason_code="estimator_unavailable",
                        )
                    # The frozen FIRST floor covers input_budget(), not the context
                    # renderer's optional slack. Refuse slack before any handoff.
                    try:
                        enforce_final_wire_input_cap(
                            provider_input_cap=cap, actual_input_tokens=public_input
                        )
                    except FirstRequestInputCapExceeded as exc:
                        raise _deny(
                            "final provider wire input exceeds its frozen cap",
                            reason_code="provider_input_cap_exceeded",
                            request_tokens=public_input,
                            remaining=cap.max_input_tokens,
                            mission_id=intent.mission_id,
                            subject_id=intent.subject_id,
                        ) from exc
                prior_output = 0
                for previous in uow.list_provider_invocations(record.run_id):
                    previous = uow.read_effective_provider_invocation(previous.invocation_id)
                    assert previous is not None
                    if previous.invocation_id == record.invocation_id:
                        continue
                    if str(previous.state) == "claimed":
                        continue  # a proven never-handed-off request has no usage
                    actual = _usage(previous)
                    if str(previous.state) not in {"succeeded", "failed"} or actual is None:
                        raise _deny(
                            "prior provider usage is unresolved; allowance held",
                            reason_code="usage_unresolved",
                        )
                    previous_price = ProviderPrice.from_record(previous)
                    if (None if previous_price is None else previous_price.digest) != price_digest:
                        raise _deny(
                            "Agent provider price changed from its frozen history",
                            reason_code="price_mismatch",
                        )
                    if (
                        price is not None
                        and previous_price is not None
                        and previous_price.known_charge(
                            previous, input_tokens=actual[0], output_tokens=actual[1]
                        )
                        is None
                    ):
                        raise _deny(
                            "prior provider price is unresolved; allowance held",
                            reason_code="usage_unresolved",
                        )
                    prior_output += actual[1]
                extra = prior_output if self.estimator.requires_prior_output_reserve else 0
                upper = public_input + extra + output
                cost_upper = None if price is None else price.cost(public_input + extra, output)
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
                        "price_json": price_json,
                        "price_digest": price_digest,
                        "cost_upper_micros": cost_upper,
                    }
                    if any(row[key] != value for key, value in expected.items()):
                        raise _deny("released provider grant identity or allowance changed")
                if self.store.connection.execute(
                    "SELECT 1 FROM provider_token_grants"
                    " WHERE mission_id=? AND state='OVERRUN' LIMIT 1",
                    (intent.mission_id,),
                ).fetchone():
                    raise _deny(
                        "observed provider usage exceeded its bound protocol",
                        reason_code="bound_overrun",
                    )
                active = self.store.connection.execute(
                    "SELECT COUNT(*) FROM provider_token_grants"
                    " WHERE state IN ('RESERVED','HANDED_OFF','UNKNOWN')"
                ).fetchone()[0]
                from .legacy_provider_slots import held_legacy_slots

                active += held_legacy_slots(self.store)
                profile_available = True
                if self.profile_slots is not None:
                    # Count durable grants in the shared orchestration transaction:
                    # separate SDK pools cannot each acquire a copy of this allowance.
                    held = self.store.connection.execute(
                        "SELECT i.config_json FROM provider_token_grants g "
                        "LEFT JOIN dispatch_intents i ON i.intent_id=g.intent_id "
                        "WHERE g.state IN ('RESERVED','HANDED_OFF','UNKNOWN')"
                    ).fetchall()
                    held_profiles = []
                    for existing in held:
                        config = json.loads(existing[0]) if existing[0] else {}
                        held_profile = config.get("runtime_profile_id")
                        agent_profile = (config.get("agent_config") or {}).get("model_profile_ref")
                        if held_profile not in self.profile_slots or agent_profile != held_profile:
                            raise _deny(
                                "held provider grant has no confirmed profile identity",
                                reason_code="profile_identity_unknown",
                            )
                        held_profiles.append(held_profile)
                    own_active = (
                        held_profiles.count(profile) + held_legacy_slots(self.store, profile)
                    )
                    profile_available = own_active < self.profile_slots[profile]
                if active < self.max_slots and profile_available:
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
                        costs = self.store.connection.execute(
                            "SELECT actual_cost_micros,cost_upper_micros,state "
                            "FROM provider_token_grants WHERE subject_id=? AND state!='RELEASED'",
                            (intent.subject_id,),
                        ).fetchall()
                        if price is not None and any(c[1] is None for c in costs):
                            raise _deny("priced subject contains unpriced provider history")
                        if price is None and any(c[1] is not None for c in costs):
                            raise _deny("unpriced subject contains priced provider history")
                        spent_cost = sum(c[0] if c[0] is not None else (c[1] or 0) for c in costs)
                        self.adapter.grow(
                            reservation,
                            required=int(spent) + upper,
                            required_cost_micros=spent_cost + (cost_upper or 0),
                        )
                    except BudgetExhausted as exc:
                        raise _deny(
                            str(exc),
                            reason_code="budget_exhausted",
                            account_id=exc.account_id,
                            dimension=exc.dimension,
                            requested=exc.requested,
                            remaining=exc.remaining,
                            mission_id=intent.mission_id,
                            subject_id=intent.subject_id,
                            request_tokens=upper,
                            request_cost_micros=cost_upper,
                        ) from exc
                    self.store.connection.execute(
                        "INSERT INTO provider_token_grants("
                        "invocation_id,handoff_ordinal,mission_id,"
                        "subject_id,agent_id,turn_id,intent_id,owner,sdk_owner,sdk_epoch,"
                        "fingerprint,request_hash,wire_hash,"
                        "public_input_upper,prior_output_upper,output_ceiling,total_upper,"
                        "price_json,price_digest,cost_upper_micros,state,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RESERVED',?,?)"
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
                            price_json,
                            price_digest,
                            cost_upper,
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
                raise _deny("provider cancelled before handoff", reason_code="cancelled")
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
                "actual provider usage exceeded the admitted bound; Mission admission stopped",
                reason_code="bound_overrun",
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
        price = ProviderPrice.from_record(record)
        # Migration v11 contained only token/unpriced grants. Their absent price
        # columns are compatible only with the exact original SDK zero sentinel.
        legacy_unpriced = (
            row["price_digest"] is None
            and row["price_json"] is None
            and row["cost_upper_micros"] is None
            and (
                price is None
                or price.digest
                == FrozenPriceEstimator("consumer-v1", "consumer", 0, 0).snapshot_digest
            )
        )
        if not legacy_unpriced and (
            (None if price is None else price.digest) != row["price_digest"]
            or (None if price is None else price.json) != row["price_json"]
        ):
            raise _deny(
                "observed provider price differs from the admitted snapshot",
                reason_code="price_mismatch",
            )
        if row["cost_upper_micros"] is None:
            price = None  # original declared unpriced profile, not a zero-priced claim
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
        actual_cost = (
            None
            if price is None
            else price.known_charge(record, input_tokens=actual[0], output_tokens=actual[1])
        )
        if price is not None and actual_cost is None:
            self._update(ticket, "UNKNOWN", actual_tokens=total, actual_output_tokens=actual[1])
            return False
        overrun = (
            total > row["total_upper"]
            or actual[1] > row["output_ceiling"]
            or (actual_cost is not None and actual_cost > row["cost_upper_micros"])
        )
        self._update(
            ticket,
            "OVERRUN" if overrun else "SETTLED",
            actual_tokens=total,
            actual_output_tokens=actual[1],
            actual_cost_micros=actual_cost,
        )
        return overrun

    def release_held_grants(
        self, *, intent_id: str, reason: str = "provider_outcome_unknown"
    ) -> int:
        """Release HELD grants for one intent so a later admission is not refused by them.

        P2.3l / N5.  After the bounded unknown-outcome wait the request is treated as
        confirmed-not-started for *grant* accounting: the SDK invocation stays
        UNKNOWN (the truth — it may have reached the model) but the grant no longer
        occupies a slot or the subject's spent envelope.  Called immediately before
        a service-intent re-hand-off, and again when the round is given up.
        """

        del reason
        released = 0
        with self.store.transaction():
            rows = self.store.connection.execute(
                "SELECT * FROM provider_token_grants"
                " WHERE intent_id=? AND state IN ('RESERVED','HANDED_OFF','UNKNOWN')",
                (intent_id,),
            ).fetchall()
            for row in rows:
                ticket = ProviderAdmissionTicket(
                    row["invocation_id"],
                    row["handoff_ordinal"],
                    row["wire_hash"],
                    row["fingerprint"],
                )
                self._update(ticket, "RELEASED")
                released += 1
        return released

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
                record = uow.read_effective_provider_invocation(row["invocation_id"])
                ticket = ProviderAdmissionTicket(
                    row["invocation_id"],
                    row["handoff_ordinal"],
                    row["wire_hash"],
                    row["fingerprint"],
                )
                if record is not None:
                    turn = uow.read_agent_turn(row["turn_id"])
                    intent = self.store.get_intent(row["intent_id"])
                    rehanded = intent is not None and (
                        intent.agent_id != row["agent_id"]
                        or intent.expected_turn_id != row["turn_id"]
                    )
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
                        # P2.3l / N5: re-hand-off rewrites the live intent onto a new
                        # executor.  The abandoned grant is no longer this pool's live
                        # authority — releasing it is the bounded-wait answer, not a
                        # TTL guess, and denying recover here used to lock every later
                        # admission as authority_rejected.
                        if rehanded:
                            self._update(ticket, "RELEASED")
                            continue
                        raise _deny("recovery SDK/intent/grant identities differ")
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
                "actual provider usage exceeded the admitted bound; Mission admission stopped",
                reason_code="bound_overrun",
            )


__all__ = ("ProviderBudgetGuard", "ProviderBudgetCommitAdapter")
