# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Store adapter for immutable operation-completion side bindings.

Writers must already own the encompassing CommitService transaction.  This is
load-bearing for PlanRevision+Scope and Acceptance+Contribution atomicity: this
adapter never opens a smaller transaction which could commit independently.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.htn import TaskSemanticBindingV1
from ..contracts.models import ContractError
from ..contracts.semantic_base import TypedRefKind, content_hash_of, hash_hex, identifier
from .htn_store import HtnStore
from .store import Store, StoreConflict, StoreError


def _completion_contracts() -> tuple[type[Any], type[Any], type[Any], type[Any]]:
    # Local import keeps schema discovery independent while the contract module and
    # this adapter are installed in parallel.
    from ..contracts.operation_completion import (
        AcceptanceContributionScopeV1,
        OccurrenceCompletionScopeV1,
        OperationCompletionRequirementsV1,
        OperationOutcomeReviewBindingV1,
    )

    return (
        OperationCompletionRequirementsV1,
        OccurrenceCompletionScopeV1,
        OperationOutcomeReviewBindingV1,
        AcceptanceContributionScopeV1,
    )


def _validated(value: object, kind: type[Any], name: str) -> tuple[Any, dict[str, Any], str, str]:
    raw = value.to_json() if isinstance(value, kind) else value
    parsed = kind.from_json(raw, name)
    document = dict(parsed.to_json())
    encoded = canonical_json(document)
    digest = parsed.content_hash()
    return parsed, document, encoded if isinstance(encoded, str) else str(encoded), digest


