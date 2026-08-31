# Runtime API Reference

## Overview

The Runtime is the core execution engine that manages Agent runs, tracks state, handles recovery, and coordinates Tools and Workflows. This document covers the public API for building and managing Runtime instances.

## Core Concepts

- **Runtime**: The main execution coordinator that manages one or more concurrent Runs
- **Run**: A single Agent execution session with its own state, budget, and lifecycle
- **RuntimePorts**: The dependency injection container providing external capabilities
- **Profile**: A named configuration that binds a Driver (ReAct, Workflow) to specific behavior

---

## Building a Runtime

### `build_runtime(ports: RuntimePorts) -> Runtime`

Creates a new Runtime instance with the provided ports.

```python
from simple_harness.runtime import build_runtime, RuntimePorts

# Provide your implementations
ports = RuntimePorts(
    provider=my_provider,
    tool_executor=my_tool_executor,
    authorization=my_authorization,
    context=my_context,
    # ... other ports
)

async with build_runtime(ports) as runtime:
    # Runtime is ready
    await runtime.start(run_start)
```

**Parameters:**
- `ports` (RuntimePorts): Container with all required and optional ports

**Returns:**
- `Runtime`: An async context manager. Use `async with` to ensure proper cleanup.

**Lifecycle:**
- `__aenter__`: Initializes the runtime, loads state, runs startup reconciliation
- `__aexit__`: Closes all runs, flushes state, releases resources

---

## RuntimePorts

The `RuntimePorts` dataclass specifies all dependencies the Runtime needs.

### Required Ports

```python
@dataclass
class RuntimePorts:
    provider: ProviderPort              # LLM provider (e.g., OpenAI adapter)
    tool_executor: ToolExecutorPort     # Tool execution and registry
    authorization: AuthorizationPort    # Permission/HITL handler
    context: ContextPort                # Execution context (database/transaction)
```

### Optional Ports

```python
    # Official Agent Memory v1 integration (optional)
    agent_memory: AgentMemoryPort | None = None
    
    # Workflow integration (optional if not using Workflows)
    workflow_services: dict[str, WorkflowHostServices] | None = None
    
    # Runtime behavior customization
    reconciliation: RuntimeReconciliationPort | None = None
    tool_catalog_generation: ToolCatalogGenerationPort | None = None
```

**See also:** `docs/api/ports.md` for detailed Port interface definitions.

---

## Durable conversation context preparation (0.3.0)

The official 0.3 path passes one `AgentMemoryPort` as `memory=` to `ConsumerRuntimePorts` or
`ProductionRuntimeConfig` and enters through `RunClient.start_conversation()` /
`signal_conversation()`. The SDK invokes bounded recall, validates result identity/hash/count,
freezes Memory as USER/untrusted Context, durably releases the result, and writes only the final
committed user+assistant Turn. Memory SDK 0.5 `MemoryManager` implements this protocol directly.
The older consumer-prepared helpers and their query/sink DTOs remain private migration code only.
They are absent from both package public surfaces and are unsupported for product composition;
consumers do not call or maintain recall/release/write lifecycle themselves.

Each `ConversationContinuationInput` may provide its own
`context_source_snapshot_ref`. The SDK never reuses the root reference for a continuation; when
the field is `None`, it derives a deterministic reference from only that continuation's current
message. The effective reference is durable before provider invocation and remains fixed across
crash recovery and replay.

### Host-issued memory action authority

Changes to an existing stored memory use the strict public
`MemoryActionAuthority` protocol. The Host issues an authority for exactly one
`REVISE`, `SUPERSEDE`, or `SUPPRESS` intent. The intent binds the subject, exact
target memory ID and revision, canonical evidence references and span hashes,
run and turn, mutation plan and operation IDs, the authority-free whole-plan
intent hash, canonical operation index, and the operation-intent hash.
The authority also carries its issuer, nonce, issue and expiry times, and a
domain-separated hash.

