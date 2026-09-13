# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Deployment slot accounting for original, unguarded Orchestrator pools.

This does not supply token authority, change frozen intent identity, or settle
usage. See plans/2026-09-12-phase3/p34/legacy-context-coexistence.md.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager

from simple_harness.agents import AgentConfig
from simple_harness.agents.base import input_hash_for
from simple_harness.agents.config import config_hash
from simple_harness.agents.contracts import _message_from_json
from simple_harness.contracts import canonical_json
from simple_harness.execution.provider_admission import (
    LocalProviderAdmission,
    ProviderAdmissionDenied,
    ProviderAdmissionFailure,
    ProviderAdmissionTicket,
)
from simple_harness.execution.provider_invocations import provider_request_fingerprint

from ..contracts.models import jsonable, sha256_hex

TABLE = "legacy_provider_slots_v1"
HELD_SQL = "state IN ('RESERVED','HANDED_OFF','UNKNOWN')"


def _require(condition, message):
    if not condition:
        raise ProviderAdmissionDenied(public_message="legacy provider slot: " + message)


def profile_has_frozen_admission(config, profile_id: str) -> bool | None:
    """Read-only tri-state: guarded, unguarded, or no frozen intent yet.

    Context policy absence alone cannot answer this question. Mixed identities
    are not upgradeable by selecting an estimator or by rewriting the intents.
    """
    if not config.orchestrator_db.exists():
        return None
    with sqlite3.connect(config.orchestrator_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dispatch_intents'").fetchone():
            return None
        identities = set()
        for (encoded,) in db.execute("SELECT config_json FROM dispatch_intents"):
            frozen = json.loads(encoded)
            if str(frozen.get("runtime_profile_id") or "default") == profile_id:
                identities.add(frozen.get("provider_admission_fingerprint"))
        if len(identities) > 1:
            raise ValueError("pool has inconsistent frozen provider admission identities")
        return None if not identities else next(iter(identities)) is not None


def held_legacy_slots(store, profile_id=None) -> int:
    """Called within the caller's existing Store reservation transaction."""
    if not store.has_table(TABLE):
        return 0
    return store.connection.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE {HELD_SQL}"
        + (" AND profile_id=?" if profile_id is not None else ""),
        () if profile_id is None else (profile_id,),
    ).fetchone()[0]


