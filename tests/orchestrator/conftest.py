# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the orchestrator test tree; reuses the BaseAgent test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

AGENT_FIXTURES = Path(__file__).resolve().parents[1] / "agents"
if str(AGENT_FIXTURES) not in sys.path:
    sys.path.insert(0, str(AGENT_FIXTURES))
