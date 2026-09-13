# Safe tool-parse diagnostics v40

Last updated: 2026-09-14 CST. Source-level focused acceptance PASS; this is a diagnostics-only successor to native snapshot-v39, not a new native acceptance claim.

OpenAICompatibleProvider now retains a finite tool_parse_reason for independently valid-usage tool failures: shape/type/id/function/name/arguments_json/arguments_non_object/normalization. No raw tool arguments, names, IDs or private exception text are copied. The original validation and failure remain; unknown usage remains unknown. No JSON repair, schema relaxation, retry increase or acceptance change. Existing length-only bounded recovery remains8K->16K->32K within admission/cancellation/deadline/budget. Input context256K/512K is unchanged.

The real HTTP adapter, failed AgentTurn persistence/repeat/new-runtime cold read, original usage/reconciliation and existing valid tool/public API behavior are covered. The constructed malformed-JSON case is not an exact reproduction or diagnosis of the missing historical v13 response.

| Check | Result |
|---|---|
| First runner launch | Failed before spawning tests: parent used wrong date in interpreter path; empty original log retained |
| Initial focused tests |36PASS/1FAIL1.22s, runner1.57s: invalid-id fixture also lacked function, so correct first diagnostic was function |
| Expanded tests after isolated fixture fix |66PASS/1FAIL2.05s, runner2.38s: public API snapshot omitted four accounting exports introduced by existing6866a17 |
| Final expanded tests |67PASS1.90s, runner2.22s; snapshot updated with those four explicit exports, no production public API change |
| Static |ruff on three changed Python files and mypy on adapter PASS|

Final command: python -m pytest -q tests/providers/test_protocol_failure_metadata.py tests/agents/test_tool_output_length_recovery.py tests/integration/test_protocol_failure_usage.py tests/integration/test_openai_mock_server.py tests/agents/test_provider_wire.py tests/agents/test_provider_response_durability.py tests/conformance/test_provider_contract.py tests/conformance/test_provider_public_api.py. Central runner g-tool-parse-reasons-final-v40c, complete source-test-live-v21 Python; no paid calls. No redundant full cumulative rerun for this diagnostics-only delta. Prior native-v39 and source cumulative retain their original exact versions.

SDK raw .local-test-evidence/2026-09-12/p33-g/g-tool-parse-reasons-final-v40c.json SHA256 c92a776223fdf6a9230c404aa6a2f81a2fbabd01aa6f8faf809338f8e274dc2d; .log SHA256 0a1fbcc7dc6a07ae6737717346185a97bf4ff1e6f316fe4e0810988fdb78cccd. Earlier failed runs are retained.

Dewey Sol/medium authored adapter/metadata tests; parent reviewed, added durable AgentTurn cold-read test, fixed isolated fixture and stale pre-existing API snapshot. Local response counters:107.49wall seconds including waits,58775uncached input/388864cached input/5116output. Parent coordination not separately timed; no matched-task savings claim. All children closed.
