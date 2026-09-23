# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Make the shared ``tests/agents`` fixtures (provider_fixture, ...) importable here."""

from __future__ import annotations

import sys
from pathlib import Path

_AGENTS = str(Path(__file__).resolve().parents[1])
if _AGENTS not in sys.path:
    sys.path.insert(0, _AGENTS)
