"""D03: deterministic uncertainty conflicts do not replace normal C arbitration."""

from dataclasses import replace

import pytest

from agent_orchestrator.contracts import Claim, ClaimStatus


def claim(cid, stance="affirms", status=ClaimStatus.UNDER_REVIEW, **changes):
    base = Claim(
        id=cid,
        content="a claim",
        type="statement",
        status=status,
        source_task="task-" + cid,
        source_attempt="attempt-" + cid,
        evidence=(),
        dependencies=(),
        verifier_results=(),
        confidence_metadata={},
        supersedes=None,
        mission_id="mission",
        result_id="result-" + cid,
        key="same",
        stance=stance,
    )
    return replace(base, **changes)


@pytest.mark.parametrize("existing", [False, True])
def test_uncertain_opposite_stance_is_a_failure_not_acceptable_uncertainty(existing):
    from agent_orchestrator.verification.conflicts import uncertainty_conflicts

    a, b = claim("a"), claim("b", "refutes", ClaimStatus.SUPPORTED)
    rows = uncertainty_conflicts(
        proposed=[a] if existing else [a, b],
        inconclusive_claim_ids=frozenset({a.id}),
        existing=[b] if existing else [],
    )
    assert len(rows) == 1
    assert rows[0]["claim_id"] == "a" and rows[0]["other_claim_id"] == "b"
    assert rows[0]["other_claim_revision"] == b.version
    assert rows[0]["reason"] == "opposite_stance"


def test_two_supported_sides_keep_normal_conflict_task_path():
    from agent_orchestrator.verification.conflicts import uncertainty_conflicts

    assert (
        uncertainty_conflicts(
            proposed=[claim("a", status=ClaimStatus.SUPPORTED)],
            inconclusive_claim_ids=frozenset(),
            existing=[claim("b", "refutes", ClaimStatus.SUPPORTED)],
        )
        == []
    )


@pytest.mark.parametrize(
    "change",
    [
        {"mission_id": "other"},
        {"key": "different"},
        {"status": ClaimStatus.SUPERSEDED},
        {"status": ClaimStatus.UNDER_REVIEW},
    ],
)
def test_ineligible_existing_claims_do_not_block_uncertainty(change):
    from agent_orchestrator.verification.conflicts import uncertainty_conflicts

    a, b = claim("a"), replace(claim("b", "refutes", ClaimStatus.SUPPORTED), **change)
    assert (
        uncertainty_conflicts(proposed=[a], inconclusive_claim_ids=frozenset({a.id}), existing=[b])
        == []
    )
