# Simple Harness SDK

Current production architecture, persistence, and consumer boundaries are indexed in
[`ARCHITECTURE/index.md`](ARCHITECTURE/index.md).
The operator procedure for locally building, freezing, verifying, and distributing SDK releases
is [`docs/build-and-release.md`](docs/build-and-release.md).

An embeddable, durable agent runtime extracted from Simple Harness.

Version 0.7.0 is the current Human Memory protocol source candidate. A Host supplies the exact
Context snapshot for every new Provider turn through `RunContextAuthorityPort`; the SDK validates
and durably freezes that snapshot before Provider reservation. Recall is explicit and same-Run:
the main model proposes a strict route/recall operation, while deterministic SDK and Host code
validate receipts and effect authority. A no-recall turn performs no Memory ranking call.

The legacy `AgentMemoryPort.record_committed_turn` terminal outbox remains available during the
cross-repository cutover, but the production kernel no longer performs automatic
`recall_for_turn`/`release_recall`. Provider persistence is public-only, route-required effects
cannot cross the same-batch barrier, and project effects require a Host-issued
`TaskExecutionEnvelope`. Workspace binding append authority is a separate strict public chain:
the model may submit a mode-free exact-root proposal, while Manual decisions and Auto mode
snapshots must be verified against Host-durable records before the Host returns a typed grant.
The append receipt carries the sorted unique root identity set and binds its canonical digest,
exact parent receipt, grant, and new revision; genesis is fixed to the canonical empty-set digest.
`ContextRouteReceipt` v2 and effect envelopes then freeze that exact binding-set receipt identity
and hash. The decoder accepts v1 only for legacy no-authority standalone routes; project v1 fails
closed. Generic Tool
authorization receipts and `RunContextSnapshot.metadata` are not workspace authority. Memory SDK
evidence/state storage, Host TaskScope archives, dynamic
Context assembly, and the single-conversation UI are later release units rather than 0.7.0
product claims.

Main-model analysis uses two deliberately separate receipt layers. The Host executor returns a
`MemoryAnalysisResultEnvelope` with a `MemoryAnalysisDeliveryReceipt`, and consumers verify that
exact provider delivery through `MemoryAnalysisDeliveryAuthorityPort`; public hashes alone are not
authority. The existing `MemoryAnalysisReceipt` remains the later Memory validator/application
record and cannot stand in for Host delivery proof.

The normal database loader never guesses across persistence versions. Operators upgrading an
exact execution schema v3 file use the explicit backup-first
`simple_harness.execution.sqlite.migrate_execution_v3_to_v4` maintenance API while the runtime
is closed. It returns a digest-verified neutral manifest, supports mapping legacy user/session
pairs to renamed complete `AgentIdentity` values, and leaves a caller-selected v3 backup beside
the database.

The following is the retained 0.6.x terminal committed-turn compatibility composition; it is not
the new 0.7.0 recall path:

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

Consumers do not call the retired automatic recall/release lifecycle. Non-text attachment, tool,
private provider metadata, and hidden reasoning payloads are excluded from durable public Context.
The old query/sink split and adapter-facing Memory DTOs remain retired from both public package
surfaces. Public contracts are documented in
[`docs/api/contracts.md`](docs/api/contracts.md); consumer validation is covered
by [`docs/conformance.md`](docs/conformance.md).

Integration status is deliberately narrow: `simple_harness` has completed exact-wheel product
cutover, automated regression, and real macOS UI validation with a configured DeepSeek provider.
AIPhone, K6/AgentOS, and NovelTagSystem remain interface-ready future consumers; their repositories
and production paths were not modified, integrated, or tested in this program.

Version 0.7.0 is the Human Memory protocol candidate: it replaces unconditional pre-Provider
recall with an explicit same-Run Context route barrier and Host-issued Context authority. It also
adds typed, domain-separated Manual/Auto workspace-binding proposals, challenges, receipts,
Host-verified grants and append-only binding-set lineage; the SDK defines these contracts but does
not implement Host filesystem membership or binding persistence. Version
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
