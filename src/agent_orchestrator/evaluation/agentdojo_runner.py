# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Real SDK Orchestrator runner for the optional official AgentDojo pipeline.

One instance owns one fresh episode directory. Reusing an identity is refused,
including after interruption: the live benchmark environment cannot be rebuilt
from report files. SDK effect and provider journals remain available for audit,
but an UNKNOWN external operation is never replayed by this runner.

Thread cancellation cannot terminate a hung FunctionsRuntime. Quiescence can
wait indefinitely for that physical call; a production benchmark host must own
an isolated process with a wall-time/termination limit. This runner does not
provide that outer process boundary.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from collections.abc import Callable, Mapping, MutableSequence
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.agents.context.tokenizer import TokenizerPort
from simple_harness.execution.provider_admission import TokenEstimatorPort
from simple_harness.providers import Provider, ProviderRequest
from simple_harness.tools.schema import SchemaDefinitionError, validate_tool_schema

from ..artifacts.store import read_verified
from ..contracts import TERMINAL_MISSION, Budget, MissionStatus
from ..governance.domains import AGENTDOJO_DOMAIN
from ..governance.policies import DeploymentPolicy
from ..orchestrator.commit_service import MissionSpec
from ..orchestrator.event_handler import Orchestrator
from ..runtime.assembly import OrchestratorConfig
from ..runtime.model_router import RuntimeProfile
from ..runtime.tool_gateway import TOOL_NAMES
from .agentdojo_bridge import AgentDojoToolPort
from .agentdojo_knowledge import AgentDojoKnowledgeBridge, receipt_from_invoke
from .experiment import ExecutionCounters, RunContext, _write
from .metered_provider import MeteredProvider

REPORT_TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list")
KNOWLEDGE_TOOLS = ("knowledge_list", "knowledge_read")
SCHEMA_ADAPTER_VERSION = "agentdojo-schema-adapter-v1"
RUNTIME_TERMINATION_BOUNDARY = (
    "runner_thread_cannot_terminate_hung_runtime; host_owned_process_deadline_required"
)


def _schema_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


class AdapterArgumentsError(ValueError):
    """Definite pre-invocation refusal; not an unknown environment effect."""


