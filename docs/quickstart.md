# Quick Start Guide

Get started with Simple Harness SDK in 10 minutes.

## Prerequisites

- Python 3.11 or higher
- `pip` package manager

## Installation

### 1. Install the SDK

```bash
pip install simple_harness_sdk-0.1.1-py3-none-any.whl
```

Or if installing from a local wheel file:

```bash
pip install /path/to/simple_harness_sdk-0.1.1-py3-none-any.whl
```

### 2. Verify Installation

```bash
python -m simple_harness.testing --help
```

You should see the conformance testing CLI help message.

---

## Minimal Example

Here's a complete working example that runs an Agent with a fake Provider and one Tool.

**Create `demo.py`:**

```python
"""Minimal Simple Harness SDK example."""

import asyncio
from simple_harness import (
    build_runtime,
    RuntimePorts,
    RunStart,
    ExecutionSessionId,
    RunId,
    RequestId,
    SqliteContextPort,
    Message,
)
from simple_harness.execution.sqlite import Database
from simple_harness.execution.uow import RunState
from simple_harness.providers import ProviderRequest, ProviderResponse, ProviderUsage
from simple_harness.tools import ToolRegistry, ToolSpec, ToolCall, ToolResult


# 1. Fake Provider (returns canned responses)
class FakeProvider:
    async def invoke(self, request, *, cancel):
        # Simple canned response
        return ProviderResponse(
            request_id=request.request_id,
            content="Hello! I'm a fake Agent. I can echo text.",
            tool_calls=[],
            usage=ProviderUsage(prompt_tokens=10, completion_tokens=15),
            finish_reason="stop",
        )


# 2. Simple Tool Executor
class SimpleToolExecutor:
    def __init__(self):
        self.registry = ToolRegistry()
        
        # Register an echo tool
        self.registry.register(ToolSpec(
            name="echo",
            description="Echo back the input text",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        ))
    
    async def execute(self, call, context):
        if call.name == "echo":
            text = call.arguments.get("text", "")
            return ToolResult.succeeded(call.call_id, {"output": text})
        
        return ToolResult.rejected(
            call.call_id,
            "unknown_tool",
            f"Tool {call.name} not found",
        )


# 3. Simple Authorization (always allow)
class AllowAllAuthorization:
    async def request_authorization(self, request):
        from simple_harness.tools import AuthorizationResult
        return AuthorizationResult.allow()


# 4. Main function
async def main():
    # Create SQLite database for execution state
    db = Database.open("demo-execution.db")
    context = SqliteContextPort(db)
    
    # Build runtime with ports
    ports = RuntimePorts(
        provider=FakeProvider(),
        tool_executor=SimpleToolExecutor(),
        authorization=AllowAllAuthorization(),
        context=context,
    )
    
    async with build_runtime(ports) as runtime:
        # Create a run
        run_start = RunStart(
            execution_session_id=ExecutionSessionId("demo-session"),
            run_id=RunId("demo-run"),
            request_id=RequestId("demo-request"),
            turn_id="turn-1",
            input={
                "messages": [
                    {"role": "user", "content": "Hello, what can you do?"}
                ],
                "capability_snapshot": {
                    "tools": ["echo"],
                },
            },
            created_at_unix_ms=1735000000000,
        )
        
        # Start the run
        print("Starting run...")
        await runtime.start(run_start)
        
        # Wait for completion
        state = await runtime.wait_idle(RunId("demo-run"))
        
        print(f"✓ Run completed with state: {state}")
        
        if state == RunState.COMPLETED:
            print("Success! Agent responded.")
        elif state == RunState.FAILED:
            print("Run failed.")
        else:
            print(f"Run ended with state: {state}")
    
    # Cleanup
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
```

**Run it:**

```bash
python demo.py
```

**Expected output:**

```
Starting run...
✓ Run completed with state: RunState.COMPLETED
Success! Agent responded.
```

---

## What Just Happened?

1. **Fake Provider**: Returns canned responses (no real LLM)
2. **Tool Executor**: Registered one `echo` tool
3. **Runtime**: Managed the Agent execution
4. **Run**: A single conversation turn completed successfully

---

## Next Steps

### 1. Use a Real LLM Provider

Replace `FakeProvider` with a real OpenAI adapter:

```python
from simple_harness.providers import OpenAICompatibleProvider, Secret

provider = OpenAICompatibleProvider(
    base_url="https://api.openai.com/v1",
    model="gpt-4",
    api_key=Secret("your-api-key-here"),
)
```

**See:** [Integration Guide](integration-guide.md#provider-implementation)

### 2. Add Real Tools

Implement file operations, API calls, etc.:

```python
# In your execute() method
if call.name == "read_file":
    path = call.arguments["path"]
    content = open(path).read()
    return ToolResult.succeeded(call.call_id, {"content": content})
```

**See:** [Ports API](api/ports.md#toolexecutorport)

### 3. Enable Workflows

Register official workflows for multi-step tasks:

```python
from simple_harness.workflows import build_official_workflow_registrations

ports = RuntimePorts(
    ...
    workflow_registrations=build_official_workflow_registrations(),
)
```

**See:** [Workflow API](api/workflow.md)

### 4. Add Memory

Integrate long-term memory:

```python
ports = RuntimePorts(
    ...
    memory_query=MyMemoryQuery(),
    memory_write=MyMemoryWrite(),
)
```

**See:** [Integration Guide](integration-guide.md#memory-integration)

### 5. Run Conformance Tests

Verify your implementation:

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

**Solution**: Make sure you installed the wheel correctly:

```bash
pip install simple_harness_sdk-0.1.1-py3-none-any.whl
pip list | grep simple-harness
```

### Python Version Error

**Solution**: SDK requires Python 3.11+. Check your version:

```bash
python --version
# Should show Python 3.11.0 or higher
```

If you have multiple Python versions:

```bash
python3.11 demo.py
```

### Database Locked Error

**Solution**: Only one Runtime can open a database at a time. Make sure previous runs closed properly:

```python
async with build_runtime(ports) as runtime:
    # Runtime auto-closes on exit
    ...

# Or manually:
db.close()
```

---

## Learn More

- **[Integration Guide](integration-guide.md)** - Complete step-by-step integration
- **[API Reference](api/)** - Detailed API documentation
  - [Runtime](api/runtime.md)
  - [Workflow](api/workflow.md)
  - [Ports](api/ports.md)
- **[Conformance Testing](conformance.md)** - Validate your implementation
- **[Examples](../examples/)** - More code examples

---

## Getting Help

- Check the [Integration Guide](integration-guide.md) for detailed implementation steps
- Review [Conformance test cases](conformance.md) to understand expected behavior
- Refer to [API documentation](api/) for interface details

---

**Ready to integrate?** → Start with the [Integration Guide](integration-guide.md)
