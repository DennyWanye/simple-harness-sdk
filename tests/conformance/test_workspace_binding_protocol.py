# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import simple_harness
import simple_harness.runtime as runtime
from simple_harness.runtime.workspace_binding_protocol import (
    CanonicalWorkspaceRoot,
    FilesystemIdentity,
    FilesystemIdentityKind,
    HostIssuedRunBindingModeSnapshot,
    ManualWorkspaceBindingAuthorizationReceipt,
    ManualWorkspaceBindingChallenge,
    RunBindingModeSnapshotRequest,
    WorkspaceBindingAuthorityGrant,
    WorkspaceBindingAuthorizationChannel,
    WorkspaceBindingAuthorizationDecision,
    WorkspaceBindingGrantSource,
    WorkspaceBindingMode,
    WorkspaceBindingProposal,
    WorkspaceBindingSetReceipt,
)


def _root(*, root_id: str = "root-1", object_id: str = "inode-9") -> CanonicalWorkspaceRoot:
    return CanonicalWorkspaceRoot(
        root_id,
        f"/workspace/{root_id}",
        FilesystemIdentity(FilesystemIdentityKind.POSIX_INODE, "device-2", object_id),
    )


def _proposal(**changes: object) -> WorkspaceBindingProposal:
    values: dict[str, object] = {
        "proposal_id": "proposal-1",
        "run_id": "run-1",
        "subject": "actor-1",
        "task_scope_id": "task-1",
        "root": _root(),
        "base_binding_set_revision": 3,
        "idempotency_key": "append-1",
    }
    values.update(changes)
    return WorkspaceBindingProposal(**values)  # type: ignore[arg-type]


def _challenge(proposal: WorkspaceBindingProposal | None = None) -> ManualWorkspaceBindingChallenge:
    proposal = proposal or _proposal()
    return ManualWorkspaceBindingChallenge(
        "challenge-1",
        proposal.proposal_id,
        proposal.proposal_hash,
        proposal.run_id,
        proposal.subject,
        proposal.task_scope_id,
        proposal.root,
        proposal.base_binding_set_revision,
        "nonce-1",
        WorkspaceBindingAuthorizationChannel.USER_CONFIRMATION,
        "evidence-1",
        "a" * 64,
        "interaction-1",
        1000,
        1100,
        2000,
        "host-challenge-1",
        "b" * 64,
    )


def _receipt(
    challenge: ManualWorkspaceBindingChallenge | None = None,
    *,
    decision: WorkspaceBindingAuthorizationDecision = WorkspaceBindingAuthorizationDecision.ALLOW,
    decided_at_millis: int = 1200,
) -> ManualWorkspaceBindingAuthorizationReceipt:
    challenge = challenge or _challenge()
    return ManualWorkspaceBindingAuthorizationReceipt(
        "decision-1",
        challenge.challenge_id,
        challenge.sdk_challenge_hash,
        challenge.proposal_id,
        challenge.proposal_hash,
        challenge.run_id,
        challenge.subject,
        challenge.task_scope_id,
        challenge.root,
        challenge.base_binding_set_revision,
        challenge.authorization_nonce,
        challenge.authorization_channel,
        "actor-1",
        challenge.authorization_evidence_id,
        challenge.authorization_evidence_hash,
        challenge.interaction_event_id,
        challenge.issued_at_millis,
        challenge.not_before_millis,
        challenge.expires_at_millis,
        decision,
        decided_at_millis,
        "host-decision-1",
        "c" * 64,
        challenge.sdk_challenge_hash,
    )


def _mode_request(**changes: object) -> RunBindingModeSnapshotRequest:
    values: dict[str, object] = {
        "request_id": "mode-request-1",
        "run_id": "run-1",
        "subject": "actor-1",
        "run_revision": 5,
        "task_scope_id": "task-1",
        "binding_set_revision": 3,
        "context_snapshot_id": "context-2",
        "context_snapshot_revision": 2,
        "context_snapshot_hash": "d" * 64,
        "configured_workspace_root": _root(root_id="configured", object_id="inode-1"),
        "configuration_revision": 7,
    }
    values.update(changes)
    return RunBindingModeSnapshotRequest(**values)  # type: ignore[arg-type]


