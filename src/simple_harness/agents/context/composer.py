# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Bounded assembly of one model request from Journal units (BA-v1.0 §7.4).

Required parts (instructions, the current input, the tail protocol group) are
never dropped; older closed units are admitted newest-first while the budget
lasts and never split (BA15 / BA17).  Dropped units are replaced by one
structural summary that names its sources (BA21) and how to read them back
(BA18).  When the required parts alone exceed the budget the assembly still
returns them and flags ``required_over_budget``; the wire refuses to send.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from simple_harness.contracts import FrozenJsonValue, canonical_json
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.execution.base_agent import AgentJournalRecord

from .protocol_groups import ContextUnit

SUMMARY_APPEND_ID = "context:dropped-history-summary"


@dataclass(frozen=True, slots=True)
class Assembly:
    messages: tuple[Message, ...]
    selected_seqs: tuple[int, ...]
    dropped_ranges: tuple[tuple[int, int], ...]
    dropped_units: tuple[ContextUnit, ...]
    message_tokens: int
    required_over_budget: bool
    summary: Message | None
    summary_source_hash: str | None
    required_tokens: int = 0


def _dropped_ranges(units: tuple[ContextUnit, ...]) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for unit in sorted(units, key=lambda u: u.seq_from):
        if ranges and ranges[-1][1] + 1 >= unit.seq_from:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], unit.seq_to))
        else:
            ranges.append((unit.seq_from, unit.seq_to))
    return tuple(ranges)


def structural_summary(
    dropped: tuple[ContextUnit, ...], *, compact: bool = False
) -> tuple[Message, str]:
    """Deterministic, source-bound summary of the units that left the window."""

    turns = sum(1 for unit in dropped if unit.kind == "user_input")
    groups = sum(1 for unit in dropped if unit.kind == "group")
    seq_from = min(unit.seq_from for unit in dropped)
    seq_to = max(unit.seq_to for unit in dropped)
    source_hash = hashlib.sha256(
        canonical_json([record.content_hash for unit in dropped for record in unit.records]).encode(
            "utf-8"
        )
    ).hexdigest()
    if compact:
        text = f"[历史已折叠] seq {seq_from}–{seq_to} 已退出窗口，可用 session_history.read 回读。"
    else:
        text = (
            f"[历史已折叠] 本会话更早的 {turns} 条用户输入与 {groups} 个工具协议组"
            f"（记录 seq {seq_from}–{seq_to}）已退出工作窗口；原文完整保存在会话 Journal，"
            f"可用 session_history.read(seq) 精确回读。"
            "此摘要为结构性派生内容，不是执行状态或授权依据。"
        )
    return (
        Message(
            MessageRole.SYSTEM,
            text,
            metadata={
                "derived": True,
                "source_seq_from": seq_from,
                "source_seq_to": seq_to,
                "source_hash": source_hash,
            },
        ),
        source_hash,
    )


def _message_of(record: AgentJournalRecord) -> Message:
    from simple_harness.contracts import thaw_json
    from simple_harness.runtime.context import _message

    message = _message(thaw_json(record.message_json))
    if record.kind == "assistant":
        # The wire restores this assistant's tool calls from the effect ledger; the
        # provider turn ordinal lets it pick the right turn even after rotation.
        _, _, ordinal = record.protocol_group_id.rpartition(":provider-turn:")
        if ordinal.isdigit():
            return Message(
                message.role,
                message.content,
                name=message.name,
                call_id=message.call_id,
                metadata={**dict(message.metadata), "provider_turn_ordinal": int(ordinal)},
            )
    return message


