# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c: the MethodSynthesizer's system side (§7.3 source 4, §18.5 C8).

Three properties, and one absence:

1. **The request hands over no authority.**  A synthesis context describes the goal,
   the criteria to cover, the operators this deployment really registers and the
   applicability reports of the methods that did not fit — and carries none of the
   fields the system binds.  ``SYSTEM_BOUND_FIELDS`` is imported from the module that
   owns it, so this request and ``planning.planner``'s ingress cannot drift apart.
2. **A reply goes through the same six steps as a human's.**  ``accept_response``
   parses strictly and hands the proposal to ``MethodRegistry.admit``; a legal
   proposal reaches ``TRIAL_ADMITTED`` and nothing further.
3. **A model may not award itself a status or an authorship.**  The declared
   ``registry_status`` travels with the submission precisely so the protocol can
   *refuse* it and record the attempt — it is never normalised away.

The absence: there is no ``promote`` on ``MethodSynthesizer``.  Past
``TRIAL_ADMITTED`` lies the offline evaluation P8 delivers, and the registry answers
``PROMOTION_NOT_AVAILABLE`` for every target.  Succeeding once is not a promotion.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from htn_world import (  # noqa: E402
    Env,
    load_proposal,
    method,
    param,
    ref,
    seed_env,
    step,
    task_binding,
)

from agent_orchestrator.contracts.htn import (  # noqa: E402
    MethodRegistryStatus,
    RegistryAuthor,
    TaskForm,
)
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.planning.htn.applicability import (  # noqa: E402
    ApplicabilityReport,
    ApplicabilityStatus,
    assess_method,
)
from agent_orchestrator.planning.htn.registry import AdmissionVerdict, RejectionCode  # noqa: E402
from agent_orchestrator.planning.htn.synthesis import (  # noqa: E402
    SYNTHESIS_AUTHOR,
    SYNTHESIS_TERMINAL_STATUSES,
    ApplicabilityNote,
    MethodSynthesizer,
    OperatorOffer,
    SynthesisRequest,
    accept_response,
    authority_claims,
    build_request,
)
from agent_orchestrator.planning.planner import (  # noqa: E402
    SYSTEM_BOUND_FIELDS,
    parse_method_proposal,
)
from agent_orchestrator.runtime.role_templates import (  # noqa: E402
    METHOD_PROPOSAL_TAG,
    METHOD_SYNTHESIZER,
    METHOD_SYNTHESIZER_VERSION,
    ROLES,
    registered_versions,
)

MISSION = "mission-1"


# --------------------------------------------------------------------------------------
# A small deployment: one compound goal, two registered operators, no method for it
# --------------------------------------------------------------------------------------


def empty_library_env() -> Env:
    env = Env(mission=MISSION)
    env.register_type(
        "synth.goal",
        form=TaskForm.COMPOUND,
        parameters=(("subject", "string"),),
        criteria=("c-root",),
        domain="synth",
    )
    env.register_type(
        "synth.collect",
        parameters=(("subject", "string"),),
        outputs=(("result", "synth.result"),),
        capabilities=("synth.read",),
        domain="synth",
    )
    env.register_type(
        "synth.deliver",
        parameters=(("subject", "string"),),
        inputs=(("result", "synth.result", True),),
        outputs=(("receipt", "synth.receipt"),),
        capabilities=("synth.send",),
        domain="synth",
    )
    return env


def goal(env: Env) -> Any:
    return task_binding(
        env, "synth.goal", task_id="task-root", obligation="obl-root", parameters={"subject": "a"}
    )


def synthesizer(env: Env) -> MethodSynthesizer:
    return MethodSynthesizer(env.registry, env.catalog)


def legal_method(method_id: str = "synth.collect-then-deliver") -> Any:
    return method(
        method_id,
        "synth.goal",
        parameter_schema="synth.goal.params",
        steps=(
            step(
                "collect",
                "synth.collect",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("synth.read",),
            ),
            step(
                "deliver",
                "synth.deliver",
                TaskForm.PRIMITIVE,
                {
                    "subject": param("subject"),
                    "result": {"op": "output", "step": "collect", "port": "result"},
                },
                capabilities=("synth.send",),
            ),
        ),
        ordering=(("collect", "deliver"),),
        links=(("c-root", "deliver", "c-delivered"),),
        finalizer="deliver",
    )


