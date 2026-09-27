# SPDX-License-Identifier: Apache-2.0
"""One original ReviewPackage, up to two original budgeted service invocations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..assurance.codec import AssuranceError, decode, fingerprint, integer, text
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.reviews import AssuranceReviewBinding, ReviewInvocation
from ..contracts import TERMINAL_MISSION
from ..contracts.resolution import (
    AllExpr,
    AnyExpr,
    CriterionExpr,
    RequirementClass,
    ReviewAccount,
    ReviewPackage,
)
from ..contracts.semantic_base import TypedRefKind
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_store import AssuranceStore
from ..storage.assurance_work import atomic
from ..storage.htn_store import HtnStore
from ..governance.budgets import BudgetError
from ..storage.store import StoreError

if TYPE_CHECKING:
    from .commit_service import CommitService, Reservation

ROLES = {
    "TASK_CONTENT": "critic",
    "METHOD_PLAN": "method_reviewer",
    "COMPOSITION": "composition_reviewer",
    "ACTION_PROPOSAL": "operation_proposal_reviewer",
    "OPERATION_OUTCOME": "operation_outcome_reviewer",
    "MISSION_FINAL": "root_reviewer",
}


def assurance_formula(expression: Any) -> dict[str, Any]:
    """Translate the original frozen expression, preserving branch order."""
    if isinstance(expression, CriterionExpr):
        return {"criterion": str(expression.criterion_id)}
    if isinstance(expression, (AllExpr, AnyExpr)):
        return {
            "all" if isinstance(expression, AllExpr) else "any": [
                assurance_formula(child) for child in expression.children
            ]
        }
    raise AssuranceError("FORMULA_INVALID")


def _validate_package(reader: AssuranceReader, package: ReviewPackage, body: dict) -> None:
    subject = body["subject"]
    if (
        body["mission_id"] != package.binding.mission_id
        or body["package_ref"]
        != Pin(str(package.package_id), 0, fingerprint(package.to_json())).to_json()
        or subject["purpose"] != str(package.purpose)
        or subject["input_manifest_hash"] != package.binding.input_manifest_hash
        or body["requirements_ref"]["revision"] != package.binding.requirements_revision
        or body["requirements_ref"]["content_hash"] != package.requirements_content_hash
        or body["criterion_ids"] != sorted(package.criterion_catalogue())
        or body["producer_agent_ids"] != sorted(package.producer_agent_ids)
        or body["formula"] != assurance_formula(package.success_expression)
        or body["mandatory_ids"]
        != sorted(
            str(c.criterion_id)
            for c in package.criteria
            if c.requirement_class is RequirementClass.HARD_CONSTRAINT
        )
    ):
        raise AssuranceError("REVIEW_PACKAGE_BINDING_MISMATCH")
    reader.read_exact_metadata(
        AssuranceRef("requirements", Pin.from_json(body["requirements_ref"]))
    )
    owner = reader.read_exact_metadata(
        AssuranceRef("task", Pin.from_json(subject["owner_task_ref"]))
    )
    if decode(owner.body_json)["task_id"] != subject["owner_task_ref"]["id"]:
        raise AssuranceError("REVIEW_OWNER_MISMATCH")
    reader.read_exact_metadata(AssuranceRef.from_json(subject["target"]))
    policy = decode(
        reader.read_exact_metadata(
            AssuranceRef.from_json(body["criterion_policy_ref"], kinds={"check_policy"})
        ).body_json
    )
    from .assurance_purpose_reviews import policy_domain_hash

    scope_pin = subject["completion_scope_ref"]
    effect_key = None
    if subject["purpose"] == "OPERATION_OUTCOME":
        outcome = reader.store.connection.execute(
            "SELECT effect_key FROM operation_outcome_review_bindings "
            "WHERE mission_id=? AND review_package_id=?",
            (body["mission_id"], body["package_ref"]["id"]),
        ).fetchone()
        if outcome is None:
            raise AssuranceError("SOURCE_UNAVAILABLE", "operation outcome binding")
        effect_key = str(outcome["effect_key"])
    domain = policy_domain_hash(
        subject["purpose"],
        scope_hash=None if scope_pin is None else scope_pin["content_hash"],
        task_hash=subject["owner_task_ref"]["content_hash"],
        effect_key=effect_key,
    )
    if (
        policy["requirements_ref"] != body["requirements_ref"]
        or policy["criteria"] != body["check_requirements"]
        or policy["scope_hash"] != domain
    ):
        raise AssuranceError("CHECK_POLICY_SCOPE_MISMATCH")
    if subject["method_instance_ref"] is not None:
        reader.read_exact_metadata(
            AssuranceRef("method_instance", Pin.from_json(subject["method_instance_ref"]))
        )
    if scope_pin is None:
        # Only METHOD_PLAN may be bound before a Scope exists (subject shape rule);
        # its approved domain is the exact planning subject read above.
        return
    scope_body = decode(
        reader.read_exact_metadata(
            AssuranceRef("completion_scope", Pin.from_json(scope_pin))
        ).body_json
    )
    from ..contracts.operation_completion import OccurrenceCompletionScopeV1
    from .operation_completion import OperationCompletionReader

    scope = OccurrenceCompletionScopeV1.from_json(scope_body)
    if (
        scope.task_ref.id != subject["owner_task_ref"]["id"]
        or scope.occurrence_id != subject["occurrence_id"]
        or scope.requirements_ref.to_json() != body["requirements_ref"]
        or OperationCompletionReader(reader.store).read_scope(
            reader.mission_id, scope.plan_ref, scope.occurrence_id
        )
        != scope
    ):
        raise AssuranceError("REVIEW_OWNER_MISMATCH")


def _receipt_ref(
    commit: CommitService, receipt_id: str, kind: str, subject: str, body: dict
) -> AssuranceRef:
    commit.store.insert_receipt(
        commit_id=receipt_id,
        kind=kind,
        subject_id=subject,
        base_version=0,
        proposal_hash=fingerprint(body),
        receipt=body,
    )
    return AssuranceRef("commit_receipt", Pin(receipt_id, 0, fingerprint(body)))


def operation_task_id(store: Any, mission_id: str, package: Any) -> str:
    """The producer Task of the operation a proposal/outcome package reviews."""
    row = store.connection.execute(
        "SELECT producer_task_id FROM operation_intent_bindings "
        "WHERE mission_id=? AND review_package_id=?",
        (mission_id, str(package.package_id)),
    ).fetchone()
    if row is None and package.binding.subject_ref.kind is TypedRefKind.OPERATION:
        row = store.connection.execute(
            "SELECT i.producer_task_id FROM operation_intent_bindings i "
            "JOIN commit_receipts r ON r.commit_id='materialize:'||i.intent_id "
            "WHERE i.mission_id=? AND json_extract(r.receipt_json,'$.operation_id')=?",
            (mission_id, str(package.binding.subject_ref.id)),
        ).fetchone()
    if row is None:
        raise AssuranceError("SOURCE_UNAVAILABLE", "operation task")
    return str(row[0])


def ensure_review_invocation(
    commit: CommitService,
    *,
    tenant_id: str,
    binding: AssuranceReviewBinding,
    package: ReviewPackage,
    request_command_id: str,
    config: Mapping[str, Any],
    reservation: Reservation,
    require_current_locked: Callable[[], None],
    prior_failure: AssuranceRef | None = None,
    repair_reason: str = "FORMAT_REPAIR",
) -> ReviewInvocation:
    """Internal original-builder entry, not a Host command accepting a verdict.

    Preparation authenticates current sources/authority and pins before calling;
    its final validator runs here in the same UoW as package/reserve/intent/receipt.
    The original service writer joins that UoW; the savepoint rolls everything
    back even when an outer caller catches an error.
    """
    from .commit_service import mission_account, task_account

    if not isinstance(binding, AssuranceReviewBinding) or not callable(require_current_locked):
        raise AssuranceError("REVIEW_PREPARATION_REQUIRED")
    body = binding.to_json()
    mission_id, review_key = body["mission_id"], body["review_key"]
    ordinal = 1 if prior_failure is None else 2
    subject_id = f"{mission_id}:assurance:{review_key}:{ordinal}"
    input_id = f"assurance-input:{review_key}:{ordinal}"
    text(request_command_id)
    text(tenant_id)
    integer(reservation.tokens, minimum=1)
    integer(reservation.cost_micros)
    # create_service_intent's existing service reserve covers tokens/cost, not
    # a separately named tool allowance. Do not claim it reserved an ignored cap.
    if reservation.tool_calls != 0:
        raise AssuranceError("REVIEW_SERVICE_RESERVATION_INVALID")
    owner_task_id = body["subject"]["owner_task_ref"]["id"]
    purpose = body["subject"]["purpose"]
    if package.account in {ReviewAccount.MISSION, ReviewAccount.MISSION_PLANNING}:
        account_id = mission_account(mission_id)
    elif package.account is ReviewAccount.OPERATION_TASK:
        # The operation's own Task — the leaf that prepared it — as on the legacy
        # reviewer path.  The subject's owner Task is the effect owner's Scope Task,
        # usually the root compound, whose budget is 0 by design (real run
        # 2026-09-27: BudgetExhausted on the root account, every round).
        account_id = task_account(operation_task_id(commit.store, mission_id, package))
    else:
        account_id = task_account(owner_task_id)
    invocation_config = dict(config)
    if "message" not in invocation_config or "agent_config" not in invocation_config:
        raise AssuranceError("REVIEW_RUNTIME_CONFIG_REQUIRED")
    from ..assurance.review_input import read_initial_materials

    read_initial_materials(dict(invocation_config["message"]), binding)
    invocation_config.update(
        {
            "assurance_protocol": "assurance-exec-v1.1",
            "review_key": review_key,
            "review_package_id": str(package.package_id),
            "invocation_ordinal": ordinal,
            "role": ROLES[purpose],
            "budget_account": str(package.account),
            "task_id": owner_task_id,
            "review_binding_hash": binding.content_hash,
        }
    )
    input_hash = fingerprint(invocation_config["message"])
    request = {
        "mission_id": mission_id,
        "tenant_id": tenant_id,
        "request_command_id": request_command_id,
        "binding_hash": binding.content_hash,
        "ordinal": ordinal,
        "config_hash": fingerprint(invocation_config),
        "reservation": {"tokens": reservation.tokens, "cost_micros": reservation.cost_micros},
        "account_id": account_id,
        "prior_failure_ref": None if prior_failure is None else prior_failure.to_json(),
    }
    ensure_id = "assurance-review-invocation:" + fingerprint(
        {"review_key": review_key, "ordinal": ordinal}
    )
    with atomic(commit.store) as connection:
        gate = commit._assurance_root_gate
        if gate is None:
            raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
        gate.require_execution()
        reader = AssuranceReader(commit.store, tenant_id=tenant_id, mission_id=mission_id)
        reader._mission_locked(connection)
        if AssuranceStore(commit.store).lane(mission_id) != "ASSURANCE_1_1":
            raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
        require_current_locked()
        old = connection.execute(
            "SELECT * FROM assurance_review_invocations WHERE review_key=? AND ordinal=?",
            (review_key, ordinal),
        ).fetchone()
        if old is not None:
            value = ReviewInvocation(old["invocation_json"])
            receipt = commit.store.get_receipt(ensure_id)
            if (
                old["invocation_hash"] != value.content_hash
                or receipt is None
                or receipt.get("request") != request
            ):
                raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
            return read_review_invocation_locked(
                commit, reader, value.to_json()["dispatch_intent_id"]
            )[0]
        mission = commit.store.get_mission(mission_id)
        if mission.status in TERMINAL_MISSION:
            raise AssuranceError("REVIEW_MISSION_TERMINAL")
        _validate_package(reader, package, body)
        if prior_failure is not None:
            _require_format_repair(commit, reader, review_key, prior_failure)
        htn = HtnStore(commit.store)
        try:
            stored = htn.get_review_package(str(package.package_id))
        except StoreError:
            htn.insert_review_package(package)
        else:
            if stored.to_json() != package.to_json():
                raise AssuranceError("REVIEW_PACKAGE_BINDING_MISMATCH")
        side = AssuranceStore(commit.store)
        binding_receipt_id = "assurance-review-prepared:" + review_key
        binding_receipt = {
            "mission_id": mission_id,
            "review_key": review_key,
            "request_command_id": request_command_id,
            "binding_hash": binding.content_hash,
            "package_ref": body["package_ref"],
        }
        previous = commit.store.get_receipt(binding_receipt_id)
        previous_binding = connection.execute(
            "SELECT * FROM assurance_review_bindings WHERE review_key=?", (review_key,)
        ).fetchone()
        if (previous is None) != (previous_binding is None):
            raise AssuranceError("REVIEW_BINDING_SOURCE_MISMATCH")
        if previous is None:
            _receipt_ref(
                commit, binding_receipt_id, "AssuranceReviewPrepared", review_key, binding_receipt
            )
        elif dict(previous) != binding_receipt:
            raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
        side._insert(
            connection,
            "assurance_review_bindings",
            {
                "review_key": review_key,
                "mission_id": mission_id,
                "package_id": str(package.package_id),
                "owner_task_id": owner_task_id,
                "request_command_id": request_command_id,
                "round_no": body["round_no"],
                "requirements_revision": body["requirements_ref"]["revision"],
                "requirements_hash": body["requirements_ref"]["content_hash"],
                "subject_hash": body["subject"]["target"]["pin"]["content_hash"],
                "input_manifest_hash": body["subject"]["input_manifest_hash"],
                "binding_hash": binding.content_hash,
                "binding_json": binding.body_json,
                "source_receipt_id": binding_receipt_id,
                "created_at_ms": (
                    previous_binding["created_at_ms"]
                    if previous_binding is not None
                    else int(commit.store.now * 1000)
                ),
            },
            identities=(
                ("review_key",),
                ("package_id",),
                ("mission_id", "request_command_id", "round_no"),
            ),
        )
        from .assurance_review_pins import bind_review_blob_pins

        bind_review_blob_pins(commit, binding)
        if commit.store.get_intent_for_subject(subject_id) is not None:
            raise AssuranceError("REVIEW_INTENT_UNBOUND")
        attempt_id = None
        if purpose == "TASK_CONTENT":
            result = commit.store.get_result(body["subject"]["target"]["pin"]["id"])
            if (
                result is None
                or result.envelope.task_id != owner_task_id
                or invocation_config.get("attempt_id") != result.envelope.attempt_id
            ):
                raise AssuranceError("REVIEW_RESULT_SOURCE_MISMATCH")
            attempt_id = result.envelope.attempt_id
        intent = commit.create_service_intent(
            kind="critic" if purpose == "TASK_CONTENT" else "plan",
            subject_id=subject_id,
            mission_id=mission_id,
            account_id=account_id,
            creation_key=subject_id,
            input_id=input_id,
            input_hash=input_hash,
            config=invocation_config,
            reservation=reservation,
            task_id=owner_task_id,
            attempt_id=attempt_id,
        )
        reserve = commit.ledger.reservation(subject_id)
        if (
            reserve is None
            or reserve["state"] != "RESERVED"
            or reserve["mission_id"] != mission_id
            or reserve["account_id"] != account_id
            or reserve["reserved_tokens"] != reservation.tokens
            or reserve["reserved_cost_micros"] != reservation.cost_micros
        ):
            raise AssuranceError("REVIEW_RESERVATION_MISMATCH")
        event = commit._emit(
            "AssuranceReservationLinked",
            mission_id,
            key=subject_id,
            task_id=owner_task_id,
            payload={
                "review_key": review_key,
                "ordinal": ordinal,
                "intent_id": intent.intent_id,
                "subject_id": subject_id,
                "reservation": reserve,
                "input_hash": input_hash,
                "catalogue_hash": body["catalogue_hash"],
            },
        )
        reservation_ref = AssuranceRef(
            "reservation_fact", Pin(event.id, 0, fingerprint(event.to_json()))
        )
        receipt = {
            "mission_id": mission_id,
            "request": request,
            "intent_id": intent.intent_id,
            "reservation_fact_ref": reservation_ref.to_json(),
        }
        source = _receipt_ref(
            commit, ensure_id, "AssuranceReviewInvocationEnsured", intent.intent_id, receipt
        )
        invocation = ReviewInvocation.from_json(
            {
                "schema_version": 1,
                "mission_id": mission_id,
                "review_key": review_key,
                "ordinal": ordinal,
                "reason": "INITIAL" if ordinal == 1 else repair_reason,
                "prior_failure_receipt_ref": None
                if prior_failure is None
                else prior_failure.to_json(),
                "dispatch_intent_id": intent.intent_id,
                "subject_id": subject_id,
                "creation_key": subject_id,
                "input_id": input_id,
                "input_hash": input_hash,
                "catalogue_hash": body["catalogue_hash"],
                "reservation_fact_ref": reservation_ref.to_json(),
                "source_receipt_ref": source.to_json(),
            }
        )
        side._insert(
            connection,
            "assurance_review_invocations",
            {
                "review_key": review_key,
                "ordinal": ordinal,
                "mission_id": mission_id,
                "dispatch_intent_id": intent.intent_id,
                "invocation_hash": invocation.content_hash,
                "invocation_json": invocation.body_json,
                "reservation_event_id": event.id,
                "source_receipt_id": ensure_id,
            },
            identities=(("review_key", "ordinal"), ("dispatch_intent_id",)),
        )
        return invocation


def read_review_invocation_locked(
    commit: CommitService,
    reader: AssuranceReader,
    intent_id: str,
) -> tuple[ReviewInvocation, AssuranceReviewBinding]:
    """Authenticate historical source identities without requiring an active hold.

    Dispatch separately requires a live original reservation; collection must be
    able to import an already settled call and its actual raw/usage facts.
    """
    connection = commit.store.connection
    if not connection.in_transaction:
        raise AssuranceError("READ_TRANSACTION_REQUIRED")
    reader._mission_locked(connection)
    row = connection.execute(
        "SELECT * FROM assurance_review_invocations WHERE dispatch_intent_id=? AND mission_id=?",
        (intent_id, reader.mission_id),
    ).fetchone()
    if row is None:
        raise AssuranceError("REVIEW_INTENT_UNBOUND")
    invocation = ReviewInvocation(row["invocation_json"])
    body = invocation.to_json()
    binding_row = connection.execute(
        "SELECT * FROM assurance_review_bindings WHERE review_key=? AND mission_id=?",
        (body["review_key"], reader.mission_id),
    ).fetchone()
    if binding_row is None:
        raise AssuranceError("REVIEW_BINDING_SOURCE_MISMATCH")
    binding = AssuranceReviewBinding(binding_row["binding_json"])
    prepared = binding.to_json()
    prepared_receipt = connection.execute(
        "SELECT * FROM commit_receipts WHERE commit_id=?", (binding_row["source_receipt_id"],)
    ).fetchone()
    expected_preparation = {
        "mission_id": reader.mission_id,
        "review_key": prepared["review_key"],
        "request_command_id": binding_row["request_command_id"],
        "binding_hash": binding.content_hash,
        "package_ref": prepared["package_ref"],
    }
    if (
        prepared_receipt is None
        or prepared_receipt["kind"] != "AssuranceReviewPrepared"
        or prepared_receipt["subject_id"] != prepared["review_key"]
        or prepared_receipt["base_version"] != 0
        or prepared_receipt["proposal_hash"] != fingerprint(expected_preparation)
        or decode(prepared_receipt["receipt_json"]) != expected_preparation
        or binding_row["package_id"] != prepared["package_ref"]["id"]
        or binding_row["owner_task_id"] != prepared["subject"]["owner_task_ref"]["id"]
        or binding_row["round_no"] != prepared["round_no"]
        or binding_row["requirements_revision"] != prepared["requirements_ref"]["revision"]
        or binding_row["requirements_hash"] != prepared["requirements_ref"]["content_hash"]
        or binding_row["subject_hash"] != prepared["subject"]["target"]["pin"]["content_hash"]
        or binding_row["input_manifest_hash"] != prepared["subject"]["input_manifest_hash"]
    ):
        raise AssuranceError("REVIEW_BINDING_SOURCE_MISMATCH")
    intent = commit.store.get_intent(intent_id)
    source = reader.read_exact_metadata(AssuranceRef.from_json(body["source_receipt_ref"]))
    source_row, receipt = decode(source.lifecycle_json), decode(source.body_json)
    reservation_ref = AssuranceRef.from_json(body["reservation_fact_ref"])
    fact = decode(reader.read_exact_metadata(reservation_ref).body_json)["payload"]
    if (
        intent is None
        or intent.kind != ("critic" if prepared["subject"]["purpose"] == "TASK_CONTENT" else "plan")
        or intent.mission_id != reader.mission_id
        or body["mission_id"] != reader.mission_id
        or row["review_key"] != body["review_key"]
        or row["ordinal"] != body["ordinal"]
        or body["dispatch_intent_id"] != intent_id
        or row["invocation_hash"] != invocation.content_hash
        or row["source_receipt_id"] != body["source_receipt_ref"]["pin"]["id"]
        or row["reservation_event_id"] != reservation_ref.pin.id
        or binding_row["binding_hash"] != binding.content_hash
        or prepared["mission_id"] != reader.mission_id
        or intent.subject_id != body["subject_id"]
        or intent.creation_key != body["creation_key"]
        or intent.input_id != body["input_id"]
        or intent.input_hash != body["input_hash"]
        or fingerprint(intent.config.get("message")) != body["input_hash"]
        or intent.config.get("assurance_protocol") != "assurance-exec-v1.1"
        or intent.config.get("review_binding_hash") != binding.content_hash
        or intent.config.get("review_key") != prepared["review_key"]
        or intent.config.get("review_package_id") != prepared["package_ref"]["id"]
        or intent.config.get("invocation_ordinal") != body["ordinal"]
        or intent.config.get("role") != ROLES[prepared["subject"]["purpose"]]
        or intent.config.get("task_id") != prepared["subject"]["owner_task_ref"]["id"]
        or body["catalogue_hash"] != prepared["catalogue_hash"]
        or source_row["kind"] != "AssuranceReviewInvocationEnsured"
        or source_row["subject_id"] != intent_id
        or source_row["base_version"] != 0
        or source_row["proposal_hash"] != fingerprint(receipt)
        or receipt.get("intent_id") != intent_id
        or receipt.get("reservation_fact_ref") != reservation_ref.to_json()
        or receipt.get("request", {}).get("config_hash") != fingerprint(dict(intent.config))
        or receipt.get("request", {}).get("binding_hash") != binding.content_hash
        or fact.get("intent_id") != intent_id
        or fact.get("subject_id") != intent.subject_id
        or fact.get("review_key") != body["review_key"]
        or fact.get("ordinal") != body["ordinal"]
        or fact.get("input_hash") != intent.input_hash
        or fact.get("catalogue_hash") != body["catalogue_hash"]
    ):
        raise AssuranceError("REVIEW_INVOCATION_SOURCE_MISMATCH")
    reserve = commit.ledger.reservation(intent.subject_id)
    historical = fact.get("reservation", {})
    if reserve is None or any(
        reserve.get(key) != historical.get(key)
        for key in (
            "reservation_id",
            "subject_id",
            "mission_id",
            "account_id",
            "created_at",
        )
    ):
        raise AssuranceError("REVIEW_RESERVATION_MISMATCH")
    if historical.get("state") != "RESERVED":
        raise AssuranceError("REVIEW_RESERVATION_MISMATCH")
    expected_request = {
        "mission_id": reader.mission_id,
        "tenant_id": reader.tenant_id,
        "request_command_id": binding_row["request_command_id"],
        "binding_hash": binding.content_hash,
        "ordinal": body["ordinal"],
        "config_hash": fingerprint(dict(intent.config)),
        "reservation": {
            "tokens": historical.get("reserved_tokens"),
            "cost_micros": historical.get("reserved_cost_micros"),
        },
        "account_id": historical.get("account_id"),
        "prior_failure_ref": body["prior_failure_receipt_ref"],
    }
    if receipt != {
        "mission_id": reader.mission_id,
        "request": expected_request,
        "intent_id": intent_id,
        "reservation_fact_ref": reservation_ref.to_json(),
    }:
        raise AssuranceError("REVIEW_INVOCATION_SOURCE_MISMATCH")
    reader.read_exact_metadata(
        AssuranceRef("review_package", Pin.from_json(prepared["package_ref"]))
    )
    return invocation, binding


def is_bound_task_review_subject(
    commit: Any, *, mission_id: str, task_id: str, attempt_id: str, subject_id: str
) -> bool:
    """Selection's existing account gate accepts only a prepared original review.

    This is called inside create_service_intent, before its invocation row exists.
    Therefore it authenticates the already committed preparation in that same
    outer UoW, rather than guessing a Critic identity from a string prefix alone.
    """
    prefix = mission_id + ":assurance:"
    if (
        not subject_id.startswith(prefix)
        or AssuranceStore(commit.store).lane(mission_id) != "ASSURANCE_1_1"
    ):
        return False
    key, separator, ordinal = subject_id[len(prefix) :].rpartition(":")
    if not separator or ordinal not in {"1", "2"}:
        return False
    row = commit.store.connection.execute(
        "SELECT * FROM assurance_review_bindings WHERE mission_id=? AND review_key=?",
        (mission_id, key),
    ).fetchone()
    if row is None:
        return False
    binding = AssuranceReviewBinding(row["binding_json"])
    body = binding.to_json()
    subject = body["subject"]
    if (
        row["binding_hash"] != binding.content_hash
        or body["review_key"] != key
        or subject["purpose"] != "TASK_CONTENT"
        or subject["target"]["kind"] != "result"
        or subject["owner_task_ref"]["id"] != task_id
    ):
        return False
    original = commit.store.get_result(subject["target"]["pin"]["id"])
    if (
        original is None
        or original.envelope.mission_id != mission_id
        or original.envelope.task_id != task_id
        or original.envelope.attempt_id != attempt_id
        or fingerprint(original.envelope.to_json()) != subject["target"]["pin"]["content_hash"]
    ):
        return False
    receipt = commit.store.connection.execute(
        "SELECT * FROM commit_receipts WHERE commit_id=?", (row["source_receipt_id"],)
    ).fetchone()
    expected = {
        "mission_id": mission_id,
        "review_key": key,
        "request_command_id": row["request_command_id"],
        "binding_hash": binding.content_hash,
        "package_ref": body["package_ref"],
    }
    return (
        receipt is not None
        and receipt["kind"] == "AssuranceReviewPrepared"
        and receipt["subject_id"] == key
        and receipt["base_version"] == 0
        and receipt["proposal_hash"] == fingerprint(expected)
        and decode(receipt["receipt_json"]) == expected
    )


def require_review_handoff(commit: CommitService, intent: Any) -> None:
    """Current-use gate at the original dispatch boundaries; never imports facts."""
    if intent.config.get("assurance_protocol") != "assurance-exec-v1.1":
        return
    with commit.store.read_view():
        mission = commit.store.get_mission(intent.mission_id)
        if mission is None or mission.status in TERMINAL_MISSION:
            raise AssuranceError("REVIEW_MISSION_TERMINAL")
        reader = AssuranceReader(commit.store, tenant_id=mission.tenant_id, mission_id=mission.id)
        _, binding = read_review_invocation_locked(commit, reader, intent.intent_id)
        gate = commit._assurance_root_gate
        if gate is None:
            raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
        gate.require_execution()
        reserve = commit.ledger.reservation(intent.subject_id)
        if reserve is None or reserve["state"] != "RESERVED":
            raise AssuranceError("REVIEW_RESERVATION_NOT_ACTIVE")
        validator = commit._assurance_review_handoff
        if validator is None:
            raise AssuranceError("REVIEW_CURRENT_USE_UNBOUND")
        validator.require_current_locked(intent, binding)


#: The classified first-invocation outcomes that fund the one second invocation,
#: with the receipt kind each is recorded under.
_SECOND_INVOCATION_SOURCES = {
    "FORMAT_INVALID": "AssuranceReviewFormatRejected",
    "TURN_FAILED": "AssuranceReviewClassified",
    # A committed reply that decodes but cannot be imported as given (2026-09-26).
    "INTERPRETATION_INVALID": "AssuranceReviewInterpretationRejected",
}

#: What the reviewer is told when its first reply could not be imported.
_INTERPRETATION_FEEDBACK = {
    "UNEXPOSED_EVIDENCE": (
        "evidence_ids cited a label that was never disclosed to you. Cite only ev- labels "
        "given in the initial evidence or returned by assurance_read_evidence with "
        "complete=true; a label seen only in a find/list result or a partial page is not "
        "evidence — read it in full first, or leave it out."
    ),
    "DUPLICATE_CRITERION": "a criterion_id appears more than once in assessments; give each exactly once.",
    "FINDING_SCOPE": "a finding names a criterion_id outside this review's criterion_ids.",
    "MANDATORY_CRITERIA_INVALID": "assessments must cover exactly the given criterion_ids, no more and no fewer.",
}


#: What a strict decoding code means for the reply, in the reviewer's own terms.
#: Real run 2026-09-28 (mission-655daf8071519553): one assessment reason was 2069
#: characters, the repair round said only "TEXT_INVALID", the reviewer sent the same
#: reply back and the Mission failed. The rules are the decoder's, restated.
_FORMAT_FEEDBACK = {
    "TEXT_INVALID": (
        "a text value is empty or too long. Each assessment reason and each finding reason "
        "must be 1 to 2000 characters; every other string (criterion_id, each evidence id, "
        "each limitation) 1 to 2048 characters. Shorten the longest reason and answer again "
        "with the same verdicts."
    ),
    "ARRAY_INVALID": (
        "an array has the wrong size: assessments needs at least one item; evidence_ids at "
        "most 64, limitations at most 16, findings at most 128."
    ),
    "DUPLICATE_SET_MEMBER": "evidence_ids or limitations repeats the same value; list each once.",
    "OBJECT_FIELDS_MISSING": (
        "a required field is missing. The reply has exactly schema_version, verdict, "
        "assessments and findings; each assessment has criterion_id, verdict, evidence_ids, "
        "reason and limitations; each finding has criterion_id, severity and reason."
    ),
    "OBJECT_FIELDS_UNKNOWN": "a field outside the ReviewReply v2 shape is present; remove it.",
    "ENUM_INVALID": (
        "an enumerated value is not allowed: verdict is ACCEPT, REWORK, INCONCLUSIVE or "
        "REJECTED; severity is BLOCKER, WARNING or INFO; an assessment verdict is one of "
        "the grades named in the request."
    ),
    "REVIEW_SCHEMA_VERSION": "schema_version must be the integer 2.",
    "JSON_INVALID": "the reply is not one JSON object; answer with the JSON object only.",
    "JSON_DUPLICATE_KEY": "an object repeats a key; give each key once.",
}


def _require_format_repair(
    commit: CommitService, reader: AssuranceReader, review_key: str, prior: AssuranceRef
) -> str:
    """Validate the first invocation's classified failure; return its classification."""
    if prior.kind != "commit_receipt":
        raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
    metadata = reader.read_exact_metadata(prior)
    row, body = decode(metadata.lifecycle_json), decode(metadata.body_json)
    previous = commit.store.connection.execute(
        "SELECT invocation_json FROM assurance_review_invocations WHERE review_key=? AND ordinal=1",
        (review_key,),
    ).fetchone()
    if previous is None:
        raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
    invocation = ReviewInvocation(previous[0]).to_json()
    intent = commit.store.get_intent(invocation["dispatch_intent_id"])
    if (
        intent is None
        or row["subject_id"] != intent.intent_id
        or row["proposal_hash"] != fingerprint(body)
        or row["base_version"] != 0
        or body.get("review_key") != review_key
        or body.get("invocation_ordinal") != 1
        or body.get("intent_id") != intent.intent_id
        or intent.state not in {"SETTLED", "FAILED"}
        or _SECOND_INVOCATION_SOURCES.get(str(body.get("classification"))) != row["kind"]
    ):
        raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
    classification = str(body["classification"])
    turn_ref = AssuranceRef.from_json(body.get("turn_ref"), kinds={"agent_turn_receipt"})
    turn = decode(reader.read_exact_metadata(turn_ref).body_json)["payload"]
    if (
        turn.get("intent_id") != intent.intent_id
        or turn.get("turn_id") != intent.expected_turn_id
        or turn.get("agent_id") != intent.agent_id
        # a malformed reply was a committed turn; a failed turn never committed
        or (turn.get("state") == "COMMITTED")
        != (classification in {"FORMAT_INVALID", "INTERPRETATION_INVALID"})
    ):
        raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
    if commit._assurance_settlement is None:
        raise AssuranceError("REVIEW_FORMAT_REPAIR_ACCOUNTING_UNAVAILABLE")
    # Absence of imported UNKNOWN is insufficient: an invocation or effect may
    # still be missing from that ledger. Read the complete original inventories
    # before funding the only permitted second invocation.
    try:
        if commit.ledger.has_unknown_usage(intent.subject_id):
            raise BudgetError("unknown provider charge on the first review invocation")
        commit._assurance_settlement.require_settled_locked(intent.subject_id, reader.mission_id)
    except BudgetError:
        # Count rule (user, 2026-09-24): overcount, never undercount, never freeze.
        # A turn that failed before committing (e.g. provider 5xx, no usage fact)
        # keeps its whole reservation held — counted at its upper bound, visible —
        # and the one second invocation is funded by its own reservation.  A
        # malformed reply was a completed call: it must settle first, as before.
        reservation = commit.ledger.reservation(intent.subject_id)
        if classification != "TURN_FAILED" or reservation is None or reservation.get("state") != "RESERVED":
            raise
    package = commit.store.connection.execute(
        "SELECT package_id FROM assurance_review_bindings WHERE review_key=?", (review_key,)
    ).fetchone()
    if HtnStore(commit.store).official_review_record(package[0]) is not None:
        raise AssuranceError("REVIEW_ALREADY_OFFICIAL")
    from ..assurance.policy import AssurancePolicy

    profile = commit.store.connection.execute(
        "SELECT policy_json FROM assurance_mission_bindings WHERE mission_id=?",
        (reader.mission_id,),
    ).fetchone()
    if profile is None or AssurancePolicy.from_json(decode(profile[0])).format_retries != 1:
        raise AssuranceError("REVIEW_FORMAT_REPAIR_DISABLED")
    return classification