`MemoryMutationOperation` carries only a `MemoryActionAuthorityRef`; a complete
authority included in model output is not trusted. The Memory consumer resolves
the reference exactly once through the Host-owned `MemoryActionAuthorityPort`,
strictly verifies every binding, and atomically consumes `replay_identity` in
the same transaction that commits the mutation. The port owns durable lookup,
not the transaction-local replay fence. An idempotent replay is permitted only
when it resolves to the same already-committed receipt.

The whole-plan and operation-intent hashes exclude every authority reference,
preventing a hash cycle while the final plan hash still commits to the
reference. The whole-plan commitment contains every canonical operation and
all plan metadata, so inserting or changing another operation invalidates the
old authority. Protected
actions may target only an `ExistingMemoryTarget`; same-plan
`CreatedByOperationTarget` chains are rejected. `CONTEST` never accepts action
authority, must propose `CONTESTED`, and cannot propose any cognitive memory
type's destructive terminal lifecycle. It remains a conservative conflict
candidate: because the Harness proposal does not carry trusted current target
state, the Memory consumer must prove that its payload and lifecycle exactly
match the target and that only the conflict flag changes, then apply its
deterministic same-slot/different-value conflict rules.

`MemoryMutationApplyResult` reports one of `COMMITTED`,
`NEEDS_USER_CONFIRMATION`, or `REJECTED`. A missing authority for an otherwise
valid protected operation is represented by typed confirmation items, not by a
recall outcome or an exception. A `COMMITTED` result is invalid unless every
protected existing-memory operation carries an action-authority reference.
Schema-3 mutation wires and legacy authority
wires are rejected rather than migrated implicitly.

---

## Starting a Run

### `RunStart`

Defines the initial state for a new Run.

```python
from simple_harness.runtime import RunStart
from simple_harness.contracts import ExecutionSessionId, RunId, RequestId

run_start = RunStart(
    execution_session_id=ExecutionSessionId("session-abc"),
    run_id=RunId("run-123"),
    request_id=RequestId("request-1"),
    turn_id="turn-001",
    input={
        "messages": [
            {"role": "user", "content": "Hello, what can you do?"}
        ],
        "capability_snapshot": {
            "tools": ["read_file", "write_file"],
        },
    },
    created_at_unix_ms=1735000000000,
)

await runtime.start(run_start)
```

**Fields:**
- `execution_session_id` (ExecutionSessionId): Durable session identity (persisted across restarts)
- `run_id` (RunId): Unique Run identity
- `request_id` (RequestId): Request correlation ID
- `turn_id` (str): Turn identifier (for logging/tracing)
- `input` (dict): Run input payload, typically containing:
  - `messages`: List of conversation messages
  - `capability_snapshot`: Available tools/workflows
- `created_at_unix_ms` (int): Creation timestamp (Unix milliseconds)

---

## Runtime Methods

### `start(run_start: RunStart) -> None`

Starts a new Run. The Run executes asynchronously in the background.

```python
await runtime.start(run_start)
# Run is now executing
```

### `wait_idle(run_id: RunId, timeout_seconds: float | None = None) -> RunState`

Waits for a Run to reach a terminal state (completed, failed, cancelled).

```python
from simple_harness.execution.uow import RunState

state = await runtime.wait_idle(RunId("run-123"), timeout_seconds=30.0)

if state == RunState.COMPLETED:
    print("Run succeeded")
elif state == RunState.FAILED:
    print("Run failed")
```

**Parameters:**
- `run_id`: The Run to wait for
- `timeout_seconds`: Optional timeout (None = wait indefinitely)

**Returns:**
- `RunState`: Terminal state (COMPLETED, FAILED, CANCELLED, or UNKNOWN if timeout)

### `cancel(run_id: RunId) -> None`

Requests cancellation of a running Run.

```python
await runtime.cancel(RunId("run-123"))
# Run will stop at next safe checkpoint
```

---

## RunClient

Lower-level interface for advanced Run control (used internally by Drivers).

