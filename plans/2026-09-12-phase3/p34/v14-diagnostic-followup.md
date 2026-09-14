# P34 targeted diagnostic follow-up / v14

Last updated: 2026-09-14 CST. Strict pair still OPEN before new execution. User requested completion of this remaining gate. No plan-test skills, no packaging.

Independent Sol/medium parser review found no confirmed unjustified rejection; CallId accepts printable ASCII1..255, so incompatible provider ID remains a possibility without original raw data. No relaxation or causal attribution.

Parent first standalone probe incorrectly passed durable messages directly to HTTP adapter, omitting production AgentProviderWire tool restoration. Two requests were rejectedHTTP400 before generation; the second safely identified missing preceding tool_calls. Original logs retained; usage absent is not a monetary-zero assertion. This is probe error, not a product defect and not exact-wire reproduction.

Corrected probe uses AgentProviderWire.prepare_request with read-only original execution_effects, zero fallbacks and original frozen request/model/8192cap. Three bounded physical calls succeed:18383/19051/19087tokens (56521total),2.019/4.241/4.958s; runner11.46s. No tools executed, original stored request unchanged. This does not reproduce or repair historical v13. Raw paths in SDK ignored .local-test-evidence/2026-09-14/p34-tool-parse-probe/{result.json,http-diagnostic/result.json,restored-wire/result.json}; initial two are invalid probe attempts.

The real-pair observer now records only finite finish_reason/parse_stage/tool_parse_reason on exceptions, in addition to existing valid usage and failure. No raw arguments/exception text or unrecognized values retained. HTTP malformed-JSON and privacy control plus adjacent pagination tests19PASS0.65s, runner1.12s, g-p34-error-observation-v14. First ruff found one line length, corrected before commit. No production change from a0ed26c. Existing strict oracle/materials/profile/budgets unchanged.

Next: exactly one fixed-source complete pair v14, both arms retained even if FIRST fails. Purpose is current acceptance with precise failure diagnostics; it is not a claim of a stochastic-provider root-cause fix or a repeated-until-green loop. Historical v13 remains FAIL regardless of the result.
