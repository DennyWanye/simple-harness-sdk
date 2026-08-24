# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Owned fresh schema v6 descriptor for SDK execution persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

SCHEMA_VERSION = 6

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


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def fresh_descriptor() -> Migration:
    resources = files("simple_harness.execution.sqlite.migrations")
    sql = resources.joinpath("0005_fresh.sql").read_text(encoding="utf-8") + _V6_CATALOG_COLUMNS
    return Migration(
        SCHEMA_VERSION,
        "0006_fresh",
        sql,
        hashlib.sha256(sql.encode()).hexdigest(),
    )


def migrations() -> tuple[Migration, ...]:
    """Return only the accepted fresh descriptor, never legacy migrations."""

    return (fresh_descriptor(),)


def initial_migration() -> Migration:
    return fresh_descriptor()


__all__ = (
    "SCHEMA_VERSION",
    "Migration",
    "fresh_descriptor",
    "initial_migration",
    "migrations",
)