def _mode_snapshot(
    request: RunBindingModeSnapshotRequest | None = None,
    *,
    mode: WorkspaceBindingMode = WorkspaceBindingMode.AUTO,
) -> HostIssuedRunBindingModeSnapshot:
    request = request or _mode_request()
    return HostIssuedRunBindingModeSnapshot(
        "mode-snapshot-1",
        request.request_id,
        request.request_hash,
        request.run_id,
        request.subject,
        request.run_revision,
        request.task_scope_id,
        request.binding_set_revision,
        request.context_snapshot_id,
        request.context_snapshot_revision,
        request.context_snapshot_hash,
        request.configured_workspace_root,
        request.configuration_revision,
        mode,
        1000,
        2000,
        "host-mode-1",
        "e" * 64,
    )


def _grant(
    proposal: WorkspaceBindingProposal | None = None,
    *,
    source: WorkspaceBindingGrantSource = WorkspaceBindingGrantSource.MANUAL,
    authority_ref: str = "host-decision-1",
    authority_hash: str = "c" * 64,
) -> WorkspaceBindingAuthorityGrant:
    proposal = proposal or _proposal()
    return WorkspaceBindingAuthorityGrant(
        "grant-1",
        source,
        proposal.proposal_id,
        proposal.proposal_hash,
        proposal.run_id,
        proposal.subject,
        proposal.task_scope_id,
        proposal.root,
        proposal.base_binding_set_revision,
        authority_ref,
        authority_hash,
        "host-grant-1",
        "f" * 64,
    )


def test_workspace_binding_protocol_is_on_both_official_public_surfaces() -> None:
    names = (
        "FilesystemIdentity",
        "CanonicalWorkspaceRoot",
        "WorkspaceBindingProposal",
        "ManualWorkspaceBindingChallenge",
        "ManualWorkspaceBindingAuthorizationReceipt",
        "HostIssuedRunBindingModeSnapshot",
        "WorkspaceBindingAuthorityGrant",
        "WorkspaceBindingSetReceipt",
        "WorkspaceBindingAuthorityPort",
    )
    for name in names:
        assert name in simple_harness.__all__
        assert name in runtime.__all__
        assert getattr(simple_harness, name) is getattr(runtime, name)


def test_exact_decoders_roundtrip_and_reject_extra_fields() -> None:
    proposal = _proposal()
    challenge = _challenge(proposal)
    receipt = _receipt(challenge)
    request = _mode_request()
    snapshot = _mode_snapshot(request)
    grant = _grant(proposal)
    binding_set = WorkspaceBindingSetReceipt(
        "binding-set-4",
        "binding-4",
        proposal.task_scope_id,
        3,
        4,
        "binding-set-3",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        proposal.root,
        grant.grant_id,
        grant.grant_hash,
        "host-binding-set-4",
        "4" * 64,
    )
    pairs = (
        (FilesystemIdentity, proposal.root.filesystem_identity),
        (CanonicalWorkspaceRoot, proposal.root),
        (WorkspaceBindingProposal, proposal),
        (ManualWorkspaceBindingChallenge, challenge),
        (ManualWorkspaceBindingAuthorizationReceipt, receipt),
        (RunBindingModeSnapshotRequest, request),
        (HostIssuedRunBindingModeSnapshot, snapshot),
        (WorkspaceBindingAuthorityGrant, grant),
        (WorkspaceBindingSetReceipt, binding_set),
    )
    for decoder, value in pairs:
        payload = value.to_json()
        assert decoder.from_json(payload) == value
        payload["model_metadata"] = {"binding_mode": "auto"}
        with pytest.raises(ValueError, match="extra"):
            decoder.from_json(payload)


