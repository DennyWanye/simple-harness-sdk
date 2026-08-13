# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

# Provider invocation ledger and budget authority

`ProviderInvocationCoordinator` is the only SDK boundary that may call a
`Provider` for a durable run. It persists the deterministic invocation identity
for the logical `(run_id, request_id)` call, content-only request fingerprint,
exact `ProviderTarget`, frozen estimator snapshot/digest, and budget reservation
before the physical call. It then advances the row with compare-and-swap
transitions:

```text
claimed -> handed_off -> succeeded | failed | unknown
```

A recovered `claimed` row is safe to resume because no provider handoff was
recorded. A recovered `handed_off` row becomes `unknown`; the SDK never blindly
replays it. Re-reading a `succeeded` invocation returns its durable normalized
response without another provider call.

Reusing `(run_id, request_id)` with different content, target, or estimator is
an identity conflict. This remains true across process restart and concurrent
coordinators; the conflicting invocation performs no transport call.

Hard-cap checks and claim insertion occur in one SQLite transaction. The
versioned `usage_json.budget` envelope stores either trusted-usage cost, a
frozen estimator upper bound, or an explicit unknown charge. Unknown cost is
never counted as zero. A hard cap requires a known pre-dispatch reservation;
when no hard cap is configured, `refuse_on_unknown` may allow the first call to
obtain trusted usage but blocks every later call after an unknown settlement.

`FrozenPriceEstimator` is pricing-key-specific. Its price snapshot and tokenizer
overhead values must be frozen by the host; UTF-8 byte length supplies the
conservative content-token bound. Construction fails unless its pricing key
matches `Provider.target.pricing_key`; response usage is charged as trusted only
when the normalized response model matches `Provider.target.model`.

Provider adapters remain stateless and do not retry or own ledger state. The
coordinator does not own agent, session, workflow, or tool state.
