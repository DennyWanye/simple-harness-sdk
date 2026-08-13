<!-- SPDX-FileCopyrightText: 2026 DennyWanye -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Provider API

The public surface is `simple_harness.providers`; it is intentionally not
duplicated into the package-root convenience namespace.

`Provider.invoke(request, *, cancel)` represents exactly one physical model
request. Providers do not own Agent, Session, Tool, retry, fallback, ledger, or
budget state. Durable claim/handoff/outcome handling belongs to the execution
coordinator.

Every Provider exposes an immutable `ProviderTarget(provider_id, model,
pricing_key, endpoint_identity, adapter_key)`. The target is not a caller hint:
the Provider must derive it from the exact configuration used by `invoke`.
Durable coordination binds this target to the request and frozen estimator
before handoff. A custom Provider is therefore a trusted-host boundary: its
conformance suite must prove that changing any physical provider, model,
endpoint, adapter, or pricing identity changes `target`, and that the outgoing
request uses `target.model` and `target.endpoint_identity` from the same source.

`OpenAICompatibleProvider` accepts an injected `httpx.AsyncClient`, absolute
HTTP(S) base URL, model, `Secret`, and timeout. It sends one
`/chat/completions` request and normalizes text, structured function calls, and
token usage. HTTP 401, 402, 429, 5xx, timeout, cancellation, transport, request
rejection, and protocol failures have distinct stable exception types.

The official adapter normalizes its endpoint from `base_url`, binds its payload
model to `target.model`, uses a fixed versioned adapter key, and accepts optional
`provider_id`/`pricing_key` labels for hosts whose provider or billing catalog
identity differs from the endpoint hostname or model string. Those labels are
constructor configuration owned by the adapter instance, not per-request input.

No adapter error exposes response bodies or the injected secret. The adapter
does not retry or fall back to another model. Streaming is intentionally not a
v0.1 operation; the request/response boundary leaves room for a future separate
streaming method without changing `invoke` single-call semantics.