def block(payload: dict[str, Any]) -> str:
    return f"<{METHOD_PROPOSAL_TAG}>{json.dumps(payload)}</{METHOD_PROPOSAL_TAG}>"


def proposal_block(contract: Any, **extra: Any) -> str:
    return block({"method": contract.to_json(), "rationale": "it covers the root", **extra})


# ======================================================================================
# 1. The request: typed context, no authority
# ======================================================================================


def test_the_request_names_the_goal_and_the_criteria_to_cover() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities())
    assert request.goal_task_id == "task-root"
    assert request.obligation_id == "obl-root"
    assert request.goal_signature["signature_id"] == "synth.goal"
    assert request.required_criteria == ("req-1",)
    assert request.goal_parameters == {"subject": "a"}


def test_the_request_offers_only_the_operators_the_deployment_registers() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities())
    offered = {item.task_type_id for item in request.operators}
    assert offered == {"synth.collect", "synth.deliver"}
    assert all(item.form == str(TaskForm.PRIMITIVE) for item in request.operators)


def test_the_request_copies_the_operator_refs_verbatim() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities())
    offer = next(item for item in request.operators if item.task_type_id == "synth.collect")
    declared = env.catalog.require(ref("synth.collect")).task_type_ref
    assert (offer.version, offer.content_hash) == (declared.version, declared.content_hash)


def test_an_unhealthy_capability_is_offered_with_its_unavailability_stated() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(
        goal(env), env.capabilities(unavailable=("synth.send",))
    )
    deliver = next(item for item in request.operators if item.task_type_id == "synth.deliver")
    assert deliver.unavailable_capabilities == ("synth.send",)
    assert deliver.available is False
    assert request.unavailable_capabilities == ("synth.send",)
    assert {item.task_type_id for item in request.available_operators} == {"synth.collect"}


def test_a_domain_filter_narrows_the_operator_offers() -> None:
    """The seed library ships two domains; a request may ask for only one."""

    env = seed_env(MISSION)
    compound = task_binding(env, "code.fix-failing-test", task_id="t-1", obligation="o-1")
    synth = MethodSynthesizer(env.registry, env.catalog)
    scoped = synth.build_request(compound, env.capabilities(), domain="code")
    everything = synth.build_request(compound, env.capabilities())
    assert scoped.operators
    assert all(item.domain in (None, "code") for item in scoped.operators)
    assert any(item.domain == "appworld" for item in everything.operators)
    assert len(scoped.operators) < len(everything.operators)


def test_a_goal_type_the_catalogue_declares_is_found_for_candidate_retrieval() -> None:
    env = seed_env(MISSION)
    compound = task_binding(env, "code.fix-failing-test", task_id="t-1", obligation="o-1")
    synth = MethodSynthesizer(env.registry, env.catalog)
    found = synth.goal_type_ref(compound.goal_signature)
    assert found is not None
    assert found.id == "code.fix-failing-test"
    assert env.registry.candidates_for(found, mission_id=MISSION)


def test_a_goal_type_nobody_declares_has_no_candidates_to_retrieve() -> None:
    env = empty_library_env()
    synth = synthesizer(env)
    assert synth.goal_type_ref(goal(env).goal_signature) is not None
    other = task_binding(env, "synth.goal", task_id="t-2", obligation="o-2")
    request = synth.build_request(other, env.capabilities(), mission_id=MISSION)
    assert request.rejected_methods == ()
    assert request.suggested_method_refs == ()


def test_the_request_carries_no_field_the_system_binds() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities())
    assert authority_claims(request.to_json()) == ()
    assert set(request.to_json()) & SYSTEM_BOUND_FIELDS == set()


def test_the_request_tells_the_model_which_fields_are_forbidden() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities())
    assert set(request.forbidden_fields) == SYSTEM_BOUND_FIELDS
    assert "registry_status" in request.forbidden_fields
    assert "manager_epoch" in request.forbidden_fields


