<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

## 0.1.2 — candidate

**Focus:** Ease of integration for external projects.

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
