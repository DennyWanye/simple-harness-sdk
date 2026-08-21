# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database


def test_integrity_and_foreign_key_checks_pass_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    with Database.open(path) as database:
        assert database.integrity_check() == ("ok",)
        assert database.foreign_key_violations() == ()

    with Database.open(path) as reopened:
        assert reopened.integrity_check() == ("ok",)
        assert reopened.foreign_key_violations() == ()


def test_foreign_keys_fail_closed(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        with pytest.raises(Exception, match="FOREIGN KEY"):
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, execution_session_id, request_id, root_run_id,
                        profile_key, driver_kind, state, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "run-1",
                        "missing-session",
                        "request-1",
                        "run-1",
                        "agent.general",
                        "react",
                        "created",
                        0,
                        1.0,
                        1.0,
                    ),
                )
