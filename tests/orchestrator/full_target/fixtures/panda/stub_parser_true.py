#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIparser stub: verification says true; translate and convert succeed.

The adapter runs tools with a minimal environment, so behaviour is baked into
each stub script rather than switched by an environment variable.
"""

from __future__ import annotations

import pathlib
import sys


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIparser stub 0.0-test\n")
        return 0
    positional = [item for item in argv if not item.startswith("-")]
    if "--verify" in argv:
        sys.stderr.write("Mode: plan verification\n")
        sys.stdout.write("Plan verification result: true\n")
        return 0
    if "-c" in argv:
        if len(positional) != 2:
            sys.stderr.write("stub parser: converter needs two files\n")
            return 1
        pathlib.Path(positional[1]).write_text(
            pathlib.Path(positional[0]).read_text(encoding="utf-8"), encoding="utf-8"
        )
        sys.stdout.write("plan back-translation done\n")
        return 0
    if len(positional) == 3:
        pathlib.Path(positional[2]).write_text("stub-parsed-model\n", encoding="utf-8")
        return 0
    sys.stderr.write("stub parser: unexpected arguments\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