def test_a_request_that_carried_an_authority_field_is_refused_at_construction() -> None:
    with pytest.raises(ContractError, match="system-bound"):
        SynthesisRequest(
            goal_task_id="t",
            obligation_id="o",
            goal_signature={"signature_id": "g", "registry_status": "ADMITTED"},
        )


def test_a_domain_parameter_named_like_an_authority_field_is_a_value_not_a_claim() -> None:
    """``planning.planner`` draws the same line: only structural keys are refused."""

    request = SynthesisRequest(
        goal_task_id="t",
        obligation_id="o",
        goal_signature={"signature_id": "g"},
        goal_parameters={"scope": "the whole repository", "manager_epoch": "n/a"},
    )
    assert authority_claims(request.to_json()) == ()
    assert request.goal_parameters["scope"] == "the whole repository"


def test_authority_claims_finds_a_nested_structural_claim() -> None:
    payload = {"operators": [{"task_type_ref": {"id": "x"}, "provenance": "system"}]}
    assert authority_claims(payload) == ("operators.[0].provenance",)


def test_the_request_names_the_output_tag_and_the_role_version() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities())
    assert request.output_tag == METHOD_PROPOSAL_TAG
    assert request.role_prompt_version == METHOD_SYNTHESIZER_VERSION


def test_the_request_has_a_stable_content_hash() -> None:
    env = empty_library_env()
    first = synthesizer(env).build_request(goal(env), env.capabilities())
    second = synthesizer(env).build_request(goal(env), env.capabilities())
    assert first.content_hash() == second.content_hash()
    narrowed = synthesizer(env).build_request(
        goal(env), env.capabilities(unavailable=("synth.send",))
    )
    assert narrowed.content_hash() != first.content_hash()


def test_a_primitive_goal_is_not_something_to_synthesise_a_method_for() -> None:
    env = empty_library_env()
    primitive = task_binding(env, "synth.collect", task_id="t-leaf", obligation="o-leaf")
    with pytest.raises(ContractError, match="compound goal"):
        synthesizer(env).build_request(primitive, env.capabilities())


def test_build_request_refuses_something_that_is_not_a_binding() -> None:
    env = empty_library_env()
    with pytest.raises(ContractError, match="TaskSemanticBindingV1"):
        synthesizer(env).build_request(object(), env.capabilities())  # type: ignore[arg-type]


def test_build_request_refuses_something_that_is_not_a_capability_snapshot() -> None:
    env = empty_library_env()
    with pytest.raises(ContractError, match="CapabilitySnapshot"):
        synthesizer(env).build_request(goal(env), object())  # type: ignore[arg-type]


def test_an_empty_library_offers_no_rejected_methods_to_explain() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities(), mission_id=MISSION)
    assert request.rejected_methods == ()


def test_a_method_that_did_not_apply_is_reported_on_its_own_axis() -> None:
    env = empty_library_env()
    env.register_predicate("synth.ready", parameters=(("subject", "string"),))
    contract = method(
        "synth.guarded",
        "synth.goal",
        parameter_schema="synth.goal.params",
        applicable=(
            {
                "op": "predicate",
                "predicate_ref": ref("synth.ready").to_json(),
                "arguments": {"subject": param("subject")},
            },
        ),
        steps=(
            step(
                "collect",
                "synth.collect",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("synth.read",),
            ),
        ),
        links=(("c-root", "collect", "c-collected"),),
        finalizer="collect",
    )
    assert env.admit(contract).admitted
    report = assess_method(
        goal(env), contract, env.snapshot(), env.capabilities(), registry=env.predicates
    )
    assert not report.applicable
    request = synthesizer(env).build_request(
        goal(env),
        env.capabilities(),
        reports={"synth.guarded@1": report},
        mission_id=MISSION,
    )
    assert [item.method_id for item in request.rejected_methods] == ["synth.guarded"]
    note = request.rejected_methods[0]
    assert note.status == str(report.status)
    assert note.needs_evidence == tuple(report.needs_evidence)


