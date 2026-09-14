# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""AgentDojo pipeline port for an injected SDK Orchestrator episode runner.

This module has no AgentDojo import side effect. The host must supply a runner
that drives a real Orchestrator Mission; this bridge does not select an agent or
provider. AgentDojo owns the task environment and FunctionsRuntime throughout.
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
import re
from collections.abc import Callable, Mapping, MutableSequence, Sequence
from copy import deepcopy
from datetime import date, datetime, time
from enum import Enum
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from threading import Lock
from typing import Any, Protocol


def _type_name(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _identity(target: Any) -> dict[str, Any]:
    """Identify code, never serialize its instance, globals, or source text."""
    source = None
    try:
        filename = inspect.getsourcefile(target)
        if filename:
            source = sha256(Path(filename).read_bytes()).hexdigest()
    except (OSError, TypeError):
        pass
    module = getattr(target, "__module__", None)
    versions = {}
    for distribution in metadata.packages_distributions().get((module or "").split(".")[0], []):
        try:
            versions[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            pass
    return {
        "module": module,
        "qualname": getattr(target, "__qualname__", None),
        "source_file_sha256": source,
        "package_versions": versions,
    }


def _redact_text(text: str) -> str:
    # Host may additionally supply exact-value redaction for deployment secrets.
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"\b(?:sk-|tsk_|key_)[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(
        r"(?i)\b(password|passwd|api[_-]?key|access[_-]?token|secret|cookie)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )


def _snapshot(
    value: Any, env: Any, redact: Callable[[str], str], seen: set[int] | None = None
) -> Any:
    """Copy declared result structure now; no repr, arbitrary __dict__, or oracle traversal."""
    from agentdojo.functions_runtime import TaskEnvironment
    from pydantic import BaseModel

    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str):
        return redact(_redact_text(value))
    if type(value) is float:
        return value if math.isfinite(value) else {"unsupported_type": "nonfinite_float"}
    if isinstance(value, (datetime, date, time)):
        return {"type": _type_name(value), "isoformat": value.isoformat()}
    if isinstance(value, Enum):
        return {"type": _type_name(value), "value": _snapshot(value.value, env, redact, seen)}
    if value is env or isinstance(value, TaskEnvironment):
        return {"excluded_type": _type_name(value), "reason": "environment"}
    seen = set() if seen is None else seen
    if id(value) in seen:
        return {"excluded_type": _type_name(value), "reason": "cycle"}
    seen = seen | {id(value)}

    def fields(items):
        result = {}
        for key, item in items:
            if not isinstance(key, str):
                return {"unsupported_type": _type_name(value), "reason": "nonstring_keys"}
            safe_key = redact(_redact_text(key))
            sensitive = re.sub(r"[^a-z0-9]", "", key.lower())
            result[safe_key] = (
                "[REDACTED]"
                if sensitive
                in {
                    "password",
                    "passwd",
                    "apikey",
                    "token",
                    "accesstoken",
                    "refreshtoken",
                    "secret",
                    "authorization",
                    "cookie",
                    "cookies",
                    "clientsecret",
                }
                else _snapshot(item, env, redact, seen)
            )
        return result

    if isinstance(value, Mapping):
        return fields(value.items())
    if isinstance(value, (list, tuple)):
        items = [_snapshot(item, env, redact, seen) for item in value]
        return items if isinstance(value, list) else {"type": "tuple", "items": items}
    if isinstance(value, BaseModel):
        return {
            "model_type": _type_name(value),
            "fields": fields((name, getattr(value, name)) for name in type(value).model_fields),
        }
    return {"unsupported_type": _type_name(value)}


class OrchestratorRunner(Protocol):
    """Synchronous boundary called by AgentDojo's synchronous pipeline API.

    A production implementation must run the SDK Orchestrator, append its
    assistant/tool transcript to ``messages``, and use ``tools.invoke`` for
    every AgentDojo tool call. It must not create a separate task environment.
    """

    def __call__(
        self,
        *,
        query: str,
        messages: MutableSequence[Any],
        tools: AgentDojoToolPort,
        extra_args: dict[str, Any],
    ) -> None: ...


class AgentDojoToolPort:
    """Expose original Function metadata and execute against the supplied runtime.

    A runtime subclass or wrapper supplied by the benchmark is deliberately
    retained: its ``run_function`` may be the prompt-injection hook.
    """

    def __init__(self, runtime: Any, env: Any, messages: MutableSequence[Any]) -> None:
        self._runtime = runtime
        self._env = env
        self._messages = messages
        self._observation_sink: Callable[[dict[str, Any]], None] | None = None
        self._observation_redact: Callable[[str], str] = lambda text: text
        self._observation_lock = Lock()
        self._observation_sequence = 0
        self._runtime_identity: dict[str, Any] = {}

    def set_observation_sink(
        self,
        sink: Callable[[dict[str, Any]], None],
        *,
        redact_text: Callable[[str], str] | None = None,
    ) -> None:
        """Bind a Host-only sink before use; evidence never enters messages/extra_args."""
        if self._observation_sink is not None or self._observation_sequence:
            raise ValueError("AgentDojo observation sink is already bound")
        self._runtime_identity = {
            "class": _identity(type(self._runtime)),
            "run_function": _identity(self._runtime.run_function),
            "agentdojo_version": metadata.version("agentdojo"),
        }
        self._observation_redact = redact_text or (lambda text: text)
        self._observation_sink = sink

    def _emit_observation(self, record: dict[str, Any]) -> None:
        if self._observation_sink is None:
            return
        raw = json.dumps(
            record, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        self._observation_sink(
            {
                "payload": json.loads(raw),
                "sha256": sha256(raw.encode()).hexdigest(),
                "hash_scope": "canonical_redacted_payload_utf8",
            }
        )

    @property
    def functions(self) -> Mapping[str, Any]:
        return self._runtime.functions

    def invoke(self, function: str, args: Mapping[str, Any], *, call_id: str | None = None) -> Any:
        """Record an official-shaped assistant call and its runtime tool result."""
        function_call = importlib.import_module("agentdojo.functions_runtime").FunctionCall(
            function=function, args=dict(args), id=call_id
        )
        types = importlib.import_module("agentdojo.types")
        formatter = importlib.import_module("agentdojo.agent_pipeline.tool_execution")
        self._messages.append(
            types.ChatAssistantMessage(role="assistant", content=None, tool_calls=[function_call])
        )
        observation = None
        if self._observation_sink is not None:
            with self._observation_lock:
                self._observation_sequence += 1
                sequence = self._observation_sequence
            observation = {
                "version": "agentdojo-tool-observation-v1",
                "sequence": sequence,
                "function": function,
                "call_id": call_id,
                "state": "started",
                "runtime": self._runtime_identity,
                "arguments": _snapshot(function_call.args, self._env, self._observation_redact),
            }
            observation["arguments_sha256"] = sha256(
                json.dumps(
                    observation["arguments"],
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            self._emit_observation(observation)
        try:
            result, error = self._runtime.run_function(self._env, function, function_call.args)
        except BaseException as exc:
            if observation is not None:
                observation.update(state="runtime_exception", exception_type=_type_name(exc))
                self._emit_observation(observation)
            raise
        if observation is not None:
            observation.update(
                state="returned",
                result=_snapshot(result, self._env, self._observation_redact),
                error=_snapshot(error, self._env, self._observation_redact),
            )
            self._emit_observation(observation)
        try:
            formatted = formatter.tool_result_to_str(result)
        except BaseException as exc:
            if observation is not None:
                observation.update(state="formatter_exception", exception_type=_type_name(exc))
                self._emit_observation(observation)
            raise
        self._messages.append(
            types.ChatToolResultMessage(
                role="tool",
                content=[types.text_content_block_from_string(formatted)],
                tool_call_id=call_id,
                tool_call=function_call,
                error=error,
            )
        )
        return result, error


def make_agentdojo_pipeline(runner: OrchestratorRunner) -> Any:
    """Create an actual BasePipelineElement when optional AgentDojo is installed.

    ``runner`` is required; no official default agent or model fallback exists.
    The returned object can be passed to AgentDojo's benchmark entry point.
    """
    if not callable(runner):
        raise TypeError("an Orchestrator runner is required")
    try:
        base = importlib.import_module("agentdojo.agent_pipeline.base_pipeline_element")
        functions = importlib.import_module("agentdojo.functions_runtime")
        types = importlib.import_module("agentdojo.types")
    except ImportError as exc:
        raise ImportError(
            "AgentDojo is optional; install it in an isolated evaluation environment"
        ) from exc

    # The optional base is loaded at call time so SDK imports work without AgentDojo.
    class OrchestratorPipeline(base.BasePipelineElement):  # type: ignore[name-defined]
        name = "simple_harness_orchestrator"

        def query(
            self,
            query: str,
            runtime: Any,
            env: Any = None,
            messages: Sequence[Any] = (),
            extra_args: dict[str, Any] | None = None,
        ) -> tuple[str, Any, Any, Sequence[Any], dict[str, Any]]:
            if not isinstance(runtime, functions.FunctionsRuntime):
                raise TypeError("the official FunctionsRuntime is required")
            if env is None:
                env = functions.EmptyEnv()
            history = deepcopy(list(messages))
            history.append(
                types.ChatUserMessage(
                    role="user", content=[types.text_content_block_from_string(query)]
                )
            )
            prefix = deepcopy(history)
            metadata = {} if extra_args is None else deepcopy(extra_args)
            tools = AgentDojoToolPort(runtime, env, history)
            runner(query=query, messages=history, tools=tools, extra_args=metadata)
            if history[: len(prefix)] != prefix:
                raise ValueError("Orchestrator runner removed original messages or query")
            if (
                history[-1]["role"] != "assistant"
                or history[-1].get("tool_calls")
                or history[-1].get("content") is None
            ):
                raise ValueError("Orchestrator runner must finish with an assistant answer")
            return query, runtime, env, history, metadata

    return OrchestratorPipeline()
