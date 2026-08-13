<!-- SPDX-FileCopyrightText: 2026 DennyWanye -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Tool API

`ToolRegistry` is explicit and per-runtime. It does not auto-discover modules,
read environment variables, or register a shell Tool. A host registers typed
`Tool` objects or `FunctionTool` handlers.

Every `ToolSpec.input_schema` uses a deliberately small, fail-closed JSON Schema
subset. The root is an object, `additionalProperties` must be `false`, and the
registry validates required fields, types, enums, string/array lengths, numeric
bounds, and reserved host fields before entering a handler. Unknown schema
keywords are rejected when the Tool is registered.

`ToolResult` exposes exactly five outcomes: `succeeded`, `partial`, `rejected`,
`failed`, and `unknown`. Duplicate call IDs cannot invoke a handler twice;
cancelled calls terminate cooperatively; a result with the wrong call ID is
rejected as late. Host exceptions are converted into a stable minimal error and
their raw text is not returned to the model.

Authorization remains host-owned through
`AuthorizationPort.authorize(PreparedToolEffect)`. An allow decision requires a
receipt reference. `ToolReconciliationPort.observe(effect)` returns only
`confirmed_not_started`, `completed`, or `still_unknown`, always with an
evidence reference. Only `completed` may carry a settled `ToolResult`.
