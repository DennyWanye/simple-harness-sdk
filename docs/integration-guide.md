# Integration Guide

Complete step-by-step guide for integrating Simple Harness SDK into your application.

> **Recommended integration path (0.3.0):** most consumers should start with
> `build_consumer_runtime` — implement 3 Protocols (`ProviderPort`,
> `ToolExecutorPort`, `AuthorizationPort`) and the SDK adapts them to the full
> kernel. See the runnable walkthrough in [Quickstart](quickstart.md) and
> `examples/minimal-consumer/`.
>
> **This guide documents the advanced path**: the kernel-level 10-Port
> `RuntimePorts` API (`build_runtime`), needed only when you require custom
> tool schemas, workflows, memory ports, or delivery sinks. Code excerpts here
> are illustrative fragments, not runnable programs; the runnable example
> lives in the Quickstart.

## Overview

This guide walks you through:
1. Setting up the SDK
2. Implementing required Ports
3. Integrating official Workflows
4. Adding Memory support
5. Running conformance tests
6. Common troubleshooting

**Estimated time:** 2-4 days for basic integration, 1-2 weeks for production-ready.

---

## Prerequisites

- Python 3.11 or higher
- Basic understanding of async/await patterns
- SQLite database for persistence
- Access to an LLM API (OpenAI, Anthropic, etc.)

---

## Architecture Overview

```
Your Application
    │
    ├─ Ports Implementation (Your Code)
    │   ├─ ProviderPort → LLM API client
    │   ├─ ToolExecutorPort → Your tools
    │   ├─ AuthorizationPort → Permission system
    │   └─ Memory/Workflow Ports → Optional
    │
    └─ SDK Runtime (Wheel Package)
        ├─ RunKernel → Execution engine
        ├─ Drivers → ReAct + Workflow
        └─ Recovery → Crash recovery
```

**Key principle:** SDK provides the engine, you provide the capabilities.

---

## Phase 1: Basic Runtime Setup

### Step 1: Install SDK

The SDK is not published to PyPI or as a GitHub Release. Clone the SDK
repository, build the 0.3.0 candidate wheel locally, and install it (identical to the
[Quickstart](quickstart.md) installation — that document is the single source
of truth for install commands):

```bash
git clone <sdk-repo-url>
cd simple-harness-sdk
uv build
uv venv --seed --python 3.11 .venv
.venv/bin/pip install dist/simple_harness_sdk-0.3.0-py3-none-any.whl
```

### Step 2: Implement ProviderPort

Create a provider adapter for your LLM:

```python
# providers/my_provider.py

import httpx
from simple_harness.providers import (
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    ProviderToolCall,
    ProviderTransportError,
    Secret,
)

class MyLLMProvider:
    """Adapter for your LLM API."""
    
    def __init__(self, api_key: Secret, base_url: str):
        self.api_key = api_key
        self.base_url = base_url
    
    async def invoke(self, request: ProviderRequest, *, cancel):
        """Execute a single LLM request."""
        
        # Build request payload
        payload = {
            "model": request.model,
            "messages": [
                {
                    "role": msg.role.value,
                    "content": msg.content,
                }
                for msg in request.messages
            ],
            "temperature": request.temperature or 0.7,
            "max_tokens": request.max_tokens or 2048,
        }
        
        # Add tool definitions if present
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        
        # Call LLM API
        headers = {
            "Authorization": f"Bearer {self.api_key.reveal()}",
            "Content-Type": "application/json",
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=60.0,
                )
                response.raise_for_status()
        except httpx.NetworkError as e:
            raise ProviderTransportError("network_error", f"Network error: {e}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                raise ProviderTransportError("server_error", "LLM server error")
            else:
                raise ProviderTransportError("client_error", f"HTTP {e.response.status_code}")
        
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        
        # Parse tool calls
        tool_calls = []
        if "tool_calls" in message:
            for tc in message["tool_calls"]:
                tool_calls.append(ProviderToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                ))
        
        return ProviderResponse(
            request_id=request.request_id,
            content=message.get("content"),
            tool_calls=tuple(tool_calls),
            usage=ProviderUsage(
                prompt_tokens=data["usage"]["prompt_tokens"],
                completion_tokens=data["usage"]["completion_tokens"],
            ),
            finish_reason=choice["finish_reason"],
        )
```

