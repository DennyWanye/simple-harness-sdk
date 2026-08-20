# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database


@pytest.mark.parametrize("wal", [False, True])
def test_explicit_open_close_and_reopen(tmp_path: Path, wal: bool) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path, wal=wal)
    assert database.is_open
    assert database.path == path.resolve()
    assert database.foreign_keys_enabled
    if wal:
        assert database.journal_mode == "wal"
    else:
        assert database.journal_mode == "delete"
    database.close()
    assert not database.is_open
    database.close()

    with Database.open(path, wal=wal) as reopened:
        assert reopened.schema_version == 2
        assert reopened.is_open
    assert not reopened.is_open


def test_connection_is_unavailable_after_close(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "execution.db")
    database.close()
    with pytest.raises(RuntimeError, match="closed"):
        _ = database.connection


def test_transaction_owner_rejects_nested_transaction(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        with database.transaction():
            with pytest.raises(RuntimeError, match="nested transaction"):
                with database.transaction():
                    pass


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        with pytest.raises(RuntimeError, match="fault"):
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO execution_sessions(session_id, created_at) VALUES (?, ?)",
                    ("session-1", 1.0),
                )
                raise RuntimeError("fault")
        assert database.connection.execute(
            "SELECT count(*) FROM execution_sessions"
        ).fetchone()[0] == 0
