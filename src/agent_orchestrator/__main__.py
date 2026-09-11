# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""``python -m agent_orchestrator`` — the operator / demo CLI (ORCH-BUILD §14.3).

Subcommands (step 2):

    mission create --tenant T --evidence-dir DIR --spec spec.json [--provider ...]
    mission get|cancel|events --evidence-dir DIR MISSION_ID
    attempt get --evidence-dir DIR ATTEMPT_ID
    artifact show --evidence-dir DIR ARTIFACT_ID
    demo --scenario single-task|static-dag|knowledge-sharing|dynamic-dag|multi-mission|approval-action --provider fixtures|env --evidence-dir DIR
    approval list|approve|reject|revoke|comment|review|arbitrate|takeover|resolve --evidence-dir DIR --as PRINCIPAL ...
    replay --evidence-dir DIR MISSION_ID [--events FILE] [--failures] [--attribution] [--out FILE]
    evaluate --plan plan.json --evidence-dir NEW_DIR [--provider fixtures|env]
    demo --scenario evaluate-policies --provider fixtures|env --evidence-dir NEW_DIR

``replay`` (step 8) rebuilds a Mission's formal state from its events on a read-only copy
of the library and compares it with the library; it never executes or writes.
``evaluate`` runs a plan — named cases × strategies × trials — every run a new Mission in
its own new directory, and writes ``evaluation.json`` / ``evaluation.md``.

``approval`` (step 7) acts as the caller named by ``--as`` — in this local build a
self-declared identity; a real deployment binds it to its authentication.  ``demo
--scenario approval-action --pause-for-approval`` stops while the Mission waits for a
person (exit code 4); run the same demo again with the same ``--idempotency-key`` to
continue after ``approval approve``.

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
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import Budget
from .governance.policies import DeploymentPolicy
from .orchestrator.commit_service import CommitService, MissionSpec
from .orchestrator.event_handler import Orchestrator
from .runtime.assembly import OrchestratorConfig, PriceTable
from .runtime.model_router import RoutingRules
from .storage.store import Store

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_WAITING = 4  # step 7: the Mission waits for a person
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
    if step not in {2, 3, 4, 5, 6, 7, 8, 9}:
        _print({"scenario": args.scenario, "status": "not_implemented", "step": step})
        return EXIT_NOT_IMPLEMENTED
    if step == 6:
        return _demo_multi_mission(args)
    if step == 7:
        return _demo_approval_action(args)
    if step == 8:
        return _demo_evaluate_policies(args)
    if step == 9:
        return _demo_policy_promotion(args)
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
                "policy_snapshot": orchestrator.policy_snapshot(),
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
                policy_snapshot=orchestrator.policy_snapshot(),
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


async def _own_pool_only(orchestrator, attempt) -> bool | None:  # type: ignore[no-untyped-def]
    intent = orchestrator.store.get_intent_for_subject(attempt.id)
    if intent is None or intent.agent_id is None or intent.expected_turn_id is None:
        return None
    for profile_id, pool in orchestrator.assembled.pools.items():
        if profile_id == attempt.runtime_profile_id:
            continue
        view = await pool.bridge.liveness(agent_id=intent.agent_id, turn_id=intent.expected_turn_id)
        if view.exists:
            return False
    return True


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
            start_snapshot = orchestrator.policy_snapshot()  # review P2-2: before anything runs
            missions = [await orchestrator.submit_mission(spec) for spec in specs]
            await orchestrator.run()
            store = orchestrator.store
            root = Path(args.evidence_dir).resolve()
            reports = []
            for mission, spec in zip(missions, specs, strict=True):
                final = store.get_mission(mission.id)
                assert final is not None
                echoes = orchestrator.echoed_models_for(mission.id)
                attempts = []
                for t in store.list_tasks(mission.id):
                    for a in store.list_attempts(t.id):
                        attempts.append(
                            {
                                "attempt_id": a.id,
                                "task_id": a.task_id,
                                "status": str(a.status),
                                "runtime_profile_id": a.runtime_profile_id,
                                "requested_model": a.model,
                                "echoed_models": echoes.get(a.id),
                                "retry_of": a.retry_of,
                                "failure": None if a.failure is None else a.failure.get("reason"),
                                # review P1-7: with one model name on both pools, the physical route is
                                # proven by the Agent living only in its own pool's execution library
                                "only_in_own_pool": await _own_pool_only(orchestrator, a),
                            }
                        )
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
                        "policy_snapshot": start_snapshot,
                        "config": config.to_json(),
                        "spec": spec.to_json(),
                        "started_at": started,
                    },
                    workspaces_root=config.workspaces_root,
                    test_report=report,
                    policy_snapshot=orchestrator.policy_snapshot(),
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
            from .observability.secrets import redact_text

            text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            text, _found = redact_text(text)
            (root / "multi-mission.json").write_text(text, encoding="utf-8")
            _print({k: v for k, v in summary.items() if k != "progress"})
            ok = all(r["status"] == "COMPLETED" for r in reports)
            return EXIT_OK if ok else EXIT_FAILED

    return asyncio.run(run())


