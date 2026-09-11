# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""``python -m agent_orchestrator`` — the operator / demo CLI (ORCH-BUILD §14.3).

Subcommands (step 2):

    mission create --tenant T --evidence-dir DIR --spec spec.json [--provider ...]
    mission get|cancel|events --evidence-dir DIR MISSION_ID
    attempt get --evidence-dir DIR ATTEMPT_ID
    artifact show --evidence-dir DIR ARTIFACT_ID
    demo --scenario single-task|static-dag|knowledge-sharing|dynamic-dag|multi-mission --provider fixtures|env --evidence-dir DIR

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
        if scenario == "dynamic-dag":
            from .testing.fixtures import demo_dynamic_dag_provider

            return demo_dynamic_dag_provider(), "agent-model", None, "fixtures"
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
    if step not in {2, 3, 4, 5, 6}:
        _print({"scenario": args.scenario, "status": "not_implemented", "step": step})
        return EXIT_NOT_IMPLEMENTED
    if step == 6:
        return _demo_multi_mission(args)
    from .observability.evidence import write_evidence
    from .testing.fixtures import (
        COMPARE_SEED,
        COMPARE_SPEC,
        COMPARE_SYNTHESIS,
        DEMO_DAG_SPEC,
        DEMO_SEED,
        RECORDER_SEED,
        RECORDER_SPEC,
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
    elif step == 5:
        spec = MissionSpec(
            goal=str(RECORDER_SPEC["goal"]),
            success_criteria=tuple(str(c) for c in RECORDER_SPEC["success_criteria"]),
            tenant_id=args.tenant,
            idempotency_key=args.idempotency_key,
            allowed_tools=tuple(str(t) for t in RECORDER_SPEC["allowed_tools"]),
            budget=Budget(max_tokens=1_200_000 if kind == "env" else 300_000, max_attempts=16),
            workspace_seed=RECORDER_SEED,
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
                "graph_version": (final.final_report or {}).get("graph_version"),
                "graph_changes": [
                    {
                        "from": c["from_version"],
                        "to": c["to_version"],
                        "basis": c.get("basis"),
                        "new_tasks": c.get("new_task_ids"),
                        "superseded": c.get("superseded"),
                    }
                    for c in orchestrator.store.list_graph_changes(mission.id)
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


def _multi_mission_profiles(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    """Two execution pools for the step-6 demo: fixtures ``small``/``large``, or — with
    ``--provider env`` — two profiles of the model in ``SH_MODEL`` (the operator's choice;
    this program's real runs use deepseek-flash for both), differing in pool and output
    caps; the physical route of each Attempt is proven by its pool's echo."""

    from .runtime.model_router import RoutingRules, RuntimeProfile

    if args.provider == "fixtures":
        from .testing.fixtures import demo_multi_mission_profiles

        profiles, rules = demo_multi_mission_profiles()
        return profiles, rules, "fixtures", None
    base_url = os.environ.get("SH_BASEURL")
    api_key = os.environ.get("SH_APIKEY")
    model = os.environ.get("SH_MODEL")
    if not (base_url and api_key and model):
        raise SystemExit("--provider env needs SH_BASEURL, SH_APIKEY and SH_MODEL")
    import httpx

    from simple_harness.providers import OpenAICompatibleProvider, Secret

    price = None
    if os.environ.get("SH_PRICE_INPUT_MICROS") and os.environ.get("SH_PRICE_OUTPUT_MICROS"):
        price = PriceTable(
            snapshot_id=f"env-{model}",
            input_micros_per_million_tokens=int(os.environ["SH_PRICE_INPUT_MICROS"]),
            output_micros_per_million_tokens=int(os.environ["SH_PRICE_OUTPUT_MICROS"]),
        )
    elif not getattr(args, "unpriced", False):
        raise SystemExit("--provider env is a paid provider: set SH_PRICE_* or pass --unpriced")

    def provider():  # type: ignore[no-untyped-def]
        return OpenAICompatibleProvider(
            httpx.AsyncClient(), base_url, model, Secret(api_key), timeout=300.0
        )

    profiles = {
        "small": RuntimeProfile(
            "small",
            provider(),
            model,
            tier=1,
            price_table=price,
            default_max_output_tokens=8192,
            max_output_tokens_ceiling=16384,
            provider_kind="env",
        ),
        "large": RuntimeProfile(
            "large",
            provider(),
            model,
            tier=2,
            price_table=price,
            default_max_output_tokens=8192,
            max_output_tokens_ceiling=32768,
            provider_kind="env",
        ),
    }
    rules = RoutingRules(
        default="small",
        by_role={"planner": "large", "manager": "large", "critic": "large"},
        escalate={"small": "large"},
        fallback={"small": "large"},
    )
    return profiles, rules, "env", price


def _demo_multi_mission(args: argparse.Namespace) -> int:
    """Step 6 (ORCH §8.4): two Missions at once under a Global Budget, two execution
    pools with routing and escalation, a bounded verification queue with backpressure;
    evidence per Mission under ``missions/<id>/`` plus ``multi-mission.json``."""

    from .observability.evidence import write_evidence
    from .orchestrator.commit_service import GLOBAL_ACCOUNT
    from .testing.fixtures import DEMO_SEED, RECORDER_SEED, RECORDER_SPEC

    profiles, rules, kind, _price = _multi_mission_profiles(args)
    real = kind == "env"
    started = time.time()
    tools = tuple(str(t) for t in RECORDER_SPEC["allowed_tools"])
    per_mission = Budget(max_tokens=1_200_000 if real else 300_000, max_attempts=16)
    if real:
        specs = [
            MissionSpec(
                goal="阅读 spec/INPUT.md 与 tests/test_recorder.py，写出输入分析 analysis.md，再写一份文档检查 DOCS.md（核对分析与测试是否一致）。tests/ 下文件不可修改。",
                success_criteria=("file:analysis.md", "file:DOCS.md"),
                tenant_id=args.tenant,
                idempotency_key=f"{args.idempotency_key}-recorder",
                allowed_tools=tools,
                budget=per_mission,
                workspace_seed=RECORDER_SEED,
            ),
            MissionSpec(
                goal="在隔离工作区实现字符串解析函数 parse_kv，并通过 tests/test_parse_kv.py；tests/ 下文件不可修改。",
                success_criteria=("pytest:tests/test_parse_kv.py",),
                tenant_id=args.tenant,
                idempotency_key=f"{args.idempotency_key}-parse-kv",
                allowed_tools=tools,
                budget=per_mission,
                workspace_seed=DEMO_SEED,
            ),
        ]
    else:
        specs = [
            MissionSpec(
                goal=str(RECORDER_SPEC["goal"]),
                success_criteria=("file:DOCS.md",),
                tenant_id=args.tenant,
                idempotency_key=f"{args.idempotency_key}-{n}",
                allowed_tools=tools,
                budget=per_mission,
                workspace_seed=RECORDER_SEED,
            )
            for n in (1, 2)
        ]
    real_knobs: dict[str, Any] = (  # flash spends its cap on reasoning (step 5 run 2)
        {
            "max_concurrent_model_calls": 4,
            "default_max_output_tokens": 8192,
            "max_output_tokens_ceiling": 32768,
            "attempt_reserve_tokens": 120_000,
            "critic_reserve_tokens": 30_000,
            "manager_reserve_tokens": 30_000,
            "planner_reserve_tokens": 30_000,
            "lease_seconds": 120.0,
            "stall_seconds": 300.0,
            "turn_deadline_seconds": 900.0,
        }
        if real
        else {}
    )
    config = OrchestratorConfig(
        evidence_root=Path(args.evidence_dir).resolve(),
        model=profiles["small"].model,
        max_concurrency=max(2, getattr(args, "max_concurrency", 1)),
        max_running_attempts=3,
        verifier_workers=1,
        max_pending_verifications=2,
        global_budget=Budget(max_tokens=int(per_mission.max_tokens or 0) * 3, max_attempts=48),
        test_timeout_seconds=getattr(args, "test_timeout", 120.0),
        hard_cap_micros=getattr(args, "hard_cap_micros", None),
        **real_knobs,
    )

    async def run() -> int:
        async with Orchestrator(config, profiles=profiles, routing=rules) as orchestrator:
            missions = [await orchestrator.submit_mission(spec) for spec in specs]
            await orchestrator.run()
            store = orchestrator.store
            root = Path(args.evidence_dir).resolve()
            reports = []
            for mission, spec in zip(missions, specs, strict=True):
                final = store.get_mission(mission.id)
                assert final is not None
                echoes = orchestrator.echoed_models_for(mission.id)
                attempts = [
                    {
                        "attempt_id": a.id,
                        "task_id": a.task_id,
                        "status": str(a.status),
                        "runtime_profile_id": a.runtime_profile_id,
                        "requested_model": a.model,
                        "echoed_models": echoes.get(a.id),
                        "retry_of": a.retry_of,
                        "failure": None if a.failure is None else a.failure.get("reason"),
                    }
                    for t in store.list_tasks(mission.id)
                    for a in store.list_attempts(t.id)
                ]
                report = {
                    "mission_id": mission.id,
                    "status": str(final.status),
                    "stop_reason": final.stop_reason,
                    "tasks": [
                        {"task_id": t.id, "status": str(t.status), "goal": t.goal}
                        for t in store.list_tasks(mission.id)
                    ],
                    "attempts": attempts,
                    "services": [
                        {
                            "kind": i.kind,
                            "subject_id": i.subject_id,
                            "runtime_profile_id": i.config.get("runtime_profile_id"),
                            "model": i.config.get("model"),
                        }
                        for i in store.list_intents("SETTLED", "FAILED")
                        if i.mission_id == mission.id and i.kind != "attempt"
                    ],
                }
                evidence = write_evidence(
                    directory=root / "missions" / mission.id,
                    store=store,
                    commit=orchestrator.commit,
                    mission_id=mission.id,
                    baseline={
                        "agent_orchestrator": __version__,
                        "provider_kind": kind,
                        "profiles": {k: p.to_json() for k, p in profiles.items()},
                        "routing": rules.to_json(),
                        "config": config.to_json(),
                        "spec": spec.to_json(),
                        "started_at": started,
                    },
                    workspaces_root=config.workspaces_root,
                    test_report=report,
                    echoes=echoes,
                    unpriced=all(p.unpriced for p in profiles.values()),
                )
                reports.append({**report, "evidence_files": evidence["files"]})
            with store.transaction():
                global_account = orchestrator.commit.ledger.account(GLOBAL_ACCOUNT).to_json()
            summary = {
                "scenario": "multi-mission",
                "provider_kind": kind,
                "elapsed_seconds": round(time.time() - started, 2),
                "profiles": {k: p.to_json() for k, p in profiles.items()},
                "routing": rules.to_json(),
                "missions": reports,
                "global_account": global_account,
                "backpressure": store.get_scheduler_state("backpressure"),
                "profile_health": store.get_scheduler_state("profile_health"),
                "progress": orchestrator.progress_log,
            }
            from .observability.secrets import guard_text

            text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            guard_text(text, where="multi-mission.json")
            (root / "multi-mission.json").write_text(text, encoding="utf-8")
            _print({k: v for k, v in summary.items() if k != "progress"})
            ok = all(r["status"] == "COMPLETED" for r in reports)
            return EXIT_OK if ok else EXIT_FAILED

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
