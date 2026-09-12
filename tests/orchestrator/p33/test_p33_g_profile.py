# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""G frozen-profile compatibility oracles, authored before the default switch."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_orchestrator.governance import domains


def test_g_default_is_v4_but_v3_canonical_snapshot_is_unchanged():
    frozen = json.loads(Path(__file__).with_name("doc-profile-v3.json").read_text())
    assert domains.DOC_PROFILE_V3.to_json() == frozen
    assert domains.DOC_PROFILE.version == "4"
    assert domains.resolve_domain(domains.DOC_DOMAIN) == domains.DOC_PROFILE
    expected = {**frozen, "version": "4"}
    assert domains.DOC_PROFILE.to_json() == expected


@pytest.mark.parametrize(
    "version,assess,binding",
    [
        ("1", False, False),
        ("2", False, False),
        ("3", True, False),
        ("4", True, True),
        ("5", False, False),
    ],
)
def test_g_capability_boundary_is_explicit(version, assess, binding):
    profile = replace(domains.DOC_PROFILE, version=version)
    assert domains.supports_document_assessments(profile) is assess
    assert domains.requires_mission_source_binding(profile) is binding
    assert not domains.supports_document_assessments(replace(profile, id=domains.CODE_DOMAIN))
    assert not domains.requires_mission_source_binding(replace(profile, id=domains.CODE_DOMAIN))


@pytest.mark.parametrize("version", ["3", "4"])
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
        replace(domains.DOC_PROFILE, version=version, **change)
