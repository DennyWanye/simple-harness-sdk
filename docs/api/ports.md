# Ports API Reference

## Overview

**Ports** are the dependency injection interfaces that allow the SDK to call into your application. By implementing these ports, you provide the SDK with access to:

- LLM providers (OpenAI, Anthropic, etc.)
- Tool implementations (file operations, API calls, etc.)
- Authorization and permissions
- Workspace and artifact management
- Memory systems
- Workflow-specific services

This document lists all port interfaces and their requirements.

---

## Port Categories

| Category | Required | Purpose |
|----------|----------|---------|
| **Core Ports** | ✅ Yes | Basic runtime operation |
| **Memory Ports** | Optional | Long-term and working memory |
| **Workflow Ports** | Optional | Enable official workflows |
| **Advanced Ports** | Optional | Customization and extensions |

---

## Core Ports (Required)

> **Which API level do you need?** Most consumers only need to implement
> **3 Protocols** — `ProviderPort`, `ToolExecutorPort`, and
> `AuthorizationPort` — and build the runtime via `build_consumer_runtime`
> (see [Quickstart](../quickstart.md)). The full 10-Port `RuntimePorts` API
> documented below is the **advanced** path, for consumers who need custom
> tool schemas, workflows, memory, or delivery sinks.

### ProviderPort

**Purpose**: Execute LLM requests (e.g., OpenAI, Anthropic).

> **Model echo contract (0.1.3)**: the `ProviderResponse.model` your provider
> returns must match the `model` you declare on `ConsumerRuntimePorts`. The
> kernel only trusts reported usage when the two match; a mismatch records an
> unknown charge and refuses the run on a later turn.

```python
from simple_harness.providers import ProviderRequest, ProviderResponse, CancelToken

class ProviderPort(Protocol):
    async def invoke(
        self,
        request: ProviderRequest,
        *,
        cancel: CancelToken,
    ) -> ProviderResponse:
        """Execute a single LLM request.
        
        Args:
            request: Contains messages, model, temperature, tools, etc.
            cancel: Cancellation token (check cancel.is_cancelled())
        
        Returns:
            ProviderResponse with content, tool_calls, usage, etc.
        
        Raises:
            ProviderTransportError: Network/connection error (retryable)
            ProviderServerError: API server error (may be retryable)
            ProviderProtocolError: Invalid response format (not retryable)
        """
        ...
```

**Example implementation:**

```python
import httpx
from simple_harness.providers import (
    ProviderResponse,
    ProviderUsage,
    ProviderToolCall,
)

class OpenAIProvider:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url
    
    async def invoke(self, request, *, cancel):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        payload = {
            "model": request.model,
            "messages": [m.to_dict() for m in request.messages],
            "temperature": request.temperature,
        }
        
        if request.tools:
            payload["tools"] = [t.to_openai_format() for t in request.tools]
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=60.0,
            )
            response.raise_for_status()
        
        data = response.json()
        choice = data["choices"][0]
        
        # Parse tool calls if present
        tool_calls = []
        if choice["message"].get("tool_calls"):
            for tc in choice["message"]["tool_calls"]:
                tool_calls.append(ProviderToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                ))
        
        return ProviderResponse(
            request_id=request.request_id,
            content=choice["message"].get("content"),
            tool_calls=tool_calls,
            usage=ProviderUsage(
                prompt_tokens=data["usage"]["prompt_tokens"],
                completion_tokens=data["usage"]["completion_tokens"],
            ),
            finish_reason=choice["finish_reason"],
        )
```

**Key requirements:**
- Must handle tool calling (structured output)
- Must classify errors correctly (transport/server/protocol)
- Must never log or persist secrets (API keys, raw requests/responses)
- Should respect `cancel` token for long requests

---

### ToolExecutorPort

**Purpose**: Execute tool calls requested by the Agent.

```python
from simple_harness.tools import ToolRegistry, ToolCall, ToolResult

class ToolExecutorPort(Protocol):
    @property
    def registry(self) -> ToolRegistry:
        """Return the tool registry with all available tools."""
        ...
    
    async def execute(
        self,
        call: ToolCall,
        context: dict,
    ) -> ToolResult:
        """Execute a tool call.
        
        Args:
            call: Tool name, arguments, call_id
            context: Execution context (run_id, authorization, etc.)
        
        Returns:
            ToolResult (succeeded/partial/rejected/failed/unknown)
        """
        ...
```

**Example implementation:**

```python
from simple_harness.tools import ToolRegistry, ToolSpec, ToolResult, ToolOutcome

class MyToolExecutor:
    def __init__(self):
        self.registry = ToolRegistry()
        
        # Register tools
        self.registry.register(ToolSpec(
            name="read_file",
            description="Read a file from disk",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ))
    
    async def execute(self, call, context):
        tool_spec = self.registry.get(call.name)
        
        if call.name == "read_file":
            try:
                path = call.arguments["path"]
                content = open(path).read()
                return ToolResult.succeeded(call.call_id, {"content": content})
            except FileNotFoundError:
                return ToolResult.failed(
                    call.call_id,
                    error_code="file_not_found",
                    error_message="File does not exist",
                )
            except Exception as e:
                return ToolResult.unknown(call.call_id, f"Unexpected error: {e}")
        
        return ToolResult.rejected(call.call_id, "not_implemented", "Tool not implemented")
```

