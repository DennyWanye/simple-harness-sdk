# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Actual AgentDojo runtime contract; no model, benchmark suite, or message send."""

from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel

from agent_orchestrator.evaluation.agentdojo_bridge import (
    AgentDojoToolPort,
    make_agentdojo_pipeline,
)

agentdojo = pytest.importorskip("agentdojo")
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement  # noqa: E402
from agentdojo.functions_runtime import (  # noqa: E402
    Depends,
    FunctionsRuntime,
    TaskEnvironment,
    make_function,
)
from agentdojo.task_suite.task_suite import (  # noqa: E402
    functions_stack_trace_from_messages,
    model_output_from_messages,
)
from agentdojo.types import (  # noqa: E402
    ChatAssistantMessage,
    ChatSystemMessage,
    text_content_block_from_string,
)


class Note(BaseModel):
    text: str


class LocalEnv(TaskEnvironment):
    note: Note


def read_note(note: Annotated[Note, Depends("note")]) -> str:
    """Read the harmless local note."""
    return note.text


class InjectingRuntime(FunctionsRuntime):
    def __init__(self):
        super().__init__([make_function(read_note)])
        self.calls = []

    def run_function(self, env, function, kwargs, raise_on_error=False):
        self.calls.append((env, function, dict(kwargs)))
        result, error = super().run_function(env, function, kwargs, raise_on_error)
        if error is None:
            return f"{result} [synthetic injection hook]", None
        return result, error


def test_official_pipeline_and_runtime_keep_environment_history_and_hook():
    runtime = InjectingRuntime()
    env = LocalEnv(note=Note(text="harmless"))
    initial = ChatSystemMessage(role="system", content=[text_content_block_from_string("safe")])
    messages = [initial]
    extra_args = {"episode": "synthetic"}
    seen = {}

    def orchestrator_runner(*, query, messages, tools, extra_args):
        seen["query"] = query
        seen["schema"] = tools.functions["read_note"].parameters.model_json_schema()
        seen["result"] = tools.invoke("read_note", {}, call_id="call-1")
        seen["extra_args"] = extra_args
        messages.append(
            ChatAssistantMessage(
                role="assistant",
                content=[text_content_block_from_string("done")],
                tool_calls=None,
            )
        )

    pipeline = make_agentdojo_pipeline(orchestrator_runner)
    assert isinstance(pipeline, BasePipelineElement)
    result_query, returned_runtime, returned_env, transcript, returned_args = pipeline.query(
        "read the local note", runtime, env, messages, extra_args
    )
    assert result_query == seen["query"] == "read the local note"
    assert returned_runtime is runtime and returned_env is env
    assert transcript[0] == initial and transcript[0] is not initial
    assert messages == [initial]
    assert [entry["role"] for entry in transcript] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert transcript[1]["content"][0]["content"] == "read the local note"
    assert transcript[2]["tool_calls"][0].id == transcript[3]["tool_call_id"] == "call-1"
    assert transcript[3]["content"][0]["content"] == "harmless [synthetic injection hook]"
    assert functions_stack_trace_from_messages(transcript)[0].function == "read_note"
    assert model_output_from_messages(transcript)[0]["content"] == "done"
    assert seen["result"] == ("harmless [synthetic injection hook]", None)
    assert runtime.calls == [(env, "read_note", {})]
    assert env.note.text == "harmless"
    assert seen["schema"]["type"] == "object"
    assert returned_args == extra_args == {"episode": "synthetic"}
    assert returned_args is not extra_args


def test_unknown_tool_uses_official_runtime_error_and_is_recorded():
    runtime = FunctionsRuntime([])

    def orchestrator_runner(*, query, messages, tools, extra_args):
        result, error = tools.invoke("missing", {}, call_id="bad-1")
        assert result == ""
        assert error and error.startswith("ToolNotFoundError:")
        messages.append(
            ChatAssistantMessage(
                role="assistant",
                content=[text_content_block_from_string("tool unavailable")],
                tool_calls=None,
            )
        )

    _, same_runtime, _, transcript, _ = make_agentdojo_pipeline(orchestrator_runner).query(
        "lookup", runtime
    )
    assert same_runtime is runtime
    assert transcript[-2]["role"] == "tool"
    assert transcript[-2]["error"].startswith("ToolNotFoundError:")


def test_runner_cannot_discard_benchmark_messages():
    def bad_runner(*, query, messages, tools, extra_args):
        messages.clear()
        messages.append(
            ChatAssistantMessage(
                role="assistant",
                content=[text_content_block_from_string("done")],
                tool_calls=None,
            )
        )

    with pytest.raises(ValueError, match="removed original messages"):
        make_agentdojo_pipeline(bad_runner).query("keep this query", FunctionsRuntime([]))


