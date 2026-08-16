# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Protocol-v1 required case catalog."""

from types import MappingProxyType

from .provider import CASES as PROVIDER_CASES
from .runtime import CASES as RUNTIME_CASES
from .tool import CASES as TOOL_CASES
from .workflow import CASES as WORKFLOW_CASES

CASES_BY_SUITE = MappingProxyType(
    {
        "provider": PROVIDER_CASES,
        "tool": TOOL_CASES,
        "runtime": RUNTIME_CASES,
        "workflow": WORKFLOW_CASES,
    }
)

__all__ = (
    "CASES_BY_SUITE",
    "PROVIDER_CASES",
    "RUNTIME_CASES",
    "TOOL_CASES",
    "WORKFLOW_CASES",
)
