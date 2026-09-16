# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3a: the Planner's two typed blocks, and what the boundary refuses (§18.5 C8).

The hierarchical Planner does not draw a DAG.  It emits one
``<plan_revision_proposal>`` block — semantic operations plus the read-set it
decided on — or one ``<method_proposal>`` block, and both go through the *real*
codec before anything downstream sees them.  Four properties are pinned here:

1. **A well-formed block becomes a contract object, and nothing more.**  Holding a
   :class:`PlanProposal` proves the shape parsed; it proves nothing about coverage,
   cycles or budgets, which is the compiler's and the Commit Service's business.
2. **Authority is never model-authored.**  Every field the system binds is refused
   at the boundary — in the block, in a ``read_set`` entry and in an operation — and
   refused rather than silently overwritten, so an attempt leaves a trace instead of
   a free probe of the gate.  A *value* that happens to share a name with one of
   those fields (a method parameter called ``scope``) is not a claim.
3. **A malformed block is a BlockError, not a second request.**  Missing, doubled,
   unparseable and non-object bodies all raise through the existing bounded-repair
   path (§18.5 C8), never a new Attempt identity.
4. **The old protocol did not move.**  ``<task_graph_proposal>`` parses exactly as
   before, and every previously registered Planner / Manager prompt keeps its bytes:
   the hierarchical template is an *additional* version of the same role.

The blocks are built with the shipped fixture provider's two new scripted steps, so
none of this depends on a live model.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

from htn_world import load_proposal  # noqa: E402

from agent_orchestrator.contracts import ContractError  # noqa: E402
from agent_orchestrator.contracts.htn import (  # noqa: E402
    BindSharedGoalOperation,
    PlanProposal,
    ProposeSuccessorOperation,
    RefineOperation,
    RetireMethodOperation,
    RunningWorkPolicy,
)
from agent_orchestrator.contracts.semantic_base import content_hash_of  # noqa: E402
from agent_orchestrator.graph.task_graph import TaskGraphProposal  # noqa: E402
from agent_orchestrator.planning.htn.registry import (  # noqa: E402
    MethodProposal,
    MethodRegistryStatus,
    RegistryAuthor,
)
from agent_orchestrator.planning.planner import (  # noqa: E402
    SYSTEM_BOUND_FIELDS,
    parse_method_proposal,
    parse_plan_proposal,
    parse_task_graph_proposal,
)
from agent_orchestrator.runtime.role_templates import (  # noqa: E402
    METHOD_PROPOSAL_TAG,
    PLAN_REVISION_PROPOSAL_TAG,
    PLANNER,
    PLANNER_HIERARCHICAL,
    PLANNER_HIERARCHICAL_VERSION,
    PLANNER_V3,
    TASK_GRAPH_PROPOSAL_TAG,
    TASK_PROPOSAL_TAG,
    TEMPLATE_VERSIONS,
    registered_versions,
)
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    method_proposal_step,
    plan_revision_proposal_step,
)

MISSION = "mission-p23a"
HASH_A = "a" * 64
HASH_B = "b" * 64


# ------------------------------------------------------------------ the world builders
def _read_item(**overrides: Any) -> dict[str, Any]:
    item = {"kind": "task", "id": "task-root", "semantic_revision": 1, "content_hash": HASH_A}
    item.update(overrides)
    return item


def _refine(**overrides: Any) -> dict[str, Any]:
    operation = {
        "op": "refine",
        "goal_id": "occ-root",
        "obligation_id": "obl-root",
        "method_ref": {"id": "plan.split", "version": 1, "content_hash": HASH_B},
        "bindings": {"subject": "readme"},
    }
    operation.update(overrides)
    return operation


def _block(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "proposal_id": "prop-1",
        "expected_plan_revision": 0,
        "read_set": [_read_item()],
        "operations": [_refine()],
        "rationale": "根目标要先拆成两步",
    }
    kwargs.update(overrides)
    return plan_revision_proposal_step(**kwargs)


def _parse(**overrides: Any) -> PlanProposal:
    return parse_plan_proposal(_block(**overrides), mission_id=MISSION)


def _refused(**overrides: Any) -> str:
    with pytest.raises(ContractError) as caught:
        _parse(**overrides)
    return str(caught.value)


# ====================================================== a well-formed plan proposal
def test_a_well_formed_block_parses_into_a_plan_proposal():
    proposal = _parse()
    assert isinstance(proposal, PlanProposal)
    assert proposal.proposal_id == "prop-1"
    assert int(proposal.expected_plan_revision) == 0
    assert proposal.rationale == "根目标要先拆成两步"
    assert proposal.running_work_policy is RunningWorkPolicy.RETAIN_IF_BINDINGS_UNCHANGED


