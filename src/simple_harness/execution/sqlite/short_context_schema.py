"""Execution9 compatibility boundary for exact immutable short source identity."""

DDL = """
CREATE TABLE short_context_upgrade_receipt (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 receipt_json TEXT NOT NULL, receipt_hash TEXT NOT NULL CHECK(length(receipt_hash)=64)
) STRICT;
CREATE TRIGGER short_context_upgrade_no_update BEFORE UPDATE ON short_context_upgrade_receipt
 BEGIN SELECT RAISE(ABORT,'short_context_upgrade_immutable'); END;
CREATE TRIGGER short_context_upgrade_no_delete BEFORE DELETE ON short_context_upgrade_receipt
 BEGIN SELECT RAISE(ABORT,'short_context_upgrade_immutable'); END;
"""
