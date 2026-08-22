<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

## 0.3.0 — candidate

### Breaking
- Execution persistence now uses fresh schema v4. The normal loader rejects schema v1-v3;
  schema v3 requires the explicit backup-first offline migrator and a complete identity map.
- The public query/sink and reserved query/write Memory ports are retired. Consumers pass one
  `AgentMemoryPort` implementation, such as Memory SDK 0.4 `MemoryManager`, to the official
  consumer or production builder; no public Memory adapter or manual recall/write lifecycle
  remains.

### Added
- Official `AgentMemoryPort` v1 contract with trusted four-part identity, personal/family
  scopes, canonical recall/release/committed-turn DTOs, stable errors, failure policy, and
  explicit borrowed/runtime ownership.
- `ConversationContextProviderPort` for product-owned non-Memory Context and automatic
  `RunClient.start_conversation()` / continuation preparation.
- `ConversationContinuationInput.context_source_snapshot_ref` lets each continuation bind its
  own product Context snapshot. When omitted, the SDK derives a deterministic content-addressed
  reference from that continuation's current message.
- Fresh execution schema v4 with immutable Agent identity bindings, richer Context staging,
  durable recall-release retry, and a terminal-only canonical committed-turn outbox.
- Lease/epoch-fenced committed-turn dispatch with restart replay, bounded backlog cleanup,
  transient retry, permanent/conflict dead-letter, and privacy-safe `REJECTED_ERASED` settlement.
- Backup-first explicit execution v3-to-v4 offline migration with a digest-verified neutral
  manifest, deterministic four-way legacy event classification, target identity remapping,
  and a versioned cursor for post-migration continuation supersession.
- A packaged PEP 561 `py.typed` marker so strict type checking follows the public Agent Memory
  and execution migration contracts from an installed wheel.

### Changed
- `build_consumer_runtime` is the official easy composition root and accepts one Memory
  instance; recall failures degrade to a frozen empty partition and replay does not recall
  again.
- Old query/sink and reserved query/write ports are retired from public exports. Consumers
  migrate to one `AgentMemoryPort` and no longer call preparation helpers manually.
- Conversation start and continuation enqueue no longer create tentative Memory writes. A
  completed root or continuation commits its user+assistant pair atomically with terminal facts;
  failed/cancelled turns produce no outbox row and replay rejects missing, added, or changed turns.
- Context preparation persists the effective root or continuation snapshot reference in the
  durable claim before invoking the product provider. Continuation replay reuses that exact
  reference; changing either the reference or payload for an existing continuation ID conflicts.
- Existing execution schema v1-v3 databases remain fail-closed in the normal loader. A closed,
  exact-v3 database can be upgraded only through the explicit offline migrator with a complete
  legacy identity map and a caller-selected same-directory backup path.

## 0.2.0 — candidate

**Focus:** Durable conversation Memory integration without replacing the 0.1.5
structured-message, tool-catalog, Provider-budget, or projection authorities.

### Added
- Typed conversation turn/continuation/output DTOs and bounded recall/apply Ports.
- Fresh execution schema v3 with immutable user/session ownership, durable private context
  staging, and a transactionally coupled conversation Memory outbox.
- Four atomic root/continuation commands, lease-based Memory dispatcher recovery, and
  SDK- or consumer-prepared context modes whose private snapshots replay byte-for-byte.
- Strict production composition that requires all authorities and owns projection, Memory,
  and SQLite lifecycle resources.

### Changed
- StartSnapshot schema is v5; schemas v1–v4 remain readable. New conversation fields are
  additive and generic runs remain supported when conversation Memory is disabled.
- Existing execution schema v1/v2 files now fail closed and require a fresh v3 storage set.
- CI builds one authoritative candidate and tests the exact wheel on Python 3.11–3.13.
  Release publication is manual and uploads the tested bytes without rebuilding.

## 0.1.5 — candidate

**Focus:** Durable per-Run context authority for SDK-first product integration.

### Added
- Typed structured message content (`ContentBlock` / `MessageContent`) with canonical
  persistence, StartSnapshot/ReAct recovery, and OpenAI-compatible serialization. Structured
  content is never coerced through `str(list)`.
- Nullable Provider usage dimensions for cached and reasoning tokens.
- Atomic per-Run Provider resolution via `ProviderBindingResolver`, binding the physical
  Provider, optional frozen estimator, budget policy, and restart-checkable fingerprint.
- Immutable, content-addressed tool-catalog generations persisted in SQLite and resolved by
  exact generation/fingerprint across WAITING and process restart.
- A transactionally coupled Provider settlement projection outbox with stable cursor reads.
- Optional `deskpet_public_progress` normalization; missing, blank, or wrong-type metadata is
  stripped without blocking business tool arguments.