def ensure_format_repair_invocation(
    commit: CommitService, *, tenant_id: str, prior_failure: AssuranceRef
) -> ReviewInvocation:
    """One bounded repair of an actual malformed response, on the same package.

    Original frozen evidence bytes are resent. No latest search, implicit second
    semantic review, extra account or fabricated zero-cost reservation is used.
    The current handoff validator must admit these bytes before reserving again.
    """
    from ..assurance.codec import canonical
    from ..runtime.agent_worker import user_message_json
    from .commit_service import Reservation

    original = commit.store.get_receipt(prior_failure.pin.id)
    if original is None:
        raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
    reader = AssuranceReader(
        commit.store, tenant_id=tenant_id, mission_id=original.get("mission_id")
    )
    with commit.store.read_view():
        reader.read_exact_metadata(prior_failure)
        replay = commit.store.connection.execute(
            "SELECT dispatch_intent_id FROM assurance_review_invocations WHERE mission_id=? AND review_key=? AND ordinal=2",
            (reader.mission_id, original.get("review_key")),
        ).fetchone()
        if replay is not None:
            invocation, _ = read_review_invocation_locked(commit, reader, replay[0])
            if invocation.to_json()["prior_failure_receipt_ref"] != prior_failure.to_json():
                raise AssuranceError("REVIEW_REPAIR_SOURCE_INVALID")
            return invocation
        classification = _require_format_repair(
            commit, reader, original.get("review_key"), prior_failure
        )
        invocation, binding = read_review_invocation_locked(
            commit, reader, original.get("intent_id")
        )
        old_intent = commit.store.get_intent(invocation.to_json()["dispatch_intent_id"])
        source = decode(
            reader.read_exact_metadata(
                AssuranceRef.from_json(invocation.to_json()["source_receipt_ref"])
            ).body_json
        )
        request = source["request"]
        package = HtnStore(commit.store).get_review_package(binding.to_json()["package_ref"]["id"])
        config = dict(old_intent.config)
        if classification == "FORMAT_INVALID":
            message = decode(config["message"]["content"])
            code = text(original.get("error_code"))
            message["format_feedback"] = (
                "Previous output failed strict ReviewReply v2 decoding: " + code
                + ("" if code not in _FORMAT_FEEDBACK else " — " + _FORMAT_FEEDBACK[code])
            )
            config["message"] = user_message_json(canonical(message))
        elif classification == "INTERPRETATION_INVALID":
            code = text(original.get("error_code"))
            message = decode(config["message"]["content"])
            message["format_feedback"] = (
                "Previous reply could not be imported (" + code + "): "
                + _INTERPRETATION_FEEDBACK.get(code, "fix the reply and answer again.")
            )
            config["message"] = user_message_json(canonical(message))
        # TURN_FAILED: the same frozen request is asked again, unchanged.

    def require_current() -> None:
        validator = commit._assurance_review_handoff
        if validator is None:
            raise AssuranceError("REVIEW_CURRENT_USE_UNBOUND")
        validator.require_current_locked(old_intent, binding)

    return ensure_review_invocation(
        commit,
        tenant_id=tenant_id,
        binding=binding,
        package=package,
        request_command_id=request["request_command_id"],
        config=config,
        reservation=Reservation(
            tokens=request["reservation"]["tokens"],
            cost_micros=request["reservation"]["cost_micros"],
        ),
        require_current_locked=require_current,
        prior_failure=prior_failure,
        repair_reason="TURN_RETRY" if classification == "TURN_FAILED" else "FORMAT_REPAIR",
    )
