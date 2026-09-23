# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Build a *development* candidate wheel from this checkout and attest its inputs.

Unlike ``reproducibility.py`` (an immutable release candidate from a clean tagged
tree), this records exactly what was built: the monorepo commit, whether the
tree was dirty, and a digest over every source input under ``src/``. The Host
pins the wheel through ``deskpet/sdk_adapters/sdk_candidate.py`` and its
``candidate-manifest.json`` (schema ``simple-harness-candidate-manifest-v1``,
``development_candidate: true``), the same shape earlier development pins used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def source_inputs() -> list[tuple[str, str]]:
    rows = []
    for path in sorted((ROOT / "src").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            rows.append((path.relative_to(ROOT).as_posix(), sha256(path)))
    return rows


def build(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sh-dev-candidate-") as raw:
        temporary = Path(raw)
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(temporary)], cwd=ROOT, check=True)
        wheels = sorted(temporary.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel, built {len(wheels)}")
        wheel = wheels[0]
        target = output / wheel.name
        shutil.copy2(wheel, target)
    inputs = source_inputs()
    version = target.name.split("-")[1]
    manifest = {
        "artifacts": {target.name: sha256(target)},
        "commit": _git("rev-parse", "HEAD") or "unknown",
        "development_candidate": True,
        "planned_tag": None,
        "provenance": {
            "release_published": False,
            "source_commit": _git("rev-parse", "HEAD") or "unknown",
            "source_input_count": len(inputs),
            "source_inputs_sha256": hashlib.sha256(
                json.dumps(inputs, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "source_root": str(ROOT),
            "version_rewrites": [],
            "working_tree_dirty": bool(_git("status", "--porcelain", "--", str(ROOT))),
        },
        "schema": "simple-harness-candidate-manifest-v1",
        "version": version,
    }
    manifest_path = output / f"{target.stem.split('-py3')[0]}.candidate-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "wheel": str(target),
        "wheel_sha256": manifest["artifacts"][target.name],
        "manifest": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "version": version,
        "commit": manifest["commit"],
        "working_tree_dirty": manifest["provenance"]["working_tree_dirty"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="directory receiving the wheel and manifest")
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), indent=2))


if __name__ == "__main__":
    main()
