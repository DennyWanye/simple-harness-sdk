# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Packaged immutable SQLite migrations."""

from .execution_v3_to_v4 import (
    ExecutionMigrationError,
    ExecutionMigrationManifest,
    LegacyDisposition,
    LegacyIdentityBinding,
    LegacyIdentityMap,
    MigrationManifestEntry,
    migrate_execution_v3_to_v4,
)

__all__ = (
    "ExecutionMigrationError",
    "ExecutionMigrationManifest",
    "LegacyDisposition",
    "LegacyIdentityBinding",
    "LegacyIdentityMap",
    "MigrationManifestEntry",
    "migrate_execution_v3_to_v4",
)
