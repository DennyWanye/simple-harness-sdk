"""Approved Spec identity and rollback through the real CommitService Store.

OCC-02 source: addendum §3.1–§3.2 and §11.  This file deliberately exercises
the authenticated USER_CONFIRMED API, existing requirements/obligation rows,
commit receipts, events, and the exact reader.  It does not claim coverage of
T0 or T3: those seams have no production writer in this slice.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from pathlib import Path

import pytest

from agent_orchestrator.api.operation_completion import OperationCompletionApi
from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.operation_completion import (
    OccurrenceCompletionScopeV1,
    OperationCompletionRequirementsV1,
)
from agent_orchestrator.contracts.resolution import (
    AllExpr,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    EvaluationKind,
    RequirementClass,
    RequirementsRevision,
)
from agent_orchestrator.contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.planning_authorization import (
    PlanningLanePolicy,
    StorePlanningAuthorityReader,
    build_planning_authorization,
)
from agent_orchestrator.orchestrator.operation_completion import (
    OperationCompletionError,
    OperationCompletionReader,
)
from agent_orchestrator.orchestrator.planning_admission_commits import PlanningCommitAdmission
from agent_orchestrator.planning.plan_preview import _source_snapshot_payload
from agent_orchestrator.runtime.planning_operations import (
    StoreOperationReader,
    build_operation_snapshot,
    read_running_work,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.operation_completion_store import OperationCompletionStore
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import InjectedCrash, StoreConflict
from simple_harness.contracts import canonical_json

_FULL_TARGET = Path(__file__).resolve().parents[1]
if str(_FULL_TARGET) not in sys.path:
    sys.path.insert(0, str(_FULL_TARGET))

from test_plan_commits import ROOT_DUTY, _world  # noqa: E402

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _criterion(identifier: str) -> Criterion:
    return Criterion(
        criterion_id=identifier,
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement=f"approved requirement {identifier}",
        requirement_class=RequirementClass.REQUIRED_OUTCOME,
        evaluation_kind=EvaluationKind.SEMANTIC,
    )


def _requirements(world) -> RequirementsRevision:
    requirements = RequirementsRevision(
        revision_id="completion-requirements-1",  # type: ignore[arg-type]
        mission_id=world.mission.id,
        revision=1,
        criteria=(_criterion("criterion-report"), _criterion("criterion-delivered")),
        success_expression=AllExpr(
            (CriterionExpr("criterion-report"), CriterionExpr("criterion-delivered"))
        ),
        authority_subject="authenticated-user-confirmation",
    )
    HtnStore(world.store).insert_requirements_revision(requirements)
    return requirements


def _bind_new_protocol(world) -> None:
    PlanningDecisionStore(world.store).bind_mission_protocol(
        world.mission.id,
        protocol_version="planning-decision-v1",
        package_version=6,
        prompt_version="planner-hierarchical-v9",
        binding_hash=HASH_A,
    )


def _requirements_ref(requirements: RequirementsRevision) -> TypedRef:
    return TypedRef(
        kind=TypedRefKind.REQUIREMENTS,
        id=str(requirements.revision_id),
        revision=int(requirements.revision),
        content_hash=requirements.content_hash(),
    )


def _proposal(
    requirements: RequirementsRevision, *, milestone: str = "DELIVERED"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_id": requirements.mission_id,
        "requirements_ref": {
            "id": str(requirements.revision_id),
            "revision": int(requirements.revision),
            "content_hash": requirements.content_hash(),
        },
        "mode": "REQUIRED_EFFECTS",
        "content_criterion_ids": ["criterion-report"],
        "effects": [
            {
                "effect_key": "deliver-report",
                "obligation_id": ROOT_DUTY,
                "criterion_ids": ["criterion-delivered"],
                "required_milestone": milestone,
                "milestone_policy_ref": {
                    "id": "approved-milestone-policy",
                    "revision": 1,
                    "content_hash": HASH_B,
                },
                "evidence_policy_ref": {
                    "id": "approved-evidence-policy",
                    "revision": 1,
                    "content_hash": HASH_C,
                },
                "source_slot_key": "approved-delivery-slot",
            }
        ],
    }


def _command(
    requirements: RequirementsRevision,
    *,
    command_id: str = "confirm-completion-1",
    milestone: str = "DELIVERED",
) -> dict[str, object]:
    return {
        "mission_id": requirements.mission_id,
        "command_id": command_id,
        "expected_requirements_ref": _requirements_ref(requirements).to_json(),
        "proposal": _proposal(requirements, milestone=milestone),
    }


def _api(world, principal_id: str = "human-confirming") -> OperationCompletionApi:
    return OperationCompletionApi(
        world.service,
        tenant_id=world.mission.tenant_id,
        principal=Principal(principal_id),
    )


def _approval_world(tmp_path):
    world = _world(tmp_path, key="completion-spec-approval")
    _bind_new_protocol(world)
    return world, _requirements(world)


def _completion_counts(world) -> tuple[int, int, int]:
    return (
        int(world.store.connection.execute("SELECT count(*) FROM commit_receipts").fetchone()[0]),
        int(
            world.store.connection.execute(
                "SELECT count(*) FROM operation_completion_specs"
            ).fetchone()[0]
        ),
        len(world.store.list_events(world.mission.id)),
    )


def test_occ02_user_confirmation_persists_exact_spec_and_source_identity(tmp_path) -> None:
    """OCC-02 / approval writer + CompletionReader seam.

    The test creates real Mission, root Obligation, requirements revision, protocol
    binding, receipt and event.  The confirmation command itself is the only
    USER_CONFIRMED authority input; no test-only approved boolean is fabricated.
    """

    world, requirements = _approval_world(tmp_path)
    command = _command(requirements)
    receipt = _api(world).approve(command)

    assert receipt.mission_id == world.mission.id
    assert receipt.requirements_ref == _requirements_ref(requirements)
    assert receipt.authority.kind == "USER_CONFIRMED"
    assert receipt.authority.issuer_id == "human-confirming"
    assert receipt.authority.requirements_ref == _requirements_ref(requirements)
    stored = OperationCompletionReader(world.store).read_requirements(
        world.mission.id, _requirements_ref(requirements)
    )
    assert (
        stored.to_json()
        == OperationCompletionRequirementsV1.from_json(command["proposal"]).to_json()
    )
    assert world.store.get_receipt(command["command_id"]) == receipt.to_json()
    assert [event.type for event in world.store.list_events(world.mission.id)][-1] == (
        "OperationCompletionSpecApproved"
    )


@pytest.mark.parametrize(
    "mutation,expected_code",
    (
        (
            lambda command: {
                **command,
                "expected_requirements_ref": {
                    **command["expected_requirements_ref"],  # type: ignore[index]
                    "kind": "task",
                },
            },
            "OP_REF_KIND_UNSUPPORTED",
        ),
        (
            lambda command: {
                **command,
                "expected_requirements_ref": {
                    **command["expected_requirements_ref"],  # type: ignore[index]
                    "content_hash": HASH_D,
                },
            },
            "OP_PAYLOAD_HASH_MISMATCH",
        ),
        (
            lambda command: {
                **command,
                "proposal": {**command["proposal"], "mission_id": "other-mission"},  # type: ignore[index]
            },
            "OP_EFFECT_SCOPE_STALE",
        ),
        (
            lambda command: {
                **command,
                "proposal": {
                    **command["proposal"],  # type: ignore[index]
                    "effects": [
                        {
                            **command["proposal"]["effects"][0],  # type: ignore[index]
                            "obligation_id": "unregistered-obligation",
                        }
                    ],
                },
            },
            "OP_COMPLETION_SCOPE_UNRESOLVED",
        ),
    ),
)
def test_occ02_approval_rejects_wrong_reference_or_unapproved_scope(
    tmp_path, mutation, expected_code: str
) -> None:
    """OCC-02 / API+Commit revalidation: identity/mapping errors make no side rows."""

    world, requirements = _approval_world(tmp_path)
    before = _completion_counts(world)
    with pytest.raises(OperationCompletionError) as caught:
        _api(world).approve(mutation(_command(requirements)))

    assert caught.value.code == expected_code
    assert _completion_counts(world) == before


def test_occ02_same_command_is_a_receipt_replay_but_changed_spec_or_issuer_conflicts(
    tmp_path,
) -> None:
    """OCC-02 / receipt identity seam: no mutable approval or principal-only replay."""

    world, requirements = _approval_world(tmp_path)
    command = _command(requirements)
    first = _api(world).approve(command)
    before = _completion_counts(world)

    assert _api(world).approve(command).to_json() == first.to_json()
    assert _completion_counts(world) == before
    with pytest.raises(OperationCompletionError, match="command identity or caller differs"):
        _api(world).approve(_command(requirements, milestone="RECEIVED"))
    with pytest.raises(OperationCompletionError, match="command identity or caller differs"):
        _api(world, principal_id="another-authenticated-human").approve(command)
    assert _completion_counts(world) == before


@pytest.mark.parametrize(
    "fault",
    ("completion_spec_after_receipt", "completion_spec_after_spec", "completion_spec_after_event"),
)
def test_occ02_approval_faults_roll_back_receipt_spec_and_event_together(
    tmp_path, fault: str
) -> None:
    """OCC-09 / approval transaction seam.

    This is the first-side-binding analogue of OCC-09: a crash between any
    approval write leaves no consumable receipt, Spec row, or approval event.
    Scope/acceptance/outcome atomicity remains future T0/T3 work.
    """

    world, requirements = _approval_world(tmp_path)
    command = _command(requirements)
    before = _completion_counts(world)
    world.store.arm(f"{fault}:operation_completion")

    with pytest.raises(InjectedCrash, match=fault):
        _api(world).approve(command)

    assert _completion_counts(world) == before
    assert world.store.get_receipt(command["command_id"]) is None
    with pytest.raises(OperationCompletionError) as missing:
        OperationCompletionReader(world.store).read_requirements(
            world.mission.id, _requirements_ref(requirements)
        )
    assert missing.value.code == "OP_REQUIREMENT_MAPPING_MISSING"


def test_occ02_cross_tenant_approval_cannot_read_or_create_a_spec(tmp_path) -> None:
    """OCC-02 / authenticated API boundary: another tenant learns no receipt detail."""

    world, requirements = _approval_world(tmp_path)
    before = _completion_counts(world)
    foreign = OperationCompletionApi(
        world.service, tenant_id="different-tenant", principal=Principal("human-confirming")
    )

    with pytest.raises(OperationCompletionError) as caught:
        foreign.approve(_command(requirements))

    assert caught.value.code == "not_found"
    assert _completion_counts(world) == before


@pytest.mark.parametrize("kind", ("model", "provider"))
def test_user_confirmation_boundary_rejects_nonhuman_principal(tmp_path, kind: str) -> None:
    from agent_orchestrator.api.operation_completion import bind_requirement_authority

    world, requirements = _approval_world(tmp_path)
    ref = _requirements_ref(requirements)
    proposal = OperationCompletionRequirementsV1.from_json(_proposal(requirements))
    principal = Principal("human-confirming")
    authority = bind_requirement_authority(
        principal=principal,
        tenant_id=world.mission.tenant_id,
        command_id="confirm-malformed-caller",
        mission_id=world.mission.id,
        requirements_ref=ref,
        normalized_spec=proposal,
    )
    # Normal Principal construction already rejects nonhuman kinds. Exercise
    # the Commit trust boundary with an invalid internal instance as well.
    malformed = object.__new__(Principal)
    object.__setattr__(malformed, "principal_id", principal.principal_id)
    object.__setattr__(malformed, "display", principal.display)
    object.__setattr__(malformed, "kind", kind)
    before = _completion_counts(world)
    with pytest.raises(OperationCompletionError) as api_refusal:
        OperationCompletionApi(
            world.service, tenant_id=world.mission.tenant_id, principal=malformed
        )
    assert api_refusal.value.code == "OP_REQUIREMENT_MAPPING_UNAPPROVED"
    with pytest.raises(OperationCompletionError) as commit_refusal:
        world.service.approve_operation_completion_spec(
            mission_id=world.mission.id,
            command_id="confirm-malformed-caller",
            expected_requirements_ref=ref,
            proposal=proposal,
            requirement_authority=authority,
            principal=malformed,
        )
    assert commit_refusal.value.code == "OP_REQUIREMENT_MAPPING_UNAPPROVED"
    assert _completion_counts(world) == before


def _requirements_r2(world) -> RequirementsRevision:
    """A real amended RequirementsRevision, not a fake reader return value."""

    requirements = RequirementsRevision(
        revision_id="completion-requirements-2",  # type: ignore[arg-type]
        mission_id=world.mission.id,
        revision=2,
        criteria=(_criterion("criterion-report"), _criterion("criterion-delivered")),
        success_expression=AllExpr(
            (CriterionExpr("criterion-report"), CriterionExpr("criterion-delivered"))
        ),
        authority_subject="authenticated-user-confirmation-amendment",
    )
    HtnStore(world.store).insert_requirements_revision(requirements)
    return requirements


def test_occ02_requirements_amendment_stales_old_reader_but_keeps_old_replay_receipt(
    tmp_path,
) -> None:
    """OCC-05/OCC-02 / CompletionReader revalidation seam.

    r1 approval remains historical and its exact command may replay, but it may
    not satisfy completion after an actual r2 Requirements row supersedes it.
    The new r2 reader must report the missing approved mapping rather than infer
    CONTENT_ONLY from no intent/action/spec row.
    """

    world, r1 = _approval_world(tmp_path)
    command = _command(r1)
    first = _api(world).approve(command)
    r2 = _requirements_r2(world)

    with pytest.raises(OperationCompletionError) as stale:
        OperationCompletionReader(world.store).read_requirements(
            world.mission.id, _requirements_ref(r1)
        )
    assert stale.value.code == "OP_EFFECT_SCOPE_STALE"
    with pytest.raises(OperationCompletionError) as missing:
        OperationCompletionReader(world.store).read_requirements(
            world.mission.id, _requirements_ref(r2)
        )
    assert missing.value.code == "OP_REQUIREMENT_MAPPING_MISSING"
    assert _api(world).approve(command).to_json() == first.to_json()
    with pytest.raises(OperationCompletionError) as still_stale:
        OperationCompletionReader(world.store).read_requirements(
            world.mission.id, _requirements_ref(r1)
        )
    assert still_stale.value.code == "OP_EFFECT_SCOPE_STALE"


def test_occ02_different_command_cannot_replace_spec_for_same_requirements(tmp_path) -> None:
    """OCC-02 / immutable `(mission, requirements_revision)` Spec identity.

    A new command changing the milestone needs a new RequirementsRevision.  Its
    failure leaves the original receipt, Spec row and approval event byte-for-byte
    usable for the original requirements identity.
    """

    world, requirements = _approval_world(tmp_path)
    first_command = _command(requirements)
    first = _api(world).approve(first_command)
    before = _completion_counts(world)

    with pytest.raises((OperationCompletionError, StoreConflict)):
        _api(world).approve(
            _command(
                requirements,
                command_id="confirm-completion-2",
                milestone="RECEIVED",
            )
        )

    assert _completion_counts(world) == before
    assert world.store.get_receipt(first_command["command_id"]) == first.to_json()
    assert (
        OperationCompletionReader(world.store)
        .read_requirements(world.mission.id, _requirements_ref(requirements))
        .content_hash()
        == first.spec_hash
    )


def _single_completion_command(world, requirements_revision: int, *, outputs=()):
    from test_plan_commits import root_network, task_binding

    from agent_orchestrator.contracts.htn import ObligationCoverage

    world.env.register_type(
        "completion.single", criteria=("criterion-report",), domain="plan", outputs=outputs
    )
    binding = task_binding(
        world.env, "completion.single", task_id="task-completion-root", obligation=ROOT_DUTY
    )
    network = dataclasses.replace(root_network(world.env, binding), plan_revision=1)
    coverage = (
        ObligationCoverage(
            obligation_id=ROOT_DUTY,
            criterion_ids=tuple(binding.goal_signature.coverage_criteria),
            covered_by=network.root_occurrence_ids,
        ),
    )
    network = dataclasses.replace(network, obligation_coverage=coverage)
    delta = dataclasses.replace(
        world.command.delta,
        method_instances=(),
        occurrences=network.occurrences,
        order_constraints=(),
        data_requirements=(),
        obligation_coverage=coverage,
        obligation_openings=(),
        referenced_occurrences=(),
        read_set=dataclasses.replace(
            world.command.read_set, requirements_revision=requirements_revision
        ),
    )
    return dataclasses.replace(
        world.command, delta=delta, network=network, task_bindings=(binding,)
    )


def _root_scope_document(
    world, requirements: RequirementsRevision, spec_hash: str
) -> dict[str, object]:
    """Build from an actual committed plan membership and semantic Task contract."""

    # Requirements were approved after this fixture's initial planner capture.
    # Re-capture the real semantic read-set at r1; the Commit guard still sees an
    # ordinary command and verifies it rather than this test bypassing the guard.
    refreshed = _single_completion_command(world, int(requirements.revision))
    receipt = _commit_with_refreshed_admission(world, refreshed, int(requirements.revision))
    assert receipt.command_id == world.command.command_id
    htn = HtnStore(world.store)
    plan = htn.active_plan_revision(world.mission.id)
    assert plan is not None
    root_occurrence = str(refreshed.network.root_occurrence_ids[0])
    member = world.store.connection.execute(
        "SELECT task_id, obligation_id FROM plan_memberships "
        "WHERE mission_id=? AND revision=? AND occurrence_id=?",
        (world.mission.id, plan.revision, root_occurrence),
    ).fetchone()
    assert member is not None
    semantic = htn.task_semantics_of(world.mission.id, str(member["task_id"]))
    assert semantic is not None
    return {
        "schema_version": 1,
        "mission_id": world.mission.id,
        "requirements_ref": {
            "id": str(requirements.revision_id),
            "revision": int(requirements.revision),
            "content_hash": requirements.content_hash(),
        },
        "spec_hash": spec_hash,
        "plan_ref": {"revision": plan.revision, "snapshot_hash": plan.snapshot_hash},
        "occurrence_id": root_occurrence,
        "task_ref": {
            "id": str(semantic.task_id),
            "revision": int(semantic.contract_revision),
            "content_hash": semantic.contract_hash,
        },
        "obligation_id": str(member["obligation_id"]),
        "role": "MIXED",
        "content_criterion_ids": ["criterion-report"],
        "required_effect_keys": ["deliver-report"],
        "owned_effect_keys": ["deliver-report"],
    }


def _commit_with_refreshed_admission(world, command, requirements_revision: int):
    """Open a fresh request/authority/preview admission for the fresh read-set."""

    request_id = "completion-scope-request-r1"
    from agent_orchestrator.contracts.planning_decisions import PlanningRequestBinding

    binding = PlanningRequestBinding(
        request_id=request_id,
        mission_id=world.mission.id,
        protocol_version="planning-decision-v1",
        package_version=6,
        package_hash=HASH_A,
        base_plan_revision=0,
        requirements_revision=requirements_revision,
        scope_epoch_digest=HASH_B,
        subject_bindings_hash=HASH_C,
        visible_refs_digest=HASH_D,
        prompt_version="planner-hierarchical-v9",
        prompt_hash=HASH_A,
        created_at=world.store.now,
        intent_id="completion-scope-intent-r1",
    )
    decisions = PlanningDecisionStore(world.store)
    decisions.insert_planning_request(binding)
    grant = PlanningAuthorizationApi(
        world.store,
        tenant_id=world.mission.tenant_id,
        principal=Principal(world.principal.principal_id),
    ).issue(world.mission.id, command_id="completion-scope-grant-r1", request_id=request_id)
    from agent_orchestrator.governance.planning_authorization import planning_policy_for_mission
    authority = build_planning_authorization(
        request_id,
        read=StorePlanningAuthorityReader(PlanningAdmissionStore(world.store), world.store),
        caller=world.principal,
        policy=planning_policy_for_mission(world.store, world.mission.id),
        now_ms=int(world.store.now * 1000),
    )
    from agent_orchestrator.governance.planning_authorization import PlanningAuthorizationSnapshot
    assert isinstance(authority, PlanningAuthorizationSnapshot), authority
    operations = build_operation_snapshot(
        world.mission.id, reader=StoreOperationReader(world.store)
    )
    runtime_work = read_running_work(world.mission.id, (), reader=StoreOperationReader(world.store))
    compilation_hash = hashlib.sha256(
        canonical_json(
            {"delta": command.delta.to_json(), "network": _source_snapshot_payload(command.network)}
        ).encode("utf-8")
    ).hexdigest()
    admission = PlanningCommitAdmission(
        request_id=request_id,
        decision_hash=HASH_C,
        decision_key="REFINE",
        authority=authority,
        operations=operations,
        runtime_work=runtime_work,
        preview_request_id=request_id,
        preview_decision_hash=HASH_C,
        preview_compilation_hash=compilation_hash,
        preview_read_set_hash=content_hash_of(command.read_set.to_json()),
    )
    assert grant.grant_id
    return world.service.commit_planning_revision(command, world.principal, admission=admission)


def test_occ02_scope_store_revalidates_exact_spec_plan_task_and_unique_identity(tmp_path) -> None:
    """OCC-02/OCC-10 / Plan Commit + OperationCompletionStore seam.

    The test does not manufacture a parent table: the Scope points at an actual
    plan receipt, plan membership and TaskSemanticBinding.  A Scope with a stale
    Requirements pin must fail even when its spec hash happens to name a row;
    otherwise a cross-Spec reader could silently reuse old completion proof.
    """

    world, requirements = _approval_world(tmp_path)
    approved = _api(world).approve(_command(requirements))
    document = _root_scope_document(world, requirements, approved.spec_hash)
    scope = OccurrenceCompletionScopeV1.from_json(document)
    completion = OperationCompletionStore(world.store)

    stale_requirements = dict(document)
    stale_requirements["requirements_ref"] = {
        "id": "requirements-after-amendment",
        "revision": 2,
        "content_hash": HASH_D,
    }
    stale_scope = OccurrenceCompletionScopeV1.from_json(stale_requirements)
    with world.store.transaction(), pytest.raises(StoreConflict):
        completion.insert_scope(
            stale_scope.scope_id,
            stale_scope,
            plan_receipt_id=world.command.command_id,
        )

    with world.store.transaction():
        stored = completion.insert_scope(
            scope.scope_id,
            scope,
            plan_receipt_id=world.command.command_id,
        )
    assert stored["scope_id"] == scope.scope_id
    assert stored["document"].to_json() == scope.to_json()

    # Task contract hash is not the whole semantic binding hash. Input/dispatch
    # generations may change the binding without creating a new Task contract.
    binding = HtnStore(world.store).task_semantics_of(world.mission.id, scope.task_ref.id)
    assert binding is not None and binding.content_hash() != binding.contract_hash
    wrong_contract = scope.to_json()
    wrong_contract["task_ref"]["content_hash"] = binding.content_hash()
    wrong_scope = OccurrenceCompletionScopeV1.from_json(wrong_contract)
    with world.store.transaction(), pytest.raises(StoreConflict):
        completion.insert_scope(
            wrong_scope.scope_id, wrong_scope, plan_receipt_id=world.command.command_id
        )

    before = world.store.connection.total_changes
    with world.store.transaction():
        replay = completion.insert_scope(
            scope.scope_id,
            scope,
            plan_receipt_id=world.command.command_id,
        )
    assert replay == stored
    assert world.store.connection.total_changes == before

    # Scope id is derived from (mission, plan revision, occurrence); callers may
    # not choose a second identity to evade the per-occurrence UNIQUE constraint.
    with world.store.transaction(), pytest.raises(StoreConflict):
        completion.insert_scope(
            "caller-selected-scope-id",
            scope,
            plan_receipt_id=world.command.command_id,
        )


def test_occ02_real_migration_installs_completion_tables_and_immutability_triggers(
    tmp_path,
) -> None:
    """OCC-09 / real Store migration seam, including trigger-body parsing.

    Opening a Store applies the production migration iterator (not an abbreviated
    SQLite fixture).  Approval then creates a real immutable Spec row, whose
    trigger rejects an attempted rewrite and preserves its canonical document.
    """

    world, requirements = _approval_world(tmp_path)
    receipt = _api(world).approve(_command(requirements))
    required_tables = {
        "operation_completion_specs",
        "operation_completion_scopes",
        "operation_outcome_review_bindings",
        "operation_acceptance_scopes",
    }
    names = {
        row[0]
        for row in world.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert required_tables <= names
    assert world.store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    original = world.store.connection.execute(
        "SELECT document_json FROM operation_completion_specs WHERE spec_id=?", (receipt.spec_id,)
    ).fetchone()[0]

    with pytest.raises(Exception, match="immutable completion spec"):
        world.store.connection.execute(
            "UPDATE operation_completion_specs SET document_json='{}' WHERE spec_id=?",
            (receipt.spec_id,),
        )
    assert (
        world.store.connection.execute(
            "SELECT document_json FROM operation_completion_specs WHERE spec_id=?",
            (receipt.spec_id,),
        ).fetchone()[0]
        == original
    )