def test_an_applicable_method_is_not_reported_as_rejected() -> None:
    env = empty_library_env()
    contract = legal_method()
    assert env.admit(contract).admitted
    applicable = ApplicabilityReport(
        status=ApplicabilityStatus.APPLICABLE,
        truth=assess_method(
            goal(env), contract, env.snapshot(), env.capabilities(), registry=env.predicates
        ).truth,
    )
    request = synthesizer(env).build_request(
        goal(env),
        env.capabilities(),
        reports={f"{contract.method_id}@1": applicable},
        mission_id=MISSION,
    )
    assert request.rejected_methods == ()


def test_the_note_keeps_the_four_axes_apart() -> None:
    note = ApplicabilityNote(
        method_id="m",
        method_version=1,
        content_hash="a" * 64,
        status="CAPABILITY_UNAVAILABLE",
        truth="UNKNOWN",
        unmet_capabilities=("tool.x",),
        needs_evidence=("digest-1",),
        conflicts=("c",),
        type_errors=("t",),
    )
    payload = note.to_json()
    assert payload["unmet_capabilities"] == ["tool.x"]
    assert payload["needs_evidence"] == ["digest-1"]
    assert payload["conflicts"] == ["c"]
    assert payload["type_errors"] == ["t"]


def test_the_offer_json_states_availability_explicitly() -> None:
    offer = OperatorOffer(
        task_type_id="x",
        version=1,
        content_hash="b" * 64,
        form="primitive",
        statement="s",
        required_capabilities=("cap",),
        unavailable_capabilities=("cap",),
    )
    assert offer.to_json()["available"] is False


def test_the_module_level_build_request_matches_the_method() -> None:
    env = empty_library_env()
    direct = synthesizer(env).build_request(goal(env), env.capabilities())
    free = build_request(goal(env), env.capabilities(), env.registry, catalog=env.catalog)
    assert free.to_json() == direct.to_json()


# ======================================================================================
# 2. The response: the same admission protocol, and only to TRIAL_ADMITTED
# ======================================================================================


def test_a_legal_proposal_reaches_trial_admitted() -> None:
    env = empty_library_env()
    receipt = synthesizer(env).accept_response(proposal_block(legal_method()), policy=env.policy())
    assert receipt.verdict is AdmissionVerdict.TRIAL_ADMITTED
    assert receipt.status is MethodRegistryStatus.TRIAL_ADMITTED
    assert receipt.registration is not None
    assert receipt.registration.trial_scope_mission == MISSION


def test_a_trial_admission_is_scoped_to_this_mission_only() -> None:
    env = empty_library_env()
    receipt = synthesizer(env).accept_response(proposal_block(legal_method()), policy=env.policy())
    assert receipt.registration is not None
    assert receipt.registration.trial_scope_mission == MISSION
    other = env.registry.candidates_for(ref("synth.goal"), mission_id="mission-2")
    assert other == ()


def test_the_admitted_method_becomes_a_candidate_for_this_mission() -> None:
    env = empty_library_env()
    synthesizer(env).accept_response(proposal_block(legal_method()), policy=env.policy())
    candidates = env.registry.candidates_for(ref("synth.goal"), mission_id=MISSION)
    assert [item.method_ref.method_id for item in candidates] == ["synth.collect-then-deliver"]
    assert candidates[0].trial_scoped


def test_every_terminal_status_a_synthesis_may_reach_is_one_of_two() -> None:
    assert SYNTHESIS_TERMINAL_STATUSES == {
        MethodRegistryStatus.TRIAL_ADMITTED,
        MethodRegistryStatus.REJECTED,
    }
    assert MethodRegistryStatus.ADMITTED not in SYNTHESIS_TERMINAL_STATUSES
    assert MethodRegistryStatus.EVALUATED not in SYNTHESIS_TERMINAL_STATUSES


def test_a_model_that_declares_a_registry_status_is_refused_and_recorded() -> None:
    env = empty_library_env()
    receipt = synthesizer(env).accept_response(
        proposal_block(legal_method(), registry_status=str(MethodRegistryStatus.ADMITTED)),
        policy=env.policy(),
    )
    assert receipt.verdict is AdmissionVerdict.REJECTED
    assert RejectionCode.MODEL_CLAIMED_STATUS in receipt.codes()
    assert receipt.registration is not None
    assert receipt.registration.status is MethodRegistryStatus.REJECTED


