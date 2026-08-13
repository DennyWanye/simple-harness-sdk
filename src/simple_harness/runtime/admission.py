# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Product-neutral root admission port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .start_snapshot import RunStart


@dataclass(frozen=True, slots=True)
class AdmissionVerdict:
    allowed: bool
    code: str = "allowed"

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be a bool")
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("admission code is required")


@runtime_checkable
class AdmissionPort(Protocol):
    async def evaluate(self, start: RunStart) -> AdmissionVerdict: ...


class AllowAllAdmission:
    async def evaluate(self, start: RunStart) -> AdmissionVerdict:
        del start
        return AdmissionVerdict(True)


__all__ = ("AdmissionPort", "AdmissionVerdict", "AllowAllAdmission")
