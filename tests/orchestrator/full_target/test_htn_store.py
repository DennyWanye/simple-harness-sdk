# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.2 red tests: migration 16 and the full-target storage layer.

Three things are pinned here, in this order of importance:

1. **The old library is untouched.**  The fifteen existing migrations keep their
   checksums byte for byte, a legacy run writes nothing into the new tables, and
   the upgrade is rehearsed on a *copy* of a v15 library (§20 rule 6).
2. **Identity is enforced by the schema, not by good manners.**  One ACTIVE plan
   revision per Mission, one official review per package, one adopted resolution
   per obligation, one ``request_hash`` per ``OperationId``.
3. **A transaction is all-or-nothing.**  Writing several tables inside one failing
   ``store.transaction()`` leaves every one of them as it was.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from full_target_world import (
    HASH_A,
    HASH_B,
    HASH_C,
    goal_signature,
    method_contract,
    task_binding,
    tref,
    vref,
)

from agent_orchestrator.contracts import Budget, ContractError, Mission, MissionStatus
from agent_orchestrator.contracts.evidence_state import (
    Availability,
    ObservationRecord,
    QueryCompleteness,
    TruthValue,
    Validity,
    ValidityWitness,
    WitnessDecision,
    WitnessPurpose,
)
from agent_orchestrator.contracts.htn import (
    AbsenceRead,
    BoundInput,
    ChildBinding,
    DataRequirement,
    MethodInstanceDraft,
    MethodRegistryStatus,
    OccurrenceSpec,
    OrderConstraint,
    ReadItem,
    ReadItemKind,
    RegistryAuthor,
    ReleaseCondition,
    Requiredness,
    ReusePolicy,
    ScopeEpochRead,
    SemanticReadSet,
    SourceRevisionPolicy,
    SupportSetRead,
    TaskForm,
    admit_method,
)
from agent_orchestrator.contracts.resolution import (
    Acceptance,
    AllExpr,
    CheckExecution,
    Criterion,
    CriterionExpr,
    CriterionOrigin,
    CriterionOutcome,
    CriterionVerdict,
    EvaluationKind,
    GoalResolution,
    OperationEnvelope,
    RequirementClass,
    RequirementsRevision,
    ResolutionCriterion,
    ReviewBinding,
    ReviewPackage,
    ReviewPurpose,
    ReviewRecord,
    ReviewVerdict,
)
from agent_orchestrator.contracts.semantic_base import TypedRefKind
from agent_orchestrator.knowledge.validity import (
    NO_SUBJECT,
    acceptance_subject,
    condition_subject,
    witness_subject,
)
from agent_orchestrator.storage import (
    acceptance_receipt_schema,
    htn_schema,
    planning_decision_schema,
    schema,
    validity_subject_schema,
)
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.store import Store, StoreConflict
from simple_harness.contracts import canonical_json

MISSION = "mission-1"

#: The checksums of every migration that existed before P1.2.  They are hard-coded
#: on purpose: a changed byte in an old DDL string would silently make every
#: deployed library "incompatible", and this list is what makes that a test
#: failure instead of a support ticket.
FROZEN_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "orchestrator-step02", "711c070232087159f141d387eee1ec1b11e427b55a55d6cd87dc93ab6f756a49"),
    (2, "orchestrator-step04", "547d35c47c9326df29d8a474dcdd5b642061c381111246df62e549022a620b07"),
    (3, "orchestrator-step05", "53d6097f712b85371abfe39509e4612570a8ac23049590fcb2595a7529d42a81"),
    (4, "orchestrator-step06", "546563801dd8061669c957f696c92eccae635d259099d33e0048d7c86e826785"),
    (5, "orchestrator-step07", "f04b968c0a2b5033178a143d76d9d1a106013bc439d763466429e9f022e10c70"),
    (6, "orchestrator-step09", "f80cc0190a2f686fcba49bb2b69a7a4937c3ac025740d15511034469ff7208c3"),
    (7, "orchestrator-p32", "59c00976a6ee23f1e132954472f225e858038866e48c46471412d2c6c3b79c20"),
    (
        8,
        "orchestrator-p33-domains",
        "8b737743812186af014161270627587e8beee29e227f9d912e233735482df1bd",
    ),
    (
        9,
        "orchestrator-p33-sources",
        "125be81cc959ed86c259c42be38a31e4a2a2f149c0ea1a422794435503a54e25",
    ),
    (
        10,
        "orchestrator-p33-assessments",
        "7f2967659291090748b540c43e01f197058f5623f9ac7ad858778e292efb92b0",
    ),
    (
        11,
        "orchestrator-p35-provider-admission",
        "afce839e65b2bac438ea00d196554e9031ca8ce6b5295cb3fe3eb47d9806671c",
    ),
    (
        12,
        "orchestrator-p34-selection",
        "a178383c2be4093e336e8373db3f954c75ce8ede42d85ad9faedbd5ddd85c72f",
    ),
    (
        13,
        "orchestrator-tail-reservations",
        "86380589044015c834a27445ea43eaec3581481e8312369cf15d175c03b8dffb",
    ),
    (
        14,
        "orchestrator-p34-fragments",
        "92870145c6f0e5c9dfeb348de8381e1c7a8ec79fd569ea5eadaba118c0d0d576",
    ),
    (
        15,
        "orchestrator-mission-system-tail",
        "9344750cddb8a3e5673ba7485e7bf4daea61cf8d48c7e5ea5d706c525b513f8b",
    ),
)

STEP02_TESTS = Path(__file__).resolve().parents[1] / "step02" / "test_store_and_budgets.py"
SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "agent_orchestrator"

#: Migration 16 as shipped.  Both are snapshots on purpose: once a library has been
#: upgraded, changing the DDL string renames nothing and fixes nothing — it only makes
#: every deployed library report a schema mismatch.  A deliberate change edits these
#: two constants in the same commit; an accidental one fails here.
MIGRATION_16_CHECKSUM = "239e8fdcd6a3f4ebb6fbe0073d416c2f6607ca927dc2a324bcdd322fcf369f62"
MIGRATION_16_TABLES: tuple[str, ...] = (
    "task_semantics",
    "method_contracts",
    "method_instances",
    "method_child_occurrences",
    "plan_revisions",
    "plan_memberships",
    "order_constraints",
    "data_requirements",
    "bound_inputs",
    "input_manifests",
    "input_manifest_bindings",
    "obligations",
    "obligation_relations",
    "obligation_expansions",
    "obligation_shape_changes",
    "requirements_revisions",
    "review_packages",
    "review_records",
    "criterion_evaluations",
    "acceptances",
    "goal_resolutions",
    "validity_witnesses",
    "observations",
    "justification_sets",
    "support_members",
    "validity_epochs",
    "validity_dirty",
    "operation_identities",
    "operation_bindings",
    "plan_read_sets",
    "plan_commit_receipts",
)

#: Migration 17 (P2.3c part 2) as shipped, pinned for the same reason as 16.  It is a
#: *separate* migration and not an edit to 16: a library that already ran 16 has to be
#: able to apply 17 on top of it, which a changed 16 would make impossible.
MIGRATION_17_CHECKSUM = "ffb48ba4314a625adcba69e33e83bd8b06f921621282e16beb62c48da66531b1"
MIGRATION_17_TABLES: tuple[str, ...] = (
    "acceptance_commit_receipts",
    "delivery_receipts",
    "acceptance_outputs",
)

#: Migration 18 (P2.3c part 2d, decision 1) as shipped.  It creates no table: it adds
#: ``validity_witnesses.subject_digest`` and re-keys the unique index on it, so that a
#: consumer may hold its DATA licence and its precondition licence at the same time.
MIGRATION_18_CHECKSUM = "a24b4ef345f3ef46ec4b43ee3d68da5f6cc3372aae7968dcc0b7b7a9efd671d8"

#: Every table the full-target migrations own.  The raw-SQL leak guard and the
#: "storage is the only writer" checks iterate this, so a migration that adds a table
#: without adding it here would ship an unguarded table.
FULL_TARGET_TABLES: tuple[str, ...] = (*MIGRATION_16_TABLES, *MIGRATION_17_TABLES)

#: The full-target tables are storage-owned.  Nothing outside ``storage/`` may name one
#: in SQL of its own; the accessors on ``HtnStore`` / ``ObligationStore`` are the way in.
#: The whitelist is **empty**, and that is the point: P1.3's raw read of
#: ``task_semantics`` in ``obligation_commits.py`` went through
#: ``HtnStore.task_semantics_of`` and the entry was deleted with it (P2.3c part 2c,
#: review F18 — the comment used to still describe the debt).  Any leak fails
#: immediately, because the check is a subset test against nothing.
KNOWN_RAW_SQL_DEBT: frozenset[tuple[str, str]] = frozenset()
SQL_ACCESS = ("FROM", "INTO", "UPDATE", "JOIN", "TABLE")


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def _mission(mission_id: str = MISSION) -> Mission:
    return Mission(
        id=mission_id,
        goal="g",
        success_criteria=("ok",),
        stop_conditions=(),
        allowed_tools=(),
        risk_level="sandbox",
        budget=Budget(max_tokens=1000, max_attempts=2),
        tenant_id="t",
        status=MissionStatus.CREATED,
        created_at=1.0,
        version=1,
        idempotency_key=mission_id,
    )


