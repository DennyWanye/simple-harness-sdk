#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Fail closed unless every frozen behavioral source has approved provenance."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys


REQUIRED_COLUMNS = (
    "schema_version",
    "source_repository",
    "source_commit",
    "source_path",
    "source_sha256",
    "git_blob_oid",
    "git_authors",
    "source_copyright",
    "source_license",
    "source_notice",
    "third_party",
    "generated",
    "disposition",
    "target_path",
    "license_verdict",
    "license_evidence",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID = re.compile(r"[0-9a-f]{40,64}\Z")


class ProvenanceError(ValueError):
    """The source provenance ledger is incomplete or inconsistent."""


def _load_lock(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ProvenanceError("unsupported source lock schema")
    source_files = data.get("source_files")
    if not isinstance(source_files, dict) or not source_files:
        raise ProvenanceError("source lock has no source_files")
    return data


def _load_manifest(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, dialect="excel-tab")
        columns = tuple(reader.fieldnames or ())
        return columns, list(reader)


def validate(manifest_path: Path, lock_path: Path) -> int:
    lock = _load_lock(lock_path)
    columns, rows = _load_manifest(manifest_path)
    if columns != REQUIRED_COLUMNS:
        raise ProvenanceError(
            "manifest columns differ from the frozen schema: " + ",".join(columns)
        )

    expected = lock["source_files"]
    assert isinstance(expected, dict)
    if len(rows) != len(expected):
        raise ProvenanceError(
            f"expected {len(expected)} source rows, found {len(rows)}"
        )

    seen: set[str] = set()
    targets: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        empty = [column for column in REQUIRED_COLUMNS if not row[column].strip()]
        if empty:
            raise ProvenanceError(
                f"line {line_number} has empty fields: {','.join(empty)}"
            )

        source_path = row["source_path"]
        if source_path in seen:
            raise ProvenanceError(f"duplicate source_path: {source_path}")
        seen.add(source_path)
        if source_path not in expected:
            raise ProvenanceError(f"source is not in frozen lock: {source_path}")
        if row["source_commit"] != lock["source_commit"]:
            raise ProvenanceError(f"source commit drift: {source_path}")
        if row["source_sha256"] != expected[source_path]:
            raise ProvenanceError(f"source SHA-256 drift: {source_path}")
        if not SHA256.fullmatch(row["source_sha256"]):
            raise ProvenanceError(f"malformed source SHA-256: {source_path}")
        if not GIT_OID.fullmatch(row["git_blob_oid"]):
            raise ProvenanceError(f"malformed git blob oid: {source_path}")
        if row["license_verdict"] != "approved":
            raise ProvenanceError(
                f"license verdict must be approved: {source_path}"
            )
        if row["disposition"] != "rewrite":
            raise ProvenanceError(
                f"foundation provenance permits clean rewrites only: {source_path}"
            )
        target = row["target_path"]
        if not target.startswith("src/simple_harness/") or not target.endswith(".py"):
            raise ProvenanceError(f"invalid SDK target: {source_path}")
        targets.add(target)

    missing = set(expected) - seen
    if missing:
        raise ProvenanceError("missing frozen sources: " + ",".join(sorted(missing)))

    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    print(
        f"PROVENANCE_PASS sources={len(rows)} targets={len(targets)} "
        f"manifest_sha256={digest}"
    )
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("source-manifest.tsv"))
    parser.add_argument(
        "--lock", type=Path, default=Path("provenance/source-lock.json")
    )
    args = parser.parse_args(argv)
    try:
        validate(args.manifest, args.lock)
    except (OSError, json.JSONDecodeError, ProvenanceError) as error:
        print(f"PROVENANCE_FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

