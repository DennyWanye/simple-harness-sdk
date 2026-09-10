# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Planner (§7.1) for step 2: one BaseAgent call that proposes exactly one Task.

The Planner draws the map; it never executes.  Its output is a *Proposal* that
the Commit Service checks (§24 step 3) — the Planner cannot write the Task DAG.
"""

from __future__ import annotations

from ..contracts import ContractError
from ..graph.task_graph import TaskGraphProposal
from ..orchestrator.commit_service import TaskProposal
from ..runtime.output_blocks import BlockError, extract_block
from ..runtime.role_templates import TASK_GRAPH_PROPOSAL_TAG, TASK_PROPOSAL_TAG


def parse_task_proposal(text: str) -> TaskProposal:
    """Strict parse of the Planner's ``<task_proposal>`` block into a ``TaskProposal``."""

    try:
        raw = extract_block(text, TASK_PROPOSAL_TAG)
    except BlockError as error:
        raise ContractError(f"task proposal unreadable: {error}") from error
    return TaskProposal.from_json(raw)


def parse_task_graph_proposal(text: str) -> TaskGraphProposal:
    """Strict parse of the Planner's ``<task_graph_proposal>`` block (step 3, D3-1).

    A step-2 style single ``<task_proposal>`` is accepted as a graph of one root
    task so a Planner that decides the Mission needs no decomposition still works.
    """

    try:
        raw = extract_block(text, TASK_GRAPH_PROPOSAL_TAG)
    except BlockError as graph_error:
        try:
            single = extract_block(text, TASK_PROPOSAL_TAG)
        except BlockError:
            raise ContractError(f"task graph proposal unreadable: {graph_error}") from graph_error
        node = {**single, "key": "root", "dependencies": []}
        node.pop("root_goal", None)
        return TaskGraphProposal.from_json({"tasks": [node]})
    return TaskGraphProposal.from_json(raw)


__all__ = ("parse_task_graph_proposal", "parse_task_proposal")