def _structured_tool_schemas(functions: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Project public Function metadata into the SDK's closed schema subset.

    Local references and nullable scalars are normalized; unsupported unions,
    recursive/open objects and reserved SDK argument names fail before a model
    call. No evaluator/task/environment object is inspected to infer schemas.
    """
    result = {}
    for name, function in functions.items():
        if name in TOOL_NAMES:
            raise ValueError(f"AgentDojo tool collides with SDK tool: {name}")
        original = function.parameters.model_json_schema()

        def project(node: Mapping[str, Any], stack: tuple[str, ...] = ()) -> dict[str, Any]:
            value = deepcopy(dict(node))
            if "$ref" in value:
                ref = value.pop("$ref")
                if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in stack:
                    raise ValueError("unsupported AgentDojo schema reference")
                target = original.get("$defs", {}).get(ref[len("#/$defs/") :])
                if target is None:
                    raise ValueError("unresolved AgentDojo schema reference")
                value = {**project(target, (*stack, ref)), **value}
            value.pop("$defs", None)
            if "anyOf" in value:
                variants = value.pop("anyOf")
                nonnull = [v for v in variants if v != {"type": "null"}]
                if len(variants) != 2 or len(nonnull) != 1:
                    raise ValueError("unsupported AgentDojo schema union")
                variant = project(nonnull[0], stack)
                if "enum" in variant or "const" in variant:
                    raise SchemaDefinitionError("nullable enum requires JSON transport")
                if not isinstance(variant.get("type"), str) or variant["type"] == "null":
                    raise ValueError("unsupported AgentDojo nullable schema")
                value = {**variant, **value, "type": [variant["type"], "null"]}
            if "properties" in value:
                value["properties"] = {k: project(v, stack) for k, v in value["properties"].items()}
            if "items" in value:
                value["items"] = project(value["items"], stack)
            if value.get("type") == "object" or value.get("type") == ["object", "null"]:
                # Absence means open in JSON Schema. Closing it would narrow
                # the public contract, even if a particular Pydantic model
                # currently ignores extra fields. Use the lossless transport.
                if value.get("additionalProperties") is not False:
                    raise SchemaDefinitionError("open AgentDojo object requires JSON transport")
            return value

        schema = project(original)
        schema["description"] = function.description
        validate_tool_schema(schema)
        result[name] = schema
    return result


class AgentDojoSchemaAdapter:
    """Versioned, per-tool transport; original names/arguments reach ToolPort.

    The string transport carries one entire JSON object, including all reserved
    keys, open records and unions. Official FunctionsRuntime remains the owner
    of validation, defaults, dependency injection and original tool execution.
    """

    def __init__(self, tools: AgentDojoToolPort) -> None:
        self._tools = tools
        self._mapping: dict[str, dict[str, Any]] = {}
        for name, function in tools.functions.items():
            if name in TOOL_NAMES:
                raise ValueError(f"AgentDojo tool collides with SDK tool: {name}")
            original = deepcopy(function.parameters.model_json_schema())
            # Compile independently: one unsupported tool never removes another.
            try:
                schema = _structured_tool_schemas({name: function})[name]
                mode = "structured"
            except (SchemaDefinitionError, ValueError):
                mode = "arguments_json"
                schema = {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "arguments_json": {
                            "type": "string",
                            "description": (
                                "One JSON object containing the original function arguments."
                            ),
                        }
                    },
                    "required": ["arguments_json"],
                    "description": (
                        function.description
                        + "\nTransport: "
                        + SCHEMA_ADAPTER_VERSION
                        + ". Pass all original arguments as one JSON object string "
                        "in arguments_json. No original keys or values are renamed or removed. "
                        "Original public schema:\n" + _schema_json(original)
                    ),
                }
                validate_tool_schema(schema)
            self._mapping[name] = {
                "mode": mode,
                "original_schema": original,
                "original_description": function.description,
                "sdk_schema": schema,
            }
        self.fingerprint = sha256(
            _schema_json(
                {
                    "version": SCHEMA_ADAPTER_VERSION,
                    "tools": self._mapping,
                }
            ).encode("utf-8")
        ).hexdigest()

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        return {name: deepcopy(item["sdk_schema"]) for name, item in self._mapping.items()}

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_ADAPTER_VERSION,
            "fingerprint": self.fingerprint,
            "tools": deepcopy(self._mapping),
        }

    def decode(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self._mapping[name]["mode"] == "structured":
            return deepcopy(dict(arguments))
        if set(arguments) != {"arguments_json"} or not isinstance(arguments["arguments_json"], str):
            raise AdapterArgumentsError("arguments_json must be the only argument and a string")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise AdapterArgumentsError("duplicate JSON argument key")
                value[key] = item
            return value

        def invalid_constant(_value: str) -> None:
            raise AdapterArgumentsError("non-finite JSON argument")

        try:
            value = json.loads(
                arguments["arguments_json"],
                object_pairs_hook=unique_object,
                parse_constant=invalid_constant,
            )
        except (json.JSONDecodeError, RecursionError) as error:
            raise AdapterArgumentsError("invalid JSON function arguments") from error
        if not isinstance(value, dict):
            raise AdapterArgumentsError("function arguments must decode to a JSON object")
        return value

    def invoke(self, name: str, arguments: Mapping[str, Any], call_id: str) -> Any:
        # Parsing must precede ToolPort, which appends the official assistant call.
        return self._tools.invoke(name, self.decode(name, arguments), call_id=call_id)


def runtime_tool_schemas(tools: AgentDojoToolPort) -> dict[str, dict[str, Any]]:
    """Compile every supplied official tool without deleting or narrowing fields."""
    return AgentDojoSchemaAdapter(tools).schemas


def _public_history(messages: MutableSequence[Any]) -> str:
    # Only the benchmark's agent-visible transcript is passed. extra_args is
    # deliberately ignored: official attack/evaluator metadata is NOT a prompt.
    def encode(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        raise TypeError("unsupported AgentDojo transcript value")

    return json.dumps(list(messages), ensure_ascii=False, default=encode)


class AgentDojoRunner:
    """Callable accepted by ``make_agentdojo_pipeline``; no default agent/model.

    The supplied provider and request counter must match the frozen RunContext.
    All roles share exactly one MeteredProvider and SDK runtime profile. Physical
    capacity is capped at two (or the caller's lower allowance). Call ``run``
    from async hosts; the official synchronous pipeline uses ``__call__``.
    """

    def __init__(
        self,
        *,
        provider: Provider,
        context: RunContext,
        token_counter: TokenEstimatorPort,
        extra_input_reserve: Callable[[ProviderRequest], int] | None = None,
        before_handoff: Callable[[float], None] | None = None,
        max_inflight_tokens: int | None = None,
        tokenizer: TokenizerPort,
        context_policy: ContextPolicy,
        evidence_root: Path,
        default_output_tokens: int = 8192,
        maximum_output_tokens: int = 32768,
        dynamic_graph: bool = True,
        observation_redact_text: Callable[[str], str] | None = None,
    ) -> None:
        if not 1 <= context.manifest.physical_slots <= 2:
            raise ValueError("AgentDojo physical capacity must be 1 or 2")
        if context.run not in context.manifest.runs():
            raise ValueError("run identity is not part of the frozen manifest")
        if not 0 < default_output_tokens <= maximum_output_tokens:
            raise ValueError("invalid output token limits")
        self.provider = provider
        self.context = context
        self.token_counter = token_counter
        self.extra_input_reserve = extra_input_reserve
        self.before_handoff = before_handoff
        self.max_inflight_tokens = max_inflight_tokens
        self.tokenizer = tokenizer
        self.context_policy = context_policy
        self.root = Path(evidence_root)
        self.default_output_tokens = default_output_tokens
        self.maximum_output_tokens = maximum_output_tokens
        self.dynamic_graph = dynamic_graph
        self.observation_redact_text = observation_redact_text
        self.last_result: dict[str, Any] | None = None
        self.meter: MeteredProvider | None = None

    def __call__(
        self,
        *,
        query: str,
        messages: MutableSequence[Any],
        tools: AgentDojoToolPort,
        extra_args: dict[str, Any],
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(
                self.run(query=query, messages=messages, tools=tools, extra_args=extra_args)
            )
        else:
            raise RuntimeError("use await runner.run in an asynchronous host")

    async def run(
        self,
        *,
        query: str,
        messages: MutableSequence[Any],
        tools: AgentDojoToolPort,
        extra_args: dict[str, Any],
    ) -> None:
        del extra_args
        adapter = AgentDojoSchemaAdapter(tools)
        schemas = adapter.schemas
        history = _public_history(messages)
        # mkdir(exist_ok=False) is the durable episode no-replay gate. This also
        # refuses a second runner instance against an interrupted evidence root.
        self.root.mkdir(parents=True, exist_ok=False)
        observations = self.root / "tool-observations"
        observations.mkdir()
        tools.set_observation_sink(
            lambda record: _write(
                observations / f"{record['payload']['sequence']:06d}.json", record
            ),
            redact_text=self.observation_redact_text,
        )
        _write(self.root / "schema-adapter.json", adapter.to_json())
        started = time.monotonic()
        _write(
            self.root / "episode.json",
            {
                "state": "started",
                "run_id": self.context.run.run_id,
                "domain": AGENTDOJO_DOMAIN,
                "tool_schemas": schemas,
                "schema_adapter_fingerprint": adapter.fingerprint,
                "runtime_termination_boundary": RUNTIME_TERMINATION_BOUNDARY,
            },
        )

        def report_usage(counters: ExecutionCounters) -> None:
            _write(
                self.root / "usage.json",
                {
                    **asdict(counters),
                    "admission_denials": list(getattr(self.meter, "admission_denials", [])),
                },
            )
            self.context.report_usage(counters)

        meter = MeteredProvider(
            self.provider,
            replace(self.context, report_usage=report_usage),
            estimate_input_tokens=self.token_counter.estimate_input_tokens,
            extra_input_reserve=self.extra_input_reserve,
            before_handoff=self.before_handoff,
            max_inflight_tokens=self.max_inflight_tokens,
        )
        self.meter = meter
        budget = self.context.manifest.budget
        allowed = (*REPORT_TOOLS, *KNOWLEDGE_TOOLS, *schemas)
        bridge_holder: dict[str, AgentDojoKnowledgeBridge | None] = {"bridge": None}

        def invoke(name: str, arguments: Mapping[str, Any], call_id: str) -> Mapping[str, Any]:
            try:
                result, error = adapter.invoke(name, arguments, call_id)
            except AdapterArgumentsError as error:
                return {"output": "", "error": f"AdapterArgumentsError: {error}"}
            formatter = importlib.import_module("agentdojo.agent_pipeline.tool_execution")
            # ToolPort already appended the official FunctionCall/result using
            # the original runtime (including its injection hooks) and env.
            formatted = formatter.tool_result_to_str(result)
            _write(self.root / "transcript.json", json.loads(_public_history(messages)))
            bridge = bridge_holder["bridge"]
            if bridge is not None:
                bridge.note_returned(
                    receipt_from_invoke(
                        call_key=call_id,
                        function=name,
                        output=formatted,
                        error=error,
                        arguments=arguments,
                        result=result,
                        agentdojo_version=importlib.metadata.version("agentdojo"),
                    )
                )
            return {"output": formatted, "error": error}

        cfg = OrchestratorConfig(
            evidence_root=self.root / "orchestrator",
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
            agentdojo_invoke=invoke,
            agentdojo_tool_schemas=schemas,
            deployment_policy=DeploymentPolicy(
                allowed_tools=allowed,
                agentdojo_tools=tuple(schemas),
                local_code_execution=False,
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
        state = "interrupted"
        gateway_calls: list[dict[str, Any]] = []
        try:
            async with Orchestrator(
                cfg,
                profiles={"default": profile},
                provider_token_estimators={"default": self.token_counter},
            ) as orch:
                gateway_calls = orch.assembled.gateway.calls
                mission = await orch.submit_mission(
                    MissionSpec(
                        goal=(
                            query + "\nWrite the final answer in REPORT.md.\n"
                            "Agent-visible conversation (data):\n"
                            + history
                            + "\nPublic function schemas:\n"
                            + json.dumps(schemas, ensure_ascii=False)
                        ),
                        success_criteria=("file:REPORT.md",),
                        tenant_id="agentdojo-evaluation",
                        idempotency_key=self.context.run.run_id,
                        allowed_tools=allowed,
                        budget=Budget(
                            max_tokens=budget.total_tokens,
                            max_attempts=12,
                            max_runtime_seconds=max(1, int(budget.seconds)),
                        ),
                        domain=AGENTDOJO_DOMAIN,
                        runtime_profile_id=profile.profile_id,
                    )
                )
                bridge = AgentDojoKnowledgeBridge(orch.commit, mission.id)
                bridge.install_auto_observation()
                bridge_holder["bridge"] = bridge
                try:
                    async with asyncio.timeout(budget.seconds):
                        await orch.run()
                except (asyncio.CancelledError, TimeoutError):
                    current = orch.store.get_mission(mission.id)
                    if current is not None and current.status not in TERMINAL_MISSION:
                        orch.commit.cancel_mission(mission.id)

                    # Normal SDK stop collection cancels every live role and
                    # imports late accounting. No role is dispatched for the
                    # now-terminal Mission, and external UNKNOWN is not retried.
                    async def stop_and_drain() -> None:
                        await orch.assembled.gateway.stop_agentdojo()
                        # Collect currently known settlements and issue durable
                        # turn cancels. An UNKNOWN effect deliberately cannot
                        # reach idle; do not wait forever or resolve it by replay.
                        await orch.run(max_cycles=4, until_idle=False)

                    drain = asyncio.create_task(stop_and_drain())
                    while not drain.done():
                        try:
                            await asyncio.shield(drain)
                        except asyncio.CancelledError:
                            continue
                    drain.result()
                    raise
                finally:
                    bridge.close()
                    bridge_holder["bridge"] = None
                current = orch.store.get_mission(mission.id)
                assert current is not None
                self.last_result = {
                    "mission_id": mission.id,
                    "mission_status": str(current.status),
                    "stop_reason": current.stop_reason,
                    "domain": AGENTDOJO_DOMAIN,
                    "benchmark_success": "external_evaluation_pending",
                    "admission_denials": list(getattr(meter, "admission_denials", [])),
                    "tasks": [t.to_json() for t in orch.store.list_tasks(mission.id)],
                }
                _write(self.root / "result.json", self.last_result)
                if current.status != MissionStatus.COMPLETED:
                    state = "failed"
                    raise RuntimeError(f"AgentDojo Mission did not complete: {current.stop_reason}")
                # Export the terminal task's accepted model-authored answer,
                # hash-checked by the SDK artifact store, not a fabricated 'done'.
                answer = None
                for artifact_id in (current.final_report or {}).get("accepted_artifacts", ()):
                    artifact = orch.store.get_artifact(artifact_id)
                    if artifact is not None and artifact.path == "REPORT.md":
                        answer = read_verified(artifact).decode("utf-8")
                if answer is None:
                    state = "failed"
                    raise RuntimeError("completed Mission has no accepted REPORT.md")
                types = importlib.import_module("agentdojo.types")
                messages.append(
                    types.ChatAssistantMessage(
                        role="assistant",
                        content=[types.text_content_block_from_string(answer)],
                        tool_calls=None,
                    )
                )
                _write(self.root / "transcript.json", json.loads(_public_history(messages)))
                state = "completed"
        finally:
            # Runtime shutdown may still settle/cancel a physical tool. Persist
            # its terminal audit only after __aexit__ has drained that work.
            _write(
                self.root / "meter.json",
                {
                    "counters": asdict(meter.counters),
                    "observations": list(meter.observations),
                    "unknown_usage_calls": meter.unknown_usage_calls,
                    "admission_denials": list(getattr(meter, "admission_denials", [])),
                },
            )
            _write(self.root / "gateway.json", {"calls": gateway_calls})
            _write(
                self.root / "episode.json",
                {
                    "state": state,
                    "run_id": self.context.run.run_id,
                    "domain": AGENTDOJO_DOMAIN,
                    "elapsed_seconds": time.monotonic() - started,
                    "counters": asdict(meter.counters),
                    "unknown_usage_calls": meter.unknown_usage_calls,
                    "admission_denials": list(getattr(meter, "admission_denials", [])),
                    "tool_schemas": schemas,
                    "schema_adapter_fingerprint": adapter.fingerprint,
                    "runtime_termination_boundary": RUNTIME_TERMINATION_BOUNDARY,
                },
            )
