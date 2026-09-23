# SPDX-License-Identifier: Apache-2.0
"""The approved check-policy domain of a review purpose (pure; no store access)."""

from __future__ import annotations

from .codec import AssuranceError, fingerprint

POLICY_DOMAIN_KIND = "assurance-policy-domain-v1"


def policy_domain_hash(
    purpose: str, *, scope_hash: str | None, task_hash: str | None = None
) -> str:
    """The approved check-policy domain of a purpose.

    Scope-content purposes (TASK_CONTENT, COMPOSITION) are approved on the frozen
    completion Scope itself: their catalogue is the Scope's content projection.
    METHOD_PLAN happens before any Scope and is always approved on its planning
    subject, the exact Task contract, even when a Scope exists by the time the
    method is admitted: the approved contract's purpose domain is the licence, and
    a Scope's content policy is never borrowed for a planning judgement.
    MISSION_FINAL, ACTION_PROPOSAL and OPERATION_OUTCOME judge a different
    catalogue than the Scope's content criteria (the whole root requirements, the
    proposal checks, an effect slot), so their domain is the Scope plus the
    purpose; no Scope is invented.
    """
    if purpose in {"TASK_CONTENT", "COMPOSITION"}:
        if scope_hash is None:
            raise AssuranceError("SUBJECT_BINDING_INVALID", purpose)
        return scope_hash
    if purpose == "METHOD_PLAN":
        if task_hash is None:
            raise AssuranceError("SUBJECT_BINDING_INVALID", purpose)
        return task_hash
    if purpose in {"MISSION_FINAL", "ACTION_PROPOSAL", "OPERATION_OUTCOME"}:
        if scope_hash is None:
            raise AssuranceError("SUBJECT_BINDING_INVALID", purpose)
        return fingerprint(
            {"kind": POLICY_DOMAIN_KIND, "purpose": purpose, "scope_hash": scope_hash}
        )
    raise AssuranceError("SUBJECT_BINDING_INVALID", purpose)


__all__ = ("POLICY_DOMAIN_KIND", "policy_domain_hash")
