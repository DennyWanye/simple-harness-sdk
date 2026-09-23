# SPDX-License-Identifier: Apache-2.0
"""A review turn can read what a real reviewer reads before concluding.

Host real-model run 13 (2026-09-23): 3+2+5 = 10 evidence tool calls exceeded the
old per-turn cap of 8 and every such review turn failed. The turn cap now equals
the gateway binding cap, and leaves room for a final answering model call.
"""

from agent_orchestrator.orchestrator.assurance_review_runtime import (
    REVIEW_MODEL_CALLS,
    REVIEW_TOOL_CALLS,
)
from agent_orchestrator.verification.reviewer_evidence_tools import MAX_EVIDENCE_TOOL_CALLS


def test_turn_tool_cap_matches_gateway_cap_and_covers_the_observed_reviewer():
    assert REVIEW_TOOL_CALLS == MAX_EVIDENCE_TOOL_CALLS
    assert REVIEW_TOOL_CALLS >= 3 + 2 + 5
    # observed: three tool rounds, then one answering call
    assert REVIEW_MODEL_CALLS >= 3 + 1
