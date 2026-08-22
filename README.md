# Simple Harness SDK

Current production architecture, persistence, and consumer boundaries are indexed in
[`ARCHITECTURE/index.md`](ARCHITECTURE/index.md).

An embeddable, durable agent runtime extracted from Simple Harness.

The current SDK exposes one official Agent Memory v1 boundary. A consumer passes one
`AgentMemoryPort`—Memory SDK 0.4 `MemoryManager` implements it directly—a trusted four-part
`AgentIdentity`, and its normal product ports; the
SDK automatically performs bounded recall, freezes the resulting Context, retries durable
recall release, and writes exactly one canonical user+assistant committed turn only after a
conversation completes. Terminal state, normal deliveries, and the Memory outbox row commit in
one SQLite transaction; failed or cancelled turns never create a Memory write. The dispatcher
survives restart and retries by stable turn identity without consumer-maintained glue code.

The normal database loader never guesses across persistence versions. Operators upgrading an
exact execution schema v3 file use the explicit backup-first
`simple_harness.execution.sqlite.migrate_execution_v3_to_v4` maintenance API while the runtime
is closed. It returns a digest-verified neutral manifest, supports mapping legacy user/session
pairs to renamed complete `AgentIdentity` values, and leaves a caller-selected v3 backup beside
the database.

```python
from simple_harness_memory import MemoryManager

from simple_harness import (
    AgentIdentity,
    ConsumerRuntimePorts,
    ConversationTurnInput,
    Message,
    MessageRole,
    ResourceOwnership,
    RunClient,
    build_consumer_runtime,
)

memory = await MemoryManager.build_development("memory.db")
ports = ConsumerRuntimePorts(
    provider=my_provider,
    tool_executor=my_tools,
    authorization=my_authorization,
    database_path="execution.db",
    memory=memory,
    memory_ownership=ResourceOwnership.RUNTIME,
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

Integration status for this candidate is deliberately narrow: `simple_harness` is the only
consumer scheduled for real product/UI validation in this program and is not claimed integrated
until that cutover completes. AIPhone and K6/AgentOS are interface-ready future consumers; their
repositories and production paths have not been modified or tested here.
