# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Reuse the existing real Commit Service graph fixture."""

import sys
from pathlib import Path

STEP07 = Path(__file__).resolve().parents[1] / "step07"
if str(STEP07) not in sys.path:
    sys.path.insert(0, str(STEP07))
