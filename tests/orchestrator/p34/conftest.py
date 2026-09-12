# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Reuse the existing real source/verification drivers, without copying receipts."""

import sys
from pathlib import Path

for name in ("p33", "step07"):
    directory = str(Path(__file__).resolve().parents[1] / name)
    if directory not in sys.path:
        sys.path.insert(0, directory)

from test_p33_source_commits import e_scenes  # noqa: E402,F401
