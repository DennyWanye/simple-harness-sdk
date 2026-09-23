#!/usr/bin/env python3
"""Opt-in real-provider H1-I WAIT lifecycle probe.

This runner deliberately has no scripted model responses.  Both planner and worker
requests use ``real_provider_config``.  A narrow provider wrapper only parks a real
worker *request* until a durable, model-authored ``PlanningWaitRegistered`` appears;
it never changes a Task row or supplies a completion.  The real worker must produce
the terminal Task state through the normal execution/verification path.

Run manually from the SDK checkout.  It writes raw runtime evidence and a redacted
JSON summary under ``.local-test-evidence/`` (ignored by Git).  It reads provider
configuration exclusively through the existing ``real_provider_config`` helper and
does not print configuration values or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FULL_TARGET = ROOT / "tests" / "orchestrator" / "full_target"
for path in (ROOT / "src", ROOT / "tests" / "agents", FULL_TARGET):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import test_h1i_production_entry as h1i  # noqa: E402
import test_real_provider_hierarchical_smoke as real_smoke  # noqa: E402
from real_provider_config import build_real_provider, resolve_real_provider  # noqa: E402

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi  # noqa: E402
from agent_orchestrator.governance.permissions import Principal  # noqa: E402
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore  # noqa: E402
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore  # noqa: E402
from agent_orchestrator.testing.fixtures import role_of  # noqa: E402

MAX_SECONDS = 300
MAX_CALLS = 12


class WaitGatedRealProvider:
    """Pass every real request through, except the worker's first request is gated."""

    def __init__(self, delegate: Any, wait_registered: asyncio.Event, *, max_calls: int) -> None:
        self._delegate = delegate
        self._wait_registered = wait_registered
        # Scheduler concurrency may be two so a parked worker does not prevent the
        # Planner from emitting WAIT.  The actual external provider is nevertheless
        # strictly serial: only this critical section reaches ``delegate.invoke``.
        self._physical_call = asyncio.Semaphore(1)
        self.calls = 0
        self.physical_calls = 0
        self.max_calls = max_calls
        self.cap_reached = asyncio.Event()
        self.errors: list[dict[str, str]] = []
        self.roles: list[str] = []
        self.worker_gated = False

    async def invoke(self, request: Any, *, cancel: Any) -> Any:
        self.calls += 1
        role = role_of(request)
        self.roles.append(role)
        if role == "worker" and not self.worker_gated:
            self.worker_gated = True
            await asyncio.wait_for(self._wait_registered.wait(), timeout=MAX_SECONDS)
        async with self._physical_call:
            if self.physical_calls >= self.max_calls:
                self.cap_reached.set()
                await asyncio.Event().wait()
            self.physical_calls += 1
            try:
                return await self._delegate.invoke(request, cancel=cancel)
            except Exception as error:
                self.errors.append({"role": role, "error_type": type(error).__name__})
                raise


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    if args.max_seconds < 1 or (args.max_calls is not None and args.max_calls < 1):
        parser.error("limits must be positive")
    args.max_calls = args.max_calls or (40 if args.complete else MAX_CALLS)
    return args


def _events(loop: Any, mission_id: str, event_type: str) -> list[Any]:
    return [event for event in loop.store.list_events(mission_id) if event.type == event_type]


def _request_hashes(loop: Any, mission_id: str) -> list[dict[str, str]]:
    rows = loop.store.connection.execute(
        "SELECT request_id, intent_id, package_hash FROM planning_requests WHERE mission_id = ? "
        "ORDER BY created_at, request_id",
        (mission_id,),
    ).fetchall()
    return [
        {"request_id": str(row[0]), "intent_id": str(row[1]), "package_hash": str(row[2])}
        for row in rows
    ]


def _complete_success(mission: Any, events: list[Any], usage: dict[str, Any]) -> bool:
    completed_wake = any(
        event.type == "PlanningWaitWoken"
        and set(event.payload.get("settled_tasks", {}).values()) == {"COMPLETED"}
        for event in events
    )
    return bool(
        mission is not None
        and str(mission.status) == "COMPLETED"
        and completed_wake
        and all(
            int(usage.get(key, -1)) == 0
            for key in (
                "reserved_tokens",
                "reserved_cost_micros",
                "reserved_attempts",
                "reserved_tool_calls",
            )
        )
    )


