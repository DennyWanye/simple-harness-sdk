# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Transaction-bound workflow trace contracts."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from simple_harness.contracts import JsonValue, validate_json_value

from .execution_ports import WorkflowTransaction


@dataclass(frozen=True, slots=True)
class WorkflowTraceEvent:
    run_id: str
    kind: str
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        payload = copy.deepcopy(dict(self.payload))
        validate_json_value(payload)
        object.__setattr__(self, "payload", MappingProxyType(payload))


class WorkflowTracePort(Protocol):
    async def append(
        self, event: WorkflowTraceEvent, *, transaction: WorkflowTransaction
    ) -> None: ...


__all__ = ("WorkflowTraceEvent", "WorkflowTracePort")
