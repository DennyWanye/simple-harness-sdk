<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

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
