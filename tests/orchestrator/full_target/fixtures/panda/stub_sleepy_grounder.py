#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""pandaPIgrounder stub wanting 3.5s: fine on its own, not after a 2s parser."""

from __future__ import annotations

import pathlib
import sys
import time


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIgrounder stub 0.0-test\n")
        return 0
    time.sleep(3.5)
    positional = [item for item in argv if not item.startswith("-")]
    if len(positional) == 2:
        pathlib.Path(positional[1]).write_text("stub-grounded-model\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
