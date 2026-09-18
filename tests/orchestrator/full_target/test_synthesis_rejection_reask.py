# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3i: a synthesiser reply refused for a slip the model can correct is asked once more.

Found by the Grok acceptance episode H-L3-C1-r0 on the 0.12.2 candidate (c7cfedd).  The
synthesiser's only round, on ``method-synthesizer-v2``, wrote a complete, *decodable*
six-step method — facts → reproduce → apply → verify → inspect → summarize, an ordering,
both root criteria linked — and the admission protocol refused it for one line::

    PORT_UNAVAILABLE: step 'summarize' binds input port 'report', which task type
    'code.summarize-review' does not declare

The request package had said ``code.summarize-review: input_ports ["findings"]``; the
model bound ``findings`` *and* ``report``.  P2.3g's second ask covered only a reply the
codec could not read, so the round was concluded ``REJECTED{asks:1}`` on the spot, the
Planner's three rounds all said ``no_applicable_method`` — correctly — and the Mission
ended ``planning_failed`` on 32K tokens.  The reply is in ``fixtures/htn/replies/``.

What this file pins:

1. the fixture decodes and is refused for exactly that line against the shipped code
   domain; drop the one binding and the same registry admits it (``TRIAL_ADMITTED``);
2. which of the protocol's rejection codes are *correctable* — every one names a
   reference or shape the package states the right value for — and which conclude a
   round on the spot; the two sets partition ``RejectionCode``;
3. on a real ``Orchestrator.run()``: the first refusal is written down as
   ``MethodSynthesisReplyRejected``, the second ask carries the protocol's line as
   ``schema_feedback`` on the same anchor, ordinal 2, same planning account, and the
   corrected reply is admitted and adopted (``PlanRevisionCommitted``); a refusal the
   model cannot correct is not re-asked; a second refusal concludes ``asks:2`` and
   nobody is asked a third time;
4. a second ask the Mission cannot afford ends it ``budget_exhausted`` in the budget's
   words — no "asked again" record before the reservation, no run-loop crash
   (verification P2.3g P2-1);
5. ``method-synthesizer-v3`` tells the model both kinds of problem travel in
   ``schema_feedback``; v2 keeps its bytes and stays pinnable.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
import test_htn_deployment_wiring as wiring  # noqa: E402
import test_method_synthesis as synth  # noqa: E402
import test_service_intent_provider_blocker as blocker  # noqa: E402
from htn_world import method, out, param, ref, step  # noqa: E402

from agent_orchestrator.contracts.htn import TaskForm  # noqa: E402
from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.resolution import ReviewAccount  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStopReason  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    MAX_SYNTHESIS_ASKS,
    Orchestrator,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    SYNTHESIS_REPLY_REJECTED,
    SYNTHESIS_REPLY_UNREADABLE,
    SYNTHESIS_ROUND_RECORDED,
)
from agent_orchestrator.planning.htn.registry import (  # noqa: E402
    AdmissionProblem,
    AdmissionStepId,
    AdmissionVerdict,
    RejectionCode,
)
from agent_orchestrator.planning.htn.synthesis import (  # noqa: E402
    CORRECTABLE_REJECTIONS,
    NON_CORRECTABLE_REJECTIONS,
    MethodSynthesizer,
    SynthesisReplyUnreadable,
    SynthesisRequest,
    rejection_is_correctable,
    rejection_problems,
    synthesis_rejection_feedback,
)
from agent_orchestrator.planning.htn.world import build_planning_world  # noqa: E402
from agent_orchestrator.planning.planner import parse_method_proposal  # noqa: E402
from agent_orchestrator.runtime.role_templates import (  # noqa: E402
    METHOD_PROPOSAL_TAG,
    METHOD_SYNTHESIZER,
    METHOD_SYNTHESIZER_V1,
    METHOD_SYNTHESIZER_V1_VERSION,
    METHOD_SYNTHESIZER_V2,
    METHOD_SYNTHESIZER_V2_VERSION,
    METHOD_SYNTHESIZER_V3,
    METHOD_SYNTHESIZER_V3_VERSION,
    METHOD_SYNTHESIZER_V4_VERSION,
    METHOD_SYNTHESIZER_V5_VERSION,
    METHOD_SYNTHESIZER_V6_VERSION,
    METHOD_SYNTHESIZER_VERSION,
    TEMPLATE_VERSIONS,
    template_for,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    method_proposal_step,
    role_of,
)
from simple_harness import MessageRole  # noqa: E402

