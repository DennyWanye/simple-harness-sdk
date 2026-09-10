# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Owned fresh schema v10 descriptor for SDK execution persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

SCHEMA_VERSION = 10

_V6_CATALOG_COLUMNS = """
ALTER TABLE tool_catalog_snapshots ADD COLUMN provider_specs_fingerprint TEXT
    CHECK(provider_specs_fingerprint IS NULL OR length(provider_specs_fingerprint) = 64);
ALTER TABLE tool_catalog_snapshots ADD COLUMN catalog_envelope_json TEXT;
ALTER TABLE tool_catalog_snapshots ADD COLUMN catalog_envelope_digest_v6 TEXT
    CHECK(catalog_envelope_digest_v6 IS NULL OR length(catalog_envelope_digest_v6) = 64);
CREATE UNIQUE INDEX tool_catalog_envelope_digest_v6_idx
    ON tool_catalog_snapshots(catalog_envelope_digest_v6)
    WHERE catalog_envelope_digest_v6 IS NOT NULL;
"""

_V7_MEMORY_AUTHORITY_COLUMNS = """
ALTER TABLE execution_effects ADD COLUMN task_execution_envelope_json TEXT;
ALTER TABLE execution_effects ADD COLUMN task_execution_envelope_hash TEXT
    CHECK(task_execution_envelope_hash IS NULL OR length(task_execution_envelope_hash) = 64);
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def legacy_v7_descriptor() -> Migration:
    resources = files("simple_harness.execution.sqlite.migrations")
    sql = (
        resources.joinpath("0005_fresh.sql").read_text(encoding="utf-8")
        + _V6_CATALOG_COLUMNS
        + _V7_MEMORY_AUTHORITY_COLUMNS
    )
    return Migration(
        7,
        "0007_fresh",
        sql,
        hashlib.sha256(sql.encode()).hexdigest(),
    )


def legacy_v8_descriptor() -> Migration:
    from .context_use import DDL

    sql = legacy_v7_descriptor().sql + DDL
    return Migration(8, "0008_fresh", sql, hashlib.sha256(sql.encode()).hexdigest())


def legacy_v9_descriptor() -> Migration:
    """Frozen v9 descriptor; byte-identical to the pre-v10 ``fresh_descriptor``."""

    from .short_context_schema import DDL

    sql = legacy_v8_descriptor().sql + DDL
    return Migration(9, "0009_fresh", sql, hashlib.sha256(sql.encode()).hexdigest())


def fresh_descriptor() -> Migration:
    from .base_agent.schema import DDL

    sql = legacy_v9_descriptor().sql + DDL
    return Migration(10, "0010_fresh", sql, hashlib.sha256(sql.encode()).hexdigest())


def accepted_descriptor_rows():
    def row(d):
        return (d.version, d.name, d.checksum)
    seven, eight, nine, ten = map(
        row,
        (legacy_v7_descriptor(), legacy_v8_descriptor(), legacy_v9_descriptor(), fresh_descriptor()),
    )
    return (
        # Existing v9 libraries stay openable; the in-place v9 -> v10 upgrader
        # belongs to a later slice.
        (nine,),
        (seven, nine),
        (eight, nine),
        (seven, eight, nine),
        # v10: fresh, or a v9 library upgraded by the future explicit migrator.
        (ten,),
        (nine, ten),
        (seven, nine, ten),
        (eight, nine, ten),
        (seven, eight, nine, ten),
    )


def migrations() -> tuple[Migration, ...]:
    """Return only the accepted fresh descriptor, never legacy migrations."""

    return (fresh_descriptor(),)


def initial_migration() -> Migration:
    return fresh_descriptor()


__all__ = (
    "SCHEMA_VERSION",
    "Migration",
    "accepted_descriptor_rows",
    "fresh_descriptor",
    "initial_migration",
    "legacy_v7_descriptor",
    "legacy_v8_descriptor",
    "legacy_v9_descriptor",
    "migrations",
)