def test_the_mission_is_supplied_by_the_caller_not_the_block():
    """Which Mission a proposal belongs to is decided by the request that made it."""

    assert str(_parse().mission_id) == MISSION
    assert "mission_id" not in json.loads(
        _block()
        .removeprefix(f"<{PLAN_REVISION_PROPOSAL_TAG}>")
        .removesuffix(f"</{PLAN_REVISION_PROPOSAL_TAG}>")
    )


def test_a_block_that_names_its_own_mission_is_refused():
    detail = _refused(extras={"mission_id": "mission-somebody-elses"})
    assert "mission_id" in detail and "the system binds" in detail


def test_the_read_set_entries_keep_the_revision_and_hash_they_claim():
    proposal = _parse(read_set=[_read_item(), _read_item(kind="method", id="plan.split")])
    assert [str(item.kind) for item in proposal.read_set] == ["task", "method"]
    assert {item.content_hash for item in proposal.read_set} == {HASH_A}


def test_all_four_operation_kinds_parse():
    proposal = _parse(
        operations=[
            _refine(),
            {"op": "retire_method", "method_instance_id": "mi-1", "reason": "绑定已变"},
            {
                "op": "bind_shared_goal",
                "consumer_method_instance_id": "mi-2",
                "step": "build",
                "goal_id": "occ-shared",
                "resolution_id": None,
            },
            {
                "op": "propose_successor",
                "old_task_id": "task-failed",
                "obligation_id": "obl-root",
                "goal_type_ref": {"id": "plan.leaf", "version": 1, "content_hash": HASH_B},
                "bindings": {"subject": "readme"},
            },
        ]
    )
    assert [type(item) for item in proposal.operations] == [
        RefineOperation,
        RetireMethodOperation,
        BindSharedGoalOperation,
        ProposeSuccessorOperation,
    ]


def test_a_trigger_ref_the_model_may_legitimately_cite_parses():
    proposal = _parse(
        trigger_refs=[
            {
                "kind": "observation",
                "id": "obs-1",
                "revision": 1,
                "content_hash": HASH_A,
                "produced_by": "model",
            }
        ]
    )
    assert [ref.id for ref in proposal.trigger_refs] == ["obs-1"]


def test_prose_around_the_block_is_tolerated():
    text = "我先说明一下思路。\n" + _block() + "\n以上。"
    assert parse_plan_proposal(text, mission_id=MISSION).proposal_id == "prop-1"


def test_a_fenced_body_inside_the_block_is_tolerated():
    body = _block()
    fenced = body.replace(
        f"<{PLAN_REVISION_PROPOSAL_TAG}>", f"<{PLAN_REVISION_PROPOSAL_TAG}>```json\n"
    ).replace(f"</{PLAN_REVISION_PROPOSAL_TAG}>", f"\n```</{PLAN_REVISION_PROPOSAL_TAG}>")
    assert parse_plan_proposal(fenced, mission_id=MISSION).proposal_id == "prop-1"


# ============================================================ authority is not authored
@pytest.mark.parametrize("field", sorted(SYSTEM_BOUND_FIELDS))
def test_every_system_bound_field_is_refused_in_the_block(field):
    detail = _refused(extras={field: "claimed"})
    assert field in detail and "the system binds" in detail


def test_a_system_bound_field_in_a_read_set_entry_is_refused():
    """``fields_of`` would refuse the unknown key anyway; the point is that this is
    reported as an authority claim, at the boundary, naming the entry — "you wrote a
    field you may not write" and "I do not know this field" call for different work."""

    detail = _refused(read_set=[_read_item(manager_epoch=7)])
    assert "read_set[0]" in detail and "manager_epoch" in detail
    assert "the system binds" in detail


def test_a_system_bound_field_in_an_operation_is_refused():
    detail = _refused(operations=[_refine(authorization_ref="approval-1")])
    assert "operations[0]" in detail and "authorization_ref" in detail
    assert "the system binds" in detail


def test_the_refusal_names_every_claimed_field_at_once():
    detail = _refused(extras={"manager_epoch": 3, "principal": "root"})
    assert "manager_epoch" in detail and "principal" in detail


def test_a_method_parameter_that_shares_a_name_with_a_bound_field_is_a_value():
    """``bindings`` is domain vocabulary; refusing it there would make the contract
    depend on what a domain happens to call its parameters."""

    proposal = _parse(operations=[_refine(bindings={"scope": "src/", "principal": "reviewer"})])
    operation = proposal.operations[0]
    assert operation.bindings == {"scope": "src/", "principal": "reviewer"}


