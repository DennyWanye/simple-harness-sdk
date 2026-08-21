# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from simple_harness.execution.sqlite import Database, ExecutionStorageError


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode")
def test_database_creation_and_reopen_force_owner_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "execution.db"
    with Database.open(path):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with Database.open(path):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.touch()
    link = tmp_path / "execution.db"
    link.symlink_to(target)
    with pytest.raises(ExecutionStorageError):
        Database.open(link)
