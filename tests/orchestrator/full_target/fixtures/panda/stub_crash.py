#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Stub that crashes with exit code 3 and writes to stderr."""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("crashing stub 0.0-test\n")
        return 0
    sys.stderr.write("terminate called after throwing an instance of 'std::bad_alloc'\n")
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
