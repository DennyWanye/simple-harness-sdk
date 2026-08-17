# SDK-A7 command receipt

- Candidate command: `uv run python scripts/build/reproducibility.py --output .local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/candidate --planned-tag v0.1.1`
- Candidate result: PASS; two builds were byte-identical and bound to committed HEAD `70a2b9fe20016772e4a34035fb96520f2f17da1e`.
- Exact-byte consumer result: clean Python 3.11 CLI PASS (22 cases / four suites) and pytest protocol PASS (`1 passed in 0.92s`) using no checkout `PYTHONPATH`.
- Candidate-contract result: PASS (`5 passed in 6.25s`).
- Wheel SHA-256: `371ceb98913986f6b3f7ef1187255483d210556e9baed94e4f4ed115d49510d5`
- Candidate manifest SHA-256: `4b834cc7f27b06c116eb0875074d34265e124e86fccf9a0068809cddfd34bf54`
- CLI report index/SHA-256: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/candidate-consumer/cli-report.json` / `2441b936abf529f8f753a8fd01ce1ffff8be1949f63cb3a18d865c2d8d2c4599`
- Pytest report index/SHA-256: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/candidate-consumer/pytest-report.xml` / `d13ffea51718452572c0ec004ce9ea7b331c920abca6d10ae34bf1f9c7dfd31c`
- Candidate-contract JUnit index/SHA-256: `.local-test-evidence/2026-08-16/sdk-v0.1.1-slice-a/r3-formal/sdk-a7-contract.xml` / `7c39b46b3a08d173feb630acab337a5eeb44b3a75fa117ae5d8495d672e21106`
- Negative assertion: dirty/untracked build inputs, wrong or drifted tags, and non-empty output targets fail closed; no tag, push, workflow dispatch, or release was performed.
