# Static gate receipt

- Tested HEAD: `70a2b9fe20016772e4a34035fb96520f2f17da1e`
- `git diff --check`: PASS
- `uv lock --check`: PASS (`Resolved 52 packages`)
- All four `.github/workflows/*.yml` files parsed successfully with Ruby YAML.
- `uv run reuse lint`: PASS; 264/264 files have copyright and license information, no bad/missing/deprecated/unused licenses.
- Commit-state check excluding this active run-dir: clean.
- Remote release-candidate workflow remains static only; it was not dispatched.