REPLIES = _HTN_FIXTURES / "replies"


def reply(name: str) -> str:
    return (REPLIES / f"{name}.txt").read_text(encoding="utf-8")


#: ``MethodSynthesisRoundRecorded`` of H-L3-C1-r0, ``orchestrator.db`` ``events``, verbatim.
EPISODE_RECORD = {
    "admitted": False,
    "asks": 1,
    "author": "model",
    "goal_task_id": "task-root",
    "method_id": "code.fix-by-reproduce-patch-verify-explain",
    "problems": [
        "PORT_UNAVAILABLE: step 'summarize' binds input port 'report', which task type "
        "'code.summarize-review' does not declare"
    ],
    "verdict": "REJECTED",
}
EPISODE_PROBLEM = EPISODE_RECORD["problems"][0]
#: The one binding that was wrong, exactly as the model spelled it.
EPISODE_SLIP = ',"report":{"op":"output","step":"verify","port":"report"}'


def c1_reply() -> str:
    return reply("grok_synthesizer_c1r0_round1")


def c1_reply_corrected() -> str:
    """The same reply with the one undeclared binding removed — nothing else."""

    text = c1_reply()
    assert text.count(EPISODE_SLIP) == 1
    return text.replace(EPISODE_SLIP, "")


def _code_world(mission: str = "m-c1"):
    return build_planning_world(mission, domains=("code",), deployed_layers=("code_test",))


# ======================================================================================
# 1. the episode, verbatim, against the shipped code domain
# ======================================================================================


def test_the_c1_reply_decodes_and_is_refused_for_exactly_the_port_the_episode_recorded():
    """The fixture reproduces the episode's refusal byte for byte — and shows why.

    Not an unreadable reply: ``parse_method_proposal`` accepts it, every content hash
    in it is the catalogue's, and the protocol's only complaint is the one line the
    episode recorded.  The package had stated the port list the model got wrong.
    """

    text = c1_reply()
    proposal = parse_method_proposal(text)  # decodes: the codec is not the problem
    assert proposal.method.method_id == EPISODE_RECORD["method_id"]
    assert [item.local_id for item in proposal.method.steps] == [
        "facts",
        "reproduce",
        "apply",
        "verify",
        "inspect",
        "summarize",
    ]
    world = _code_world()
    receipt = MethodSynthesizer(world.registry, world.catalog).accept_response(
        text, policy=world.policy()
    )
    assert receipt.verdict is AdmissionVerdict.REJECTED
    assert list(rejection_problems(receipt)) == EPISODE_RECORD["problems"]
    assert receipt.codes() == {RejectionCode.PORT_UNAVAILABLE}
    # what the package said, read off the same catalogue the request is built from
    summarize = world.catalog.require(ref("code.summarize-review"))
    assert [port.port_key for port in summarize.input_ports] == ["findings"]
    assert [port.port_key for port in summarize.output_ports] == ["summary"]
    written = json.loads(re.search(r"<method_proposal>(.*)</method_proposal>", text, re.S).group(1))
    bound = written["method"]["steps"][-1]["arguments"]
    assert set(bound) == {"findings", "report"}, "findings is declared; report is the slip"
    assert bound["report"] == {"op": "output", "step": "verify", "port": "report"}
    # every ref the model copied really is the catalogue's
    for item in proposal.method.steps:
        assert world.catalog.resolve(item.task_type_ref) is not None, item.local_id
    assert world.catalog.resolve(proposal.method.goal_type_ref) is not None


def test_the_c1_refusal_is_correctable_and_its_feedback_is_the_protocols_own_line():
    world = _code_world()
    receipt = MethodSynthesizer(world.registry, world.catalog).accept_response(
        c1_reply(), policy=world.policy()
    )
    assert rejection_is_correctable(receipt) is True
    feedback = synthesis_rejection_feedback(receipt)
    assert feedback[0] == EPISODE_PROBLEM
    assert len(feedback) == 2
    assert METHOD_PROPOSAL_TAG in feedback[-1] and "注册协议" in feedback[-1]
    assert "method_id" in feedback[-1] and "method_version" in feedback[-1]


