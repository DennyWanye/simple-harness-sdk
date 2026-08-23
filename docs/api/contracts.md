# Public contracts

The contract surface is deliberately small, immutable, JSON-safe, and
independent from every consumer product. All names documented here are also
exported from `simple_harness`; consumers do not need deep imports.

## Strict JSON

- `JsonPrimitive`, `JsonValue`, and `FrozenJsonValue` describe supported wire
  values and immutable in-process snapshots.
- `validate_json_value(value)` rejects non-string object keys, tuples, bytes,
  arbitrary objects, and non-finite numbers.
- `canonical_json(value)` emits sorted compact UTF-8 JSON.
- `freeze_json(value)` validates and recursively snapshots dict/list input;
  `thaw_json(value)` returns detached plain dict/list output.

JSON validation never calls an object's conversion hooks. A host must transform
domain objects explicitly before crossing an SDK boundary.

## Correlation identity

`ExecutionSessionId`, `RunId`, `RequestId`, `CallId`, `EffectId`, and `EventId`
are distinct frozen value types. They intentionally do not compare equal across
types even when the contained text matches. Each accepts 1–255 printable ASCII
characters and serializes through `str(value)` or `value.to_json()`.

`ExecutionSessionId` means the SDK's durable execution-isolation identity; it
is not the product's chat/message Session ID.

`CorrelationIds` always binds an execution session, Run, and request. `call_id`
is optional; an `effect_id` is valid only with a `call_id`.

## Messages

`Message` is a frozen, slotted dataclass with a `MessageRole` of `system`,
`user`, `assistant`, or `tool`. Tool messages require a typed `CallId`; other
roles cannot carry one. `metadata` is copied and recursively frozen at
construction. `to_dict()` returns a detached JSON object.

## Events

`EventEnvelope` is a frozen, slotted typed event containing `EventId`,
`EventKind`, `CorrelationIds`, a positive sequence, a finite non-negative Unix
timestamp, and a recursively frozen JSON payload. Schema version 1 is explicit
in its wire representation.

The foundation `EventKind` values cover Run lifecycle, Provider invocation,
Tool Effect, child signal, and delivery. Later runtime slices may add new enum
members as part of the documented 0.x public API change process.

## Errors

`HarnessError` carries exactly three public fields: lowercase stable `code`,
safe `public_message`, and `retryable`. `private_cause` is available only to the
host's private diagnostic boundary and is excluded from `str`, `repr`, and
`to_dict()`.

`ContractValidationError` is the non-retryable subtype used by deterministic
validation. `ErrorCode` provides the foundation codes, while constructors may
accept a validated lowercase snake-case extension code for later modules.

## Public API compatibility

`tests/unit/contracts/public-api.json` is the reviewed API snapshot. An export
change requires an intentional snapshot change, documentation update, and
SemVer review; consumers must not import private implementation helpers.

## Agent Memory v1

`AgentMemoryPort` is the only public Memory protocol. It has exactly three async methods:
`recall_for_turn(request)`, `release_recall(request)`, and
`record_committed_turn(request)`. Requests and results are frozen, slotted values with
canonical SHA-256 identities.

`AgentIdentity` binds `deployment_id`, `household_id`, `actor_id`, and `session_id`.
`MemoryScopeRef` supports `personal(actor_id)` and `family(household_id)` recall scopes;
automatic committed-turn writes target only the trusted actor's personal scope.

Consumers compose Memory once through `ConsumerRuntimePorts(memory=...)` or
`ProductionRuntimeConfig(memory=...)`, then enter conversations through
`RunClient.start_conversation()` and `RunClient.signal_conversation()`. The SDK owns recall,
USER/untrusted Context injection, replay reuse, and release retry; the committed-turn values
define the delivery contract wired by the following durable-outbox slice.
`ResourceOwnership.BORROWED` leaves the object open; `RUNTIME` closes it exactly once.
Memory SDK 0.5 `MemoryManager` implements `AgentMemoryPort` directly; consumers do not add an
adapter or invoke its recall/record methods themselves. `memory=None` preserves ordinary durable
execution without creating Memory stages or outbox rows.

`ConversationContextProviderPort` is a separate read-only source for product-owned persona,
history, skills, and tool hints. It cannot supply or forge the Memory partition. Providers
return the same durable `source_snapshot_ref` they received.

`ConversationTurnInput.context_source_snapshot_ref` binds the root Context snapshot.
`ConversationContinuationInput.context_source_snapshot_ref` independently binds each
continuation; it never inherits the root reference. If either input omits the field, the SDK
derives a deterministic content-addressed reference from that turn's current message and stores
the effective reference in the preparation claim before calling the provider. Reusing a
continuation ID with a different reference or payload fails with a stable conflict.

Recall timeout, transient failure, or invalid output produces a durable empty Memory stage;
provider invocation continues without leaking exception text, paths, or Memory payload into
the public error state.

### Execution v3-to-v4 maintenance contract

The normal SQLite loader remains fail-closed for old schemas. With the runtime fully closed,
operators may call `migrate_execution_v3_to_v4` from `simple_harness.execution.sqlite`, supplying
a new same-directory backup path and a complete `LegacyIdentityMap`. Each legacy
`(user_id, session_id)` maps uniquely to one complete target `AgentIdentity`; target actor and
session IDs may be renamed, and target session IDs must also be unique.

The returned `ExecutionMigrationManifest` uses protocol
`simple-harness/execution-migration-manifest/v1`. Each `MigrationManifestEntry` identifies the
legacy source event, payload hash, Run, causal terminal/continuation/claim facts, optional
canonical committed turn, and exactly one `LegacyDisposition`: `KEEP_COMPLETED_PAIR`,
`SUPPRESS_TENTATIVE`, `SUPPRESS_TERMINAL`, or `DEFERRED_TURN`. `to_json()` emits a digest;
`from_json()` validates protocol, schema pair, field types, and that digest. This neutral
manifest is a coordinator/audit handoff; a Memory implementation does not import the Harness
migrator.

The explicit operation verifies an exact v3 descriptor and source integrity, creates and hashes
the backup before transformation, builds a fresh v4 database, verifies counts/FKs/integrity, and
atomically replaces the source. A failure after replacement restores the exact backup. Legacy
terminal ambiguity is never resolved by timestamps: missing or multiple event/receipt/claim
candidates fail closed.

This candidate makes only an interface-readiness statement for future consumers. `simple_harness`
is the sole product designated for the later real integration/UI gate. AIPhone and K6/AgentOS
have not been integrated or tested and require no repository change to prove this SDK contract.

### Migration from 0.2 query/sink ports

| Retired public shape | Agent Memory v1 replacement |
| --- | --- |
| `ConversationMemoryQueryPort` + `ConversationMemorySinkPort` | one `AgentMemoryPort` |
| `MemoryQueryPort` / `MemoryWritePort` | `AgentMemoryPort` |
| consumer calls prepare/recall helpers before `start()` | `RunClient.start_conversation()` |
| separate query/sink close ownership | one explicit `ResourceOwnership` |
| `user_id` + `session_id` | `AgentIdentity` |
