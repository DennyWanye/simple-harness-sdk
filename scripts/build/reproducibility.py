# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Build and attest an immutable, reproducible SDK candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
VERSION = str(
    runpy.run_path(str(ROOT / "src/simple_harness/version.py"))["__version__"]
)
_BUILD_SURFACE_FILES = frozenset(
    {
        "CHANGELOG.md",
        "NOTICE",
        "README.md",
        "REUSE.toml",
        "pyproject.toml",
        "source-manifest.tsv",
        "uv.lock",
    }
)
_BUILD_SURFACE_DIRECTORIES = (
    ".github/",
    "docs/",
    "LICENSES/",
    "scripts/",
    "src/",
    "tests/",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        args,
        cwd=ROOT if cwd is None else cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tag_commit(tag: str) -> str | None:
    completed = subprocess.run(
        ("git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _dirty_build_paths() -> tuple[str, ...]:
    status = _run("git", "status", "--porcelain=v1", "--untracked-files=all")
    dirty: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        paths = line[3:].replace('"', "").split(" -> ")
        if any(
            path in _BUILD_SURFACE_FILES
            or path.startswith(_BUILD_SURFACE_DIRECTORIES)
            for path in paths
        ):
            dirty.append(line)
    return tuple(dirty)


def _build(destination: Path) -> None:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = "0"
    subprocess.run(
        ("uv", "build", "--out-dir", str(destination)),
        cwd=ROOT,
        env=environment,
        check=True,
    )


def _artifact_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: sha256(path)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _distribution_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: sha256(path)
        for path in sorted(directory.iterdir())
        if path.is_file()
        and (path.name.endswith(".whl") or path.name.endswith(".tar.gz"))
    }


def build_candidate(
    output: Path,
    *,
    planned_tag: str,
) -> dict[str, object]:
    expected_tag = f"v{VERSION}"
    if planned_tag != expected_tag:
        raise RuntimeError(f"planned_tag must be exactly {expected_tag}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("candidate output directory must be empty")
    dirty = _dirty_build_paths()
    if dirty:
        raise RuntimeError(
            "tracked or untracked build inputs make the candidate non-immutable: "
            + ", ".join(dirty)
        )
    commit = _run("git", "rev-parse", "HEAD")
    existing_tag_commit = _tag_commit(planned_tag)
    if existing_tag_commit is not None and existing_tag_commit != commit:
        raise RuntimeError(
            f"existing tag {planned_tag} points to {existing_tag_commit}, not {commit}"
        )
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="simple-harness-repro-") as raw:
        temporary = Path(raw)
        first, second = temporary / "first", temporary / "second"
        first.mkdir()
        second.mkdir()
        _build(first)
        _build(second)
        first_hashes = _distribution_hashes(first)
        second_hashes = _distribution_hashes(second)
        if first_hashes != second_hashes:
            raise RuntimeError("candidate builds are not byte-for-byte reproducible")
        for path in first.iterdir():
            if path.name in first_hashes:
                shutil.copy2(path, output / path.name)

    build_info = {
        "schema": "simple-harness-build-info-v1",
        "version": VERSION,
        "planned_tag": planned_tag,
        "commit": commit,
        "source_date_epoch": 0,
        "artifacts": first_hashes,
    }
    (output / "BUILD_INFO.json").write_text(
        json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"simple-harness-sdk-{VERSION}",
        "documentNamespace": f"https://simple-harness.invalid/spdx/{commit}",
        "creationInfo": {
            "created": "1970-01-01T00:00:00Z",
            "creators": ["Tool: simple-harness-reproducibility-v1"],
        },
        "packages": [
            {
                "SPDXID": "SPDXRef-Package",
                "name": "simple-harness-sdk",
                "versionInfo": VERSION,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "Apache-2.0",
                "licenseDeclared": "Apache-2.0",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/simple-harness-sdk@{VERSION}",
                    }
                ],
            }
        ],
    }
    (output / "SBOM.spdx.json").write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    shutil.copy2(ROOT / "NOTICE", output / "NOTICE")
    sums = _artifact_hashes(output)
    (output / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="utf-8",
    )
    manifest = {
        "schema": "simple-harness-candidate-manifest-v1",
        "version": VERSION,
        "planned_tag": planned_tag,
        "commit": commit,
        "artifacts": first_hashes,
        "build_info_sha256": sha256(output / "BUILD_INFO.json"),
        "sbom_sha256": sha256(output / "SBOM.spdx.json"),
        "notice_sha256": sha256(output / "NOTICE"),
        "sha256sums_sha256": sha256(output / "SHA256SUMS"),
        "license_owner_receipt": "source-request-ledger:owner-approval-2026-08-13",
    }
    (output / "candidate-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--planned-tag", default=f"v{VERSION}")
    args = parser.parse_args()
    manifest = build_candidate(
        args.output, planned_tag=args.planned_tag
    )
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
