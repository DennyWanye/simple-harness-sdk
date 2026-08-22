<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Build and release runbook

This is the authoritative operator procedure for building and distributing
`simple-harness-sdk`. Build authority is local: GitHub Releases may distribute the exact frozen
bytes, but must never rebuild or modify them.

## Invariants

- Build once from the exact candidate commit in a clean detached worktree.
- The annotated version tag, `BUILD_INFO.txt`, and package version must identify that commit.
- Publish the wheel, sdist, `SHA256SUMS`, and `BUILD_INFO.txt` as one immutable set.
- Never overwrite a published asset. If bytes or metadata must change, issue a new version.
- Never publish from a dirty worktree or from a later documentation-only `main` commit.
- Run the exact-wheel gate before uploading and download the assets back after uploading.

## Prerequisites

- Git and authenticated GitHub CLI (`gh auth status`).
- `uv` and Python 3.11 or newer.
- A clean repository and a candidate commit already merged to `main`.
- The package version in `src/simple_harness/version.py` matches the intended tag.

## 1. Freeze the candidate

Set the release identity explicitly. The values below are the current 0.3.0 example; future
releases must replace all three values.

```bash
SDK_REPO=/Users/denny/projects/simple-harness-sdk
RELEASE_TAG=v0.3.0
CANDIDATE_COMMIT=fbb156fb912a49c60770c408893f8c7730616760

git -C "$SDK_REPO" status --short
test "$(git -C "$SDK_REPO" rev-parse "$RELEASE_TAG^{}")" = "$CANDIDATE_COMMIT"
git -C "$SDK_REPO" merge-base --is-ancestor "$CANDIDATE_COMMIT" main
```

For a new version, create the annotated tag locally only after the candidate commit and version
have been reviewed:

```bash
git -C "$SDK_REPO" tag -a "$RELEASE_TAG" "$CANDIDATE_COMMIT" \
  -m "simple-harness-sdk ${RELEASE_TAG#v}"
```

Do not move an existing tag.

## 2. Build once in a clean worktree

```bash
RELEASE_DIR="$(mktemp -d /tmp/simple-harness-sdk-release.XXXXXX)"
git -C "$SDK_REPO" worktree add --detach "$RELEASE_DIR" "$CANDIDATE_COMMIT"
cd "$RELEASE_DIR"

uv sync --frozen --group dev
uv run --frozen --group dev pytest -q
uv run --frozen --group dev mypy
uv run --frozen --group dev ruff check \
  src/simple_harness/runtime/conversation_memory.py \
  src/simple_harness/runtime/conversation_context.py \
  src/simple_harness/runtime/drivers/react.py \
  src/simple_harness/runtime/production.py \
  src/simple_harness/execution/context_staging.py \
  src/simple_harness/execution/memory_outbox.py \
  src/simple_harness/execution/sqlite/migrations/execution_v3_to_v4.py \
  src/simple_harness/execution/sqlite/storage.py \
  src/simple_harness/testing/arm64_candidate.py \
  tests/conformance/future_consumer_fixture.py \
  tests/conformance/test_future_consumer_memory.py \
  scripts/build/authoritative_provenance.py
uv run --frozen python scripts/check_source_provenance.py
uv run --frozen reuse lint

uv build --out-dir dist
BUILD_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
uv run python scripts/build/authoritative_provenance.py emit \
  --dist dist \
  --source-commit "$CANDIDATE_COMMIT" \
  --build-utc "$BUILD_UTC"
uv run python scripts/build/authoritative_provenance.py verify \
  --dist dist \
  --source-commit "$CANDIDATE_COMMIT" \
  --version "${RELEASE_TAG#v}"
uvx --from "twine>=6.1,<7" twine check dist/*.whl dist/*.tar.gz
./scripts/verify_release_gate.sh
(cd dist && shasum -a 256 -c SHA256SUMS)
```

`dist/` is now the frozen local publication set. Preserve these exact bytes; do not run
`uv build` again for the same version.

## 3. Publish the tag and frozen assets

Push the already tested source and tag, then create a GitHub Release only as a download channel:

```bash
git -C "$SDK_REPO" push origin main "refs/tags/$RELEASE_TAG"

gh release create "$RELEASE_TAG" \
  "$RELEASE_DIR"/dist/*.whl \
  "$RELEASE_DIR"/dist/*.tar.gz \
  "$RELEASE_DIR"/dist/SHA256SUMS \
  "$RELEASE_DIR"/dist/BUILD_INFO.txt \
  --repo DennyWanye/simple-harness-sdk \
  --verify-tag \
  --title "simple-harness-sdk $RELEASE_TAG" \
  --notes "Locally built and verified frozen SDK artifacts."
```

Do not use `gh release upload --clobber`. If the Release already exists, compare every remote
asset first and abort on any difference.

## 4. Download-back verification

```bash
VERIFY_DIR="$(mktemp -d /tmp/simple-harness-sdk-download.XXXXXX)"
gh release download "$RELEASE_TAG" \
  --repo DennyWanye/simple-harness-sdk \
  --dir "$VERIFY_DIR"
(cd "$VERIFY_DIR" && shasum -a 256 -c SHA256SUMS)
cmp "$RELEASE_DIR/dist/BUILD_INFO.txt" "$VERIFY_DIR/BUILD_INFO.txt"
```

Only after this succeeds may consumer repositories update their dependency lock.

## 5. Consumer URL and AIPhone handoff

The stable wheel URL is:

```text
https://github.com/DennyWanye/simple-harness-sdk/releases/download/<tag>/<wheel-filename>
```

AIPhone must pin the exact tag URL and `#sha256=<wheel_sha256>` from `BUILD_INFO.txt`, then update
its expected distribution versions, provenance manifest, hashed requirements, offline wheelhouse,
lockfile, and candidate tests. Never point AIPhone at `latest` or an unversioned URL.

## 6. Cleanup and recordkeeping

After the local publication set has been copied to its retained location and download-back
verification has passed:

```bash
git -C "$SDK_REPO" worktree remove "$RELEASE_DIR"
```

Update `INTEGRATION_STATUS.md`, `ARCHITECTURE/ARCHITECTURE.md`, and the consumer handoff with the
tag, source commit, wheel SHA-256, test result, and Release URL. Do not commit generated wheel,
sdist, logs, credentials, or raw test evidence.