### Changed
- SQLite schema version is 2. Existing 0.1.4 databases migrate in place.
- StartSnapshot schema version is 4 and remains backward-readable for schema versions 1–3.

### Integration boundary
- Product code remains responsible for enforcing the 8 MiB per-content-block and 16 MiB
  aggregate-per-Run ingress limits before constructing SDK messages.
- Catalog generations are retained indefinitely in 0.1.5; a future GC may delete them only
  after every referencing Run is terminal.

## 0.1.4 — candidate

**Focus:** Release-blocking hardening of the consumer facade and the release/CI pipeline.

### Fixed
- Delivery no longer fabricates `DELIVERED`: the no-op `_DefaultDeliverySink` was removed
  from the production namespace. `build_consumer_runtime` now accepts an optional
  `delivery_sinks` mapping; when omitted, no sink is registered and deliveries stay PENDING
  (fail-closed). `DeliveryDispatcher` now permits an empty sink set. A test-only
  `NoopDeliverySink` lives in `simple_harness.testing`.
- Tool calls now pass a real execution context (`run_id` / `request_id` / `call_id`) to
  `ToolExecutorPort.execute` instead of an empty dict.
- `build_consumer_runtime` is documented as a demo/basic facade; production consumers
  should assemble `RuntimePorts` directly (the facade uses a zero-cost price estimator and
  no-op reconciliation).
- The Database opened by `build_consumer_runtime` is now closed on Runtime shutdown via a
  `close_hook` (registered by the facade, not by the generic `Runtime.close()`), so a
  consumer-built runtime no longer leaks its SQLite connection.
- Added a driver-failure terminalization regression test: a raising driver durable-
  terminalizes the run to FAILED, the failure log carries `run_id` via `extra` (no
  secondary logging `TypeError`), and the public payload never exposes `private_cause`.

### Changed
- `MemoryQueryPort` / `MemoryWritePort` are marked `reserved` (declared but not yet wired
  into the Runtime); consumers must not assume recall or working memory is active.
- Release/CI hygiene: `ci.yml` now runs the full pytest suite plus scoped ruff/mypy;
  `release.yml` gates publish on a same-file `test` job (full pytest + conformance) since
  `needs` cannot reference a separate manual workflow; hardcoded version literals were
  removed from `release-candidate-conformance.yml` and `verify_release_gate.sh` in favour
  of the single `src/simple_harness/version.py` source.

### Backward compatibility
- `build_consumer_runtime`'s new `delivery_sinks` argument is optional; 0.1.3 consumers
  build and run unchanged. `Runtime`/`build_runtime` gain an optional `close_hook` (default
  `None`, no behaviour change).

## 0.1.3 — candidate

### Observability (post-release, 2026-08-19)

- Added structured stdlib `logging` events on the SDK's core execution paths so hosts
  can observe the engine: `run.start` / `run.complete` / `run.fail` / `run.cancelled` /
  `run.admission_denied` (kernel), `provider.invoked` / `provider.usage_untrusted` /
  `provider.charge_unknown` / `reconcile.unknown_settled` (dispatch), `tool.invoked` /
  `tool.authorized` / `tool.denied` / `tool.effect_settled` (executor), and
  `budget.refused_on_unknown` / `budget.exceeded` (budget).
- Events follow a `<module>.<action>` name with structured `extra` fields; tool
  arguments are logged as keys only and never as values.
- Added a regression suite (`tests/unit/runtime/test_logging_observability.py`) that
  locks the observability contract via caplog behaviour tests, AST existence checks,
  and redaction assertions.

**Focus:** Fix two consumer-layer design defects.

### Fixed
- `ConsumerRuntimePorts` now accepts `model` (default `"consumer-model"`); the consumer
  provider adapter uses it as `ProviderTarget.model` instead of the hardcoded constant.
  This lets real consumers whose `ProviderPort` echoes a real model name have their usage
  trusted, instead of always landing in `BudgetCharge.unknown()` and refusing multi-turn runs.
- `ConsumerRuntimePorts` now accepts `tool_schemas` (a name → closed input schema mapping);
  tools with a declared schema accept their arguments. Tools without a schema keep the
  fail-closed no-argument default (the SDK JSON-Schema subset forbids `additionalProperties`).

### Backward compatibility
- New fields are appended after existing fields and carry defaults, so 0.1.2 consumers build
  and run unchanged.

### Semantics note
- When usage is trusted, it is recorded as `TRUSTED_USAGE` at the consumer price estimator,
  which is currently a frozen zero-price estimator — trusted usage therefore books at zero
  cost. This is intentional for the consumer facade and not a pricing path.

## 0.1.2 — candidate

**Focus:** Ease of integration for external projects.

### Post-release fixes (2026-08-19, docs/examples only — no SDK code changes)

