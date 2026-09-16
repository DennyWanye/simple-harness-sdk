#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIgrounder stub: writes the SAS output file and exits 0."""

from __future__ import annotations

import pathlib
import sys


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIgrounder stub 0.0-test\n")
        return 0
    positional = [item for item in argv if not item.startswith("-")]
    if len(positional) != 2:
        sys.stderr.write("stub grounder: expected input.htn output.sas\n")
        return 1
    pathlib.Path(positional[1]).write_text("stub-grounded-model\n", encoding="utf-8")
    sys.stdout.write("grounding finished\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
