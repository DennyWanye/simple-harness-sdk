# P34 targeted diagnostic follow-up / v14

Last updated: 2026-09-14 CST. v14 finished: FIRST strictFAIL and COMPARE strictPASS; the paired gate remains OPEN at this checkpoint. User requested completion of this remaining gate. No plan-test skills, no packaging.

Independent Sol/medium parser review found no confirmed unjustified rejection; CallId accepts printable ASCII1..255, so incompatible provider ID remains a possibility without original raw data. No relaxation or causal attribution.

Parent first standalone probe incorrectly passed durable messages directly to HTTP adapter, omitting production AgentProviderWire tool restoration. Two requests were rejectedHTTP400 before generation; the second safely identified missing preceding tool_calls. Original logs retained; usage absent is not a monetary-zero assertion. This is probe error, not a product defect and not exact-wire reproduction.

Corrected probe uses AgentProviderWire.prepare_request with read-only original execution_effects, zero fallbacks and original frozen request/model/8192cap. Three bounded physical calls succeed:18383/19051/19087tokens (56521total),2.019/4.241/4.958s; runner11.46s. No tools executed, original stored request unchanged. This does not reproduce or repair historical v13. Raw paths in SDK ignored .local-test-evidence/2026-09-14/p34-tool-parse-probe/{result.json,http-diagnostic/result.json,restored-wire/result.json}; initial two are invalid probe attempts.

The real-pair observer now records only finite finish_reason/parse_stage/tool_parse_reason on exceptions, in addition to existing valid usage and failure. No raw arguments/exception text or unrecognized values retained. HTTP malformed-JSON and privacy control plus adjacent pagination tests19PASS0.65s, runner1.12s, g-p34-error-observation-v14. First ruff found one line length, corrected before commit. No production change from a0ed26c. Existing strict oracle/materials/profile/budgets unchanged.

Predeclared v14 execution: exactly one fixed-source complete pair, both arms retained even if FIRST fails. Purpose is current acceptance with precise failure diagnostics; it is not a claim of a stochastic-provider root-cause fix or a repeated-until-green loop. Historical v13 remains FAIL regardless of the result.


## Completed fixed pair v14

SDK e10123db2196aee434b55614c2d4f7f80298e2ac / Host0e1a36db, frozen source-snapshot-v40. Same v7 contract/profile, no reroll. FIRST COMPLETED but strictFAIL60calls650020tokens425.324s; COMPARE COMPLETED and strictPASS105calls1214646tokens671.226s. Total165calls1864666tokens, runner1097.48s, pytest1FAIL1096.87s. Cache input subsets444671/863872. Both final reserve0/rehandoff0; group59210 has no remaining processes.

FIRST now has precise two failures: B/task2 provider-turn4 arguments_json with finish_reason=length, output8192/reasoning7339/total13189; same Agent next request16384 succeeds. C/task3 provider-turn8 arguments_json with finish_reason=tool_calls, output1113/reasoning371/total19709; this is independent malformed JSON, not truncation. No raw failed arguments were retained. COMPARE passed all unchanged consumer/synthesis/fragment/repair checks; v14 still cannot close the paired gate.

Raw Host .local-test-evidence/2026-09-13/p33-g/source-snapshot-v40/sdk/.local-test-evidence/2026-09-14/p34-real-search-value-741a127b08c246aba14b518712edf8c5/comparison.json SHA256 d65f79078e2471485120f7b727140413993110815d8f72ea2aaf1c309c392d9f. The source v8 start32K profile is a prospective output-truncation experiment only; it will not trigger a paid pair alone. The subsequent strict mode is documented separately.
