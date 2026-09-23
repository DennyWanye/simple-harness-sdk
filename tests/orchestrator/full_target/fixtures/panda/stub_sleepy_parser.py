#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIparser stub that burns 2s of the shared pipeline budget."""

from __future__ import annotations

import pathlib
import sys
import time


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIparser stub 0.0-test\n")
        return 0
    time.sleep(2.0)
    positional = [item for item in argv if not item.startswith("-")]
    if len(positional) == 3:
        pathlib.Path(positional[2]).write_text("stub-parsed-model\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