**Key requirements:**
- Must validate arguments against `input_schema` before execution
- Must return correct outcome (succeeded/partial/rejected/failed/unknown)
- Must not expose raw exceptions or sensitive data in error messages
- `unknown` outcome means "side effect may have happened" (for recovery)

---

### AuthorizationPort

**Purpose**: Handle human-in-the-loop authorization requests.

```python
from simple_harness.tools import AuthorizationRequest, AuthorizationResult

class AuthorizationPort(Protocol):
    async def request_authorization(
        self,
        request: AuthorizationRequest,
    ) -> AuthorizationResult:
        """Request human authorization for a tool call.
        
        Args:
            request: Contains tool_call, run_id, risk_level, etc.
        
        Returns:
            AuthorizationResult (allow/deny/defer)
        """
        ...
```

**Example implementation:**

```python
class UIAuthorization:
    async def request_authorization(self, request):
        # Show UI prompt to user
        user_decision = await self.show_permission_dialog(
            tool_name=request.tool_call.name,
            arguments=request.tool_call.arguments,
            risk_level=request.risk_level,
        )
        
        if user_decision == "allow":
            return AuthorizationResult.allow()
        elif user_decision == "deny":
            return AuthorizationResult.deny("User denied permission")
        else:
            return AuthorizationResult.defer("User did not respond")
```

---

### ContextPort

**Purpose**: Provide execution context (database, transactions).

```python
from simple_harness.runtime import SqliteContextPort

class ContextPort(Protocol):
    async def open_transaction(self) -> ExecutionTransaction:
        """Open a new database transaction."""
        ...
```

**Most consumers use `SqliteContextPort` (provided by SDK):**

```python
from simple_harness.runtime import SqliteContextPort
from simple_harness.execution.sqlite import Database

db = Database.open("/path/to/execution.sqlite")
context = SqliteContextPort(db)

ports = RuntimePorts(
    ...
    context=context,
)
```

---

## Memory Ports (Optional)

### ConversationMemoryQueryPort (0.2.0 production)

This is the durable conversation-recall authority used by
`ProductionRuntimeConfig.conversation_query`:

```python
class ConversationMemoryQueryPort(Protocol):
    async def recall_bounded(
        self, query: ConversationMemoryRecallQuery
    ) -> ConversationMemoryRecallResult: ...

    async def release(
        self, *, user_id: str, context_query_id: str, result_hash: str
    ) -> None: ...

    async def close(self) -> None: ...
```

`recall_bounded` is wrapped by the SDK's finite overall timeout. The SDK independently
checks query identity/hash, canonical result hash/byte count, item structure/count, and
the caller's item/byte limits before staging. `release` must be idempotent: preparation
durably stages first, then makes a bounded release call; replay may repeat that same call
without repeating the logical release side effect. Production composition rejects a query
port that omits any of `recall_bounded`, `release`, or `close`.

`MemoryQueryPort` below is the legacy reserved interface; it is not the 0.2.0 conversation
authority.

### MemoryQueryPort

**Purpose**: Read-only access to long-term memory.

```python
from simple_harness.runtime import MemoryQueryPort

class MemoryQueryPort(Protocol):
    async def recall_readonly(
        self,
        query: str,
        limit: int,
        scope: str,
    ) -> list[dict[str, JsonValue]]:
        """Return relevant memory entries.
        
        Args:
            query: Natural language query
            limit: Maximum results
            scope: Memory scope (e.g., "user:123")
        
        Returns:
            List of memory entries (JSON-safe dicts)
        """
        ...
```

**Example:**

```python
class MyMemoryQuery:
    def __init__(self, db):
        self.db = db
    
    async def recall_readonly(self, query, limit, scope):
        # Vector search in your memory database
        results = await self.db.vector_search(
            query=query,
            limit=limit,
            scope=scope,
        )
        
        return [
            {
                "content": r.text,
                "timestamp": r.created_at,
                "relevance": r.score,
            }
            for r in results
        ]
```

---

### MemoryWritePort

**Purpose**: Write session-scoped working memory (todos, notes).

```python
from simple_harness.runtime import MemoryWritePort

class MemoryWritePort(Protocol):
    async def replace_session_todos(
        self,
        session_id: str,
        items: list[dict[str, JsonValue]],
    ) -> None:
        """Replace the full todo list for a session.
        
        Args:
            session_id: Execution session ID
            items: New todo list
        """
        ...
```

**Example:**

