# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``ProtocolGroupAdapter``: the ``ProtocolGroupSnapshot`` of one Journal high-water (§4.1, C1).

Closure is produced by real facts, never by model text:

* a ``USER_ANCHOR`` is one persisted ``user_input`` record (closed by definition);
* a ``CLOSED_TOOL`` group is an assistant record whose issued call ids (from the
  execution effects ledger, keyed by the provider turn ordinal in the group id)
  are each answered by exactly one ``tool_result`` record carrying that call id;
  a missing answer keeps the group ``OPEN_TAIL`` (mandatory);
* an assistant record without calls is ``TERMINAL_ANSWER`` when its Turn is
  committed / failed, otherwise ``HISTORY_MESSAGE``;
* other ``feedback`` records are ``HISTORY_MESSAGE`` groups.

Group ids are the Journal's own ``protocol_group_id``; ``source_hash`` covers the
exact record refs (record id + content hash) and nothing else.  ``journal_only``
records (externalised large tool results) belong to their group's record set but
are never rendered.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from simple_harness.contracts import thaw_json
from simple_harness.execution.base_agent import AgentJournalRecord
from simple_harness.execution.sqlite.base_agent import history, turns

from ..codec import check
from ..errors import ArpError
from ..pins import Pin
from ..strict import digest

GROUP_KINDS = (
    "USER_ANCHOR",
    "CLOSED_TOOL",
    "OPEN_TAIL",
    "TERMINAL_ANSWER",
    "OPAQUE_REQUIRED",
    "HISTORY_MESSAGE",
)
OPTIONAL_GROUP_CAP = 8192
PROVENANCE_OF = {
    "instructions": "USER_INPUT",
    "user_input": "USER_INPUT",
    "assistant": "AGENT_CLAIM",
    "tool_result": "TOOL_RESULT",
    "feedback": "VERIFIER_FEEDBACK",
}


@dataclass(frozen=True, slots=True)
class GroupView:
    """One protocol group with its exact records (the typed ``JournalGroup`` plus records)."""

    group_id: str
    kind: str
    turn_id: str
    records: tuple[AgentJournalRecord, ...]
    call_ids: tuple[str, ...]
    result_call_ids: tuple[str, ...]
    mandatory: bool
    closed: bool
    source_hash: str
    budget_charge: int

    @property
    def seq_from(self) -> int:
        return self.records[0].seq

    @property
    def seq_to(self) -> int:
        return self.records[-1].seq

    @property
    def indexable(self) -> bool:
        return self.closed and self.kind not in ("OPEN_TAIL", "OPAQUE_REQUIRED")

    @property
    def visible_records(self) -> tuple[AgentJournalRecord, ...]:
        return tuple(r for r in self.records if r.visibility == "context")

    @property
    def record_refs(self) -> tuple[Pin, ...]:
        return tuple(Pin("journal_record", r.record_id, r.seq, r.content_hash) for r in self.records)

    @property
    def view_hash(self) -> str:
        return digest([[r.record_id, r.content_hash] for r in self.visible_records])

    def to_json(self, session_id: str) -> dict[str, Any]:
        closure = None
        if self.kind == "CLOSED_TOOL":
            closure = Pin(
                "receipt",
                f"{self.group_id}:closed",
                0,
                digest({"calls": list(self.call_ids), "results": [[r.record_id, r.content_hash] for r in self.records if r.kind == "tool_result"]}),
            ).to_json()
        return {
            "session_id": session_id,
            "group_id": self.group_id,
            "seq_from": self.seq_from,
            "seq_to": self.seq_to,
            "source_hash": self.source_hash,
            "closed": self.closed,
            "record_ids": [r.record_id for r in self.records],
            "rendered_view_ref": Pin("artifact", f"view:{self.group_id}", 0, self.view_hash).to_json(),
            "budget_charge": self.budget_charge,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "mandatory": self.mandatory,
            "closure_receipt_ref": closure,
            "call_ids": list(self.call_ids),
            "result_call_ids": list(self.result_call_ids),
            "indexable": self.indexable,
        }


