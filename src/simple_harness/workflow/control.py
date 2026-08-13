# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Framework-neutral durable workflow interrupt control flow."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from .contracts import JsonValue, canonical_json, validate_json_value
from .errors import InvalidStatePatch


@dataclass(frozen=True, slots=True)
class WorkflowInterrupt:
    interrupt_id: str
    task_id: str
    ordinal: int
    payload: dict[str, JsonValue]

    @property
    def id(self) -> str:
        return self.interrupt_id

    @property
    def value(self) -> dict[str, JsonValue]:
        return copy.deepcopy(self.payload)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "interrupt_id": self.interrupt_id,
            "task_id": self.task_id,
            "ordinal": self.ordinal,
            "payload": copy.deepcopy(self.payload),
        }


class WorkflowSuspended(BaseException):
    """Private control-flow signal caught only by the native executor."""

    def __init__(self, interrupt: WorkflowInterrupt) -> None:
        super().__init__(interrupt.interrupt_id)
        self.interrupt = interrupt


@dataclass(slots=True)
class ExecutionControl:
    task_id: str
    responses: Mapping[str, JsonValue] = field(default_factory=dict)
    consumed_interrupt_ids: list[str] = field(default_factory=list)
    calls: int = 0


_CONTROL: contextvars.ContextVar[ExecutionControl | None] = contextvars.ContextVar(
    "simple_harness_workflow_execution_control", default=None
)


def _interrupt_id(task_id: str, ordinal: int) -> str:
    return hashlib.sha256(f"{task_id}|{ordinal}".encode()).hexdigest()


@contextlib.contextmanager
def bind_execution_control(control: ExecutionControl) -> Iterator[ExecutionControl]:
    token = _CONTROL.set(control)
    try:
        yield control
    finally:
        _CONTROL.reset(token)


def workflow_interrupt(payload: Mapping[str, JsonValue]) -> JsonValue:
    """Return the durable response or suspend the current exclusive task."""

    control = _CONTROL.get()
    if control is None:
        raise InvalidStatePatch(
            "interrupt_outside_execution",
            "workflow_interrupt must run inside a native workflow task",
        )
    if control.calls:
        raise InvalidStatePatch(
            "multiple_task_interrupts",
            "A native workflow task may contain only one interrupt point",
        )
    copied = copy.deepcopy(dict(payload))
    validate_json_value(copied, path="$.interrupt.payload")
    control.calls += 1
    ordinal = 0
    interrupt_id = _interrupt_id(control.task_id, ordinal)
    if interrupt_id in control.responses:
        response = copy.deepcopy(control.responses[interrupt_id])
        validate_json_value(response, path="$.interrupt.response")
        control.consumed_interrupt_ids.append(interrupt_id)
        return response
    interrupt = WorkflowInterrupt(interrupt_id, control.task_id, ordinal, copied)
    # Force canonical validation before this value reaches durable storage.
    canonical_json(interrupt.to_dict())
    raise WorkflowSuspended(interrupt)


__all__ = [
    "ExecutionControl",
    "WorkflowInterrupt",
    "WorkflowSuspended",
    "bind_execution_control",
    "workflow_interrupt",
]
