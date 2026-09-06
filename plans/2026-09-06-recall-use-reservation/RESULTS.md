# Receipt-bound Provider reservation — source checkpoint

Date: 2026-09-06. Base `0282fa982995b24bc893fdf6bed69d2caacd6587` (frozen H073).
Tree: `simple-harness-sdk-recall-use-reservation`, branch `feat/recall-use-reservation`.

Implementation and oracle source are ready for the first read-only challenge.
Tests: **NOT_RUN**; no source PASS, installed PASS, native PASS or formal401 change.
Only source formatting/import organization has run. No build or new environment.

The test files separate independent identity vectors, actual public Memory+Harness
consumer, owned SQLite fault/recovery, and old-binary WAL migration controls.
The old-binary fixture uses an existing H073 interpreter designated by
`H073_PYTHON`; the Memory public suite uses existing M0613 with the new Harness
source overlaid, and will not be labelled exact installed successor evidence.

After read-only source challenge, all execution must use the coordinator's fixed
145 `scripts/run_resource_bounded.py` default shared OS lock (2GiB/180s). BUSY means
no run and no alternative lock. No model/native or whole-repository regression.
Raw evidence will stay ignored under `.local-test-evidence/2026-09-06/recall-use/`.

Public payload-free view and Host call contract: [CONTRACT.md](CONTRACT.md).
Host default wiring, original401 cell closure and candidate packaging are pending.

First fixed fb0feaf received two read-only P1s (multi-result key collision and
pre-checkpoint mode downgrade). They are corrected in the successor source and
covered by new public multi-result and accepted-Run crash controls. Still NOT_RUN;
no assertion that the original source was accepted. Frozen old failure remains.
