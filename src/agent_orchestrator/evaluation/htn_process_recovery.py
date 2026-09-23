"""Parent-owned SIGKILL and cold worker restart for frozen H8 recovery runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..planning.htn.cross_domain_acceptance import ScenarioKind
from .appworld_resume import durable_document, read_document
from .htn_matrix import EpisodeReceipt


class ProcessRecoveryExecutor:
    def __init__(self, ordinary: Any, deployment: str, manifest_path: Path):
        self.ordinary, self.deployment, self.manifest_path = (
            ordinary,
            deployment,
            manifest_path.resolve(),
        )

    async def execute(self, manifest: Any, run: Any, root: Path, *, resume: bool) -> EpisodeReceipt:
        if run.scenario.kind != ScenarioKind.RECOVERY:
            return await self.ordinary.execute(manifest, run, root, resume=resume)
        root = root.resolve()
        state_path, result_path = root / "recovery-state.json", root / "process-result.json"
        if result_path.exists():
            result = EpisodeReceipt.from_json(read_document(result_path))
            result.verify(manifest, run, root)
            return result
        for generation in range(2):
            command = [
                sys.executable,
                "-m",
                __name__,
                "--deployment",
                self.deployment,
                "--manifest",
                str(self.manifest_path),
                "--root",
                str(root),
                "--run-id",
                run.run_id,
            ]
            if resume:
                command.append("--resume")
            with (root / f"worker-{generation}.log").open("ab") as log:
                proc = await asyncio.create_subprocess_exec(*command, stdout=log, stderr=log)
                killed = False
                try:
                    async with asyncio.timeout(manifest.budget.seconds + 60):
                        while proc.returncode is None:
                            if state_path.exists():
                                state = read_document(state_path)
                                if state.get("phase") == "KILL_READY":
                                    if (
                                        state.get("worker_pid") != proc.pid
                                        or state.get("parent_pid") != os.getpid()
                                        or state.get("manifest_hash") != manifest.fingerprint
                                        or state.get("identity")
                                        != content_hash_of([run.run_id, run.scenario.to_json()])
                                    ):
                                        raise ContractError(
                                            "kill checkpoint belongs to another process/run"
                                        )
                                    proc.kill()  # only the child owned by this supervisor
                                    returncode = await proc.wait()
                                    if returncode != -signal.SIGKILL:
                                        raise ContractError("worker did not terminate by SIGKILL")
                                    state.update(
                                        phase="KILLED",
                                        killed_pid=proc.pid,
                                        kill_returncode=returncode,
                                        supervisor_pid=os.getpid(),
                                    )
                                    durable_document(state_path, state)
                                    killed, resume = True, True
                                    break
                            await asyncio.sleep(0.1)
                        if not killed:
                            await proc.wait()
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
            if killed:
                continue
            if proc.returncode != 0 or not result_path.exists():
                raise ContractError("H8 worker failed; original evidence and budget retained")
            result = EpisodeReceipt.from_json(read_document(result_path))
            result.verify(manifest, run, root)
            return result
        raise ContractError("H8 recovery exceeded its one cold restart")


async def _worker(args: Any) -> None:
    from .htn_batch import _load_deployment, _load_manifest, _require_ignored_root

    executor, configured = _load_deployment(args.deployment)
    manifest = _load_manifest(args.manifest)
    if configured.fingerprint != manifest.fingerprint:
        raise ContractError("worker deployment differs from frozen manifest")
    run = next((r for r in manifest.runs() if r.run_id == args.run_id), None)
    if run is None or run.scenario.kind != ScenarioKind.RECOVERY:
        raise ContractError("worker requires the exact frozen recovery run")
    root = _require_ignored_root(args.root, executor.checkout)
    executor.process_recovery = True
    result = await executor.execute(manifest, run, root, resume=args.resume)
    result.verify(manifest, run, root)
    durable_document(root / "process-result.json", asdict(result))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(_worker(args))
    except BaseException as error:
        print(json.dumps({"worker_error": type(error).__name__}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
