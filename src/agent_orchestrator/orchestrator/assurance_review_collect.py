# SPDX-License-Identifier: Apache-2.0
"""Import actual review turns/exposure before interpretation or official judgement."""

from __future__ import annotations

import json
from typing import Any

from simple_harness.agents import AgentTurnResult
from simple_harness.contracts import canonical_json

from ..assurance.checks import ReviewReply
from ..assurance.codec import MAX_RECORD_BYTES, AssuranceError, decode, fingerprint
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.review_input import read_initial_materials
from ..runtime.assurance_turn_sources import read_actual_review_turn
from ..storage.assurance_reads import AssuranceReader
from ..storage.assurance_work import atomic
from ..storage.htn_store import HtnStore
from ..verification.reviewer_evidence_tools import (
    import_reviewer_disclosure,
    record_disclosure_batch,
)
from .assurance_review_transport import read_review_invocation_locked
from .failure_classes import record_review_interruption, review_turn_interrupted


async def collect_assurance_review(orchestrator: Any, intent: Any) -> None:
    """Called by the original plan collector, using its actual configured bridge.

    Importing a turn does not write an official ReviewRecord. The durable REVIEW
    consumer revalidates current evidence before doing that. A malformed response
    may enable one format repair; missing exposure/current validity may not.
    """
    store, commit = orchestrator.store, orchestrator.commit
    mission = store.get_mission(intent.mission_id)
    reader = AssuranceReader(store, tenant_id=mission.tenant_id, mission_id=mission.id)
    with store.read_view():
        invocation, binding = read_review_invocation_locked(commit, reader, intent.intent_id)
    # Existing execution DB is read outside the orchestrator transaction. Its
    # committed result is immutable. No cross-database atomicity is claimed.
    actual = read_actual_review_turn(orchestrator.bridge_for(intent), intent)
    # Persist actual raw output even when it exceeds the bounded review codec.
    # The immutable Agent result has already been decoded and hashed by the
    # runtime reader; its interpretation below still uses the bounded codec.
    result = AgentTurnResult.from_json(json.loads(actual.result_json))
    source = decode(actual.source_json)
    raw = "" if result.public_output is None else result.public_output.content
    if not isinstance(raw, str):
        # Preserve actual structured output bytes too; the review codec later
        # rejects it rather than silently concatenating hidden/text blocks.
        raw = canonical_json(result.to_json()["public_output"])
    raw_bytes = raw.encode("utf-8")
    cas = orchestrator.assembled.workspaces.artifact_store
    raw_hash = cas.put_bytes(raw_bytes)
    raw_path = ".assurance/review-output/" + fingerprint(
        {"intent": intent.intent_id, "turn": result.turn_id}
    )
    raw_ref = AssuranceRef("source", Pin(raw_path, 1, raw_hash))
    bound = binding.to_json()
    value = invocation.to_json()
    manifest = None if actual.exposure_json is None else decode(actual.exposure_json, limit=MAX_RECORD_BYTES)
    turn_payload = {
        "review_key": bound["review_key"],
        "invocation_ordinal": value["ordinal"],
        "intent_id": intent.intent_id,
        **source,
        "raw_output_ref": raw_ref.to_json(),
        "raw_output_hash": raw_hash,
        "provider_manifest_ref": None,
        "exposure_error": actual.exposure_error,
    }
    source_id = "assurance-review-turn:" + fingerprint(
        {"intent": intent.intent_id, "turn": result.turn_id}
    )
    with atomic(store):
        # Match current original intent identity even when settlement already ran.
        read_review_invocation_locked(commit, reader, intent.intent_id)
        if manifest is not None:
            manifest_hash = HtnStore(store).insert_input_manifest(
                mission.id,
                bound["subject"]["owner_task_ref"]["id"],
                manifest,
            )
            turn_payload["provider_manifest_ref"] = AssuranceRef(
                "input_manifest", Pin(manifest_hash, 0, manifest_hash)
            ).to_json()
        old = store.get_receipt(source_id)
        if old is not None:
            turn_ref = AssuranceRef.from_json(old.get("event_ref"), kinds={"agent_turn_receipt"})
            event = decode(reader.read_exact_metadata(turn_ref).body_json)
            if event["payload"] != turn_payload or old.get("source_hash") != fingerprint(
                turn_payload
            ):
                raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
        else:
            # Raw model text is explicitly untrusted. Source provenance comes
            # from the authenticated execution import Event, never this label.
            store.put_source(
                {
                    "mission_id": mission.id,
                    "tenant_id": mission.tenant_id,
                    "path": raw_path,
                    "version_hash": raw_hash,
                    "kind": "assurance-review-output",
                    "trust": "untrusted_external",
                    "registered_at": source["committed_at"],
                    "superseded_by": None,
                    "revoked": False,
                    "revision": 1,
                }
            )
            event = commit._emit(
                "AssuranceReviewTurnImported",
                mission.id,
                key=source_id,
                task_id=bound["subject"]["owner_task_ref"]["id"],
                payload=turn_payload,
            )
            turn_ref = AssuranceRef(
                "agent_turn_receipt", Pin(event.id, 0, fingerprint(event.to_json()))
            )
            store.insert_receipt(
                commit_id=source_id,
                kind="AssuranceReviewTurnImported",
                subject_id=intent.intent_id,
                base_version=0,
                proposal_hash=fingerprint(turn_payload),
                receipt={
                    "mission_id": mission.id,
                    "intent_id": intent.intent_id,
                    "source_hash": fingerprint(turn_payload),
                    "event_ref": turn_ref.to_json(),
                },
            )
    # The source transaction commits before exposure/codec errors can occur.
    exposure_error = actual.exposure_error
    if manifest is not None:
        try:
            _import_initial_exposure(commit, reader, binding, turn_ref, result.agent_id, manifest)
            # §4 appended evidence: only complete tool reads that this exact final
            # request contained. A tool value that never reached the model is absent.
            import_reviewer_disclosure(commit, reader, binding, turn_ref, result.agent_id, manifest)
        except AssuranceError as error:
            exposure_error = error.code
    classification = "READY_FOR_CURRENT_REVIEW"
    error_code = exposure_error
    reply = None
    echoed = orchestrator.bridge_for(intent).echoed_models(agent_id=result.agent_id)
    if manifest is not None and (
        manifest.get("response_model") != orchestrator._expected_model(intent)
        or echoed != {orchestrator._expected_model(intent)}
    ):
        classification, error_code = "MODEL_IDENTITY_MISMATCH", "REVIEW_MODEL_IDENTITY_MISMATCH"
    elif source["state"] != "COMMITTED":
        classification, error_code = "TURN_FAILED", "REVIEW_TURN_NOT_COMMITTED"
    elif exposure_error is not None:
        classification = "EXPOSURE_UNAVAILABLE"
    else:
        try:
            reply = ReviewReply.from_json(decode(raw))
        except AssuranceError as error:
            classification, error_code = "FORMAT_INVALID", error.code
    # Costs and unknown holds stay owned by the original runtime/ledger.
    orchestrator._import_usage(intent)
    orchestrator._settle_intent(intent, "SETTLED" if source["state"] == "COMMITTED" else "FAILED")
    orchestrator._settle_service_if_known(intent.subject_id, mission.id)
    with atomic(store):
        result_body = {
            "mission_id": mission.id,
            "review_key": bound["review_key"],
            "invocation_ordinal": value["ordinal"],
            "intent_id": intent.intent_id,
            "turn_ref": turn_ref.to_json(),
            "classification": classification,
            "error_code": error_code,
            "raw_output_hash": raw_hash,
        }
        receipt_id = "assurance-review-classified:" + fingerprint(
            {"intent": intent.intent_id, "turn": result.turn_id}
        )
        previous = store.get_receipt(receipt_id)
        if previous is not None and dict(previous) != result_body:
            raise AssuranceError("IMMUTABLE_IDENTITY_CONFLICT")
        if previous is None:
            kind = (
                "AssuranceReviewFormatRejected"
                if classification == "FORMAT_INVALID"
                else "AssuranceReviewClassified"
            )
            store.insert_receipt(
                commit_id=receipt_id,
                kind=kind,
                subject_id=intent.intent_id,
                base_version=0,
                proposal_hash=fingerprint(result_body),
                receipt=result_body,
            )
            commit._emit(
                kind,
                mission.id,
                key=receipt_id,
                payload={
                    **result_body,
                    "classification_receipt_ref": AssuranceRef(
                        "commit_receipt", Pin(receipt_id, 0, fingerprint(result_body))
                    ).to_json(),
                    "parsed_verdict": None if reply is None else reply.verdict,
                },
            )
        if classification == "TURN_FAILED" and review_turn_interrupted(result.error):
            # 2026-09-29：被重启打断的调用单独记一条，不改上面的分类回执。
            record_review_interruption(
                store, mission_id=mission.id, review_key=bound["review_key"],
                ordinal=int(value["ordinal"]), intent_id=intent.intent_id,
                error_code=str((result.error or {}).get("error_code", "")))


def _import_initial_exposure(
    commit: Any,
    reader: AssuranceReader,
    binding: Any,
    turn_ref: AssuranceRef,
    agent_id: str,
    manifest: dict,
) -> None:
    bound = binding.to_json()
    matches = []
    for row in manifest["messages"]:
        if row["kind"] != "user_input" or row["turn_id"] is None:
            continue
        try:
            entries = read_initial_materials(row["message"], binding)
        except AssuranceError:
            continue
        matches.append((row["message_id"], entries))
    if len(matches) != 1:
        raise AssuranceError("UNEXPOSED_EVIDENCE")
    message_id, entries = matches[0]
    if not entries:
        # Empty catalogues need no fabricated disclosure batch. The actual
        # request manifest remains the proof of an empty initial exposure set.
        return
    record_disclosure_batch(
        commit,
        reader,
        review_key=bound["review_key"],
        turn_ref=turn_ref,
        agent_id=agent_id,
        manifest=manifest,
        entries=entries,
        message_ids=(message_id,),
    )
