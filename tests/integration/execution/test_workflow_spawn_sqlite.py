# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Real SQLite authority layout for T4.2 workflow-spawn recovery."""

from __future__ import annotations

from pathlib import Path

from simple_harness.execution.sqlite import Database


def test_spawn_recovery_schema_has_dedicated_chain_authorities(tmp_path: Path) -> None:
    with Database.open(tmp_path / "spawn.db") as database:
        expected = {
            "workflow_spawn_continuations",
            "workflow_spawn_completion_receipts",
            "workflow_spawn_child_wait_receipts",
            "workflow_spawn_continuation_ready",
            "workflow_spawn_ready_activations",
        }
        assert expected <= database.table_names()
        assert {
            "activation_receipt_id",
            "ready_receipt_id",
            "spawn_operation_id",
            "predecessor_activation_receipt_id",
            "state",
            "canonical_hash",
        } <= database.column_names("workflow_spawn_ready_activations")
        indexes = {
            str(row[1])
            for row in database.connection.execute(
                "PRAGMA index_list(workflow_spawn_ready_activations)"
            )
        }
        assert "workflow_spawn_ready_one_active_idx" in indexes
