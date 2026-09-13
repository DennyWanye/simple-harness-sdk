# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Expose the established step-06 fixture builders to the standalone P36 selector."""

from __future__ import annotations

import sys
from pathlib import Path

STEP06 = Path(__file__).resolve().parents[1] / "step06"
if str(STEP06) not in sys.path:
    sys.path.insert(0, str(STEP06))
