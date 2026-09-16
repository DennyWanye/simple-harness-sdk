#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIengine stub that emits far more than the excerpt bound after the plan."""

from __future__ import annotations

import sys

PLAN = """==>
0 load crate depot
root 1
1 deliver crate depot dock -> m-deliver-direct 0
<==
"""


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIengine stub 0.0-test\n")
        return 0
    sys.stdout.write("- Status: Solved\n")
    sys.stdout.write(PLAN)
    sys.stdout.write("x" * 20000 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