@dataclass(frozen=True, slots=True)
class GroupSnapshot:
    body: Mapping[str, Any]
    groups: tuple[GroupView, ...]
    instructions: tuple[AgentJournalRecord, ...]
    highwater: int
    current_turn_id: str

    @property
    def snapshot_hash(self) -> str:
        return str(self.body["snapshot_hash"])

    @property
    def pin(self) -> Pin:
        return Pin("artifact", f"group-snapshot:{self.highwater}", self.highwater, self.snapshot_hash)

    @property
    def expected_group_set_hash(self) -> str:
        """Hash of the complete indexable closed-group set (group id + source hash)."""

        return digest(sorted([g.group_id, g.source_hash] for g in self.groups if g.indexable))

    def group(self, group_id: str) -> GroupView:
        for group in self.groups:
            if group.group_id == group_id:
                return group
        raise ArpError("SOURCE_UNAVAILABLE", f"unknown group {group_id}")


def source_hash_of(records: tuple[AgentJournalRecord, ...]) -> str:
    return digest([[r.record_id, r.content_hash] for r in records])


def _call_id_of(record: AgentJournalRecord) -> str | None:
    message = thaw_json(record.message_json)
    value = message.get("call_id") if isinstance(message, dict) else None
    return value if isinstance(value, str) and value else None


def issued_calls(connection: sqlite3.Connection, run_id: str) -> dict[int, tuple[str, ...]]:
    """Issued raw call ids per provider turn ordinal from the effects ledger (real facts)."""

    calls: dict[int, list[str]] = {}
    for row in connection.execute(
        "SELECT raw_call_id, turn_ordinal FROM execution_effects WHERE run_id=? AND raw_call_id IS NOT NULL"
        " ORDER BY turn_ordinal, call_ordinal",
        (run_id,),
    ):
        calls.setdefault(int(row[1]), []).append(str(row[0]))
    return {k: tuple(v) for k, v in calls.items()}


