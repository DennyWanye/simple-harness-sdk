# Frozen H077 artifact — 2026-09-06

- Exact source: `c29af66902dd0b418ab12c1c8f871182280f77bd`.
- Wheel: `/Users/denny/projects/simple-harness-sdk-short-context-revision/.local-test-evidence/2026-09-06/decision-terminal-recovery/artifact-077/build/simple_harness_sdk-0.7.7-py3-none-any.whl`.
- Wheel SHA256: `60f7fb164f65ca98a1aa54e4fc75cd7abbc08e409a6a10fe397106d47148c5a3`.
- Candidate manifest: `/Users/denny/projects/simple-harness-sdk-short-context-revision/.local-test-evidence/2026-09-06/decision-terminal-recovery/artifact-077/simple_harness_sdk-0.7.7.candidate-manifest.json`.
- Manifest SHA256: `0e91fc0afe1c4a6948487005ea373db24f1ffd0239096e679504f47681dfec5e`.
- Small isolated target: `/Users/denny/projects/simple-harness-sdk-short-context-revision/.local-test-evidence/2026-09-06/decision-terminal-recovery/artifact-077/installed-target`.

One offline build, using clean fixed git archive and existing hatchling1.32.0 tools;
`uv pip install --offline --no-deps --target`. No new venv, no second build, no
old wheel overwrite or push/tag. Execution schema remains9; this changes no DB
schema and performs no implicit migration/recovery on open.

Installed public consumer passed: real Runtime futureexpiry has one public failed
terminal with zero tool sends; a genuine old075 missing-proof fixture recovers and
reopens exactly; the already-recovered actual r6 COPY replays with zero extra writes.
The original r6 userdata is still NOT recovered. These are SDK public controls,
not Host/native acceptance. Root __all__ snapshot was checked (not a full submodule
matrix);109 loaded SDK modules belonged to the target and exact directURL matched.

Resource PG72585 exit0, remaining `[]`,
peak104992KiB, elapsed0.675s. Raw:
`.local-test-evidence/2026-09-06/decision-terminal-recovery/installed-resource/command.log` SHA256
`3af526fead4e004ad65055211d8c9f7bc347bfad26036be8cad547b020e77d61`;
`.local-test-evidence/2026-09-06/decision-terminal-recovery/artifact-077/installed-public/result.json` SHA256
`3fe3edd5b37469e007118750d98047602adb4d97f467639fefc5491301518880`.

Dirac source/results and artifact/installed scoped ACCEPT. Host connection is an
independent leaf; no repeated21 source tests or original lease10/analysis14 run.

Public Host sequence:

```python
record = uow.read_run_terminal_record(RunId(actual_sdk_run_id))
# Only for the exact missing-terminal error on an owned failed Run:
witness = uow.read_expired_authorization_terminal_recovery(RunId(actual_sdk_run_id))
proof = uow.recover_expired_authorization_terminal(witness, now=trusted_now)
record = uow.read_run_terminal_record(RunId(actual_sdk_run_id))
assert record.terminal_evidence == proof
```

No SDK SQL, fabricated terminal, lost original IDs, or broader legacy repair is
required or supported. Unknown histories/conflicting terminal events refuse.
