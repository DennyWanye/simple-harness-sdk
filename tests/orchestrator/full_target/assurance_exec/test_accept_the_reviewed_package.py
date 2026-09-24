# SPDX-License-Identifier: Apache-2.0
"""On the Assurance lane an acceptance takes the package the official review judged.

Host native run arp.14 (2026-09-24): the first assured ACCEPT of a document task crashed
six cycles on ``review package ... already stored`` — Assurance froze the package before
its Critic (criteria from the approved check policy), the acceptance reader rebuilt it
from the layers that ran (a different criteria spelling, sorted), and the second insert
of the same package id conflicted.  The reviewed package is accepted when its subject is
the same; a different subject is still refused by the store.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_aer_contract_codecs import criterion, review_package  # noqa: E402

from agent_orchestrator.contracts.resolution import CriterionExpr  # noqa: E402
from agent_orchestrator.orchestrator.leaf_acceptance import LeafAcceptanceAssembly  # noqa: E402
from agent_orchestrator.storage.store import StoreConflict, StoreError  # noqa: E402


class Semantics:
    def __init__(self, stored=None):  # type: ignore[no-untyped-def]
        self.stored, self.inserted = stored, []

    def get_review_package(self, package_id):  # type: ignore[no-untyped-def]
        if self.stored is None:
            raise StoreError("none")
        return self.stored

    def insert_review_package(self, package):  # type: ignore[no-untyped-def]
        if self.stored is not None:
            raise StoreConflict("review package already stored")
        self.inserted.append(package)


def _assembly(semantics):  # type: ignore[no-untyped-def]
    class Probe(LeafAcceptanceAssembly):
        pass

    Probe.semantics = semantics  # type: ignore[assignment,misc]  # shadows the store-backed property
    return Probe.__new__(Probe)


def _packages():  # type: ignore[no-untyped-def]
    reviewed_view = criterion("c-notes-file-written")
    accept_view = replace(reviewed_view, statement=reviewed_view.statement + " (checks that ran)")
    frozen = review_package(CriterionExpr(reviewed_view.criterion_id), reviewed_view)
    rebuilt = replace(frozen, criteria=(accept_view,))
    assert frozen.content_hash() != rebuilt.content_hash()
    return frozen, rebuilt


def test_assured_acceptance_takes_the_reviewed_package() -> None:
    frozen, rebuilt = _packages()
    semantics = Semantics(stored=frozen)
    assert _assembly(semantics)._stored_package(rebuilt, reviewed=True) == frozen
    assert semantics.inserted == []


def test_outside_the_assured_lane_a_differing_package_is_still_a_conflict() -> None:
    frozen, rebuilt = _packages()
    with pytest.raises(StoreConflict):
        _assembly(Semantics(stored=frozen))._stored_package(rebuilt, reviewed=False)


def test_a_different_subject_is_never_taken_over() -> None:
    frozen, rebuilt = _packages()
    other = replace(rebuilt, binding=replace(rebuilt.binding, input_manifest_hash="e" * 64))
    with pytest.raises(StoreConflict):
        _assembly(Semantics(stored=frozen))._stored_package(other, reviewed=True)


def test_a_first_acceptance_inserts_its_package() -> None:
    _, rebuilt = _packages()
    semantics = Semantics()
    assert _assembly(semantics)._stored_package(rebuilt, reviewed=True) == rebuilt
    assert semantics.inserted == [rebuilt]
