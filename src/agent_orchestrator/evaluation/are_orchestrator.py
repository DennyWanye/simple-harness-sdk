# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit ARE -> SDK Mission runner; no default ARE agent, oracle or judge.

One instance owns a fresh episode and a persistent asyncio.Runner. Every turn
and role shares one MeteredProvider (including its absolute wall deadline).
Pass close_provider for an owned AsyncClient/provider; it runs on that same
loop after all Missions drain. Otherwise the supplied provider is borrowed.

A blocked AppTool thread cannot be killed here. Cancellation freezes dispatch,
retains UNKNOWN and waits for physical settlement. Production launchers MUST
own a process with an external hard deadline; bridge join failure is not exit.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import TokenizerPort
from simple_harness.execution.provider_admission import TokenEstimatorPort
from simple_harness.providers import Provider, ProviderRequest
from simple_harness.tools.schema import validate_tool_schema

from ..artifacts.store import read_verified
from ..contracts import TERMINAL_MISSION, Budget, MissionStatus
from ..governance.domains import ARE_DOMAIN
from ..governance.policies import DeploymentPolicy
from ..orchestrator.commit_service import MissionSpec
from ..orchestrator.event_handler import Orchestrator
from ..runtime.assembly import OrchestratorConfig
from ..runtime.model_router import RuntimeProfile
from ..runtime.tool_gateway import TOOL_NAMES
from .are_bridge import ARENotification, AREToolPort, AREVisibleState
from .experiment import ExecutionCounters, RunContext, _write
from .metered_provider import MeteredProvider

REPORT_TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list")
SCHEMA_ADAPTER_VERSION = "are-arguments-json-v1"
RUNTIME_TERMINATION_BOUNDARY = (
    "runner_thread_cannot_terminate_hung_apptool; host_owned_process_deadline_required"
)


class AREArgumentsError(ValueError):
    """Malformed transport rejected before any original AppTool handoff."""


def _decode(arguments: Mapping[str, Any]) -> dict[str, Any]:
    if set(arguments) != {"arguments_json"} or not isinstance(arguments["arguments_json"], str):
        raise AREArgumentsError("arguments_json must be the only argument and a string")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AREArgumentsError("duplicate argument key")
            result[key] = value
        return result

    def invalid(_value: str) -> None:
        raise AREArgumentsError("non-finite argument value")

    try:
        value = json.loads(
            arguments["arguments_json"], object_pairs_hook=unique, parse_constant=invalid
        )
    except (ValueError, RecursionError) as error:
        raise AREArgumentsError("invalid JSON arguments") from error
    if not isinstance(value, dict):
        raise AREArgumentsError("arguments must decode to a JSON object")
    return value


def runtime_tool_schemas(tools: AREToolPort) -> dict[str, dict[str, Any]]:
    """Lossless JSON transport, original public argument types/defaults in schema.

    ARE AppTool owns Python defaults and call behavior; no private function,
    class instance, app state or Scenario serialization enters this projection.
    """
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools.descriptions:
        if tool.name in {*TOOL_NAMES, "poll_notifications"}:
            raise ValueError("ARE tool collides with SDK tool or notification inbox")
        schemas[tool.name] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "arguments_json": {
                    "type": "string",
                    "description": "One JSON object of original AppTool arguments.",
                }
            },
            "required": ["arguments_json"],
            "description": (
                tool.description
                + "\nTransport: "
                + SCHEMA_ADAPTER_VERSION
                + ". Preserve original argument keys and values inside arguments_json. "
                "Original public argument descriptions:\n"
                + json.dumps(tool.argument_details or tool.arguments, ensure_ascii=False)
            ),
        }
    schemas["poll_notifications"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "description": (
            "Consume queued agent-visible user/environment notifications, including late "
            "conditions during this Mission. Returns ARE simulated time, independent of wall time. "
            "An empty inbox means no message is currently due; check again when needed."
        ),
    }
    for schema in schemas.values():
        validate_tool_schema(schema)
    return schemas


def _visible(notifications: tuple[ARENotification, ...], state: AREVisibleState) -> str:
    return json.dumps(
        {"notifications": [asdict(n) for n in notifications], "state": asdict(state)},
        default=lambda value: value.isoformat(),
        ensure_ascii=False,
    )


