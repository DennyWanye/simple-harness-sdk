# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Operation commands bound to the existing authenticated facade caller."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts.operation_intents import SubmitOperationIntentV2
from ..contracts.semantic_base import identifier
from ..governance.permissions import Principal


class OperationIntentApi:
    def __init__(self, orchestrator: Any, *, tenant_id: str, principal: Principal) -> None:
        if not isinstance(principal, Principal) or principal.kind != "human":
            raise ValueError("operation submission requires an authenticated human caller")
        self._orchestrator = orchestrator
        self._tenant = identifier(tenant_id, "tenant_id")
        self._principal = principal

    def submit(self, command: Mapping[str, Any]) -> dict[str, Any]:
        return self._orchestrator.submit_operation_intent(
            SubmitOperationIntentV2.from_json(command),
            tenant_id=self._tenant,
            principal=self._principal,
        )

    def status(self, intent_id: str) -> dict[str, Any]:
        return self._orchestrator.commit.operation_intent_status(
            identifier(intent_id, "intent_id"),
            tenant_id=self._tenant,
            principal=self._principal,
        )


__all__ = ("OperationIntentApi",)
