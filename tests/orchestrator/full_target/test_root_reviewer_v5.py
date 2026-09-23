"""Root-reviewer v5 fixes the live delimiter without moving replayable prompts."""

from __future__ import annotations

import pytest

from agent_orchestrator.contracts import ContractError
from agent_orchestrator.runtime.role_templates import (
    ROOT_REVIEWER,
    ROOT_REVIEWER_V1,
    ROOT_REVIEWER_V2,
    ROOT_REVIEWER_V3,
    ROOT_REVIEWER_V4,
    template_for,
)
from agent_orchestrator.verification.critics import parse_critic_verdict


def test_root_reviewer_v5_uses_only_valid_delimiters_and_preserves_prior_versions() -> None:
    assert ROOT_REVIEWER.prompt_version == "root-reviewer-v5"
    assert "<critic_verdict>" in ROOT_REVIEWER.instructions
    assert "</critic_verdict>" in ROOT_REVIEWER.instructions
    assert "<cricit_verdict>" not in ROOT_REVIEWER.instructions
    assert '"verdict":"FAIL"' in ROOT_REVIEWER.instructions
    assert '"mission_criteria"' in ROOT_REVIEWER.instructions

    for version, template in (
        ("root-reviewer-v1", ROOT_REVIEWER_V1),
        ("root-reviewer-v2", ROOT_REVIEWER_V2),
        ("root-reviewer-v3", ROOT_REVIEWER_V3),
        ("root-reviewer-v4", ROOT_REVIEWER_V4),
    ):
        assert template_for(ROOT_REVIEWER, {"root_reviewer": version}) is template


def test_root_reviewer_v5_example_is_accepted_and_malformed_tag_remains_strict() -> None:
    valid = (
        '<critic_verdict>{"verdict":"FAIL","findings":[{"severity":"blocker",'
        '"detail":"missing evidence"}],"mission_criteria":[{"criterion":"c-1",'
        '"met":false,"reason":"missing evidence"}]}</critic_verdict>'
    )
    assert parse_critic_verdict(valid, expected_criteria=["c-1"]).verdict == "FAIL"
    with pytest.raises(ContractError, match="critic verdict unreadable"):
        parse_critic_verdict(
            valid.replace("critic_verdict", "cricit_verdict"), expected_criteria=["c-1"]
        )
