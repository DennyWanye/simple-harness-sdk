#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIengine stub: its own --timelimit budget ran out, exit code 0."""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIengine stub 0.0-test\n")
        return 0
    sys.stdout.write("Reached time limit - stopping search.\n")
    sys.stdout.write("Search Results\n")
    sys.stdout.write("- Status: Timeout\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
