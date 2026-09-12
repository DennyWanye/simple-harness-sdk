# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""G frozen-profile compatibility oracles, authored before the default switch."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_orchestrator.governance import domains


def test_g_successor_preserves_v3_and_v4_canonical_snapshots():
    frozen = json.loads(Path(__file__).with_name("doc-profile-v3.json").read_text())
    assert domains.DOC_PROFILE_V3.to_json() == frozen
    assert domains.DOC_PROFILE.version == "6"
    assert domains.resolve_domain(domains.DOC_DOMAIN) == domains.DOC_PROFILE
    expected = {**frozen, "version": "4"}
    assert domains.DOC_PROFILE_V4.to_json() == expected
    expected_v5 = {
        **expected,
        "version": "5",
        "planner_floor": ["format_check", "rule_check", "critic_review"],
        "role_templates": {role: f"{role}-doc-research-v2" for role in frozen["role_templates"]},
    }
    assert domains.DOC_PROFILE_V5.to_json() == expected_v5
    assert domains.DOC_PROFILE.to_json() == {
        **expected_v5,
        "version": "6",
        "role_templates": {
            role: f"{role}-doc-research-v{'2' if role == 'critic' else '3'}"
            for role in frozen["role_templates"]
        },
    }


@pytest.mark.parametrize(
    "version,assess,binding,critic_proof",
    [
        ("1", False, False, False),
        ("2", False, False, False),
        ("3", True, False, False),
        ("4", True, True, False),
        ("5", True, True, True),
        ("6", True, True, True),
        ("7", False, False, False),
    ],
)
def test_g_capability_boundary_is_explicit(version, assess, binding, critic_proof):
    profile = getattr(domains, f"DOC_PROFILE_V{version}", None)
    if profile is None:
        profile = replace(domains.DOC_PROFILE, version=version)
    assert domains.supports_document_assessments(profile) is assess
    assert domains.requires_mission_source_binding(profile) is binding
    assert domains.requires_document_critic_proof(profile) is critic_proof
    assert not domains.supports_document_assessments(replace(profile, id=domains.CODE_DOMAIN))
    assert not domains.requires_mission_source_binding(replace(profile, id=domains.CODE_DOMAIN))
    assert not domains.requires_document_critic_proof(replace(profile, id=domains.CODE_DOMAIN))


@pytest.mark.parametrize("version", ["3", "4", "5", "6"])
@pytest.mark.parametrize(
    "change",
    [
        dict(adapters={}),
        dict(completion_rules={}),
        dict(
            completion_rules={
                "inconclusive_retry_limit": True,
                "inconclusive_share_limit": 0.5,
                "require_limitations": True,
            }
        ),
    ],
)
def test_g_v3_v4_reject_same_malformed_completion_contract(version, change):
    with pytest.raises(ValueError):
        replace(getattr(domains, f"DOC_PROFILE_V{version}"), **change)