def read_set(*, requirements_revision: int = 1) -> SemanticReadSet:
    return SemanticReadSet(
        requirements_revision=requirements_revision,
        goal_revisions=(
            ReadItem(kind=ReadItemKind.TASK, id="task-1", semantic_revision=1, content_hash=HASH_A),
        ),
        method_revisions=(
            ReadItem(
                kind=ReadItemKind.METHOD,
                id="compare-two-sources",
                semantic_revision=1,
                content_hash=HASH_B,
            ),
        ),
        observation_revisions=(),
        acceptance_revisions=(),
        manager_epoch=2,
        budget_grant_revision=3,
        support_sets=(
            SupportSetRead(support_set_id="support-1", revision=1, member_digest=HASH_C),
        ),
        scope_epochs=(ScopeEpochRead(scope_id="mission-1", validity_epoch=4),),
        absences=(
            AbsenceRead(predicate="has-active-writer", scope_id="mission-1", range_revision=1),
        ),
    )


def child_binding(
    *,
    instance_id: str = "instance-1",
    slot_key: str = "extract",
    occurrence: str = "occurrence-1",
    obligation: str = "obligation-2",
    reuse_policy: ReusePolicy = ReusePolicy.NEW_WORK,
    goal_occurrence: str | None = None,
) -> ChildBinding:
    return ChildBinding(
        instance_id=instance_id,  # type: ignore[arg-type]
        slot_key=slot_key,
        occurrence_id=occurrence,  # type: ignore[arg-type]
        obligation_id=obligation,  # type: ignore[arg-type]
        requiredness=Requiredness.REQUIRED,
        reuse_policy=reuse_policy,
        goal_occurrence_id=goal_occurrence,  # type: ignore[arg-type]
    )


def instance_draft(
    *,
    instance_id: str = "instance-1",
    goal_id: str = "task-1",
    plan_revision: int = 1,
    bindings: tuple[ChildBinding, ...] | None = None,
) -> MethodInstanceDraft:
    return MethodInstanceDraft(
        instance_id=instance_id,  # type: ignore[arg-type]
        goal_id=goal_id,  # type: ignore[arg-type]
        obligation_id="obligation-1",  # type: ignore[arg-type]
        method_ref=method_contract().method_ref(),
        child_bindings=(
            bindings if bindings is not None else (child_binding(instance_id=instance_id),)
        ),
        plan_revision=plan_revision,  # type: ignore[arg-type]
        goal_occurrence_id="occurrence-root",  # type: ignore[arg-type]
    )


def occurrence_spec(
    *, occurrence: str = "occurrence-1", task_id: str = "task-2", obligation: str = "obligation-2"
) -> OccurrenceSpec:
    return OccurrenceSpec(
        occurrence_id=occurrence,  # type: ignore[arg-type]
        task_id=task_id,  # type: ignore[arg-type]
        obligation_id=obligation,  # type: ignore[arg-type]
        form=TaskForm.PRIMITIVE,
        requiredness=Requiredness.REQUIRED,
    )


def data_requirement(*, requirement_id: str = "requirement-1") -> DataRequirement:
    return DataRequirement(
        requirement_id=requirement_id,
        producer_occurrence="occurrence-1",  # type: ignore[arg-type]
        output_port="report",
        consumer_occurrence="occurrence-2",  # type: ignore[arg-type]
        input_port="source",
        schema_ref=vref("report-schema"),
        assurance_policy_ref="assurance-default",
        freshness_policy_ref="freshness-default",
        source_revision_policy=SourceRevisionPolicy.PINNED,
    )


def bound_input(*, requirement_id: str = "requirement-1") -> BoundInput:
    return BoundInput(
        requirement_id=requirement_id,
        producer_result_id="result-1",
        acceptance_id="acceptance-1",
        artifact_id="artifact-1",
        content_hash=HASH_C,
        schema_ref=vref("report-schema"),
        source_revision="1",
    )


def criterion(identifier: str = "c-1") -> Criterion:
    return Criterion(
        criterion_id=identifier,
        revision=1,
        origin=CriterionOrigin.USER_EXPLICIT,
        statement=f"criterion {identifier} is satisfied",
        requirement_class=RequirementClass.REQUIRED_OUTCOME,
        evaluation_kind=EvaluationKind.SEMANTIC,
    )


def requirements_revision(*, revision: int = 1, mission_id: str = MISSION) -> RequirementsRevision:
    return RequirementsRevision(
        revision_id=f"requirements-{revision}",  # type: ignore[arg-type]
        mission_id=mission_id,
        revision=revision,
        criteria=(criterion("c-1"),),
        success_expression=AllExpr((CriterionExpr("c-1"),)),
    )


def review_binding() -> ReviewBinding:
    return ReviewBinding(
        mission_id=MISSION,
        obligation_id="obligation-1",
        subject_ref=tref(TypedRefKind.TASK, "task-1"),
        requirements_revision=1,
        input_manifest_hash=HASH_B,
        policy_ref=tref(TypedRefKind.REQUIREMENTS, "policy-1"),
    )


def review_package(
    *, package_id: str = "package-1", purpose: ReviewPurpose = ReviewPurpose.TASK_CONTENT
) -> ReviewPackage:
    return ReviewPackage(
        package_id=package_id,  # type: ignore[arg-type]
        purpose=purpose,
        binding=review_binding(),
        criteria=(criterion("c-1"),),
        success_expression=AllExpr((CriterionExpr("c-1"),)),
    )


def review_record(
    *,
    record_id: str = "review-1",
    package_id: str = "package-1",
    purpose: ReviewPurpose = ReviewPurpose.TASK_CONTENT,
    verdict: ReviewVerdict = ReviewVerdict.ACCEPT,
) -> ReviewRecord:
    return ReviewRecord(
        record_id=record_id,  # type: ignore[arg-type]
        package_id=package_id,  # type: ignore[arg-type]
        purpose=purpose,
        binding=review_binding(),
        reviewer_agent_id="independent-agent",
        reviewer_turn_id="turn-2",
        evidence_manifest_hash=HASH_A,
        criteria=(
            CriterionOutcome(
                criterion_id="c-1",
                verdict=CriterionVerdict.PASS,
                check_execution=CheckExecution.SUCCEEDED,
            ),
        ),
        verdict=verdict,
    )


def acceptance(
    *,
    acceptance_id: str = "acceptance-1",
    review_record_id: str = "review-1",
    validity_ms: int = 10,
) -> Acceptance:
    return Acceptance(
        acceptance_id=acceptance_id,  # type: ignore[arg-type]
        mission_id=MISSION,
        task_id="task-1",  # type: ignore[arg-type]
        obligation_id="obligation-1",  # type: ignore[arg-type]
        requirements_revision=1,
        contract_revision=1,
        input_manifest_hash=HASH_B,
        review_record_id=review_record_id,  # type: ignore[arg-type]
        accepted_at_ms=validity_ms,
        validity=Validity.CURRENT,
    )


def goal_resolution(
    *, resolution_id: str = "resolution-1", obligation: str = "obligation-1"
) -> GoalResolution:
    return GoalResolution(
        resolution_id=resolution_id,  # type: ignore[arg-type]
        mission_id=MISSION,
        obligation_id=obligation,
        goal_task_id="task-1",
        requirements_version=1,
        contract_revision=1,
        method_instance_id="instance-1",
        input_manifest_hash=HASH_B,
        artifact_refs=(),
        child_resolution_ids=(),
        criteria=(ResolutionCriterion(criterion_id="c-1", verdict=CriterionVerdict.PASS),),
        review_receipt_id="review-1",
        verdict=ReviewVerdict.ACCEPT,
        validity=Validity.CURRENT,
    )


def witness(
    *,
    witness_id: str = "witness-1",
    scope_epoch: int = 1,
    support_revision: int = 4,
    support_refs_acceptance: str | None = None,
    condition_digests: tuple[str, ...] = (),
    truth: TruthValue = TruthValue.TRUE,
) -> ValidityWitness:
    """One licence.  ``support_refs_acceptance`` / ``condition_digests`` pick its lane.

    A witness with neither names no subject, which is what migration 18 back-fills
    onto every row written before it.
    """

    support = [tref(TypedRefKind.OBSERVATION, "observation-1")]
    if support_refs_acceptance is not None:
        support.append(tref(TypedRefKind.ACCEPTANCE, support_refs_acceptance))
    usable = truth is TruthValue.TRUE
    return ValidityWitness(
        witness_id=witness_id,
        consumer_ref=tref(TypedRefKind.TASK, "task-2"),
        purpose=WitnessPurpose.START,
        truth=truth,
        freshness=Validity.CURRENT,
        availability=Availability.READABLE,
        decision=WitnessDecision.USABLE if usable else WitnessDecision.BLOCKED,
        scope_id="mission-1",
        scope_epoch=scope_epoch,
        support_revision=support_revision,
        as_of_ms=1_000,
        not_after_ms=2_000,
        support_refs=tuple(support),
        reason_codes=tuple(f"condition:{digest}" for digest in condition_digests),
    )


def observation(*, observation_id: str = "observation-1") -> ObservationRecord:
    return ObservationRecord(
        observation_id=observation_id,
        proposition_key="source-readable(alpha)",
        polarity=True,
        source_ref=tref(TypedRefKind.SOURCE, "source-alpha"),
        observed_at_ms=900,
        recorded_at_ms=950,
        coverage=QueryCompleteness.BEST_EFFORT,
        observer_id="observer-1",
    )


def envelope(
    *,
    operation_id: str = "operation-1",
    occurrence: str = "send-approved-report-1",
    request_hash: str = HASH_C,
) -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=operation_id,  # type: ignore[arg-type]
        operation_occurrence_id=occurrence,  # type: ignore[arg-type]
        mission_id=MISSION,
        obligation_id="obligation-send",
        scope_id="mission-1",
        connector_id="test-delivery",
        connector_version="1",
        operation_name="enqueue",
        operation_kind="EVENT_WRITE",  # type: ignore[arg-type]
        target_ref="recipient-1",
        expected_target_version=None,
        parameters_artifact_ref=tref(TypedRefKind.ARTIFACT, "parameters-1"),
        request_hash=request_hash,
        requirements_revision=1,
        review_ref=tref(TypedRefKind.REVIEW, "review-1"),
        accepted_input_refs=(tref(TypedRefKind.ACCEPTANCE, "report-1"),),
        effect_contract_ref=tref(TypedRefKind.REQUIREMENTS, "delivery-contract-1"),
    )


