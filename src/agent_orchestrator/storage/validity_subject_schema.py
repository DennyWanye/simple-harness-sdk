# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""FULL-TARGET P2.3c DDL (migration 18): *which subject* a validity witness licenses.

Migration 16 keyed ``validity_witnesses`` on
``(mission, consumer_kind, consumer_id, purpose, scope, epoch, support_revision)``
and made that key UNIQUE.  AER §18 only asks for an index on
``(consumer_ref, purpose, scope)``; the UNIQUE was a tightening this repository
added, and part 2c's real-model smoke found what it costs: one leaf that both
consumes an accepted output (the DATA lane) *and* sits under a gated method (the
START-precondition lane) holds **two** licences at the same instant, of the same
purpose, in the same epoch, at the same support revision.  Under the old key the
second one could not be stored, so the occurrence waited for a licence that had
in fact been granted.

AER §8.1 lists ``support_selection`` as part of a witness's own identity — "which
support was this taken over" — so two licences over two different supports are
two rows, not one row that has to choose.  This migration therefore adds the
missing dimension as a **storage-derived column**: ``subject_digest``, "the object
this licence was issued for".  It is declared by the issuer and re-checked by
:func:`agent_orchestrator.knowledge.validity.witness_subject` before the insert,
never sniffed out of the row.

The ``ValidityWitness`` contract is byte-for-byte unchanged: the AER schema for it
is ``additionalProperties: false`` with a closed ``purpose`` enum, so the subject
may not travel as a contract field and does not enter ``witness_json``.

Migration 16's and 17's bytes are untouched.  Old rows land on ``subject_digest =
''``; because the old unique key is strictly narrower than the new one, no library
that was consistent under 16 can collide under 18, so the in-place upgrade path in
``store._initialize_or_validate`` applies as it stands.
"""

DDL = """
ALTER TABLE validity_witnesses ADD COLUMN subject_digest TEXT NOT NULL DEFAULT '';
DROP INDEX validity_witnesses_consumer_idx;
CREATE UNIQUE INDEX validity_witnesses_consumer_idx_v2 ON validity_witnesses(
 mission_id, consumer_kind, consumer_id, purpose, subject_digest,
 scope_id, scope_epoch, support_revision);
CREATE INDEX validity_witnesses_subject_idx
 ON validity_witnesses(mission_id, consumer_id, purpose, subject_digest);
"""

__all__ = ("DDL",)
