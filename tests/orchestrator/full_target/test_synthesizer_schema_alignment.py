# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3g: the MethodSynthesizer's prompt says what the codec accepts, and one more ask.

Found by the Grok acceptance episode H-L3-C1 (branch ``p2.3e-run-exit``, 8b8466d).
Three model replies from that episode are in ``fixtures/htn/replies/`` verbatim:

* the synthesiser's only round wrote a *complete* method — seven steps, an ordering,
  criterion links — spelled ``id`` / ``version`` / ``goal_signature_ref`` /
  ``parameter_bindings`` / ``input_bindings`` / ``coverage_criteria`` /
  ``parent_criterion``: the shapes the request and the Planner package had shown it.
  ``method-synthesizer-v1`` said "the full MethodContract JSON" and named no key, so
  the codec refused it for ten missing fields and the round was concluded
  ``UNREADABLE`` on the spot;
* Planner rounds 2 and 3 — every method NEEDS_EVIDENCE, round 1 refused
  ``proposal_not_grounded`` — answered with a ``<method_proposal>`` in a third
  spelling, because ``planner-hierarchical-v1``'s words told them to.  Both were
  filed ``proposal_unreadable: block_missing`` and the ladder was spent.

What this file pins:

1. the field-by-field gap between the prompt, the request and the codec, and that
   ``method-synthesizer-v2`` closes it — key names, one block that parses, the goal
   type ref and the codec's own field list carried in the request;
2. a reply the codec cannot read is **asked once more** with the codec's problems
   attached, on the same anchor, ordinal +1, written down — bounded at
   ``MAX_SYNTHESIS_ASKS``; a reply that was read and refused is a conclusion;
3. a Planner that writes the other role's block is filed ``proposal_wrong_block``
   with a hint that says whose job the method is, and the hint reaches the next
   round; ``planner-hierarchical-v4`` never asks for a method and gives the Planner
   an explicit empty-operations shape, which is filed ``no_applicable_method``;
4. no alias normalisation is performed on the way in (see the last section for why).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

import test_evidence_saturation as saturation  # noqa: E402
import test_hierarchical_event_flow as flow  # noqa: E402
import test_htn_end_to_end as e2e  # noqa: E402
import test_method_synthesis as synth  # noqa: E402
import test_service_intent_provider_blocker as blocker  # noqa: E402
from htn_world import method, param, ref, step  # noqa: E402

