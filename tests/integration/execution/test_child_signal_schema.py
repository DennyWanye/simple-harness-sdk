# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_atomic_child_terminal_signal import finalize, setup_child


def test_schema_rejects_claim_without_complete_lease(tmp_path: Path) -> None:
    database, uow = setup_child(tmp_path / "execution.db")
    try:
        finalize(uow)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            database.connection.execute(
                "UPDATE child_signals SET state = 'claimed' WHERE signal_id = 'signal-1'"
            )
        record = uow.read_child_signal("signal-1")
        assert record is not None and record.state.value == "pending"
    finally:
        database.close()


def test_schema_preserves_monotonic_epoch_across_expired_reclaim(
    tmp_path: Path,
) -> None:
    database, uow = setup_child(tmp_path / "execution.db")
    try:
        finalize(uow)
        database.connection.execute(
            """
            UPDATE child_signals
            SET state = 'claimed', version = version + 1,
                claimed_by = 'runtime-a', claimed_at = 10.0,
                claim_expires_at = 20.0, claim_epoch = claim_epoch + 1,
                updated_at = 10.0
            WHERE signal_id = 'signal-1' AND state = 'pending'
            """
        )
        database.connection.execute(
            """
            UPDATE child_signals
            SET version = version + 1, claimed_by = 'runtime-b',
                claimed_at = 21.0, claim_expires_at = 31.0,
                claim_epoch = claim_epoch + 1, updated_at = 21.0
            WHERE signal_id = 'signal-1' AND state = 'claimed'
              AND claim_expires_at <= 21.0
            """
        )
        row = database.connection.execute(
            """
            SELECT claimed_by, claim_epoch, version
            FROM child_signals WHERE signal_id = 'signal-1'
            """
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("runtime-b", 2, 2)
    finally:
        database.close()


def test_schema_rejects_ack_without_durable_receipt(tmp_path: Path) -> None:
    database, uow = setup_child(tmp_path / "execution.db")
    try:
        finalize(uow)
        database.connection.execute(
            """
            UPDATE child_signals
            SET state = 'claimed', version = version + 1,
                claimed_by = 'runtime-a', claimed_at = 10.0,
                claim_expires_at = 20.0, claim_epoch = claim_epoch + 1,
                updated_at = 10.0
            WHERE signal_id = 'signal-1' AND state = 'pending'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            database.connection.execute(
                """
                UPDATE child_signals
                SET state = 'acked', version = version + 1, acked_at = 12.0
                WHERE signal_id = 'signal-1'
                """
            )
        row = database.connection.execute(
            """
            SELECT state, acked_at, ack_receipt_id
            FROM child_signals WHERE signal_id = 'signal-1'
            """
        ).fetchone()
        assert row is not None and tuple(row) == ("claimed", None, None)
    finally:
        database.close()
