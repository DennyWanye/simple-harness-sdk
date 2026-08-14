# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Capability builder error types."""

from __future__ import annotations


class CapabilityBuildError(RuntimeError):
    """Error raised during capability build admission, validation, or finalization."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


__all__ = ["CapabilityBuildError"]