def test_the_declared_status_is_kept_rather_than_normalised_away() -> None:
    text = proposal_block(legal_method(), registry_status=str(MethodRegistryStatus.ADMITTED))
    assert parse_method_proposal(text).declared_status is MethodRegistryStatus.ADMITTED
    assert MethodSynthesizer.proposal_declares_status(text) is True
    assert MethodSynthesizer.proposal_declares_status(proposal_block(legal_method())) is False


def test_a_model_may_not_present_itself_as_the_system() -> None:
    env = empty_library_env()
    receipt = synthesizer(env).accept_response(
        proposal_block(legal_method(), author=str(RegistryAuthor.SYSTEM)),
        policy=env.policy(),
    )
    assert receipt.verdict is AdmissionVerdict.REJECTED
    assert RejectionCode.MODEL_CLAIMED_STATUS in receipt.codes()


def test_a_proposal_naming_an_operator_nobody_registered_is_refused() -> None:
    env = empty_library_env()
    contract = method(
        "synth.ghost",
        "synth.goal",
        parameter_schema="synth.goal.params",
        steps=(step("ghost", "synth.nowhere", TaskForm.PRIMITIVE, {"subject": param("subject")}),),
        links=(("c-root", "ghost", "c-done"),),
        finalizer="ghost",
    )
    receipt = synthesizer(env).accept_response(proposal_block(contract), policy=env.policy())
    assert receipt.verdict is AdmissionVerdict.REJECTED
    assert receipt.missing_operators or RejectionCode.UNKNOWN_TASK_TYPE in receipt.codes()


def test_a_proposal_that_covers_no_root_criterion_is_refused() -> None:
    """The composition must say which child criterion carries which parent one."""

    env = empty_library_env()
    contract = method(
        "synth.uncovered",
        "synth.goal",
        parameter_schema="synth.goal.params",
        steps=(
            step(
                "collect",
                "synth.collect",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("synth.read",),
            ),
        ),
        # A link to a parent criterion the goal type does not declare covers nothing.
        links=(("c-unrelated", "collect", "c-collected"),),
        finalizer="collect",
    )
    receipt = synthesizer(env).accept_response(proposal_block(contract), policy=env.policy())
    assert receipt.verdict is AdmissionVerdict.REJECTED
    assert RejectionCode.ROOT_COVERAGE_GAP in receipt.codes()


def test_an_unreadable_block_is_a_bounded_repair_not_a_rejected_method() -> None:
    env = empty_library_env()
    with pytest.raises(ContractError, match="method proposal unreadable"):
        synthesizer(env).accept_response("我没有方法。", policy=env.policy())
    assert env.registry.method_refs() == ()


def test_accept_response_needs_a_real_admission_policy() -> None:
    env = empty_library_env()
    with pytest.raises(ContractError, match="AdmissionPolicy"):
        synthesizer(env).accept_response(proposal_block(legal_method()), policy=object())  # type: ignore[arg-type]


def test_the_module_level_accept_response_matches_the_method() -> None:
    env = empty_library_env()
    receipt = accept_response(
        proposal_block(legal_method()),
        registry=env.registry,
        catalog=env.catalog,
        policy=env.policy(),
    )
    assert receipt.verdict is AdmissionVerdict.TRIAL_ADMITTED


def test_a_scripted_seed_domain_proposal_goes_through_the_same_path() -> None:
    """The fixture block a deterministic test uses, admitted through synthesis."""

    env = seed_env(MISSION)
    payload = load_proposal("valid")
    receipt = MethodSynthesizer(env.registry, env.catalog).accept_response(
        block({"method": payload["method"], "rationale": "r"}), policy=env.policy()
    )
    assert receipt.verdict in (AdmissionVerdict.TRIAL_ADMITTED, AdmissionVerdict.REJECTED)
    assert receipt.status in SYNTHESIS_TERMINAL_STATUSES


# ======================================================================================
# 3. Promotion is not available, and the synthesiser has no way to ask for it
# ======================================================================================


def test_the_synthesizer_has_no_promote_method() -> None:
    env = empty_library_env()
    assert not hasattr(synthesizer(env), "promote")
    assert "promote" not in dir(MethodSynthesizer)