@pytest.fixture
def store(tmp_path) -> Store:
    opened = Store.open(tmp_path / "orchestrator.db")
    opened.insert_mission(_mission(), spec_hash="h")
    return opened


@pytest.fixture
def htn(store: Store) -> HtnStore:
    return HtnStore(store)


@pytest.fixture
def planned(htn: HtnStore) -> HtnStore:
    """A store with plan revision 1 already prepared, so FK targets exist."""

    htn.insert_plan_revision(MISSION, 1, snapshot_hash=HASH_A, read_set=read_set())
    return htn


# --------------------------------------------------------------------------------------
# Migration 16 and the legacy guard
# --------------------------------------------------------------------------------------


def test_migration_nineteen_is_the_new_head() -> None:
    assert schema.SCHEMA_VERSION == 19
    assert schema.SCHEMA_NAME == "orchestrator-planning-decision-v1"
    assert schema.MIGRATIONS[-1].ddl is planning_decision_schema.DDL


def test_migration_seventeen_is_still_migration_seventeen() -> None:
    """P2.3c part 2d adds 18; it does not move, rename or re-number 17."""

    seventeen = schema.MIGRATIONS[16]
    assert seventeen.version == 17
    assert seventeen.name == "orchestrator-full-target-acceptance-receipts"
    assert seventeen.ddl is acceptance_receipt_schema.DDL


def test_migration_sixteen_is_still_migration_sixteen() -> None:
    """P2.3c part 2 adds 17; it does not move, rename or re-number 16."""

    sixteen = schema.MIGRATIONS[15]
    assert sixteen.version == 16
    assert sixteen.name == "orchestrator-full-target-htn"
    assert sixteen.ddl is htn_schema.DDL


def test_the_fifteen_older_migrations_keep_their_checksums() -> None:
    assert len(schema.MIGRATIONS) == 19
    for migration, expected in zip(schema.MIGRATIONS[:15], FROZEN_MIGRATIONS, strict=True):
        assert (migration.version, migration.name, migration.checksum) == expected


def test_migration_sixteen_is_pinned_to_its_checksum_and_table_list() -> None:
    """Changing the shipped DDL must be a deliberate act, not a quiet edit.

    Migration 16 has been applied to real libraries, so its bytes are frozen the
    same way the fifteen before it are: a change here makes every deployed library
    report a schema mismatch, and this assertion is where that is noticed.
    """

    assert schema.MIGRATIONS[15].checksum == MIGRATION_16_CHECKSUM
    assert htn_schema.TABLES == MIGRATION_16_TABLES
    assert len(set(htn_schema.TABLES)) == len(htn_schema.TABLES)


def test_migration_seventeen_is_pinned_to_its_checksum_and_table_list() -> None:
    """The same pin, one migration later: the accept-side receipts and output index."""

    assert schema.MIGRATIONS[16].checksum == MIGRATION_17_CHECKSUM
    assert acceptance_receipt_schema.TABLES == MIGRATION_17_TABLES
    assert len(set(FULL_TARGET_TABLES)) == len(FULL_TARGET_TABLES)


def test_migration_eighteen_is_pinned_and_creates_no_table() -> None:
    """Decision 1 widens one key; it does not introduce state of its own."""

    assert schema.MIGRATIONS[17].checksum == MIGRATION_18_CHECKSUM
    ddl = validity_subject_schema.DDL
    assert "CREATE TABLE" not in ddl.upper()
    assert "DROP TABLE" not in ddl.upper()
    # The only index it drops is the one it immediately replaces.
    assert ddl.upper().count("DROP INDEX") == 1
    assert "validity_witnesses_consumer_idx" in ddl


def test_migration_sixteen_checksum_is_unchanged_by_eighteen() -> None:
    """Decision 1's anti-regression anchor (memo test 4).

    The subject column is added by a *new* migration; migration 16's bytes are not
    edited.  A library that already ran 16 and 17 has to be able to apply 18 on top,
    which a changed 16 would make impossible — and that is exactly what this pin
    catches, in the same file that pins 16 itself.
    """

    assert schema.MIGRATIONS[15].checksum == MIGRATION_16_CHECKSUM
    assert schema.MIGRATIONS[15].ddl is htn_schema.DDL
    assert "subject_digest" not in htn_schema.DDL


def test_migration_seventeen_does_not_touch_a_migration_sixteen_table() -> None:
    """Additive means additive: 17 creates three new tables and alters none of 16's."""

    ddl = acceptance_receipt_schema.DDL
    assert "ALTER TABLE" not in ddl.upper()
    assert "DROP " not in ddl.upper()
    for table in MIGRATION_16_TABLES:
        assert f"CREATE TABLE {table}" not in ddl


def test_the_declared_table_list_is_exactly_what_migration_sixteen_adds(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TABLES`` is what the legacy guard iterates, so it may not drift from the DDL."""

    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:15])
    older = Store.open(tmp_path / "v15.db")
    before = _table_names(older)
    older.close()
    monkeypatch.undo()

    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:16])
    newer = Store.open(tmp_path / "v16.db")
    after = _table_names(newer)
    newer.close()
    monkeypatch.undo()
    assert after - before == set(htn_schema.TABLES)


def test_the_declared_table_list_is_exactly_what_migration_seventeen_adds(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:16])
    older = Store.open(tmp_path / "v16.db")
    before = _table_names(older)
    older.close()
    monkeypatch.undo()

    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:17])
    newer = Store.open(tmp_path / "v17.db")
    after = _table_names(newer)
    newer.close()
    monkeypatch.undo()
    assert after - before == set(acceptance_receipt_schema.TABLES)


def test_migration_eighteen_upgrades_an_existing_library_in_place(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memo test 3: a deployed migration-17 library opens, upgrades and keeps its rows.

    The old unique key is strictly narrower than the new one, so no library that was
    consistent under 17 can collide under 18; the back-filled rows read back with the
    empty subject, which is what they always meant.
    """

    path = tmp_path / "deployed.db"
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:17])
    older = Store.open(path)
    older.insert_mission(_mission(), spec_hash="h")
    legacy = witness()
    # Written the way migration 17 wrote it — with no subject column at all, which is
    # the row shape this migration has to be able to upgrade.
    with older.transaction() as connection:
        connection.execute(
            "INSERT INTO validity_witnesses(witness_id,mission_id,consumer_kind,consumer_id,"
            "purpose,scope_id,scope_epoch,support_revision,truth,freshness,availability,"
            "decision,as_of_ms,not_after_ms,witness_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                legacy.witness_id,
                MISSION,
                str(legacy.consumer_ref.kind),
                legacy.consumer_ref.id,
                str(legacy.purpose),
                legacy.scope_id,
                legacy.scope_epoch,
                legacy.support_revision,
                str(legacy.truth),
                str(legacy.freshness),
                str(legacy.availability),
                str(legacy.decision),
                legacy.as_of_ms,
                legacy.not_after_ms,
                canonical_json(legacy.to_json()),
                1.0,
            ),
        )
    older.close()
    monkeypatch.undo()

    upgraded = Store.open(path)
    try:
        applied = [
            tuple(row)
            for row in upgraded.connection.execute(
                "SELECT version,name,checksum FROM orch_schema_migrations ORDER BY version"
            )
        ]
        assert applied[-2] == (
            18,
            "orchestrator-full-target-witness-subject",
            MIGRATION_18_CHECKSUM,
        )
        assert applied[-1] == (19, schema.SCHEMA_NAME, schema.MIGRATIONS[-1].checksum)
        assert (tmp_path / "deployed.db.pre-schema-19.backup").is_file()
        stored = HtnStore(upgraded).list_validity_witnesses(MISSION)
        assert [item.witness_id for item in stored] == ["witness-1"]
        subjects = [
            row[0]
            for row in upgraded.connection.execute("SELECT subject_digest FROM validity_witnesses")
        ]
        assert subjects == [NO_SUBJECT]
    finally:
        upgraded.close()


def test_every_new_table_exists_and_is_strict(store: Store) -> None:
    rows = {
        row[0]: row[1]
        for row in store.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        )
    }
    for table in FULL_TARGET_TABLES:
        assert table in rows, table
        assert "STRICT" in rows[table], table


def test_every_new_table_is_empty_in_a_fresh_library(store: Store) -> None:
    for table in FULL_TARGET_TABLES:
        count = store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        assert count == 0, table


