#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Adversarial stub printing both verdicts. The rejection must win."""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if "--version" in argv:
        sys.stdout.write("pandaPIparser stub 0.0-test\n")
        return 0
    sys.stdout.write("Plan verification result: true\n")
    sys.stdout.write("Plan verification result: false\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