```python
client: RunClient = runtime.client

# Start with more control
await client.start(run_start)

# Send authorization decisions (for HITL)
await client.decide_authorization(
    run_id=RunId("run-123"),
    decision_id="decision-xyz",
    nonce="nonce-abc",
    expected_version=1,
    decision=AuthorizationDecision.ALLOW,
)

# Signal child runs
await client.signal_child(
    run_id=RunId("parent-run"),
    child_run_id=RunId("child-run"),
    signal_payload={...},
)
```

**Most consumers use `runtime.start()` and `runtime.wait_idle()` instead of the low-level client.**

---

## Error Handling

Runtime methods may raise:

- `HarnessError`: Base error type with `code`, `public_message`, `retryable`
- `ContractValidationError`: Input validation failed (non-retryable)

```python
from simple_harness.contracts import HarnessError

try:
    await runtime.start(run_start)
except HarnessError as e:
    print(f"Error: {e.code} - {e.public_message}")
    if e.retryable:
        # Can retry
        pass
```

**Common error codes:**
- `invalid_input`: RunStart validation failed
- `run_already_exists`: Run ID collision
- `database_error`: Persistence failure (may be retryable)

---

## Lifecycle States

```python
from simple_harness.runtime import RuntimeLifecycleState

# Check runtime state
if runtime.state == RuntimeLifecycleState.READY:
    await runtime.start(run_start)
```

**States:**
- `INITIALIZING`: Runtime is starting up
- `READY`: Ready to accept runs
- `CLOSING`: Shutdown in progress
- `CLOSED`: Runtime has been closed

---

## Budget and Termination

The Runtime enforces hard limits via `TerminationLimits`:

```python
from simple_harness.runtime.termination import TerminationLimits

# Configure in your profile or context
limits = TerminationLimits(
    max_turns=10,                    # Maximum Agent turns
    max_tool_calls=20,               # Maximum tool invocations
    max_wall_seconds=300.0,          # 5 minute timeout
    max_cost_micros=1_000_000,       # $1.00 maximum cost
    max_consecutive_same_tool=3,     # Prevent infinite loops
)
```

When a limit is exceeded, the Run enters `RunState.FAILED` with a termination error.

---

## Crash Recovery

The Runtime automatically recovers interrupted Runs on startup:

1. **Startup Reconciliation**: On `build_runtime()`, scans for incomplete runs
2. **Idempotent Recovery**: Already-completed effects are not replayed
3. **Unknown State**: Effects in "started" state are marked as `unknown` and require manual reconciliation

```python
# Recovery happens automatically
async with build_runtime(ports) as runtime:
    # If a previous process crashed, incomplete runs are reconciled here
    pass
```

**See:** `docs/conformance.md` for `runtime.restart_without_replay` test case.

---

## Example: Complete Flow

```python
import asyncio
from simple_harness.runtime import build_runtime, RuntimePorts, RunStart
from simple_harness.contracts import ExecutionSessionId, RunId, RequestId

async def main():
    ports = RuntimePorts(
        provider=MyProvider(),
        tool_executor=MyToolExecutor(),
        authorization=MyAuthorization(),
        context=MyContext(),
    )
    
    async with build_runtime(ports) as runtime:
        run_start = RunStart(
            execution_session_id=ExecutionSessionId("session-1"),
            run_id=RunId("run-1"),
            request_id=RequestId("req-1"),
            turn_id="turn-1",
            input={
                "messages": [{"role": "user", "content": "Hello"}],
                "capability_snapshot": {"tools": []},
            },
            created_at_unix_ms=1735000000000,
        )
        
        await runtime.start(run_start)
        state = await runtime.wait_idle(RunId("run-1"))
        
        print(f"Run completed with state: {state}")

asyncio.run(main())
```

---

## See Also

- [Ports API](ports.md) - Detailed Port interface specifications
- [Workflow API](workflow.md) - Workflow integration
- [Contracts](contracts.md) - Core data types
- [Conformance](../conformance.md) - Runtime test cases
