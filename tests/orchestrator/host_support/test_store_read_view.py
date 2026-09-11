# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host support S2 review round 2 P1-B: ``Store.read_view`` writes nothing, rolls back on
an exception, and never leaves the Store in a state where later transactions stop being
atomic — even when its own COMMIT fails."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_orchestrator.storage.store import Store, StoreError


def _count(store: Store) -> int:
    return int(store.connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0])


def _store(tmp_path: Path) -> Store:
    store = Store.open(tmp_path / "orchestrator.db")
    store.connection.execute("CREATE TABLE IF NOT EXISTS probe (value INTEGER)")
    return store


def test_a_transaction_inside_a_read_view_is_refused_and_nothing_is_written(tmp_path):
    store = _store(tmp_path)
    try:
        with pytest.raises(StoreError, match="read view"):
            with store.read_view():
                with store.transaction():
                    store.connection.execute("INSERT INTO probe VALUES (1)")
        assert _count(store) == 0
    finally:
        store.close()


def test_an_exception_inside_a_read_view_resets_the_store(tmp_path):
    store = _store(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            with store.read_view():
                raise RuntimeError("boom")
        assert store._depth == 0 and store._reading is False  # noqa: SLF001
        with pytest.raises(ValueError):  # a later transaction is atomic again
            with store.transaction():
                store.connection.execute("INSERT INTO probe VALUES (2)")
                raise ValueError("roll back")
        assert _count(store) == 0
    finally:
        store.close()


class _CommitFails:
    """Delegates to the real connection; the first COMMIT fails."""

    def __init__(self, connection) -> None:  # type: ignore[no-untyped-def]
        self._connection = connection
        self.failed = False

    def execute(self, sql, *args):  # type: ignore[no-untyped-def]
        if sql == "COMMIT" and not self.failed:
            self.failed = True
            self._connection.execute("ROLLBACK")
            raise RuntimeError("commit failed")
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._connection, name)


def test_a_failed_commit_of_a_read_view_still_resets_the_store(tmp_path):
    store = _store(tmp_path)
    real = store._connection  # noqa: SLF001
    store._connection = _CommitFails(real)  # type: ignore[assignment]  # noqa: SLF001
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            with store.read_view():
                pass
        assert store._depth == 0 and store._reading is False  # noqa: SLF001
        store._connection = real  # noqa: SLF001
        with pytest.raises(ValueError):
            with store.transaction():
                store.connection.execute("INSERT INTO probe VALUES (3)")
                raise ValueError("roll back")
        assert _count(store) == 0  # still atomic: the failed view left nothing behind
    finally:
        store._connection = real  # noqa: SLF001
        store.close()
