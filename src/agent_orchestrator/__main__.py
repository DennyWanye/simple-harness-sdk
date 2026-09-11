# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""``python -m agent_orchestrator`` — the operator / demo CLI (ORCH-BUILD §14.3).

Subcommands (step 2):

    mission create --tenant T --evidence-dir DIR --spec spec.json [--provider ...]
    mission get|cancel|events --evidence-dir DIR MISSION_ID
    attempt get --evidence-dir DIR ATTEMPT_ID
    artifact show --evidence-dir DIR ARTIFACT_ID
    demo --scenario single-task|static-dag|knowledge-sharing --provider fixtures|env --evidence-dir DIR

``--provider env`` reads ``SH_BASEURL`` / ``SH_APIKEY`` / ``SH_MODEL`` (and optional
``SH_PRICE_INPUT_MICROS`` / ``SH_PRICE_OUTPUT_MICROS`` per million tokens) from the
environment; the key never reaches any file.  Scenarios of later steps report
``not_implemented`` with exit code 3.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import Budget
from .orchestrator.commit_service import CommitService, MissionSpec
from .orchestrator.event_handler import Orchestrator
from .runtime.assembly import OrchestratorConfig, PriceTable
from .storage.store import Store

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3
SCENARIOS = {
    "single-task": 2,
    "static-dag": 3,
    "knowledge-sharing": 4,
    "dynamic-dag": 5,
    "multi-mission": 6,
    "approval-action": 7,
    "evaluate-policies": 8,
    "policy-promotion": 9,
}


