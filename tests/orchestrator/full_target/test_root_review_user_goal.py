# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3k / defect N2: the root reviewer is shown the user's goal, not only the template.

Grok C2-r0 (and C2-r1, C4-r0, C4-r1, identical in shape): the root review package
carried ``goal_statement = "make the named failing test pass and explain the change"``
— the goal *signature's* template sentence — and nothing the user had written.  The
Mission's goal was "让 ingest 流水线端到端跑通，坏行要留痕不要丢", its ``failing_test``
parameter pointed at ``tests/test_public_pipeline.py``, a suite that is green by
construction, and the hidden tests a Worker cannot see are where the real acceptance
lives.  So ``c-test-passes``'s evidence requirement — "the report shows the named
failing test now passes" — was unsatisfiable by any Worker, and the reviewer, reading
only the template, rejected a patch the hidden grader then passed (C2-r1, C4-r0).

The reviewer was right about what it was shown.  The fix is on what it is shown:
``RootReviewRequest`` now carries ``mission_goal`` (the Mission's goal, verbatim) and
``goal_parameters`` (the root binding's typed parameters), both inside the hashed
request so the intent's ``context_version`` covers them; and ``root-reviewer-v3`` says
that ``evidence_requirement`` is the method author's wording, to be read against
``mission_goal`` — and, when the named test was green at baseline, to ask instead for
a test covering the user's goal going red-to-green or added and passing.  The seed
wording is deliberately *not* softened (see journal §2m): on a genuine failing test
(C3) it is exactly the right requirement.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import FAILING_TEST, REPOSITORY  # noqa: E402
from test_root_review_evidence import CodeWorld, _c3_artifacts  # noqa: E402

from agent_orchestrator.orchestrator.root_review import (  # noqa: E402
    PATH_PLACEHOLDER,
    WORKSPACE_PLACEHOLDER,
    RootReviewRequest,
    sanitised_goal_parameters,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "htn" / "c2_root_review"

#: sha256 of ``root-reviewer-v2``'s instructions, frozen (§26.3): the Grok batch-2
#: episodes replay on it, so v3 is registered beside it and v2 is never edited.
FROZEN_ROOT_REVIEWER_V2 = "75debfd9640f1c635b808cdaf7657168ce10782d9e0cd93f08450fdc0d744c76"

#: H1-E closes H0's third freeze gap: v3 (the live root reviewer) was registered but
#: had no literal anywhere.  The digest is the value H0 recorded in
#: ``plans/llm-native-htn/H0/prompt-digests.json``; registering it here means a future
#: edit of v3's words fails loudly instead of silently rewriting a shipped prompt.
FROZEN_ROOT_REVIEWER_V3 = "21a7814076b72957f41c47bf21fb340d2c2243e3fc0687fb397a143373930980"


# ======================================================================================
# 1. The defect, pinned by the real C2-r0 package
# ======================================================================================


def test_the_c2_package_before_the_fix_showed_the_template_and_not_the_user_goal() -> None:
    before = json.loads((FIXTURE / "package_before.json").read_text())
    verdict = json.loads((FIXTURE / "verdict_before.json").read_text())
    goal = json.loads((FIXTURE / "mission_goal.json").read_text())
    assert before["goal_statement"] == "make the named failing test pass and explain the change"
    assert "mission_goal" not in before and "goal_parameters" not in before
    # The user's goal names no failing test at all; the parameter names a green suite.
    assert "failing" not in goal["mission_goal"].lower()
    assert goal["goal_parameters"]["failing_test"] == "tests/test_public_pipeline.py"
    # And the reviewer, reading the template, rejected on exactly that wording.
    assert verdict["verdict"] == "REJECTED"
    assert verdict["criteria"] == {"c-change-explained": "PASS", "c-test-passes": "FAIL"}
    finding = verdict["findings"][0]["detail"]
    assert "named failing test" in finding and "already green" in finding
    requirement = next(
        cover["evidence_requirement"]
        for item in before["criteria"]
        if item["criterion_id"] == "c-test-passes"
        for cover in item["covered_by"]
    )
    assert "named failing test" in requirement


# ======================================================================================
# 2. The request carries the user's goal and the root parameters, inside the hash
# ======================================================================================


def _c3(tmp_path) -> CodeWorld:
    world = CodeWorld(tmp_path)
    for index, (step, (path, data)) in enumerate(_c3_artifacts().items()):
        world.deliver(step, path=path, data=data, now_ms=1_000_000 + index * 1_000)
    world.dispatch.issue_input_witnesses(
        world.mission.id, world.dispatch.network(world.mission.id), now_ms=1_100_000
    )
    world.coordinator().cut(world.mission.id, now_ms=2_000_000)
    return world


def test_the_request_carries_the_mission_goal_and_the_root_parameters(tmp_path) -> None:
    world = _c3(tmp_path)
    shown = world.request().to_json()
    assert shown["mission_goal"] == world.mission.goal
    assert shown["goal_parameters"] == {"repository": REPOSITORY, "failing_test": FAILING_TEST}
    # The template sentence is still there, named for what it is.
    assert shown["goal_statement"] == "make the named failing test pass and explain the change"


def test_the_user_goal_is_covered_by_the_request_hash(tmp_path) -> None:
    """``content_hash()`` is the intent's ``context_version``: a reviewer asked about a
    package must have been asked with the goal in it, or the record would not say so."""

    world = _c3(tmp_path)
    request = world.request()
    without = RootReviewRequest(
        package_id=request.package_id,
        goal_task_id=request.goal_task_id,
        goal_statement=request.goal_statement,
        criteria=request.criteria,
        contributions=request.contributions,
        requirements_revision=request.requirements_revision,
        schema_feedback=request.schema_feedback,
        requirements_revision_semantics=request.requirements_revision_semantics,
        mission_goal="",
        goal_parameters=request.goal_parameters,
    )
    assert without.content_hash() != request.content_hash()
    assert without.to_json()["mission_goal"] == ""
    assert "goal_parameters" in without.to_json()


def test_the_sealed_intent_shows_the_reviewer_the_same_goal(tmp_path) -> None:
    """What the model reads is the request's JSON, verbatim (P2.3h: excerpts travel in
    the body); the goal has to be in *that* and not only in the typed object."""

    import asyncio

    from test_root_review_coordinator import _orchestrator
    from test_root_review_evidence import _plan_world_with_evidence

    world = _plan_world_with_evidence(
        tmp_path, review_text="verdict: PASS — c-root is satisfied", key="p23k-n2-sealed"
    )
    world.store.close()

    async def case() -> dict[str, Any]:
        async with _orchestrator(tmp_path) as loop:
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            coordination = loop._root_review(mission, loop._new_mode(mission))
            coordination.cut(mission.id, now_ms=2_000_000)
            package = coordination.live_package(mission.id)
            assert package is not None
            await loop._ask_root_reviewer(mission, coordination, package)
            intent = loop.store.get_intent_for_subject(
                f"{mission.id}:root-review:{package.package_id}:1"
            )
            assert intent is not None
            return json.loads(intent.config["message"]["content"])

    shown = asyncio.run(case())
    assert shown["mission_goal"] == world.mission.goal == "交付一个可验收的层次计划"
    assert shown["goal_parameters"] == {"subject": "alpha"}


# ======================================================================================
# 3. The prompt: v3 explains the field; v2 keeps its bytes
# ======================================================================================


def test_the_prompt_v3_reads_the_requirement_against_the_user_goal_and_v2_is_frozen() -> None:
    from agent_orchestrator.runtime.role_templates import (
        ROOT_REVIEWER,
        ROOT_REVIEWER_V1,
        ROOT_REVIEWER_V2,
        ROOT_REVIEWER_V2_VERSION,
        ROOT_REVIEWER_VERSION,
        TEMPLATE_VERSIONS,
        template_for,
    )

    assert ROOT_REVIEWER.prompt_version == ROOT_REVIEWER_VERSION == "root-reviewer-v3"
    for field in ("mission_goal", "goal_parameters"):
        assert field in ROOT_REVIEWER.instructions, field
        assert field not in ROOT_REVIEWER_V2.instructions, field
        assert field not in ROOT_REVIEWER_V1.instructions, field
    # The rule itself: the author's wording yields to the user's goal, and a named
    # test that was green at baseline turns into "show a test covering the goal".
    assert "以 mission_goal 为准" in ROOT_REVIEWER.instructions
    assert "基线" in ROOT_REVIEWER.instructions and "由红转绿" in ROOT_REVIEWER.instructions
    assert ROOT_REVIEWER_V2.prompt_version == ROOT_REVIEWER_V2_VERSION == "root-reviewer-v2"
    assert (
        hashlib.sha256(ROOT_REVIEWER_V2.instructions.encode("utf-8")).hexdigest()
        == FROZEN_ROOT_REVIEWER_V2
    )
    # H1-E closes H0's third freeze gap: v3, the live template, now has a literal too.
    assert (
        hashlib.sha256(ROOT_REVIEWER.instructions.encode("utf-8")).hexdigest()
        == FROZEN_ROOT_REVIEWER_V3
    )
    assert TEMPLATE_VERSIONS["root_reviewer"].keys() >= {
        "root-reviewer-v1",
        "root-reviewer-v2",
        "root-reviewer-v3",
    }
    assert template_for(ROOT_REVIEWER, {"root_reviewer": "root-reviewer-v2"}) is ROOT_REVIEWER_V2
    # Everything v2 said, v3 still says: it is a revision, not a rewrite.
    for field in ("excerpt", "covered_by", "carries_root_criteria", "不得据此判 false"):
        assert field in ROOT_REVIEWER.instructions, field


# ======================================================================================
# 4. Verification P1-3: no host path in the request, and a hash that does not move
#    with the checkout's location
# ======================================================================================


def test_host_paths_in_the_root_parameters_become_placeholders() -> None:
    cleaned = sanitised_goal_parameters(
        {
            "repository": "/Users/someone/work/episodes/C3/worktree",
            "failing_test": "tests/test_public_window.py::test_window_sum",
            "scratch": "/Users/someone/work/episodes/C3/worktree/tmp/notes.md",
            "elsewhere": "/var/log/other",
            "windows": "C:\\work\\repo",
            "count": 3,
            "nested": {"path": "/Users/someone/work/episodes/C3/worktree/src"},
            "many": ["/Users/someone/work/episodes/C3/worktree/a", "b"],
        }
    )
    assert cleaned == {
        "repository": WORKSPACE_PLACEHOLDER,
        "failing_test": "tests/test_public_window.py::test_window_sum",
        "scratch": f"{WORKSPACE_PLACEHOLDER}/tmp/notes.md",
        "elsewhere": PATH_PLACEHOLDER,
        "windows": PATH_PLACEHOLDER,
        "count": 3,
        "nested": {"path": f"{WORKSPACE_PLACEHOLDER}/src"},
        "many": [f"{WORKSPACE_PLACEHOLDER}/a", "b"],
    }
    assert sanitised_goal_parameters({"repository": "repo-1", "failing_test": "t.py"}) == {
        "repository": "repo-1",
        "failing_test": "t.py",
    }


def test_the_request_hash_does_not_move_with_the_checkout_location() -> None:
    """Two hosts, two worktree paths, one Mission state: one ``context_version``."""

    def request(repository: str) -> RootReviewRequest:
        return RootReviewRequest(
            package_id="pkg-1",
            goal_task_id="task-root",
            goal_statement="make the named failing test pass and explain the change",
            criteria=(),
            contributions=(),
            requirements_revision=3,
            mission_goal="修掉失败的测试",
            goal_parameters=sanitised_goal_parameters(
                {"repository": repository, "failing_test": "tests/test_kv.py::test_get"}
            ),
        )

    first = request("/Users/alice/runs/C3-r0/worktree")
    second = request("/home/bob/evidence/2026-09-16/H-L3-C3-r0/worktree")
    assert first.content_hash() == second.content_hash()
    assert first.to_json()["goal_parameters"]["repository"] == WORKSPACE_PLACEHOLDER


def test_the_real_request_carries_no_host_path(tmp_path, monkeypatch) -> None:
    """On the shipped code domain with the C3 artifacts, a worktree bound as an
    absolute path reaches the reviewer as ``<workspace>`` and nowhere else."""

    import test_htn_deployment_wiring as wiring

    worktree = str(Path(tmp_path) / "episodes" / "H-L3-C3-r0" / "worktree")
    monkeypatch.setattr(wiring, "REPOSITORY", worktree)
    world = _c3(tmp_path)
    shown = world.request().to_json()
    assert shown["goal_parameters"] == {
        "repository": WORKSPACE_PLACEHOLDER,
        "failing_test": FAILING_TEST,
    }
    assert worktree not in json.dumps(shown, ensure_ascii=False)
    assert str(tmp_path) not in json.dumps(shown, ensure_ascii=False)
