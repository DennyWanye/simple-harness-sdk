# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Planner (§7.1) for step 2: one BaseAgent call that proposes exactly one Task.

The Planner draws the map; it never executes.  Its output is a *Proposal* that
the Commit Service checks (§24 step 3) — the Planner cannot write the Task DAG.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts import ContractError
from ..contracts.htn import PlanProposal
from ..graph.task_graph import TaskGraphProposal
from ..orchestrator.commit_service import TaskProposal
from ..planning.htn.registry import MethodProposal
from ..runtime.output_blocks import BlockError, extract_block
from ..runtime.role_templates import (
    METHOD_PROPOSAL_TAG,
    PLAN_REVISION_PROPOSAL_TAG,
    TASK_GRAPH_PROPOSAL_TAG,
    TASK_PROPOSAL_TAG,
)

#: §18.5 / §7.3: what a model may *never* write into a proposal, because each of
#: these is the system's own answer to "may this happen at all".  They are refused
#: at the boundary rather than overwritten, because silently replacing a claimed
#: ``manager_epoch`` with the real one would let a model probe the gate for free and
#: would leave no record that it tried.
SYSTEM_BOUND_FIELDS = frozenset(
    {
        "mission_id",
        "principal",
        "principal_id",
        "scope",
        "scope_id",
        "manager_epoch",
        "budget_account",
        "budget_grant_revision",
        "registry_status",
        "opened_by",
        "authorization_ref",
        "grant_ref",
        "provenance",
        "authored_by",
    }
)


#: P2.3g.  The ``BlockError`` reason for a Planner reply that carries a
#: ``<method_proposal>`` block and no ``<plan_revision_proposal>``.  In the Grok
#: acceptance episode H-L3-C1 two rounds were filed ``block_missing`` for this, and
#: the repair hint the next round was given said "no block found" — which was false
#: (a block was there) and unhelpful (the model wrote the other role's block again).
PROPOSAL_WRONG_BLOCK = "proposal_wrong_block"

#: P2.3g.  The ``rationale`` prefix a Planner writes when no registered method is
#: usable for the goal it was asked about.  The plan contract refuses an empty
#: ``operations`` list (``proposal.operations must not be empty``), so the explicit
#: refusal is read *here*, before the codec, and surfaces as its own exception.
NO_APPLICABLE_METHOD = "no_applicable_method"


class NoApplicableMethodDeclared(ContractError):
    """The Planner said, in the agreed shape, that no registered method applies.

    Not a malformed block and not a grounding failure: a well-formed
    ``<plan_revision_proposal>`` whose ``operations`` is ``[]``.  The caller records
    it under its own reason code and lets the synthesis path decide what to do — the
    Planner has answered, it just has nothing to refine with.
    """

    def __init__(self, rationale: str, *, proposal_id: str) -> None:
        super().__init__(
            f"plan revision proposal {proposal_id!r} has no operations and declares "
            f"{NO_APPLICABLE_METHOD}: {rationale[:300]}"
        )
        self.rationale = rationale
        self.proposal_id = proposal_id


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


def _refuse_authority_claims(payload: Mapping[str, Any], where: str) -> None:
    """Refuse a block that fills in a field the system binds.

    Only the *structural* keys are inspected — the block's own fields, its
    ``read_set`` entries and each operation's own fields.  A method parameter that
    happens to be called ``scope`` inside ``bindings`` is a value, not a claim, and
    refusing it would make the contract depend on a domain's vocabulary.
    """

    claimed = sorted(SYSTEM_BOUND_FIELDS & set(payload))
    if claimed:
        raise ContractError(
            f"{where} sets {claimed}, which the system binds; a model proposes the shape "
            "of the work, never its authority (§18.5)"
        )


def parse_plan_proposal(text: str, *, mission_id: str) -> PlanProposal:
    """Strict parse of the Planner's ``<plan_revision_proposal>`` block (§18.3, C8).

    ``mission_id`` is supplied by the caller and not read from the block: which
    Mission a proposal belongs to is decided by the request that produced it, so a
    block naming one is refused rather than believed.
    """

    try:
        raw = extract_block(text, PLAN_REVISION_PROPOSAL_TAG)
    except BlockError as error:
        if error.reason == "block_missing" and f"<{METHOD_PROPOSAL_TAG}>" in text:
            # P2.3g: the other role's block.  Reported under its own reason so the
            # rejection and the repair hint say what actually happened.
            wrong = BlockError(
                PROPOSAL_WRONG_BLOCK,
                f"a <{METHOD_PROPOSAL_TAG}> block was given where a "
                f"<{PLAN_REVISION_PROPOSAL_TAG}> was required",
            )
            raise ContractError(f"plan revision proposal unreadable: {wrong}") from wrong
        raise ContractError(f"plan revision proposal unreadable: {error}") from error
    _refuse_authority_claims(raw, "plan revision proposal")
    operations = raw.get("operations")
    if isinstance(operations, list) and not operations:
        # P2.3g: the explicit "no method applies" answer.  Read before the codec,
        # which refuses an empty operations list as malformed; the rationale is kept
        # verbatim so the record says what the Planner said.
        raise NoApplicableMethodDeclared(
            str(raw.get("rationale", "")), proposal_id=str(raw.get("proposal_id", ""))
        )
    for index, item in enumerate(raw.get("read_set") or ()):
        if isinstance(item, Mapping):
            _refuse_authority_claims(item, f"plan revision proposal read_set[{index}]")
    for index, item in enumerate(raw.get("operations") or ()):
        if isinstance(item, Mapping):
            _refuse_authority_claims(item, f"plan revision proposal operations[{index}]")
    return PlanProposal.from_json({**raw, "mission_id": mission_id})


def parse_method_proposal(text: str) -> MethodProposal:
    """Strict parse of a ``<method_proposal>`` block into a registry submission (§7.3).

    A declared ``registry_status`` is *kept*, not dropped: the admission protocol
    refuses a model-authored claim explicitly and records the refusal, which a
    silent normalisation here would hide.
    """

    try:
        raw = extract_block(text, METHOD_PROPOSAL_TAG)
    except BlockError as error:
        raise ContractError(f"method proposal unreadable: {error}") from error
    return MethodProposal.from_json(raw)


__all__ = (
    "NO_APPLICABLE_METHOD",
    "PROPOSAL_WRONG_BLOCK",
    "SYSTEM_BOUND_FIELDS",
    "NoApplicableMethodDeclared",
    "parse_method_proposal",
    "parse_plan_proposal",
    "parse_task_graph_proposal",
    "parse_task_proposal",
)