def test_the_corrected_c1_reply_is_admitted_on_the_registry_that_refused_the_first():
    """Drop the one binding: same method_id, same version, new hash → TRIAL_ADMITTED.

    On the *same* registry: the refused definition is kept under its own content hash
    (§7.3 wants the refusal to be a fact one can point at) and does not block the
    corrected one — which is what the second ask needs, since a model keeps its
    ``method_id`` and ``method_version`` when it corrects a slip.
    """

    world = _code_world()
    synthesizer = MethodSynthesizer(world.registry, world.catalog)
    first = synthesizer.accept_response(c1_reply(), policy=world.policy())
    assert first.verdict is AdmissionVerdict.REJECTED
    second = synthesizer.accept_response(c1_reply_corrected(), policy=world.policy())
    assert second.verdict is AdmissionVerdict.TRIAL_ADMITTED, rejection_problems(second)
    assert second.problems == ()
    assert second.method_ref.method_id == first.method_ref.method_id
    assert second.method_ref.version == first.method_ref.version == 1
    assert second.method_ref.content_hash != first.method_ref.content_hash
    assert second.method_ref in world.registry.method_refs()
    assert second.status.value == "TRIAL_ADMITTED"


def test_on_the_real_dispatch_the_corrected_c1_method_is_published_to_the_library(tmp_path):
    """``apply_synthesizer_reply`` twice on the shipped code domain: the refused
    definition is not stored, the admitted one is — at the version the plan will read."""

    service, mission, semantics, _world, dispatch = wiring._both_lane_world(tmp_path)
    first = dispatch.apply_synthesizer_reply(mission.id, c1_reply())
    assert first.verdict is AdmissionVerdict.REJECTED
    assert list(rejection_problems(first)) == EPISODE_RECORD["problems"]
    with pytest.raises(Exception):
        semantics.get_method(EPISODE_RECORD["method_id"], 1)
    second = dispatch.apply_synthesizer_reply(mission.id, c1_reply_corrected())
    assert second.admitted, rejection_problems(second)
    stored = semantics.get_method(EPISODE_RECORD["method_id"], 1)
    assert stored.contract.method_ref() == second.method_ref
    assert str(stored.registration.status) == "TRIAL_ADMITTED"
    service.store.close()


# ======================================================================================
# 2. which refusals are correctable
# ======================================================================================


def test_every_rejection_code_is_classified_once_and_the_reference_slips_are_correctable():
    """The two sets partition the enum; a new code must pick a side at import time."""

    assert CORRECTABLE_REJECTIONS | NON_CORRECTABLE_REJECTIONS == frozenset(RejectionCode)
    assert CORRECTABLE_REJECTIONS.isdisjoint(NON_CORRECTABLE_REJECTIONS)
    assert CORRECTABLE_REJECTIONS == frozenset(
        {
            RejectionCode.PORT_UNAVAILABLE,  # the C1 slip: a port the type does not declare
            RejectionCode.UNKNOWN_TASK_TYPE,  # a ref not offered (goal type or compound step)
            RejectionCode.UNKNOWN_OPERATOR,  # a primitive step's ref not in operators[]
            RejectionCode.UNKNOWN_SCHEMA,  # parameter/output schema ref not the one shown
            RejectionCode.FORM_MISMATCH,  # step form not the type's
            RejectionCode.MALFORMED_DEFINITION,  # argument / link naming an unknown step/parameter
            RejectionCode.ORDERING_CYCLE,  # the partial order the model wrote is cyclic
            RejectionCode.ROOT_COVERAGE_GAP,  # a coverage criterion the package listed is unlinked
        }
    )
    assert NON_CORRECTABLE_REJECTIONS == frozenset(
        {
            RejectionCode.MODEL_CLAIMED_STATUS,  # §18.5 / §6.3 boundary: the refusal is the record
            RejectionCode.ALREADY_REGISTERED,  # registry state, not the reply's shape
            RejectionCode.UNKNOWN_PREDICATE,  # a precondition the package never offered
            RejectionCode.PREDICATE_TYPE_ERROR,  # likewise; I18 is not relaxed by asking again
            RejectionCode.UNKNOWN_CAPABILITY,  # a deployment fact
            RejectionCode.UNBOUNDED_RECURSION,  # a policy limit the package does not state
            RejectionCode.SIZE_BOUND,  # likewise; width overflow is special-cased in P2.3q
        }
    )


