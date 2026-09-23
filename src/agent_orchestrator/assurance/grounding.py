# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Mandatory bounded support computation for the Assurance evaluator.

The preparation layer must reselect anchors from its complete current snapshot.
This function never accepts prior ClosureResult/SupportWitness objects as seeds.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.evidence_state import TruthValue
from ..knowledge.bounded_closure import (
    ClosureBudget,
    ClosureLimits,
    _Exhausted,
    bounded_grounded_closure,
)
from ..knowledge.justifications import (
    AnchorSelection,
    Atom,
    ClosureResult,
    SupportGraph,
)
from .codec import AssuranceError


@dataclass(frozen=True, slots=True)
class GroundedSupport:
    supported: ClosureResult
    clean: ClosureResult

    def usable(self, proposition_key: str) -> bool:
        return (
            self.supported.truth_for(proposition_key) is TruthValue.TRUE
            and self.clean.reached(Atom(proposition_key))
            and not self.clean.rests_on_assumption(proposition_key)
        )


def compute_grounded_support(graph: SupportGraph, selection: AnchorSelection) -> GroundedSupport:
    if not isinstance(selection, AnchorSelection):
        raise AssuranceError("FRESH_ANCHOR_SELECTION_REQUIRED")
    limits = ClosureLimits()
    budget = ClosureBudget(limits)
    result = bounded_grounded_closure(
        graph,
        selection.anchors,
        limits=limits,
        node_budget=30_000,
        assumptions=None,
        budget=budget,
    )
    if not result.is_complete:
        raise AssuranceError("EVIDENCE_EVALUATION_INCOMPLETE")
    conflicts = set()
    try:
        for atom in result.witnesses:
            budget.check(1)
            if result.reached(atom.negated):
                conflicts.add(atom.key)
    except _Exhausted as error:
        raise AssuranceError("EVIDENCE_EVALUATION_INCOMPLETE") from error
    # Start afresh from the CURRENT selected anchors, excluding both polarities
    # of every conflicted proposition. Derived TRUEs from the first pass cannot
    # seed the clean pass. A separate clean branch can still support a result.
    clean = bounded_grounded_closure(
        graph,
        selection.anchors,
        limits=limits,
        node_budget=30_000,
        assumptions=None,
        budget=budget,
        excluded_keys=frozenset(conflicts),
    )
    if not clean.is_complete:
        raise AssuranceError("EVIDENCE_EVALUATION_INCOMPLETE")
    return GroundedSupport(result, clean)
