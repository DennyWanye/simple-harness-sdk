# Simple Harness SDK

Current production architecture, persistence, and consumer boundaries are indexed in
[`ARCHITECTURE/index.md`](ARCHITECTURE/index.md).

An embeddable, durable agent runtime extracted from Simple Harness.

The current SDK exposes one official Agent Memory v1 boundary. A consumer passes one
`AgentMemoryPort`, a trusted four-part `AgentIdentity`, and its normal product ports; the
SDK automatically performs bounded recall, freezes the resulting Context, retries durable
recall release, and writes exactly one canonical user+assistant committed turn only after a
conversation completes. Terminal state, normal deliveries, and the Memory outbox row commit in
one SQLite transaction; failed or cancelled turns never create a Memory write. The dispatcher
survives restart and retries by stable turn identity without consumer-maintained glue code.

```python
from simple_harness import (
    AgentIdentity,
    ConsumerRuntimePorts,
    ConversationTurnInput,
    Message,
    MessageRole,
    RunClient,
    build_consumer_runtime,
)

ports = ConsumerRuntimePorts(
    provider=my_provider,
    tool_executor=my_tools,
    authorization=my_authorization,
    database_path="execution.db",
    memory=my_memory,  # implements AgentMemoryPort
)
runtime = await build_consumer_runtime(ports)
async with runtime:
    await RunClient(runtime).start_conversation(
        ConversationTurnInput(
            AgentIdentity("deployment-1", "household-1", "actor-1", "session-1"),
            Message(MessageRole.USER, "Hello"),
            "Hello",
        )
    )
```

Consumers do not call recall/release/write manually. Non-text attachment, tool, and reasoning
payloads are excluded unless the consumer supplies explicit `memory_text`. The old query/sink split has been
retired from the public API. Public contracts are documented in
[`docs/api/contracts.md`](docs/api/contracts.md); consumer validation is covered
by [`docs/conformance.md`](docs/conformance.md).