def _receipt_with(receipt, *codes: RejectionCode):
    """The same receipt with these problems appended (a mixed step-report)."""

    extra = tuple(
        AdmissionProblem(
            code=code, detail=f"synthetic {code!s}", step=AdmissionStepId.STRUCTURAL_CHECKS
        )
        for code in codes
    )
    return dataclasses.replace(receipt, problems=(*receipt.problems, *extra))


def test_a_refusal_the_model_cannot_correct_is_not_correctable_even_beside_one_it_could():
    env = synth.empty_library_env()
    synthesizer = synth.synthesizer(env)
    # a port slip in the neutral domain: correctable
    slipped = method(
        "synth.goal.slipped",
        "synth.goal",
        parameter_schema="synth.goal.params",
        steps=(
            step("collect", "synth.collect", TaskForm.PRIMITIVE, {"subject": param("subject")}),
            step(
                "deliver",
                "synth.deliver",
                TaskForm.PRIMITIVE,
                {
                    "subject": param("subject"),
                    "result": out("collect", "result"),
                    "report": out("collect", "result"),
                },
            ),
        ),
        links=(("c-root", "deliver", "c-delivered"),),
        finalizer="deliver",
    )
    port = synthesizer.accept_response(method_proposal_step(slipped.to_json()), policy=env.policy())
    assert port.verdict is AdmissionVerdict.REJECTED
    assert port.codes() == {RejectionCode.PORT_UNAVAILABLE}, rejection_problems(port)
    assert rejection_is_correctable(port) is True
    # a capability nobody declares: a deployment fact, concluded on the spot
    gapped = method(
        "synth.goal.gapped",
        "synth.goal",
        parameter_schema="synth.goal.params",
        steps=(
            step(
                "collect",
                "synth.collect",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("synth.capability-nobody-declares",),
            ),
        ),
        links=(("c-root", "collect", "c-collected"),),
        finalizer="collect",
    )
    capability = synthesizer.accept_response(
        method_proposal_step(gapped.to_json()), policy=env.policy()
    )
    assert capability.codes() == {RejectionCode.UNKNOWN_CAPABILITY}, rejection_problems(
        capability
    )
    assert rejection_is_correctable(capability) is False
    # a claimed status: the boundary, never re-asked
    claimed = synthesizer.accept_response(
        method_proposal_step(
            synth.legal_method().to_json(), extras={"registry_status": "ADMITTED"}
        ),
        policy=env.policy(),
    )
    assert claimed.codes() == {RejectionCode.MODEL_CLAIMED_STATUS}
    assert rejection_is_correctable(claimed) is False
    # one uncorrectable problem beside a correctable one: not correctable
    assert rejection_is_correctable(_receipt_with(port, RejectionCode.UNKNOWN_CAPABILITY)) is False
    assert rejection_is_correctable(_receipt_with(port, RejectionCode.ORDERING_CYCLE)) is True
    # an admitted receipt is not a refusal; a refusal with no problems is not correctable
    admitted = synthesizer.accept_response(
        method_proposal_step(synth.legal_method().to_json()), policy=env.policy()
    )
    assert admitted.admitted and rejection_is_correctable(admitted) is False
    assert rejection_is_correctable(dataclasses.replace(port, problems=())) is False
    # the codec's refusal is a different thing and is not a receipt at all
    with pytest.raises(SynthesisReplyUnreadable):
        synthesizer.accept_response("没有块", policy=env.policy())


# ======================================================================================
# 3. on a real Orchestrator: the second ask, its bound, and the refusals that take it
# ======================================================================================


