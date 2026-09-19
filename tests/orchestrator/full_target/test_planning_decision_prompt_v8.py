# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-E red tests: ``planner-hierarchical-v8``, package-4 pairing, frozen digests.

Slice H1-E adds the v8 hierarchical Planner prompt for the new planning-decision
protocol (V2 plan §9 and §41, ruling addendum §7–§8), the *integer* package version
4 it is written against, and the digest-freeze bookkeeping H0 flagged as missing.

What this file pins, in order:

1. the twelve mandatory §41 points, each by its literal key phrase;
2. the §13/§24 minimal-legal JSON example the prompt carries — parseable, with the
   exact core-field set, and with the one reference that the already-merged
   ``contracts.planning_decisions`` core can validate;
3. the pairing rule: v8 <-> package 4, package 4 refuses v7, the default stays 3;
4. every *existing* prompt still hashes to the value H0 recorded, read from
   ``plans/llm-native-htn/H0/prompt-digests.json`` rather than from a copy of it;
5. the three prompt-freeze gaps H0 recorded are closed by this slice.

Red first: the module-level imports below only reference symbols that already exist
today; the new ones are read as attributes so each missing piece fails its own test
rather than the whole module at collection time.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_root_review_user_goal as root_review_user_goal  # noqa: E402
from test_output_port_claims import FROZEN_PROMPT_DIGESTS  # noqa: E402

from agent_orchestrator.contracts.planning_decisions import (  # noqa: E402
    PlanningDecisionType,
    PlanningRefKind,
    PlanningRefV1,
)
from agent_orchestrator.runtime import role_templates  # noqa: E402
from agent_orchestrator.runtime.role_templates import TEMPLATE_VERSIONS  # noqa: E402

hierarchical_planner_pairing_is_valid = getattr(
    role_templates, "hierarchical_planner_pairing_is_valid", None
)

REPO_ROOT = Path(__file__).resolve().parents[3]
H0_DIGESTS = REPO_ROOT / "plans" / "llm-native-htn" / "H0" / "prompt-digests.json"

#: §13's core-field set, verbatim.  The envelope type itself is not in the merged
#: contract core yet (it waits on H1-A2), so the example can only be checked structurally.
CORE_FIELDS = {
    "schema_version",
    "decision_type",
    "subject_key",
    "rationale",
    "reason_refs",
    "assumptions",
    "payload",
    "uncertainties",
    "alternatives",
    "replan_triggers",
}


def _digest(template) -> str:
    return hashlib.sha256(template.instructions.encode("utf-8")).hexdigest()


def _registered_template(version: str):
    for templates in TEMPLATE_VERSIONS.values():
        if version in templates:
            return templates[version]
    return None


# ======================================================================================
# 1. The twelve §41 points, each keyed by the phrase the plan names
# ======================================================================================


POINT_PHRASES: list[tuple[str, list[str]]] = [
    ("1 只提出一个决定", ["只提出一个决定"]),
    (
        "2 只输出一个 <planning_decision> 块",
        ["只输出一个 <planning_decision>", "</planning_decision>"],
    ),
    ("3 类型只能取 enabled_decision_types", ["enabled_decision_types"]),
    ("4 subject_key 只照抄", ["subject_key", "照抄"]),
    (
        "5 引用只能从 visible_refs 完整照抄四元组",
        ["visible_refs", "四元组", "semantic_revision", "content_hash"],
    ),
    (
        "6 禁止系统字段并列出字段名",
        ["系统字段", "mission_id", "plan_revision", "decision_id", "request_id"],
    ),
    (
        "7 本阶段不能请求取证，证明不了就受阻或不改",
        ["REQUEST_EVIDENCE", "DECLARE_BLOCKED", "NO_CHANGE"],
    ),
    (
        "8 被拒的展开用一个 REPAIR/REPLACE_METHOD",
        ["rejected_refinements", "REPAIR", "REPLACE_METHOD"],
    ),
    ("9 无可用方法用 DECLARE_BLOCKED 交系统合成", ["DECLARE_BLOCKED", "合成"]),
    ("10 不输出内部思维链", ["思维链"]),
    ("11 块外禁止文字", ["块外不要输出任何文字"]),
]


def test_v8_is_registered_as_its_own_hierarchical_planner_version() -> None:
    assert role_templates.PLANNER_HIERARCHICAL_V8.prompt_version == "planner-hierarchical-v8"
    assert (
        TEMPLATE_VERSIONS["planner"]["planner-hierarchical-v8"]
        is role_templates.PLANNER_HIERARCHICAL_V8
    )
    assert role_templates.PLANNER_HIERARCHICAL_V8.name == "planner"


def test_v8_text_covers_every_one_of_the_twelve_points() -> None:
    text = role_templates.PLANNER_HIERARCHICAL_V8.instructions
    for point, phrases in POINT_PHRASES:
        for phrase in phrases:
            assert phrase in text, f"§41 point {point}: prompt is missing {phrase!r}"


def test_v8_does_not_induce_demoted_operations() -> None:
    text = role_templates.PLANNER_HIERARCHICAL_V8.instructions
    assert "BIND_EXISTING_GOAL" not in text
    assert "PROPOSE_SUCCESSOR" not in text
    assert "goal_ref" not in text
    assert "resolution_ref" not in text


# ======================================================================================
# 2. The minimal legal example: parseable, field-complete, reference-valid
# ======================================================================================