def assemble(
    units: tuple[ContextUnit, ...],
    *,
    budget_tokens: int,
    count: Callable[[AgentJournalRecord], int],
) -> Assembly:
    if not units:
        return Assembly((), (), (), (), 0, False, None, None, 0)
    instructions = tuple(unit for unit in units if unit.kind == "instructions")
    body = tuple(unit for unit in units if unit.kind != "instructions")
    if not body:
        required: list[ContextUnit] = list(instructions)
        optional: list[ContextUnit] = []
    else:
        tail = body[-1]
        # The current input is the last user_input unit at or before the tail.
        current_input_index = max(
            (index for index, unit in enumerate(body) if unit.kind == "user_input"),
            default=None,
        )
        required = list(instructions)
        if current_input_index is not None and body[current_input_index] is not tail:
            required.append(body[current_input_index])
        required.append(tail)
        required_ids = {id(unit) for unit in required}
        optional = [unit for unit in body if id(unit) not in required_ids]

    def unit_tokens(unit: ContextUnit) -> int:
        return sum(count(record) for record in unit.records)

    required_used = sum(unit_tokens(unit) for unit in required)
    required_over_budget = required_used > budget_tokens
    summary: Message | None = None
    summary_hash: str | None = None
    selected: list[ContextUnit] = list(required)
    dropped: list[ContextUnit] = []
    used = required_used
    # Newest-first admission of closed units; a unit that does not fit is dropped
    # and everything older with it (no gaps inside the window, BA17).  The folded-
    # history summary is charged *before* admission (review S3-01): admit with the
    # summary's cost reserved, then re-run once the exact dropped range is known,
    # until the assembly is stable (the summary text only varies by a few digits).
    summary_reserve = 0
    for _ in range(4):
        selected = list(required)
        dropped = []
        used = required_used
        admitting = not required_over_budget
        for unit in reversed(optional):
            cost = unit_tokens(unit)
            if admitting and used + cost <= budget_tokens - summary_reserve:
                used += cost
                selected.append(unit)
            else:
                admitting = False
                dropped.append(unit)
        if not dropped:
            summary, summary_hash = None, None
            break
        summary, summary_hash = structural_summary(tuple(dropped))
        needed = count_message_tokens(count, summary)
        if needed <= summary_reserve:
            break
        summary_reserve = needed
    if summary is not None:
        needed = count_message_tokens(count, summary)
        if used + needed > budget_tokens and not required_over_budget:
            # Required parts fit but not with the full pointer: send the compact form,
            # and if even that does not fit, keep the pointer in the selection only.
            compact, summary_hash = structural_summary(tuple(dropped), compact=True)
            needed = count_message_tokens(count, compact)
            summary = compact if used + needed <= budget_tokens else None
            if summary is None:
                needed = 0
        used += needed
    if not required_over_budget and used > budget_tokens:
        # Invariant: an assembly that is not "required too large" never exceeds the
        # budget; the wire guard must only ever see genuine BA16 cases.
        raise AssertionError("context assembly exceeded its budget")
    ordered = sorted(selected, key=lambda u: u.seq_from)
    messages: list[Message] = []
    inserted_summary = False
    for unit in ordered:
        if unit.kind != "instructions" and summary is not None and not inserted_summary:
            messages.append(summary)
            inserted_summary = True
        messages.extend(_message_of(record) for record in unit.records)
    if summary is not None and not inserted_summary:
        messages.append(summary)
    return Assembly(
        messages=tuple(messages),
        selected_seqs=tuple(record.seq for unit in ordered for record in unit.records),
        dropped_ranges=_dropped_ranges(tuple(dropped)),
        dropped_units=tuple(dropped),
        message_tokens=used,
        required_over_budget=required_over_budget,
        summary=summary,
        summary_source_hash=summary_hash,
        required_tokens=required_used,
    )


def count_message_tokens(count: Callable[[AgentJournalRecord], int], message: Message) -> int:
    """Count a synthetic (non-Journal) message with the same counter as Journal records."""

    probe = AgentJournalRecord(
        record_id="probe",
        agent_id="probe",
        seq=1,
        append_id="probe",
        kind="feedback",
        turn_id=None,
        protocol_group_id="probe",
        message_json=cast(FrozenJsonValue, message.to_dict()),
        content_hash="0" * 64,
        provenance="derived",
        visibility="context",
        full_record_seq=None,
        lease_epoch=1,
        created_at=0.0,
    )
    return count(probe)


__all__ = ("SUMMARY_APPEND_ID", "Assembly", "assemble", "structural_summary")
