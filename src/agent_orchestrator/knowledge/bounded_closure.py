# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Bounded worklist for the existing grounded support semantics.

No prior truth or derived witness is accepted as a seed. The original premise,
source-path and clean-support algorithms remain the semantic authority.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from time import thread_time_ns

from ..contracts.models import ContractError
from ..contracts.semantic_base import EvidenceRef, index
from .justifications import (
    Anchor,
    Atom,
    ClosureResult,
    ClosureStatus,
    EvidencePremise,
    SupportGraph,
    SupportWitness,
    WitnessKind,
    _assumption_backed,
    _assumption_state,
    _path_groups,
    _premises_satisfied,
    _source_groups,
)


@dataclass(frozen=True, slots=True)
class ClosureLimits:
    literals: int = 10_000
    rules: int = 20_000
    premise_visits: int = 1_000_000
    cpu_ms: int = 250

    def __post_init__(self) -> None:
        for name, maximum in (
            ("literals", 10_000),
            ("rules", 20_000),
            ("premise_visits", 1_000_000),
            ("cpu_ms", 250),
        ):
            value = getattr(self, name)
            index(value, name, minimum=1)
            if value > maximum:
                raise ContractError(f"{name} exceeds the Assurance evaluation limit")


class _Exhausted(Exception):
    pass


class ClosureBudget:
    """One CPU/visit allowance shared by support and conflict-free recomputation."""

    def __init__(self, limits: ClosureLimits) -> None:
        self.limits = limits
        self.deadline = thread_time_ns() + limits.cpu_ms * 1_000_000
        self.visits = 0

    def check(self, charge: int = 0) -> None:
        self.visits += charge
        if self.visits > self.limits.premise_visits or thread_time_ns() >= self.deadline:
            raise _Exhausted


def bounded_grounded_closure(
    graph: SupportGraph,
    anchors: Iterable[Anchor],
    *,
    limits: ClosureLimits,
    node_budget: int,
    assumptions: Mapping[str, bool] | None,
    budget: ClosureBudget | None = None,
    excluded_keys: frozenset[str] = frozenset(),
) -> ClosureResult:
    if not isinstance(graph, SupportGraph) or not isinstance(limits, ClosureLimits):
        raise ContractError("bounded closure requires an original SupportGraph and ClosureLimits")
    index(node_budget, "node_budget", minimum=1)
    shared = budget if budget is not None else ClosureBudget(limits)
    if not isinstance(shared, ClosureBudget) or shared.limits != limits:
        raise ContractError("closure budget must match its limits")
    nodes = 0
    literals: set[Atom] = set()
    witnesses: dict[Atom, list[SupportWitness]] = {}
    admitted: set[str] = set()
    evidence_refs: set[EvidenceRef] = set()
    ref_groups: dict[EvidenceRef, set[str]] = {}

    def check(charge: int = 0) -> None:
        shared.check(charge)

    def literal(atom: Atom) -> None:
        literals.add(atom)
        if len(literals) > limits.literals:
            raise _Exhausted

    try:
        check()
        if len(graph) > limits.rules:
            raise _Exhausted
        state = _assumption_state(assumptions)
        for anchor in anchors:
            check()
            if not isinstance(anchor, Anchor):
                raise ContractError("grounded closure seeds on AnchorSelector output only")
            literal(anchor.atom)
            if anchor.atom.key in excluded_keys:
                continue
            if anchor.signature in admitted:
                continue
            if nodes >= node_budget:
                raise _Exhausted
            admitted.add(anchor.signature)
            nodes += 1
            witnesses.setdefault(anchor.atom, []).append(
                SupportWitness(
                    atom=anchor.atom,
                    kind=WitnessKind.ANCHOR,
                    signature=anchor.signature,
                    source_group=anchor.source_group,
                )
            )
            if anchor.evidence_ref is not None:
                evidence_refs.add(anchor.evidence_ref)
                ref_groups.setdefault(anchor.evidence_ref, set()).add(anchor.source_group)

        rules = graph.justifications()
        check()
        waiting: dict[Atom, list[int]] = defaultdict(list)
        missing: dict[int, set[Atom]] = {}
        ready = []
        for position, entry in enumerate(rules):
            check()
            literal(entry.atom)
            absent = set()
            usable = True
            for premise in entry.premises:
                check(1)
                if isinstance(premise, EvidencePremise):
                    usable &= premise.ref in evidence_refs
                else:
                    literal(premise.atom)
                    if premise.atom not in witnesses:
                        absent.add(premise.atom)
            for condition in entry.conditions:
                check(1)
                usable &= state.get(condition.name) is True
            usable &= entry.atom.key not in excluded_keys
            if not usable:
                continue
            missing[position] = absent
            if not absent:
                ready.append(position)
            for atom in absent:
                waiting[atom].append(position)

        # Preserve the old synchronous rounds and stable rule ordering, including
        # witness depths. Only rules whose last missing atom arrived are visited.
        while ready:
            fired = []
            for position in sorted(ready):
                entry = rules[position]
                check(len(entry.premises) + len(entry.conditions))
                satisfied = _premises_satisfied(entry, witnesses, evidence_refs, state)
                if satisfied is None:
                    raise ContractError(
                        "worklist premise bookkeeping disagrees with original evaluator"
                    )
                fired.append((entry, satisfied))
            added: set[Atom] = set()
            for entry, (depth, used) in fired:
                check()
                if nodes >= node_budget:
                    raise _Exhausted
                if entry.atom not in witnesses:
                    added.add(entry.atom)
                admitted.add(entry.signature)
                nodes += 1
                witnesses.setdefault(entry.atom, []).append(
                    SupportWitness(
                        atom=entry.atom,
                        kind=WitnessKind.DERIVED,
                        signature=entry.signature,
                        rule_version=entry.rule_version,
                        premises=entry.premises,
                        source_group=entry.source_group,
                        depth=depth + 1,
                        assumptions=used,
                    )
                )
            ready = []
            for atom in added:
                for position in waiting.pop(atom, ()):
                    check(1)
                    missing[position].remove(atom)
                    if not missing[position]:
                        ready.append(position)

        frozen = {atom: tuple(items) for atom, items in witnesses.items()}

        def charge() -> None:
            check(1)

        groups = _source_groups(frozen, ref_groups, check=charge)
        paths = _path_groups(frozen, groups, ref_groups, check=charge)
        assumed = _assumption_backed(frozen, check=charge)
        check()
        return ClosureResult(
            status=ClosureStatus.COMPLETE,
            witnesses=frozen,
            source_groups=groups,
            path_groups=paths,
            assumption_backed=assumed,
            admitted_signatures=frozenset(admitted),
            node_count=nodes,
            node_budget=node_budget,
        )
    except _Exhausted:
        # Do not expose an apparent TRUE from a prefix that omitted its contrary
        # branch. The status is an evaluator limitation, not an UNKNOWN fact.
        return ClosureResult(
            status=ClosureStatus.EVALUATION_INCOMPLETE, node_count=nodes, node_budget=node_budget
        )
