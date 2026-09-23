#!/usr/bin/env python3
"""Opt-in V1.4 real runtime entry. Provider values remain in process memory."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "agents")]
from real_provider_config import resolve_real_provider
from agent_orchestrator.contracts.htn import MethodRef
from agent_orchestrator.evaluation.htn_batch import main as batch_main, _load_manifest, _require_ignored_root
from agent_orchestrator.evaluation.htn_deployment import build_deployment
from agent_orchestrator.evaluation.htn_method_cohort import execute_method_cohort
from agent_orchestrator.evaluation.htn_oracles import write_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "h8", "h8-probe", "h6", "h6-source", "h6-source-recover"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--resume-interrupted", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--scenario")
    parser.add_argument("--arm")
    parser.add_argument("--trial", type=int, default=0)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--source-manifest", type=Path, help="original candidate source deployment manifest")
    parser.add_argument("--method-ref", type=Path, help="JSON MethodRef from original candidate admission")
    parser.add_argument("--cohort", type=Path, help="explicit cohort/fixture/oracle JSON array")
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()
    if args.command == "h6-source-recover":
        from agent_orchestrator.evaluation.htn_method_source import recover_source_library
        print(json.dumps(recover_source_library(_require_ignored_root(args.root, ROOT))))
        return 0
    provider = resolve_real_provider()
    if provider is None or provider.model not in {"deepseek-flash", "deepseek-v4.1-flash"}:
        raise ValueError("configured V4.1 Flash provider required")
    os.environ.update(SH_BASEURL=provider.base_url, SH_APIKEY=provider.api_key, SH_MODEL=provider.model,
                      H8_DEPLOYMENT_CONFIG=str(args.config.resolve()))
    if args.command in {"freeze", "h8"}:
        arguments = ["--deployment", "agent_orchestrator.evaluation.htn_deployment:build_deployment",
                     "--manifest", str(args.manifest), "--root", str(args.root)]
        if args.command == "freeze":
            arguments.append("--freeze-only")
        if args.resume_interrupted:
            arguments.append("--resume-interrupted")
        return batch_main(arguments)
    executor, configured = build_deployment()
    frozen = _load_manifest(args.manifest)
    if configured.fingerprint != frozen.fingerprint:
        raise ValueError("actual deployment differs from frozen manifest")
    root = _require_ignored_root(args.root, ROOT)
    if args.command in {"h8-probe", "h6-source"}:
        if not args.run_id and not (args.scenario and args.arm):
            parser.error("h8-probe requires --run-id or --scenario and --arm")
        runs = [run for run in frozen.runs() if (run.run_id == args.run_id if args.run_id else
                run.scenario.scenario_id == args.scenario and str(run.arm) == args.arm and run.trial == args.trial)]
        if len(runs) != 1:
            parser.error("--run-id must identify exactly one frozen run")
        executor.preflight(frozen)
        if args.command == "h6-source":
            from agent_orchestrator.evaluation.htn_method_source import execute_method_source
            result = asyncio.run(execute_method_source(executor=executor, manifest=frozen,
                run=runs[0], root=root))
            print(json.dumps({"scope": "H6-real-source", "state": result["state"],
                              "candidate_count": len(result["candidates"])}))
            return 0 if result["state"] == "CANDIDATES_AVAILABLE" else 1
        receipt = asyncio.run(executor.execute(frozen, runs[0], root, resume=args.resume_interrupted))
        from dataclasses import asdict
        write_evidence(root, "probe-receipt.json", asdict(receipt))
        print(json.dumps({"scope": "single-runtime-probe-not-H8-matrix", "run_id": runs[0].run_id,
                          "status": receipt.terminal_status, "domain_success": receipt.domain_success}))
        return 0 if receipt.domain_success else 1
    if not args.runtime_root or not args.method_ref or not args.cohort:
        parser.error("h6 requires --runtime-root, --method-ref and --cohort")
    _require_ignored_root(args.runtime_root, ROOT)
    result = asyncio.run(execute_method_cohort(executor=executor, manifest=frozen,
        method=MethodRef.from_json(json.loads(args.method_ref.read_text())),
        runtime_root=args.runtime_root, evidence_root=root,
        scenarios=json.loads(args.cohort.read_text()), promote=args.promote,
        source_manifest=None if args.source_manifest is None else _load_manifest(args.source_manifest)))
    print(json.dumps({"scope": "H6-real-cohort", "state": result["state"]}))
    return 0 if result["state"] in {"EVALUATED", "ADMITTED"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        import traceback
        frames = [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                  for frame in traceback.extract_tb(error.__traceback__)]
        print(json.dumps({"status": "STOPPED", "error_type": type(error).__name__, "frames": frames}), file=sys.stderr)
        raise SystemExit(2)