def test_the_registry_refuses_every_promotion_target() -> None:
    env = empty_library_env()
    receipt = synthesizer(env).accept_response(proposal_block(legal_method()), policy=env.policy())
    for target in (MethodRegistryStatus.EVALUATED, MethodRegistryStatus.ADMITTED):
        answer = env.registry.promote(receipt.method_ref, target, policy=env.policy())
        assert answer.verdict is AdmissionVerdict.PROMOTION_NOT_AVAILABLE


def test_succeeding_once_does_not_change_the_registration() -> None:
    env = empty_library_env()
    receipt = synthesizer(env).accept_response(proposal_block(legal_method()), policy=env.policy())
    env.registry.promote(receipt.method_ref, MethodRegistryStatus.ADMITTED, policy=env.policy())
    registration = env.registry.registration(receipt.method_ref)
    assert registration is not None
    assert registration.status is MethodRegistryStatus.TRIAL_ADMITTED


# ======================================================================================
# 4. The role template: a new role, on the planning account
# ======================================================================================


def test_the_method_synthesizer_is_a_new_role_not_a_new_planner_version() -> None:
    assert METHOD_SYNTHESIZER.name == "method_synthesizer"
    assert METHOD_SYNTHESIZER.name not in ROLES
    assert METHOD_SYNTHESIZER_VERSION in registered_versions()["method_synthesizer"]


def test_the_template_states_that_the_registry_writes_the_status() -> None:
    instructions = METHOD_SYNTHESIZER.instructions
    assert "registry_status" in instructions
    assert "注册状态由注册服务写入" in instructions
    assert "mission_planning" in instructions
    assert "Task Critic" in instructions


def test_the_template_lists_every_system_bound_field() -> None:
    instructions = METHOD_SYNTHESIZER.instructions
    missing = sorted(name for name in SYSTEM_BOUND_FIELDS if name not in instructions)
    assert missing == []


def test_the_template_asks_for_exactly_one_method_proposal_block() -> None:
    instructions = METHOD_SYNTHESIZER.instructions
    assert f"<{METHOD_PROPOSAL_TAG}>" in instructions
    assert "只输出一个" in instructions
    assert METHOD_SYNTHESIZER.tool_names == ()


def test_registering_the_new_role_left_every_other_template_alone() -> None:
    versions = registered_versions()
    assert METHOD_SYNTHESIZER_VERSION in versions["method_synthesizer"]
    for name, template in ROLES.items():
        assert template.prompt_version in versions[name]


# ======================================================================================
# 5. Mutation self-check
# ======================================================================================


def test_mutant_a_request_that_leaked_the_registry_status_would_be_caught() -> None:
    env = empty_library_env()
    good = synthesizer(env).build_request(goal(env), env.capabilities())
    leaked = {**good.to_json(), "registry_status": "TRIAL_ADMITTED"}
    assert authority_claims(good.to_json()) == ()
    assert authority_claims(leaked) == ("registry_status",)


def test_mutant_normalising_the_declared_status_would_hide_the_attempt() -> None:
    env = empty_library_env()
    claimed = proposal_block(legal_method(), registry_status=str(MethodRegistryStatus.ADMITTED))
    refused = synthesizer(env).accept_response(claimed, policy=env.policy())
    assert refused.verdict is AdmissionVerdict.REJECTED
    # The clean version of the same method is admitted, so the refusal is about the
    # claim and not about the definition.
    clean = MethodSynthesizer(empty_library_env().registry, env.catalog).accept_response(
        proposal_block(legal_method()), policy=env.policy()
    )
    assert clean.verdict is AdmissionVerdict.TRIAL_ADMITTED


def test_mutant_offering_an_operator_the_catalogue_lacks_would_be_admitted_blindly() -> None:
    env = empty_library_env()
    request = synthesizer(env).build_request(goal(env), env.capabilities())
    assert "synth.nowhere" not in {item.task_type_id for item in request.operators}
    ghost = method(
        "synth.ghost2",
        "synth.goal",
        parameter_schema="synth.goal.params",
        steps=(step("ghost", "synth.nowhere", TaskForm.PRIMITIVE, {"subject": param("subject")}),),
        links=(("c-root", "ghost", "c-done"),),
        finalizer="ghost",
    )
    assert (
        synthesizer(env).accept_response(proposal_block(ghost), policy=env.policy()).verdict
        is AdmissionVerdict.REJECTED
    )


