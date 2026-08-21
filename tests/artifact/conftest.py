# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class BuildArtifacts(NamedTuple):
    first: Path
    second: Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build(destination: Path) -> None:
    subprocess.run(
        ["uv", "build", "--out-dir", str(destination)],
        cwd=REPOSITORY_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.fixture(scope="session")
def reproducible_artifacts(tmp_path_factory: pytest.TempPathFactory) -> BuildArtifacts:
    root = tmp_path_factory.mktemp("reproducible-build")
    first = root / "first"
    second = root / "second"
    first.mkdir()
    second.mkdir()
    _build(first)
    _build(second)
    return BuildArtifacts(first=first, second=second)


@pytest.fixture(scope="session")
def python_executable() -> Path:
    return Path(sys.executable)
