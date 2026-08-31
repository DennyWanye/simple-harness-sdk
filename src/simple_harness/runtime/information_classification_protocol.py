# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Shared information-classification vocabulary with no runtime dependencies."""

from __future__ import annotations

from enum import StrEnum


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class InformationAttribute(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    GOAL = "goal"
    WORK = "work"
    RELATIONSHIP = "relationship"
    FAMILY = "family"
    HEALTH = "health"
    LOCATION = "location"
    FINANCIAL = "financial"
    OTHER = "other"


__all__ = ("InformationAttribute", "PrivacyClass")