#: The C1 slip transposed to the neutral plan domain: ``review`` binds ``report``, which
#: ``plan.review`` does not declare (its input port is ``result``).
def _slipped():
    return method(
        "plan.outer.free",
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "review",
                "plan.review",
                TaskForm.PRIMITIVE,
                {
                    "subject": param("subject"),
                    "result": out("leaf", "result"),
                    "report": out("leaf", "result"),
                },
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "review", "c-reviewed"),),
        finalizer="review",
    )


def _corrected():
    """Same method_id, same version, the one binding gone — as a model corrects a slip."""

    return method(
        "plan.outer.free",
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
            step(
                "review",
                "plan.review",
                TaskForm.PRIMITIVE,
                {"subject": param("subject"), "result": out("leaf", "result")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "review", "c-reviewed"),),
        finalizer="review",
    )


def _uncorrectable():
    """A capability this deployment never declared: refused, and not a slip."""

    return method(
        "plan.outer.gapped",
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read", "plan.capability-nobody-declares"),
            ),
        ),
        links=(("c-root", "leaf", "c-done"),),
        finalizer="leaf",
    )


PLAN_SLIP = (
    "PORT_UNAVAILABLE: step 'review' binds input port 'report', which task type "
    "'plan.review' does not declare"
)


def _last_user_text(request: Any) -> str:
    for message in reversed(request.messages):
        if str(message.role) == str(MessageRole.USER):
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


def _run(tmp_path, *, key: str, synthesizer_steps: list[Any], planner_steps: list[Any], tweak=None):
    """H-L3-C1's opening on a real ``Orchestrator``, driven by ``run()`` alone."""

    world, _invented, config = blocker.saturation_world(tmp_path, key=key)
    provider = RoleScriptedProvider(
        {"planner": list(planner_steps), "method_synthesizer": list(synthesizer_steps)}
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            if tweak is not None:
                tweak(loop)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            await asyncio.wait_for(loop.run(max_cycles=400), timeout=60)
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
                "types": [item.type for item in events],
                "rejected": [
                    dict(item.payload) for item in events if item.type == SYNTHESIS_REPLY_REJECTED
                ],
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
                    json.loads(_last_user_text(request))
                    for request in provider.requests
                    if role_of(request) == "method_synthesizer"
                ],
                "roles": dict(provider.by_role),
                "status": final.status,
                "stop_reason": final.stop_reason,
                "report": dict(final.final_report or {}),
                "mission_id": mission_id,
            }

    return asyncio.run(case())


def test_a_correctable_refusal_is_asked_once_more_and_the_corrected_reply_is_adopted(tmp_path):
    """The C1 shape end to end: refused for a port, asked again with the protocol's
    line, admitted, adopted — on the same anchor, ordinal 2, same planning account.

    **Mutation**: drop ``PORT_UNAVAILABLE`` from ``CORRECTABLE_REJECTIONS`` → red
    (concluded REJECTED on ask 1, no second request, no commit); record the first ask
    before the second is open → still green here, red in the budget test below.
    """

    outcome = _run(
        tmp_path,
        key="p23i-reask-admitted",
        synthesizer_steps=[
            method_proposal_step(_slipped().to_json()),
            method_proposal_step(_corrected().to_json()),
        ],
        planner_steps=[
            "nothing to propose",
            "still nothing",
            saturation._adopt(_corrected().method_ref()),
        ],
    )
    assert outcome["roles"].get("method_synthesizer") == 2, outcome["roles"]
    assert outcome["unreadable"] == [], "the reply was read; this is the other door"
    assert len(outcome["rejected"]) == 1, outcome["types"]
    first = outcome["rejected"][0]
    assert first["ordinal"] == 1 and first["verdict"] == "REJECTED"
    assert first["method_id"] == "plan.outer.free"
    assert first["problems"] == [PLAN_SLIP]
    assert outcome["synthesis"] == [
        {
            **outcome["synthesis"][0],
            "admitted": True,
            "asks": 2,
            "verdict": "TRIAL_ADMITTED",
            "method_id": "plan.outer.free",
            "retry_refused": "",
        }
    ], outcome["synthesis"]
    mission_id = outcome["mission_id"]
    assert [item[0] for item in outcome["synth_intents"]] == [
        f"{mission_id}:synthesizer:task-root:1",
        f"{mission_id}:synthesizer:task-root:2",
    ]
    assert [item[3] for item in outcome["synth_intents"]] == [1, 2]
    assert [item[1] for item in outcome["synth_intents"]] == ["FAILED", "SETTLED"]
    assert {item[2] for item in outcome["synth_intents"]} == {str(ReviewAccount.MISSION_PLANNING)}
    # the second ask carried the protocol's line, verbatim, and the first carried none
    first_package, second_package = outcome["synth_requests"]
    assert first_package["schema_feedback"] == []
    assert second_package["schema_feedback"][0] == PLAN_SLIP
    assert "注册协议" in second_package["schema_feedback"][-1]
    assert second_package["role_prompt_version"] == METHOD_SYNTHESIZER_VERSION
    assert outcome["committed"], outcome["types"]
    assert outcome["status"] is not MissionStatus.PLANNING