**Key points:**
- Handle network errors correctly (transport vs server vs protocol)
- Never log API keys or raw request/response bodies
- Support tool calling (structured output)

### Step 3: Implement ToolExecutorPort

Create a tool registry and executor:

```python
# tools/my_tools.py

from simple_harness.tools import (
    ToolRegistry,
    ToolSpec,
    ToolCall,
    ToolResult,
    ToolOutcome,
)

class MyToolExecutor:
    """Execute tools in your application context."""
    
    def __init__(self):
        self.registry = ToolRegistry()
        self._register_tools()
    
    def _register_tools(self):
        """Register available tools."""
        
        # Example: File read tool
        self.registry.register(ToolSpec(
            name="read_file",
            description="Read contents of a file",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to read",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ))
        
        # Example: Web search tool
        self.registry.register(ToolSpec(
            name="web_search",
            description="Search the web for information",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ))
    
    async def execute(self, call: ToolCall, context: dict):
        """Execute a tool call."""
        
        # Route to appropriate handler
        if call.name == "read_file":
            return await self._read_file(call)
        elif call.name == "web_search":
            return await self._web_search(call)
        else:
            return ToolResult.rejected(
                call.call_id,
                "unknown_tool",
                f"Tool {call.name} not found",
            )
    
    async def _read_file(self, call: ToolCall):
        """Handle read_file tool."""
        try:
            path = call.arguments["path"]
            
            # Security: Validate path is within allowed directories
            if not self._is_safe_path(path):
                return ToolResult.rejected(
                    call.call_id,
                    "forbidden_path",
                    "Path access denied",
                )
            
            # Read file
            with open(path, 'r') as f:
                content = f.read()
            
            return ToolResult.succeeded(
                call.call_id,
                {"content": content, "path": path},
            )
        
        except FileNotFoundError:
            return ToolResult.failed(
                call.call_id,
                "file_not_found",
                "File does not exist",
            )
        except PermissionError:
            return ToolResult.failed(
                call.call_id,
                "permission_denied",
                "Cannot read file",
            )
        except Exception as e:
            # Unknown state - side effect may have happened
            return ToolResult.unknown(
                call.call_id,
                f"Unexpected error: {type(e).__name__}",
            )
    
    async def _web_search(self, call: ToolCall):
        """Handle web_search tool."""
        query = call.arguments["query"]
        limit = call.arguments.get("limit", 5)
        
        # Call your search API
        results = await self.search_api.search(query, limit=limit)
        
        return ToolResult.succeeded(
            call.call_id,
            {
                "results": [
                    {"title": r.title, "url": r.url, "snippet": r.snippet}
                    for r in results
                ],
            },
        )
    
    def _is_safe_path(self, path: str) -> bool:
        """Validate file path is within allowed directories."""
        # Implement your security policy
        allowed_dirs = ["/workspace", "/tmp"]
        return any(path.startswith(d) for d in allowed_dirs)
```

**Key points:**
- Validate all arguments before execution
- Return correct outcome types (succeeded/failed/rejected/unknown)
- `unknown` means "side effect might have happened" (for recovery)
- Don't expose raw exceptions to the Agent

### Step 4: Implement AuthorizationPort

```python
# authorization/my_auth.py

from simple_harness.tools import (
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationDecision,
)

class MyAuthorization:
    """Handle authorization requests."""
    
    def __init__(self, ui_bridge):
        self.ui_bridge = ui_bridge
    
    async def request_authorization(
        self,
        request: AuthorizationRequest,
    ):
        """Request user approval for a tool call."""
        
        # Check if tool requires approval
        if not self._requires_approval(request.tool_call.name):
            return AuthorizationResult.allow()
        
        # Show UI prompt to user
        decision = await self.ui_bridge.show_permission_dialog(
            tool_name=request.tool_call.name,
            arguments=request.tool_call.arguments,
            run_id=request.run_id,
            risk_level=request.risk_level,
        )
        
        if decision == "allow":
            return AuthorizationResult.allow()
        elif decision == "deny":
            return AuthorizationResult.deny("User denied permission")
        else:
            # User didn't respond or deferred
            return AuthorizationResult.defer("User did not respond")
    
    def _requires_approval(self, tool_name: str) -> bool:
        """Determine if tool needs user approval."""
        high_risk_tools = [
            "delete_file",
            "run_shell",
            "send_email",
            "make_payment",
        ]
        return tool_name in high_risk_tools
```

