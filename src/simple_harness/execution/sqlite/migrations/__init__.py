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
from .execution_v5_to_v6 import ExecutionV5ToV6MigrationReceipt, migrate_execution_v5_to_v6

__all__ = (
    "ExecutionMigrationError",
    "ExecutionMigrationManifest",
    "ExecutionV5ToV6MigrationReceipt",
    "LegacyDisposition",
    "LegacyIdentityBinding",
    "LegacyIdentityMap",
    "MigrationManifestEntry",
    "migrate_execution_v3_to_v4",
    "migrate_execution_v5_to_v6",
)
