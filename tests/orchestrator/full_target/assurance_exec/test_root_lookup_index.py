# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-26: the Assurance root lookup has an index.

A real Host library held 475k ``AssuranceClockObserved`` receipts (written by an
orchestration loop that never slept, fixed in ``_start_planning``), and the root
check scanned every commit receipt on each cycle: 78.7 ms per lookup, the event
loop at 100% CPU.  Clock observations keep their original one-receipt-per-change
semantics; only the lookup is indexed.
"""

from __future__ import annotations

from agent_orchestrator.storage import schema


def test_the_root_lookup_has_an_index() -> None:
    # Looked up by name: a later migration (29, NEXT-TG-1.0 2A) must not hide it.
    [index] = [m for m in schema.MIGRATIONS if m.name == "orchestrator-commit-receipts-kind-index"]
    assert "commit_receipts(kind)" in index.ddl
