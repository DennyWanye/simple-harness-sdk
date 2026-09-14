"""Replay coverage for the published code-v1 claim-grading contract.

New default code-v2 safety/observation tests live in gap_phase1. These historical
cases intentionally retain their original semantic-claim behavior and assertions.
"""

import pytest

from agent_orchestrator.governance import domains


@pytest.fixture(autouse=True)
def frozen_legacy_code_domain(monkeypatch):
    monkeypatch.setattr(
        domains,
        "DOMAINS",
        {
            **domains.DOMAINS,
            domains.CODE_DOMAIN: domains.CODE_PROFILE_V1,
        },
    )
