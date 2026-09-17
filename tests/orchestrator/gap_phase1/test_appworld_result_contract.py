"""The example shown to AppWorld result-producing roles must pass real ingress."""

import json

import pytest

from agent_orchestrator.contracts import ContractError, ResultEnvelope
from agent_orchestrator.governance.domains import (
    APPWORLD_PROFILE,
    APPWORLD_PROFILE_V1,
    APPWORLD_PROFILE_V2,
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
    assert resolve_domain("appworld-v1").version == "3"
    restored = DomainProfileV1.from_json(APPWORLD_PROFILE_V1.to_json())
    assert restored.version == "1"
    for role in RESULT_ROLES:
        legacy = template_for_domain(ROLES[role], restored, {})
        current = template_for_domain(ROLES[role], APPWORLD_PROFILE, {})
        assert legacy.prompt_version == f"{role}-appworld-v1"
        assert current.prompt_version == f"{role}-appworld-v3"
        assert {"knowledge_list", "knowledge_read"} <= set(current.tool_names)
        assert "SUPERSEDED" in current.instructions
        previous = template_for_domain(ROLES[role], APPWORLD_PROFILE_V2, {})
        assert previous.prompt_version == f"{role}-appworld-v2"
        assert "knowledge_list" not in previous.tool_names
        assert "used_knowledge只填写当前上下文提供的ID。" in previous.instructions
        example, _ = json.JSONDecoder().raw_decode(
            legacy.instructions[legacy.instructions.index('{"'):]
        )
        with pytest.raises(ContractError, match="unknown fields.*schema_version"):
            ResultEnvelope.from_json({**example, "id": "result-provisional"})
    for role in ("planner", "manager", "critic"):
        assert template_for_domain(ROLES[role], restored, {}) == template_for_domain(
            ROLES[role], APPWORLD_PROFILE, {}
        )


def test_the_hierarchical_worker_pointer_is_beside_the_profile_not_inside_it():
    """P2.3d / defect D1: the hierarchical Worker prompt AppWorld Missions get.

    It is **not** a ``role_templates`` entry: that mapping is read as "role name →
    prompt version" both by ``template_for_domain`` and by callers that iterate it, so
    a ``worker_hierarchical`` key there is a key that breaks both readings.  Keeping
    the pointer beside the profiles also means no profile version has to move: the
    hierarchical mode had no working AppWorld path to replay.
    """

    from agent_orchestrator.governance.domains import HIERARCHICAL_WORKER_TEMPLATES
    from agent_orchestrator.runtime.role_templates import (
        ROLES,
        hierarchical_worker_for_domain,
    )

    assert set(APPWORLD_PROFILE.role_templates) <= set(ROLES), (
        "every key of role_templates names a role; a non-role key breaks both readers"
    )
    assert HIERARCHICAL_WORKER_TEMPLATES["appworld-v1"] == "worker-appworld-hierarchical-v1"
    chosen = hierarchical_worker_for_domain(APPWORLD_PROFILE)
    assert chosen.prompt_version == "worker-appworld-hierarchical-v1"
    assert "appworld_execute" in chosen.tool_names
