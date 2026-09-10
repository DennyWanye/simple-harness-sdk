# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Planner (§7.1) for step 2: one BaseAgent call that proposes exactly one Task.

The Planner draws the map; it never executes.  Its output is a *Proposal* that
the Commit Service checks (§24 step 3) — the Planner cannot write the Task DAG.
"""

from __future__ import annotations

from ..contracts import ContractError
from ..orchestrator.commit_service import TaskProposal
from ..runtime.output_blocks import BlockError, extract_block
from ..runtime.role_templates import TASK_PROPOSAL_TAG


def parse_task_proposal(text: str) -> TaskProposal:
    """Strict parse of the Planner's ``<task_proposal>`` block into a ``TaskProposal``."""

    try:
        raw = extract_block(text, TASK_PROPOSAL_TAG)
    except BlockError as error:
        raise ContractError(f"task proposal unreadable: {error}") from error
    return TaskProposal.from_json(raw)


__all__ = ("parse_task_proposal",)
