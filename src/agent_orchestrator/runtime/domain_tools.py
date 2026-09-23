# SPDX-License-Identifier: Apache-2.0
"""Deployment-supplied tools; the gateway does not branch on domain identity."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DomainTool:
    schema: dict[str, Any]
    invoke: Callable[[Mapping[str, Any], str, str], Mapping[str, Any]] = field(repr=False, compare=False)
    # The callback receives trusted Mission and effect-call identities, never model
    # arguments for either. Effectful implementations must deduplicate call_id.
    read_only: bool = False
