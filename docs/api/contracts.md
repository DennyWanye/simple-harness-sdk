# Public contracts

The v0.1 contract surface is deliberately small, immutable, JSON-safe, and
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

