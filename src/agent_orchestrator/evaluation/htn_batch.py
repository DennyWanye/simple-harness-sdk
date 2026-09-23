# SPDX-License-Identifier: Apache-2.0
"""Small CLI for executing a frozen H8 matrix against a real deployment."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..contracts.models import ContractError
from .htn_executor import RuntimeEpisodeExecutor
from .htn_matrix import H8Manifest, H8RunLedger

DeploymentFactory = Callable[[], tuple[RuntimeEpisodeExecutor, H8Manifest]]


def _load_manifest(path: Path) -> H8Manifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("unable to read the frozen H8 manifest") from error
    if not isinstance(raw, Mapping):
        raise ContractError("the frozen H8 manifest must be a JSON object")
    return H8Manifest.from_json(raw)


def _load_deployment(spec: str) -> tuple[RuntimeEpisodeExecutor, H8Manifest]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ContractError("deployment must use module:factory syntax")
    try:
        factory: Any = getattr(importlib.import_module(module_name), attribute)
        result = factory()
    except Exception as error:
        # Do not include exception text: provider constructors may put secrets or
        # authenticated endpoints in their diagnostics.
        raise ContractError(f"deployment factory failed ({type(error).__name__})") from error
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or not isinstance(result[0], RuntimeEpisodeExecutor)
        or not isinstance(result[1], H8Manifest)
    ):
        raise ContractError("deployment factory must return (RuntimeEpisodeExecutor, H8Manifest)")
    return result


def _require_ignored_root(root: Path, checkout: Path) -> Path:
    resolved_root = root.resolve()
    resolved_checkout = checkout.resolve()
    if not resolved_root.is_relative_to(resolved_checkout) or resolved_root == resolved_checkout:
        raise ContractError("H8 evidence root must be an ignored path inside the frozen checkout")
    relative = resolved_root.relative_to(resolved_checkout)
    check = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", str(relative)],
        cwd=resolved_checkout,
        check=False,
        capture_output=True,
        timeout=10,
    )
    if check.returncode != 0:
        raise ContractError("H8 evidence root is not ignored by the frozen checkout")
    return resolved_root


def _progress(event: dict[str, Any]) -> None:
    # The ledger callback contains only frozen run identity and state.
    print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)


async def run_batch(
    *,
    deployment: str,
    manifest_path: Path,
    root: Path,
    resume_interrupted: bool,
) -> dict[str, int]:
    frozen = _load_manifest(manifest_path)
    executor, configured = _load_deployment(deployment)
    if configured.fingerprint != frozen.fingerprint:
        raise ContractError("deployment factory manifest differs from the supplied frozen manifest")
    evidence_root = _require_ignored_root(root, executor.checkout)
    executor.preflight(frozen)
    ledger = H8RunLedger(evidence_root, frozen)
    try:
        from .htn_process_recovery import ProcessRecoveryExecutor
        return await ledger.run(
            ProcessRecoveryExecutor(executor, deployment, manifest_path),
            resume_interrupted=resume_interrupted,
            on_progress=_progress,
        )
    finally:
        ledger.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the frozen H8 four-arm matrix with a real deployment factory."
    )
    parser.add_argument("--deployment", required=True, metavar="MODULE:FACTORY")
    parser.add_argument("--manifest", required=True, type=Path, metavar="JSON")
    parser.add_argument("--root", required=True, type=Path, metavar="IGNORED_EVIDENCE_DIR")
    parser.add_argument("--resume-interrupted", action="store_true")
    parser.add_argument("--freeze-only", action="store_true",
                        help="write the manifest and required service policy file, without executing an episode")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.freeze_only:
            executor, configured = _load_deployment(args.deployment)
            evidence_root = _require_ignored_root(args.root, executor.checkout)
            manifest_path = _require_ignored_root(args.manifest, executor.checkout)
            from .htn_oracles import write_evidence
            from .appworld_state_observations import AppWorldStatePolicy
            policies = {AppWorldStatePolicy.from_json(s.fixture["state_policy"]).content_hash:
                        s.fixture["state_policy"] for s in configured.scenarios if s.domain == "appworld-v1"}
            write_evidence(manifest_path.parent, manifest_path.name, configured.to_json())
            policy_file = write_evidence(evidence_root, "appworld-state-policies.json",
                                        [policies[key] for key in sorted(policies)])
            print(json.dumps({"manifest": str(manifest_path), "manifest_hash": configured.fingerprint,
                "appworld_service_policy_file": str(evidence_root / policy_file.relative_path),
                "required_service_argument": "--state-policy-file", "episodes_executed": 0}))
            return 0
        counts = asyncio.run(
            run_batch(
                deployment=args.deployment,
                manifest_path=args.manifest,
                root=args.root,
                resume_interrupted=args.resume_interrupted,
            )
        )
    except BaseException as error:
        # This CLI is frequently pointed at authenticated providers. Keep stderr
        # useful while never reproducing provider messages, URLs or credentials.
        print(f"H8 batch stopped: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps({"states": counts}, sort_keys=True, separators=(",", ":")))
    return 0 if counts.get("FAIL", 0) == 0 and counts.get("INTERRUPTED", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