def test_manual_exact_linkage_time_deny_and_self_consistent_forgery_fail() -> None:
    proposal = _proposal()
    challenge = _challenge(proposal)
    receipt = _receipt(challenge)
    challenge.verify_proposal(proposal)
    receipt.verify_challenge(challenge)

    changed_proposal = _proposal(root=_root(object_id="inode-replaced"))
    with pytest.raises(ValueError, match="differs from proposal"):
        challenge.verify_proposal(changed_proposal)
    forged = replace(receipt, authorization_nonce="model-chosen-nonce")
    assert forged.receipt_hash != receipt.receipt_hash
    with pytest.raises(ValueError, match="differs from challenge"):
        forged.verify_challenge(challenge)
    with pytest.raises(ValueError, match="validity interval"):
        _receipt(challenge, decided_at_millis=2100).verify_challenge(challenge)
    with pytest.raises(ValueError, match="does not authorize"):
        _receipt(challenge, decision=WorkspaceBindingAuthorizationDecision.DENY).verify_challenge(
            challenge
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"run_id": "run-2"},
        {"subject": "actor-2"},
        {"task_scope_id": "task-2"},
        {"root": _root(object_id="inode-replaced")},
        {"base_binding_set_revision": 4},
        {"authorization_nonce": "nonce-2"},
        {"authorization_channel": WorkspaceBindingAuthorizationChannel.PROJECT_PICKER},
        {"authorization_evidence_id": "evidence-2"},
        {"authorization_evidence_hash": "8" * 64},
        {"interaction_event_id": "interaction-2"},
        {"challenge_issued_at_millis": 999},
        {"challenge_not_before_millis": 1200},
        {"challenge_expires_at_millis": 1900},
    ),
)
def test_manual_self_consistent_wrong_exact_field_is_rejected(
    changes: dict[str, object],
) -> None:
    challenge = _challenge()
    receipt = replace(_receipt(challenge), **changes)
    with pytest.raises(ValueError, match="differs from challenge"):
        receipt.verify_challenge(challenge)


def test_auto_mode_is_host_snapshot_only_and_binds_run_context_config_and_time() -> None:
    request = _mode_request()
    snapshot = _mode_snapshot(request)
    assert "mode" not in _proposal().to_json()
    snapshot.verify_request(request, now_millis=1500)
    with pytest.raises(ValueError, match="does not authorize Auto"):
        _mode_snapshot(request, mode=WorkspaceBindingMode.MANUAL).verify_request(
            request, now_millis=1500
        )
    with pytest.raises(ValueError, match="not currently valid"):
        snapshot.verify_request(request, now_millis=2000)
    for changed in (
        {"run_id": "run-2"},
        {"subject": "actor-2"},
        {"run_revision": 6},
        {"task_scope_id": "task-2"},
        {"binding_set_revision": 4},
        {"context_snapshot_revision": 3},
        {"context_snapshot_hash": "9" * 64},
        {"configuration_revision": 8},
        {"configured_workspace_root": _root(root_id="other")},
    ):
        with pytest.raises(ValueError, match="differs from request"):
            snapshot.verify_request(_mode_request(**changed), now_millis=1500)

    genesis_request = _mode_request(binding_set_revision=0)
    _mode_snapshot(genesis_request).verify_request(genesis_request, now_millis=1500)


def test_grant_and_binding_set_receipt_close_exact_append_lineage() -> None:
    proposal = _proposal()
    grant = _grant(proposal)
    grant.verify_proposal(proposal)
    receipt = WorkspaceBindingSetReceipt(
        "binding-set-4",
        "binding-4",
        proposal.task_scope_id,
        3,
        4,
        "binding-set-3",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        proposal.root,
        grant.grant_id,
        grant.grant_hash,
        "host-binding-set-4",
        "4" * 64,
    )
    receipt.verify_grant(grant)
    with pytest.raises(ValueError, match="advance base revision"):
        replace(receipt, binding_set_revision=5)
    with pytest.raises(ValueError, match="differs from grant"):
        receipt.verify_grant(_grant(_proposal(task_scope_id="task-2")))
    hashes = {
        proposal.root.filesystem_identity.identity_hash,
        proposal.root.path_hash,
        proposal.root.root_identity_hash,
        proposal.proposal_hash,
        grant.grant_hash,
        receipt.receipt_hash,
    }
    assert len(hashes) == 6