```python
class MyMemoryWrite:
    def __init__(self, db):
        self.db = db
    
    async def replace_session_todos(self, session_id, items):
        # Atomic replace
        async with self.db.transaction() as tx:
            await tx.execute(
                "DELETE FROM todos WHERE session_id = ?",
                (session_id,)
            )
            for item in items:
                await tx.execute(
                    "INSERT INTO todos (session_id, content) VALUES (?, ?)",
                    (session_id, item.get("content", ""))
                )
```

---

## Workflow Ports (Optional)

These ports are only needed if you want to use the official workflows.

### DurableTaskHostServices

**Required for**: `workflow.durable_task`

```python
class DurableTaskHostServices(Protocol):
    async def propose(self, state: dict) -> dict:
        """Generate a task execution plan."""
        ...
    
    async def propose_for_execution(
        self,
        state: dict,
        *,
        execution_identity: dict,
    ) -> dict:
        """Generate executable plan with execution context."""
        ...
    
    async def execute_tools(
        self,
        calls: list[dict],
        **kwargs,
    ) -> dict:
        """Execute tool calls in the plan."""
        ...
    
    async def check_completion_evidence(
        self,
        state: dict,
        outcome: dict,
    ) -> bool:
        """Verify task completion."""
        ...
    
    async def completion_decision(
        self,
        decision: str,
        state: dict,
    ) -> str:
        """Finalize completion decision."""
        ...
    
    async def run_tests(self, state: dict) -> dict:
        """Run tests on task output."""
        ...
    
    async def audit(self, audit: dict, state: dict) -> dict:
        """Audit task for correctness."""
        ...
```

---

### PersonalWorkflowHostServices

**Required for**: `workflow.personal_v1`

```python
class PersonalWorkflowHostServices(Protocol):
    async def execute(self, **values) -> dict:
        """Execute a personal workflow node.
        
        Args:
            **values: Node-specific parameters
        
        Returns:
            Node output as dict
        """
        ...
```

---

### CapabilityBuildHostServices

**Required for**: `workflow.capability_build`

```python
class CapabilityBuildHostServices(Protocol):
    async def search(self, **values) -> dict:
        """Search for existing capabilities."""
        ...
    
    async def authorize_source(self, **values) -> dict:
        """Check if capability source is trusted."""
        ...
    
    async def build(self, **values) -> dict:
        """Build capability in isolated environment."""
        ...
    
    async def store(self, **values) -> dict:
        """Store built capability package."""
        ...
    
    async def activate(self, **values) -> dict:
        """Activate installed capability."""
        ...
    
    async def authorize_build(self, **values) -> dict:
        """Authorize build operation."""
        ...
```

---

## Advanced Ports (Optional)

### RuntimeReconciliationPort

**Purpose**: Custom reconciliation logic for `unknown` effects.

```python
class RuntimeReconciliationPort(Protocol):
    async def reconcile_unknown_effect(
        self,
        effect_id: str,
        tool_call: dict,
    ) -> ReconciliationOutcome:
        """Determine final state of an unknown effect.
        
        Returns:
            SUCCEEDED | FAILED | STILL_UNKNOWN
        """
        ...
```

---

### ToolCatalogGenerationPort

**Purpose**: Customize tool catalog versioning.

```python
class ToolCatalogGenerationPort(Protocol):
    def compute_generation(self, specs: list[ToolSpec]) -> int:
        """Compute catalog generation number."""
        ...
```

---

## Port Integration Checklist

When integrating the SDK, implement ports in this order:

### Phase 1: Minimal Runtime
- [ ] `ProviderPort` - LLM provider
- [ ] `ToolExecutorPort` - At least 1 tool
- [ ] `AuthorizationPort` - Even if always-allow
- [ ] `ContextPort` - Use `SqliteContextPort`

**Result**: Can run basic Agent conversations

### Phase 2: Durable Task
- [ ] `DurableTaskHostServices` - All methods
- [ ] Register `workflow.durable_task` profile

**Result**: Can run multi-step tasks with recovery

### Phase 3: Personal Workflows
- [ ] `PersonalWorkflowHostServices`
- [ ] Personal workflow catalog (define & store user workflows)
- [ ] Register `workflow.personal_v1` profile

**Result**: Users can save and run custom workflows

### Phase 4: Memory Integration
- [ ] `MemoryQueryPort` - Read memory
- [ ] `MemoryWritePort` - Write working memory
- [ ] Pass to `RuntimePorts`

**Result**: Agent has long-term memory

### Phase 5: Capability Build (Optional)
- [ ] `CapabilityBuildHostServices`
- [ ] Isolated build environment
- [ ] Package store
- [ ] Register `workflow.capability_build` profile

**Result**: Agent can install new tools on-demand

---

## See Also

- [Runtime API](runtime.md) - How to build Runtime with ports
- [Workflow API](workflow.md) - Workflow-specific ports
- [Integration Guide](../integration-guide.md) - Step-by-step port implementation
- [Conformance](../conformance.md) - Port testing
