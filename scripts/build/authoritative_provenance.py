#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Emit or verify canonical metadata around immutable CI-built distributions."""

from __future__ import annotations

import argparse
import hashlib
import re
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEYS = (
    "package",
    "version",
    "source_commit",
    "requires_python",
    "build_utc",
    "wheel_sha256",
    "sdist_sha256",
)


class ProvenanceError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def project_identity() -> tuple[str, str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    namespace: dict[str, object] = {}
    exec(
        (ROOT / "src/simple_harness/version.py").read_text(encoding="utf-8"),
        namespace,
    )
    name = project["name"]
    version = namespace["__version__"]
    requires_python = project["requires-python"]
    if not isinstance(name, str) or not name:
        raise ProvenanceError("project identity is invalid")
    if not isinstance(version, str) or not version:
        raise ProvenanceError("project identity is invalid")
    if not isinstance(requires_python, str) or not requires_python:
        raise ProvenanceError("project identity is invalid")
    return name, version, requires_python


def _artifacts(dist: Path) -> tuple[Path, Path]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted((*dist.glob("*.tar.gz"), *dist.glob("*.zip")))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ProvenanceError("dist must contain exactly one wheel and one sdist")
    return wheels[0], sdists[0]


def _validate_identity(source_commit: str, build_utc: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ProvenanceError("source_commit must be a full lowercase Git SHA")
    if not build_utc.endswith("Z"):
        raise ProvenanceError("build_utc must be UTC ISO-8601")
    try:
        datetime.fromisoformat(build_utc[:-1] + "+00:00")
    except ValueError as error:
        raise ProvenanceError("build_utc must be UTC ISO-8601") from error


def emit(dist: Path, *, source_commit: str, build_utc: str) -> None:
    _validate_identity(source_commit, build_utc)
    wheel, sdist = _artifacts(dist)
    package, version, requires_python = project_identity()
    sums = {path.name: sha256(path) for path in (wheel, sdist)}
    (dist / "SHA256SUMS").write_text(
        "".join(f"{sums[name]}  {name}\n" for name in sorted(sums)),
        encoding="utf-8",
    )
    values = {
        "package": package,
        "version": version,
        "source_commit": source_commit,
        "requires_python": requires_python,
        "build_utc": build_utc,
        "wheel_sha256": sums[wheel.name],
        "sdist_sha256": sums[sdist.name],
    }
    (dist / "BUILD_INFO.txt").write_text(
        "".join(f"{key}={values[key]}\n" for key in KEYS),
        encoding="utf-8",
    )


def verify(
    dist: Path,
    *,
    source_commit: str | None = None,
    version: str | None = None,
) -> None:
    wheel, sdist = _artifacts(dist)
    lines = (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected_names = sorted((wheel.name, sdist.name))
    if len(lines) != 2:
        raise ProvenanceError("SHA256SUMS must contain exactly wheel and sdist")
    sums: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+)", line)
        if match is None or match.group(2) in sums:
            raise ProvenanceError("SHA256SUMS is not canonical")
        sums[match.group(2)] = match.group(1)
    if list(sums) != expected_names:
        raise ProvenanceError("SHA256SUMS filenames are not canonical")
    for artifact in (wheel, sdist):
        if sums[artifact.name] != sha256(artifact):
            raise ProvenanceError("artifact checksum differs")
    values: dict[str, str] = {}
    for line in (dist / "BUILD_INFO.txt").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not value or key in values:
            raise ProvenanceError("BUILD_INFO is not canonical")
        values[key] = value
    if tuple(values) != KEYS:
        raise ProvenanceError("BUILD_INFO keys are not canonical")
    _validate_identity(values["source_commit"], values["build_utc"])
    package, project_version, requires_python = project_identity()
    expected = {
        "package": package,
        "version": project_version if version is None else version,
        "requires_python": requires_python,
        "wheel_sha256": sums[wheel.name],
        "sdist_sha256": sums[sdist.name],
    }
    if source_commit is not None:
        expected["source_commit"] = source_commit
    if any(values.get(key) != value for key, value in expected.items()):
        raise ProvenanceError("BUILD_INFO identity differs")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("emit", "verify"))
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--version")
    parser.add_argument("--build-utc")
    args = parser.parse_args()
    if args.mode == "emit":
        if args.source_commit is None or args.build_utc is None:
            parser.error("emit requires --source-commit and --build-utc")
        emit(
            args.dist,
            source_commit=args.source_commit,
            build_utc=args.build_utc,
        )
    else:
        verify(
            args.dist,
            source_commit=args.source_commit,
            version=args.version,
        )


if __name__ == "__main__":
    main()