def capture(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    agent_id: str,
    highwater: int,
    current_turn_id: str | None = None,
    charge: Callable[[AgentJournalRecord], int],
    session_ref: Pin,
) -> GroupSnapshot:
    """Freeze the Journal at ``highwater`` into a validated ``ProtocolGroupSnapshot``."""

    records = history.read_records(connection, agent_id, from_seq=1, to_seq=highwater)
    if records and records[-1].seq != highwater and highwater > 0:
        raise ArpError("JOURNAL_SOURCE_UNAVAILABLE", "journal shorter than the requested highwater")
    turn_rows = {t.turn_id: t for t in turns.list_turns(connection, agent_id)}
    calls_by_ordinal = issued_calls(connection, agent_id)
    instructions = tuple(r for r in records if r.kind == "instructions")
    # Chronological raw groups keyed by the Journal's own protocol_group_id.
    ordered: list[tuple[str, list[AgentJournalRecord]]] = []
    index: dict[str, int] = {}
    for record in records:
        if record.kind == "instructions":
            continue
        key = record.protocol_group_id
        if key not in index:
            index[key] = len(ordered)
            ordered.append((key, []))
        ordered[index[key]][1].append(record)
    if current_turn_id is None:
        latest = next((r for r in reversed(records) if r.kind == "user_input"), None)
        current_turn_id = latest.turn_id if latest is not None and latest.turn_id else None
    if current_turn_id is None:
        raise ArpError("STATE_COMBINATION_INVALID", "no current input anchor in the journal")
    views: list[GroupView] = []
    turn_of_group: dict[str, str] = {}
    running_turn: str | None = None
    for group_id, members in ordered:
        members.sort(key=lambda r: r.seq)
        first = members[0]
        if first.kind == "user_input":
            running_turn = first.turn_id or running_turn
            turn_id = first.turn_id or (running_turn or current_turn_id)
            views.append(
                GroupView(
                    group_id, "USER_ANCHOR", turn_id, tuple(members), (), (),
                    mandatory=(turn_id == current_turn_id), closed=True,
                    source_hash=source_hash_of(tuple(members)),
                    budget_charge=sum(charge(r) for r in members if r.visibility == "context"),
                )
            )
            turn_of_group[group_id] = turn_id
            continue
        turn_id = running_turn or current_turn_id
        turn_row = turn_rows.get(turn_id)
        turn_done = turn_row is not None and turn_row.phase in ("committed", "failed")
        if first.kind == "assistant":
            _, _, ordinal = group_id.rpartition(":provider-turn:")
            issued = calls_by_ordinal.get(int(ordinal), ()) if ordinal.isdigit() else ()
            answered = tuple(c for c in (_call_id_of(r) for r in members if r.kind == "tool_result") if c)
            if issued:
                closed = set(issued) == set(answered) and len(set(answered)) == len(answered)
                kind = "CLOSED_TOOL" if closed else "OPEN_TAIL"
            else:
                closed = True
                kind = "TERMINAL_ANSWER" if turn_done else "HISTORY_MESSAGE"
            views.append(
                GroupView(
                    group_id, kind, turn_id, tuple(members), tuple(issued), answered,
                    mandatory=(kind == "OPEN_TAIL"), closed=closed,
                    source_hash=source_hash_of(tuple(members)),
                    budget_charge=sum(charge(r) for r in members if r.visibility == "context"),
                )
            )
        else:
            views.append(
                GroupView(
                    group_id, "HISTORY_MESSAGE", turn_id, tuple(members), (), (),
                    mandatory=False, closed=True,
                    source_hash=source_hash_of(tuple(members)),
                    budget_charge=sum(charge(r) for r in members if r.visibility == "context"),
                )
            )
        turn_of_group[group_id] = turn_id
    # Bounded optional enumeration (§4.1 step 7): mandatory groups are always complete;
    # only the newest OPTIONAL_GROUP_CAP optional groups are enumerated.
    optional = [g for g in views if not g.mandatory]
    omitted = max(0, len(optional) - OPTIONAL_GROUP_CAP)
    kept_optional = set(id(g) for g in optional[omitted:])
    groups = tuple(g for g in views if g.mandatory or id(g) in kept_optional)
    enumeration_complete = omitted == 0
    # Turn coverage: every enumerated historic Turn lists ALL its groups; a Turn with
    # omitted groups cannot be counted (§4.1 step 6/7).
    turn_groups: dict[str, list[str]] = {}
    for g in views:
        turn_groups.setdefault(g.turn_id, []).append(g.group_id)
    enumerated = {g.group_id for g in groups}
    coverage = []
    for turn_id, ids in turn_groups.items():
        if not set(ids) <= enumerated:
            continue
        row = turn_rows.get(turn_id)
        coverage.append(
            {
                "turn_id": turn_id,
                "completed": row is not None and row.phase in ("committed", "failed"),
                "required_group_ids": list(ids),
                "source_highwater": max(g.seq_to for g in groups if g.turn_id == turn_id),
            }
        )
    body: dict[str, Any] = {
        "session_ref": session_ref.to_json(),
        "journal_highwater": highwater,
        "groups": [g.to_json(session_id) for g in groups],
        "turns": coverage,
        "enumeration_complete": enumeration_complete,
        "omitted_optional_groups": omitted,
        "current_turn_id": current_turn_id,
        "mandatory_group_ids": [g.group_id for g in groups if g.mandatory],
    }
    body["snapshot_hash"] = digest(body)
    return GroupSnapshot(check("ProtocolGroupSnapshot", body), groups, instructions, highwater, current_turn_id)


__all__ = ("GROUP_KINDS", "GroupSnapshot", "GroupView", "PROVENANCE_OF", "capture", "issued_calls", "source_hash_of")