def _print(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _config(
    args: argparse.Namespace, *, model: str | None, price: PriceTable | None
) -> OrchestratorConfig:
    return OrchestratorConfig(
        evidence_root=Path(args.evidence_dir).resolve(),
        model=model or "agent-model",
        price_table=price,
        hard_cap_micros=getattr(args, "hard_cap_micros", None),
        max_concurrency=getattr(args, "max_concurrency", 1),
        test_timeout_seconds=getattr(args, "test_timeout", 120.0),
    )


def _provider(args: argparse.Namespace, *, scenario: str = "single-task"):  # type: ignore[no-untyped-def]
    """Return (provider, model, price_table, provider_kind)."""

    if args.provider == "fixtures":
        from .testing.fixtures import demo_single_task_provider, demo_static_dag_provider

        if scenario == "static-dag":
            return demo_static_dag_provider(), "agent-model", None, "fixtures"
        if scenario == "knowledge-sharing":
            from .testing.fixtures import demo_knowledge_sharing_provider

            return demo_knowledge_sharing_provider(), "agent-model", None, "fixtures"
        return demo_single_task_provider(), "agent-model", None, "fixtures"
    if args.provider == "env":
        base_url = os.environ.get("SH_BASEURL")
        api_key = os.environ.get("SH_APIKEY")
        model = os.environ.get("SH_MODEL")
        if not (base_url and api_key and model):
            raise SystemExit("--provider env needs SH_BASEURL, SH_APIKEY and SH_MODEL")
        import httpx

        from simple_harness.providers import OpenAICompatibleProvider, Secret

        provider = OpenAICompatibleProvider(
            httpx.AsyncClient(), base_url, model, Secret(api_key), timeout=180.0
        )
        price = None
        if os.environ.get("SH_PRICE_INPUT_MICROS") and os.environ.get("SH_PRICE_OUTPUT_MICROS"):
            price = PriceTable(
                snapshot_id=f"env-{model}",
                input_micros_per_million_tokens=int(os.environ["SH_PRICE_INPUT_MICROS"]),
                output_micros_per_million_tokens=int(os.environ["SH_PRICE_OUTPUT_MICROS"]),
            )
        elif not getattr(args, "unpriced", False):
            raise SystemExit(
                "--provider env is a paid provider: set SH_PRICE_INPUT_MICROS/SH_PRICE_OUTPUT_MICROS"
                " (micros per million tokens) or pass --unpriced to record costs as unpriced"
            )
        return provider, model, price, "env"
    raise SystemExit(f"unknown provider {args.provider!r}")


def _open_store(args: argparse.Namespace) -> Store:
    return Store.open(Path(args.evidence_dir).resolve() / "orchestrator.db")


# ------------------------------------------------------------------ commands
def cmd_mission(args: argparse.Namespace) -> int:
    if args.action == "create":
        spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        provider, model, price, kind = _provider(args)

        async def run() -> int:
            async with Orchestrator(
                _config(args, model=model, price=price), provider
            ) as orchestrator:
                from .api.missions import MissionApi

                mission, created = MissionApi(orchestrator.commit).create(
                    tenant_id=args.tenant, request=spec_data
                )
                _print(
                    {"mission_id": mission.id, "created": created, "status": str(mission.status)}
                )
                if args.run:
                    await orchestrator.run()
                    final = orchestrator.store.get_mission(mission.id)
                    assert final is not None
                    _print(
                        {
                            "mission_id": final.id,
                            "status": str(final.status),
                            "stop_reason": final.stop_reason,
                        }
                    )
                    return EXIT_OK if str(final.status) == "COMPLETED" else EXIT_FAILED
            return EXIT_OK

        return asyncio.run(run())
    store = _open_store(args)
    try:
        if args.action == "get":
            _print(store.snapshot(args.mission_id))
        elif args.action == "events":
            _print([event.to_json() for event in store.list_events(args.mission_id)])
        elif args.action == "cancel":
            mission = CommitService(store).cancel_mission(args.mission_id)
            _print({"mission_id": mission.id, "status": str(mission.status)})
    finally:
        store.close()
    return EXIT_OK


def cmd_attempt(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        attempt = store.get_attempt(args.attempt_id)
        if attempt is None:
            _print({"error": "unknown attempt"})
            return EXIT_FAILED
        stored = store.find_result_for_attempt(attempt.id)
        _print(
            {
                "attempt": attempt.to_json(),
                "intent": (lambda i: None if i is None else i.to_json())(
                    store.get_intent_for_subject(attempt.id)
                ),
                "result": None
                if stored is None
                else {
                    **stored.to_json(),
                    "verifications": store.list_verifications(stored.envelope.id),
                },
            }
        )
    finally:
        store.close()
    return EXIT_OK


def cmd_artifact(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        artifact = store.get_artifact(args.artifact_id)
        if artifact is None:
            _print({"error": "unknown artifact"})
            return EXIT_FAILED
        record = artifact.to_json()
        path = Path(artifact.storage_uri)
        record["content"] = path.read_text(encoding="utf-8") if path.is_file() else None
        _print(record)
    finally:
        store.close()
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    step = SCENARIOS.get(args.scenario)
    if step is None:
        _print({"error": f"unknown scenario {args.scenario}"})
        return EXIT_USAGE
    if step not in {2, 3, 4}:
        _print({"scenario": args.scenario, "status": "not_implemented", "step": step})
        return EXIT_NOT_IMPLEMENTED
    from .observability.evidence import write_evidence
    from .testing.fixtures import (
        COMPARE_SEED,
        COMPARE_SPEC,
        COMPARE_SYNTHESIS,
        DEMO_DAG_SPEC,
        DEMO_SEED,
        TEXTKIT_SEED,
    )

    provider, model, price, kind = _provider(args, scenario=args.scenario)
    started = time.time()
    if step == 2:
        spec = MissionSpec(
            goal="在隔离工作区实现字符串解析函数 parse_kv，并通过给定测试",
            success_criteria=("pytest:tests/test_parse_kv.py", "实现应处理空字符串"),
            tenant_id=args.tenant,
            idempotency_key=args.idempotency_key,
            allowed_tools=(
                "workspace_read_file",
                "workspace_write_file",
                "workspace_list",
                "run_tests",
            ),
            budget=Budget(max_tokens=400_000, max_attempts=3),
            workspace_seed=DEMO_SEED,
        )
    elif step == 4:
        spec = MissionSpec(
            goal=str(COMPARE_SPEC["goal"]),
            success_criteria=tuple(str(c) for c in COMPARE_SPEC["success_criteria"]),
            tenant_id=args.tenant,
            idempotency_key=args.idempotency_key,
            allowed_tools=tuple(str(t) for t in COMPARE_SPEC["allowed_tools"]),
            budget=Budget(max_tokens=1_200_000 if kind == "env" else 400_000, max_attempts=16),
            workspace_seed=COMPARE_SEED,
            untrusted_sources=tuple(str(p) for p in COMPARE_SPEC["untrusted_sources"]),
            synthesis={
                **COMPARE_SYNTHESIS,
                "budget": {"max_tokens": 200_000 if kind == "env" else 30_000, "max_attempts": 2},
            },
            conflict_reserve_tokens=200_000 if kind == "env" else 20_000,
        )
    else:
        spec = MissionSpec(
            goal=str(DEMO_DAG_SPEC["goal"]),
            success_criteria=tuple(str(c) for c in DEMO_DAG_SPEC["success_criteria"]),
            tenant_id=args.tenant,
            idempotency_key=args.idempotency_key,
            allowed_tools=tuple(str(t) for t in DEMO_DAG_SPEC["allowed_tools"]),
            budget=Budget(max_tokens=400_000, max_attempts=12),
            workspace_seed=TEXTKIT_SEED,
        )

    async def run() -> int:
        config = _config(args, model=model, price=price)
        async with Orchestrator(config, provider) as orchestrator:
            baseline = {
                "agent_orchestrator": __version__,
                "provider_kind": kind,
                "model": model,
                "config": config.to_json(),
                "spec": spec.to_json(),
                "started_at": started,
            }
            mission = await orchestrator.submit_mission(spec)
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            assert final is not None
            report = {
                "mission_id": mission.id,
                "status": str(final.status),
                "stop_reason": final.stop_reason,
                "elapsed_seconds": round(time.time() - started, 2),
                "progress": orchestrator.progress_log,
                "provider_calls": getattr(provider, "by_role", None),
                "tasks": [
                    {
                        "task_id": task.id,
                        "kind": task.kind,
                        "status": str(task.status),
                        "dependencies": list(task.dependency_ids),
                        "attempts": task.attempt_count,
                    }
                    for task in orchestrator.store.list_tasks(mission.id)
                ],
                "knowledge": [
                    {"id": k.id, "status": k.status, "key": k.key, "used_by": list(k.used_by)}
                    for k in orchestrator.store.list_knowledge(mission.id)
                ],
                "conflicts": [
                    {"conflict_id": c["conflict_id"], "key": c["key"], "state": c["state"]}
                    for c in orchestrator.store.list_conflicts(mission.id)
                ],
                "lineage": {
                    "knowledge": [
                        k["id"]
                        for k in (final.final_report or {}).get("lineage", {}).get("knowledge", [])
                    ],
                    "agents": (final.final_report or {}).get("lineage", {}).get("agents", []),
                },
            }
            evidence = write_evidence(
                directory=Path(args.evidence_dir).resolve(),
                store=orchestrator.store,
                commit=orchestrator.commit,
                mission_id=mission.id,
                baseline=baseline,
                workspaces_root=config.workspaces_root,
                test_report=report,
            )
            _print({**report, "evidence_files": evidence["files"]})
            return EXIT_OK if str(final.status) == "COMPLETED" else EXIT_FAILED

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_orchestrator", description=__doc__)
    parser.add_argument("--version", action="version", version=f"agent_orchestrator {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, provider: bool) -> None:
        p.add_argument("--evidence-dir", required=True)
        if provider:
            p.add_argument("--provider", default="fixtures", choices=("fixtures", "env"))
            p.add_argument("--tenant", default="local")
            p.add_argument("--hard-cap-micros", type=int, default=None, dest="hard_cap_micros")
            p.add_argument("--max-concurrency", type=int, default=1, dest="max_concurrency")
            p.add_argument("--test-timeout", type=float, default=120.0, dest="test_timeout")
            p.add_argument(
                "--unpriced",
                action="store_true",
                help="allow a paid provider without a price table (costs recorded as unpriced)",
            )

    mission = sub.add_parser("mission")
    mission_sub = mission.add_subparsers(dest="action", required=True)
    create = mission_sub.add_parser("create")
    common(create, provider=True)
    create.add_argument(
        "--spec", required=True, help="JSON file with goal/success_criteria/idempotency_key/..."
    )
    create.add_argument(
        "--run", action="store_true", help="run the orchestrator until idle after creating"
    )
    for action in ("get", "events", "cancel"):
        p = mission_sub.add_parser(action)
        common(p, provider=False)
        p.add_argument("mission_id")

    attempt = sub.add_parser("attempt")
    attempt_sub = attempt.add_subparsers(dest="action", required=True)
    get_attempt = attempt_sub.add_parser("get")
    common(get_attempt, provider=False)
    get_attempt.add_argument("attempt_id")

    artifact = sub.add_parser("artifact")
    artifact_sub = artifact.add_subparsers(dest="action", required=True)
    show = artifact_sub.add_parser("show")
    common(show, provider=False)
    show.add_argument("artifact_id")

    demo = sub.add_parser("demo")
    common(demo, provider=True)
    demo.add_argument("--scenario", required=True)
    demo.add_argument(
        "--idempotency-key", default=f"demo-{int(time.time())}", dest="idempotency_key"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mission":
        return cmd_mission(args)
    if args.command == "attempt":
        return cmd_attempt(args)
    if args.command == "artifact":
        return cmd_artifact(args)
    if args.command == "demo":
        return cmd_demo(args)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