### Step 5: Build Runtime

```python
# runtime/setup.py

from simple_harness.runtime import build_runtime, RuntimePorts, SqliteContextPort
from simple_harness.execution.sqlite import Database
from simple_harness.providers import Secret

from providers.my_provider import MyLLMProvider
from tools.my_tools import MyToolExecutor
from authorization.my_auth import MyAuthorization

async def create_runtime():
    """Build and initialize runtime."""
    
    # Setup persistence
    db = Database.open("/path/to/execution.db")
    context = SqliteContextPort(db)
    
    # Setup ports
    ports = RuntimePorts(
        provider=MyLLMProvider(
            api_key=Secret(os.environ["LLM_API_KEY"]),
            base_url="https://api.openai.com/v1",
        ),
        tool_executor=MyToolExecutor(),
        authorization=MyAuthorization(ui_bridge),
        context=context,
    )
    
    # Build runtime
    runtime = await build_runtime(ports).__aenter__()
    
    return runtime, db
```

### Step 6: Start Your First Run

```python
# main.py

import asyncio
from simple_harness.runtime import RunStart
from simple_harness.contracts import ExecutionSessionId, RunId, RequestId
from simple_harness.execution.uow import RunState

async def main():
    runtime, db = await create_runtime()
    
    try:
        # Create run
        run_start = RunStart(
            execution_session_id=ExecutionSessionId("session-1"),
            run_id=RunId("run-1"),
            request_id=RequestId("req-1"),
            turn_id="turn-1",
            input={
                "messages": [
                    {"role": "user", "content": "List files in /workspace"}
                ],
                "capability_snapshot": {
                    "tools": ["read_file", "list_directory"],
                },
            },
            created_at_unix_ms=int(time.time() * 1000),
        )
        
        # Start run
        await runtime.start(run_start)
        
        # Wait for completion
        state = await runtime.wait_idle(RunId("run-1"))
        
        print(f"Run completed: {state}")
        
        if state == RunState.COMPLETED:
            # Fetch results from database
            run = runtime.read_run("run-1")
            print(f"Result: {run.terminal_payload}")
    
    finally:
        await runtime.__aexit__(None, None, None)
        db.close()

asyncio.run(main())
```

**Checkpoint:** At this point, you should have a working basic Runtime that can execute simple Agent conversations.

---

## Phase 2: Workflow Integration

### Step 1: Enable Official Workflows

```python
from simple_harness.workflows import build_official_workflow_registrations

ports = RuntimePorts(
    provider=my_provider,
    tool_executor=my_tools,
    authorization=my_auth,
    context=my_context,
    
    # Add official workflows
    workflow_registrations=build_official_workflow_registrations(),
)
```

### Step 2: Implement DurableTaskHostServices

Required for `workflow.durable_task`:

```python
# workflows/durable_task_host.py

from simple_harness.workflow import DurableTaskHostServices

class MyDurableTaskHost:
    """Host services for durable_task workflow."""
    
    async def propose(self, state):
        """Generate execution plan for task."""
        request = state["request"]
        
        # Call LLM to create plan
        plan = await self.llm.generate_plan(request)
        
        return {"plan": plan, "steps": plan["steps"]}
    
    async def propose_for_execution(self, state, *, execution_identity):
        """Refine plan for execution."""
        plan = state["plan"]
        
        # Add execution context
        return {
            "executable_plan": plan,
            "execution_id": execution_identity["run_id"],
        }
    
    async def execute_tools(self, calls, **kwargs):
        """Execute tool calls in plan."""
        results = {}
        
        for call in calls:
            tool_call = ToolCall(
                call_id=call["call_id"],
                name=call["name"],
                arguments=call["arguments"],
            )
            result = await self.tool_executor.execute(tool_call, {})
            results[call["call_id"]] = result.to_dict()
        
        return {"results": results}
    
    async def check_completion_evidence(self, state, outcome):
        """Verify task is complete."""
        # Check if output meets requirements
        return outcome.get("status") == "completed"
    
    async def completion_decision(self, decision, state):
        """Finalize completion."""
        return decision
    
    async def run_tests(self, state):
        """Run tests on task output."""
        # Run your test suite
        return {"passed": True, "evidence_refs": ["test-log.txt"]}
    
    async def audit(self, audit, state):
        """Audit task completion."""
        # Verify correctness
        return {**audit, "passed": True}
```