REAL_KNOBS: dict[str, Any] = {  # flash spends its output cap on reasoning (step 5 run 2)
    "max_concurrent_model_calls": 2,
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


def _approval_chain(store: Store, mission_id: str, service: Any) -> dict[str, Any]:
    """Candidate → approval → hand-off → service receipt, one line per action version."""

    state = service.state()
    return {
        "actions": [
            {
                "action_key": a["action_key"],
                "version": a["version"],
                "state": a["state"],
                "level": a.get("level"),
                "connector": a["connector"],
                "operation": a["operation"],
                "target": a["target"],
                "params_hash": a["params_hash"],
                "candidate_artifact_id": a.get("artifact_id"),
                "artifact_hash": a["artifact_hash"],
                "approval_request_id": a.get("approval_request_id"),
                "decision_receipts": list(a.get("decision_receipts") or []),
                "idempotency_key": a.get("idempotency_key"),
                "handoffs": a.get("handoffs", 0),
                "receipt_hash": (a.get("receipt") or {}).get("receipt_hash"),
                "service_ref": (a.get("receipt") or {}).get("service_ref"),
            }
            for a in store.list_actions(mission_id)
        ],
        "service": {
            "kind": "test service (not production)",
            "config": state.get("config", {}),
            "applied_count": state.get("applied_count", 0),
        },
    }


def _demo_approval_action(args: argparse.Namespace) -> int:
    """Step 7 (ORCH §9.1): a Worker writes an action candidate against the dedicated test
    configuration service; the system asks for approval and ``run()`` goes idle; the demo
    operator (``--as``) approves; the executor hands the approved version off as the last
    step of the Mission judgment and checks the service's receipt.  Passing against the
    test service grants nothing for production."""

    from .api.approvals import ApprovalApi
    from .contracts import MissionStatus
    from .governance.permissions import Principal
    from .governance.policies import DeploymentPolicy
    from .observability.evidence import write_evidence
    from .runtime.connectors import TestConfigService
    from .testing.fixtures import APPROVAL_SEED, APPROVAL_SPEC

    if args.provider == "fixtures":
        from .testing.fixtures import demo_approval_action_provider

        provider, model, price, kind = (
            demo_approval_action_provider(),
            "agent-model",
            None,
            "fixtures",
        )
    else:
        provider, model, price, kind = _provider(args, scenario="approval-action")
    real = kind == "env"
    root = Path(args.evidence_dir).resolve()
    service = TestConfigService(root / "test-services" / "config.json")
    deployment = DeploymentPolicy(enabled_connectors=("test_config",))
    config = replace(
        _config(args, model=model, price=price),
        deployment_policy=deployment,
        **(REAL_KNOBS if real else {}),
    )
    spec = MissionSpec(
        goal=str(APPROVAL_SPEC["goal"]),
        success_criteria=tuple(str(c) for c in APPROVAL_SPEC["success_criteria"]),
        tenant_id=args.tenant,
        idempotency_key=args.idempotency_key,
        allowed_tools=tuple(str(t) for t in APPROVAL_SPEC["allowed_tools"]),
        budget=Budget(max_tokens=800_000 if real else 200_000, max_attempts=8),
        workspace_seed=APPROVAL_SEED,
    )
    started = time.time()

    async def run() -> int:
        async with Orchestrator(
            config, provider, connectors={"test_config": service}
        ) as orchestrator:
            start_snapshot = orchestrator.policy_snapshot()  # review P2-2: before anything runs
            mission = await orchestrator.submit_mission(spec)  # idempotent: a rerun continues it
            await orchestrator.run()
            store = orchestrator.store
            pending = [
                r for r in store.list_approvals(mission.id, "PENDING") if r["kind"] == "action"
            ]
            approved_here = []
            if pending and not args.pause_for_approval:
                operator = ApprovalApi(
                    orchestrator.commit,
                    Principal(args.as_principal, args.as_principal),
                    deployment=deployment,
                )
                approved_here = [operator.approve(r["request_id"])["receipt_hash"] for r in pending]
                await orchestrator.run()
            final = store.get_mission(mission.id)
            assert final is not None
            waiting = store.waiting_on(mission.id)
            report = {
                "scenario": "approval-action",
                "mission_id": mission.id,
                "status": str(final.status),
                "stop_reason": final.stop_reason,
                "waiting_on": waiting,
                "operator": args.as_principal,
                "approved_in_this_run": approved_here,
                "chain": _approval_chain(store, mission.id, service),
                "elapsed_seconds": round(time.time() - started, 2),
                "provider_calls": getattr(provider, "by_role", None),
                "progress": orchestrator.progress_log,
            }
            evidence = write_evidence(
                directory=root,
                store=store,
                commit=orchestrator.commit,
                mission_id=mission.id,
                baseline={
                    "agent_orchestrator": __version__,
                    "provider_kind": kind,
                    "model": model,
                    "policy_snapshot": start_snapshot,
                    "config": config.to_json(),
                    "spec": spec.to_json(),
                    "connectors": {
                        "test_config": {
                            "kind": "test service (not production)",
                            "state_file": "test-services/config.json",
                            "operations": {n: o.to_json() for n, o in service.operations.items()},
                        }
                    },
                    "started_at": started,
                },
                workspaces_root=config.workspaces_root,
                test_report=report,
                policy_snapshot=orchestrator.policy_snapshot(),
            )
            _print(
                {
                    **{k: v for k, v in report.items() if k != "progress"},
                    "evidence_files": evidence["files"],
                }
            )
            if str(final.status) == "COMPLETED":
                return EXIT_OK
            if final.status is MissionStatus.ACTIVE and waiting:
                return EXIT_WAITING
            return EXIT_FAILED

    return asyncio.run(run())


ORACLE_PAIRS = (
    "from parse_kv import parse_kv\n\n\ndef test_pairs():\n"
    "    assert parse_kv('x=1;y=2') == {'x': '1', 'y': '2'}\n"
)
ORACLE_SPACES = (
    "from parse_kv import parse_kv\n\n\ndef test_spaces():\n"
    "    assert parse_kv(' a = 1 ') == {'a': '1'}\n"
)


def _evaluation_cases(args: argparse.Namespace, names: list[str]) -> tuple[Any, ...]:
    """The named cases an evaluation plan may use (fixtures, or the real provider for
    ``parse-kv``); every trial gets a fresh provider (plan D8-6')."""

    from .observability.evaluation import EvaluationCase, Oracle
    from .testing.fixtures import (
        DEMO_BAD,
        DEMO_DAG_SPEC,
        DEMO_GOOD,
        DEMO_PROPOSAL,
        DEMO_SEED,
        TEXTKIT_SEED,
        RoleScriptedProvider,
        critic_step,
        demo_static_dag_provider,
        demo_worker_script,
        proposal_step,
    )

    real = args.provider == "env"
    tools = ("workspace_read_file", "workspace_write_file", "workspace_list", "run_tests")

    def parse_kv(tenant: str, key: str) -> MissionSpec:
        return MissionSpec(
            goal="在隔离工作区实现字符串解析函数 parse_kv(text) -> dict（按 ; 分隔、= 分键值），并通过 tests/test_parse_kv.py；tests/ 下文件不可修改。",
            success_criteria=("pytest:tests/test_parse_kv.py",),
            tenant_id=tenant,
            idempotency_key=key,
            allowed_tools=tools,
            budget=Budget(max_tokens=600_000 if real else 200_000, max_attempts=4),
            workspace_seed=DEMO_SEED,
        )

    def textkit(tenant: str, key: str) -> MissionSpec:
        return MissionSpec(
            goal=str(DEMO_DAG_SPEC["goal"]),
            success_criteria=tuple(str(c) for c in DEMO_DAG_SPEC["success_criteria"]),
            tenant_id=tenant,
            idempotency_key=key,
            allowed_tools=tuple(str(t) for t in DEMO_DAG_SPEC["allowed_tools"]),
            budget=Budget(max_tokens=400_000, max_attempts=12),
            workspace_seed=TEXTKIT_SEED,
        )

    def scripted(code: str, attempts: int = 1) -> Any:
        def make() -> RoleScriptedProvider:
            return RoleScriptedProvider(
                {
                    "planner": [proposal_step(DEMO_PROPOSAL)],
                    "worker": demo_worker_script(code) * attempts,
                    "critic": [critic_step(verdict="PASS", criteria_met=True)] * attempts,
                }
            )

        return make

    def env() -> Any:
        return _provider(args, scenario="evaluate-policies")[0]

    pairs = Oracle({"hidden/test_oracle.py": ORACLE_PAIRS}, "hidden/test_oracle.py")
    spaces = Oracle({"hidden/test_oracle.py": ORACLE_SPACES}, "hidden/test_oracle.py")
    catalog = {
        "parse-kv": EvaluationCase(
            "parse-kv",
            parse_kv,
            env if real else scripted(DEMO_GOOD),
            kind="env" if real else "fixtures",
            oracle=pairs,
        ),
    }
    if not real:
        catalog.update(
            {
                "parse-kv-strict": EvaluationCase(
                    "parse-kv-strict", parse_kv, scripted(DEMO_GOOD), oracle=spaces
                ),
                "parse-kv-bad": EvaluationCase(
                    "parse-kv-bad", parse_kv, scripted(DEMO_BAD, attempts=2)
                ),
                "textkit": EvaluationCase("textkit", textkit, demo_static_dag_provider),
            }
        )
    unknown = [n for n in names if n not in catalog]
    if unknown:
        raise ValueError(  # review P2-5: a usage error, exit 2
            f"unknown evaluation cases for --provider {args.provider}: {unknown}; known: {sorted(catalog)}"
        )
    return tuple(catalog[n] for n in names)


def _evaluation_config(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    if args.provider != "env":
        return {"test_timeout_seconds": getattr(args, "test_timeout", 120.0), **config}
    _provider_object, model, price, _kind = _provider(args, scenario="evaluate-policies")
    knobs = {k: v for k, v in REAL_KNOBS.items()}
    return {
        **knobs,
        "model": model,
        "price_table": price,
        "test_timeout_seconds": getattr(args, "test_timeout", 120.0),
        **config,
    }


def cmd_evaluate(args: argparse.Namespace) -> int:
    """``evaluate --plan plan.json`` (plan D8-9'): cases × strategies × trials, every run a
    new Mission in its own directory; the report lands in ``--evidence-dir``.  Exit 0 when
    every run was counted, 1 when any run was a harness error, 2 for a bad or refused plan."""

    from .observability.evaluation import EvaluationPlan, Strategy, run_plan

    try:  # review P2-5: a bad plan is an answer (exit 2), never a traceback
        data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        config = dict(data.get("config", {}))
        if isinstance(config.get("global_budget"), dict):
            config["global_budget"] = Budget.from_json(config["global_budget"])
        plan = EvaluationPlan(
            name=str(data["name"]),
            cases=_evaluation_cases(args, [str(n) for n in data["cases"]]),
            strategies=tuple(
                Strategy(str(s["name"]), dict(s.get("overrides", {}))) for s in data["strategies"]
            ),
            trials=int(data.get("trials", 1)),
            config=_evaluation_config(args, config),
            timeout_seconds=float(data.get("timeout_seconds", 300.0)),
            min_samples=int(data.get("min_samples", 3)),
        )
        report = run_plan(plan, Path(args.evidence_dir).resolve())
    except (OSError, KeyError, TypeError, ValueError) as error:
        _print({"error": str(error)})
        return EXIT_USAGE
    _print(
        {"plan": report["plan"], "summary": report["summary"], "comparisons": report["comparisons"]}
    )
    errors = sum(v["harness_errors"] for v in report["summary"].values())
    return EXIT_OK if errors == 0 else EXIT_FAILED  # a broken harness is not a clean evaluation


def _demo_evaluate_policies(args: argparse.Namespace) -> int:
    """Step 8 (ORCH §10): the same cases under two strategies (the full policy and one
    without the Critic), two independent trials each, every run a new Mission; plus the
    attribution of one completed run and the replay of one failed run."""

    import tempfile

    from .observability.evaluation import EvaluationPlan, Strategy, run_plan
    from .observability.replay import library_copy, replay_mission
    from .observability.secrets import redact_text
    from .observability.traces import attribution

    real = args.provider == "env"
    names = ["parse-kv"] if real else ["parse-kv", "parse-kv-strict", "parse-kv-bad", "textkit"]
    plan = EvaluationPlan(
        name="evaluate-policies",
        cases=_evaluation_cases(args, names),
        strategies=(Strategy("full-policy"), Strategy("no-critic", {"ablations": ("critic",)})),
        trials=2,
        config=_evaluation_config(args, {}),
        timeout_seconds=1800.0 if real else 300.0,
    )
    root = Path(args.evidence_dir).resolve()
    try:
        report = run_plan(plan, root)
    except ValueError as error:
        _print({"error": str(error)})
        return EXIT_USAGE
    samples: dict[str, Any] = {"attribution": None, "replay": None}
    success = next((r for r in report["runs"] if r["category"] == "success"), None)
    failure = next((r for r in report["runs"] if r["category"] == "failure"), None)
    if success is not None:
        with tempfile.TemporaryDirectory() as scratch:
            store = Store.open_readonly(
                library_copy(Path(success["run_dir"]) / "orchestrator.db", Path(scratch))
            )
            try:
                samples["attribution"] = {
                    "run": success["run_dir"],
                    **attribution(store, success["mission_id"]),
                }
            finally:
                store.close()
    if failure is not None:
        samples["replay"] = {
            "run": failure["run_dir"],
            **replay_mission(
                mission_id=failure["mission_id"],
                library=Path(failure["run_dir"]) / "orchestrator.db",
                failures=True,
            ),
        }
    text, _found = redact_text(
        json.dumps(samples, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    )
    (root / "samples.json").write_text(text, encoding="utf-8")
    _print(
        {
            "scenario": "evaluate-policies",
            "note": report["note"],
            "runs": len(report["runs"]),
            "summary": {
                k: {f: v[f] for f in ("samples", "successes", "success_rate", "harness_errors")}
                for k, v in report["summary"].items()
            },
            "comparisons": [c["verdict"] for c in report["comparisons"]],
            "files": ["evaluation.json", "evaluation.md", "samples.json"],
        }
    )
    errors = sum(v["harness_errors"] for v in report["summary"].values())
    return EXIT_OK if errors == 0 else EXIT_FAILED


def cmd_replay(args: argparse.Namespace) -> int:
    """``replay`` (plan D8-9'): read-only; a mismatch, a gap or coverage below 100 % exits
    1; a missing library, events file or Mission exits 2."""

    import tempfile

    from .observability.replay import library_copy, replay_mission
    from .observability.secrets import redact_text
    from .observability.traces import attribution
    from .storage.store import StoreError

    library = Path(args.evidence_dir).resolve() / "orchestrator.db"
    events = Path(args.events).resolve() if args.events else None
    problem = None
    if events is not None and not events.is_file():
        problem = f"no events file at {events}"
    elif events is None and not library.is_file():
        problem = f"no library at {library} and no --events file"
    elif args.attribution and not library.is_file():
        problem = f"--attribution reads the library and there is none at {library}"
    if problem is not None:  # review P2-5: an answer, never a traceback or a silent gap
        _print({"error": problem})
        return EXIT_USAGE
    try:
        report = replay_mission(
            mission_id=args.mission_id,
            library=library if library.is_file() else None,
            events_file=events,
            failures=args.failures,
        )
    except (StoreError, ValueError) as error:
        _print({"error": str(error)})
        return EXIT_USAGE
    if args.attribution:
        with tempfile.TemporaryDirectory() as scratch:
            store = Store.open_readonly(library_copy(library, Path(scratch)))
            try:
                report["attribution"] = attribution(store, args.mission_id)
            finally:
                store.close()
    text, _found = redact_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    comparison = report.get("comparison") or {}
    complete = (
        comparison.get("consistent", True)
        and comparison.get("coverage", 1.0) == 1.0
        and not report["gaps"]
    )
    return EXIT_OK if complete else EXIT_FAILED


def cmd_approval(args: argparse.Namespace) -> int:
    """``approval ...`` (plan D7-10'): the caller named by ``--as`` decides; the local
    build takes that name as given — a real deployment binds it to authentication."""

    from .api.approvals import ApprovalApi, ApprovalRequestError
    from .contracts import ContractError
    from .governance.permissions import Principal
    from .orchestrator.action_commits import ActionCommitError
    from .orchestrator.commit_service import CommitRejected
    from .storage.store import StoreError

    store = _open_store(args)
    try:
        api = ApprovalApi(CommitService(store), Principal(args.as_principal, args.as_principal))
        try:
            value: Any
            if args.action == "list":
                value = api.list(args.mission_id, state=None if args.all else "PENDING")
            elif args.action == "approve":
                value = api.approve(args.request_id, nonce=args.nonce)
            elif args.action == "reject":
                value = api.reject(args.request_id, reason=args.reason, nonce=args.nonce)
            elif args.action == "revoke":
                value = api.revoke(args.request_id, reason=args.reason)
            elif args.action == "comment":
                value = api.comment(args.target_id, args.text)
            elif args.action == "review":
                value = api.review(
                    args.request_id, verdict=args.verdict, note=args.note, nonce=args.nonce
                )
            elif args.action == "arbitrate":
                value = api.arbitrate(
                    args.request_id, ruling=args.ruling, basis=args.basis, nonce=args.nonce
                )
            elif args.action == "takeover":
                value = api.takeover(
                    args.task_id, action=args.takeover_action, basis=args.basis, note=args.note
                )
            else:  # resolve
                value = api.resolve_unknown(
                    args.action_key,
                    outcome=args.outcome,
                    basis=args.basis,
                    evidence=json.loads(args.evidence),
                )
        except (
            ApprovalRequestError,
            ActionCommitError,
            CommitRejected,
            ContractError,
            StoreError,
            ValueError,
        ) as error:  # review P2-8: a refusal is an answer, never a traceback
            _print({"error": str(error)})
            return EXIT_FAILED
        _print(value)
    finally:
        store.close()
    return EXIT_OK


def _routing_from_json(data: Mapping[str, Any]) -> RoutingRules:
    return RoutingRules(
        default=str(data["default"]),
        by_role={str(k): str(v) for k, v in dict(data.get("by_role") or {}).items()},
        by_task_kind={str(k): str(v) for k, v in dict(data.get("by_task_kind") or {}).items()},
        escalate={str(k): str(v) for k, v in dict(data.get("escalate") or {}).items()},
        escalate_after_failures=int(data.get("escalate_after_failures", 1)),
        fallback={str(k): str(v) for k, v in dict(data.get("fallback") or {}).items()},
    )


def _policy_cases(args: argparse.Namespace, names: list[str]) -> tuple[Any, ...]:
    """The held-out cases ``policy evaluate`` may use (plan D9-7'): on fixtures the
    multi-profile ``holdout`` case; with a real provider ``parse-kv`` (fresh provider
    per trial, reported as a real trial)."""

    from .observability.evaluation import EvaluationCase
    from .testing.fixtures import (
        POLICY_HOLDOUT_GOAL,
        policy_demo_profiles,
        policy_demo_routing,
        policy_spec,
    )

    if args.provider == "env":

        def parse_kv(tenant: str, key: str) -> MissionSpec:
            return replace(
                policy_spec(
                    "在隔离工作区实现字符串解析函数 parse_kv(text) -> dict（按 ; 分隔、= 分键值），"
                    "并通过 tests/test_parse_kv.py；tests/ 下文件不可修改。",
                    tenant,
                    key,
                ),
                budget=Budget(max_tokens=600_000, max_attempts=4),
            )

        catalog = {
            "parse-kv": EvaluationCase(
                "parse-kv",
                parse_kv,
                lambda: _provider(args, scenario="policy-promotion")[0],
                kind="env",
            )
        }
    else:
        catalog = {
            "holdout": EvaluationCase(
                "holdout",
                lambda tenant, key: policy_spec(POLICY_HOLDOUT_GOAL, tenant, key),
                provider=lambda: None,
                profiles=lambda: policy_demo_profiles(missions=1),
                routing=policy_demo_routing(),
            )
        }
    unknown = [n for n in names if n not in catalog]
    if unknown:
        raise ValueError(
            f"unknown policy evaluation cases for --provider {args.provider}: {unknown}; "
            f"known: {sorted(catalog)}"
        )
    return tuple(catalog[n] for n in names)


def cmd_policy(args: argparse.Namespace) -> int:  # noqa: C901 - one command, several verbs
    """``policy propose / evaluate / approve / reject / promote / rollback / list / show /
    status`` (ORCH-BUILD §11.4; plan D9-11'): the caller named by ``--as`` decides (the
    local build takes the name as given — a real deployment authenticates it).  Exit 0
    done, 1 refused by a rule or a gate, 2 a bad call."""

    import tempfile

    from .api.policies import PolicyApi, PolicyRequestError
    from .governance.permissions import Principal
    from .governance.promotion import registry_consistency
    from .observability.evaluation import EvaluationRefused
    from .observability.replay import library_copy
    from .orchestrator.policy_commits import PolicyCommitError

    library = Path(args.evidence_dir).resolve() / "orchestrator.db"
    if not library.is_file():  # review P2-4: never create an empty library by accident
        _print({"error": f"no orchestrator library at {library}"})
        return EXIT_USAGE
    scratch = tempfile.TemporaryDirectory(prefix="orch-policy-")
    reading = args.action in {"list", "show", "status"}
    store = (
        Store.open_readonly(library_copy(library, Path(scratch.name)))
        if reading
        else _open_store(args)
    )
    try:
        commit = CommitService(store)
        who = getattr(args, "as_principal", None) or "cli-reader"
        principal = Principal(who, who)
        try:
            value: Any
            if args.action in {"list", "show", "status"}:
                reader = PolicyApi(commit, principal)
                if args.action == "list":
                    value = reader.list()
                elif args.action == "show":
                    value = reader.show(args.identifier)
                else:
                    value = {**reader.status(), "consistency": registry_consistency(store)}
            elif args.action == "propose":
                api = PolicyApi(commit, principal)
                if args.params:
                    value = api.propose(
                        json.loads(Path(args.params).read_text(encoding="utf-8")), note=args.note
                    )
                else:
                    from .governance.learning import learn_and_register

                    active = store.active_policy()
                    if active is None or not active.get("params"):
                        raise ValueError("this library has no ACTIVE policy to learn against")
                    routing = (
                        None
                        if not args.routing
                        else _routing_from_json(
                            json.loads(Path(args.routing).read_text(encoding="utf-8"))
                        )
                    )
                    learned = learn_and_register(
                        commit,
                        [Path(h).resolve() for h in args.history],
                        base_params=active["params"],
                        routing=routing,
                        min_missions=args.min_missions,
                        min_group=args.min_group,
                    )
                    result = learned["result"]
                    value = {
                        "outcome": result.outcome,
                        "reasons": result.reasons,
                        "note": result.note,
                        "proposal": learned["proposal"],
                        "refusal": learned["refusal"],
                    }
                    if learned["proposal"] is None:
                        _print(value)
                        return EXIT_FAILED
            elif args.action == "evaluate":
                from .governance.gates import evaluate_candidate

                outcome = evaluate_candidate(
                    commit,
                    args.proposal_id,
                    cases=_policy_cases(
                        args, args.case or (["parse-kv"] if args.provider == "env" else ["holdout"])
                    ),
                    directory=Path(args.eval_dir).resolve(),
                    trials=args.trials,
                    config=_evaluation_config(args, {}),
                    timeout_seconds=args.timeout_seconds,
                )
                value = {
                    "verdict": outcome["verdict"],
                    "reasons": outcome["reasons"],
                    "evidence_kind": outcome["evidence_kind"],
                    "evaluation_id": outcome["evaluation"]["evaluation_id"],
                    "report": str(Path(args.eval_dir).resolve() / "gate.json"),
                }
                if outcome["verdict"] != "PASSED":
                    _print(value)
                    return EXIT_FAILED
            elif args.action in {"approve", "reject"}:
                api = PolicyApi(commit, principal)
                decide = api.approve if args.action == "approve" else api.reject
                value = decide(args.proposal_id, nonce=args.nonce, note=args.note)
            elif args.action == "promote":
                deployment = DeploymentPolicy()  # review P2-5: the deployment's cooldown only
                value = PolicyApi(commit, principal, deployment=deployment).promote(
                    args.proposal_id, accept_fixture_evidence=args.accept_fixture_evidence
                )
            else:  # rollback
                value = PolicyApi(commit, principal).rollback(to=args.to, reason=args.reason)
        except (PolicyRequestError, PolicyCommitError, EvaluationRefused) as error:
            _print({"error": str(error)})
            return EXIT_FAILED
        except (OSError, KeyError, TypeError, ValueError) as error:
            _print({"error": str(error)})
            return EXIT_USAGE
        _print(value)
    finally:
        store.close()
        scratch.cleanup()
    return EXIT_OK


def _demo_policy_promotion(args: argparse.Namespace) -> int:  # noqa: C901 - one narrative
    """Step 9 (ORCH-BUILD §11; original §28 stage four): history → a traceable candidate
    from the rule improver → gates against the ACTIVE version (one candidate fails, one
    passes and still only runs in evaluation libraries) → a person approves → promotion
    (a Mission created before it keeps its version) → rollback (the rolled-back version's
    events and costs untouched) — plus the honest refusal for too little history.  On
    fixtures this proves the mechanism, never a quality gain."""

    if args.provider != "fixtures":
        _print(
            {
                "error": "policy-promotion runs on fixtures; a real trial is "
                "`policy evaluate --provider env` on a proposal"
            }
        )
        return EXIT_USAGE
    from .api.policies import PolicyApi
    from .governance.gates import evaluate_candidate_async
    from .governance.learning import learn_and_register
    from .governance.permissions import Principal
    from .governance.promotion import DEPLOYMENT_TIMELINE, registry_consistency
    from .observability.evaluation import EvaluationCase
    from .observability.evidence import write_evidence
    from .observability.secrets import redact_text
    from .orchestrator.policy_commits import PolicyCommitError
    from .testing.fixtures import (
        POLICY_HISTORY_GOAL,
        POLICY_HOLDOUT_GOAL,
        policy_demo_profiles,
        policy_demo_routing,
        policy_spec,
    )

    root = Path(args.evidence_dir).resolve()
    routing = policy_demo_routing()
    key = args.idempotency_key
    timeout = getattr(args, "test_timeout", 120.0)
    history_config = OrchestratorConfig(
        evidence_root=root / "history", max_concurrency=1, test_timeout_seconds=timeout
    )
    production_config = OrchestratorConfig(
        evidence_root=root / "production", max_concurrency=1, test_timeout_seconds=timeout
    )
    person = Principal(args.as_principal, args.as_principal)
    holdout = EvaluationCase(
        "holdout",
        lambda tenant, run_key: policy_spec(POLICY_HOLDOUT_GOAL, tenant, run_key),
        provider=lambda: None,
        profiles=lambda: policy_demo_profiles(missions=1),
        routing=routing,
    )

    def write(name: str, value: Any) -> None:
        text, _found = redact_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
        )
        (root / name).write_text(text, encoding="utf-8")

    async def run() -> int:
        # 1. history under the built-in policy: six Missions, the cheap profile fails first
        async with Orchestrator(
            history_config, profiles=policy_demo_profiles(missions=6), routing=routing
        ) as history:
            for n in range(6):
                await history.submit_mission(
                    policy_spec(POLICY_HISTORY_GOAL, args.tenant, f"{key}-history-{n}")
                )
                await history.run()
        # 2. the production deployment
        async with Orchestrator(
            production_config, profiles=policy_demo_profiles(missions=3), routing=routing
        ) as orch:
            store, commit = orch.store, orch.commit
            start_snapshot = orch.policy_snapshot()
            seed = store.active_policy()
            assert seed is not None
            api = PolicyApi(
                commit,
                person,
                deployment=DeploymentPolicy(policy_cooldown_seconds=0),  # demo: no waiting
                max_concurrency=production_config.max_concurrency,
                profiles=tuple(orch.model_router.profiles),
            )
            thin = learn_and_register(
                commit,
                [root / "history"],
                base_params=seed["params"],
                routing=routing,
                min_missions=10,
            )
            learned = learn_and_register(
                commit, [root / "history"], base_params=seed["params"], routing=routing
            )
            candidate = learned["proposal"]
            if candidate is None:
                _print(
                    {
                        "error": "the rule improver proposed nothing",
                        "reasons": learned["result"].reasons,
                    }
                )
                return EXIT_FAILED
            flaky = api.propose(
                {"routing": {"by_task_kind": {"code": "flaky"}}}, note="试试另一个档位"
            )
            failed = await evaluate_candidate_async(
                commit,
                flaky["proposal_id"],
                cases=[holdout],
                directory=root / "evaluations" / "flaky",
                config={"test_timeout_seconds": timeout},
            )
            passed = await evaluate_candidate_async(
                commit,
                candidate["proposal_id"],
                cases=[holdout],
                directory=root / "evaluations" / "learned",
                config={"test_timeout_seconds": timeout},
            )
            try:
                api.promote(candidate["proposal_id"])
                unapproved = "promoted without approval (must not happen)"
            except PolicyCommitError as error:
                unapproved = str(error)
            before = await orch.submit_mission(
                policy_spec(POLICY_HISTORY_GOAL, args.tenant, f"{key}-before")
            )
            api.approve(candidate["proposal_id"], note="门槛通过（机制验证），批准上线")
            promoted = api.promote(candidate["proposal_id"])
            await orch.run()  # `before` runs now — under the seed it was bound to
            after = await orch.submit_mission(
                policy_spec(POLICY_HISTORY_GOAL, args.tenant, f"{key}-after")
            )
            await orch.run()
            events_before = [e.to_json() for e in store.iter_events(after.id)]

            def usage(mission_id: str) -> Any:
                return store.connection.execute(
                    "SELECT SUM(input_tokens + output_tokens), COUNT(*) FROM imported_usage WHERE mission_id = ?",
                    (mission_id,),
                ).fetchone()

            usage_before = usage(after.id)
            health = api.status()
            rolled = api.rollback(reason="演示：候选版本需要撤回")
            events_unchanged = [e.to_json() for e in store.iter_events(after.id)] == events_before
            usage_unchanged = usage(after.id) == usage_before
            later = await orch.submit_mission(
                policy_spec(POLICY_HISTORY_GOAL, args.tenant, f"{key}-later")
            )
            await orch.run()

            def mission_view(mission: Any) -> dict[str, Any]:
                final = store.get_mission(mission.id)
                assert final is not None
                attempts = [
                    a for t in store.list_tasks(mission.id) for a in store.list_attempts(t.id)
                ]
                return {
                    "mission_id": mission.id,
                    "policy_version_id": orch.policy_version_of(mission.id),
                    "status": str(final.status),
                    "attempts": len(attempts),
                    "first_profile": attempts[0].runtime_profile_id if attempts else None,
                }

            missions = {
                "before": mission_view(before),
                "after": mission_view(after),
                "later": mission_view(later),
            }
            for name, mission in (("before", before), ("after", after), ("later", later)):
                write_evidence(
                    directory=root / "production" / "missions" / mission.id,
                    store=store,
                    commit=commit,
                    mission_id=mission.id,
                    baseline={
                        "agent_orchestrator": __version__,
                        "provider_kind": "fixtures",
                        "policy_version_id": missions[name]["policy_version_id"],
                        "policy_snapshot": start_snapshot,
                        "routing": routing.to_json(),
                    },
                    workspaces_root=production_config.workspaces_root,
                    test_report=missions[name],
                    policy_snapshot=orch.policy_snapshot(),
                )
            consistency = registry_consistency(store)
            proposals = store.list_policy_proposals()
            write(
                "registry.json",
                {
                    "versions": store.list_policy_versions(),
                    "proposals": proposals,
                    "evaluations": {
                        p["proposal_id"]: store.list_policy_evaluations(p["proposal_id"])
                        for p in proposals
                    },
                    "decisions": {
                        p["proposal_id"]: store.list_policy_decisions(p["proposal_id"])
                        for p in proposals
                    },
                    "activations": store.list_policy_activations(),
                    "consistency": consistency,
                },
            )
            lines = []
            for event in store.iter_events(DEPLOYMENT_TIMELINE):
                text, _found = redact_text(
                    json.dumps(event.to_json(), ensure_ascii=False, sort_keys=True, default=str)
                )
                lines.append(text + "\n")
            (root / "policy_events.jsonl").write_text("".join(lines), encoding="utf-8")
            version = store.get_policy_version(str(candidate["version_id"]))
            summary = {
                "scenario": "policy-promotion",
                "note": "机制验证（fixture）：证明提出 → 门槛 → 批准 → 晋级 → 回滚的闭环存在，不代表真实质量提升",
                "thin_history": {
                    "outcome": thin["result"].outcome,
                    "reasons": thin["result"].reasons,
                },
                "learned": {
                    "outcome": learned["result"].outcome,
                    "proposal_id": candidate["proposal_id"],
                    "learner": learned["result"].manifest["learner"],
                    "training_missions": len(learned["result"].manifest["training"]),
                    "routing": None
                    if version is None
                    else version["params"]["routing"]["by_task_kind"],
                    "note": learned["result"].note,
                },
                "flaky": {
                    "proposal_id": flaky["proposal_id"],
                    "verdict": failed["verdict"],
                    "reasons": failed["reasons"],
                },
                "gate": {
                    "verdict": passed["verdict"],
                    "reasons": passed["reasons"],
                    "evidence_kind": passed["evidence_kind"],
                    "directory": str(root / "evaluations" / "learned"),
                },
                "unapproved_promotion": unapproved,
                "promotion": promoted,
                "versions": {"seed": seed["version_id"], "candidate": candidate["version_id"]},
                "missions": missions,
                "rollback": {
                    "version_id": rolled["version_id"],
                    "from_version_id": rolled["from_version_id"],
                    "still_bound": rolled["still_bound"],
                    "events_unchanged": events_unchanged,
                    "usage_unchanged": usage_unchanged,
                },
                "health": health["versions"],
                "registry_consistency": consistency,
                "files": [
                    "summary.json",
                    "registry.json",
                    "policy_events.jsonl",
                    "evaluations/",
                    "production/missions/",
                ],
            }
            write("summary.json", summary)
            _print({k: v for k, v in summary.items() if k != "health"})
            ok = (
                thin["result"].outcome == "insufficient"
                and failed["verdict"] == "FAILED"
                and passed["verdict"] == "PASSED"
                and missions["before"]["policy_version_id"] == seed["version_id"]
                and missions["after"]["policy_version_id"] == candidate["version_id"]
                and missions["later"]["policy_version_id"] == seed["version_id"]
                and all(m["status"] == "COMPLETED" for m in missions.values())
                and events_unchanged
                and usage_unchanged
                and not consistency
            )
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

    approval = sub.add_parser("approval")
    approval_sub = approval.add_subparsers(dest="action", required=True)

    def caller(p: argparse.ArgumentParser) -> None:
        common(p, provider=False)
        p.add_argument(
            "--as",
            required=True,
            dest="as_principal",
            help="the caller's identity (local build: self-declared; a real deployment authenticates it)",
        )

    listing = approval_sub.add_parser("list")
    caller(listing)
    listing.add_argument("--mission", dest="mission_id", default=None)
    listing.add_argument("--all", action="store_true", help="every request, not only PENDING")
    for name, target in (
        ("approve", "request_id"),
        ("reject", "request_id"),
        ("revoke", "request_id"),
    ):
        p = approval_sub.add_parser(name)
        caller(p)
        p.add_argument(target)
        if name != "approve":
            p.add_argument("--reason", required=True)
        if name != "revoke":
            p.add_argument("--nonce", default=None)
    p = approval_sub.add_parser("comment")
    caller(p)
    p.add_argument("target_id")
    p.add_argument("--text", required=True)
    p = approval_sub.add_parser("review")
    caller(p)
    p.add_argument("request_id")
    p.add_argument("--verdict", required=True, choices=("pass", "fail"))
    p.add_argument("--note", default="")
    p.add_argument("--nonce", default=None)
    p = approval_sub.add_parser("arbitrate")
    caller(p)
    p.add_argument("request_id")
    p.add_argument("--ruling", required=True)
    p.add_argument("--basis", required=True)
    p.add_argument("--nonce", default=None)
    p = approval_sub.add_parser("takeover")
    caller(p)
    p.add_argument("task_id")
    p.add_argument(
        "--action", required=True, choices=("stop", "retry_with_note"), dest="takeover_action"
    )
    p.add_argument("--basis", required=True)
    p.add_argument("--note", default="")
    p = approval_sub.add_parser("resolve")
    caller(p)
    p.add_argument("action_key")
    p.add_argument("--outcome", required=True, choices=("succeeded", "failed"))
    p.add_argument("--basis", required=True)
    p.add_argument("--evidence", required=True, help="a JSON object with what the person saw")

    replay = sub.add_parser("replay")  # step 8
    common(replay, provider=False)
    replay.add_argument("mission_id")
    replay.add_argument(
        "--events", default=None, help="replay an events.jsonl instead of the library's events"
    )
    replay.add_argument("--failures", action="store_true", help="add the timeline of what failed")
    replay.add_argument(
        "--attribution", action="store_true", help="add the contribution attribution"
    )
    replay.add_argument("--out", default=None, help="also write the report to this file")
    evaluate = sub.add_parser("evaluate")  # step 8
    common(evaluate, provider=True)
    evaluate.add_argument(
        "--plan", required=True, help="JSON: name, cases, strategies, trials, config"
    )

    policy = sub.add_parser("policy")  # step 9
    policy_sub = policy.add_subparsers(dest="action", required=True)
    p = policy_sub.add_parser("propose")
    caller(p)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--history", nargs="+", help="history evidence directories (read only)")
    source.add_argument("--params", help="a person's parameters: JSON, whitelist only")
    p.add_argument("--routing", default=None, help="JSON routing rules for rule R2")
    p.add_argument("--min-missions", type=int, default=6, dest="min_missions")
    p.add_argument("--min-group", type=int, default=5, dest="min_group")
    p.add_argument("--note", default="")
    p = policy_sub.add_parser("evaluate")
    common(p, provider=True)
    p.add_argument("proposal_id")
    p.add_argument("--eval-dir", required=True, dest="eval_dir", help="a new directory")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--case", action="append", default=None)
    p.add_argument("--timeout", type=float, default=300.0, dest="timeout_seconds")
    for name in ("approve", "reject"):
        p = policy_sub.add_parser(name)
        caller(p)
        p.add_argument("proposal_id")
        p.add_argument("--nonce", default=None)
        p.add_argument("--note", default="")
    p = policy_sub.add_parser("promote")
    caller(p)
    p.add_argument("proposal_id")
    p.add_argument("--accept-fixture-evidence", action="store_true", dest="accept_fixture_evidence")
    p = policy_sub.add_parser("rollback")
    caller(p)
    p.add_argument("--to", default=None, help="a RETIRED version (default: the most recent one)")
    p.add_argument("--reason", required=True)
    for name in ("list", "status"):
        p = policy_sub.add_parser(name)
        common(p, provider=False)
    p = policy_sub.add_parser("show")
    common(p, provider=False)
    p.add_argument("identifier")

    demo = sub.add_parser("demo")
    common(demo, provider=True)
    demo.add_argument("--scenario", required=True)
    demo.add_argument(  # step 7
        "--pause-for-approval",
        action="store_true",
        dest="pause_for_approval",
        help="stop while the Mission waits for a person (exit 4); rerun with the same key to continue",
    )
    demo.add_argument(
        "--as", default="demo-operator", dest="as_principal", help="the demo operator who approves"
    )
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
    if args.command == "approval":
        return cmd_approval(args)
    if args.command == "replay":
        return cmd_replay(args)
    if args.command == "evaluate":
        return cmd_evaluate(args)
    if args.command == "policy":
        return cmd_policy(args)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
