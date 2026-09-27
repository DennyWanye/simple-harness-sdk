# SPDX-License-Identifier: Apache-2.0
"""One original HTN epoch projection for dispatch and TaskGraph read tokens."""
from __future__ import annotations

from collections.abc import Mapping

from ..contracts.models import sha256_hex
from ..storage.assurance_reads import MISSION_EPOCH_SCOPE
from ..storage.htn_store import HtnStore
from ..storage.store import Store


def current_scope_epochs(store: Store, mission_id: str) -> dict[str, int]:
    with store.read_view() as db:
        semantics = HtnStore(store)
        scopes = {"mission"}
        scopes.update(str(witness.scope_id) for witness in semantics.list_validity_witnesses(mission_id))
        scopes.update(str(row[0]) for row in db.execute(
            "SELECT scope_id FROM validity_epochs WHERE mission_id=?", (mission_id,)))
        # epoch() owns the original zero-before-first-bump contract. Reading only
        # the physical rows would omit valid initial/witness scopes.
        return {scope: semantics.epoch(mission_id, scope) for scope in sorted(scopes)}


def planning_scope_digest(epochs: Mapping[str, int]) -> str:
    """The scope epochs a planning decision is cut against (``REQUEST_BINDING_STALE``).

    Request binding and admission both hash through here, so they always agree on
    what is counted.  Assurance keeps its own evidence epoch in the same table
    (``assurance:mission``) and bumps it whenever a planning grant or a request
    authority binding is written — which the Host does right *after* a request is
    bound.  That epoch is Assurance's freshness signal for its own checks, not a
    change in what the plan means, so it is left out here.  Counting it made every
    reply of a TaskGraph Mission under Assurance stale until the Mission failed
    (NEXT-TG-1.0 2A behaviour acceptance, mission-9d7dba608eb2aaf2).
    """
    return sha256_hex({str(scope): int(epoch) for scope, epoch in sorted(epochs.items())
                       if str(scope) != MISSION_EPOCH_SCOPE})
