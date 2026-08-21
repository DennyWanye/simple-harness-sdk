# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from check_source_provenance import ProvenanceError, validate  # noqa: E402


def _rows() -> tuple[list[str], list[dict[str, str]]]:
    with (REPOSITORY_ROOT / "source-manifest.tsv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, dialect="excel-tab")
        return list(reader.fieldnames or ()), list(reader)


def _write_manifest(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=columns, dialect="excel-tab", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def test_complete_frozen_source_manifest_is_approved() -> None:
    count = validate(
        REPOSITORY_ROOT / "source-manifest.tsv",
        REPOSITORY_ROOT / "provenance/source-lock.json",
    )
    lock = json.loads((REPOSITORY_ROOT / "provenance/source-lock.json").read_text(encoding="utf-8"))
    assert count == 30 == len(lock["source_files"])


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("git_authors", "", "empty fields"),
        ("license_verdict", "pending", "must be approved"),
        ("source_sha256", "0" * 64, "source SHA-256 drift"),
        ("disposition", "copy", "clean rewrites only"),
    ],
)
def test_gate_rejects_incomplete_or_unapproved_rows(
    tmp_path: Path, column: str, value: str, message: str
) -> None:
    columns, rows = _rows()
    rows[0][column] = value
    candidate = tmp_path / "manifest.tsv"
    _write_manifest(candidate, columns, rows)
    with pytest.raises(ProvenanceError, match=message):
        validate(candidate, REPOSITORY_ROOT / "provenance/source-lock.json")


def test_command_line_gate_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/check_source_provenance.py"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "PROVENANCE_PASS sources=30" in completed.stdout
