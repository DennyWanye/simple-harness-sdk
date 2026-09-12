# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P3.3 tests reuse the step-7 ledger helpers (one ACTIVE Mission, a Task, the people
who decide) the same way the P3.2 suite does."""

from __future__ import annotations

import sys
from pathlib import Path

STEP07 = Path(__file__).resolve().parents[1] / "step07"
if str(STEP07) not in sys.path:
    sys.path.insert(0, str(STEP07))
