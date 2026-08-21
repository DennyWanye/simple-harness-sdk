#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

# verify_from_zero.sh — simulate an external user integrating the SDK from zero.
#
# Steps:
#   1. Clone the SDK repository into a clean temporary directory.
#   2. Extract the build & install commands VERBATIM from docs/quickstart.md
#      (the script carries no wheel path or version of its own — if the doc
#      lies, this gate goes red).
#   3. Extract the single runnable ```python block from docs/quickstart.md and
#      execute it against the installed wheel.
#   4. Run examples/minimal-consumer/demo.py twice (re-runnability proof).
#   5. Emit structured PASS/FAIL per step; exit 0 only if every step passed.
#
# Usage:
#   ./verify_from_zero.sh [sdk-repo-path]
# Default sdk-repo-path is this script's own repository root.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_REPO="${1:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)}"

FAILED=0
WORK="$(mktemp -d /tmp/simple-harness-verify-from-zero.XXXXXX)"

say()  { printf '%s\n' "$*"; }
step() { say ""; say "== STEP: $1"; }
pass() { say "PASS: $1"; }
fail() { say "FAIL: $1"; FAILED=1; }

cleanup() {
  if [ "$FAILED" -eq 0 ]; then
    rm -rf "$WORK"
  else
    say "Work directory preserved for debugging: $WORK"
  fi
}
trap cleanup EXIT

# --- Step 1: clean clone -----------------------------------------------------
step "clone SDK repository"
say "source: $SDK_REPO"
if git clone -q "$SDK_REPO" "$WORK/simple-harness-sdk"; then
  pass "clone"
else
  fail "clone"
  say ""; say "RESULT: FAIL"; exit 1
fi
CLONE="$WORK/simple-harness-sdk"
QUICKSTART="$CLONE/docs/quickstart.md"
[ -f "$QUICKSTART" ] || { fail "quickstart.md missing in clone"; say "RESULT: FAIL"; exit 1; }

# --- Step 2: extract + execute install commands from quickstart --------------
step "extract install commands from docs/quickstart.md"

# The Installation section's first ```bash block, minus the clone/cd lines
# (this script performs the clone itself). Everything else runs verbatim.
awk '
  /^## Installation/ {in_section=1; next}
  /^## / && in_section {in_section=0}
  in_section && /^```bash$/ {in_block=1; next}
  in_block && /^```$/ {in_block=0; in_section=0; next}
  in_block {print}
' "$QUICKSTART" | grep -v -E '^(git clone|cd )' > "$WORK/install_cmds.sh" || true

say "extracted commands:"
sed 's/^/  | /' "$WORK/install_cmds.sh"

if ! grep -q 'uv build' "$WORK/install_cmds.sh"; then
  fail "quickstart install section did not yield a 'uv build' command"
fi
if ! grep -q 'pip install' "$WORK/install_cmds.sh"; then
  fail "quickstart install section did not yield a 'pip install' command"
fi

if [ "$FAILED" -eq 0 ]; then
  step "execute extracted install commands (verbatim, in clone root)"
  if (cd "$CLONE" && bash -e "$WORK/install_cmds.sh"); then
    pass "build + install (commands extracted from quickstart)"
  else
    fail "build + install (commands extracted from quickstart)"
  fi
fi

# --- Step 3: extract + run the quickstart python block -----------------------
step "extract runnable python block from docs/quickstart.md"

PY_BLOCK_COUNT="$(grep -c '^```python$' "$QUICKSTART" || true)"
if [ "$PY_BLOCK_COUNT" != "1" ]; then
  fail "quickstart must contain exactly one plain \`\`\`python block, found $PY_BLOCK_COUNT"
fi

awk '/^```python$/{flag=1;next} /^```/{if(flag)flag=0} flag' "$QUICKSTART" > "$WORK/quickstart_example.py"

if [ ! -s "$WORK/quickstart_example.py" ]; then
  fail "extracted python block is empty"
fi

if [ "$FAILED" -eq 0 ]; then
  step "execute quickstart python block verbatim"
  if (cd "$WORK" && "$CLONE/.venv/bin/python" "$WORK/quickstart_example.py"); then
    pass "quickstart python block runs and reaches declared terminal state"
  else
    fail "quickstart python block execution"
  fi
fi

# --- Step 4: minimal-consumer, twice -----------------------------------------
if [ "$FAILED" -eq 0 ]; then
  step "run examples/minimal-consumer/demo.py (run 1 of 2)"
  OUT1="$(cd "$CLONE/examples/minimal-consumer" && "$CLONE/.venv/bin/python" demo.py 2>&1)"
  RC1=$?
  say "$OUT1"
  if [ "$RC1" -eq 0 ] && printf '%s' "$OUT1" | grep -q "terminal state: completed"; then
    pass "minimal-consumer run 1 (exit 0, COMPLETED)"
  else
    fail "minimal-consumer run 1 (exit=$RC1)"
  fi

  step "run examples/minimal-consumer/demo.py (run 2 of 2 — re-runnability)"
  OUT2="$(cd "$CLONE/examples/minimal-consumer" && "$CLONE/.venv/bin/python" demo.py 2>&1)"
  RC2=$?
  say "$OUT2"
  if [ "$RC2" -eq 0 ] && printf '%s' "$OUT2" | grep -q "terminal state: completed"; then
    pass "minimal-consumer run 2 (exit 0, COMPLETED)"
  else
    fail "minimal-consumer run 2 (exit=$RC2)"
  fi
fi

# --- Result ------------------------------------------------------------------
say ""
if [ "$FAILED" -eq 0 ]; then
  say "RESULT: PASS"
  exit 0
else
  say "RESULT: FAIL"
  exit 1
fi
