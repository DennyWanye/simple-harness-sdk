#!/usr/bin/env bash
# verify_release_gate.sh — harness SDK release gate for the dist/ 0.1.3 wheel.
#
# Steps:
#   1. Locate dist/simple_harness_sdk-0.1.3-py3-none-any.whl, compute its
#      SHA-256, and cross-check it against dist/SHA256SUMS (integrity anchor).
#   2. Create a clean venv with Python >= 3.11 and install the wheel.
#   3. Run examples/minimal-consumer/demo.py (must reach COMPLETED, exit 0).
#   4. Run the SDK conformance CLI (provider + tool suites) against
#      examples/minimal-consumer/conformance_host.py.
#   5. Emit structured PASS/FAIL; exit 0 only if every step passed.
#
# Usage:
#   ./scripts/verify_release_gate.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WHEEL="$REPO_ROOT/dist/simple_harness_sdk-0.1.3-py3-none-any.whl"
SUMS="$REPO_ROOT/dist/SHA256SUMS"
CONSUMER_DIR="$REPO_ROOT/examples/minimal-consumer"

FAILED=0
WORK="$(mktemp -d /tmp/simple-harness-release-gate.XXXXXX)"

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

find_python() {
  local p
  for p in python3 python3.13 python3.12 python3.11; do
    if command -v "$p" >/dev/null 2>&1; then
      if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        printf '%s\n' "$p"
        return 0
      fi
    fi
  done
  return 1
}

# --- Step 1: wheel + SHA-256 -------------------------------------------------
step "locate 0.1.3 wheel and compute SHA-256"
if [ ! -f "$WHEEL" ]; then
  fail "wheel missing: $WHEEL"
  say ""; say "RESULT: FAIL"; exit 1
fi
SHA="$(shasum -a 256 "$WHEEL" | awk '{print $1}')"
say "computed SHA-256: $SHA"

step "cross-check SHA against dist/SHA256SUMS"
EXPECTED="$(grep 'simple_harness_sdk-0.1.3-py3-none-any.whl' "$SUMS" | awk '{print $1}' | head -1)"
if [ -z "$EXPECTED" ]; then
  fail "no 0.1.3 wheel entry in dist/SHA256SUMS"
elif [ "$SHA" != "$EXPECTED" ]; then
  fail "wheel SHA mismatch (computed $SHA vs SHA256SUMS $EXPECTED)"
else
  pass "wheel SHA-256 matches dist/SHA256SUMS"
fi

# --- Step 2: clean venv + install --------------------------------------------
step "create clean venv"
PYTHON="$(find_python)"
if [ -z "$PYTHON" ]; then
  fail "no Python >= 3.11 found on PATH"
  say ""; say "RESULT: FAIL"; exit 1
fi
if "$PYTHON" -m venv "$WORK/venv"; then
  pass "venv ($PYTHON $("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"
else
  fail "venv"
  say ""; say "RESULT: FAIL"; exit 1
fi
VENV_PY="$WORK/venv/bin/python"

step "install 0.1.3 wheel"
if "$VENV_PY" -m pip install -q "$WHEEL"; then
  pass "install"
else
  fail "install"
fi

# --- Step 3: minimal-consumer ------------------------------------------------
if [ "$FAILED" -eq 0 ]; then
  step "run examples/minimal-consumer/demo.py"
  OUT="$(cd "$CONSUMER_DIR" && "$VENV_PY" demo.py 2>&1)"
  RC=$?
  say "$OUT"
  if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q "terminal state: completed"; then
    pass "minimal-consumer (exit 0, COMPLETED)"
  else
    fail "minimal-consumer (exit=$RC)"
  fi
fi

# --- Step 4: conformance -----------------------------------------------------
if [ "$FAILED" -eq 0 ]; then
  step "run conformance CLI (provider,tool)"
  if PYTHONPATH="$CONSUMER_DIR" "$VENV_PY" -m simple_harness.testing \
      --host conformance_host:build_host --suite provider,tool \
      --artifact-sha256 "$SHA"; then
    pass "conformance (provider,tool)"
  else
    fail "conformance (provider,tool)"
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