def test_module_import_does_not_require_agentdojo():
    code = (
        "import sys; "
        "sys.modules['agentdojo'] = None; "
        "import agent_orchestrator.evaluation.agentdojo_bridge as bridge; "
        "assert callable(bridge.make_agentdojo_pipeline)"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_runner_cannot_mutate_original_message_contents():
    initial = ChatSystemMessage(role="system", content=[text_content_block_from_string("original")])

    def bad_runner(*, query, messages, tools, extra_args):
        messages[0]["content"][0]["content"] = "rewritten"
        messages.append(
            ChatAssistantMessage(
                role="assistant", content=[text_content_block_from_string("done")], tool_calls=None
            )
        )

    with pytest.raises(ValueError, match="original messages"):
        make_agentdojo_pipeline(bad_runner).query("keep", FunctionsRuntime([]), messages=[initial])
    assert initial["content"][0]["content"] == "original"


def test_raw_observation_preserves_official_hook_types_and_call_time_snapshot():
    from agentdojo.agent_pipeline.tool_execution import tool_result_to_str

    class StructuredHook(InjectingRuntime):
        def run_function(self, env, function, kwargs, raise_on_error=False):
            super().run_function(env, function, kwargs, raise_on_error)
            self.result = [1, "1"]
            return self.result, None

    runtime = StructuredHook()
    env = LocalEnv(note=Note(text="ENV_ORACLE_MUST_NOT_BE_COPIED"))
    transcript, records = [], []
    port = AgentDojoToolPort(runtime, env, transcript)
    port.set_observation_sink(records.append)
    result, error = port.invoke("read_note", {}, call_id="structured-1")
    assert result is runtime.result and error is None
    assert tool_result_to_str([1, "1"]) == tool_result_to_str(["1", "1"])
    assert transcript[-1]["content"][0]["content"] == tool_result_to_str(result)
    runtime.result[0] = 999
    record = records[-1]
    assert record["payload"]["result"] == [1, "1"]
    assert records[0]["payload"]["state"] == "started"
    assert record["payload"]["error"] is None
    assert record["payload"]["call_id"] == "structured-1"
    identity = record["payload"]["runtime"]
    assert identity["class"]["module"] == __name__
    assert identity["class"]["qualname"].endswith("StructuredHook")
    assert (
        identity["class"]["source_file_sha256"] == sha256(Path(__file__).read_bytes()).hexdigest()
    )
    assert identity["agentdojo_version"] == "0.1.35"
    raw = json.dumps(
        record["payload"],
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    assert sha256(raw.encode()).hexdigest() == record["sha256"]
    assert "ENV_ORACLE_MUST_NOT_BE_COPIED" not in json.dumps(records)
    assert "agentdojo-tool-observation-v1" not in str(transcript)
    assert runtime.calls == [(env, "read_note", {})]


def test_observation_redacts_fields_exact_values_and_excludes_environment():
    class Result(BaseModel):
        password: str
        details: dict

    class StructuredRuntime(FunctionsRuntime):
        def run_function(self, env, function, kwargs, raise_on_error=False):
            kwargs["nested"]["value"] = "mutated"
            return Result(
                password="secret-password",
                details={
                    "value": "opaque-host-secret",
                    "token": "secret-token",
                    "environment": env,
                },
            ), "Bearer secret-bearer"

    env = LocalEnv(note=Note(text="hidden-oracle"))
    port = AgentDojoToolPort(StructuredRuntime([]), env, [])
    records = []
    port.set_observation_sink(
        records.append, redact_text=lambda s: s.replace("opaque-host-secret", "[REDACTED]")
    )
    result, error = port.invoke(
        "hook", {"nested": {"value": "original"}, "api_key": "secret-key"}, call_id="r"
    )
    assert result.password == "secret-password" and error == "Bearer secret-bearer"
    payload = records[-1]["payload"]
    assert payload["arguments"] == {"nested": {"value": "original"}, "api_key": "[REDACTED]"}
    assert payload["result"]["fields"]["password"] == "[REDACTED]"
    assert payload["result"]["fields"]["details"]["environment"]["reason"] == "environment"
    assert payload["error"] == "Bearer [REDACTED]"
    for secret in (
        "secret-password",
        "secret-key",
        "secret-token",
        "opaque-host-secret",
        "hidden-oracle",
        "secret-bearer",
    ):
        assert secret not in json.dumps(records)


@pytest.mark.parametrize("failure", ["runtime", "formatter"])
def test_observation_records_exception_type_without_exception_repr(failure):
    class DangerousError(RuntimeError):
        def __str__(self):
            raise AssertionError("exception text must not be read")

    class BrokenRuntime(FunctionsRuntime):
        def run_function(self, env, function, kwargs, raise_on_error=False):
            if failure == "runtime":
                raise DangerousError("secret error argument")
            return [None], None  # official formatter rejects this, after raw snapshot

    records, transcript = [], []
    port = AgentDojoToolPort(BrokenRuntime([]), LocalEnv(note=Note(text="private")), transcript)
    port.set_observation_sink(records.append)
    with pytest.raises(DangerousError if failure == "runtime" else TypeError):
        port.invoke("hook", {}, call_id="failure")
    payload = records[-1]["payload"]
    assert payload["state"] == failure + "_exception"
    assert payload["exception_type"].endswith(
        "DangerousError" if failure == "runtime" else "TypeError"
    )
    assert "secret error argument" not in json.dumps(records)
    if failure == "formatter":
        assert payload["result"] == [None]
    assert len(transcript) == 1  # same unfinished official transcript as before
