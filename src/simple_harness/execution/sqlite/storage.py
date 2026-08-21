# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed creation and validation for execution SQLite files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class ExecutionStorageError(RuntimeError):
    code = "execution_storage_unsafe"


def prepare_execution_database(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    parent = candidate.parent.resolve(strict=True)
    if not parent.is_dir():
        raise FileNotFoundError(f"database parent directory does not exist: {parent}")
    resolved = parent / candidate.name
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags, 0o600)
    except OSError as error:
        raise ExecutionStorageError("execution database cannot be opened safely") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ExecutionStorageError("execution database must be a regular file")
        if os.name == "posix":
            if details.st_uid != os.getuid():
                raise ExecutionStorageError("execution database must be owner-managed")
            os.fchmod(descriptor, 0o600)
            verified = os.fstat(descriptor)
            if stat.S_IMODE(verified.st_mode) != 0o600:
                raise ExecutionStorageError("execution database mode must be 0600")
    finally:
        os.close(descriptor)
    final = resolved.lstat()
    if not stat.S_ISREG(final.st_mode) or stat.S_ISLNK(final.st_mode):
        raise ExecutionStorageError("execution database path changed during validation")
    if os.name == "posix" and (
        final.st_uid != os.getuid() or stat.S_IMODE(final.st_mode) != 0o600
    ):
        raise ExecutionStorageError("execution database ownership changed during validation")
    return resolved


__all__ = ("ExecutionStorageError", "prepare_execution_database")
