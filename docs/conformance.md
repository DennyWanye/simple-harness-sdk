# Consumer conformance protocol

Simple Harness SDK 0.1.1 ships executable provider, tool, runtime, and workflow
conformance suites. The Host supplies a synchronous factory that returns a
`ConformanceHost`. Each `open_suite(name)` call must return a fresh asynchronous
context whose `__aexit__` calls its suite's `aclose()` exactly once.

The four suites expose named typed operations such as `physical_request()`,
`reconcile()`, `restart_without_replay()`, and `reopen()`. Operations return raw
`CaseObservation` facts and bounded evidence; Hosts cannot return PASS/FAIL.
SDK-owned verifiers derive every case verdict from identities, physical call
counts, persisted/reopened state, terminal outcomes, and redaction observations.

```bash
python -m simple_harness.testing \
  --host my_product.conformance:build_host \
  --suite provider,tool,runtime,workflow \
  --artifact-sha256 "$VERIFIED_WHEEL_SHA256" \
  --json conformance-report.json
```

All protocol-v1 cases are required. A missing suite capability, protocol-major
mismatch, skipped case, failed case, Host error, or suite lifecycle error makes
the command exit nonzero. Reports include the SDK/protocol/Host versions,
platform, Python version, caller-verified exact wheel SHA-256, case durations, and redacted
evidence. Hosts must never place credentials or raw Provider bodies in evidence;
the runner also redacts common secret fields and token forms as defense in depth.

Pytest consumers use the same runner:

```python
def test_sdk_conformance(simple_harness_conformance_report):
    assert simple_harness_conformance_report.passed
```

```bash
pytest consumer_test.py \
  --simple-harness-host my_product.conformance:build_host \
  --simple-harness-artifact-sha256 "$VERIFIED_WHEEL_SHA256" \
  --simple-harness-suite provider,tool,runtime,workflow
```

The wheel contains the protocol and suite runner, but not this repository's SDK
test sources. A consumer Host adapter therefore remains independently owned by
the consumer.
