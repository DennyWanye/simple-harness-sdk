# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit authenticated confirmation of a normalized completion specification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts import ContractError
from ..contracts.operation_completion import OperationCompletionRequirementsV1
from ..contracts.semantic_base import TypedRef, TypedRefKind, content_hash_of, fields_of, identifier
from ..governance.permissions import Principal
from ..orchestrator.operation_completion import (
    ApprovedRequirementAuthority,
    CompletionSpecReceipt,
    OperationCompletionError,
    approval_body,
)


def bind_requirement_authority(
    *,
    principal: Principal,
    tenant_id: str,
    command_id: str,
    mission_id: str,
    requirements_ref: TypedRef,
    normalized_spec: OperationCompletionRequirementsV1,
) -> ApprovedRequirementAuthority:
    """Bind this explicit API confirmation; no wire authority/approved field exists.

    Registered structured-input policy approvals must have their own actual policy
    and decision receipts. This confirmation path does not fabricate that source.
    """
    if not isinstance(principal, Principal):
        raise ContractError("completion confirmation needs an authenticated Principal")
    if principal.kind != "human":
        raise OperationCompletionError(
            "OP_REQUIREMENT_MAPPING_UNAPPROVED", "user confirmation requires a human caller"
        )
    return ApprovedRequirementAuthority(
        kind="USER_CONFIRMED",
        tenant_id=identifier(tenant_id, "tenant_id"),
        issuer_id=principal.principal_id,
        command_id=command_id,
        command_body_hash=content_hash_of(
            approval_body(mission_id, command_id, requirements_ref, normalized_spec)
        ),
        requirements_ref=requirements_ref,
        normalized_spec_hash=normalized_spec.content_hash(),
        policy_ref=None,
        check_receipt_refs=(),
    )


class OperationCompletionApi:
    def __init__(self, commit: Any, *, tenant_id: str, principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise ContractError("completion approval needs an authenticated Principal")
        if principal.kind != "human":
            raise OperationCompletionError(
                "OP_REQUIREMENT_MAPPING_UNAPPROVED", "user confirmation requires a human caller"
            )
        self._commit = commit
        self._tenant = identifier(tenant_id, "tenant_id")
        self._principal = principal

    def approve(self, command: Mapping[str, Any]) -> CompletionSpecReceipt:
        value = fields_of(
            command,
            "completion confirmation",
            required=(
                "mission_id",
                "command_id",
                "expected_requirements_ref",
                "proposal",
            ),
        )
        mission_id = identifier(value["mission_id"], "mission_id")
        command_id = identifier(value["command_id"], "command_id")
        mission = self._commit.store.get_mission(mission_id)
        if mission is None or mission.tenant_id != self._tenant:
            raise OperationCompletionError("not_found", "no such mission for this caller")
        ref = TypedRef.from_json(value["expected_requirements_ref"])
        if ref.kind is not TypedRefKind.REQUIREMENTS:
            raise OperationCompletionError("OP_REF_KIND_UNSUPPORTED", "expected requirements kind")
        proposal = OperationCompletionRequirementsV1.from_json(value["proposal"])
        authority = bind_requirement_authority(
            principal=self._principal,
            tenant_id=self._tenant,
            command_id=command_id,
            mission_id=mission_id,
            requirements_ref=ref,
            normalized_spec=proposal,
        )
        return self._commit.approve_operation_completion_spec(
            mission_id=mission_id,
            command_id=command_id,
            expected_requirements_ref=ref,
            proposal=proposal,
            requirement_authority=authority,
            principal=self._principal,
        )