def _pin(document: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = document[field]
    if not isinstance(value, Mapping):  # strict codec normally makes this unreachable
        raise StoreConflict(f"{field} is not a completion pin")
    return value


def _derived_id(role: str, *parts: object) -> str:
    digest = content_hash_of([role, *[str(part) for part in parts]])
    return f"{role}-{digest[:32]}"


class OperationCompletionStore:
    """Exact readers and idempotent insert-only writers for the four bindings."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _connection(self) -> Any:
        connection = self._store.connection
        if (
            not connection.in_transaction
            or self._store._depth <= 0  # noqa: SLF001 - enforce Store's writer boundary
            or self._store._reading  # noqa: SLF001 - a read_view is never write authority
        ):
            raise StoreError("operation completion writes require an outer Store transaction")
        return connection

    def _timestamp(
        self,
        connection: Any,
        *,
        table: str,
        key_column: str,
        key_value: str,
        supplied: int | None,
    ) -> int:
        """Keep an omitted write timestamp stable across idempotent retries."""

        if supplied is not None:
            return int(supplied)
        existing = connection.execute(
            f"SELECT created_at_ms FROM {table} WHERE {key_column}=?",  # noqa: S608
            (key_value,),
        ).fetchone()
        return int(existing[0]) if existing is not None else int(self._store.now * 1000)

    @staticmethod
    def _insert_exact(
        connection: Any,
        *,
        table: str,
        key_where: str,
        key_values: tuple[Any, ...],
        fields: tuple[str, ...],
        values: tuple[Any, ...],
        conflict: str,
    ) -> None:
        existing = connection.execute(
            f"SELECT {','.join(fields)} FROM {table} WHERE {key_where}",  # noqa: S608
            key_values,
        ).fetchone()
        if existing is not None:
            if tuple(existing) == values:
                return
            raise StoreConflict(conflict)
        try:
            connection.execute(
                f"INSERT INTO {table}({','.join(fields)}) "  # noqa: S608
                f"VALUES ({','.join('?' for _ in fields)})",
                values,
            )
        except sqlite3.IntegrityError as error:
            raise StoreConflict(f"{conflict}: {error}") from error

    def _decoded(self, row: Any, kind: type[Any], name: str) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        try:
            raw = json.loads(value.pop("document_json"))
            document = kind.from_json(raw, name)
            digest = document.content_hash()
        except (ContractError, RecursionError, TypeError, ValueError) as error:
            raise StoreConflict(f"{name} is unreadable") from error
        try:
            if "approval_receipt_id" in value:
                self._validate_spec_row(value, document, digest)
            elif "plan_receipt_id" in value:
                self._validate_scope_row(value, document, digest)
            elif "binding_hash" in value:
                self._validate_outcome_row(value, document, digest)
            elif "contribution_kind" in value:
                self._validate_contribution_row(value, document, digest)
            else:
                raise StoreConflict(f"{name} has an unknown completion row shape")
        except (ContractError, KeyError, TypeError, ValueError) as error:
            raise StoreConflict(f"{name} immutable identity differs") from error
        value["document"] = document
        return value

    def _validate_spec_row(self, value: Mapping[str, Any], document: Any, digest: str) -> None:
        if (
            digest != str(value["spec_hash"])
            or document.spec_id != str(value["spec_id"])
            or document.mission_id != str(value["mission_id"])
            or document.requirements_ref.revision != int(value["requirements_revision"])
            or document.requirements_ref.content_hash != str(value["requirements_hash"])
        ):
            raise StoreConflict("stored completion Spec identity differs")
        receipt = self._store.get_receipt(str(value["approval_receipt_id"]))
        requirements_ref = receipt.get("requirements_ref") if receipt is not None else None
        if (
            receipt is None
            or receipt.get("kind") != "operation_completion_spec_approved"
            or receipt.get("mission_id") != document.mission_id
            or receipt.get("spec_id") != document.spec_id
            or receipt.get("spec_hash") != digest
            or not isinstance(requirements_ref, Mapping)
            or requirements_ref.get("kind") != str(TypedRefKind.REQUIREMENTS)
            or requirements_ref.get("id") != document.requirements_ref.id
            or requirements_ref.get("revision") != document.requirements_ref.revision
            or requirements_ref.get("content_hash") != document.requirements_ref.content_hash
        ):
            raise StoreConflict("stored completion Spec approval receipt differs")

    def _validate_scope_row(self, value: Mapping[str, Any], document: Any, digest: str) -> None:
        if (
            digest != str(value["scope_hash"])
            or document.scope_id != str(value["scope_id"])
            or document.mission_id != str(value["mission_id"])
            or document.plan_ref.revision != int(value["plan_revision"])
            or document.occurrence_id != str(value["occurrence_id"])
            or document.task_ref.id != str(value["task_id"])
            or document.task_ref.content_hash != str(value["task_contract_hash"])
            or document.spec_hash != str(value["spec_hash"])
        ):
            raise StoreConflict("stored completion scope identity differs")
        semantic = self._store.connection.execute(
            "SELECT binding_json,content_hash FROM task_semantics "
            "WHERE mission_id=? AND task_id=? AND binding_revision=?",
            (
                str(value["mission_id"]),
                str(value["task_id"]),
                int(value["task_binding_revision"]),
            ),
        ).fetchone()
        if semantic is None:
            raise StoreConflict("stored completion scope task binding is unavailable")
        binding = TaskSemanticBindingV1.from_json(json.loads(str(semantic["binding_json"])))
        if (
            binding.content_hash() != str(semantic["content_hash"])
            or binding.contract_revision != document.task_ref.revision
            or binding.contract_hash != document.task_ref.content_hash
        ):
            raise StoreConflict("stored completion scope task binding differs")

    def _validate_outcome_row(self, value: Mapping[str, Any], document: Any, digest: str) -> None:
        if (
            digest != str(value["binding_hash"])
            or document.mission_id != str(value["mission_id"])
            or document.spec_hash != str(value["spec_hash"])
            or document.effect_key != str(value["effect_key"])
            or document.completion_scope_id != str(value["completion_scope_id"])
            or document.intent_id != str(value["intent_id"])
            or document.operation_id != str(value["operation_id"])
            or document.operation_occurrence_id != str(value["operation_occurrence_id"])
            or document.action_key != str(value["action_key"])
            or document.action_version != int(value["action_version"])
            or document.request_hash != str(value["request_hash"])
            or document.effect_contract_hash != str(value["effect_contract_hash"])
        ):
            raise StoreConflict("stored operation outcome binding identity differs")
        scope = self._store.connection.execute(
            "SELECT scope_hash FROM operation_completion_scopes WHERE mission_id=? AND scope_id=?",
            (str(value["mission_id"]), str(value["completion_scope_id"])),
        ).fetchone()
        if scope is None or str(value["binding_id"]) != _derived_id(
            "op-outcome-review",
            document.intent_id,
            document.spec_hash,
            document.effect_key,
            str(scope["scope_hash"]),
            str(value["source_manifest_hash"]),
        ):
            raise StoreConflict("stored operation outcome binding derived identity differs")

    def _validate_contribution_row(
        self, value: Mapping[str, Any], document: Any, digest: str
    ) -> None:
        delivery_ref = document.delivery_receipt_ref
        delivery_id = None if delivery_ref is None else delivery_ref.id
        if (
            digest != str(value["contribution_hash"])
            or document.mission_id != str(value["mission_id"])
            or document.acceptance_id != str(value["acceptance_id"])
            or document.completion_scope_id != str(value["completion_scope_id"])
            or str(document.kind) != str(value["contribution_kind"])
            or document.outcome_binding_id != value["outcome_binding_id"]
            or delivery_id != value["delivery_receipt_id"]
        ):
            raise StoreConflict("stored acceptance contribution identity differs")
        scope = self._store.connection.execute(
            "SELECT scope_hash,spec_hash FROM operation_completion_scopes "
            "WHERE mission_id=? AND scope_id=?",
            (str(value["mission_id"]), str(value["completion_scope_id"])),
        ).fetchone()
        if (
            scope is None
            or str(scope["scope_hash"]) != str(value["scope_hash"])
            or str(scope["spec_hash"]) != document.spec_hash
        ):
            raise StoreConflict("stored acceptance contribution parent scope differs")
        self._validate_acceptance_producer(
            mission_id=str(value["mission_id"]),
            acceptance_id=str(value["acceptance_id"]),
            producer_receipt_id=str(value["producer_receipt_id"]),
            output_artifact_refs=document.output_artifact_refs,
        )

    def _validate_acceptance_producer(
        self,
        *,
        mission_id: str,
        acceptance_id: str,
        producer_receipt_id: str,
        output_artifact_refs: tuple[Any, ...],
    ) -> None:
        """Bind a contribution to its real accept-side receipt and accepted artifacts."""

        semantics = HtnStore(self._store)
        try:
            acceptance = semantics.get_acceptance(acceptance_id)
        except StoreError as error:
            raise StoreConflict(
                "acceptance contribution source acceptance is unavailable"
            ) from error
        if acceptance.mission_id != mission_id or str(acceptance.acceptance_id) != acceptance_id:
            raise StoreConflict("acceptance contribution source acceptance differs")
        receipt = semantics.find_acceptance_receipt(mission_id, producer_receipt_id)
        expected_identity = {
            "kind": "acceptance",
            "subject_id": acceptance_id,
            "content_hash": content_hash_of(acceptance.to_json()),
            "task_id": str(acceptance.task_id),
            "obligation_id": str(acceptance.obligation_id),
        }
        if (
            receipt is None
            or receipt.kind != "acceptance"
            or receipt.subject_id != acceptance_id
            or dict(receipt.output_identity) != expected_identity
        ):
            raise StoreConflict("acceptance contribution producer receipt differs")
        accepted_artifacts = {
            (ref.id, ref.revision, ref.content_hash)
            for ref in acceptance.artifact_refs
            if str(ref.kind) == "artifact"
        }
        if any(
            (ref.id, ref.revision, ref.content_hash) not in accepted_artifacts
            for ref in output_artifact_refs
        ):
            raise StoreConflict("acceptance contribution artifact was not accepted")

    def insert_spec(
        self,
        spec_id: str,
        document: object,
        *,
        approval_receipt_id: str,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        spec_kind, _, _, _ = _completion_contracts()
        parsed, body, encoded, digest = _validated(document, spec_kind, "completion_spec")
        mission_id = str(body["mission_id"])
        requirement = _pin(body, "requirements_ref")
        revision = int(requirement["revision"])
        requirement_hash = str(requirement["content_hash"])
        connection = self._connection()
        stable_spec_id = identifier(spec_id, "spec_id")
        if stable_spec_id != parsed.spec_id:
            raise StoreConflict("operation completion spec derived identity mismatch")
        source = connection.execute(
            "SELECT content_hash FROM requirements_revisions WHERE mission_id=? AND revision=?",
            (mission_id, revision),
        ).fetchone()
        if source is None or str(source[0]) != requirement_hash:
            raise StoreConflict("operation completion requirements identity mismatch")
        receipt = connection.execute(
            "SELECT kind,subject_id FROM commit_receipts WHERE commit_id=?",
            (str(approval_receipt_id),),
        ).fetchone()
        if receipt is None or (str(receipt["kind"]), str(receipt["subject_id"])) != (
            "operation_completion_spec_approved",
            stable_spec_id,
        ):
            raise StoreConflict("operation completion approval receipt mismatch")
        timestamp = self._timestamp(
            connection,
            table="operation_completion_specs",
            key_column="spec_id",
            key_value=stable_spec_id,
            supplied=created_at_ms,
        )
        fields = (
            "spec_id",
            "mission_id",
            "requirements_revision",
            "requirements_hash",
            "spec_hash",
            "document_json",
            "approval_receipt_id",
            "created_at_ms",
        )
        values = (
            stable_spec_id,
            mission_id,
            revision,
            requirement_hash,
            str(digest),
            encoded,
            str(approval_receipt_id),
            timestamp,
        )
        self._insert_exact(
            connection,
            table="operation_completion_specs",
            key_where="spec_id=?",
            key_values=(stable_spec_id,),
            fields=fields,
            values=values,
            conflict="operation completion spec conflict",
        )
        return self.get_spec_exact(mission_id, revision, requirement_hash) or {}

    def get_spec_exact(
        self, mission_id: str, requirements_revision: int, requirements_hash: str
    ) -> dict[str, Any] | None:
        spec_kind, _, _, _ = _completion_contracts()
        row = self._store.connection.execute(
            "SELECT * FROM operation_completion_specs WHERE mission_id=? "
            "AND requirements_revision=? AND requirements_hash=?",
            (str(mission_id), int(requirements_revision), str(requirements_hash)),
        ).fetchone()
        return self._decoded(row, spec_kind, "stored_completion_spec")

    def insert_scope(
        self,
        scope_id: str,
        document: object,
        *,
        plan_receipt_id: str,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        _, scope_kind, _, _ = _completion_contracts()
        parsed, body, encoded, digest = _validated(document, scope_kind, "completion_scope")
        mission_id = str(body["mission_id"])
        plan_ref = _pin(body, "plan_ref")
        task_ref = _pin(body, "task_ref")
        plan_revision = int(plan_ref["revision"])
        occurrence_id = str(body["occurrence_id"])
        task_id = str(task_ref["id"])
        task_contract_revision = int(task_ref["revision"])
        task_contract_hash = str(task_ref["content_hash"])
        spec_hash = str(body["spec_hash"])
        connection = self._connection()
        stable_scope_id = identifier(scope_id, "scope_id")
        if stable_scope_id != parsed.scope_id:
            raise StoreConflict("operation completion scope derived identity mismatch")

        plan = connection.execute(
            "SELECT snapshot_hash FROM plan_revisions WHERE mission_id=? AND revision=?",
            (mission_id, plan_revision),
        ).fetchone()
        member = connection.execute(
            "SELECT task_id,obligation_id FROM plan_memberships "
            "WHERE mission_id=? AND revision=? AND occurrence_id=?",
            (mission_id, plan_revision, occurrence_id),
        ).fetchone()
        if (
            plan is None
            or str(plan[0]) != str(plan_ref["snapshot_hash"])
            or member is None
            or str(member["task_id"]) != task_id
            or str(member["obligation_id"]) != str(body["obligation_id"])
        ):
            raise StoreConflict("operation completion plan membership mismatch")
        semantic = connection.execute(
            "SELECT binding_revision,binding_json,content_hash FROM task_semantics "
            "WHERE task_id=? AND mission_id=? ORDER BY binding_revision DESC LIMIT 1",
            (task_id, mission_id),
        ).fetchone()
        if semantic is None:
            raise StoreConflict("operation completion task contract mismatch")
        binding = TaskSemanticBindingV1.from_json(json.loads(semantic["binding_json"]))
        if (
            binding.content_hash() != str(semantic["content_hash"])
            or int(binding.contract_revision) != task_contract_revision
            or binding.contract_hash != task_contract_hash
            or str(binding.task_id) != task_id
            or str(binding.obligation_id) != str(body["obligation_id"])
        ):
            raise StoreConflict("operation completion task contract mismatch")
        task_binding_revision = int(semantic[0])
        plan_receipt = connection.execute(
            "SELECT mission_id,new_plan_revision FROM plan_commit_receipts WHERE command_id=?",
            (str(plan_receipt_id),),
        ).fetchone()
        if plan_receipt is None or (str(plan_receipt[0]), int(plan_receipt[1])) != (
            mission_id,
            plan_revision,
        ):
            raise StoreConflict("operation completion plan receipt mismatch")
        spec = connection.execute(
            "SELECT spec_id,requirements_revision,requirements_hash "
            "FROM operation_completion_specs WHERE mission_id=? AND spec_hash=?",
            (mission_id, spec_hash),
        ).fetchone()
        requirements_ref = _pin(body, "requirements_ref")
        if (
            spec is None
            or int(spec["requirements_revision"]) != int(requirements_ref["revision"])
            or str(spec["requirements_hash"]) != str(requirements_ref["content_hash"])
        ):
            raise StoreConflict("operation completion spec is unavailable")
        timestamp = self._timestamp(
            connection,
            table="operation_completion_scopes",
            key_column="scope_id",
            key_value=stable_scope_id,
            supplied=created_at_ms,
        )
        fields = (
            "scope_id",
            "mission_id",
            "plan_revision",
            "occurrence_id",
            "task_id",
            "task_binding_revision",
            "task_contract_hash",
            "spec_id",
            "spec_hash",
            "scope_hash",
            "document_json",
            "plan_receipt_id",
            "created_at_ms",
        )
        values = (
            stable_scope_id,
            mission_id,
            plan_revision,
            occurrence_id,
            task_id,
            task_binding_revision,
            task_contract_hash,
            str(spec[0]),
            spec_hash,
            str(digest),
            encoded,
            str(plan_receipt_id),
            timestamp,
        )
        self._insert_exact(
            connection,
            table="operation_completion_scopes",
            key_where="scope_id=?",
            key_values=(stable_scope_id,),
            fields=fields,
            values=values,
            conflict="operation completion scope conflict",
        )
        return self.get_scope_exact(mission_id, plan_revision, occurrence_id) or {}

    def get_scope_exact(
        self, mission_id: str, plan_revision: int, occurrence_id: str
    ) -> dict[str, Any] | None:
        _, scope_kind, _, _ = _completion_contracts()
        row = self._store.connection.execute(
            "SELECT * FROM operation_completion_scopes WHERE mission_id=? "
            "AND plan_revision=? AND occurrence_id=?",
            (str(mission_id), int(plan_revision), str(occurrence_id)),
        ).fetchone()
        return self._decoded(row, scope_kind, "stored_completion_scope")

    def insert_outcome_binding(
        self,
        binding_id: str,
        document: object,
        *,
        source_manifest_hash: str,
        review_package_id: str,
        producer_receipt_id: str,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        spec_kind, scope_kind, binding_kind, _ = _completion_contracts()
        _parsed, body, encoded, digest = _validated(
            document, binding_kind, "operation_outcome_binding"
        )
        mission_id = str(body["mission_id"])
        spec_hash = str(body["spec_hash"])
        effect_key = str(body["effect_key"])
        completion_scope_id = str(body["completion_scope_id"])
        manifest_hash = hash_hex(source_manifest_hash, "source_manifest_hash")
        connection = self._connection()
        spec = connection.execute(
            "SELECT spec_id,requirements_revision,document_json "
            "FROM operation_completion_specs "
            "WHERE mission_id=? AND spec_hash=?",
            (mission_id, spec_hash),
        ).fetchone()
        scope = connection.execute(
            "SELECT spec_id,spec_hash,scope_hash,document_json "
            "FROM operation_completion_scopes "
            "WHERE mission_id=? AND scope_id=?",
            (mission_id, completion_scope_id),
        ).fetchone()
        if (
            spec is None
            or scope is None
            or str(scope["spec_id"]) != str(spec[0])
            or str(scope["spec_hash"]) != spec_hash
        ):
            raise StoreConflict("operation outcome completion identity mismatch")
        intent = connection.execute(
            "SELECT mission_id,candidate_file_hash,parameters_content_hash,params_hash,"
            "effect_content_hash,request_hash,binding_json FROM operation_intent_bindings "
            "WHERE intent_id=?",
            (str(body["intent_id"]),),
        ).fetchone()
        identity = connection.execute(
            "SELECT mission_id FROM operation_identities WHERE operation_id=? AND request_hash=?",
            (str(body["operation_id"]), str(body["request_hash"])),
        ).fetchone()
        operation = connection.execute(
            "SELECT operation_id,mission_id,request_hash FROM operation_bindings "
            "WHERE operation_occurrence_id=?",
            (str(body["operation_occurrence_id"]),),
        ).fetchone()
        link = connection.execute(
            "SELECT action_key,action_version,request_hash,mission_id,link_hash,"
            "operation_occurrence_id,params_hash "
            "FROM planning_operation_action_links WHERE operation_id=?",
            (str(body["operation_id"]),),
        ).fetchone()
        if (
            intent is None
            or (
                str(intent["mission_id"]),
                str(intent["candidate_file_hash"]),
                str(intent["parameters_content_hash"]),
                str(intent["params_hash"]),
                str(intent["effect_content_hash"]),
                str(intent["request_hash"]),
            )
            != (
                mission_id,
                str(body["candidate_file_hash"]),
                str(body["parameters_content_hash"]),
                str(body["params_hash"]),
                str(body["effect_contract_hash"]),
                str(body["request_hash"]),
            )
            or identity is None
            or str(identity[0]) != mission_id
            or operation is None
            or (
                str(operation["operation_id"]),
                str(operation["mission_id"]),
                str(operation["request_hash"]),
            )
            != (str(body["operation_id"]), mission_id, str(body["request_hash"]))
            or link is None
            or (
                str(link["action_key"]),
                int(link["action_version"]),
                str(link["request_hash"]),
                str(link["mission_id"]),
                str(link["link_hash"]),
                str(link["operation_occurrence_id"]),
                str(link["params_hash"]),
            )
            != (
                str(body["action_key"]),
                int(body["action_version"]),
                str(body["request_hash"]),
                mission_id,
                str(body["link_hash"]),
                str(body["operation_occurrence_id"]),
                str(body["params_hash"]),
            )
        ):
            raise StoreConflict("operation outcome immutable execution identity mismatch")
        try:
            intent_binding = json.loads(str(intent["binding_json"]))
        except (ContractError, TypeError, ValueError) as error:
            raise StoreConflict("operation outcome intent binding is unreadable") from error
        completion = intent_binding.get("completion") if isinstance(intent_binding, dict) else None
        if not isinstance(completion, dict):
            raise StoreConflict("operation outcome intent completion identity is absent")
        old_spec = connection.execute(
            "SELECT document_json FROM operation_completion_specs "
            "WHERE mission_id=? AND spec_id=? AND spec_hash=?",
            (
                mission_id,
                str(completion.get("spec_id")),
                str(completion.get("spec_hash")),
            ),
        ).fetchone()
        old_scope = connection.execute(
            "SELECT document_json,scope_hash FROM operation_completion_scopes "
            "WHERE mission_id=? AND scope_id=? AND spec_hash=?",
            (
                mission_id,
                str(completion.get("scope_id")),
                str(completion.get("spec_hash")),
            ),
        ).fetchone()
        if (
            old_spec is None
            or old_scope is None
            or str(old_scope["scope_hash"]) != str(completion.get("scope_hash"))
        ):
            raise StoreConflict("operation outcome frozen intent completion source is unavailable")
        try:
            old_spec_document = spec_kind.from_json(
                json.loads(str(old_spec["document_json"])), "intent_completion_spec"
            )
            old_scope_document = scope_kind.from_json(
                json.loads(str(old_scope["document_json"])), "intent_completion_scope"
            )
            current_spec_document = spec_kind.from_json(
                json.loads(str(spec["document_json"])), "outcome_completion_spec"
            )
            current_scope_document = scope_kind.from_json(
                json.loads(str(scope["document_json"])), "outcome_completion_scope"
            )
            old_spec_document.effect(str(completion.get("effect_key")))
            current_spec_document.effect(effect_key)
        except (ContractError, TypeError, ValueError) as error:
            raise StoreConflict("operation outcome completion source is unreadable") from error
        if (
            str(completion.get("effect_key")) not in old_scope_document.owned_effect_keys
            or effect_key not in current_scope_document.owned_effect_keys
            or old_scope_document.obligation_id != current_scope_document.obligation_id
            or old_scope_document.task_ref != current_scope_document.task_ref
        ):
            raise StoreConflict(
                "operation outcome fact is not valid for the current completion slot"
            )
        stable_binding_id = identifier(binding_id, "binding_id")
        expected_binding_id = _derived_id(
            "op-outcome-review",
            str(body["intent_id"]),
            spec_hash,
            effect_key,
            str(scope["scope_hash"]),
            manifest_hash,
        )
        if stable_binding_id != expected_binding_id:
            raise StoreConflict("operation outcome review derived identity mismatch")
        review_package = connection.execute(
            "SELECT mission_id,purpose,requirements_revision FROM review_packages "
            "WHERE package_id=?",
            (str(review_package_id),),
        ).fetchone()
        if review_package is None or (
            str(review_package["mission_id"]),
            str(review_package["purpose"]),
            int(review_package["requirements_revision"]),
        ) != (mission_id, "OPERATION_OUTCOME", int(spec["requirements_revision"])):
            raise StoreConflict("operation outcome review package mismatch")
        timestamp = self._timestamp(
            connection,
            table="operation_outcome_review_bindings",
            key_column="binding_id",
            key_value=stable_binding_id,
            supplied=created_at_ms,
        )
        fields = (
            "binding_id",
            "mission_id",
            "spec_id",
            "spec_hash",
            "effect_key",
            "completion_scope_id",
            "intent_id",
            "operation_id",
            "operation_occurrence_id",
            "action_key",
            "action_version",
            "request_hash",
            "effect_contract_hash",
            "source_manifest_hash",
            "binding_hash",
            "document_json",
            "review_package_id",
            "producer_receipt_id",
            "created_at_ms",
        )
        values = (
            stable_binding_id,
            mission_id,
            str(spec["spec_id"]),
            spec_hash,
            effect_key,
            completion_scope_id,
            str(body["intent_id"]),
            str(body["operation_id"]),
            str(body["operation_occurrence_id"]),
            str(body["action_key"]),
            int(body["action_version"]),
            str(body["request_hash"]),
            str(body["effect_contract_hash"]),
            manifest_hash,
            str(digest),
            encoded,
            str(review_package_id),
            str(producer_receipt_id),
            timestamp,
        )
        self._insert_exact(
            connection,
            table="operation_outcome_review_bindings",
            key_where="binding_id=?",
            key_values=(stable_binding_id,),
            fields=fields,
            values=values,
            conflict="operation outcome review binding conflict",
        )
        return self.get_outcome_binding_exact(mission_id, stable_binding_id) or {}

    def get_outcome_binding_exact(self, mission_id: str, binding_id: str) -> dict[str, Any] | None:
        _, _, binding_kind, _ = _completion_contracts()
        row = self._store.connection.execute(
            "SELECT * FROM operation_outcome_review_bindings WHERE mission_id=? AND binding_id=?",
            (str(mission_id), str(binding_id)),
        ).fetchone()
        return self._decoded(row, binding_kind, "stored_operation_outcome_binding")

    def insert_acceptance_scope(
        self,
        document: object,
        *,
        producer_receipt_id: str,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        _, scope_kind, _, contribution_kind = _completion_contracts()
        parsed, body, encoded, digest = _validated(
            document, contribution_kind, "acceptance_contribution_scope"
        )
        mission_id = str(body["mission_id"])
        acceptance_id = str(body["acceptance_id"])
        completion_scope_id = str(body["completion_scope_id"])
        connection = self._connection()
        scope = connection.execute(
            "SELECT spec_hash,scope_hash,document_json,task_id,task_binding_revision "
            "FROM operation_completion_scopes "
            "WHERE mission_id=? AND scope_id=?",
            (mission_id, completion_scope_id),
        ).fetchone()
        if scope is None or str(scope["spec_hash"]) != str(body["spec_hash"]):
            raise StoreConflict("acceptance contribution completion scope mismatch")
        try:
            scope_document = scope_kind.from_json(
                json.loads(str(scope["document_json"])), "acceptance_completion_scope"
            )
        except (ContractError, TypeError, ValueError) as error:
            raise StoreConflict("acceptance contribution completion scope is unreadable") from error
        acceptance = connection.execute(
            "SELECT mission_id,task_id,requirements_revision,contract_revision "
            "FROM acceptances WHERE acceptance_id=?",
            (acceptance_id,),
        ).fetchone()
        requirements_revision = int(scope_document.requirements_ref.revision)
        if acceptance is None or (
            str(acceptance["mission_id"]),
            str(acceptance["task_id"]),
            int(acceptance["requirements_revision"]),
            int(acceptance["contract_revision"]),
        ) != (
            mission_id,
            str(scope["task_id"]),
            requirements_revision,
            int(scope_document.task_ref.revision),
        ):
            raise StoreConflict("acceptance contribution acceptance mismatch")
        self._validate_acceptance_producer(
            mission_id=mission_id,
            acceptance_id=acceptance_id,
            producer_receipt_id=str(producer_receipt_id),
            output_artifact_refs=parsed.output_artifact_refs,
        )
        outcome_binding_id = body.get("outcome_binding_id")
        delivery = body.get("delivery_receipt_ref")
        delivery_receipt_id = (
            None if delivery is None else str(_pin(body, "delivery_receipt_ref")["id"])
        )
        kind = str(body["kind"])
        content_ids = set(body["content_criterion_ids"])
        effect_keys = set(body["effect_keys"])
        if (
            (kind == "CONTENT" and str(scope_document.role) != "CONTENT")
            or (kind == "PREPARATION" and str(scope_document.role) not in {"MIXED", "AGGREGATE"})
            or not content_ids.issubset(set(scope_document.content_criterion_ids))
            or not effect_keys.issubset(set(scope_document.owned_effect_keys))
        ):
            raise StoreConflict("acceptance contribution exceeds completion scope")
        for artifact_ref in body["output_artifact_refs"]:
            artifact = connection.execute(
                "SELECT mission_id,version,content_hash FROM artifacts WHERE artifact_id=?",
                (str(artifact_ref["id"]),),
            ).fetchone()
            if artifact is None or (
                str(artifact["mission_id"]),
                int(artifact["version"]),
                str(artifact["content_hash"]),
            ) != (
                mission_id,
                int(artifact_ref["revision"]),
                str(artifact_ref["content_hash"]),
            ):
                raise StoreConflict("acceptance contribution artifact identity mismatch")
        if outcome_binding_id is not None:
            outcome = connection.execute(
                "SELECT spec_hash,completion_scope_id,effect_key FROM "
                "operation_outcome_review_bindings WHERE mission_id=? AND binding_id=?",
                (mission_id, str(outcome_binding_id)),
            ).fetchone()
            if outcome is None or (
                str(outcome["spec_hash"]),
                str(outcome["completion_scope_id"]),
                str(outcome["effect_key"]),
            ) != (str(body["spec_hash"]), completion_scope_id, str(body["effect_keys"][0])):
                raise StoreConflict("acceptance contribution outcome binding mismatch")
        if delivery_receipt_id is not None:
            receipt = connection.execute(
                "SELECT acceptance_id FROM delivery_receipts WHERE mission_id=? AND receipt_id=?",
                (mission_id, delivery_receipt_id),
            ).fetchone()
            if receipt is None or str(receipt[0]) != acceptance_id:
                raise StoreConflict("acceptance contribution delivery receipt mismatch")
        timestamp = self._timestamp(
            connection,
            table="operation_acceptance_scopes",
            key_column="acceptance_id",
            key_value=acceptance_id,
            supplied=created_at_ms,
        )
        fields = (
            "acceptance_id",
            "mission_id",
            "completion_scope_id",
            "contribution_kind",
            "scope_hash",
            "contribution_hash",
            "document_json",
            "outcome_binding_id",
            "delivery_receipt_id",
            "producer_receipt_id",
            "created_at_ms",
        )
        values = (
            acceptance_id,
            mission_id,
            completion_scope_id,
            kind,
            str(scope["scope_hash"]),
            str(digest),
            encoded,
            None if outcome_binding_id is None else str(outcome_binding_id),
            delivery_receipt_id,
            str(producer_receipt_id),
            timestamp,
        )
        self._insert_exact(
            connection,
            table="operation_acceptance_scopes",
            key_where="acceptance_id=?",
            key_values=(acceptance_id,),
            fields=fields,
            values=values,
            conflict="operation acceptance scope conflict",
        )
        return self.get_acceptance_scope_exact(mission_id, acceptance_id) or {}

    def get_acceptance_scope_exact(
        self, mission_id: str, acceptance_id: str
    ) -> dict[str, Any] | None:
        _, _, _, contribution_kind = _completion_contracts()
        row = self._store.connection.execute(
            "SELECT * FROM operation_acceptance_scopes WHERE mission_id=? AND acceptance_id=?",
            (str(mission_id), str(acceptance_id)),
        ).fetchone()
        return self._decoded(row, contribution_kind, "stored_acceptance_contribution_scope")

    def list_scoped_contributions(
        self, mission_id: str, completion_scope_id: str
    ) -> tuple[dict[str, Any], ...]:
        _, _, _, contribution_kind = _completion_contracts()
        rows = self._store.connection.execute(
            "SELECT * FROM operation_acceptance_scopes WHERE mission_id=? "
            "AND completion_scope_id=? ORDER BY acceptance_id",
            (str(mission_id), str(completion_scope_id)),
        ).fetchall()
        return tuple(
            self._decoded(row, contribution_kind, "stored_acceptance_contribution_scope") or {}
            for row in rows
        )

    def retained_content_contributions(self, scope: Any) -> tuple[dict[str, Any], ...]:
        """Read original content receipts across an unchanged H4 occurrence scope.

        No Acceptance is moved or copied. Effects keep their exact original
        outcome scope and are deliberately excluded from historical content reuse.
        """
        current = self.list_scoped_contributions(scope.mission_id, scope.scope_id)
        protocol = self._store.connection.execute(
            "SELECT package_version FROM mission_planning_protocols WHERE mission_id=?",
            (scope.mission_id,),
        ).fetchone()
        if protocol is None or int(protocol[0]) < 7:
            return current
        _, scope_kind, _, _ = _completion_contracts()
        rows = self._store.connection.execute(
            "SELECT * FROM operation_completion_scopes WHERE mission_id=? AND occurrence_id=? "
            "AND task_id=? AND plan_revision<? ORDER BY plan_revision DESC",
            (scope.mission_id, scope.occurrence_id, scope.task_ref.id, scope.plan_ref.revision),
        ).fetchall()
        identity = {key: value for key, value in scope.to_json().items() if key != "plan_ref"}
        htn = HtnStore(self._store)
        def data_at(revision: int) -> list[str]:
            return sorted(canonical_json(item.to_json()) for item in htn.list_data_requirements(
                scope.mission_id, revision) if str(item.consumer_occurrence) == scope.occurrence_id)
        data = data_at(scope.plan_ref.revision)
        found = {row["document"].acceptance_id: row for row in current}
        for raw in rows:
            stored = self._decoded(raw, scope_kind, "retained_completion_scope")
            if stored is None:
                continue
            old = stored["document"]
            if {key: value for key, value in old.to_json().items() if key != "plan_ref"} != identity:
                continue
            receipt = htn.get_commit_receipt(stored["plan_receipt_id"])
            if (receipt.mission_id != scope.mission_id or receipt.new_plan_revision != old.plan_ref.revision
                    or receipt.output_identity.get("snapshot_hash") != old.plan_ref.snapshot_hash
                    or data_at(old.plan_ref.revision) != data):
                continue
            for contribution in self.list_scoped_contributions(scope.mission_id, old.scope_id):
                document = contribution["document"]
                if str(document.kind) in {"CONTENT", "PREPARATION"}:
                    found.setdefault(document.acceptance_id, contribution)
        return tuple(found[key] for key in sorted(found))

    def list_outcome_bindings_for_intent(
        self, mission_id: str, intent_id: str
    ) -> tuple[dict[str, Any], ...]:
        _, _, binding_kind, _ = _completion_contracts()
        rows = self._store.connection.execute(
            "SELECT * FROM operation_outcome_review_bindings "
            "WHERE mission_id=? AND intent_id=? ORDER BY binding_id",
            (str(mission_id), str(intent_id)),
        ).fetchall()
        return tuple(
            self._decoded(row, binding_kind, "stored_operation_outcome_binding") or {}
            for row in rows
        )

    def list_outcome_bindings_for_effect(
        self, mission_id: str, spec_id: str, effect_key: str
    ) -> tuple[dict[str, Any], ...]:
        _, _, binding_kind, _ = _completion_contracts()
        rows = self._store.connection.execute(
            "SELECT * FROM operation_outcome_review_bindings WHERE mission_id=? "
            "AND spec_id=? AND effect_key=? ORDER BY binding_id",
            (str(mission_id), str(spec_id), str(effect_key)),
        ).fetchall()
        return tuple(
            self._decoded(row, binding_kind, "stored_operation_outcome_binding") or {}
            for row in rows
        )


__all__ = ("OperationCompletionStore",)