def test_a_legacy_run_writes_nothing_into_the_new_tables(tmp_path) -> None:
    """Replay the step-02 store / budget tests, then look for leaks."""

    spec = importlib.util.spec_from_file_location("step02_store_and_budgets", STEP02_TESTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.test_store_opens_validates_and_reopens(tmp_path)
    module.test_cas_and_idempotent_events(tmp_path)
    module.test_budget_chain_reserve_settle_and_unpriced(tmp_path)

    legacy = Store.open(tmp_path / "o.db")
    try:
        assert legacy.count_events("mission-1") == 1
        for table in FULL_TARGET_TABLES:
            count = legacy.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            assert count == 0, table
    finally:
        legacy.close()


#: One row for every table migrations 7–15 created, so the rehearsal compares real
#: content and not just an empty shell.  Written through a plain connection (foreign
#: keys default off there): the drill is about bytes surviving the upgrade, not about
#: re-deriving a consistent Mission.
LEGACY_TAIL_ROWS: tuple[tuple[str, str, tuple[Any, ...]], ...] = (
    (
        "workspaces",
        "INSERT INTO workspaces VALUES (?,?,?,?,?,?,?,?,?)",
        ("workspace-1", "attempt", MISSION, "attempt-1", "snapshot-1", "ACTIVE", "{}", 1.0, 1.0),
    ),
    (
        "mission_domains",
        "INSERT INTO mission_domains VALUES (?,?,?,?,?)",
        (MISSION, "domain-1", "1", "{}", 1.0),
    ),
    (
        "sources",
        "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
        (MISSION, "t", "docs/a.md", "d" * 64, "file", "untrusted_external", 1.0, None, 0, 1),
    ),
    (
        "criterion_assessments",
        "INSERT INTO criterion_assessments VALUES (?,?,?,?,?,?,?,?)",
        ("receipt-1", MISSION, "task-1", "result-1", "claim-1", "c-1", "{}", 1.0),
    ),
    (
        "provider_token_grants",
        "INSERT INTO provider_token_grants VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "invocation-1",
            1,
            MISSION,
            "attempt-1",
            "agent-1",
            "turn-1",
            "intent-1",
            "owner",
            "sdk-owner",
            1,
            "fingerprint",
            "e" * 64,
            "f" * 64,
            10,
            0,
            5,
            20,
            "SETTLED",
            12,
            4,
            1,
            1.0,
            1.0,
            None,
            None,
            None,
            None,
        ),
    ),
    ("search_bindings", "INSERT INTO search_bindings VALUES (?,?)", (MISSION, "{}")),
    (
        "selection_rounds",
        "INSERT INTO selection_rounds VALUES (?,?,?,?,?,?)",
        ("round-1", MISSION, "task-1", 1, "OPEN", "{}"),
    ),
    (
        "selection_candidates",
        "INSERT INTO selection_candidates VALUES (?,?,?,?)",
        ("result-1", "round-1", "CANDIDATE", "{}"),
    ),
    (
        "budget_tail_holds",
        "INSERT INTO budget_tail_holds VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "hold-1",
            MISSION,
            "budget:mission-1",
            "attempt-1",
            "1",
            "critic",
            "{}",
            1,
            "HELD",
            None,
            1.0,
            1.0,
        ),
    ),
    (
        "budget_tail_transfers",
        "INSERT INTO budget_tail_transfers VALUES (?,?,?,?,?)",
        ("hold-1", "transfer-1", "{}", "{}", 1.0),
    ),
    (
        "fragment_validations",
        "INSERT INTO fragment_validations VALUES (?,?,?,?,?,?)",
        ("fragment-1", MISSION, "result-1", "task-1", "projection-1", 1.0),
    ),
    (
        "mission_system_tail_pools",
        "INSERT INTO mission_system_tail_pools VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pool-1",
            MISSION,
            "attempt-1",
            "conflict",
            "1",
            "{}",
            "{}",
            1,
            "HELD",
            None,
            1.0,
            1.0,
        ),
    ),
    (
        "mission_system_tail_tasks",
        "INSERT INTO mission_system_tail_tasks VALUES (?,?,?,?,?,?)",
        ("task-1", "pool-1", "hold-1", "{}", "{}", 1.0),
    ),
)
LEGACY_TAIL_TABLES: tuple[str, ...] = tuple(entry[0] for entry in LEGACY_TAIL_ROWS)


def _open_at_version_fifteen(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:15])
    older = Store.open(path)
    older.insert_mission(_mission(), spec_hash="h")
    older.append_event(_legacy_event())
    older.close()
    monkeypatch.undo()
    connection = sqlite3.connect(path)
    try:
        for _table, statement, values in LEGACY_TAIL_ROWS:
            connection.execute(statement, values)
        connection.commit()
    finally:
        connection.close()


