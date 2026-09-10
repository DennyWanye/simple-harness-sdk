# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""SQLite persistence helpers owned by the BaseAgent layer (execution schema v10).

Every helper in this package accepts the caller's open connection and never opens
its own transaction; ``SqliteExecutionUnitOfWork`` stays the single transaction
owner.
"""
