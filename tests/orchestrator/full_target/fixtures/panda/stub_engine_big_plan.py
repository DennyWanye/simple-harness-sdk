#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIengine stub whose status line and plan sit past the excerpt bound.

A real engine prints heuristic and search statistics first, so classification
and plan extraction must read the whole stream rather than the first 4 KiB.
"""

from __future__ import annotations

import sys

ACTIONS = 400


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIengine stub 0.0-test\n")
        return 0
    for index in range(200):
        sys.stdout.write(f"[{index:04d}] expanded nodes, heuristic rc2(ff), padding padding\n")
    sys.stdout.write("Search Results\n")
    sys.stdout.write("- Status: Solved\n")
    sys.stdout.write(f"- Found solution of length {ACTIONS}\n")
    sys.stdout.write("==>\n")
    for index in range(ACTIONS):
        sys.stdout.write(f"{index} act{index} crate depot\n")
    sys.stdout.write(f"root {ACTIONS}\n")
    sys.stdout.write(f"{ACTIONS} __top -> __top_method {ACTIONS + 1}\n")
    children = " ".join(str(index) for index in range(ACTIONS))
    sys.stdout.write(f"{ACTIONS + 1} big crate -> m-big {children}\n")
    sys.stdout.write("<==\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
