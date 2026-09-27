# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Generate and verify the installed TaskGraph derivation manifest (NEXT-TG-1.0 §6.2).

``agent_orchestrator/orchestrator/taskgraph_deployment_manifest.json`` tells
``InstalledHtnWiringAcceptance`` which real HTN core-chain run this package derives
from and exactly which files it installs.  It is a build product, not a test verdict:

``generate --upstream EVIDENCE.json``
    Hashes the upstream evidence *files* (wheel, candidate manifest, Mission receipt,
    cold-replay receipt) — never a hash copied from a document — then builds the wheel
    once, takes the inventory from what the wheel actually installs under
    ``agent_orchestrator/`` and ``simple_harness/`` (the reader's own rule: no
    ``__pycache__``, no ``.pyc/.pyo``, not the manifest itself), and writes the manifest
    with ``deployment_id`` = sha256 of the canonical manifest without that field.
    ``taskgraph_acceptance`` stays ``NOT_RUN``; this script never writes ``VALIDATED``.

``verify --wheel W``
    Unpacks W into a fresh directory, imports the package from there (not from
    ``src/``) and runs the original reader.  Then proves the reader refuses: a missing
    file, a file whose bytes changed, a changed manifest (identity mismatch), and
    missing upstream evidence.

The evidence file is JSON: ``{"mission_id", "wheel", "candidate_manifest",
"mission_receipt", "cold_replay_receipt"}`` with paths to the actual files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_REL = "agent_orchestrator/orchestrator/taskgraph_deployment_manifest.json"
NAMESPACES = ("agent_orchestrator", "simple_harness")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> str:
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from simple_harness.contracts import canonical_json
    finally:
        sys.path.pop(0)
    return canonical_json(value)


def upstream_from(evidence_path: Path) -> dict[str, str]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    base = evidence_path.parent
    files = {key: (base / evidence[key]).resolve()
             for key in ("wheel", "candidate_manifest", "mission_receipt", "cold_replay_receipt")}
    for key, path in files.items():
        if not path.is_file():
            raise SystemExit(f"upstream evidence missing: {key} -> {path}")
    mission_id = evidence["mission_id"]
    wheel_sha = _sha(files["wheel"].read_bytes())
    candidate = json.loads(files["candidate_manifest"].read_text(encoding="utf-8"))
    if candidate.get("artifacts", {}).get(files["wheel"].name) != wheel_sha:
        raise SystemExit("candidate manifest does not name this wheel's bytes")
    receipt_bytes = files["mission_receipt"].read_bytes()
    receipt = json.loads(receipt_bytes)
    replay = json.loads(files["cold_replay_receipt"].read_text(encoding="utf-8"))
    if receipt.get("mission_id") != mission_id or receipt.get("status") != "COMPLETED":
        raise SystemExit("Mission receipt is not this completed Mission")
    if receipt.get("sdk_wheel_sha256") != wheel_sha:
        raise SystemExit("Mission receipt was not produced on this wheel")
    if (replay.get("mission_id") != mission_id or replay.get("unchanged") is not True
            or replay.get("mission_receipt_sha256") != _sha(receipt_bytes)):
        raise SystemExit("cold-replay receipt does not prove this Mission receipt unchanged")
    return {
        "status": "READY_FOR_TASKGRAPH_WIRING",
        "scope": "CORE_INTEGRATION_E2E",
        "wheel_sha256": wheel_sha,
        "manifest_sha256": _sha(files["candidate_manifest"].read_bytes()),
        "source_inputs_sha256": candidate["provenance"]["source_inputs_sha256"],
        "mission_id": mission_id,
        "mission_receipt_sha256": _sha(receipt_bytes),
        "cold_replay_receipt_sha256": _sha(files["cold_replay_receipt"].read_bytes()),
        # The legacy replay projection still reports unknown event kinds (09-22 note).
        "semantic_replay": "PARTIAL",
    }