from agent_orchestrator.contracts.htn import MethodContract, TaskForm  # noqa: E402
from agent_orchestrator.contracts.models import ContractError, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.resolution import ReviewAccount  # noqa: E402
from agent_orchestrator.contracts.state_machines import TERMINAL_MISSION  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    MAX_SYNTHESIS_ASKS,
    Orchestrator,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    SYNTHESIS_REPLY_UNREADABLE,
    SYNTHESIS_ROUND_RECORDED,
)
from agent_orchestrator.planning.htn.registry import AdmissionVerdict  # noqa: E402
from agent_orchestrator.planning.htn.synthesis import (  # noqa: E402
    METHOD_SHAPE,
    SynthesisReplyUnreadable,
    synthesis_schema_feedback,
)
from agent_orchestrator.planning.planner import (  # noqa: E402
    NO_APPLICABLE_METHOD,
    PROPOSAL_WRONG_BLOCK,
    NoApplicableMethodDeclared,
    parse_method_proposal,
    parse_plan_proposal,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.runtime.output_blocks import BlockError, repair_hint  # noqa: E402
from agent_orchestrator.runtime.role_templates import (  # noqa: E402
    HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE,
    METHOD_PROPOSAL_TAG,
    METHOD_SYNTHESIZER,
    METHOD_SYNTHESIZER_V1,
    METHOD_SYNTHESIZER_V1_VERSION,
    METHOD_SYNTHESIZER_V2,
    METHOD_SYNTHESIZER_V2_VERSION,
    METHOD_SYNTHESIZER_VERSION,
    PLAN_REVISION_PROPOSAL_TAG,
    PLANNER_HIERARCHICAL_V3,
    PLANNER_HIERARCHICAL_V4,
    PLANNER_HIERARCHICAL_V4_VERSION,
    TEMPLATE_VERSIONS,
    hierarchical_planner_versions,
    template_for,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    method_proposal_step,
    plan_revision_proposal_step,
    role_of,
)
from simple_harness import MessageRole  # noqa: E402

REPLIES = _HTN_FIXTURES / "replies"


def reply(name: str) -> str:
    """One model reply of the Grok episode, verbatim (no local paths inside)."""

    return (REPLIES / f"{name}.txt").read_text(encoding="utf-8")


#: What the codec said about the synthesiser's reply in the real episode
#: (``MethodSynthesisRoundRecorded.problems[0]``, ``execution.db`` of H-L3-C1-r0).
EPISODE_MISSING_FIELDS = [
    "basis_refs",
    "expected_effects",
    "exploration_assumptions",
    "goal_type_ref",
    "method_id",
    "method_version",
    "output_schema_ref",
    "parameter_schema_ref",
    "required_capabilities",
    "schema_version",
]

#: The spellings the model used instead — each one is a name it had been *shown*.
EPISODE_ALIASES = {
    "id": "method_id",
    "version": "method_version",
    "goal_signature_ref": "goal_type_ref",
    "parameter_bindings": "steps[].arguments",
    "input_bindings": "steps[].arguments",
    "coverage_criteria": "(no counterpart on a step)",
    "parent_criterion": "criterion_links[].parent_criterion_id",
    "step_local_id": "criterion_links[].child_step",
    "child_criterion": "criterion_links[].child_criterion_id",
}


# ======================================================================================
# 1. the gap, field by field, and v2 closing it
# ======================================================================================


def test_the_grok_synthesizer_reply_is_refused_for_exactly_the_fields_the_episode_recorded():
    """The fixture reproduces the episode's refusal byte for byte — and shows why."""

    text = reply("grok_synthesizer_round1")
    with pytest.raises(ContractError) as caught:
        parse_method_proposal(text)
    assert str(caught.value) == (
        f"method_proposal.method is missing required fields: {EPISODE_MISSING_FIELDS}"
    )
    written = json.loads(re.search(r"<method_proposal>(.*)</method_proposal>", text, re.S).group(1))
    method_keys = set(written["method"])
    # what it wrote is what it had been shown: the request's goal_signature shape …
    assert {"id", "version", "goal_signature_ref", "statement"} <= method_keys
    assert set(written["method"]["goal_signature_ref"]) == {
        "signature_id",
        "version",
        "parameter_schema_ref",
        "output_schema_ref",
    }
    # … and a step shape of its own; none of it is a codec key
    step_keys = set(written["method"]["steps"][0])
    assert {"parameter_bindings", "input_bindings", "coverage_criteria"} <= step_keys
    assert not ({"arguments", "obligation_relation"} & step_keys)
    assert set(written["method"]["composition"]["criterion_links"][0]) == {
        "parent_criterion",
        "step_local_id",
        "child_criterion",
    }
    for alias in EPISODE_ALIASES:
        assert alias in text, alias


def test_v2_names_every_field_the_codec_requires_and_v1_named_none_of_them():
    """The prompt states the codec's keys; v1 said "the full MethodContract JSON"."""

    # P2.3i registered v3 beside it as the default; v2 is the version this test is about.
    v2 = METHOD_SYNTHESIZER_V2.instructions
    for group, names in METHOD_SHAPE.items():
        for name in names:
            assert name in v2, f"{group}.{name} is not in method-synthesizer-v2"
    # the spellings the episode used are named as *not* accepted
    for alias in (
        "parameter_bindings",
        "input_bindings",
        "from_goal_parameter",
        "goal_signature_ref",
    ):
        assert alias in v2, alias
    v1 = METHOD_SYNTHESIZER_V1.instructions
    assert "完整 MethodContract JSON" in v1
    assert not any(
        name in v1 for name in ("method_version", "obligation_relation", "evidence_requirement")
    )
    assert METHOD_SYNTHESIZER_V2.prompt_version == METHOD_SYNTHESIZER_V2_VERSION
    assert METHOD_SYNTHESIZER_V2_VERSION == "method-synthesizer-v2"
    assert METHOD_SYNTHESIZER.prompt_version == METHOD_SYNTHESIZER_VERSION
    # P2.3i: v3; P2.3j merge: the default is v4 (v3 plus ``review_feedback``).
    assert METHOD_SYNTHESIZER_VERSION == "method-synthesizer-v6"  # P2.3m: v6 beside v5
    assert METHOD_SYNTHESIZER.tool_names == METHOD_SYNTHESIZER_V2.tool_names == ()
    assert v2.startswith("[role:method_synthesizer]")


def _minimal_method_json() -> dict[str, Any]:
    return synth.legal_method().to_json()


def test_method_shape_is_the_codecs_own_field_list():
    """Drop any name METHOD_SHAPE lists → the codec names it; add one → refused.

    So the list the request hands the model cannot drift from the codec.
    """

    base = _minimal_method_json()
    assert set(base) == set(METHOD_SHAPE["method"])
    assert set(base["steps"][0]) == set(METHOD_SHAPE["step"])
    assert set(base["composition"]) == set(METHOD_SHAPE["composition"])
    assert set(base["composition"]["criterion_links"][0]) == set(METHOD_SHAPE["criterion_link"])
    assert set(base["goal_type_ref"]) == set(METHOD_SHAPE["versioned_ref"])

    def refused(payload: dict[str, Any]) -> str:
        with pytest.raises(ContractError) as caught:
            MethodContract.from_json(payload)
        return str(caught.value)

    for name in METHOD_SHAPE["method"]:
        assert name in refused({k: v for k, v in base.items() if k != name}), name
    for name in METHOD_SHAPE["step"]:
        mutated = json.loads(json.dumps(base))
        del mutated["steps"][0][name]
        assert name in refused(mutated), name
    for name in METHOD_SHAPE["composition"]:
        mutated = json.loads(json.dumps(base))
        del mutated["composition"][name]
        assert name in refused(mutated), name
    for name in METHOD_SHAPE["criterion_link"]:
        mutated = json.loads(json.dumps(base))
        del mutated["composition"]["criterion_links"][0][name]
        assert name in refused(mutated), name
    assert "unknown fields: ['id']" in refused({**base, "id": base["method_id"]})


def _example_block(text: str) -> str:
    match = re.search(r"<method_proposal>\n(.*?)\n</method_proposal>", text, re.S)
    assert match, "v2 carries one complete example block"
    return match.group(1)


def test_the_example_in_v2_parses_and_is_admitted_once_the_placeholders_are_copied():
    """ "Write it like this and copy the ids and hashes from the input" — done literally.

    The example's ids are the neutral ``dom.*``; the placeholders are the Chinese
    "copy X from the input" strings.  A model that follows the sentence swaps those
    for the request's values and nothing else — which is exactly what this test does,
    and the result reaches ``TRIAL_ADMITTED`` against a deployment shaped like the
    example.  **Mutation**: drop any key from the example and this goes red.
    """

    env = synth.empty_library_env()
    request = synth.synthesizer(env).build_request(synth.goal(env), env.capabilities())
    package = request.to_json()
    # the same example block is carried, byte for byte, by v2 and by v3
    assert _example_block(METHOD_SYNTHESIZER_V2.instructions) == _example_block(
        METHOD_SYNTHESIZER.instructions
    )
    body = _example_block(METHOD_SYNTHESIZER_V2.instructions)
    operators = {
        item["task_type_ref"]["id"]: item["task_type_ref"] for item in package["operators"]
    }
    swaps = {
        '"照抄输入 goal_type_ref.content_hash"': json.dumps(
            package["goal_type_ref"]["content_hash"]
        ),
        '"照抄输入 goal_signature.parameter_schema_ref.content_hash"': json.dumps(
            package["goal_signature"]["parameter_schema_ref"]["content_hash"]
        ),
        '"照抄输入 goal_signature.output_schema_ref.content_hash"': json.dumps(
            package["goal_signature"]["output_schema_ref"]["content_hash"]
        ),
        '"照抄 operators 里 dom.collect 的 content_hash"': json.dumps(
            operators["synth.collect"]["content_hash"]
        ),
        '"照抄 operators 里 dom.deliver 的 content_hash"': json.dumps(
            operators["synth.deliver"]["content_hash"]
        ),
        '"照抄输入 goal_signature.coverage_criteria 里的一条"': json.dumps(
            package["goal_signature"]["coverage_criteria"][0]
        ),
        '"dom.goal.params"': json.dumps(package["goal_signature"]["parameter_schema_ref"]["id"]),
        '"dom.goal.outputs"': json.dumps(package["goal_signature"]["output_schema_ref"]["id"]),
        '"dom.goal.by-collect-then-deliver"': '"synth.goal.by-collect-then-deliver"',
        '"dom.goal"': json.dumps(package["goal_type_ref"]["id"]),
        '"dom.collect"': '"synth.collect"',
        '"dom.deliver"': '"synth.deliver"',
        '"dom.read"': '"synth.read"',
        '"dom.send"': '"synth.send"',
    }
    for old, new in swaps.items():
        assert old in body, old
        body = body.replace(old, new)
    assert "照抄" not in body and "dom." not in body, "every placeholder was a named one"
    proposal = parse_method_proposal(f"<{METHOD_PROPOSAL_TAG}>{body}</{METHOD_PROPOSAL_TAG}>")
    assert set(proposal.method.to_json()) == set(METHOD_SHAPE["method"])
    receipt = synth.synthesizer(env).accept_response(
        f"<{METHOD_PROPOSAL_TAG}>{body}</{METHOD_PROPOSAL_TAG}>", policy=env.policy()
    )
    assert receipt.verdict is AdmissionVerdict.TRIAL_ADMITTED, receipt.problems


def test_the_request_carries_the_goal_type_ref_and_the_field_list_the_method_must_copy():
    env = synth.empty_library_env()
    request = synth.synthesizer(env).build_request(synth.goal(env), env.capabilities())
    package = request.to_json()
    declared = env.catalog.require(ref("synth.goal")).task_type_ref
    assert package["goal_type_ref"] == declared.to_json()
    assert package["goal_type_ref"]["content_hash"] == declared.content_hash
    assert package["method_shape"] == {key: list(value) for key, value in METHOD_SHAPE.items()}
    assert package["schema_feedback"] == []
    # every operator ref carries its content hash, as the prompt says to copy
    assert all(len(item["task_type_ref"]["content_hash"]) == 64 for item in package["operators"])
    # the second ask is the same request plus the codec's words, and it says so
    again = synth.synthesizer(env).build_request(
        synth.goal(env), env.capabilities(), schema_feedback=("method is missing x",)
    )
    assert again.to_json()["schema_feedback"] == ["method is missing x"]
    assert again.content_hash() != request.content_hash()
    assert {k: v for k, v in again.to_json().items() if k != "schema_feedback"} == {
        k: v for k, v in package.items() if k != "schema_feedback"
    }


def test_v1_keeps_its_bytes_stays_registered_and_is_still_pinnable():
    """§18.5 C8: a new wording is a new version beside the old one, never an edit."""

    v1 = hashlib.sha256(METHOD_SYNTHESIZER_V1.instructions.encode("utf-8")).hexdigest()
    assert v1 == "9341ab10390015fb45d528d95dac0af5b29658f0ee9b70060369d607f8f0ae32"
    assert METHOD_SYNTHESIZER_V1.prompt_version == METHOD_SYNTHESIZER_V1_VERSION
    versions = TEMPLATE_VERSIONS["method_synthesizer"]
    assert versions[METHOD_SYNTHESIZER_V1_VERSION] is METHOD_SYNTHESIZER_V1
    assert versions[METHOD_SYNTHESIZER_V2_VERSION] is METHOD_SYNTHESIZER_V2
    assert versions[METHOD_SYNTHESIZER_VERSION] is METHOD_SYNTHESIZER
    pinned = template_for(METHOD_SYNTHESIZER, {"method_synthesizer": METHOD_SYNTHESIZER_V1_VERSION})
    assert pinned is METHOD_SYNTHESIZER_V1
    assert template_for(METHOD_SYNTHESIZER, None) is METHOD_SYNTHESIZER


# ======================================================================================
# 2. one more ask, with the codec's words attached
# ======================================================================================


def test_an_unreadable_reply_is_named_and_a_refused_one_is_not():
    """``SynthesisReplyUnreadable`` is the codec's refusal, never the registry's."""

    env = synth.empty_library_env()
    with pytest.raises(SynthesisReplyUnreadable) as schema:
        synth.synthesizer(env).accept_response(
            reply("grok_synthesizer_round1"), policy=env.policy()
        )
    assert schema.value.block_defect == "schema"
    assert schema.value.problems[0].startswith("method_proposal.method is missing required fields")
    with pytest.raises(SynthesisReplyUnreadable) as missing:
        synth.synthesizer(env).accept_response("我没有方法。", policy=env.policy())
    assert missing.value.block_defect == "block_missing"
    assert isinstance(schema.value, ContractError) and isinstance(missing.value, ContractError)
    assert env.registry.method_refs() == ()
    # read and refused by the admission protocol: a conclusion, not an unreadable reply
    refused = method(
        "synth.nowhere",
        "synth.goal",
        parameter_schema="synth.goal.params",
        steps=(
            step("x", "synth.no-such-operator", TaskForm.PRIMITIVE, {"subject": param("subject")}),
        ),
        links=(("c-root", "x", "c-x"),),
        finalizer="x",
    )
    receipt = synth.synthesizer(env).accept_response(
        method_proposal_step(refused.to_json()), policy=env.policy()
    )
    assert receipt.verdict is AdmissionVerdict.REJECTED
    feedback = synthesis_schema_feedback(schema.value)
    assert feedback[0] == schema.value.problems[0]
    assert METHOD_PROPOSAL_TAG in feedback[-1] and "method_shape" in feedback[-1]


def _synthesis_flow(tmp_path, *, key: str, synthesizer_steps: list[Any], planner_steps: list[Any]):
    """H-L3-C1's opening on a real ``Orchestrator``, driven a cycle at a time."""

    world, invented, config = blocker.saturation_world(tmp_path, key=key)
    provider = RoleScriptedProvider(
        {"planner": list(planner_steps), "method_synthesizer": list(synthesizer_steps)}
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            for _ in range(40):
                await loop._cycle()
                await asyncio.sleep(0.02)
                current = loop.store.get_mission(mission_id)
                events = list(loop.store.list_events(mission_id))
                if current is not None and (
                    current.status in TERMINAL_MISSION
                    or any(item.type == "PlanRevisionCommitted" for item in events)
                ):
                    break
            events = list(loop.store.list_events(mission_id))
            intents = [
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
                )
                if item.mission_id == mission_id
                and str(item.config.get("role", "")) == "method_synthesizer"
            ]
            final = loop.store.get_mission(mission_id)
            return {
                "invented": invented,
                "types": [item.type for item in events],
                "unreadable": [
                    dict(item.payload) for item in events if item.type == SYNTHESIS_REPLY_UNREADABLE
                ],
                "synthesis": [
                    dict(item.payload) for item in events if item.type == SYNTHESIS_ROUND_RECORDED
                ],
                "committed": [item for item in events if item.type == "PlanRevisionCommitted"],
                "synth_intents": sorted(
                    (
                        str(item.subject_id),
                        str(item.state),
                        str(item.config.get("budget_account")),
                        int(item.config.get("ordinal", 0)),
                    )
                    for item in intents
                ),
                "synth_requests": [
                    request
                    for request in provider.requests
                    if role_of(request) == "method_synthesizer"
                ],
                "roles": dict(provider.by_role),
                "status": final.status,
                "report": dict(final.final_report or {}),
                "mission_id": mission_id,
            }

    return asyncio.run(case())


def _last_user_text(request: Any) -> str:
    for message in reversed(request.messages):
        if str(message.role) == str(MessageRole.USER):
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


def test_a_reply_the_codec_cannot_read_is_asked_once_more_with_the_problems_attached(tmp_path):
    """The episode's synthesiser reply, then a legal one: the round is admitted.

    Same anchor (``…:synthesizer:task-root:2``), ordinal +1, the first reply written
    down as ``MethodSynthesisReplyUnreadable`` with the codec's words, the second ask's
    request carrying those words as ``schema_feedback``, both asks on the Mission's
    planning account, and the round concluded ``admitted`` with ``asks == 2``.
    **Mutation**: ``MAX_SYNTHESIS_ASKS = 1`` → red (concluded UNREADABLE on ask 1);
    ``schema_feedback`` not handed to the request → red (second package carries none).
    """

    outcome = _synthesis_flow(
        tmp_path,
        key="p23g-retry-admitted",
        synthesizer_steps=[
            reply("grok_synthesizer_round1"),
            method_proposal_step(saturation._free_method().to_json()),
        ],
        planner_steps=[
            "nothing to propose",
            "still nothing",
            saturation._adopt(saturation._free_method().method_ref()),
        ],
    )
    assert outcome["roles"].get("method_synthesizer") == 2, outcome["roles"]
    assert len(outcome["unreadable"]) == 1, outcome["types"]
    first = outcome["unreadable"][0]
    assert first["ordinal"] == 1 and first["block_defect"] == "schema"
    assert first["problems"][0].startswith("method_proposal.method is missing required fields")
    assert outcome["synthesis"] == [
        {**outcome["synthesis"][0], "admitted": True, "asks": 2, "verdict": "TRIAL_ADMITTED"}
    ], outcome["synthesis"]
    subjects = [item[0] for item in outcome["synth_intents"]]
    mission_id = outcome["mission_id"]
    assert subjects == [
        f"{mission_id}:synthesizer:task-root:1",
        f"{mission_id}:synthesizer:task-root:2",
    ], subjects
    assert [item[3] for item in outcome["synth_intents"]] == [1, 2]
    assert {item[2] for item in outcome["synth_intents"]} == {str(ReviewAccount.MISSION_PLANNING)}
    assert [item[1] for item in outcome["synth_intents"]] == ["FAILED", "SETTLED"]
    # the second ask carried the codec's words, the first carried none
    second = json.loads(_last_user_text(outcome["synth_requests"][1]))
    first_package = json.loads(_last_user_text(outcome["synth_requests"][0]))
    assert first_package["schema_feedback"] == []
    assert second["schema_feedback"][0] == first["problems"][0]
    assert second["goal_type_ref"] and second["method_shape"]["method"] == list(
        METHOD_SHAPE["method"]
    )
    assert outcome["committed"], outcome["types"]


def test_a_second_unreadable_reply_concludes_the_round_and_nobody_is_asked_a_third_time(tmp_path):
    outcome = _synthesis_flow(
        tmp_path,
        key="p23g-retry-twice",
        synthesizer_steps=[reply("grok_synthesizer_round1"), "还是没有方法。"],
        planner_steps=["nothing to propose", "still nothing"],
    )
    assert MAX_SYNTHESIS_ASKS == 2
    assert outcome["roles"].get("method_synthesizer") == 2, outcome["roles"]
    assert len(outcome["unreadable"]) == 1
    assert outcome["synthesis"] and outcome["synthesis"][0]["admitted"] is False
    assert outcome["synthesis"][0]["verdict"] == "UNREADABLE"
    assert outcome["synthesis"][0]["asks"] == 2
    assert "block_missing" in outcome["synthesis"][0]["problems"][0]
    assert len(outcome["synth_intents"]) == 2
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["report"]["planning_failure"]["reason"] == "method_synthesis_refused"


def test_the_retry_is_bounded_by_a_constant_read_off_the_intents_ordinal():
    source = inspect.getsource(Orchestrator._collect_synthesizer)
    assert "and ordinal < MAX_SYNTHESIS_ASKS:" in source
    assert "record_synthesis_reply_unreadable(" in source
    assert "feedback = synthesis_schema_feedback(unreadable)" in source
    assert "except SynthesisReplyUnreadable as unreadable:" in source
    assert "asks=ordinal" in source
    # P2.3i: a read-and-refused reply is re-asked only when every problem is a
    # correctable slip; the gate is the one predicate, pinned in
    # ``test_synthesis_rejection_reask``.
    assert "if not admitted and rejection_is_correctable(receipt):" in source


# ======================================================================================
# 3. the Planner never proposes a method
# ======================================================================================


@pytest.mark.parametrize("name", ["grok_planner_round2", "grok_planner_round3"])
def test_a_planner_reply_carrying_the_other_roles_block_is_the_wrong_block(name: str):
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal(reply(name), mission_id="m")
    cause = caught.value.__cause__
    assert isinstance(cause, BlockError) and cause.reason == PROPOSAL_WRONG_BLOCK
    assert PROPOSAL_WRONG_BLOCK in str(caught.value)
    hint = repair_hint(cause, PLAN_REVISION_PROPOSAL_TAG)
    assert "Planner" in hint and "不提方法" in hint
    assert NO_APPLICABLE_METHOD in hint and PLAN_REVISION_PROPOSAL_TAG in hint
    # a reply with neither block keeps its old name
    with pytest.raises(ContractError) as plain:
        parse_plan_proposal("我想了想，先不改计划。", mission_id="m")
    assert plain.value.__cause__.reason == "block_missing"


def test_the_wrong_block_is_filed_under_its_own_reason_and_the_hint_reaches_the_next_round(
    tmp_path,
):
    """Round 2 of the episode, then a legal adoption: the second package carries the hint.

    **Mutation**: remove the ``<method_proposal>`` check in ``parse_plan_proposal`` and
    the reason falls back to ``proposal_unreadable`` with a "no block found" hint.
    """

    holder: dict[str, Any] = {}
    contract = e2e._outer()
    provider = RoleScriptedProvider(
        {"planner": [reply("grok_planner_round2"), e2e._proposal_text(contract)]}
    )
    config = OrchestratorConfig(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )

    async def case() -> None:
        async with Orchestrator(config, provider) as orchestrator:
            mission, _env, _contract = flow._seed_hierarchical(orchestrator, key="p23g-wrong-block")
            await orchestrator.run()
            rejections = flow._events(orchestrator, mission.id, "PlanningRejected")
            holder["rejections"] = [dict(item.payload) for item in rejections]
            holder["committed"] = flow._events(orchestrator, mission.id, "PlanRevisionCommitted")
            holder["requests"] = [
                _last_user_text(request)
                for request in provider.requests
                if role_of(request) == "planner"
            ]

    asyncio.run(case())
    first = holder["rejections"][0]
    assert first["reason"] == PROPOSAL_WRONG_BLOCK, holder["rejections"]
    assert first["detail"]["block_defect"] == PROPOSAL_WRONG_BLOCK
    assert "不提方法" in first["detail"]["repair_hint"]
    assert holder["committed"], "the adoption after the hint went through"
    requests = holder["requests"]
    assert len(requests) == 2, len(requests)
    assert PROPOSAL_WRONG_BLOCK not in requests[0]
    assert PROPOSAL_WRONG_BLOCK in requests[1] and "不提方法" in requests[1]


def test_an_empty_operations_proposal_is_the_planners_explicit_no_method_answer(tmp_path):
    """The shape v4 prescribes is read before the codec and filed under its own reason."""

    text = plan_revision_proposal_step(
        proposal_id="p-none",
        operations=[],
        read_set=[
            {"kind": "task", "id": "task-root", "semantic_revision": 1, "content_hash": "a" * 64}
        ],
        rationale="no_applicable_method: 三个方法都要先观察到 test-is-failing",
    )
    with pytest.raises(NoApplicableMethodDeclared) as caught:
        parse_plan_proposal(text, mission_id="m")
    assert caught.value.rationale.startswith("no_applicable_method: ")
    assert caught.value.proposal_id == "p-none"
    assert isinstance(caught.value, ContractError)
    reason, detail = flow._reject_reason(tmp_path, text=text, key="p23g-no-method")
    assert reason == NO_APPLICABLE_METHOD
    assert detail == {"proposal_id": "p-none", "rationale": caught.value.rationale}
    assert "repair_hint" not in detail and "block_defect" not in detail


def test_v4_never_tells_the_planner_to_propose_a_method_and_v3_keeps_its_bytes():
    v3 = PLANNER_HIERARCHICAL_V3.instructions
    v4 = PLANNER_HIERARCHICAL_V4.instructions
    assert "改为只输出一个 <method_proposal>" in v3
    assert "改为只输出一个 <method_proposal>" not in v4
    assert "你永远不提出方法" in v4 and PROPOSAL_WRONG_BLOCK in v4
    assert NO_APPLICABLE_METHOD in v4 and "operations 写空数组 []" in v4
    assert v4.count(f"<{PLAN_REVISION_PROPOSAL_TAG}>") >= 1
    assert hashlib.sha256(v3.encode("utf-8")).hexdigest() == (
        "ba244a12bf504051d7ebc954f462cf23f9c7734dff980c539187834a0a670acb"
    )
    assert PLANNER_HIERARCHICAL_V4.prompt_version == PLANNER_HIERARCHICAL_V4_VERSION
    assert TEMPLATE_VERSIONS["planner"][PLANNER_HIERARCHICAL_V4_VERSION] is PLANNER_HIERARCHICAL_V4
    # P2.3j: v3 and v4 stay registered (replayable) under package 2; the current
    # package is 3.  P2.3n: the unpinned default moved from v5 to v6; v5 stays a
    # pin of this package.
    assert PLANNER_HIERARCHICAL_V4_VERSION in HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE[2]
    assert PLANNER_HIERARCHICAL_V3.prompt_version in HIERARCHICAL_PLANNER_VERSIONS_BY_PACKAGE[2]
    assert PLANNER_HIERARCHICAL_V4_VERSION not in hierarchical_planner_versions()
    assert "return PLANNER_HIERARCHICAL_V7" in inspect.getsource(
        Orchestrator._hierarchical_planner_template
    )


# ======================================================================================
# 4. no alias normalisation on the way in
# ======================================================================================


def test_no_alias_is_normalised_on_the_way_in():
    """Decided against, and pinned: the three real replies show why a rename map
    would not have made any of them admissible.

    Round 1 lacks ``arguments`` (its ``parameter_bindings`` / ``input_bindings`` use
    ``from_goal_parameter`` / ``from_step``, a different *value language*, not a
    different key), ``obligation_relation``, ``evidence_requirement`` — text nobody
    but the author can supply — and every ``composition`` key but one.  Rounds 2 and
    3 carry no content hashes and no ``arguments`` at all.  A pure rename fixes none
    of that; it only hides which spelling the model used, and §18.5 refuses at the
    boundary rather than rewriting.  The deterministic mechanism is the second ask
    with the codec's words, above.
    """

    env = synth.empty_library_env()
    for name in ("grok_synthesizer_round1", "grok_planner_round2", "grok_planner_round3"):
        with pytest.raises(SynthesisReplyUnreadable) as caught:
            synth.synthesizer(env).accept_response(reply(name), policy=env.policy())
        assert "missing required fields" in str(caught.value)
    assert env.registry.method_refs() == ()
    source = inspect.getsource(sys.modules["agent_orchestrator.planning.htn.synthesis"])
    assert "normalised_fields" not in source and "alias" not in source.lower()