- Fixed `examples/minimal-consumer/`: the demo previously printed
  `Run completed: None` (it printed `wait_idle()`'s `None` return), always
  exited 0, and could not be re-run (hardcoded IDs + persistent
  `execution.db`). The demo now reads the real terminal state via
  `client.query(run_id)`, exits 0 only on `COMPLETED`, and uses fresh
  run/session IDs plus a temporary database per invocation.
- Fixed the example's mock provider to the real 0.1.2 provider contract
  (`Message`/`CallId`/`ProviderUsage(input/output/total_tokens)`), and
  documented three integration gotchas discovered while repairing it:
  1. `RunStart.input` must set `max_output_tokens`, otherwise the provider
     reservation is unpriceable and the run fails with `react_cost_exceeded`.
  2. The consumer adapter pins `ProviderTarget(model="consumer-model")`;
     a provider response whose `model` does not match gets its usage recorded
     as an unknown charge, also tripping `react_cost_exceeded`.
  3. The consumer adapter registers placeholder tool specs
     (`additionalProperties: false`, no properties), so tool calls with
     non-empty argument mappings fail schema validation; consumer-level tools
     effectively cannot take arguments in 0.1.2 (use the 10-Port
     `RuntimePorts` API for real schemas).
- Rewrote `docs/quickstart.md` for the real 0.1.2 API: installation section
  now states the only acquisition path (clone repository + `uv build` +
  `pip install dist/simple_harness_sdk-0.1.2-py3-none-any.whl`), and exactly
  one self-contained runnable ```python block (all other snippets marked
  `python fragment`).
- Promoted `build_consumer_runtime` as the recommended integration path in
  `docs/integration-guide.md` and `docs/api/ports.md`; the full 10-Port
  `RuntimePorts` API is now labeled advanced usage.
- Added `examples/minimal-consumer/verify_from_zero.sh`: clean-clone gate that
  extracts build/install commands and the runnable example verbatim from
  `docs/quickstart.md`, executes them, and runs the demo twice (structured
  PASS/FAIL, exit-code gated).
- Added `examples/minimal-consumer/conformance_host.py`: a consumer-level host
  for the SDK conformance protocol covering the `provider` and `tool` suites
  (exercises the provider/tool contracts directly, so it is unaffected by the
  two 0.1.2 consumer-adapter limitations above).
- Added `scripts/verify_release_gate.sh`: one-shot release gate that installs
  the `dist/` 0.1.2 wheel into a clean venv, runs `minimal-consumer`, and runs
  `python -m simple_harness.testing --suite provider,tool` against the
  conformance host (structured PASS/FAIL, exit-code gated).
- Recorded 0.1.2 provenance in `dist/BUILD_INFO.txt` and `dist/SHA256SUMS`
  (wheel built locally at commit `cb1f245`, before `896b685`'s observability
  fix; wheel SHA-256
  `387c8d1d97c0f89e4664347fb57ca6a43a0e7fa772b07a0f34c6f3a6e86efd4c`).

### Documentation
- Added comprehensive Integration Guide (`docs/integration-guide.md`) with step-by-step Port implementation examples
- Added Quickstart guide (`docs/quickstart.md`) for 10-minute first-run experience
- Added complete API reference documentation:
  - `docs/api/ports.md` — All Port interfaces with implementation examples
  - `docs/api/runtime.md` — Runtime lifecycle and error handling
  - `docs/api/workflow.md` — Official workflows and host services
- Added minimal consumer example (`examples/minimal-consumer/`) with working code
- Updated AI Phone handoff document with v0.1.1 changes and Memory integration guide

### API Surface
- Added `MemoryQueryPort` and `MemoryWritePort` interfaces for future Memory SDK integration
- Exported Memory ports from `simple_harness.runtime` module

### Developer Experience
- Created runnable minimal consumer example demonstrating:
  - Mock LLM provider implementation
  - Tool executor with calculator and echo tools
  - Authorization port integration
  - SQLite context persistence
  - Complete runtime setup and execution flow

### Internal Cleanup
- Removed unused `ModelPersonalWorkflowMatcher` class from `turn_authority.py`

## 0.1.1 — candidate

- Added typed provider/tool/runtime/workflow consumer operations with SDK-owned
  case verifiers; consumer Hosts provide observations but never verdicts.
- Added one shared CLI and pytest runner with fail-closed protocol/capability and
  required-case handling.
- Added redacted, fixed-schema conformance reports.
- Made `AuthorizationPort.bind_effect_handoff(...)` mandatory before every
  physical Tool handoff; Hosts that only implement the 0.1.0 `authorize(...)`
  seam must add decision and handoff receipt binders.
- Frozen the ReAct policy fingerprint in start snapshot schema v3 and added
  crash-window delivery/lifecycle recovery coverage.
- Added deterministic candidate build attestations and a non-publishing remote
  three-platform workflow contract.

## 0.1.0

- Initial durable runtime and workflow foundation.