def test_a_claimed_tool_attribution_inside_a_trigger_ref_is_refused():
    """Not one of the block's own fields, so the deep provenance walk has to catch it."""

    detail = _refused(
        trigger_refs=[
            {
                "kind": "tool_receipt",
                "id": "receipt-1",
                "revision": 1,
                "content_hash": HASH_A,
                "produced_by": "tool",
            }
        ]
    )
    assert "produced_by" in detail


#: Every field the system binds, written out.  The parametrised test above iterates
#: ``SYSTEM_BOUND_FIELDS`` itself, so it can only ever say "each field I refuse is
#: refused" — deleting one from the set deletes its test with it.  This literal is
#: what makes a *shrinking* set fail: adding a field is a deliberate edit in two
#: places, and removing one is caught here.
GOLDEN_BOUND_FIELDS = frozenset(
    {
        "authored_by",
        "authorization_ref",
        "budget_account",
        "budget_grant_revision",
        "grant_ref",
        "manager_epoch",
        "mission_id",
        "opened_by",
        "principal",
        "principal_id",
        "provenance",
        "registry_status",
        "scope",
        "scope_id",
    }
)


def test_the_bound_field_set_is_exactly_the_golden_set():
    assert SYSTEM_BOUND_FIELDS == GOLDEN_BOUND_FIELDS
    assert len(GOLDEN_BOUND_FIELDS) == 14


def test_the_golden_set_names_every_authority_channel_the_gate_checks():
    assert {
        "mission_id",
        "manager_epoch",
        "scope",
        "principal",
        "budget_account",
        "registry_status",
        "authorization_ref",
        "grant_ref",
        "provenance",
    } <= GOLDEN_BOUND_FIELDS


def test_the_hierarchical_prompt_lists_every_bound_field():
    """A field refused by the parser and absent from the prompt is a trap: the model
    is never told, and its whole proposal is thrown away for writing it."""

    text = PLANNER_HIERARCHICAL.instructions
    assert sorted(field for field in GOLDEN_BOUND_FIELDS if field not in text) == []


# =========================================================== an unreadable block
def test_a_missing_block_is_reported_as_a_block_error():
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal("我想了想，先不改计划。", mission_id=MISSION)
    assert "block_missing" in str(caught.value)


def test_an_empty_output_is_reported_as_a_block_error():
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal("   ", mission_id=MISSION)
    assert "empty_output" in str(caught.value)


def test_two_blocks_are_ambiguous_rather_than_first_wins():
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal(_block() + _block(proposal_id="prop-2"), mission_id=MISSION)
    assert "block_ambiguous" in str(caught.value)


def test_a_body_that_is_not_json_is_reported_as_a_block_error():
    text = f"<{PLAN_REVISION_PROPOSAL_TAG}>{{not json</{PLAN_REVISION_PROPOSAL_TAG}>"
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal(text, mission_id=MISSION)
    assert "invalid_json" in str(caught.value)


def test_a_body_that_is_not_an_object_is_reported_as_a_block_error():
    text = f"<{PLAN_REVISION_PROPOSAL_TAG}>[1, 2]</{PLAN_REVISION_PROPOSAL_TAG}>"
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal(text, mission_id=MISSION)
    assert "not_an_object" in str(caught.value)


def test_an_unreadable_block_never_becomes_a_second_identity():
    """§18.5 C8: the repair is bounded — one ``ContractError`` back to the same
    Attempt, not a new request.  The parser is pure, so the only thing to pin is
    that it raises instead of returning a partial proposal."""

    from agent_orchestrator.runtime.output_blocks import BlockError, repair_hint

    hint = repair_hint(BlockError("block_missing", "no block"), PLAN_REVISION_PROPOSAL_TAG)
    assert PLAN_REVISION_PROPOSAL_TAG in hint


# =========================================================== the contract's own floors
def test_an_empty_read_set_is_refused():
    assert "read_set" in _refused(read_set=[])


def test_no_operations_is_refused():
    assert "operations" in _refused(operations=[])


def test_a_missing_required_field_is_refused():
    assert "rationale" in _refused(drop=("rationale",))


def test_another_schema_version_is_refused():
    assert "schema_version" in _refused(schema_version=2)


def test_an_unknown_operation_is_refused_with_the_known_ones_named():
    detail = _refused(operations=[{"op": "delete_everything", "goal_id": "occ-root"}])
    assert "refine" in detail and "retire_method" in detail


def test_an_unknown_running_work_policy_is_refused():
    assert "running_work_policy" in _refused(running_work_policy="just_kill_it")


