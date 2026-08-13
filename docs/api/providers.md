<!-- SPDX-FileCopyrightText: 2026 DennyWanye -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Provider API

The public surface is `simple_harness.providers`; it is intentionally not
duplicated into the package-root convenience namespace.

`Provider.invoke(request, *, cancel)` represents exactly one physical model
request. Providers do not own Agent, Session, Tool, retry, fallback, ledger, or
budget state. Durable claim/handoff/outcome handling belongs to the execution
coordinator.

`OpenAICompatibleProvider` accepts an injected `httpx.AsyncClient`, absolute
HTTP(S) base URL, model, `Secret`, and timeout. It sends one
`/chat/completions` request and normalizes text, structured function calls, and
token usage. HTTP 401, 402, 429, 5xx, timeout, cancellation, transport, request
rejection, and protocol failures have distinct stable exception types.

No adapter error exposes response bodies or the injected secret. The adapter
does not retry or fall back to another model. Streaming is intentionally not a
v0.1 operation; the request/response boundary leaves room for a future separate
streaming method without changing `invoke` single-call semantics.
