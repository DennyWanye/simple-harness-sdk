# Quick Start Guide

Get started with Simple Harness SDK 0.3.0 in 10 minutes.

> **Code block convention in this document:** exactly **one** code block is a
> complete, self-contained, runnable program — the plain ` ```python ` block in
> [Minimal Example](#minimal-example). Every other Python snippet is marked
> ` ```python fragment ` and is an illustrative excerpt that will **not** run
> standalone. Verification scripts extract and execute the single plain
> `python` block verbatim; fragment blocks are never executed.

## Prerequisites

- Python 3.11 or higher
- [uv](https://docs.astral.sh/uv/) (used to build the wheel and create the venv)
- `git`

## Installation

The SDK is not published to PyPI or as a GitHub Release. The only way to get
the 0.3.0 candidate wheel is to clone the SDK repository and build it locally:

```bash
git clone <sdk-repo-url>
cd simple-harness-sdk
uv build
uv venv --seed --python 3.11 .venv
.venv/bin/pip install dist/simple_harness_sdk-0.3.0-py3-none-any.whl
```

Notes:

- `uv build` produces `dist/simple_harness_sdk-0.3.0-py3-none-any.whl`.
- If your `python3` is already ≥ 3.11, `python3 -m venv .venv` works instead of
  the `uv venv` line.
- All commands above run from the repository root; the install line uses a
  repo-relative wheel path.

### Verify Installation

```bash
.venv/bin/python -c "import simple_harness; print(simple_harness.__version__)"
```

Expected output:

```text
0.3.0
```

---

## Minimal Example

The recommended integration path is `build_consumer_runtime`: you implement
three simple Protocols (Provider, ToolExecutor, Authorization) and the SDK
adapts them to the full kernel ports.

Save this complete program as `demo.py` (outside the SDK repository, e.g. in
your own project directory):

```python
"""Minimal Simple Harness SDK 0.3.0 example (self-contained, runnable).

Exit code 0 only when the run reaches the COMPLETED terminal state.
"""

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

from simple_harness.runtime import (
    build_consumer_runtime,
    ConsumerRuntimePorts,
    RunStart,
    RunClient,
)
from simple_harness.contracts import (
    CallId,
    ExecutionSessionId,
    Message,
    MessageRole,
    RequestId,
    RunId,
)
from simple_harness.execution.uow import RunState
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    ProviderUsage,
)
from simple_harness.runtime import AuthorizationRequest, AuthorizationResult
from simple_harness.tools import ToolCall, ToolResult


# Declare the model your provider returns. The kernel only trusts reported
# usage when the response model matches this target; a mismatch records an
# unknown charge and refuses the run on a later turn.
MODEL = "demo-model"

# Closed input schema for the echo tool. Tools without a declared schema keep
# the fail-closed no-argument default (the SDK schema subset forbids
# additionalProperties).
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "additionalProperties": False,
}


# 1. Provider port — fake LLM that demonstrates one tool call.
class FakeProvider:
    def __init__(self):
        self.call_count = 0

    async def invoke(self, request: ProviderRequest, *, cancel) -> ProviderResponse:
        self.call_count += 1
        last_message = request.messages[-1]

        if self.call_count == 1 and last_message.role == MessageRole.USER:
            # First turn: ask for the echo tool.
            return ProviderResponse(
                request_id=request.request_id,
                message=Message(MessageRole.ASSISTANT, ""),
                tool_calls=(
                    ProviderToolCall(
                        CallId(f"call-{uuid.uuid4().hex[:8]}"),
                        "echo",
                        {"text": "hello"},
                    ),
                ),
                usage=ProviderUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                model=MODEL,
                finish_reason="tool_calls",
            )

        # Second turn (after the tool result): final answer.
        return ProviderResponse(
            request_id=request.request_id,
            message=Message(MessageRole.ASSISTANT, "Hello from the SDK!"),
            tool_calls=(),
            usage=ProviderUsage(input_tokens=20, output_tokens=8, total_tokens=28),
            model=MODEL,
            finish_reason="stop",
        )


# 2. Tool executor port — one echo tool.
class EchoToolExecutor:
    async def execute(self, call: ToolCall, context: dict) -> ToolResult:
        if call.name == "echo":
            text = call.arguments.get("text", "hello")
            print(f"[Tool] echo(text={text!r})")
            return ToolResult.succeeded(call.call_id, {"echoed": text})
        return ToolResult.rejected(call.call_id, "unknown_tool", f"Tool {call.name} not found")


# 3. Authorization port — always allow (demo only).
class AllowAllAuthorization:
    async def request_authorization(self, request: AuthorizationRequest) -> AuthorizationResult:
        return AuthorizationResult.allow()


async def main() -> int:
    # Fresh temp database + fresh IDs on every run: re-runnable by design.
    db_path = Path(tempfile.mkdtemp(prefix="quickstart-")) / "execution.db"
    suffix = uuid.uuid4().hex[:8]
    run_id = RunId(f"run-{suffix}")

    ports = ConsumerRuntimePorts(
        provider=FakeProvider(),
        tool_executor=EchoToolExecutor(),
        authorization=AllowAllAuthorization(),
        database_path=str(db_path),
        tool_names=("echo",),
        tool_schemas={"echo": ECHO_SCHEMA},
        model=MODEL,
    )
    runtime = await build_consumer_runtime(ports)
    await runtime.__aenter__()
    try:
        client = RunClient(runtime)
        await client.start(RunStart(
            execution_session_id=ExecutionSessionId(f"session-{suffix}"),
            run_id=run_id,
            request_id=RequestId(f"req-{suffix}"),
            turn_id="turn-001",
            tool_catalog_generation=1,
            input={
                "messages": [{"role": "user", "content": "Say hello"}],
                "capability_snapshot": {"tools": ["echo"]},
                # Without an output-token bound the provider reservation is
                # unpriceable and the run fails with react_cost_exceeded.
                "max_output_tokens": 1024,
            },
        ))

        # wait_idle() returns None — it only waits until the run is no longer
        # live. Read the real terminal state back via client.query().
        await runtime.wait_idle(run_id)
        record = client.query(run_id)
        state = record.state if record is not None else None
        print(f"Run terminal state: {state}")

        if state != RunState.COMPLETED:
            print(f"FAIL: expected {RunState.COMPLETED}, got {state}")
            return 1
        print("SUCCESS: run completed")
        return 0
    finally:
        await runtime.__aexit__(None, None, None)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

**Run it:**

```bash
.venv/bin/python demo.py
```

**Expected output** (IDs vary per run):

```text
[Tool] echo(text='hello')
Run terminal state: completed
SUCCESS: run completed
```

The process exits `0` only when the run reaches `COMPLETED`; any other
terminal state exits `1`.

---

## What Just Happened?

1. **Fake Provider**: Returns canned responses (no real LLM), demonstrating
   one tool call followed by a final answer
2. **Tool Executor**: Executes one `echo` tool
3. **Authorization**: Allows every tool call (demo only)
4. **Runtime**: Managed the durable Agent execution end to end
5. **Terminal assertion**: `client.query(run_id)` read back the real terminal
   state and the process exit code reflects it

---

## Next Steps

The snippets below are **fragments** (marked `python fragment`): they show the
shape of each API but are not runnable standalone.

### 1. Use a Real LLM Provider

Replace `FakeProvider` with a real API client:

```python fragment
class OpenAIProvider:
    async def invoke(self, request, *, cancel):
        data = await call_openai(request)  # your HTTP client here
        return ProviderResponse(
            request_id=request.request_id,
            message=Message(MessageRole.ASSISTANT, data["choices"][0]["message"]["content"]),
            tool_calls=(),
            usage=ProviderUsage(
                input_tokens=data["usage"]["prompt_tokens"],
                output_tokens=data["usage"]["completion_tokens"],
                total_tokens=data["usage"]["total_tokens"],
            ),
            model="consumer-model",  # must match the configured consumer target
            finish_reason="stop",
        )
```

**See:** [Integration Guide](integration-guide.md)

### 2. Add Real Tools

Implement file operations, API calls, etc. The
consumer adapter registers placeholder tool specs that reject all arguments,
so tools must work with an empty argument mapping (upgrade path: use the
10-Port `RuntimePorts` API, see [Ports API](api/ports.md)):

```python fragment
class FileToolExecutor:
    async def execute(self, call, context):
        if call.name == "read_file":
            content = open(call.arguments["path"]).read()
            return ToolResult.succeeded(call.call_id, {"content": content})
        return ToolResult.rejected(call.call_id, "unknown_tool", "Tool not found")
```

**See:** [Ports API](api/ports.md)

### 3. Add official Agent Memory

Memory SDK 0.4 `MemoryManager` implements Harness `AgentMemoryPort` directly. Pass it once; do
not add a public adapter or call recall/append yourself:

```python fragment
from simple_harness import ResourceOwnership
from simple_harness_memory import MemoryManager

memory = await MemoryManager.build_development("memory.db")
ports = ConsumerRuntimePorts(
    provider=my_provider,
    tool_executor=my_tools,
    authorization=my_authorization,
    database_path="execution.db",
    memory=memory,
    memory_ownership=ResourceOwnership.RUNTIME,
)
```

Use `RunClient.start_conversation()` with trusted `AgentIdentity`; Harness automatically performs
bounded recall, freezes it as USER/untrusted Context, and dispatches one terminal committed turn.

### 4. Advanced: the 10-Port RuntimePorts API

`build_consumer_runtime` covers most consumers. If you need full control
(custom tool schemas, workflows, memory, delivery sinks), drop down to the
kernel-level `build_runtime` + `RuntimePorts` (10 ports) — this is the
advanced path:

```python fragment
from simple_harness.runtime import build_runtime, RuntimePorts, RuntimeProfile

runtime = build_runtime(
    uow=uow,
    profiles={"agent.general": RuntimeProfile("agent.general", "react")},
    drivers=drivers,
    ports=RuntimePorts(...),  # 10 kernel ports
)
```

**See:** [Ports API](api/ports.md), [Integration Guide](integration-guide.md)

### 5. Run Conformance Tests

Verify your port implementations against the SDK conformance suite:

```bash
python -m simple_harness.testing \
  --host my_module:build_host \
  --suite provider,tool,runtime \
  --artifact-sha256 <wheel-sha256>
```

**See:** [Conformance Guide](conformance.md)

---

## Common Issues

### Import Error: `No module named 'simple_harness'`

**Solution**: Make sure you installed the wheel into the same environment you
run with:

```bash
.venv/bin/pip list | grep simple-harness
```

### Python Version Error

**Solution**: SDK requires Python 3.11+. Check your version:

```bash
.venv/bin/python --version
```

### Run fails with `react_cost_exceeded`

**Solution**: two common causes, both shown in the Minimal Example above:

1. `max_output_tokens` missing from the run input — the provider reservation
   is then unpriceable and recorded as an unknown charge.
2. The provider's `ProviderResponse.model` does not match the adapter target
   (`"consumer-model"`) — reported usage is then treated as an unknown charge.

### Run completes but prints `Run completed: None`

**Solution**: `runtime.wait_idle(run_id)` returns `None` by design — it only
waits until the run is no longer live. Read the terminal state via
`client.query(run_id)` (see the Minimal Example).

---

## Learn More

- **[Integration Guide](integration-guide.md)** - Complete step-by-step integration
- **[API Reference](api/)** - Detailed API documentation
  - [Runtime](api/runtime.md)
  - [Workflow](api/workflow.md)
  - [Ports](api/ports.md)
- **[Conformance Testing](conformance.md)** - Validate your implementation
- **[Examples](../examples/)** - More code examples (see `examples/minimal-consumer/`)

---

**Ready to integrate?** → Start with the [Integration Guide](integration-guide.md)