def test_a_refusal_the_model_cannot_correct_concludes_the_round_on_the_spot(tmp_path):
    """**Mutation**: ``rejection_is_correctable`` returning True for everything → red."""

    outcome = _run(
        tmp_path,
        key="p23i-uncorrectable",
        synthesizer_steps=[method_proposal_step(_uncorrectable().to_json())],
        planner_steps=["nothing to propose", "still nothing"],
    )
    assert outcome["roles"].get("method_synthesizer") == 1, outcome["roles"]
    assert outcome["rejected"] == [] and outcome["unreadable"] == []
    assert len(outcome["synthesis"]) == 1
    record = outcome["synthesis"][0]
    assert record["admitted"] is False and record["verdict"] == "REJECTED"
    assert record["asks"] == 1 and record["retry_refused"] == ""
    assert record["problems"][0].startswith("UNKNOWN_CAPABILITY: ")
    assert len(outcome["synth_intents"]) == 1
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.PLANNING_FAILED)
    # Under ``run()`` the Planner's ladder and the synthesis round race; whichever
    # ends last names the stop, and both names are honest.  What is pinned is that
    # neither is "budget" and nobody was asked twice.
    assert outcome["report"]["planning_failure"]["reason"] in {
        "method_synthesis_refused",
        "proposal_unreadable",
    }


def test_a_second_refusal_concludes_the_round_with_two_asks_and_nobody_is_asked_a_third_time(
    tmp_path,
):
    """**Mutation**: ``MAX_SYNTHESIS_ASKS = 3`` → red (a third request, ``asks: 3``)."""

    outcome = _run(
        tmp_path,
        key="p23i-refused-twice",
        synthesizer_steps=[
            method_proposal_step(_slipped().to_json()),
            method_proposal_step(_slipped().to_json()),
        ],
        planner_steps=["nothing to propose", "still nothing"],
    )
    assert MAX_SYNTHESIS_ASKS == 2
    assert outcome["roles"].get("method_synthesizer") == 2, outcome["roles"]
    assert [item["ordinal"] for item in outcome["rejected"]] == [1]
    assert len(outcome["synthesis"]) == 1
    record = outcome["synthesis"][0]
    assert record["admitted"] is False and record["verdict"] == "REJECTED"
    assert record["asks"] == 2 and record["retry_refused"] == ""
    assert record["problems"] == [PLAN_SLIP]
    assert len(outcome["synth_intents"]) == 2
    assert len(outcome["synth_requests"]) == 2
    assert outcome["synth_requests"][1]["schema_feedback"][0] == PLAN_SLIP
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["report"]["planning_failure"]["reason"] == "method_synthesis_refused"


