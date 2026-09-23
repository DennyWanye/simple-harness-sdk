# SPDX-License-Identifier: Apache-2.0
"""Reuse the original complete execution inventory before releasing a hold."""

from __future__ import annotations

from typing import Any

from ..contracts import ContractError
from ..governance.budgets import BudgetError
from ..runtime.planning_operations import SourceUnavailable
from .taskgraph_runtime_imports import TaskGraphRuntimeImports


class AssuranceSettlement:
    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator
        self.sources = TaskGraphRuntimeImports(orchestrator)

    def require_settled_locked(self, subject_id: str, mission_id: str) -> None:
        store = self.orchestrator.store
        if not store.connection.in_transaction:
            raise BudgetError("Assurance settlement requires the original transaction")
        intent = store.get_intent_for_subject(subject_id)
        if (
            intent is None
            or intent.mission_id != mission_id
            or intent.state not in {"SETTLED", "FAILED"}
        ):
            raise BudgetError("Assurance original executor is not closed; reservation held")
        try:
            source = self.sources.read_subject(intent)
        except (SourceUnavailable, ContractError, OSError) as error:
            raise BudgetError(
                "Assurance original execution inventory unavailable; reservation held"
            ) from error
        if not source.accounting_complete or not source.physical_settled:
            raise BudgetError(
                "Assurance physical/accounting responsibility unresolved; reservation held"
            )
