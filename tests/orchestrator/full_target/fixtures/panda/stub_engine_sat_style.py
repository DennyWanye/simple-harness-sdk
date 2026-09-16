#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIengine stub imitating the SAT back end: a plan but no status line.

The SAT / BDD back ends do not print "- Status:" lines, so the adapter must
refuse to classify rather than assume success.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIengine stub 0.0-test\n")
        return 0
    sys.stdout.write("==>\n0 load crate depot\nroot 1\n1 deliver -> m-deliver-direct 0\n<==\n")
    sys.stdout.write("Total cost of solution: 1 true cost: 1\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
