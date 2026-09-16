#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIengine stub: prints the progression-search result block and a plan.

The plan repeats one grounded action under two method occurrences so the
adapter's occurrence handling can be observed end to end.
"""

from __future__ import annotations

import sys

PLAN = """==>
0 load crate depot
1 unload crate dock
2 load crate depot
3 unload crate dock
root 4
4 __top -> __top_method 5 6
5 deliver crate depot dock -> m-deliver-direct 0 1
6 deliver crate depot dock -> m-deliver-direct 2 3
<==
"""


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIengine stub 0.0-test\n")
        return 0
    sys.stdout.write("Time limit: 1800 seconds\n")
    sys.stdout.write("Search Results\n")
    sys.stdout.write("- Search time 0.01 seconds\n")
    sys.stdout.write("- Status: Solved\n")
    sys.stdout.write("- Found solution of length 4\n")
    sys.stdout.write(PLAN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
