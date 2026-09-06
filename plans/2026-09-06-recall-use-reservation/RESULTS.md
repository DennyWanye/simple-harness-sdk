# Receipt-bound Provider use — scoped source results

Date: 2026-09-06. Tree `simple-harness-sdk-recall-use-reservation`, branch
`feat/recall-use-reservation`; frozen base H073 `0282fa982995b24bc893fdf6bed69d2caacd6587`.
Fixed runtime source `69db778fd2f26369ea162c51d7ec569545cb0b81` received Dirac
read-only scoped ACCEPT before execution. No Host wiring/native/formal401 claim.

| Batch | Result | Actual scope |
|---|---|---|
| tests-r4 | 32 PASS, 3.68s | Independent identity vectors; public Memory2item/2independent results; actual public Harness consumer/continuation/reopen; owned storage admission/fault/terminal controls |
| tests-r3 migration subset | 3 PASS | Actual nonempty frozen H073 DB, WAL-only commit, explicit7→8 backup upgrade, reopen/exact receipt reuse, unknown catalog refusal, rollback/retained backup, old binary refusal |
| adjacent-r1 | 33 PASS, 0.43s | Existing consumer adapter, Provider atomic dispatch/recovery, real ReAct route barrier/sink neighbors |

Batches have distinct scopes; do not add them into a formal401 score. The r3 batch
as a whole was **13 PASS/7 FAIL**, not globally green: its three migration cases
passed; seven terminal cases failed and then passed in r4. r4 excluded migration.

Public2-result control is two independent real recall results, each selecting the
same two real materialized memories. Actual Memory use receipts bind a stable
per-result child attempt under one physical Provider reservation. No expected
receipt is signed by the adapter. Confirmed-not-started retains UNKNOWN discipline
and obtains a new real grant. Same-owner concurrent coroutines are not dual-owner
process proof. Source terminal tests compare actual stored response/grant/links
and reject altered request/scope/snapshot/response/missing grant, including reopen
and late-response expiry. These storage tests are separate from public consumer.

## Retained red → green history

- fb0feaf review found two P1s: per-result Memory attempt collision and pre-driver
  required-mode downgrade. 071abd0 fixed them; original source retained.
- tests-r1: collection failed on a real cold-import cycle; context_use now imports
  the existing public memory_protocol aggregate, preserving strict DTO identity.
- tests-r2: 13 PASS/15 FAIL. Test errors were wrong RunStart export level,
  conflating selected item ID with source memory ID, and a broad conflict regex.
- tests-r3: 13 PASS/7 FAIL. Actual consumer had sent successfully then hit the old
  UNROUTED no-recall sink rule. 0f60de0/69db778 verify the actual consumed grant and
  stored response at terminal, without fabricating a route or no-recall receipt.
- Old schema1 sink and tool route barriers remain unchanged and passed adjacent-r1.

Raw logs/resource receipts remain ignored at
`.local-test-evidence/2026-09-06/recall-use/`. Command-log SHA256:

| Path | SHA256 |
|---|---|
| tests-r1/command.log | 39bbab389a191d777748d768e419795ce08cc40116ba328d22fad201785472e2 |
| tests-r2/command.log | 04359a873e0bf455e0866927ebc87e4aee10da2e2c09cc1cbd3391a371ece4ac |
| tests-r3/command.log | 5c3d8c0f78740cc50ee2922b0a4f85e5bd9293f34e0b9ba73b1cab2d1744a8bc |
| tests-r4/command.log | 15b54626abe32e03ba6f8c2bde9fd111a1cd23ed2846071784c68d285efcaaf3 |
| adjacent-r1/command.log | 567d36d1acf8068f8da6a28513c897538765250b382bad91b2e0fe98dd3ed1b4 |

Every execution used fixed145 default shared OS lock, 2048MiB/180s. r4 PID38868
exit0, peak186592KiB,4.121s, no remaining group members; adjacent PID38959 exit0,
peak66032KiB,0.665s, no remaining group members. No model/native/new full venv.
Borrowed M0613/H073 environment was not installed into or modified; new Harness
source was a declared PYTHONPATH overlay, not an installed successor.

## Minimal repeat commands

From this tree, use a fresh evidence directory for each invocation:

```sh
TASK_PY=/Users/denny/projects/simple-harness-memory-sdk-typed-short-sources/.local-test-evidence/2026-09-06/typed-short-sources/artifact/venv/bin/python
python3 /Users/denny/projects/simple_harness-primary-candidate/scripts/run_resource_bounded.py --evidence-dir .local-test-evidence/2026-09-06/recall-use/repeat-core -- env PYTHONPATH="$PWD/src" "$TASK_PY" -m pytest -q tests/unit/execution/test_context_use_contract.py tests/integration/runtime/test_context_use_public_memory.py tests/integration/runtime/test_context_use_durable.py tests/integration/runtime/test_context_use_admission.py
```

Migration additionally requires H073_PYTHON pointing to that unchanged interpreter
and H073_WHEEL pointing to the frozen operation-audit-073/build1 wheel. Its test
uses `-I`, exact wheel SHA `1a9ed5c95e6cddd4e0ccd85124320a6001008740a53213712fc89f3467cb4cd7`,
installed package-byte comparison and actual directURL (uv's archive_info is empty).
Then select only `tests/integration/runtime/test_context_use_migration.py` through
the same wrapper. Adjacent filenames are recorded in the table's command log.

## Static and packaging boundary

Mypy selected11 modules follows legacy imports: after fixing this change's typed
boundaries it still reports165 diagnostics in19 imported files. The same check of
an exact0282 source archive reports166; normalized path/message comparison shows
**zero newly introduced diagnostics**, not a green whole-project typing claim.
Raw typing-r1 (wrong filename), typing-r2 (new defects), typing-r3 and exact baseline
are retained. No suppression/config weakening was used.

Local heads/tags/remotes inventory:23 refs, highest0.7.3, no0.7.4. User authorized
0.7.4 candidate freeze; H073 current exports are snapshotted from its actual isolated
binary, old0.7.2 snapshot is retained. Target install and artifact results will be
recorded separately. Official schema8 requires explicit public backup migration
before Host switches an existing DB; no active userdata is touched here.


Final artifact checkpoint: source92292699, double offline wheels and target-installed
public6PASS; version/API/admission/public freeze batch13PASS. Exact identities,
resource receipts and remaining Host migration/wiring gates are in
[ARTIFACT-HANDOFF.md](ARTIFACT-HANDOFF.md). No original401 status is changed here.
