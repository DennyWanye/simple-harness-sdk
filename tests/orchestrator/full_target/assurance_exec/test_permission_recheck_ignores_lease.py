# SPDX-License-Identifier: Apache-2.0
"""A final re-check compares the grant, not its lease.

Host real-model run 10 (2026-09-23, Grok lane): every review preparation failed
with RECHECK_REQUIRED at commit because the production authority re-issues
``not_after_ms`` relative to *now*, and all four barriers compared the whole
``CurrentReadPermission``. The seam fixture used a fixed deadline, so no seam saw it.
"""

from __future__ import annotations

import pytest

from agent_orchestrator.assurance.certificates import UseIdentity
from agent_orchestrator.assurance.codec import AssuranceError
from agent_orchestrator.assurance.evidence import ReadItem
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.assurance.root_gate import CurrentReadPermission
from agent_orchestrator.orchestrator.assurance_check_use import _require_same_permission

REF = AssuranceRef("result", Pin("result-1", 0, "1" * 64))
IDENTITY = UseIdentity("mission-1", "REVIEW", "review-key", "scope-1", "user", "DISCLOSE", "root-1")
TTL = 60_000


def _authority(state):
    def authority(identity, ref):
        return CurrentReadPermission(
            ReadItem("ACCESS", ref.key, state["access"]),
            ReadItem("POLICY", "policy", "f" * 64),
            state["now"] + TTL,  # production shape: lease relative to now
        )
    return authority


def test_same_grant_later_millisecond_passes():
    state = {"access": "a" * 64, "now": 1_000}
    authority = _authority(state)
    captured = authority(IDENTITY, REF)
    state["now"] = 1_250
    _require_same_permission(authority, IDENTITY, REF, captured, 1_250)


def test_changed_access_is_recheck():
    state = {"access": "a" * 64, "now": 1_000}
    authority = _authority(state)
    captured = authority(IDENTITY, REF)
    state["access"], state["now"] = "b" * 64, 1_250
    with pytest.raises(AssuranceError) as raised:
        _require_same_permission(authority, IDENTITY, REF, captured, 1_250)
    assert raised.value.code == "RECHECK_REQUIRED"


def test_captured_lease_still_bounds_the_use():
    state = {"access": "a" * 64, "now": 1_000}
    authority = _authority(state)
    captured = authority(IDENTITY, REF)
    state["now"] = 1_000 + TTL
    with pytest.raises(AssuranceError) as raised:
        _require_same_permission(authority, IDENTITY, REF, captured, 1_000 + TTL)
    assert raised.value.code == "CHECK_USE_EXPIRED"