def _table_names(store: Store) -> set[str]:
    return {
        row[0]
        for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _raw_sql_uses(text: str, table: str) -> bool:
    """True when ``text`` contains SQL that names ``table`` as a relation.

    The keyword match is case-sensitive: SQL in this repository is written in upper
    case, so prose like "re-select anchors from observations" is not a leak while
    ``FROM observations`` is.
    """

    return any(re.search(rf"\b{keyword}\s+{re.escape(table)}\b", text) for keyword in SQL_ACCESS)


def test_no_module_outside_storage_writes_the_new_tables_in_sql() -> None:
    """§18.5: the new tables have one writing authority, and it is ``storage``.

    A module that reaches past ``HtnStore`` / ``ObligationStore`` with its own SQL
    becomes a second writer of the same rows — the exact shape §18.5 forbids when it
    says a client, planner or graph service must not update the edges on its own.
    """

    leaks: set[tuple[str, str]] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative.startswith("storage/"):
            continue
        text = path.read_text()
        for table in FULL_TARGET_TABLES:
            if _raw_sql_uses(text, table):
                leaks.add((relative, table))
    assert leaks <= KNOWN_RAW_SQL_DEBT, sorted(leaks - KNOWN_RAW_SQL_DEBT)


def test_the_graph_and_knowledge_layers_never_name_a_new_table_in_sql() -> None:
    """Those two layers are pure: no whitelist, now or later."""

    for prefix in ("graph/", "knowledge/", "planning/", "verification/", "scheduling/"):
        for path in sorted((SOURCE_ROOT / prefix.rstrip("/")).rglob("*.py")):
            text = path.read_text()
            for table in FULL_TARGET_TABLES:
                assert not _raw_sql_uses(text, table), f"{path}: {table}"


def _legacy_event():
    from agent_orchestrator.contracts import Event

    return Event(
        id="event-1",
        type="MissionCreated",
        trace_id="trace",
        mission_id=MISSION,
        task_id=None,
        attempt_id=None,
        actor_type="system",
        actor_id="test",
        payload={"k": 1},
        idempotency_key="MissionCreated:mission-1",
        created_at=1.0,
    )


def _dump(path: Path, table: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]  # noqa: S608
    finally:
        connection.close()


def test_upgrading_a_copy_of_a_v15_library_keeps_every_old_row(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§20 rule 6: the migration is rehearsed on a copy before it touches anything."""

    original = tmp_path / "v15.db"
    _open_at_version_fifteen(original, monkeypatch)
    sampled = ("missions", "events", *LEGACY_TAIL_TABLES)
    before = {table: _dump(original, table) for table in sampled}
    assert all(before[table] for table in sampled), before
    assert [row[0] for row in _dump(original, "orch_schema_migrations")] == list(range(1, 16))

    rehearsal = tmp_path / "copy" / "v15.db"
    rehearsal.parent.mkdir()
    shutil.copy(original, rehearsal)

    upgraded = Store.open(rehearsal)
    try:
        after = {table: _dump(rehearsal, table) for table in sampled}
        assert after == before
        applied = _dump(rehearsal, "orch_schema_migrations")
        assert [row[0] for row in applied] == list(range(1, 20))
        assert applied[-1][1] == "orchestrator-planning-decision-v1"
        assert applied[-1][2] == schema.MIGRATIONS[-1].checksum
        assert applied[-2][1] == "orchestrator-full-target-witness-subject"
        assert applied[-2][2] == MIGRATION_18_CHECKSUM
        assert applied[-3][1] == "orchestrator-full-target-acceptance-receipts"
        assert applied[-3][2] == MIGRATION_17_CHECKSUM
        assert applied[-4][1] == "orchestrator-full-target-htn"
        assert applied[-4][2] == MIGRATION_16_CHECKSUM
        assert applied[:15] == _dump(original, "orch_schema_migrations")[:15]
        for table in FULL_TARGET_TABLES:
            assert (
                upgraded.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
            ), table
    finally:
        upgraded.close()
    assert _dump(original, "missions") == before["missions"]


def test_the_upgrade_writes_a_backup_of_the_old_library(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v15.db"
    _open_at_version_fifteen(path, monkeypatch)
    upgraded = Store.open(path)
    upgraded.close()
    backup = tmp_path / "v15.db.pre-schema-19.backup"
    assert backup.is_file()
    assert [row[0] for row in _dump(backup, "orch_schema_migrations")] == list(range(1, 16))
    assert _dump(backup, "missions")


# --------------------------------------------------------------------------------------
# task_semantics
# --------------------------------------------------------------------------------------


def test_a_task_semantic_binding_round_trips(htn: HtnStore) -> None:
    binding = task_binding()
    digest = htn.put_task_semantics(MISSION, binding)
    assert digest == binding.content_hash()
    assert htn.get_task_semantics("task-1", 1) == binding
    assert htn.latest_task_semantics("task-1") == binding


def test_the_same_task_and_revision_may_only_be_bound_once(htn: HtnStore) -> None:
    htn.put_task_semantics(MISSION, task_binding())
    with pytest.raises(StoreConflict, match="already stored"):
        htn.put_task_semantics(MISSION, task_binding())


def test_task_semantics_are_listed_by_form(htn: HtnStore) -> None:
    compound = task_binding(task_id="task-1", form=TaskForm.COMPOUND)
    primitive = task_binding(task_id="task-2", obligation="obligation-2", form=TaskForm.PRIMITIVE)
    htn.put_task_semantics(MISSION, compound)
    htn.put_task_semantics(MISSION, primitive)
    assert htn.list_task_semantics(MISSION) == (compound, primitive)
    assert htn.list_task_semantics(MISSION, form="primitive") == (primitive,)


def test_an_unknown_task_binding_is_a_conflict(htn: HtnStore) -> None:
    assert htn.latest_task_semantics("task-absent") is None
    with pytest.raises(StoreConflict, match="no semantic binding"):
        htn.get_task_semantics("task-absent", 1)


# --------------------------------------------------------------------------------------
# method registry
# --------------------------------------------------------------------------------------


def test_a_method_definition_round_trips_with_its_registration(htn: HtnStore) -> None:
    contract = method_contract()
    registration = admit_method(
        contract.method_ref(), MethodRegistryStatus.ADMITTED, author=RegistryAuthor.SYSTEM
    )
    htn.register_method(contract, registration)
    stored = htn.get_method("compare-two-sources", 1)
    assert stored.contract == contract
    assert stored.registration == registration


def test_registering_the_same_bytes_twice_returns_what_is_stored(htn: HtnStore) -> None:
    contract = method_contract()
    registration = admit_method(
        contract.method_ref(), MethodRegistryStatus.DRAFT, author=RegistryAuthor.MODEL
    )
    first = htn.register_method(contract, registration)
    second = htn.register_method(contract, registration)
    assert first == second
    assert len(htn.list_methods()) == 1


def test_a_changed_definition_may_not_reuse_its_version(htn: HtnStore) -> None:
    contract = method_contract()
    htn.register_method(
        contract,
        admit_method(
            contract.method_ref(), MethodRegistryStatus.ADMITTED, author=RegistryAuthor.SYSTEM
        ),
    )
    changed = method_contract(step_capabilities=("sources.read", "sources.write"))
    with pytest.raises(StoreConflict, match="different content"):
        htn.register_method(
            changed,
            admit_method(
                changed.method_ref(), MethodRegistryStatus.ADMITTED, author=RegistryAuthor.SYSTEM
            ),
        )


def test_a_registration_must_describe_the_definition_it_is_stored_with(htn: HtnStore) -> None:
    contract = method_contract()
    other = method_contract(method_id="other-method")
    with pytest.raises(StoreConflict, match="does not describe"):
        htn.register_method(
            contract,
            admit_method(
                other.method_ref(), MethodRegistryStatus.ADMITTED, author=RegistryAuthor.SYSTEM
            ),
        )


def test_the_registry_may_promote_a_stored_method(htn: HtnStore) -> None:
    contract = method_contract()
    htn.register_method(
        contract,
        admit_method(
            contract.method_ref(), MethodRegistryStatus.DRAFT, author=RegistryAuthor.MODEL
        ),
    )
    promoted = admit_method(
        contract.method_ref(),
        MethodRegistryStatus.ADMITTED,
        author=RegistryAuthor.SYSTEM,
        admission_receipt_ref=tref(TypedRefKind.REVIEW, "admission-1"),
    )
    htn.set_method_registration(promoted)
    stored = htn.get_method("compare-two-sources", 1)
    assert stored.registration.status is MethodRegistryStatus.ADMITTED
    assert stored.registration.admission_receipt_ref == tref(TypedRefKind.REVIEW, "admission-1")
    assert htn.list_methods(status=MethodRegistryStatus.ADMITTED) == (stored,)
    assert htn.list_methods(status=MethodRegistryStatus.DRAFT) == ()


def test_a_hand_edited_registration_row_is_refused_by_the_contract_codec(
    htn: HtnStore, store: Store
) -> None:
    """The registration comes back through its codec, so the §7.3 rules run again.

    Editing the row to make a model-authored method ADMITTED must not survive the
    read: the codec refuses the combination the registry service never granted.
    """

    contract = method_contract()
    htn.register_method(
        contract,
        admit_method(
            contract.method_ref(), MethodRegistryStatus.DRAFT, author=RegistryAuthor.MODEL
        ),
    )
    forged = json.loads(
        store.connection.execute(
            "SELECT registration_json FROM method_contracts WHERE method_id = ?",
            ("compare-two-sources",),
        ).fetchone()[0]
    )
    forged["status"] = "ADMITTED"
    with store.transaction() as connection:
        connection.execute(
            "UPDATE method_contracts SET registration_json = ?, registry_status = 'DRAFT'"
            " WHERE method_id = ?",
            (json.dumps(forged), "compare-two-sources"),
        )
    with pytest.raises(ContractError, match="may only be submitted as DRAFT"):
        htn.get_method("compare-two-sources", 1)


def test_promoting_a_method_that_was_never_stored_is_a_conflict(htn: HtnStore) -> None:
    contract = method_contract()
    with pytest.raises(StoreConflict, match="not stored"):
        htn.set_method_registration(
            admit_method(
                contract.method_ref(), MethodRegistryStatus.ADMITTED, author=RegistryAuthor.SYSTEM
            )
        )


# --------------------------------------------------------------------------------------
# method instances and child occurrences
# --------------------------------------------------------------------------------------


def test_a_method_instance_round_trips_with_its_child_bindings(htn: HtnStore) -> None:
    draft = instance_draft()
    htn.insert_method_instance(MISSION, draft)
    assert htn.get_method_instance(MISSION, "instance-1") == draft
    assert htn.list_child_occurrences(MISSION, "instance-1") == draft.child_bindings
    assert htn.method_instance_state(MISSION, "instance-1") == "DRAFT"


def test_the_same_instance_id_may_not_be_stored_twice(htn: HtnStore) -> None:
    htn.insert_method_instance(MISSION, instance_draft())
    with pytest.raises(StoreConflict, match="already stored"):
        htn.insert_method_instance(MISSION, instance_draft())


def test_a_slot_of_one_instance_may_only_be_bound_once(htn: HtnStore, store: Store) -> None:
    """The contract keeps slot keys unique inside a draft; the index keeps them
    unique in the library, so a second writer cannot rebind a slot behind its back."""

    htn.insert_method_instance(MISSION, instance_draft())
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO method_child_occurrences(instance_id,slot_key,mission_id,"
                "occurrence_id,obligation_id,goal_occurrence_id,requiredness,reuse_policy,"
                "binding_json,created_at) VALUES ('instance-1','extract',?,'occurrence-x',"
                "'obligation-3','occurrence-x','required','new_work','{}',1.0)",
                (MISSION,),
            )
    assert htn.list_child_occurrences(MISSION, "instance-1") == instance_draft().child_bindings


def test_method_instance_states_move_between_the_declared_values(htn: HtnStore) -> None:
    htn.insert_method_instance(MISSION, instance_draft())
    assert htn.set_method_instance_state(MISSION, "instance-1", "ADOPTED") == "ADOPTED"
    assert htn.list_method_instances(MISSION, state="ADOPTED") == (instance_draft(),)
    assert htn.list_method_instances(MISSION, state="RETIRED") == ()
    with pytest.raises(StoreConflict, match="must be one of"):
        htn.set_method_instance_state(MISSION, "instance-1", "ACTIVE")


def test_a_shared_goal_occurrence_lists_every_slot_that_adopted_it(htn: HtnStore) -> None:
    first = instance_draft(
        instance_id="instance-1",
        bindings=(
            child_binding(
                instance_id="instance-1",
                slot_key="extract",
                occurrence="occurrence-a",
                reuse_policy=ReusePolicy.SHARE_ACTIVE,
                goal_occurrence="occurrence-shared",
            ),
        ),
    )
    second = instance_draft(
        instance_id="instance-2",
        goal_id="task-2",
        bindings=(
            child_binding(
                instance_id="instance-2",
                slot_key="extract",
                occurrence="occurrence-b",
                reuse_policy=ReusePolicy.SHARE_ACTIVE,
                goal_occurrence="occurrence-shared",
            ),
        ),
    )
    htn.insert_method_instance(MISSION, first)
    htn.insert_method_instance(MISSION, second)
    consumers = htn.occurrence_consumers(MISSION, "occurrence-shared")
    assert len(consumers) == 2
    assert {str(binding.instance_id) for binding in consumers} == {"instance-1", "instance-2"}


def test_method_instances_are_listed_by_plan_revision(htn: HtnStore) -> None:
    htn.insert_method_instance(MISSION, instance_draft(instance_id="instance-1", plan_revision=1))
    htn.insert_method_instance(
        MISSION,
        instance_draft(
            instance_id="instance-2",
            plan_revision=2,
            bindings=(child_binding(instance_id="instance-2", occurrence="occurrence-2"),),
        ),
    )
    assert len(htn.list_method_instances(MISSION, plan_revision=1)) == 1
    assert len(htn.list_method_instances(MISSION)) == 2


# --------------------------------------------------------------------------------------
# plan revisions
# --------------------------------------------------------------------------------------


def test_a_plan_revision_round_trips_with_its_read_set(htn: HtnStore) -> None:
    stored = htn.insert_plan_revision(MISSION, 1, snapshot_hash=HASH_A, read_set=read_set())
    assert htn.get_plan_revision(MISSION, 1) == stored
    assert stored.read_set == read_set()
    assert stored.state == "PREPARED"


def test_a_plan_revision_number_is_unique_per_mission(planned: HtnStore) -> None:
    with pytest.raises(StoreConflict, match="already stored"):
        planned.insert_plan_revision(MISSION, 1, snapshot_hash=HASH_B, read_set=read_set())


def test_a_mission_has_at_most_one_active_plan_revision(planned: HtnStore) -> None:
    planned.insert_plan_revision(
        MISSION, 2, snapshot_hash=HASH_B, read_set=read_set(), base_revision=1
    )
    assert planned.active_plan_revision(MISSION) is None
    planned.activate_plan_revision(MISSION, 1)
    assert planned.active_plan_revision(MISSION).revision == 1
    planned.activate_plan_revision(MISSION, 2)
    active = planned.active_plan_revision(MISSION)
    assert active.revision == 2
    assert planned.get_plan_revision(MISSION, 1).state == "RETIRED"
    states = [item.state for item in planned.list_plan_revisions(MISSION)]
    assert states.count("ACTIVE") == 1


def test_a_second_revision_may_not_be_inserted_straight_into_active(planned: HtnStore) -> None:
    """The index, not the activation path, is what makes "one plan" true.

    ``activate_plan_revision`` retires the old revision first, so it never tests the
    partial unique index.  Inserting a second ACTIVE revision directly does, and this
    is the case a writer that skipped the activation helper would hit.
    """

    planned.activate_plan_revision(MISSION, 1)
    with pytest.raises(StoreConflict, match="already stored"):
        planned.insert_plan_revision(
            MISSION,
            2,
            snapshot_hash=HASH_B,
            read_set=read_set(),
            state="ACTIVE",
            base_revision=1,
        )
    assert [item.state for item in planned.list_plan_revisions(MISSION)] == ["ACTIVE"]
    assert planned.active_plan_revision(MISSION).revision == 1


def test_a_first_revision_may_be_inserted_active(htn: HtnStore) -> None:
    htn.insert_plan_revision(MISSION, 1, snapshot_hash=HASH_A, read_set=read_set(), state="ACTIVE")
    assert htn.active_plan_revision(MISSION).revision == 1


def test_an_unknown_plan_revision_state_is_refused(htn: HtnStore) -> None:
    with pytest.raises(StoreConflict, match="must be one of"):
        htn.insert_plan_revision(
            MISSION, 1, snapshot_hash=HASH_A, read_set=read_set(), state="LIVE"
        )


def test_a_retired_plan_revision_cannot_be_reactivated(planned: HtnStore) -> None:
    planned.insert_plan_revision(
        MISSION, 2, snapshot_hash=HASH_B, read_set=read_set(), base_revision=1
    )
    planned.activate_plan_revision(MISSION, 1)
    planned.activate_plan_revision(MISSION, 2)
    with pytest.raises(StoreConflict, match="retired"):
        planned.activate_plan_revision(MISSION, 1)


def test_activating_an_unknown_revision_is_a_conflict(planned: HtnStore) -> None:
    with pytest.raises(StoreConflict, match="no plan revision"):
        planned.activate_plan_revision(MISSION, 7)


def test_plan_memberships_round_trip_and_are_unique_per_occurrence(planned: HtnStore) -> None:
    spec = occurrence_spec()
    planned.insert_plan_membership(MISSION, 1, spec, instance_id="instance-1")
    assert planned.list_plan_memberships(MISSION, 1) == (spec,)
    with pytest.raises(StoreConflict, match="already a member"):
        planned.insert_plan_membership(MISSION, 1, spec)


def test_a_membership_needs_the_plan_revision_it_claims(htn: HtnStore) -> None:
    with pytest.raises(StoreConflict):
        htn.insert_plan_membership(MISSION, 9, occurrence_spec())


# --------------------------------------------------------------------------------------
# order, data and input manifests
# --------------------------------------------------------------------------------------


def test_order_constraints_are_indexed_before_and_after(planned: HtnStore) -> None:
    first = OrderConstraint(
        before="occurrence-1",  # type: ignore[arg-type]
        after="occurrence-2",  # type: ignore[arg-type]
        release_condition=ReleaseCondition.ACCEPTED,
    )
    second = OrderConstraint(
        before="occurrence-2",  # type: ignore[arg-type]
        after="occurrence-3",  # type: ignore[arg-type]
        release_condition=ReleaseCondition.SETTLED_TERMINAL,
    )
    planned.insert_order_constraint(MISSION, 1, first)
    planned.insert_order_constraint(MISSION, 1, second)
    assert planned.list_order_constraints(MISSION, 1) == (first, second)
    assert planned.list_order_constraints(MISSION, 1, before="occurrence-2") == (second,)
    assert planned.list_order_constraints(MISSION, 1, after="occurrence-2") == (first,)


def test_the_same_order_edge_is_stored_once(planned: HtnStore) -> None:
    constraint = OrderConstraint(
        before="occurrence-1",  # type: ignore[arg-type]
        after="occurrence-2",  # type: ignore[arg-type]
    )
    planned.insert_order_constraint(MISSION, 1, constraint)
    with pytest.raises(StoreConflict, match="already stored"):
        planned.insert_order_constraint(MISSION, 1, constraint)


def test_an_order_constraint_needs_its_plan_revision(htn: HtnStore) -> None:
    with pytest.raises(StoreConflict):
        htn.insert_order_constraint(
            MISSION,
            3,
            OrderConstraint(
                before="occurrence-1",  # type: ignore[arg-type]
                after="occurrence-2",  # type: ignore[arg-type]
            ),
        )


def test_data_requirements_round_trip_and_filter_by_port(planned: HtnStore) -> None:
    requirement = data_requirement()
    planned.insert_data_requirement(MISSION, 1, requirement)
    assert planned.list_data_requirements(MISSION, 1) == (requirement,)
    assert planned.list_data_requirements(MISSION, 1, consumer_occurrence="occurrence-2") == (
        requirement,
    )
    assert planned.list_data_requirements(MISSION, 1, producer_occurrence="occurrence-9") == ()
    with pytest.raises(StoreConflict, match="already stored"):
        planned.insert_data_requirement(MISSION, 1, requirement)


def test_a_bound_input_needs_the_requirement_it_resolves(planned: HtnStore) -> None:
    with pytest.raises(StoreConflict):
        planned.insert_bound_input(MISSION, 1, bound_input())
    planned.insert_data_requirement(MISSION, 1, data_requirement())
    stored = planned.insert_bound_input(MISSION, 1, bound_input())
    assert planned.list_bound_inputs(MISSION, 1) == (stored,)


def test_a_requirement_may_be_rebound_under_a_new_binding_revision(planned: HtnStore) -> None:
    planned.insert_data_requirement(MISSION, 1, data_requirement())
    planned.insert_bound_input(MISSION, 1, bound_input(), input_binding_revision=0)
    planned.insert_bound_input(MISSION, 1, bound_input(), input_binding_revision=1)
    assert len(planned.list_bound_inputs(MISSION, 1, requirement_id="requirement-1")) == 2
    with pytest.raises(StoreConflict, match="already stored"):
        planned.insert_bound_input(MISSION, 1, bound_input(), input_binding_revision=1)


def test_an_input_manifest_is_addressed_by_its_own_hash_and_is_idempotent(htn: HtnStore) -> None:
    document = {"inputs": [{"port": "source", "artifact_id": "artifact-1"}]}
    digest = htn.insert_input_manifest(MISSION, "task-1", document, request_id="request-1")
    assert htn.insert_input_manifest(MISSION, "task-1", document) == digest
    assert htn.get_input_manifest(digest) == document
    assert len(htn.list_manifest_bindings(digest)) == 1


def test_two_tasks_with_identical_inputs_share_one_immutable_manifest(
    htn: HtnStore, store: Store
) -> None:
    """Reuse is an explicit read of one frozen artefact, not a refusal."""

    document = {"inputs": []}
    digest = htn.insert_input_manifest(MISSION, "task-1", document)
    assert htn.insert_input_manifest(MISSION, "task-2", document) == digest
    store.insert_mission(_mission("mission-2"), spec_hash="h")
    assert htn.insert_input_manifest("mission-2", "task-9", document) == digest

    assert store.connection.execute("SELECT count(*) FROM input_manifests").fetchone()[0] == 1
    bindings = htn.list_manifest_bindings(digest)
    assert {(entry["mission_id"], entry["task_id"]) for entry in bindings} == {
        (MISSION, "task-1"),
        (MISSION, "task-2"),
        ("mission-2", "task-9"),
    }
    assert htn.get_input_manifest(digest) == document


def test_one_request_id_belongs_to_one_manifest(htn: HtnStore) -> None:
    htn.insert_input_manifest(MISSION, "task-1", {"inputs": []}, request_id="request-1")
    with pytest.raises(StoreConflict, match="could not be bound"):
        htn.insert_input_manifest(
            MISSION, "task-1", {"inputs": ["changed"]}, request_id="request-1"
        )


def test_an_unknown_input_manifest_is_a_conflict(htn: HtnStore) -> None:
    with pytest.raises(StoreConflict, match="no input manifest"):
        htn.get_input_manifest("f" * 64)


# --------------------------------------------------------------------------------------
# requirements, review, acceptance, resolution
# --------------------------------------------------------------------------------------


def test_a_requirements_revision_round_trips(htn: HtnStore) -> None:
    revision = requirements_revision()
    htn.insert_requirements_revision(revision)
    assert htn.get_requirements_revision(MISSION, 1).to_json() == revision.to_json()
    htn.insert_requirements_revision(requirements_revision(revision=2))
    latest = htn.latest_requirements_revision(MISSION)
    assert latest is not None and latest.revision == 2


def test_a_requirements_revision_number_is_unique_per_mission(htn: HtnStore) -> None:
    htn.insert_requirements_revision(requirements_revision())
    with pytest.raises(StoreConflict, match="already stored"):
        htn.insert_requirements_revision(requirements_revision())


def test_a_review_package_round_trips(htn: HtnStore) -> None:
    package = review_package()
    htn.insert_review_package(package)
    assert htn.get_review_package("package-1") == package
    assert htn.list_review_packages(MISSION) == (package,)
    assert htn.list_review_packages(MISSION, purpose=ReviewPurpose.MISSION_FINAL) == ()


def test_a_review_record_writes_its_criterion_evaluations(htn: HtnStore) -> None:
    htn.insert_review_package(review_package())
    record = review_record()
    htn.insert_review_record(record, official=True)
    stored = htn.get_review_record("review-1")
    assert stored.record == record
    assert stored.official is True
    evaluations = htn.list_criterion_evaluations("review-1")
    assert len(evaluations) == 1
    assert evaluations[0]["criterion_id"] == "c-1"
    assert evaluations[0]["verdict"] == "PASS"
    assert len(evaluations[0]["check_receipt_hash"]) == 64


def test_only_one_review_record_per_package_is_official(htn: HtnStore) -> None:
    htn.insert_review_package(review_package())
    htn.insert_review_record(review_record(), official=True)
    htn.insert_review_record(
        review_record(record_id="review-2", verdict=ReviewVerdict.REWORK), official=False
    )
    with pytest.raises(StoreConflict, match="conflicts with what is stored"):
        htn.insert_review_record(review_record(record_id="review-3"), official=True)
    assert htn.official_review_record("package-1").record_id == "review-1"
    assert len(htn.list_review_records("package-1")) == 2


def test_a_review_record_may_not_change_its_package_purpose(htn: HtnStore) -> None:
    htn.insert_review_package(review_package())
    with pytest.raises(StoreConflict, match="purpose"):
        htn.insert_review_record(review_record(purpose=ReviewPurpose.METHOD_PLAN))


def test_a_review_record_needs_its_package(htn: HtnStore) -> None:
    with pytest.raises(StoreConflict, match="no review package"):
        htn.insert_review_record(review_record())


def test_an_acceptance_is_idempotent_and_refuses_a_changed_body(htn: HtnStore) -> None:
    htn.insert_review_package(review_package())
    htn.insert_review_record(review_record(), official=True)
    digest = htn.insert_acceptance(acceptance())
    assert htn.insert_acceptance(acceptance()) == digest
    with pytest.raises(StoreConflict, match="different content"):
        htn.insert_acceptance(acceptance(validity_ms=99))
    assert htn.get_acceptance("acceptance-1").accepted_at_ms == 10


def test_one_review_record_backs_at_most_one_acceptance(htn: HtnStore) -> None:
    htn.insert_review_package(review_package())
    htn.insert_review_record(review_record(), official=True)
    htn.insert_acceptance(acceptance())
    with pytest.raises(StoreConflict):
        htn.insert_acceptance(acceptance(acceptance_id="acceptance-2"))
    assert htn.list_acceptances(MISSION, obligation_id="obligation-1") == (acceptance(),)


def test_an_acceptance_needs_the_review_record_it_quotes(htn: HtnStore) -> None:
    with pytest.raises(StoreConflict):
        htn.insert_acceptance(acceptance())


def test_at_most_one_goal_resolution_is_adopted_per_obligation(htn: HtnStore) -> None:
    htn.insert_goal_resolution(goal_resolution())
    htn.insert_goal_resolution(goal_resolution(resolution_id="resolution-2"))
    assert htn.adopted_goal_resolution(MISSION, "obligation-1") is None
    htn.adopt_goal_resolution(MISSION, "resolution-1")
    assert htn.adopted_goal_resolution(MISSION, "obligation-1").resolution_id == "resolution-1"
    htn.adopt_goal_resolution(MISSION, "resolution-2")
    adopted = htn.adopted_goal_resolution(MISSION, "obligation-1")
    assert adopted.resolution_id == "resolution-2"
    assert len(htn.list_goal_resolutions(MISSION, obligation_id="obligation-1")) == 2


def test_a_goal_resolution_round_trips_and_is_unique(htn: HtnStore) -> None:
    resolution = goal_resolution()
    htn.insert_goal_resolution(resolution)
    assert htn.get_goal_resolution("resolution-1").to_json() == resolution.to_json()
    with pytest.raises(StoreConflict):
        htn.insert_goal_resolution(resolution)


# --------------------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------------------


def test_a_validity_witness_round_trips(htn: HtnStore) -> None:
    stored = witness()
    htn.insert_validity_witness(MISSION, stored, subject=NO_SUBJECT)
    assert htn.get_validity_witness("witness-1") == stored
    assert htn.list_validity_witnesses(MISSION, scope_id="mission-1") == (stored,)


def test_one_witness_per_consumer_purpose_scope_and_support_revision(htn: HtnStore) -> None:
    htn.insert_validity_witness(MISSION, witness(), subject=NO_SUBJECT)
    with pytest.raises(StoreConflict, match="conflicts with one already stored"):
        htn.insert_validity_witness(MISSION, witness(witness_id="witness-2"), subject=NO_SUBJECT)
    htn.insert_validity_witness(
        MISSION, witness(witness_id="witness-3", scope_epoch=2), subject=NO_SUBJECT
    )
    assert len(htn.list_validity_witnesses(MISSION)) == 2


def test_two_licences_over_two_subjects_live_side_by_side(htn: HtnStore) -> None:
    """P2.3c part 2d, decision 1 (memo test 1), at the storage level.

    One consumer, one purpose, one scope epoch, one support revision — and two
    licences, because they were taken over two different supports.  AER §8.1 puts
    ``support_selection`` inside a witness's identity, so these are two rows.
    """

    data = witness(witness_id="witness-data", support_refs_acceptance="acceptance-7")
    conditions = witness(witness_id="witness-pre", condition_digests=("d1", "d2"))
    assert witness_subject(data) == acceptance_subject("acceptance-7")
    assert witness_subject(conditions) == condition_subject(("d2", "d1"))
    htn.insert_validity_witness(MISSION, data, subject=witness_subject(data))
    htn.insert_validity_witness(MISSION, conditions, subject=witness_subject(conditions))
    assert len(htn.list_validity_witnesses(MISSION)) == 2
    assert htn.list_validity_witnesses(MISSION, subject=acceptance_subject("acceptance-7")) == (
        data,
    )


def test_two_conclusions_about_the_same_subject_still_conflict(htn: HtnStore) -> None:
    """Decision 1 widened the key by one dimension; it did not open it."""

    first = witness(witness_id="witness-data", support_refs_acceptance="acceptance-7")
    second = witness(
        witness_id="witness-data-2",
        support_refs_acceptance="acceptance-7",
        truth=TruthValue.UNKNOWN,
    )
    htn.insert_validity_witness(MISSION, first, subject=witness_subject(first))
    with pytest.raises(StoreConflict, match="conflicts with one already stored"):
        htn.insert_validity_witness(MISSION, second, subject=witness_subject(second))


def test_a_witness_cannot_be_stored_under_a_subject_it_does_not_name(htn: HtnStore) -> None:
    """Memo test 5, and decision 1's first mutation self-check.

    The issuer declares the subject and the store re-derives it from the witness.  A
    declaration the witness does not support is refused, so a caller cannot file two
    unrelated licences under one key by simply naming the same subject twice — nor a
    single licence under a subject that hides it from the consumer looking for it.

    **Mutation**: comment out the ``witness_subject(witness) == subject`` check in
    ``HtnStore.insert_validity_witness`` and this test goes red.
    """

    stored = witness(witness_id="witness-data", support_refs_acceptance="acceptance-7")
    with pytest.raises(StoreConflict, match="but names"):
        htn.insert_validity_witness(MISSION, stored, subject=acceptance_subject("acceptance-9"))
    with pytest.raises(StoreConflict, match="but names"):
        htn.insert_validity_witness(MISSION, stored, subject=NO_SUBJECT)
    assert htn.list_validity_witnesses(MISSION) == ()


def test_the_index_key_still_separates_two_different_acceptances(htn: HtnStore) -> None:
    """Memo test 6, decision 1's core mutation self-check.

    **Mutation**: put the old seven-column key back (or write a constant into
    ``subject_digest``) and the second insert below starts raising ``StoreConflict``
    — which is precisely the defect part 2c's smoke hit, where a leaf's second
    licence could not be stored and the occurrence waited for it for ever.
    """

    for index, acceptance in enumerate(("acceptance-7", "acceptance-8")):
        stored = witness(witness_id=f"witness-{index}", support_refs_acceptance=acceptance)
        htn.insert_validity_witness(MISSION, stored, subject=witness_subject(stored))
    assert len(htn.list_validity_witnesses(MISSION)) == 2
    assert {
        row[0]
        for row in htn._store.connection.execute("SELECT subject_digest FROM validity_witnesses")
    } == {acceptance_subject("acceptance-7"), acceptance_subject("acceptance-8")}


def test_an_observation_round_trips(htn: HtnStore) -> None:
    record = observation()
    htn.insert_observation(MISSION, record, scope_id="mission-1")
    assert htn.get_observation("observation-1") == record
    assert htn.list_observations(MISSION, proposition_key="source-readable(alpha)") == (record,)
    assert htn.list_observations(MISSION, proposition_key="other") == ()
    with pytest.raises(StoreConflict, match="already stored"):
        htn.insert_observation(MISSION, record)


def test_a_justification_set_indexes_its_members_in_reverse(htn: HtnStore) -> None:
    members = (
        (tref(TypedRefKind.OBSERVATION, "observation-1"), True),
        (tref(TypedRefKind.ACCEPTANCE, "acceptance-1"), False),
    )
    stored = htn.insert_justification_set(
        MISSION,
        "support-1",
        subject_kind="resolution",
        subject_id="resolution-1",
        members=members,
        member_revision=2,
        rule_ref="rule-1",
    )
    assert htn.get_justification_set("support-1") == stored
    assert htn.list_justification_sets(MISSION, "resolution", "resolution-1") == (stored,)
    assert htn.consumers_of(MISSION, "observation", "observation-1") == (stored,)
    assert htn.consumers_of(MISSION, "observation", "observation-9") == ()
    assert len(stored.member_digest) == 64


def test_a_justification_set_needs_members_and_rejects_a_repeated_one(htn: HtnStore) -> None:
    reference = tref(TypedRefKind.OBSERVATION, "observation-1")
    with pytest.raises(StoreConflict, match="at least one member"):
        htn.insert_justification_set(
            MISSION, "support-1", subject_kind="claim", subject_id="claim-1", members=()
        )
    with pytest.raises(StoreConflict, match="appears twice"):
        htn.insert_justification_set(
            MISSION,
            "support-1",
            subject_kind="claim",
            subject_id="claim-1",
            members=((reference, True), (reference, False)),
        )
    assert htn.list_justification_sets(MISSION, "claim", "claim-1") == ()


def test_epochs_advance_and_dirty_subjects_queue_up(htn: HtnStore) -> None:
    assert htn.epoch(MISSION, "mission-1") == 0
    assert htn.bump_epoch(MISSION, "mission-1", bumped_by="evidence-commit") == 0
    assert htn.bump_epoch(MISSION, "mission-1", bumped_by="evidence-commit") == 1
    assert htn.epoch(MISSION, "mission-1") == 1
    entry = htn.mark_dirty(
        MISSION,
        subject_kind="resolution",
        subject_id="resolution-1",
        scope_id="mission-1",
        epoch=1,
        reason="support retracted",
    )
    assert htn.list_dirty(MISSION) == (entry,)
    htn.mark_dirty(
        MISSION,
        subject_kind="resolution",
        subject_id="resolution-1",
        scope_id="mission-1",
        epoch=1,
        reason="support retracted",
        state="CLEARED",
    )
    assert htn.list_dirty(MISSION) == ()
    assert len(htn.list_dirty(MISSION, state="CLEARED")) == 1


# --------------------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------------------


def test_an_operation_binding_round_trips(htn: HtnStore) -> None:
    stored = envelope()
    htn.bind_operation(stored, principal_id="principal-1")
    assert htn.get_operation_binding("principal-1", "mission-1", "send-approved-report-1") == stored
    assert htn.list_operation_bindings(MISSION, operation_id="operation-1") == (stored,)


def test_the_same_operation_id_may_not_carry_a_second_request_hash(htn: HtnStore) -> None:
    htn.bind_operation(envelope(), principal_id="principal-1")
    with pytest.raises(StoreConflict, match="OPERATION_PAYLOAD_CONFLICT"):
        htn.bind_operation(
            envelope(occurrence="send-approved-report-2", request_hash=HASH_B),
            principal_id="principal-1",
        )
    assert len(htn.list_operation_bindings(MISSION)) == 1


def test_the_same_operation_id_with_the_same_request_is_a_retry(htn: HtnStore) -> None:
    htn.bind_operation(envelope(), principal_id="principal-1")
    htn.bind_operation(envelope(occurrence="send-approved-report-2"), principal_id="principal-1")
    assert len(htn.list_operation_bindings(MISSION, operation_id="operation-1")) == 2


def test_one_occurrence_is_bound_once(htn: HtnStore) -> None:
    htn.bind_operation(envelope(), principal_id="principal-1")
    with pytest.raises(StoreConflict, match="already bound"):
        htn.bind_operation(envelope(), principal_id="principal-2")


# --------------------------------------------------------------------------------------
# read sets and commit receipts
# --------------------------------------------------------------------------------------


def test_a_read_set_round_trips_and_indexes_every_subject(htn: HtnStore) -> None:
    digest = htn.record_read_set(MISSION, "proposal-1", read_set())
    assert len(digest) == 64
    assert htn.get_read_set(MISSION, "proposal-1") == read_set()
    items = htn.list_read_set_items(MISSION, "proposal-1")
    kinds = {item["subject_type"] for item in items}
    assert {"task", "method", "support_set", "scope_epoch", "absence"} <= kinds
    assert {"requirements", "manager_epoch", "budget_grant_revision"} <= kinds
    assert htn.read_set_consumers(MISSION, "task", "task-1") == ("proposal-1",)


def test_a_proposal_records_its_read_set_once(htn: HtnStore) -> None:
    htn.record_read_set(MISSION, "proposal-1", read_set())
    with pytest.raises(StoreConflict, match="appears twice"):
        htn.record_read_set(MISSION, "proposal-1", read_set())


def test_two_proposals_that_read_the_same_subject_are_both_indexed(htn: HtnStore) -> None:
    htn.record_read_set(MISSION, "proposal-1", read_set())
    htn.record_read_set(MISSION, "proposal-2", read_set(requirements_revision=2))
    assert htn.read_set_consumers(MISSION, "task", "task-1") == ("proposal-1", "proposal-2")
    assert htn.get_read_set(MISSION, "proposal-2").requirements_revision == 2


def test_a_commit_receipt_replays_instead_of_committing_twice(htn: HtnStore) -> None:
    receipt = htn.record_commit_receipt(
        MISSION,
        command_id="command-1",
        delta_id="delta-1",
        base_plan_revision=0,
        new_plan_revision=1,
        intent_hash=HASH_A,
        read_set=read_set(),
        output_identity={"occurrences": ["occurrence-1"]},
        detail={"note": "first commit"},
    )
    replay = htn.record_commit_receipt(
        MISSION,
        command_id="command-1",
        delta_id="delta-1",
        base_plan_revision=0,
        new_plan_revision=1,
        intent_hash=HASH_A,
        read_set=read_set(),
        output_identity={"occurrences": ["occurrence-1"]},
        detail={"note": "first commit"},
    )
    assert replay == receipt
    assert htn.get_commit_receipt("command-1") == receipt
    assert htn.list_commit_receipts(MISSION) == (receipt,)
    assert receipt.read_set_hash == json.loads(json.dumps(replay.read_set_hash))


def test_a_command_id_may_not_change_its_intent(htn: HtnStore) -> None:
    htn.record_commit_receipt(
        MISSION,
        command_id="command-1",
        delta_id="delta-1",
        base_plan_revision=0,
        new_plan_revision=1,
        intent_hash=HASH_A,
        read_set=read_set(),
        output_identity={},
    )
    with pytest.raises(StoreConflict, match="already applied with intent"):
        htn.record_commit_receipt(
            MISSION,
            command_id="command-1",
            delta_id="delta-1",
            base_plan_revision=0,
            new_plan_revision=1,
            intent_hash=HASH_B,
            read_set=read_set(),
            output_identity={},
        )
    with pytest.raises(StoreConflict, match="different read-set"):
        htn.record_commit_receipt(
            MISSION,
            command_id="command-1",
            delta_id="delta-1",
            base_plan_revision=0,
            new_plan_revision=1,
            intent_hash=HASH_A,
            read_set=read_set(requirements_revision=2),
            output_identity={},
        )


def test_two_commands_may_not_produce_the_same_plan_revision(htn: HtnStore) -> None:
    htn.record_commit_receipt(
        MISSION,
        command_id="command-1",
        delta_id="delta-1",
        base_plan_revision=0,
        new_plan_revision=1,
        intent_hash=HASH_A,
        read_set=read_set(),
        output_identity={},
    )
    with pytest.raises(StoreConflict, match="conflicts with a stored one"):
        htn.record_commit_receipt(
            MISSION,
            command_id="command-2",
            delta_id="delta-2",
            base_plan_revision=0,
            new_plan_revision=1,
            intent_hash=HASH_B,
            read_set=read_set(),
            output_identity={},
        )


def test_an_unknown_commit_receipt_is_a_conflict(htn: HtnStore) -> None:
    with pytest.raises(StoreConflict, match="no commit receipt"):
        htn.get_commit_receipt("command-absent")


# --------------------------------------------------------------------------------------
# transaction consistency
# --------------------------------------------------------------------------------------


def test_a_failing_transaction_rolls_back_every_table_it_touched(
    store: Store, planned: HtnStore
) -> None:
    contract = method_contract()
    with pytest.raises(RuntimeError, match="deliberate"):
        with store.transaction():
            planned.put_task_semantics(MISSION, task_binding())
            planned.register_method(
                contract,
                admit_method(
                    contract.method_ref(),
                    MethodRegistryStatus.ADMITTED,
                    author=RegistryAuthor.SYSTEM,
                ),
            )
            planned.insert_method_instance(MISSION, instance_draft())
            planned.insert_plan_membership(MISSION, 1, occurrence_spec())
            planned.insert_data_requirement(MISSION, 1, data_requirement())
            planned.insert_review_package(review_package())
            planned.record_read_set(MISSION, "proposal-1", read_set())
            raise RuntimeError("deliberate")

    for table in (
        "task_semantics",
        "method_contracts",
        "method_instances",
        "method_child_occurrences",
        "plan_memberships",
        "data_requirements",
        "review_packages",
        "plan_read_sets",
    ):
        count = store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
        assert count == 0, table
    assert store.connection.execute("SELECT count(*) FROM plan_revisions").fetchone()[0] == 1


def test_a_multi_table_transaction_that_succeeds_lands_as_one(
    store: Store, planned: HtnStore
) -> None:
    with store.transaction():
        planned.put_task_semantics(MISSION, task_binding())
        planned.insert_method_instance(MISSION, instance_draft())
        planned.insert_plan_membership(MISSION, 1, occurrence_spec())
        planned.activate_plan_revision(MISSION, 1)
    assert planned.get_task_semantics("task-1", 1) == task_binding()
    assert planned.get_method_instance(MISSION, "instance-1") == instance_draft()
    assert planned.active_plan_revision(MISSION).revision == 1


def test_the_store_does_not_judge_a_plan_only_stores_it(planned: HtnStore) -> None:
    """P2.3a decides admission; the store keeps an uncovered occurrence happily."""

    planned.insert_plan_membership(
        MISSION, 1, occurrence_spec(occurrence="occurrence-orphan", task_id="task-orphan")
    )
    assert planned.list_plan_memberships(MISSION, 1)[0].occurrence_id == "occurrence-orphan"
    assert planned.list_data_requirements(MISSION, 1) == ()


def test_the_goal_signature_of_a_binding_is_queryable(htn: HtnStore, store: Store) -> None:
    htn.put_task_semantics(MISSION, task_binding())
    row = store.connection.execute(
        "SELECT goal_signature_id, form, mission_id FROM task_semantics WHERE task_id = ?",
        ("task-1",),
    ).fetchone()
    assert row[0] == goal_signature().signature_id
    assert row[1] == "compound"
    assert row[2] == MISSION
