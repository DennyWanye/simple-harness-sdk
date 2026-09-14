# Explicit DeepSeek strict profile for P34

Last updated: 2026-09-14 CST. Current source implementation validated in focused checks; committed recheck and fixed full pair remain pending. Original source acceptance45PASS/1OPEN/2packaging DEFERRED. No UI, installer or whole-Phase3 completion claim.

The v14 failures distinguish insufficient initial output(length8192) from invalid JSON at normal tool_calls termination. An explicit new configuration combines the existing32768 output ceiling as its initial allowance with DeepSeek strict function schemas. Total Mission8M/24attempts, all Task budgets/goals/criteria/materials/deadlines, provider model deepseek-flash and256K context remain unchanged. Historical v7 and prospective v8 exports remain separate. No old failure is deleted or reclassified, and no new full pair is launched merely to retry v8.

## Production contract

OpenAICompatibleProvider accepts tool_schema_mode=deepseek-strict-v1 only at official HTTPS Beta endpoint. It uses distinct adapter_key openai-compatible.chat-completions.deepseek-strict-v1; normal legacy mode retains identical payload and target identity. Trailing slash normalization follows the adapter's existing URL behavior. The shared pure serializer performs bounded schema projection and sets strict:true on all functions. Parsing remains unchanged: malformed JSON is refused, known usage preserved, no argument repair or tool execution bypass.

The compiler preserves optional-field omission by enumerating closed object subsets under typed-root anyOf (at most3optional/object,2048output nodes,128KiB). minLength/maxLength become an absolute-end pattern, including trailing-newline semantics. Unrestricted objects, unsupported/unknown keywords, nullable types and documented unsupported array length constraints are rejected locally. No tool names or actual arguments change. Property ordering is canonical so durable JSON roundtrip preserves the schema branch list and exact body.

DeepSeekV41TokenEstimator takes the same explicit mode and calls the same serializer used by HTTP. Its strict fingerprint differs; the existing legacy fingerprint remains deepseek-v41:0f06e5994d18d90023a59966c593aef152d66a0679f0df56c35e7268e15c959d. RuntimeProfile refuses a known adapter/counter mode mismatch. New P34 context256-8m-strict32k-v9 routes to independent deepseek-strict-context-256k-v1 pool with matching counter and frozen context/admission identity. Old pools are not rewritten; a cold strict pool rejects replacement by legacy identity.

Strict is an explicit provider configuration with all functions enabled under that mode; it does not automatically redirect existing official-root deployments or their frozen continuations. Source-native legacy UI evidence retains its earlier versions. No claim that this one change removes all possible model-quality or provider failures.

## Evidence

[Official tool-call specification](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/) requires Beta, strict functions and supported schema. Parent credential-scoped probes retained locally:

| Probe | Actual result |
|---|---|
| Bare root anyOf |HTTP400: top level must be object; usage absent, not a zero-cost claim|
| Typed root anyOf, run_tests omitted path |HTTP200, valid empty arguments,528tokens0.824s|
| Typed root read with all4 requested fields |HTTP200, exact values/original-schema valid,660tokens1.714s|
| Correct absolute-end string length |19local equivalence cases including trailing newlines; HTTP200 exact read fields,635tokens1.283s|
| Actual v14 failure position under strict32K |SDK adapter succeeded, normal final stop/no tools,19563tokens5.243s; no original ledger changes. This is not a Mission completion or tool-execution result|

Raw SDK .local-test-evidence/2026-09-14/p34-strict-probe/ and p34-tool-parse-probe/strict-v14-request/. No credentials or failed raw arguments stored. Successful small probes1823tokens in total; the actual failed-position successor19563 is separate.

| Software check | Actual result |
|---|---|
| Initial v8 profile |36PASS/1FAIL1.21s: parent added missing estimate_input_tokens to a test-only Counter; no production defect|
| First strict integration |157PASS7.66s, runner8.23s, including pinned counter/HTTP equality, legacy fingerprint, profile/registry/length recovery|
| Canonical cold-body red test |1FAIL0.09s before property sorting; confirmed real implementation gap|
| Canonical fix broad check |156PASS/2FAIL6.85s; two trailing-slash expectations still required rejection after intentional normalization|
| Corrected endpoint controls |13PASS0.22s; trailing slash moved to positive normalization cases; nonofficial endpoints still refused|
| Static |ruff all changed Python files, mypy four production files PASS|

Precommit runner tracked-diff hashes exclude new untracked files; committed recheck is required before freezing the paid candidate. All failed checks retained. Hubble implemented adapter/compiler/tests; parent corrected unsupported minItems/maxItems/nullable handling, end-anchor semantics and canonical property ordering, integrated shared counting/profile isolation and fixed test-only Counter/URL expectations. No matched-task savings claim.
