# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from simple_harness.execution.sqlite import SCHEMA_VERSION, Database

EXPECTED_TABLES = {
    "sdk_schema_migrations",
    "execution_sessions",
    "runs",
    "run_start_snapshots",
    "run_events",
    "run_admissions",
    "decisions",
    "continuations",
    "profile_launch_tickets",
    "child_commands",
    "run_links",
    "child_signals",
    "child_signal_ack_receipts",
    "workflow_checkpoints",
    "workflow_leases",
    "provider_invocations",
    "run_fences",
    "execution_effects",
    "delivery_outbox",
    "tool_catalog_snapshots",
    "provider_projection_outbox",
}
FORBIDDEN_PRODUCT_TERMS = {
    "product_sessions",
    "messages",
    "ui_projections",
    "task_grants",
    "capability_projections",
    "deskpet",
}


def test_first_open_creates_only_clean_sdk_schema_v2(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    database = Database.open(path)
    try:
        tables = database.table_names()
        assert EXPECTED_TABLES <= tables
        assert not FORBIDDEN_PRODUCT_TERMS.intersection(tables)
        assert database.schema_version == SCHEMA_VERSION == 2
        rows = database.connection.execute(
            "SELECT version, name, checksum FROM sdk_schema_migrations ORDER BY version"
        ).fetchall()
        assert [tuple(row[:2]) for row in rows] == [
            (1, "0001_initial"),
            (2, "0002_context_authority"),
        ]
        assert all(len(row[2]) == 64 for row in rows)
    finally:
        database.close()


def test_schema_reserves_complete_later_durable_authorities(tmp_path: Path) -> None:
    with Database.open(tmp_path / "execution.db") as database:
        effect_columns = database.column_names("execution_effects")
        assert {
            "request_hash",
            "authorization_receipt_ref",
            "handoff_receipt_ref",
            "evidence_ref",
            "fence_epoch",
            "state",
            "version",
        } <= effect_columns
        assert {
            "request_fingerprint",
            "response_json",
            "usage_json",
            "state",
            "version",
        } <= database.column_names("provider_invocations")
        assert {
            "checkpoint_json",
            "lease_epoch",
        } <= database.column_names("workflow_checkpoints")
        assert {
            "payload_json",
            "state",
            "version",
        } <= database.column_names("delivery_outbox")
        assert {
            "claimed_by",
            "claimed_at",
            "claim_expires_at",
            "claim_epoch",
            "acked_at",
            "ack_receipt_id",
        } <= database.column_names("child_signals")
        assert {
            "signal_id",
            "owner_id",
            "claim_epoch",
            "continuation_id",
            "event_id",
            "continuation_payload_hash",
            "event_payload_hash",
        } <= database.column_names("child_signal_ack_receipts")


def test_database_refuses_foreign_or_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    with Database.open(path) as database:
        database.connection.execute(
            "UPDATE sdk_schema_migrations SET version = 99 WHERE version = 2"
        )
        database.connection.commit()

    with pytest.raises(RuntimeError, match="schema migration history"):
        Database.open(path)


def test_missing_parent_directory_is_not_created_implicitly(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "execution.db"
    with pytest.raises(FileNotFoundError):
        Database.open(path)
    assert not path.parent.exists()
