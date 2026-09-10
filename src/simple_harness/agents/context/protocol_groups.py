# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Group Journal records into atomic units the assembler never splits (BA15).

A *unit* is either one user input, the instructions block, or one protocol
group: an assistant message together with the tool results (and feedback) that
answer it.  The last unit of the Journal is the *tail*: it is the group still
being worked (or the just-finished exchange) and is always part of the request.
"""

from __future__ import annotations

from dataclasses import dataclass

from simple_harness.execution.base_agent import AgentJournalRecord


@dataclass(frozen=True, slots=True)
class ContextUnit:
    kind: str  # instructions | user_input | group
    group_id: str
    turn_id: str | None
    records: tuple[AgentJournalRecord, ...]

    @property
    def seq_from(self) -> int:
        return self.records[0].seq

    @property
    def seq_to(self) -> int:
        return self.records[-1].seq


def build_units(records: tuple[AgentJournalRecord, ...]) -> tuple[ContextUnit, ...]:
    """Chronological units; ``journal_only`` records never enter a unit."""

    visible = [record for record in records if record.visibility == "context"]
    units: list[ContextUnit] = []
    current: list[AgentJournalRecord] = []
    current_key: tuple[str, str] | None = None

    def flush() -> None:
        nonlocal current, current_key
        if current and current_key is not None:
            units.append(
                ContextUnit(
                    kind=current_key[0],
                    group_id=current_key[1],
                    turn_id=current[0].turn_id,
                    records=tuple(current),
                )
            )
        current = []
        current_key = None

    for record in visible:
        if record.kind == "instructions":
            key = ("instructions", "instructions")
        elif record.kind == "user_input":
            key = ("user_input", record.protocol_group_id)
        else:
            key = ("group", record.protocol_group_id)
        if key != current_key:
            flush()
            current_key = key
        current.append(record)
    flush()
    return tuple(units)


__all__ = ("ContextUnit", "build_units")
