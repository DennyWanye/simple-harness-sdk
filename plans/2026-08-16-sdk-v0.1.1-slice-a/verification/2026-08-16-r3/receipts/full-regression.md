# Committed-HEAD full regression receipt

- Tested HEAD: `70a2b9fe20016772e4a34035fb96520f2f17da1e`
- Command: `uv run pytest -q`
- Result: PASS (`1181 passed, 2 skipped in 12.86s`)
- Expected skips: the pytest plugin's two guard tests require an explicit `--simple-harness-host`; exact-wheel consumer protocol was separately executed and passed.
- Raw JUnit index: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/r3-formal/full-committed-head.xml`
- Raw JUnit SHA-256: `3aa2797198ea913f6bda5eec930a7ae69cb43cf1d41a16c68a8c3140aa304fec`
- Raw JUnit size: 202241 bytes