def test_mutant_a_synthesizer_that_returned_an_admitted_receipt_would_be_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = empty_library_env()
    synth = synthesizer(env)
    real = env.registry.admit

    def promoted(*args: Any, **kwargs: Any) -> Any:
        import dataclasses

        receipt = real(*args, **kwargs)
        return dataclasses.replace(receipt, transitions=(MethodRegistryStatus.ADMITTED,))

    monkeypatch.setattr(env.registry, "admit", promoted)
    with pytest.raises(ContractError, match="not a promotion"):
        synth.accept_response(proposal_block(legal_method()), policy=env.policy())


# ======================================================================================
# 6. Review fixes (P1-7): the synthesis path attributes a reply to the model
# ======================================================================================


def test_the_synthesis_path_attributes_a_reply_to_the_model() -> None:
    """Not a default with a caller-supplied alternative: a fixed value (§6.3, §7.3)."""

    assert SYNTHESIS_AUTHOR is RegistryAuthor.MODEL
    signature = inspect.signature(MethodSynthesizer.accept_response)
    assert "author" not in signature.parameters
    assert "author_override" in signature.parameters
    assert signature.parameters["author_override"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["author_override"].default is None


def test_a_reply_admitted_through_synthesis_is_recorded_as_model_authored() -> None:
    env = empty_library_env()
    receipt = synthesizer(env).accept_response(proposal_block(legal_method()), policy=env.policy())
    assert receipt.author is RegistryAuthor.MODEL
    # Past DRAFT the *row* is always written by the registry service, which is what
    # stops a promoted status from being attributed to the submitter.
    assert receipt.registration is not None
    assert receipt.registration.author is RegistryAuthor.SYSTEM


def test_the_synthesis_path_cannot_be_used_to_launder_authorship() -> None:
    """A payload claiming SYSTEM is refused, because the path presents MODEL."""

    env = empty_library_env()
    receipt = synthesizer(env).accept_response(
        proposal_block(legal_method(), author=str(RegistryAuthor.SYSTEM)),
        policy=env.policy(),
    )
    assert receipt.verdict is AdmissionVerdict.REJECTED
    assert RejectionCode.MODEL_CLAIMED_STATUS in receipt.codes()


def test_a_caller_with_a_genuinely_system_authored_definition_says_so() -> None:
    """The override exists for a human- or tool-authored definition, and only then."""

    env = empty_library_env()
    receipt = synthesizer(env).accept_response(
        proposal_block(legal_method(), author=str(RegistryAuthor.SYSTEM)),
        policy=env.policy(),
        author_override=RegistryAuthor.SYSTEM,
    )
    assert receipt.verdict is AdmissionVerdict.TRIAL_ADMITTED
    assert receipt.author is RegistryAuthor.SYSTEM


def test_the_module_level_helper_carries_the_same_lock() -> None:
    env = empty_library_env()
    signature = inspect.signature(accept_response)
    assert "author" not in signature.parameters
    assert "author_override" in signature.parameters
    receipt = accept_response(
        proposal_block(legal_method(), author=str(RegistryAuthor.SYSTEM)),
        registry=env.registry,
        catalog=env.catalog,
        policy=env.policy(),
    )
    assert receipt.verdict is AdmissionVerdict.REJECTED


def test_mutant_defaulting_the_author_to_system_would_admit_a_model_claim() -> None:
    env = empty_library_env()
    claimed = proposal_block(legal_method(), author=str(RegistryAuthor.SYSTEM))
    assert synthesizer(env).accept_response(claimed, policy=env.policy()).verdict is (
        AdmissionVerdict.REJECTED
    )
    # The same text with the override *is* admitted, so the refusal above is about
    # who the path says produced it — which is the whole point of the lock.
    fresh = empty_library_env()
    assert (
        MethodSynthesizer(fresh.registry, fresh.catalog)
        .accept_response(claimed, policy=fresh.policy(), author_override=RegistryAuthor.SYSTEM)
        .verdict
        is AdmissionVerdict.TRIAL_ADMITTED
    )
