# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""G frozen-profile compatibility oracles, authored before the default switch."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_orchestrator.governance import domains


def test_doc8_scope_review_keeps_published_doc7_and_prompt_bytes():
    from agent_orchestrator.runtime.role_templates import TEMPLATE_VERSIONS

    canonical = json.dumps(domains.DOC_PROFILE_V7.to_json(), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "0d4468a4f32bf3e85242fb57dcb2386ee402849ca79140633772dc8174ea1736"
    )
    for role, version, digest in (
        ("worker", "3", "aa9dd98b9dc1ce6d8d3a0622d1c9ab19c8c58717119a5ca4c9a714c0f1e6bf0b"),
        ("critic", "2", "bf5dcf81f3007dbc46292b4fb64f18059822a10e61d9a25abe4dfa64ee940859"),
    ):
        old = TEMPLATE_VERSIONS[role][f"{role}-doc-research-v{version}"]
        assert hashlib.sha256(old.instructions.encode()).hexdigest() == digest
        new = TEMPLATE_VERSIONS[role][domains.DOC_PROFILE.role_templates[role]]
        assert new.tool_names == old.tool_names
        assert new.instructions.startswith(old.instructions)
    old_data = domains.DOC_PROFILE_V7.to_json()
    new_data = domains.DOC_PROFILE.to_json()
    for key in ("version", "role_templates"):
        old_data.pop(key)
        new_data.pop(key)
    assert old_data == new_data


def test_g_successor_preserves_v3_and_v4_canonical_snapshots():
    frozen = json.loads(Path(__file__).with_name("doc-profile-v3.json").read_text())
    assert domains.DOC_PROFILE_V3.to_json() == frozen
    assert domains.DOC_PROFILE_V6.version == "6"
    assert domains.DOC_PROFILE.version == "9"
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
    expected_v6 = {
        **expected_v5,
        "version": "6",
        "role_templates": {
            role: f"{role}-doc-research-v{'2' if role == 'critic' else '3'}"
            for role in frozen["role_templates"]
        },
    }
    assert domains.DOC_PROFILE_V6.to_json() == expected_v6
    assert domains.DOC_PROFILE_V7.to_json() == {
        **expected_v6,
        "version": "7",
        "role_templates": {
            **expected_v6["role_templates"],
            "manager": "manager-doc-research-v4",
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
        ("7", True, True, True),
        ("8", True, True, True),
        ("9", True, True, True),
        ("10", False, False, False),
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


def test_doc9_preserves_frozen_doc8_profile():
    canonical = json.dumps(domains.DOC_PROFILE_V8.to_json(), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "4a38110eeb434a7842201d4f7b6ebe0ab9c4957122bbedccefc272f9e908b76c"
    )
