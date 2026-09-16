# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Which subject a :class:`ValidityWitness` was issued for (plan §18.1 ``knowledge/``).

AER §8.1 puts ``support_selection`` inside a witness's own identity: a licence says
"this consumer may proceed, now, **over this support**".  Two supports are two
licences.  The library keeps that dimension in the storage-derived
``validity_witnesses.subject_digest`` column (migration 18), and this module is the
one place that says what belongs in it.

Two lanes issue START licences today and they are licences over different things:

``acceptance:<acceptance_id>``
    the DATA lane — "you may consume *this accepted output*".  The witness names the
    acceptance in its ``support_refs``, which is how §11.5 requires a witness to say
    what it was taken over.

``conditions:<digest>``
    the START-precondition lane — "the preconditions of the method that adopted you
    were re-evaluated and hold".  A condition is not a stored object and has no
    :class:`TypedRefKind`, so the conditions travel as ``condition:<digest>`` reason
    codes and the subject is the digest of the whole (sorted) set.  One licence
    covers all of a consumer's preconditions, so one subject covers them too.

A witness that names neither — the ACCEPT lane before part 2d gave it a support ref,
and any legacy row — has the empty subject.  That is the value migration 18 back-fills,
so "no subject declared" keeps behaving exactly as it did under migration 16's key.

:func:`witness_subject` is a **pure function of the witness**.  It exists so the row
can be recomputed and audited (§16.1): the issuer declares the subject at insert time
and :meth:`HtnStore.insert_validity_witness` refuses any declaration this function
does not agree with.  Declaration plus recomputation, never one-sided sniffing — a
store that guessed the subject out of the row could be made to file two different
licences under one key again, which is the defect this module exists to close.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from ..contracts.evidence_state import ValidityWitness
from ..contracts.semantic_base import TypedRefKind

#: Reason-code prefix that carries **which precondition** a START licence was taken
#: for.  ``orchestrator.hierarchical_dispatch`` writes them; this module reads them.
CONDITION_REASON_PREFIX = "condition:"

#: Subject prefix for the DATA lane: the accepted output being licensed.
ACCEPTANCE_SUBJECT = "acceptance:"

#: Subject prefix for the precondition lane: the set of conditions being licensed.
CONDITION_SUBJECT = "conditions:"

#: The subject of a licence that names no subject at all.  Migration 18 back-fills
#: every pre-existing row with it, so those rows keep migration 16's key exactly.
NO_SUBJECT = ""


def condition_digests(witness: ValidityWitness) -> tuple[str, ...]:
    """The precondition digests this witness names, sorted and de-duplicated."""

    return tuple(
        sorted(
            {
                code[len(CONDITION_REASON_PREFIX) :]
                for code in witness.reason_codes
                if code.startswith(CONDITION_REASON_PREFIX)
            }
        )
    )


def acceptance_subjects(witness: ValidityWitness) -> tuple[str, ...]:
    """The acceptances this witness names in its support, sorted and de-duplicated."""

    return tuple(
        sorted(
            {
                str(reference.id)
                for reference in witness.support_refs
                if reference.kind is TypedRefKind.ACCEPTANCE
            }
        )
    )


def witness_subject(witness: ValidityWitness) -> str:
    """The subject this witness licenses, recomputed from the witness alone.

    Precondition digests win over an acceptance support ref: a licence that
    re-evaluated conditions *is* the precondition lane's licence even if one of the
    conditions happened to be supported by an acceptance.  A witness that names
    several acceptances names no single subject and gets :data:`NO_SUBJECT` — the
    lanes in this repository each cover exactly one acceptance, so that case is a
    caller mistake rather than a third lane, and the empty subject makes it collide
    loudly instead of filing quietly beside an unrelated licence.
    """

    digests = condition_digests(witness)
    if digests:
        return CONDITION_SUBJECT + hashlib.sha256("|".join(digests).encode("utf-8")).hexdigest()
    acceptances = acceptance_subjects(witness)
    if len(acceptances) == 1:
        return ACCEPTANCE_SUBJECT + acceptances[0]
    return NO_SUBJECT


def condition_subject(digests: Iterable[str]) -> str:
    """The subject a precondition licence over ``digests`` must be filed under."""

    ordered = sorted({str(item) for item in digests})
    if not ordered:
        return NO_SUBJECT
    return CONDITION_SUBJECT + hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()


def acceptance_subject(acceptance_id: str) -> str:
    """The subject a DATA licence over ``acceptance_id`` must be filed under."""

    return ACCEPTANCE_SUBJECT + str(acceptance_id)


__all__ = (
    "ACCEPTANCE_SUBJECT",
    "CONDITION_REASON_PREFIX",
    "CONDITION_SUBJECT",
    "NO_SUBJECT",
    "acceptance_subject",
    "acceptance_subjects",
    "condition_digests",
    "condition_subject",
    "witness_subject",
)
