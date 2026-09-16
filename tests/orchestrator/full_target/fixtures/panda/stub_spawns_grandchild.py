#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Stub that leaves a grandchild behind and then hangs.

Killing only the direct child lets the grandchild survive and drop its marker.
A session-wide kill reaps both, so the marker never appears.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

# A literal path: the adapter runs tools with a minimal environment and its
# own TMPDIR, so tempfile.gettempdir() here would not be the test's tempdir.
MARKER = pathlib.Path("/tmp/sh-panda-grandchild.marker")  # noqa: S108
GRANDCHILD = (
    "import pathlib, sys, time; time.sleep(3); "
    "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
)


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("grandchild stub 0.0-test\n")
        return 0
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", GRANDCHILD, str(MARKER)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
