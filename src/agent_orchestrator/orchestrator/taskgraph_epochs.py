# SPDX-License-Identifier: Apache-2.0
"""One original HTN epoch projection for dispatch and TaskGraph read tokens."""
from __future__ import annotations

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
