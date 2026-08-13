# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Ticket-only child launch and restart contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from simple_harness.contracts import FrozenJsonValue, JsonValue, freeze_json
from simple_harness.execution.contracts.children import (
    AttachmentPolicy,
    ChildLaunchResult,
)


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ProfileLaunchTicketRef:
    ticket_id: str
    catalog_generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticket_id", _required(self.ticket_id, "ticket_id"))
        if isinstance(self.catalog_generation, bool) or self.catalog_generation < 1:
            raise ValueError("catalog_generation must be a positive integer")


@dataclass(frozen=True, slots=True)
class ChildLaunchRequest:
    ticket: ProfileLaunchTicketRef
    command_id: str
    child_run_id: str
    request_id: str
    attachment_policy: AttachmentPolicy
    launch_payload: FrozenJsonValue
    start_snapshot: FrozenJsonValue

    def __post_init__(self) -> None:
        if not isinstance(self.ticket, ProfileLaunchTicketRef):
            raise TypeError("child entry requires ProfileLaunchTicketRef")
        for name in ("command_id", "child_run_id", "request_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(
            self, "attachment_policy", AttachmentPolicy(self.attachment_policy)
        )
        for name in ("launch_payload", "start_snapshot"):
            value = getattr(self, name)
            if not isinstance(value, dict):
                raise TypeError(f"{name} must be a JSON object")
            object.__setattr__(self, name, freeze_json(value))


class ChildRunUnitOfWork(Protocol):
    def claim_profile_launch_and_commit_child(
        self,
        *,
        ticket_id: str,
        expected_catalog_generation: int,
        launch_request: Mapping[str, JsonValue],
        command_id: str,
        child_run_id: str,
        request_id: str,
        attachment_policy: AttachmentPolicy,
        start_snapshot: Mapping[str, JsonValue],
        event_id: str,
        now: float,
    ) -> ChildLaunchResult: ...


__all__ = (
    "ChildLaunchRequest",
    "ChildRunUnitOfWork",
    "ProfileLaunchTicketRef",
)
