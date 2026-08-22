# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""SQLite implementation of durable execution persistence."""

from .database import Database, ExecutionSchemaIncompatible
from .migrations import (
    ExecutionMigrationError,
    ExecutionMigrationManifest,
    LegacyDisposition,
    LegacyIdentityBinding,
    LegacyIdentityMap,
    MigrationManifestEntry,
    migrate_execution_v3_to_v4,
)
from .schema import SCHEMA_VERSION
from .storage import ExecutionStorageError
from .uow import SqliteExecutionUnitOfWork

__all__ = (
    "Database",
    "ExecutionSchemaIncompatible",
    "ExecutionMigrationError",
    "ExecutionMigrationManifest",
    "LegacyDisposition",
    "LegacyIdentityBinding",
    "LegacyIdentityMap",
    "MigrationManifestEntry",
    "ExecutionStorageError",
    "SCHEMA_VERSION",
    "SqliteExecutionUnitOfWork",
    "migrate_execution_v3_to_v4",
)
