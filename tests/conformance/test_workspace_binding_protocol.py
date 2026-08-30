# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import simple_harness
import simple_harness.runtime as runtime
from simple_harness.runtime.workspace_binding_protocol import (
    EMPTY_WORKSPACE_BINDING_ROOT_SET_DIGEST,
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
    workspace_binding_root_set_digest,
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


def _binding_set_receipt(
    proposal: WorkspaceBindingProposal,
    grant: WorkspaceBindingAuthorityGrant,
    *,
    parent: WorkspaceBindingSetReceipt | None,
    receipt_id: str,
    binding_id: str = "binding-1",
) -> WorkspaceBindingSetReceipt:
    previous_hashes = () if parent is None else parent.root_identity_hashes
    root_hashes = tuple(sorted((*previous_hashes, proposal.root.root_identity_hash)))
    return WorkspaceBindingSetReceipt(
        receipt_id=receipt_id,
        binding_id=binding_id,
        task_scope_id=proposal.task_scope_id,
        base_binding_set_revision=proposal.base_binding_set_revision,
        binding_set_revision=proposal.base_binding_set_revision + 1,
        parent_receipt_id=None if parent is None else parent.receipt_id,
        parent_receipt_hash=None if parent is None else parent.receipt_hash,
        previous_root_set_digest=(
            EMPTY_WORKSPACE_BINDING_ROOT_SET_DIGEST
            if parent is None
            else parent.root_set_digest
        ),
        root_set_digest=workspace_binding_root_set_digest(root_hashes),
        root_identity_hashes=root_hashes,
        appended_root=proposal.root,
        grant_id=grant.grant_id,
        grant_hash=grant.grant_hash,
        host_receipt_ref=f"host-{receipt_id}",
        host_receipt_hash="4" * 64,
    )


def test_workspace_binding_protocol_is_on_both_official_public_surfaces() -> None:
    names = (
        "EMPTY_WORKSPACE_BINDING_ROOT_SET_DIGEST",
        "FilesystemIdentity",
        "CanonicalWorkspaceRoot",
        "WorkspaceBindingProposal",
        "ManualWorkspaceBindingChallenge",
        "ManualWorkspaceBindingAuthorizationReceipt",
        "HostIssuedRunBindingModeSnapshot",
        "WorkspaceBindingAuthorityGrant",
        "WorkspaceBindingSetReceipt",
        "WorkspaceBindingAuthorityPort",
        "workspace_binding_root_set_digest",
    )
    for name in names:
        assert name in simple_harness.__all__
        assert name in runtime.__all__
        assert getattr(simple_harness, name) is getattr(runtime, name)


def test_exact_decoders_roundtrip_and_reject_extra_fields() -> None:
    proposal = _proposal(base_binding_set_revision=0)
    challenge = _challenge(proposal)
    receipt = _receipt(challenge)
    request = _mode_request()
    snapshot = _mode_snapshot(request)
    grant = _grant(proposal)
    binding_set = _binding_set_receipt(
        proposal, grant, parent=None, receipt_id="binding-set-1"
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
    first_proposal = _proposal(base_binding_set_revision=0)
    first_grant = _grant(first_proposal)
    parent = _binding_set_receipt(
        first_proposal, first_grant, parent=None, receipt_id="binding-set-1"
    )
    parent.verify_parent_and_grant(None, first_grant)

    proposal = _proposal(
        proposal_id="proposal-2",
        root=_root(root_id="root-2", object_id="inode-10"),
        base_binding_set_revision=1,
        idempotency_key="append-2",
    )
    grant = replace(_grant(proposal), grant_id="grant-2", host_grant_ref="host-grant-2")
    grant.verify_proposal(proposal)
    receipt = _binding_set_receipt(
        proposal, grant, parent=parent, receipt_id="binding-set-2"
    )
    receipt.verify_parent_and_grant(parent, grant)
    with pytest.raises(ValueError, match="advance base revision"):
        replace(receipt, binding_set_revision=5)
    with pytest.raises(ValueError, match="differs from grant"):
        receipt.verify_grant(_grant(_proposal(task_scope_id="task-2")))
    with pytest.raises(ValueError, match="differs from exact parent"):
        receipt.verify_parent_and_grant(replace(parent, binding_id="other-binding"), grant)
    arbitrary_hashes = (proposal.root.root_identity_hash,)
    with pytest.raises(ValueError, match="exact parent union"):
        replace(
            receipt,
            root_identity_hashes=arbitrary_hashes,
            root_set_digest=workspace_binding_root_set_digest(arbitrary_hashes),
        ).verify_parent_and_grant(parent, grant)
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
    receipt = _binding_set_receipt(
        proposal, grant, parent=None, receipt_id="binding-set-1"
    )
    receipt.verify_parent_and_grant(None, grant)
    assert WorkspaceBindingSetReceipt.from_json(receipt.to_json()) == receipt
    with pytest.raises(ValueError, match="must not claim a parent"):
        replace(receipt, parent_receipt_id="binding-set-0", parent_receipt_hash="3" * 64)
    with pytest.raises(ValueError, match="empty parent set"):
        replace(receipt, previous_root_set_digest="0" * 64)
    with pytest.raises(ValueError, match="exactly the appended root"):
        extra = tuple(sorted((receipt.appended_root.root_identity_hash, "a" * 64)))
        replace(
            receipt,
            root_identity_hashes=extra,
            root_set_digest=workspace_binding_root_set_digest(extra),
        )


def test_non_genesis_append_requires_exact_parent_receipt_pair() -> None:
    first = _proposal(base_binding_set_revision=0)
    first_grant = _grant(first)
    parent = _binding_set_receipt(first, first_grant, parent=None, receipt_id="binding-set-1")
    proposal = _proposal(
        proposal_id="proposal-2",
        root=_root(root_id="root-2", object_id="inode-10"),
        base_binding_set_revision=1,
        idempotency_key="append-2",
    )
    grant = replace(_grant(proposal), grant_id="grant-2")
    receipt = _binding_set_receipt(proposal, grant, parent=parent, receipt_id="binding-set-2")
    with pytest.raises(ValueError, match="must identify its parent"):
        replace(receipt, parent_receipt_hash=None)
    with pytest.raises(ValueError, match="must be unique"):
        replay_proposal = _proposal(
            proposal_id="proposal-replay",
            root=first.root,
            base_binding_set_revision=1,
            idempotency_key="append-replay",
        )
        replay_grant = replace(_grant(replay_proposal), grant_id="grant-replay")
        replay = _binding_set_receipt(
            replay_proposal, replay_grant, parent=parent, receipt_id="binding-set-replay"
        )
        replay.verify_parent_and_grant(parent, replay_grant)


def test_real_filesystem_identity_probe_proves_each_exact_append(tmp_path: Path) -> None:
    parent: WorkspaceBindingSetReceipt | None = None
    for index in range(3):
        directory = tmp_path / f"root-{index}"
        directory.mkdir()
        stat = directory.stat()
        root = CanonicalWorkspaceRoot(
            f"root-{index}",
            str(directory.resolve()),
            FilesystemIdentity(
                FilesystemIdentityKind.POSIX_INODE,
                str(stat.st_dev),
                str(stat.st_ino),
            ),
        )
        proposal = _proposal(
            proposal_id=f"proposal-{index}",
            root=root,
            base_binding_set_revision=index,
            idempotency_key=f"append-{index}",
        )
        grant = replace(
            _grant(proposal),
            grant_id=f"grant-{index}",
            host_grant_ref=f"host-grant-{index}",
        )
        current = _binding_set_receipt(
            proposal,
            grant,
            parent=parent,
            receipt_id=f"binding-set-{index + 1}",
        )
        decoded = WorkspaceBindingSetReceipt.from_json(current.to_json())
        decoded.verify_parent_and_grant(parent, grant)
        prior_hashes = () if parent is None else parent.root_identity_hashes
        assert decoded.root_identity_hashes == tuple(
            sorted((*prior_hashes, root.root_identity_hash))
        )
        parent = decoded


class DurableFakeAuthority:
    """In-memory durable lookup/consume fixture, not DTO self-authentication."""

    def __init__(
        self,
        challenge: ManualWorkspaceBindingChallenge,
        receipt: ManualWorkspaceBindingAuthorizationReceipt,
        manual_grant: WorkspaceBindingAuthorityGrant,
        *,
        mode_request: RunBindingModeSnapshotRequest | None = None,
        mode_snapshot: HostIssuedRunBindingModeSnapshot | None = None,
        auto_proposal: WorkspaceBindingProposal | None = None,
        auto_grant: WorkspaceBindingAuthorityGrant | None = None,
        now_millis: int = 1500,
    ) -> None:
        self.challenge = challenge
        self.receipt = receipt
        self.manual_grant = manual_grant
        self.mode_request = mode_request
        self.mode_snapshot = mode_snapshot
        self.auto_proposal = auto_proposal
        self.auto_grant = auto_grant
        self.now_millis = now_millis
        self.consumed_nonces: dict[str, tuple[str, str, str]] = {}
        self.issued_snapshot_hashes: set[str] = set()
        self.grants = {manual_grant.grant_id: manual_grant}
        if auto_grant is not None:
            self.grants[auto_grant.grant_id] = auto_grant

    async def verify_manual_authorization(
        self,
        proposal: WorkspaceBindingProposal,
        challenge: ManualWorkspaceBindingChallenge,
        receipt: ManualWorkspaceBindingAuthorizationReceipt,
    ) -> WorkspaceBindingAuthorityGrant:
        consume_payload = (proposal.proposal_hash, challenge.challenge_hash, receipt.receipt_hash)
        consumed = self.consumed_nonces.get(challenge.authorization_nonce)
        if consumed is not None:
            if consumed != consume_payload:
                raise ValueError("manual authorization nonce replay payload conflicts")
            return self.manual_grant
        if challenge != self.challenge or receipt != self.receipt:
            raise ValueError("durable Host authorization record differs")
        challenge.verify_proposal(proposal)
        receipt.verify_challenge(challenge)
        self.manual_grant.verify_proposal(proposal)
        self.consumed_nonces[challenge.authorization_nonce] = consume_payload
        return self.manual_grant

    async def issue_run_binding_mode_snapshot(
        self, request: RunBindingModeSnapshotRequest
    ) -> HostIssuedRunBindingModeSnapshot:
        if request != self.mode_request or self.mode_snapshot is None:
            raise ValueError("durable Host Run binding-mode request differs")
        self.mode_snapshot.verify_request(request, now_millis=self.now_millis)
        self.issued_snapshot_hashes.add(self.mode_snapshot.snapshot_hash)
        return self.mode_snapshot

    async def authorize_auto_binding(
        self,
        proposal: WorkspaceBindingProposal,
        snapshot: HostIssuedRunBindingModeSnapshot,
    ) -> WorkspaceBindingAuthorityGrant:
        if (
            self.mode_request is None
            or self.mode_snapshot is None
            or self.auto_proposal is None
            or self.auto_grant is None
            or snapshot != self.mode_snapshot
            or snapshot.snapshot_hash not in self.issued_snapshot_hashes
        ):
            raise ValueError("durable Host Auto snapshot differs or was not issued")
        snapshot.verify_request(self.mode_request, now_millis=self.now_millis)
        if (
            proposal.run_id != snapshot.run_id
            or proposal.subject != snapshot.subject
            or proposal.task_scope_id != snapshot.task_scope_id
            or proposal.base_binding_set_revision != snapshot.binding_set_revision
        ):
            raise ValueError("Auto proposal differs from frozen Run binding revision")
        if proposal != self.auto_proposal:
            raise ValueError("durable Host Auto proposal differs")
        configured = snapshot.configured_workspace_root.canonical_path.rstrip("/") + "/"
        if not proposal.root.canonical_path.startswith(configured):
            raise ValueError("Auto proposal root is not a strict configured-root descendant")
        if self.auto_grant.source is not WorkspaceBindingGrantSource.AUTO:
            raise ValueError("durable Host Auto grant has wrong source")
        self.auto_grant.verify_proposal(proposal)
        return self.auto_grant

    async def verify_binding_grant(
        self, proposal: WorkspaceBindingProposal, grant: WorkspaceBindingAuthorityGrant
    ) -> None:
        if self.grants.get(grant.grant_id) != grant:
            raise ValueError("durable Host grant differs")
        grant.verify_proposal(proposal)


def test_fully_self_consistent_forged_receipt_and_grant_are_not_authority() -> None:
    proposal = _proposal()
    challenge = _challenge(proposal)
    receipt = _receipt(challenge)
    grant = _grant(proposal)
    authority = DurableFakeAuthority(challenge, receipt, grant)
    assert asyncio.run(authority.verify_manual_authorization(proposal, challenge, receipt)) == grant
    assert asyncio.run(authority.verify_manual_authorization(proposal, challenge, receipt)) == grant
    forged_receipt = replace(receipt, host_receipt_ref="model-forged-host-record")
    with pytest.raises(ValueError, match="nonce replay payload conflicts"):
        asyncio.run(authority.verify_manual_authorization(proposal, challenge, forged_receipt))
    fresh_authority = DurableFakeAuthority(challenge, receipt, grant)
    with pytest.raises(ValueError, match="durable Host authorization"):
        asyncio.run(
            fresh_authority.verify_manual_authorization(
                proposal,
                challenge,
                replace(receipt, host_receipt_ref="model-forged-host-record"),
            )
        )
    forged_grant = replace(grant, host_grant_ref="model-forged-grant")
    with pytest.raises(ValueError, match="durable Host grant"):
        asyncio.run(authority.verify_binding_grant(proposal, forged_grant))


def test_durable_fake_auto_issue_authorize_and_verify_fail_closed() -> None:
    manual_proposal = _proposal()
    challenge = _challenge(manual_proposal)
    receipt = _receipt(challenge)
    manual_grant = _grant(manual_proposal)
    request = _mode_request()
    snapshot = _mode_snapshot(request)
    auto_proposal = _proposal(
        proposal_id="proposal-auto",
        root=CanonicalWorkspaceRoot(
            "auto-child",
            "/workspace/configured/child",
            FilesystemIdentity(FilesystemIdentityKind.POSIX_INODE, "device-2", "inode-auto"),
        ),
        idempotency_key="append-auto",
    )
    auto_grant = replace(
        _grant(
            auto_proposal,
            source=WorkspaceBindingGrantSource.AUTO,
            authority_ref=snapshot.authority_receipt_ref,
            authority_hash=snapshot.authority_receipt_hash,
        ),
        grant_id="grant-auto",
        host_grant_ref="host-grant-auto",
    )
    authority = DurableFakeAuthority(
        challenge,
        receipt,
        manual_grant,
        mode_request=request,
        mode_snapshot=snapshot,
        auto_proposal=auto_proposal,
        auto_grant=auto_grant,
    )
    issued = asyncio.run(authority.issue_run_binding_mode_snapshot(request))
    assert asyncio.run(authority.authorize_auto_binding(auto_proposal, issued)) == auto_grant
    asyncio.run(authority.verify_binding_grant(auto_proposal, auto_grant))

    forged = replace(snapshot, authority_receipt_ref="model-forged-snapshot")
    with pytest.raises(ValueError, match="snapshot differs"):
        asyncio.run(authority.authorize_auto_binding(auto_proposal, forged))
    with pytest.raises(ValueError, match="request differs"):
        asyncio.run(authority.issue_run_binding_mode_snapshot(_mode_request(run_revision=6)))
    with pytest.raises(ValueError, match="frozen Run binding revision"):
        asyncio.run(
            authority.authorize_auto_binding(
                replace(auto_proposal, base_binding_set_revision=4), issued
            )
        )

    manual_mode_authority = DurableFakeAuthority(
        challenge,
        receipt,
        manual_grant,
        mode_request=request,
        mode_snapshot=_mode_snapshot(request, mode=WorkspaceBindingMode.MANUAL),
    )
    with pytest.raises(ValueError, match="does not authorize Auto"):
        asyncio.run(manual_mode_authority.issue_run_binding_mode_snapshot(request))
    expired_authority = DurableFakeAuthority(
        challenge,
        receipt,
        manual_grant,
        mode_request=request,
        mode_snapshot=snapshot,
        now_millis=2000,
    )
    with pytest.raises(ValueError, match="not currently valid"):
        asyncio.run(expired_authority.issue_run_binding_mode_snapshot(request))
