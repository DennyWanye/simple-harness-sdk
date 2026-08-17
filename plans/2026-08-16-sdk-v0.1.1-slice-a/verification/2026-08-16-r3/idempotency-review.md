# Slice A idempotency and semantic-equivalence review

- Authorization decision creation/resolution uses durable decision identity, nonce and version CAS. Duplicate ALLOW returns the same decision result; wrong Run/nonce/version and late/expired/cancelled signals fail before physical Tool execution.
- Effect handoff requires SDK and Host hash-bound receipts. Missing Host `bind_effect_handoff` cannot self-sign or invoke the Tool. Post-handoff uncertainty is reconciled instead of blindly replayed.
- Delivery is durable at-least-once with a stable idempotency key. The SDK may retry after sink-success/before-settle crash; the Host sink deduplicates the same key. The fault test observes two attempts and one visible delivery.
- Capability build has six checkpointed nodes. Every physical Port receives `sha256(admission_fingerprint + "|" + stage)`; a before-commit crash retries the same key, while the idempotent Host returns the stored receipt and exposes one physical effect.
- Runtime `start`/`close` transitions are lock protected; concurrent start shares one task and concurrent close converges to one CLOSED transition.
- Candidate generation rejects dirty build inputs, wrong/drifted tags and non-empty targets. Two builds produce identical wheel/sdist bytes and bind manifest, BUILD_INFO and SHA256SUMS to the same commit.
- Scenario accounting contains distinct black-box classes, not retries re-labelled as new inputs. Each required `min_root_runs` count is backed by separate production Run roots or isolated durable databases; A6/A7 correctly assert that no SDK Run is created.

VERDICT: PASS