def _starve_the_second_ask(loop: Orchestrator) -> None:
    """Make the reservation for ask 2 (and only ask 2) exceed the Mission's account."""

    original_reservation = loop._reservation
    original_create = loop._create_synthesizer_intent

    # P2.3j merge: the second ask also carries ``synthesis_round`` / ``review_feedback``;
    # the stub forwards whatever the handler passes so it stays a starvation, not a stub.
    async def create(
        mission_id: str, goal_task_id: str, *, ordinal: int, schema_feedback=(), **carried
    ):
        if ordinal >= 2:
            loop._reservation = lambda tokens, profile_id=None: original_reservation(  # type: ignore[method-assign]
                10**9, profile_id
            )
        try:
            return await original_create(
                mission_id,
                goal_task_id,
                ordinal=ordinal,
                schema_feedback=schema_feedback,
                **carried,
            )
        finally:
            loop._reservation = original_reservation  # type: ignore[method-assign]

    loop._create_synthesizer_intent = create  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("name", "first_reply", "verdict"),
    [
        ("rejected", method_proposal_step(_slipped().to_json()), "REJECTED"),
        ("unreadable", reply("grok_synthesizer_round1"), "UNREADABLE"),
    ],
    ids=["rejected", "unreadable"],
)
def test_a_second_ask_the_mission_cannot_afford_stops_it_for_the_budget_and_says_so(
    tmp_path, name: str, first_reply: str, verdict: str
):
    """Verification P2.3g P2-1, both doors.

    No "asked again" record precedes the reservation; the round is concluded on the
    reply it has with ``retry_refused`` in its own field; the Mission stops
    ``budget_exhausted`` — the budget's words, not "the synthesis was refused" — and
    ``run()`` returns instead of carrying a ``BudgetExhausted`` out of ``_cycle()``.
    **Mutation**: fold ``except BudgetExhausted`` into the generic refusal → red
    (``method_synthesis_refused``); record the first ask before opening the second →
    red (a ``MethodSynthesisReply*`` row exists).
    """

    outcome = _run(
        tmp_path,
        key=f"p23i-budget-{name}",
        synthesizer_steps=[first_reply],
        planner_steps=["nothing to propose", "still nothing"],
        tweak=_starve_the_second_ask,
    )
    assert outcome["roles"].get("method_synthesizer") == 1, outcome["roles"]
    assert outcome["rejected"] == [] and outcome["unreadable"] == [], outcome["types"]
    assert len(outcome["synthesis"]) == 1
    record = outcome["synthesis"][0]
    assert record["admitted"] is False and record["verdict"] == verdict
    assert record["asks"] == 1
    assert record["retry_refused"].startswith("budget_exhausted: budget exhausted on ")
    assert "retry not asked" not in " ".join(record["problems"])
    assert len(outcome["synth_intents"]) == 1
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.BUDGET_EXHAUSTED)
    failure = outcome["report"]["planning_failure"]
    assert failure["reason"] == "budget_exhausted", failure
    assert failure["phase"] == "method_synthesis" and failure["ordinal"] == 2
    assert failure["goal_task_id"] == "task-root" and failure["dimension"] == "tokens"
    assert failure["requested"] == 10**9 and failure["remaining"] < 10**9


def test_the_second_ask_is_opened_before_the_first_is_written_down_and_the_gate_is_one_predicate():
    source = inspect.getsource(Orchestrator._collect_synthesizer)
    assert "if not admitted and rejection_is_correctable(receipt):" in source
    assert "feedback = synthesis_rejection_feedback(receipt)" in source
    assert "record_synthesis_reply_rejected(" in source
    assert "and ordinal < MAX_SYNTHESIS_ASKS:" in source
    assert source.count("< MAX_SYNTHESIS_ASKS") == 1, "one bound, one gate"
    # P2-1: the intent is created first; the first-ask record only on success
    assert source.index("await self._create_synthesizer_intent(") < source.index(
        "else:\n                record_first_ask()"
    )
    assert source.count("record_first_ask()\n") == 1, "one call site, after the open"
    assert "except BudgetExhausted as error:" in source
    assert "stop_reason=MissionStopReason.BUDGET_EXHAUSTED" in source
    assert '"phase": "method_synthesis"' in source
    assert "retry_refused=retry_refused" in source
    assert SYNTHESIS_REPLY_REJECTED == "MethodSynthesisReplyRejected"
    assert SYNTHESIS_REPLY_REJECTED in flow.NEW_EVENT_TYPES


# ======================================================================================
# 4. the prompt: v3 says what schema_feedback may hold; v2 keeps its bytes
# ======================================================================================


