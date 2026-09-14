# Local DGX source integration

Last updated: 2026-09-14 CST. Source functionality implemented; native and near-window acceptance pending at this checkpoint.

User scope: replace paid model use with the existing local Qwen endpoint, default 256K shared total context. No deployment changes, packaging or release.

Production path: source launcher defaults to `--provider local --local-profile <absolute JSON>`; credentials are read only from the four existing LOCAL fields. Missing or mismatched local configuration fails without paid fallback. Explicit DeepSeek mode remains available for separately authorized work. Provider registry default on this Mac is dgx-local; other enabled entries are disabled, credentials remain in OS Keychain. Normal user-data files are local and not shipped in Git.

The local source profile binds endpoint/model, four tokenizer file hashes and runtime library versions. HF template counting uses the same OpenAI request serializer, structured HF tool arguments and explicit `return_dict=False`. SDK RequestGuard uses the exact request hook for this counter; existing counters keep their original behavior. Private LAN HTTP is an explicit SDK opt-in for literal RFC1918/ULA addresses. Source local profile passes this option with a 900-second provider deadline.

The model reports max_model_len=262144. Mission default total=262144, input allowance=228352 (223K), output reserve=32768, safety margin=1024. Outputs start at8192 and may grow to32768 only through the existing bounded recovery. One physical local slot. This is a shared window, not256K input plus32K output. Existing frozen Mission identities are not silently migrated.

Validation before source freeze:

| Check | Status | Measured test time |
|---|---|---:|
| SDK provider/HF/exact guard/agent affected tests |105 PASS|5.92s|
| Host local/source/long context/key/wiring tests |81 PASS|18.33s|
| MissionsView tests |81 PASS|1.40s runner|
| TypeScript typecheck; SDK Ruff |PASS|not separately measured|
| Actual SDK short/tool/continuation |3 PASS, input estimates56/345/402 exactly match server|1.246/4.812/2.540s|
| Near256K request |PENDING|—|
| Source native UI and cold read |PENDING|—|

Earlier failures retained: SDK refused private HTTP before explicit support; HF5 returned BatchEncoding until return_dict=False; HF tool continuation required JSON-object arguments. After production fix one stale test string expectation failed (104PASS/1FAIL), corrected to assert HF object and unchanged HTTP JSON string;105PASS. Two wrong test path selections ran zero tests; no false acceptance. Legacy Host callback keyword compatibility repaired and81PASS. These are not discarded successes.

Raw local evidence: `.local-test-evidence/2026-09-14/dgx-local-connect/`; centralized SDK runner logs: SDK `.local-test-evidence/2026-09-12/p33-g/`, labels `g-local-sdk-final-v5`, `g-local-host-profile-v5`, `g-local-sdk-probes-v3`. Raw evidence, credentials, tokenizer cache and user config remain ignored/local. New acceptance is separate from the historical Phase3 DeepSeek pair and native-v39 results.

Source launch: `scripts/native/launch_source_orchestrator.py --help`. Local profile schema: base_url, model, max_total_tokens=262144, tokenizer_path, tokenizer_files (SHA256 map), chat_template_kwargs={}. Tokenizer assets must match the deployed server, be local and require no remote code. Install the SDK optional `hf-tokenizer` extra in the source Python environment. Use a new source run directory for the new provider; do not retarget old frozen DeepSeek pools.