def wheel_inventory(wheel: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if (info.is_dir() or name.parts[0] not in NAMESPACES or "__pycache__" in name.parts
                    or name.suffix in {".pyc", ".pyo"} or str(name) == MANIFEST_REL):
                continue
            inventory[str(name)] = _sha(archive.read(info))
    return inventory


def build_wheel(into: Path) -> Path:
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(into)], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    wheels = sorted(into.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel, built {len(wheels)}")
    return wheels[0]


def generate(evidence: Path) -> dict[str, object]:
    upstream = upstream_from(evidence)
    with tempfile.TemporaryDirectory(prefix="tg-manifest-") as raw:
        sources = wheel_inventory(build_wheel(Path(raw)))
    identity = {"schema": "taskgraph-htn-wiring-derivation-v1", "upstream": upstream,
                "source_files": sources, "taskgraph_acceptance": "NOT_RUN"}
    value = {**identity, "deployment_id": _sha(_canonical(identity).encode())}
    target = ROOT / "src" / MANIFEST_REL
    target.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"manifest": str(target), "deployment_id": value["deployment_id"], "source_files": len(sources),
            "upstream_mission": upstream["mission_id"], "upstream_wheel_sha256": upstream["wheel_sha256"]}


_READ = (
    "import sys; sys.path.insert(0, sys.argv[1]);"
    "from agent_orchestrator.orchestrator.taskgraph_deployment import InstalledHtnWiringAcceptance as R;"
    "r = R(); assert str(r.root) == sys.argv[1], (r.root, sys.argv[1]);"
    "print(r._read()['deployment_id'])"
)


def _read(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", _READ, str(root)], capture_output=True, text=True, check=False)


def verify(wheel: Path) -> dict[str, object]:
    results: dict[str, object] = {"wheel": str(wheel), "wheel_sha256": _sha(wheel.read_bytes())}
    with tempfile.TemporaryDirectory(prefix="tg-verify-") as raw:
        base = (Path(raw) / "installed").resolve()
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(base)
        good = _read(base)
        if good.returncode != 0:
            raise SystemExit(f"reader refused the built package:\n{good.stderr}")
        results["deployment_id"] = good.stdout.strip()
        manifest = json.loads((base / MANIFEST_REL).read_text(encoding="utf-8"))
        # A module the reader does not import itself, so the refusal is the reader's
        # inventory check and not an import failure of the reader.
        victim = next(name for name in sorted(manifest["source_files"]) if "/testing/" in name)

        def counterexample(name: str, mutate) -> None:
            copy = (Path(raw) / name).resolve()
            shutil.copytree(base, copy)
            mutate(copy)
            outcome = _read(copy)
            refused = outcome.returncode != 0 and "taskgraph_deployed_source_unverified" in outcome.stderr
            results[f"refuses_{name}"] = refused
            if not refused:
                raise SystemExit(f"counterexample {name} was not refused by the reader:\n"
                                 f"{outcome.stdout}{outcome.stderr}")

        counterexample("missing_file", lambda root: (root / victim).unlink())
        counterexample("changed_bytes", lambda root: (root / victim).write_bytes(
            (root / victim).read_bytes() + b"\n# changed\n"))

        def changed_manifest(root: Path) -> None:
            value = json.loads((root / MANIFEST_REL).read_text(encoding="utf-8"))
            value["upstream"]["mission_id"] = value["upstream"]["mission_id"] + "-other"
            (root / MANIFEST_REL).write_text(json.dumps(value), encoding="utf-8")

        def missing_evidence(root: Path) -> None:
            value = json.loads((root / MANIFEST_REL).read_text(encoding="utf-8"))
            del value["upstream"]["cold_replay_receipt_sha256"]
            (root / MANIFEST_REL).write_text(json.dumps(value), encoding="utf-8")

        counterexample("changed_manifest", changed_manifest)
        counterexample("missing_evidence", missing_evidence)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("generate")
    make.add_argument("--upstream", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    result = generate(args.upstream.resolve()) if args.command == "generate" else verify(args.wheel.resolve())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