def _example_json() -> dict:
    lines = role_templates.PLANNER_HIERARCHICAL_V8.instructions.splitlines()
    marker = next(i for i, line in enumerate(lines) if "最小合法示例" in line)
    return json.loads(lines[marker + 1])


def test_the_example_is_parseable_and_carries_exactly_the_core_fields() -> None:
    example = _example_json()
    assert set(example) == CORE_FIELDS
    assert example["schema_version"] == 1
    assert example["decision_type"] in {member.value for member in PlanningDecisionType}
    assert isinstance(example["reason_refs"], list)
    assert isinstance(example["payload"], dict)


def test_the_example_ref_is_a_valid_four_tuple_the_merged_core_accepts() -> None:
    payload = _example_json()["payload"]
    assert set(payload) == {"method_ref", "bindings"}
    ref = PlanningRefV1.from_json(payload["method_ref"])
    assert ref.kind is PlanningRefKind.METHOD
    assert ref.to_json() == payload["method_ref"]


# ======================================================================================
# 3. Pairing: v8 <-> package 4, package 4 refuses v7, the default stays 3
# ======================================================================================


def test_package_four_is_new_and_the_default_package_stays_three() -> None:
    assert role_templates.PLANNING_DECISION_PACKAGE_VERSION == 4
    assert role_templates.HIERARCHICAL_PLANNER_PACKAGE_VERSION == 3
    assert role_templates.HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE[4] == frozenset(
        {"planner-hierarchical-v8"}
    )


def test_v8_pairs_only_with_package_four_and_package_four_refuses_v7() -> None:
    assert hierarchical_planner_pairing_is_valid("planner-hierarchical-v8", 4) is True
    assert hierarchical_planner_pairing_is_valid("planner-hierarchical-v7", 4) is False
    assert hierarchical_planner_pairing_is_valid("planner-hierarchical-v8", 3) is False
    assert role_templates.hierarchical_planner_versions(4) == frozenset(
        {"planner-hierarchical-v8"}
    )
    # The mode-level set is the union of every group, so nothing is orphaned.
    assert "planner-hierarchical-v8" in role_templates.HIERARCHICAL_PLANNER_VERSIONS
    assert frozenset().union(
        *role_templates.HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE.values()
    ) == role_templates.HIERARCHICAL_PLANNER_VERSIONS


# ======================================================================================
# 4. Every prompt H0 froze keeps its bytes (read the expectation from H0, not a copy)
# ======================================================================================


def test_every_existing_prompt_still_matches_the_h0_recorded_digest() -> None:
    record = json.loads(H0_DIGESTS.read_text(encoding="utf-8"))
    assert record["head_short"] == "7f839f0"
    for version, expected in sorted(record["prompt_shas"].items()):
        template = _registered_template(version)
        assert template is not None, f"{version} is no longer registered"
        assert _digest(template) == expected, (
            f"{version} changed its bytes; a shipped prompt is never edited in place"
        )


# ======================================================================================
# 5. The three H0 prompt-freeze gaps are closed by this slice
# ======================================================================================


def test_the_three_h0_freeze_gaps_are_now_closed() -> None:
    record = json.loads(H0_DIGESTS.read_text(encoding="utf-8"))
    assert record["prompt_freeze_gaps"] == [
        "planner-hierarchical-v1 not in FROZEN_PROMPT_DIGESTS",
        "planner-hierarchical-v2 not in FROZEN_PROMPT_DIGESTS",
        "root-reviewer-v3 has no FROZEN_* literal",
    ]
    sha = record["prompt_shas"]
    assert FROZEN_PROMPT_DIGESTS["PLANNER_HIERARCHICAL_V1"] == (
        "planner-hierarchical-v1",
        sha["planner-hierarchical-v1"],
    )
    assert FROZEN_PROMPT_DIGESTS["PLANNER_HIERARCHICAL"] == (
        "planner-hierarchical-v2",
        sha["planner-hierarchical-v2"],
    )
    assert root_review_user_goal.FROZEN_ROOT_REVIEWER_V3 == sha["root-reviewer-v3"]
    assert _digest(role_templates.ROOT_REVIEWER) == root_review_user_goal.FROZEN_ROOT_REVIEWER_V3


def test_v8_itself_is_in_the_frozen_table_with_its_live_digest() -> None:
    version, digest = FROZEN_PROMPT_DIGESTS["PLANNER_HIERARCHICAL_V8"]
    assert version == "planner-hierarchical-v8"
    assert _digest(role_templates.PLANNER_HIERARCHICAL_V8) == digest


def test_the_prompt_document_carries_the_code_text_verbatim() -> None:
    """``plans/llm-native-htn/H1/prompt-v8.md`` must not drift from the code.

    The doc is the human-readable record of the shipped words; the fenced block it
    carries has to equal ``PLANNER_HIERARCHICAL_V8.instructions`` byte for byte, or
    the two accounts of the same prompt disagree.
    """

    import re

    doc = (REPO_ROOT / "plans" / "llm-native-htn" / "H1" / "prompt-v8.md").read_text(
        encoding="utf-8"
    )
    match = re.search(r"```text\n(.*?)```\n", doc, re.S)
    assert match is not None, "prompt-v8.md lost its verbatim prompt block"
    assert match.group(1) == role_templates.PLANNER_HIERARCHICAL_V8.instructions