### Step 3: Register Workflow Services

```python
ports = RuntimePorts(
    ...
    workflow_services={
        "durable_task": MyDurableTaskHost(),
        "personal_v1": MyPersonalWorkflowHost(),
        # capability_build is optional
    },
)
```

---

## Phase 3: Agent Memory Integration

Install Memory SDK 0.5 with its Harness extra, build one `MemoryManager`, and pass it directly to
the official consumer builder. The SDK owns recall, release, frozen Context, terminal committed
turns, retry, and restart recovery.

```python
from simple_harness import ConsumerRuntimePorts, ResourceOwnership, build_consumer_runtime
from simple_harness_memory import MemoryManager

memory = await MemoryManager.build_development("memory.db")
runtime = await build_consumer_runtime(ConsumerRuntimePorts(
    provider=my_provider,
    tool_executor=my_tools,
    authorization=my_authorization,
    database_path="execution.db",
    memory=memory,
    memory_ownership=ResourceOwnership.RUNTIME,
))
```

Use a separate `ConversationContextProviderPort` only for product-owned persona, history, skills,
or tool hints. It must not inject Memory authority. Start typed turns with trusted
`AgentIdentity` through `RunClient.start_conversation()`; do not call recall or append manually.

---

## Phase 4: Conformance Testing

### Run Conformance Suite

```bash
python -m simple_harness.testing \
  --host my_app.sdk_adapter:build_host \
  --suite provider,tool,runtime,workflow \
  --artifact-sha256 48048ffbb827df15ae27efad67fa78d31302c9869381cb175d0d908c5f204e2f \
  --json conformance-report.json
```

### Implement ConformanceHost

```python
# tests/conformance_adapter.py

from simple_harness.testing import ConformanceHost, ConformanceHostMetadata

def build_host():
    """Factory for conformance testing."""
    
    class MyConformanceHost:
        def metadata(self):
            return ConformanceHostMetadata(
                name="MyApp",
                version="1.0.0",
                platform="linux",
            )
        
        def open_suite(self, name):
            # Return suite-specific context
            return MySuiteContext(name)
    
    return MyConformanceHost()
```

### Fix Failing Tests

Review conformance report and fix failures:

```json
{
  "passed": false,
  "failed": [
    {
      "case_id": "provider.redaction",
      "message": "API key leaked in error message"
    }
  ]
}
```

---

## Common Troubleshooting

### Issue: Import Error

**Symptom:** `ModuleNotFoundError: No module named 'simple_harness'`

**Solution:**
```bash
.venv/bin/pip install dist/simple_harness_sdk-0.3.0-py3-none-any.whl
.venv/bin/pip list | grep simple-harness
```

### Issue: Database Locked

**Symptom:** `sqlite3.OperationalError: database is locked`

**Solution:** Ensure only one Runtime instance opens the database:
```python
async with build_runtime(ports) as runtime:
    # Runtime owns the database
    pass
# Database is released here
```

### Issue: Tool Not Found

**Symptom:** Agent says "Tool X not available"

**Solution:** Verify tool is registered AND included in capability_snapshot:
```python
# In RunStart
input={
    "capability_snapshot": {
        "tools": ["read_file", "web_search"],  # Must match registered names
    },
}
```

### Issue: Memory Leak

**Symptom:** Memory usage grows over time

**Solution:** Close Runtime properly:
```python
try:
    runtime = await build_runtime(ports).__aenter__()
    # Use runtime
finally:
    await runtime.__aexit__(None, None, None)
    db.close()
```

---

## Next Steps

1. **Review API documentation:** [Runtime](api/runtime.md), [Workflow](api/workflow.md), [Ports](api/ports.md)
2. **Run conformance tests:** Validate your implementation
3. **Implement custom workflows:** Add your own workflows
4. **Production hardening:** Error handling, logging, monitoring
5. **Performance tuning:** Optimize database, memory usage

---

## See Also

- [Quickstart](quickstart.md) - Minimal example
- [API Reference](api/) - Detailed API docs
- [Conformance](conformance.md) - Testing guide
- [AIPhone Handoff](consumers/aiphone-handoff.md) - Mobile integration example