def test_genesis_append_requires_base_zero_and_no_parent_receipt() -> None:
    proposal = _proposal(base_binding_set_revision=0)
    grant = _grant(proposal)
    receipt = WorkspaceBindingSetReceipt(
        "binding-set-1",
        "binding-1",
        proposal.task_scope_id,
        0,
        1,
        None,
        None,
        "0" * 64,
        "1" * 64,
        proposal.root,
        grant.grant_id,
        grant.grant_hash,
        "host-binding-set-1",
        "2" * 64,
    )
    receipt.verify_grant(grant)
    assert WorkspaceBindingSetReceipt.from_json(receipt.to_json()) == receipt
    with pytest.raises(ValueError, match="must not claim a parent"):
        replace(receipt, parent_receipt_id="binding-set-0", parent_receipt_hash="3" * 64)


def test_non_genesis_append_requires_exact_parent_receipt_pair() -> None:
    proposal = _proposal()
    grant = _grant(proposal)
    receipt = WorkspaceBindingSetReceipt(
        "binding-set-4",
        "binding-4",
        proposal.task_scope_id,
        3,
        4,
        "binding-set-3",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        proposal.root,
        grant.grant_id,
        grant.grant_hash,
        "host-binding-set-4",
        "4" * 64,
    )
    with pytest.raises(ValueError, match="must identify its parent"):
        replace(receipt, parent_receipt_hash=None)


class DurableFakeAuthority:
    """Consumer fixture: public record self-consistency is not Host authenticity."""

    def __init__(
        self,
        challenge: ManualWorkspaceBindingChallenge,
        receipt: ManualWorkspaceBindingAuthorizationReceipt,
        grant: WorkspaceBindingAuthorityGrant,
    ) -> None:
        self.challenge = challenge
        self.receipt = receipt
        self.grant = grant

    async def verify_manual_authorization(
        self,
        proposal: WorkspaceBindingProposal,
        challenge: ManualWorkspaceBindingChallenge,
        receipt: ManualWorkspaceBindingAuthorizationReceipt,
    ) -> WorkspaceBindingAuthorityGrant:
        if challenge != self.challenge or receipt != self.receipt:
            raise ValueError("durable Host authorization record differs")
        challenge.verify_proposal(proposal)
        receipt.verify_challenge(challenge)
        return self.grant

    async def issue_run_binding_mode_snapshot(
        self, request: RunBindingModeSnapshotRequest
    ) -> HostIssuedRunBindingModeSnapshot:
        raise NotImplementedError(request)

    async def authorize_auto_binding(
        self,
        proposal: WorkspaceBindingProposal,
        snapshot: HostIssuedRunBindingModeSnapshot,
    ) -> WorkspaceBindingAuthorityGrant:
        raise NotImplementedError(proposal, snapshot)

    async def verify_binding_grant(
        self, proposal: WorkspaceBindingProposal, grant: WorkspaceBindingAuthorityGrant
    ) -> None:
        if grant != self.grant:
            raise ValueError("durable Host grant differs")
        grant.verify_proposal(proposal)


def test_fully_self_consistent_forged_receipt_and_grant_are_not_authority() -> None:
    proposal = _proposal()
    challenge = _challenge(proposal)
    receipt = _receipt(challenge)
    grant = _grant(proposal)
    authority = DurableFakeAuthority(challenge, receipt, grant)
    assert asyncio.run(authority.verify_manual_authorization(proposal, challenge, receipt)) == grant
    forged_receipt = replace(receipt, host_receipt_ref="model-forged-host-record")
    with pytest.raises(ValueError, match="durable Host authorization"):
        asyncio.run(authority.verify_manual_authorization(proposal, challenge, forged_receipt))
    forged_grant = replace(grant, host_grant_ref="model-forged-grant")
    with pytest.raises(ValueError, match="durable Host grant"):
        asyncio.run(authority.verify_binding_grant(proposal, forged_grant))