# ============================================================ the method proposal block
def test_a_well_formed_method_proposal_parses():
    proposal = parse_method_proposal(method_proposal_step(load_proposal("valid")["method"]))
    assert isinstance(proposal, MethodProposal)
    assert proposal.author is RegistryAuthor.MODEL
    assert proposal.rationale == "脚本化方法"


def test_a_method_proposal_defaults_to_draft_status():
    proposal = parse_method_proposal(method_proposal_step(load_proposal("valid")["method"]))
    assert proposal.declared_status is MethodRegistryStatus.DRAFT


def test_a_model_claimed_registry_status_is_kept_as_a_claim():
    """Dropped here it would be refused by nobody; the admission protocol has to see
    it to record the refusal (§7.3)."""

    proposal = parse_method_proposal(
        method_proposal_step(
            load_proposal("valid")["method"], extras={"registry_status": "ADMITTED"}
        )
    )
    assert proposal.declared_status is MethodRegistryStatus.ADMITTED


def test_a_missing_method_block_is_reported_as_a_block_error():
    with pytest.raises(ContractError) as caught:
        parse_method_proposal("这个目标我没有方法。")
    assert "block_missing" in str(caught.value)


def test_a_method_block_with_bad_json_is_reported_as_a_block_error():
    with pytest.raises(ContractError) as caught:
        parse_method_proposal(f"<{METHOD_PROPOSAL_TAG}>{{</{METHOD_PROPOSAL_TAG}>")
    assert "invalid_json" in str(caught.value)


def test_a_method_block_without_a_definition_is_refused():
    with pytest.raises(ContractError):
        parse_method_proposal(f'<{METHOD_PROPOSAL_TAG}>{{"rationale":"x"}}</{METHOD_PROPOSAL_TAG}>')


def test_the_two_new_tags_are_distinct_from_each_other_and_from_the_old_ones():
    tags = {
        PLAN_REVISION_PROPOSAL_TAG,
        METHOD_PROPOSAL_TAG,
        TASK_GRAPH_PROPOSAL_TAG,
        TASK_PROPOSAL_TAG,
    }
    assert len(tags) == 4


# ============================================================ the old protocol stands
LEGACY_TASKS = [
    {
        "key": "A",
        "goal": "写 README",
        "rationale": "根目标要有交付物",
        "dependencies": [],
        "success_criteria": ["file:README.md"],
        "verification_policy": ["format_check"],
        "allowed_tools": ["workspace_write_file"],
        "budget": {"max_tokens": 20_000, "max_attempts": 3},
        "outputs": ["README.md"],
    }
]


def test_the_old_task_graph_block_parses_exactly_as_before():
    body = json.dumps({"tasks": LEGACY_TASKS})
    text = f"<{TASK_GRAPH_PROPOSAL_TAG}>{body}</{TASK_GRAPH_PROPOSAL_TAG}>"
    proposal = parse_task_graph_proposal(text)
    assert isinstance(proposal, TaskGraphProposal)
    assert [node.key for node in proposal.tasks] == ["A"]


def test_the_old_task_graph_block_is_not_subject_to_the_new_authority_check():
    """§18.5 rule 4: the legacy path keeps its exact behaviour.  A key the typed
    parser would refuse is simply an unknown field to the old codec, and whatever it
    did with one before, it still does."""

    payload = {"tasks": LEGACY_TASKS, "manager_epoch": 9}
    text = f"<{TASK_GRAPH_PROPOSAL_TAG}>{json.dumps(payload)}</{TASK_GRAPH_PROPOSAL_TAG}>"
    try:
        parsed: object = parse_task_graph_proposal(text)
    except ContractError as error:
        assert "the system binds" not in str(error)
    else:
        assert isinstance(parsed, TaskGraphProposal)


def test_a_typed_block_is_not_accepted_by_the_old_parser():
    with pytest.raises(ContractError) as caught:
        parse_task_graph_proposal(_block())
    assert "block_missing" in str(caught.value)


def test_a_dag_block_is_not_accepted_by_the_typed_parser():
    body = json.dumps({"tasks": LEGACY_TASKS})
    text = f"<{TASK_GRAPH_PROPOSAL_TAG}>{body}</{TASK_GRAPH_PROPOSAL_TAG}>"
    with pytest.raises(ContractError) as caught:
        parse_plan_proposal(text, mission_id=MISSION)
    assert "block_missing" in str(caught.value)