def test_v3_tells_the_model_a_protocol_refusal_may_travel_in_schema_feedback():
    # P2.3j merge: v3 is this slice's prompt and keeps its bytes; the default moved to
    # v4 (v3 plus ``review_feedback``) and, with P2.3k, to v5 (v4 plus "feed the
    # explaining step the change") — each carries every sentence checked here.
    v3 = METHOD_SYNTHESIZER_V3.instructions
    v2 = METHOD_SYNTHESIZER_V2.instructions
    assert METHOD_SYNTHESIZER_V3.prompt_version == METHOD_SYNTHESIZER_V3_VERSION
    assert METHOD_SYNTHESIZER_V3_VERSION == "method-synthesizer-v3"
    assert METHOD_SYNTHESIZER.prompt_version == METHOD_SYNTHESIZER_VERSION
    assert METHOD_SYNTHESIZER_VERSION == "method-synthesizer-v7"
    assert "拒绝码" in METHOD_SYNTHESIZER.instructions
    for code in sorted(CORRECTABLE_REJECTIONS, key=str):
        assert str(code) in v3, f"v3 names every correctable code: {code!s}"
    for code in sorted(NON_CORRECTABLE_REJECTIONS, key=str):
        assert str(code) not in v3, f"v3 does not invite a repair of {code!s}"
    assert "拒绝码" in v3 and "拒绝码" not in v2
    assert "保留 method_id 与 method_version" in v3
    assert "input_ports" in v3 and "output_ports" in v3
    assert "没有通过解码" in v2
    # the schema_feedback sentence of v2 is replaced, the rest is v2 byte for byte
    assert v3.startswith("[role:method_synthesizer]")
    assert len(v3) > len(v2)
    assert v3.count("<method_proposal>") == v2.count("<method_proposal>")


def test_v2_keeps_its_bytes_stays_registered_and_is_still_pinnable():
    """§18.5 C8: a new wording is a new version beside the old one, never an edit."""

    assert hashlib.sha256(METHOD_SYNTHESIZER_V2.instructions.encode("utf-8")).hexdigest() == (
        "27ccb23492ef00b73404735a03f51439d2bf0eb1339a6a6b4320950ee8beaccb"
    )
    assert hashlib.sha256(METHOD_SYNTHESIZER_V1.instructions.encode("utf-8")).hexdigest() == (
        "9341ab10390015fb45d528d95dac0af5b29658f0ee9b70060369d607f8f0ae32"
    )
    versions = TEMPLATE_VERSIONS["method_synthesizer"]
    assert set(versions) == {
        METHOD_SYNTHESIZER_V1_VERSION,
        METHOD_SYNTHESIZER_V2_VERSION,
        METHOD_SYNTHESIZER_V3_VERSION,
        METHOD_SYNTHESIZER_V4_VERSION,
        METHOD_SYNTHESIZER_V5_VERSION,
        METHOD_SYNTHESIZER_V6_VERSION,
        METHOD_SYNTHESIZER_VERSION,
    }
    assert versions[METHOD_SYNTHESIZER_V3_VERSION] is METHOD_SYNTHESIZER_V3
    assert hashlib.sha256(METHOD_SYNTHESIZER_V3.instructions.encode("utf-8")).hexdigest() == (
        "a38309fdb328c929d6ddefa37ff4de294628f0f508dfad82c7a27bb1cd0e6c9c"
    )
    assert versions[METHOD_SYNTHESIZER_V2_VERSION] is METHOD_SYNTHESIZER_V2
    assert versions[METHOD_SYNTHESIZER_VERSION] is METHOD_SYNTHESIZER
    assert (
        template_for(METHOD_SYNTHESIZER, {"method_synthesizer": METHOD_SYNTHESIZER_V2_VERSION})
        is METHOD_SYNTHESIZER_V2
    )
    assert (
        template_for(METHOD_SYNTHESIZER, {"method_synthesizer": METHOD_SYNTHESIZER_V1_VERSION})
        is METHOD_SYNTHESIZER_V1
    )
    assert template_for(METHOD_SYNTHESIZER, None) is METHOD_SYNTHESIZER
    # a fresh request names the default version
    env = synth.empty_library_env()
    request = synth.synthesizer(env).build_request(synth.goal(env), env.capabilities())
    assert isinstance(request, SynthesisRequest)
    assert request.to_json()["role_prompt_version"] == "method-synthesizer-v7"
