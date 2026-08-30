# Simple Harness SDK

Current production architecture, persistence, and consumer boundaries are indexed in
[`ARCHITECTURE/index.md`](ARCHITECTURE/index.md).
The operator procedure for locally building, freezing, verifying, and distributing SDK releases
is [`docs/build-and-release.md`](docs/build-and-release.md).

An embeddable, durable agent runtime extracted from Simple Harness.

The current SDK exposes one official Agent Memory v1 boundary. A consumer passes one
`AgentMemoryPort`—Memory SDK 0.5 `MemoryManager` implements it directly—a trusted four-part
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
payloads are excluded unless the consumer supplies explicit `memory_text`. The old query/sink
split, manual preparation helpers, and their adapter-facing DTOs have been retired from both
public package surfaces. Public contracts are documented in
[`docs/api/contracts.md`](docs/api/contracts.md); consumer validation is covered
by [`docs/conformance.md`](docs/conformance.md).

Integration status is deliberately narrow: `simple_harness` has completed exact-wheel product
cutover, automated regression, and real macOS UI validation with a configured DeepSeek provider.
AIPhone, K6/AgentOS, and NovelTagSystem remain interface-ready future consumers; their repositories
and production paths were not modified, integrated, or tested in this program.

Version 0.7.0 is the Human Memory protocol candidate: it replaces unconditional pre-Provider
recall with an explicit same-Run Context route barrier and Host-issued Context authority. Version
0.6.4 preserves nested authorization metadata when a user-confirmation nonce is reissued;
its public APIs and execution schema v6 are unchanged. Version 0.6.3 added the typed
`HostControlAuthorityV1` / `HostControlRunStartV1` contract and
`RunClient.start_host_control`. Host control roots retain normal admission, durable Run ownership,
driver execution, recovery, and terminal settlement while deliberately skipping conversation
Memory preparation. The mode and complete authority are frozen in the v6 start snapshot; ordinary
and Host-control identities cannot be reused across modes.

Version 0.6.2 includes the provider-neutral Runtime capability catalog and same-Run progressive Tool
exposure. Search, describe, and activation change visibility only; actual Tool execution retains
the existing authorization and durable Effect authority. Fresh databases use execution schema v6;
an exact v5 database requires the explicit backup-first offline v5-to-v6 migrator. Release source,
artifact hashes, and the consuming Host pin are verified separately before publication. The 0.6.0
prepublish artifact was rejected before release because the public ReAct builder did not expose the
Host resolver seam; 0.6.2 includes that seam plus morphology-safe discovery and privacy-safe handler diagnostics.
