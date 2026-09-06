# Mandatory context repair: fixed source handoff

2026-09-06. Product25221a4 / final tests28160d6, based on frozen H078 bb9abfd.
Dirac final read-only source/results **limited ACCEPT**, 14 unique new controls:
11 SDK and 3 Host. H079 version/build/installed gate is handed to main; M618 remains
for r17. No further source writes or tests after this handoff.

SDK persistence: successful Provider response before the Host decision hook;
schema8 bounded repair journal/phase, stable request/response/turn identities,
idempotent feedback append and original budgets. Typed attestation checks follow
actual scope across checkpoint versions. Every repair-bearing proposed terminal,
including routed Runs, still requires current ACK/mandatory-exit reconciliation.
No fake tool calls, new ACK meaning, private Host SQL, new table or old Run edits.

Source results: first8 SDK controls passed in batches2+1+5; new protected corruption
controls3PASS/.27s; actual Host main-sink/publicMemory/timer/ACK/physical-guard
controls3PASS/6.63s. Last PG21416 exit0/.439s/remaining[]/cleanupnull. All prior
fixture failures retained. Four SDK interruption controls close/open SQLite under
an unexpired original lease, not a new-owner production restart. Host positive
reads public audit and matches completed context.apply repair identity and
context.no_recall rejection, reusing known audit kinds. No full audit coverage,
old binary smoke, model/native, 401/240 or full program completion claim.

[Full exact batches, commands, failures, log hashes and native remaining gate](/Users/denny/projects/simple_harness-typed-recall-context-use-full/plans/2026-09-06-prospective-mandatory-repair/RESULTS.md).
Raw evidence is ignored in that Host tree under
`.local-test-evidence/2026-09-06/mandatory-context-repair/`; none is committed.

The SDK public API snapshot contains the four additive public DTO/error exports;
version intentionally stays unallocated in this source leaf until main performs
H079 versioning. This is source-overlay validation against the existing M618
installed target, not an H079 installed identity. Frozen H078 wheels untouched.