def create_slot_table(store):
    # Additive, versioned auxiliary deployment table. No old row is updated.
    with store.transaction():
        store.connection.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            profile_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            handoff_ordinal INTEGER NOT NULL CHECK(handoff_ordinal > 0),
            run_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            wire_hash TEXT,
            sdk_owner TEXT,
            sdk_epoch INTEGER,
            state TEXT NOT NULL CHECK(state IN
                ('RESERVED','HANDED_OFF','UNKNOWN','RELEASED','SETTLED')),
            PRIMARY KEY(profile_id, invocation_id, handoff_ordinal)
        ) STRICT""")


class LegacyProviderSlots(LocalProviderAdmission):
    """Same local identity, durable shared slots, and the original lifecycle fence."""

    def __init__(self, store, *, profile_id, max_slots, profile_slots, fence):
        super().__init__(None, fence)
        self.store = store
        self.profile_id = profile_id
        self.max_slots = max_slots
        self.profile_slots = profile_slots
        self._leases = {}

    def _intent(self, uow, record):
        binding = uow.read_agent_binding_for_run(record.run_id.value)
        _require(binding is not None, "missing original Agent binding")
        rows = self.store.connection.execute(
            "SELECT intent_id FROM dispatch_intents WHERE agent_id=?",
            (binding.agent_id,),
        ).fetchall()
        _require(len(rows) == 1, "missing or ambiguous original intent")
        intent = self.store.get_intent(rows[0][0])
        _require(
            intent.config.get("provider_admission_fingerprint") is None,
            "external admission identity is not legacy",
        )
        _require(
            str(intent.config.get("runtime_profile_id") or "default") == self.profile_id,
            "profile identity differs",
        )
        _require(binding.creation_key == intent.creation_key, "creation identity differs")
        frozen_config = AgentConfig.from_json(dict(intent.config["agent_config"]))
        _require(
            binding.config_hash == config_hash(frozen_config), "frozen Agent config hash differs"
        )
        _require(
            canonical_json(frozen_config.to_json())
            == canonical_json(jsonable(binding.config_json)),
            "frozen Agent config differs",
        )
        turn = uow.read_agent_turn(intent.expected_turn_id)
        _require(
            turn is not None
            and turn.agent_id == binding.agent_id
            and turn.input_id == intent.input_id,
            "missing or different original turn",
        )
        _require(len(uow.list_agent_turns(binding.agent_id)) == 1, "Agent reused across turns")
        message = intent.config["message"]
        _require(sha256_hex(message) == intent.input_hash, "frozen input hash differs")
        actual = _message_from_json(dict(message))
        _require(
            turn.input_hash == input_hash_for(actual)
            and canonical_json(jsonable(turn.input_json))
            == canonical_json({"message": actual.to_dict()}),
            "SDK input differs",
        )
        _require(record.request_json is not None, "missing frozen provider request")
        return binding, turn, intent

    def _row(self, invocation_id, ordinal):
        return self.store.connection.execute(
            f"SELECT * FROM {TABLE} WHERE profile_id=? AND invocation_id=? AND handoff_ordinal=?",
            (self.profile_id, invocation_id, ordinal),
        ).fetchone()

    def _state(self, invocation_id, ordinal, state):
        self.store.connection.execute(
            f"UPDATE {TABLE} SET state=? "
            "WHERE profile_id=? AND invocation_id=? AND handoff_ordinal=?",
            (state, self.profile_id, invocation_id, ordinal),
        )

    def _insert(self, record, binding, turn, intent, ordinal, state, *, wire=None, lease=None):
        self.store.connection.execute(
            f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.profile_id,
                record.invocation_id,
                ordinal,
                record.run_id.value,
                binding.agent_id,
                turn.turn_id,
                intent.intent_id,
                record.request_fingerprint,
                wire,
                None if lease is None else lease.owner_id,
                None if lease is None else lease.epoch,
                state,
            ),
        )

    @staticmethod
    def _terminal_usage(record):
        if str(record.state) not in {"succeeded", "failed"}:
            return False
        usage = record.usage_json
        values = usage.get("usage") if isinstance(usage, Mapping) else None
        return isinstance(values, Mapping) and all(
            type(values.get(key)) is int and values[key] >= 0
            for key in ("input_tokens", "output_tokens")
        )

    def recover(self, uow):
        """Import original calls before startup; never interpret absent rows as zero."""
        with self.store.transaction():
            originals = {
                row[0]: uow.read_provider_invocation(row[0])
                for row in uow.database.connection.execute(
                    "SELECT invocation_id FROM provider_invocations"
                ).fetchall()
            }
            rows = self.store.connection.execute(
                f"SELECT * FROM {TABLE} WHERE profile_id=?",
                (self.profile_id,),
            ).fetchall()
            for row in rows:
                _require(row["invocation_id"] in originals, "missing original invocation")
            for original in originals.values():
                _require(original is not None, "missing original invocation")
                binding, turn, intent = self._intent(uow, original)
                ordinal = original.handoff_attempt
                if ordinal and self._row(original.invocation_id, ordinal) is None:
                    self._insert(original, binding, turn, intent, ordinal, "UNKNOWN")
            rows = self.store.connection.execute(
                f"SELECT * FROM {TABLE} WHERE profile_id=?",
                (self.profile_id,),
            ).fetchall()
            for row in rows:
                original = originals[row["invocation_id"]]
                binding, turn, intent = self._intent(uow, original)
                _require(
                    (
                        row["request_hash"],
                        row["run_id"],
                        row["agent_id"],
                        row["turn_id"],
                        row["intent_id"],
                    )
                    == (
                        original.request_fingerprint,
                        binding.run_id,
                        binding.agent_id,
                        turn.turn_id,
                        intent.intent_id,
                    ),
                    "stored slot identity differs",
                )
                if row["state"] in {"SETTLED", "RELEASED"}:
                    continue
                record = uow.read_effective_provider_invocation(original.invocation_id)
                _require(
                    record is not None and record.request_fingerprint == row["request_hash"],
                    "missing or different effective invocation",
                )
                ordinal = row["handoff_ordinal"]
                state = row["state"]
                resolution = uow.read_reconciliation_resolution(
                    kind="provider",
                    ledger_identity=original.invocation_id,
                    handoff_attempt=ordinal,
                )
                if resolution is not None and str(resolution.outcome) == "confirmed_not_started":
                    state = "RELEASED"
                elif record.handoff_attempt == ordinal and self._terminal_usage(record):
                    state = "SETTLED"
                elif original.handoff_attempt >= ordinal:
                    state = "UNKNOWN"
                elif state == "RESERVED":
                    lease = uow.read_provider_runtime_lease(binding.run_id)
                    if (
                        lease is None
                        or lease.owner_id != row["sdk_owner"]
                        or lease.epoch != row["sdk_epoch"]
                        or lease.expires_at <= time.time()
                    ):
                        state = "RELEASED"
                self._state(original.invocation_id, ordinal, state)

    async def acquire(self, *, request, record, cancel, uow, execution_lease):
        self.recover(uow)
        binding, turn, intent = self._intent(uow, record)
        deadline = turn.created_at + binding.config_json["limits"]["turn_deadline_seconds"]
        ticket = ProviderAdmissionTicket(
            record.invocation_id,
            record.handoff_attempt + 1,
            provider_request_fingerprint(request),
            self.fingerprint,
        )
        try:
            while True:
                _require(time.time() < deadline, "slot wait exceeded turn deadline")
                with self._fence(binding.agent_id, turn.turn_id):
                    _require(
                        not cancel.is_cancelled
                        and uow.read_agent_turn_cancel(turn.turn_id) is None,
                        "cancelled before handoff",
                    )
                    lease = uow.read_provider_runtime_lease(binding.run_id)
                    _require(
                        lease is not None
                        and lease.owner_id == execution_lease.owner_id
                        and lease.epoch == execution_lease.epoch
                        and lease.expires_at > time.time(),
                        "execution owner is no longer current",
                    )
                    unresolved = self.store.connection.execute(
                        f"SELECT 1 FROM {TABLE} WHERE profile_id=? AND run_id=? AND {HELD_SQL}",
                        (self.profile_id, binding.run_id),
                    ).fetchone()
                    if unresolved:
                        raise ProviderAdmissionDenied(
                            admission_detail=ProviderAdmissionFailure(
                                reason_code="usage_unresolved"
                            )
                        )
                    active = self.store.connection.execute(
                        f"SELECT COUNT(*) FROM provider_token_grants WHERE {HELD_SQL}"
                    ).fetchone()[0] + held_legacy_slots(self.store)
                    if (
                        active < self.max_slots
                        and held_legacy_slots(self.store, self.profile_id) < self.profile_slots
                    ):
                        old = self._row(ticket.invocation_id, ticket.handoff_ordinal)
                        if old is None:
                            self._insert(
                                record,
                                binding,
                                turn,
                                intent,
                                ticket.handoff_ordinal,
                                "RESERVED",
                                wire=ticket.wire_fingerprint,
                                lease=lease,
                            )
                        else:
                            _require(
                                old["state"] == "RELEASED"
                                and old["wire_hash"] == ticket.wire_fingerprint,
                                "reserved call identity cannot be reused",
                            )
                            self.store.connection.execute(
                                f"UPDATE {TABLE} SET state='RESERVED',sdk_owner=?,sdk_epoch=?"
                                " WHERE profile_id=? AND invocation_id=? AND handoff_ordinal=?",
                                (
                                    lease.owner_id,
                                    lease.epoch,
                                    self.profile_id,
                                    ticket.invocation_id,
                                    ticket.handoff_ordinal,
                                ),
                            )
                        self._acquired[record.invocation_id] = (binding.agent_id, turn.turn_id)
                        self._leases[record.invocation_id] = (lease.owner_id, lease.epoch)
                        return ticket
                self._waiting[record.invocation_id] = (binding.agent_id, turn.turn_id)
                await asyncio.sleep(0.01)
        finally:
            self._waiting.pop(record.invocation_id, None)

    @contextmanager
    def handoff(self, ticket, *, request, cancel):
        with super().handoff(ticket, request=request, cancel=cancel):
            row = self._row(ticket.invocation_id, ticket.handoff_ordinal)
            _require(
                row is not None
                and row["state"] == "RESERVED"
                and ticket.authority_fingerprint == self.fingerprint
                and (row["sdk_owner"], row["sdk_epoch"]) == self._leases.get(ticket.invocation_id)
                and row["wire_hash"] == provider_request_fingerprint(request),
                "wire or reservation differs before handoff",
            )
            yield
            self._state(ticket.invocation_id, ticket.handoff_ordinal, "HANDED_OFF")

    def observe(self, ticket, *, record):
        try:
            with self.store.transaction():
                if record is not None:
                    row = self._row(ticket.invocation_id, ticket.handoff_ordinal)
                    _require(
                        row is not None and row["request_hash"] == record.request_fingerprint,
                        "observed request identity differs",
                    )
                    if row["state"] in {"SETTLED", "RELEASED"} or (
                        row["sdk_owner"],
                        row["sdk_epoch"],
                    ) != self._leases.get(ticket.invocation_id):
                        return  # a stale callback cannot release a successor's reservation
                    if (
                        record.handoff_attempt < ticket.handoff_ordinal
                        and str(record.state) == "claimed"
                    ):
                        self._state(ticket.invocation_id, ticket.handoff_ordinal, "RELEASED")
                    elif record.handoff_attempt == ticket.handoff_ordinal:
                        self._state(
                            ticket.invocation_id,
                            ticket.handoff_ordinal,
                            "SETTLED" if self._terminal_usage(record) else "UNKNOWN",
                        )
        finally:
            self._leases.pop(ticket.invocation_id, None)
            super().observe(ticket, record=record)
