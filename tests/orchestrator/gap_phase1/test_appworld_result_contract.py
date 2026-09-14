"""The example shown to AppWorld result-producing roles must pass real ingress."""

import json

import pytest

from agent_orchestrator.contracts import ContractError, ResultEnvelope
from agent_orchestrator.governance.domains import (
    APPWORLD_PROFILE,
    APPWORLD_PROFILE_V1,
    DomainProfileV1,
    resolve_domain,
)
from agent_orchestrator.runtime.role_templates import ROLES, template_for_domain

RESULT_ROLES = (
    "worker", "arbiter", "synthesizer", "explorer", "exploiter", "simplifier",
    "connector", "failure_analyst",
)


@pytest.mark.parametrize("role", RESULT_ROLES)
def test_published_appworld_result_example_passes_strict_ingress(role):
    prompt = template_for_domain(ROLES[role], APPWORLD_PROFILE, {}).instructions
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"'):])
    # Ingress supplies its own result identity; the Agent supplies the other fields.
    envelope = ResultEnvelope.from_json({**example, "id": "result-provisional"})
    assert envelope.outcome == "candidate"


def test_new_default_and_frozen_legacy_keep_distinct_contract_versions():
    assert resolve_domain("appworld-v1").version == "2"
    restored = DomainProfileV1.from_json(APPWORLD_PROFILE_V1.to_json())
    assert restored.version == "1"
    for role in RESULT_ROLES:
        legacy = template_for_domain(ROLES[role], restored, {})
        current = template_for_domain(ROLES[role], APPWORLD_PROFILE, {})
        assert legacy.prompt_version == f"{role}-appworld-v1"
        assert current.prompt_version == f"{role}-appworld-v2"
        example, _ = json.JSONDecoder().raw_decode(
            legacy.instructions[legacy.instructions.index('{"'):]
        )
        with pytest.raises(ContractError, match="unknown fields.*schema_version"):
            ResultEnvelope.from_json({**example, "id": "result-provisional"})
    for role in ("planner", "manager", "critic"):
        assert template_for_domain(ROLES[role], restored, {}) == template_for_domain(
            ROLES[role], APPWORLD_PROFILE, {}
        )
