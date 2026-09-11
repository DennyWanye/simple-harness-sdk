# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Host-support 0.9.8 tests reuse the step-4 knowledge helpers (Commit-Service-level
dispute construction)."""

from __future__ import annotations

import sys
from pathlib import Path

STEP04 = Path(__file__).resolve().parents[1] / "step04"
if str(STEP04) not in sys.path:
    sys.path.insert(0, str(STEP04))