async def main() -> int:
    args = _arguments()
    config = resolve_real_provider()
    if config is None:
        print(json.dumps({"status": "BLOCKED", "reason": "real_provider_config_unavailable"}))
        return 2

    stamp = str(time.time_ns())
    evidence = ROOT / ".local-test-evidence" / time.strftime("%Y-%m-%d") / f"h1i-real-wait-{stamp}"
    evidence.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    summary: dict[str, Any] = {
        "status": "INCOMPLETE",
        "scope": (
            "real WAIT through Mission completion; instrumented SDK scenario, not full H1-I"
            if args.complete
            else "real WAIT through worker completion and wake intent creation; not full H1-I"
        ),
        "model": config.model,
        "limits": {
            "seconds": args.max_seconds,
            "calls": args.max_calls,
            "scheduler_model_concurrency": 2,
            "physical_provider_concurrency": 1,
        },
        "instrumentation": (
            "first worker request waits for durable PlanningWaitRegistered; "
            "no model output or Task state is injected"
        ),
        "interruption_origin": "none",
        "known_limitations": [
            "The model may refuse the requested WAIT or fail to complete the real worker task.",
            "SDK real-provider evidence only; UI/package acceptance is separate.",
        ],
    }
    wait_registered = asyncio.Event()
    raw_provider = build_real_provider(config, timeout=120)
    provider = WaitGatedRealProvider(raw_provider, wait_registered, max_calls=args.max_calls)
    runner: asyncio.Task[None] | None = None

    try:
        repo = real_smoke._repo(evidence / "repo")
        runtime_config = h1i.OrchestratorConfig(
            evidence_root=evidence / "runtime",
            model=config.model,
            max_concurrency=2,
            max_concurrent_model_calls=2,
            default_max_output_tokens=4096,
            test_timeout_seconds=120,
            turn_deadline_seconds=args.max_seconds,
        )
        spec = h1i.MissionSpec(
            goal=(
                "修复仓库里失败的 tests/test_kv.py::test_parse_kv_strips_whitespace，"
                "不要改测试。先用真实 Planner 选择方法并让真实 Worker 执行。Worker 已经处于"
                "ACTIVE 或 VERIFYING 时，下一轮 Planner 必须从 visible_refs 原样引用该 Task，"
                "输出 WAIT；被唤醒后继续根据真实完成状态决策。"
            ),
            success_criteria=("pytest:tests/test_kv.py", "改动有说明"),
            tenant_id="real-h1i-wait",
            idempotency_key=evidence.name,
            allowed_tools=(
                "workspace_read_file",
                "workspace_write_file",
                "workspace_list",
                "run_tests",
            ),
            budget=h1i.Budget(max_tokens=600_000, max_attempts=8),
            orchestration_semantics_version=h1i.HIERARCHICAL_SEMANTICS,
            planning_protocol_version="planning-decision-v1",
            workspace_seed={"kvlib.py": real_smoke.PACKAGE, "tests/test_kv.py": real_smoke.FAILING},
        )
        async with h1i.Orchestrator(
            runtime_config, provider, critic_wait_seconds=args.max_seconds
        ) as loop:
            mission = await loop.submit_mission(spec)
            semantics = h1i.HtnStore(loop.store)
            environment = h1i.build_planning_world(
                mission.id,
                domains=("code",),
                semantics=semantics,
                deployed_layers=h1i.deployed_layers(runtime_config.deployment_policy),
                observers=h1i.code_observers(repo, allow_test_execution=True),
            )
            loop.install_hierarchical(planning=environment)
            binding = real_smoke._root_binding(environment, str(repo))
            h1i.ObligationStore(loop.store).register(
                h1i.Obligation(
                    obligation_id=h1i.ROOT_DUTY,
                    mission_id=mission.id,
                    requirement_refs=tuple(binding.goal_signature.coverage_criteria),
                    goal_signature_id=real_smoke.GOAL_TYPE,
                ),
                recursion_fuel=h1i.FUEL,
            )
            loop.commit.admit_obligation_demand(
                mission.id,
                h1i.ROOT_DUTY,
                principal="mission-submitter",
                requester={"kind": "mission_root"},
                evidence={
                    "mission_id": mission.id,
                    "requirement_refs": list(binding.goal_signature.coverage_criteria),
                },
            )
            semantics.put_task_semantics(mission.id, binding)
            h1i._approve_root_content_only_spec(
                loop.commit, mission, binding, command_id="real-wait-completion:" + mission.id
            )
            real_smoke._look(environment, semantics, mission.id, str(repo))
            loop.commit.begin_planning(mission.id)
            await loop._try_planner_intent(mission.id, ordinal=1)
            opener = loop.store.get_intent_for_subject(f"{mission.id}:planner:1")
            assert opener is not None
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(mission.id, command_id=f"grant-{evidence.name}", request_id=opener.intent_id)
            runner = asyncio.create_task(loop.run(max_cycles=400))
            probe_opened = False
            decisions = PlanningDecisionStore(loop.store)
            admissions = PlanningAdmissionStore(loop.store)
            issued: list[str] = []
            while time.monotonic() - started < args.max_seconds:
                # Complete mode authorizes only the exact persisted requests that
                # lack an authority binding; it never edits their state or payload.
                if args.complete:
                    for intent in loop.store.list_intents(
                        "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                    ):
                        request = decisions.get_planning_request_for_intent(intent.intent_id)
                        if (
                            intent.mission_id == mission.id
                            and request is not None
                            and admissions.get_request_binding(request.request_id) is None
                        ):
                            PlanningAuthorizationApi(
                                loop.store,
                                tenant_id=mission.tenant_id,
                                principal=Principal(loop._owner),
                            ).issue(
                                mission.id,
                                command_id=f"complete-host-grant:{evidence.name}:{request.request_id}",
                                request_id=request.request_id,
                            )
                            issued.append(request.request_id)
                if _events(loop, mission.id, "PlanningWaitRegistered"):
                    wait_registered.set()
                active = [
                    task
                    for task in loop.store.list_tasks(mission.id)
                    if str(task.status) in {"ACTIVE", "VERIFYING"}
                ]
                if active and not probe_opened:
                    ordinal = loop._next_planning_ordinal(mission.id)
                    await loop._planner_round_on_committed_plan(
                        mission.id, ordinal=ordinal, phase="real_wait_probe"
                    )
                    probe = loop.store.get_intent_for_subject(f"{mission.id}:planner:{ordinal}")
                    if probe is None:
                        summary["status"] = "BLOCKED"
                        summary["obstacle"] = "committed plan did not create a planner intent"
                        break
                    subjects = probe.config.get("planning_package", {}).get("planning_subjects", [])
                    if not subjects:
                        summary["status"] = "BLOCKED"
                        summary["obstacle"] = (
                            "committed-plan package has no planning_subjects"
                        )
                        break
                    PlanningAuthorizationApi(
                        loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
                    ).issue(
                        mission.id,
                        command_id=f"wait-grant-{evidence.name}",
                        request_id=probe.intent_id,
                    )
                    probe_opened = True
                if provider.cap_reached.is_set():
                    summary["interruption_origin"] = "runner_call_cap_cancel"
                    break
                if runner.done():
                    try:
                        await runner
                    except Exception as error:  # actual loop exception, not a synthetic cap failure
                        summary.update(
                            status="ERROR",
                            runner_error_type=type(error).__name__,
                            runner_error=str(error)[:500],
                        )
                    break
                if (
                    not args.complete
                    and wait_registered.is_set()
                    and _events(loop, mission.id, "PlanningWaitWoken")
                ):
                    break
                await asyncio.sleep(0.2)
            else:
                summary["status"] = "TIMEOUT"
            if summary["status"] == "INCOMPLETE" and wait_registered.is_set() and not args.complete:
                woken = _events(loop, mission.id, "PlanningWaitWoken")
                settled = woken[-1].payload.get("settled_tasks", {}) if woken else {}
                summary["status"] = (
                    "PASS" if settled and set(settled.values()) == {"COMPLETED"} else "PARTIAL"
                )
            if runner is not None and not runner.done():
                runner.cancel()
            if runner is not None:
                await asyncio.gather(runner, return_exceptions=True)
            events = loop.store.list_events(mission.id)
            summary.update(
                mission_id=mission.id,
                elapsed_seconds=round(time.monotonic() - started, 3),
                provider_calls=provider.calls,
                physical_provider_calls=provider.physical_calls,
                provider_roles=provider.roles,
                decisions=[
                    event.payload for event in events if event.type == "PlanningDecisionEvaluated"
                ],
                waits=[
                    event.payload
                    for event in events
                    if event.type in {"PlanningWaitRegistered", "PlanningWaitWoken"}
                ],
                request_hashes=_request_hashes(loop, mission.id),
                usage=real_smoke._account(loop, mission.id),
                mission_status=str(loop.store.get_mission(mission.id).status),
                accepted_outputs=[
                    event.payload.get("accepted_outputs")
                    for event in events
                    if event.type == "AcceptanceCommitted"
                ],
                issued_host_test_grants=issued,
                stop_reason=str(loop.store.get_mission(mission.id).stop_reason),
                prompt_versions=sorted(
                    {
                        str(intent.config.get("prompt_version", ""))
                        for intent in loop.store.list_intents(
                            "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
                        )
                        if intent.mission_id == mission.id
                        and intent.kind == "plan"
                    }
                ),
                provider_errors=provider.errors,
                dispatch_intent_states={
                    state: sum(
                        1
                        for intent in loop.store.list_intents(
                            "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
                        )
                        if intent.mission_id == mission.id and intent.state == state
                    )
                    for state in (
                        "PENDING",
                        "CLAIMED",
                        "AGENT_CREATED",
                        "SUBMITTED",
                        "SETTLED",
                        "FAILED",
                    )
                },
            )
            # Read only durable outcome metadata; request/target JSON can contain
            # authentication material and must never be copied into this summary.
            execution_path = (evidence / "runtime" / "execution.db").resolve()
            with sqlite3.connect(execution_path.as_uri() + "?mode=ro", uri=True) as read:
                summary["provider_outcomes"] = [
                    {"state": state, "error_code": error, "count": count}
                    for state, error, count in read.execute(
                        "SELECT state,error_code,count(*) FROM provider_invocations "
                        "GROUP BY state,error_code ORDER BY state,error_code"
                    )
                ]
            if args.complete and summary["status"] == "INCOMPLETE":
                final = loop.store.get_mission(mission.id)
                usage = summary["usage"]
                summary["status"] = "PASS" if _complete_success(final, events, usage) else "PARTIAL"
    except Exception as error:  # summary is the receipt; never print provider configuration
        summary.update(status="ERROR", error_type=type(error).__name__, error=str(error)[:500])
    finally:
        summary["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (evidence / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    key: summary.get(key)
                    for key in ("status", "model", "elapsed_seconds", "provider_calls", "obstacle")
                },
                ensure_ascii=False,
            )
        )
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
