#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Adversarial stub: a budget message and an unsolvable claim in one run.

The adapter must report SEARCH_LIMIT_REACHED. An exhausted budget is never
evidence that the goal is impossible.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIengine stub 0.0-test\n")
        return 0
    sys.stdout.write("Reached time limit - stopping search.\n")
    sys.stdout.write("- Status: Proven unsolvable\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