# ============================================================ the template registry
#: The Planner prompt versions that existed before P2.3a, each with the sha256 of its
#: exact instruction text, written out as a literal.  A new *version* is welcome; a
#: reworded old one is not, because a Mission pinned to it would replay under
#: different words (host support 0.9.8).
#:
#: The digests are literals on purpose.  Comparing two values both computed from the
#: live module — ``content_hash_of(TEMPLATE_VERSIONS[v]) == content_hash_of(PLANNER_V3)``
#: — is true no matter what the text says, so it would pass through any rewording.
#: Only a pinned constant fails when the words move.  (Same pattern as p33's
#: ``BASELINE_CHECK_AST``.)
FROZEN_PLANNER_PROMPTS = {
    "planner-v3": "353b0dd0a0727b4a8ce74fe82354d17a02b6afe0a6c883f011b77ee22c4287e9",
    "planner-v4": "13537f0abf6322c7075af9b5ddb3c0b7316c0271830311f49c3d6c195f5c9aad",
}


def _prompt_digest(version: str) -> str:
    return hashlib.sha256(
        TEMPLATE_VERSIONS["planner"][version].instructions.encode("utf-8")
    ).hexdigest()


def test_the_hierarchical_planner_is_registered_as_its_own_version():
    assert PLANNER_HIERARCHICAL_VERSION in registered_versions()["planner"]
    assert TEMPLATE_VERSIONS["planner"][PLANNER_HIERARCHICAL_VERSION] is PLANNER_HIERARCHICAL
    assert PLANNER_HIERARCHICAL.name == "planner"


def test_the_hierarchical_planner_is_an_addition_not_a_replacement():
    for version in FROZEN_PLANNER_PROMPTS:
        assert version in registered_versions()["planner"], version
    assert PLANNER.prompt_version != PLANNER_HIERARCHICAL_VERSION


@pytest.mark.parametrize(("version", "digest"), sorted(FROZEN_PLANNER_PROMPTS.items()))
def test_a_previously_registered_planner_prompt_keeps_its_exact_bytes(version, digest):
    assert _prompt_digest(version) == digest


def test_the_frozen_digests_are_literals_this_module_does_not_derive():
    """Guards the guard: a digest recomputed from the live template is always equal to
    itself, which is how a byte-freeze test quietly stops freezing anything."""

    for digest in FROZEN_PLANNER_PROMPTS.values():
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


def test_the_two_frozen_prompts_are_different_texts():
    assert len(set(FROZEN_PLANNER_PROMPTS.values())) == 2
    assert _prompt_digest("planner-v4") == _prompt_digest(PLANNER.prompt_version)
    assert _prompt_digest("planner-v3") == _prompt_digest(PLANNER_V3.prompt_version)


def test_the_hierarchical_prompt_is_not_one_of_the_frozen_texts():
    hierarchical = hashlib.sha256(PLANNER_HIERARCHICAL.instructions.encode("utf-8")).hexdigest()
    assert hierarchical not in set(FROZEN_PLANNER_PROMPTS.values())
    assert content_hash_of(PLANNER_HIERARCHICAL.instructions) != content_hash_of(
        PLANNER_V3.instructions
    )


def test_the_hierarchical_prompt_names_both_blocks_and_the_bound_fields():
    text = PLANNER_HIERARCHICAL.instructions
    assert PLAN_REVISION_PROPOSAL_TAG in text and METHOD_PROPOSAL_TAG in text
    assert "manager_epoch" in text and "registry_status" in text
    assert TASK_GRAPH_PROPOSAL_TAG not in text


def test_the_hierarchical_prompt_names_every_operation_the_codec_accepts():
    text = PLANNER_HIERARCHICAL.instructions
    for op in ("refine", "retire_method", "bind_shared_goal", "propose_successor"):
        assert op in text, op


def test_the_hierarchical_planner_asks_for_no_tools():
    """It proposes; it does not act.  A tool on this template would be a way to run
    work without going through a plan revision at all."""

    assert PLANNER_HIERARCHICAL.tool_names == ()


# ============================================================ the scripted blocks
def test_the_fixture_step_produces_exactly_one_parseable_block():
    text = plan_revision_proposal_step(read_set=[_read_item()], operations=[_refine()])
    assert text.count(f"<{PLAN_REVISION_PROPOSAL_TAG}>") == 1
    assert parse_plan_proposal(text, mission_id=MISSION).proposal_id == "prop-1"


def test_the_fixture_step_never_writes_a_mission_id_of_its_own():
    assert "mission_id" not in plan_revision_proposal_step(
        read_set=[_read_item()], operations=[_refine()]
    )


def test_the_fixture_method_step_produces_exactly_one_parseable_block():
    text = method_proposal_step(load_proposal("valid")["method"])
    assert text.count(f"<{METHOD_PROPOSAL_TAG}>") == 1
    assert parse_method_proposal(text).method.method_id == "code.fix-by-direct-patch"
