#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Stub that answers --version instantly and then hangs, to exercise timeouts."""

from __future__ import annotations

import sys
import time


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("hanging stub 0.0-test\n")
        return 0
    time.sleep(600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