class AREOrchestratorRunner:
    """Synchronous ARERunner port, one episode, sequential turns on one event loop."""

    def __init__(
        self,
        *,
        provider: Provider,
        context: RunContext,
        token_counter: TokenEstimatorPort,
        tokenizer: TokenizerPort,
        context_policy: ContextPolicy,
        evidence_root: Path,
        extra_input_reserve: Callable[[ProviderRequest], int] | None = None,
        before_handoff: Callable[[float], None] | None = None,
        max_inflight_tokens: int | None = None,
        default_output_tokens: int = 8192,
        maximum_output_tokens: int = 32768,
        dynamic_graph: bool = True,
        close_provider: Callable[[], Awaitable[None]] | None = None,
        poll_seconds: float = 0.02,
    ) -> None:
        if not 1 <= context.manifest.physical_slots <= 2:
            raise ValueError("ARE physical capacity must be 1 or 2")
        if context.run not in context.manifest.runs():
            raise ValueError("run identity is not part of frozen manifest")
        if not 0 < default_output_tokens <= maximum_output_tokens:
            raise ValueError("invalid output token limits")
        from .are_bridge import _finite_seconds

        _finite_seconds("poll_seconds", poll_seconds)
        self.provider, self.context, self.token_counter = provider, context, token_counter
        self.tokenizer, self.context_policy = tokenizer, context_policy
        self.root = Path(evidence_root)
        self.default_output_tokens, self.maximum_output_tokens = (
            default_output_tokens,
            maximum_output_tokens,
        )
        self.dynamic_graph, self.poll_seconds = dynamic_graph, poll_seconds
        self._close_provider = close_provider
        self._extra_input_reserve = extra_input_reserve
        self._before_handoff = before_handoff
        self._max_inflight_tokens = max_inflight_tokens
        self._loop: asyncio.Runner | None = None
        self._thread: int | None = None
        self._closed = False
        self._turn = 0
        self._started = 0.0
        self._tools: AREToolPort | None = None
        self._history: list[dict[str, str]] = []
        self.results: list[dict[str, Any]] = []
        self.meter: MeteredProvider | None = None
        self.last_result: dict[str, Any] | None = None

    def run_turn(
        self,
        *,
        notifications: tuple[ARENotification, ...],
        state: AREVisibleState,
        tools: AREToolPort,
        stop_event: threading.Event,
    ) -> str | None:
        if self._closed:
            raise RuntimeError("ARE runner is closed")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("run_turn must run on the synchronous ARE worker")
        if self._thread is not None and self._thread != threading.get_ident():
            raise RuntimeError("ARE turns and close must share their owner thread")
        if self._loop is None:
            # Durable no-replay gate; never reuse a partially run environment.
            self.root.mkdir(parents=True, exist_ok=False)
            self._thread = threading.get_ident()
            self._loop = asyncio.Runner()
            self._started = time.monotonic()
            self._tools = tools
        elif tools is not self._tools:
            raise RuntimeError("ARE runner cannot switch episode tools")
        # Runner.run installs SIGINT handlers on the main thread. The benchmark
        # parent owns signals; use the persistent loop directly instead.
        return self._loop.get_loop().run_until_complete(
            self._run_turn(notifications, state, tools, stop_event)
        )

    async def _run_turn(
        self,
        notifications: tuple[ARENotification, ...],
        state: AREVisibleState,
        tools: AREToolPort,
        stop_event: threading.Event,
    ) -> str | None:
        if self.meter is None:

            def report(counters: ExecutionCounters) -> None:
                _write(self.root / "usage.json", asdict(counters))
                self.context.report_usage(counters)

            self.meter = MeteredProvider(
                self.provider,
                replace(self.context, report_usage=report),
                estimate_input_tokens=self.token_counter.estimate_input_tokens,
                extra_input_reserve=self._extra_input_reserve,
                before_handoff=self._before_handoff,
                max_inflight_tokens=self._max_inflight_tokens,
            )
        meter = self.meter
        self._turn += 1
        root = self.root / f"turn-{self._turn:04d}"
        root.mkdir()
        budget = self.context.manifest.budget
        remaining = self._started + budget.seconds - time.monotonic()
        schemas = runtime_tool_schemas(tools)
        mapping = {"version": SCHEMA_ADAPTER_VERSION, "schemas": schemas}
        fingerprint = sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()
        _write(root / "schema-adapter.json", {**mapping, "fingerprint": fingerprint})
        allowed = (*REPORT_TOOLS, *schemas)
        consumed_notifications: list[dict[str, Any]] = []

        def invoke(name: str, arguments: Mapping[str, Any], call_id: str) -> Mapping[str, Any]:
            if name == "poll_notifications":
                visible = json.loads(_visible(tools.poll_notifications(), tools.visible_state()))
                consumed_notifications.append(visible)
                _write(root / "notifications.json", {"polls": consumed_notifications})
                return visible
            try:
                decoded = _decode(arguments)
            except AREArgumentsError as error:
                return {"output": "", "error": str(error), "executed": False}
            # Preserve the official AppTool invocation, formatter and event hooks.
            return {"output": str(tools.invoke(name, decoded)), "call_id": call_id}

        cfg = OrchestratorConfig(
            evidence_root=root / "orchestrator",
            model=self.context.manifest.model,
            max_concurrency=3,
            max_concurrent_model_calls=self.context.manifest.physical_slots,
            dynamic_graph=self.dynamic_graph,
            knowledge_sharing=True,
            default_max_output_tokens=self.default_output_tokens,
            max_output_tokens_ceiling=self.maximum_output_tokens,
            max_model_calls_per_turn=budget.calls,
            max_tool_calls_per_turn=budget.calls * 3,
            turn_deadline_seconds=budget.seconds,
            stall_seconds=min(300, budget.seconds),
            attempt_reserve_tokens=min(100000, budget.total_tokens // 3),
            planner_reserve_tokens=min(50000, budget.total_tokens // 5),
            critic_reserve_tokens=min(50000, budget.total_tokens // 5),
            are_invoke=invoke,
            are_tool_schemas=schemas,
            deployment_policy=DeploymentPolicy(
                allowed_tools=allowed, are_tools=tuple(schemas), local_code_execution=False
            ),
        )
        profile = RuntimeProfile(
            "default",
            meter,
            self.context.manifest.model,
            provider_kind="env",
            context_policy=self.context_policy,
            tokenizer=self.tokenizer,
            default_max_output_tokens=self.default_output_tokens,
            max_output_tokens_ceiling=self.maximum_output_tokens,
            max_concurrent_model_calls=self.context.manifest.physical_slots,
        )
        outcome = "interrupted"
        gateway_calls: list[dict[str, Any]] = []
        _write(
            root / "episode.json",
            {
                "state": "started",
                "domain": ARE_DOMAIN,
                "runtime_termination_boundary": RUNTIME_TERMINATION_BOUNDARY,
            },
        )
        try:
            if remaining <= 0 or stop_event.is_set():
                stop_event.set()
                return None
            async with Orchestrator(
                cfg,
                profiles={"default": profile},
                provider_token_estimators={"default": self.token_counter},
            ) as orch:
                gateway_calls = orch.assembled.gateway.calls
                public_input = _visible(notifications, state)
                mission = await orch.submit_mission(
                    MissionSpec(
                        goal=(
                            "Handle the agent-visible ARE messages and write the final answer "
                            "in REPORT.md. "
                            "Consume poll_notifications during work and before reporting.\n"
                            + public_input
                            + "\nPrior visible turns:\n"
                            + json.dumps(self._history, ensure_ascii=False)
                            + "\nPublic tools:\n"
                            + json.dumps(schemas, ensure_ascii=False)
                        ),
                        success_criteria=("file:REPORT.md",),
                        tenant_id="are-evaluation",
                        idempotency_key=f"{self.context.run.run_id}:turn:{self._turn}",
                        allowed_tools=allowed,
                        domain=ARE_DOMAIN,
                        runtime_profile_id="default",
                        budget=Budget(
                            max_tokens=budget.total_tokens,
                            max_attempts=12,
                            max_runtime_seconds=max(1, int(remaining)),
                        ),
                    )
                )

                async def monitor_stop() -> None:
                    while not stop_event.is_set():
                        await asyncio.sleep(self.poll_seconds)

                work = asyncio.create_task(orch.run())
                monitor = asyncio.create_task(monitor_stop())
                try:
                    done, _ = await asyncio.wait(
                        (work, monitor),
                        timeout=max(0, self._started + budget.seconds - time.monotonic()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if work not in done or stop_event.is_set():
                        stop_event.set()
                        current = orch.store.get_mission(mission.id)
                        if current is not None and current.status not in TERMINAL_MISSION:
                            orch.commit.cancel_mission(mission.id)
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
                        # Set the gateway stop flag before cancelling active role
                        # turns. In-flight physical tools settle as UNKNOWN.
                        _write(
                            root / "episode.json",
                            {
                                "state": "stopping",
                                "mission_id": mission.id,
                                "runtime_termination_boundary": RUNTIME_TERMINATION_BOUNDARY,
                            },
                        )
                        await orch.assembled.gateway.stop_are()
                        await orch.run(max_cycles=4, until_idle=False)
                        current = orch.store.get_mission(mission.id)
                        assert current is not None
                        self.last_result = {
                            "mission_id": mission.id,
                            "mission_status": str(current.status),
                            "stop_reason": current.stop_reason,
                            "domain": ARE_DOMAIN,
                            "benchmark_success": "external_evaluation_pending",
                        }
                        self.results.append(self.last_result)
                        _write(root / "result.json", self.last_result)
                        return None
                    await work
                finally:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                current = orch.store.get_mission(mission.id)
                assert current is not None
                self.last_result = {
                    "mission_id": mission.id,
                    "mission_status": str(current.status),
                    "stop_reason": current.stop_reason,
                    "domain": ARE_DOMAIN,
                    "benchmark_success": "external_evaluation_pending",
                    "tasks": [t.to_json() for t in orch.store.list_tasks(mission.id)],
                }
                self.results.append(self.last_result)
                _write(root / "result.json", self.last_result)
                if current.status != MissionStatus.COMPLETED:
                    outcome = "failed"
                    raise RuntimeError(f"ARE Mission did not complete: {current.stop_reason}")
                answer = None
                for artifact_id in (current.final_report or {}).get("accepted_artifacts", ()):
                    artifact = orch.store.get_artifact(artifact_id)
                    if artifact is not None and artifact.path == "REPORT.md":
                        answer = read_verified(artifact).decode("utf-8")
                if answer is None:
                    outcome = "failed"
                    raise RuntimeError("completed ARE Mission has no accepted REPORT.md")
                self._history.append(
                    {
                        "input": public_input,
                        "answer": answer,
                        "consumed_notifications": json.dumps(
                            consumed_notifications, ensure_ascii=False
                        ),
                    }
                )
                outcome = "completed"
                return answer
        finally:
            if outcome != "completed":
                stop_event.set()
            _write(root / "gateway.json", {"calls": gateway_calls})
            _write(
                root / "episode.json",
                {
                    "state": outcome,
                    "domain": ARE_DOMAIN,
                    "run_id": self.context.run.run_id,
                    "turn": self._turn,
                    "elapsed_seconds": time.monotonic() - self._started,
                    "counters": asdict(meter.counters),
                    "unknown_usage_calls": meter.unknown_usage_calls,
                "admission_denials": [dict(item) for item in meter.admission_denials],
                    "schema_adapter_fingerprint": fingerprint,
                    "runtime_termination_boundary": RUNTIME_TERMINATION_BOUNDARY,
                },
            )

    def execution_metadata(self) -> dict[str, Any]:
        return {
            "domain": ARE_DOMAIN,
            "run_id": self.context.run.run_id,
            "missions": [
                {k: v for k, v in result.items() if k != "tasks"} for result in self.results
            ],
            "counters": asdict(self.meter.counters) if self.meter is not None else None,
            "unknown_usage_calls": self.meter.unknown_usage_calls if self.meter else 0,
            "admission_denials": (
                [dict(item) for item in self.meter.admission_denials] if self.meter else []
            ),
            "runtime_termination_boundary": RUNTIME_TERMINATION_BOUNDARY,
        }

    def close(self) -> None:
        """Close owned resources once, on the same loop as all provider calls."""
        if self._closed:
            return
        if self._thread is not None and self._thread != threading.get_ident():
            raise RuntimeError("ARE close must run on its owner thread")
        self._closed = True
        if self._loop is None and self._close_provider is not None:
            self._loop = asyncio.Runner()
        if self._loop is not None:
            try:
                if self._close_provider is not None:
                    async def close_owned() -> None:
                        assert self._close_provider is not None
                        await self._close_provider()
                    self._loop.get_loop().run_until_complete(close_owned())
            finally:
                self._loop.close()
