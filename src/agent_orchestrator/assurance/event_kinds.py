# SPDX-License-Identifier: Apache-2.0
"""Exact immutable bridges; names are not substitutes for issuer validation."""

EVENT_REF_KINDS = {
    "reservation_fact": "AssuranceReservationLinked",
    "agent_turn_receipt": "AssuranceReviewTurnImported",
    "execution_receipt": "AssuranceExecutionImported",
    "local_check_receipt": "AssuranceLocalCheckFinished",
    "check_spec": "AssuranceCheckSpecRegistered",
    "disclosure_receipt": "AssuranceEvidenceDisclosed",
}

# Fixed code-owned SQL literals, never request content.
SOURCE_EVENT_SQL = ",".join("'" + value + "'" for value in sorted(EVENT_REF_KINDS.values()))
