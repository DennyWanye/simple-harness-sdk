# SPDX-License-Identifier: Apache-2.0
"""C07 (SDK half): the fixed-caller Assurance read verbs over a real Orchestrator.

Real ``Orchestrator`` + ``install_assurance`` (native root, fixed principal,
factory, four consumers), real assured and legacy Missions, real facade. No
model, no Host process: the Host route/UI half of C07 lives in the Host tests.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_orchestrator.api.assurance import AssuranceReadError, ITEM_KINDS
from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.assurance.contracts import ContractViolation, validate
from agent_orchestrator.assurance.policy import AssurancePolicy
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.assurance_assembly import (
    AssuranceDeploymentPorts,
    install_assurance,
)
from agent_orchestrator.orchestrator.assurance_consumers import NOTIFICATION_EVENT
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

TENANT = "tenant-c07"
PRINCIPAL = Principal("c07-current-user")
HOST_FINGERPRINT = "ab" * 32


def _spec(key: str, **extra):
    return MissionSpec(
        goal="assured " + key,
        success_criteria=("the answer file is written", "it names the fixture"),
        tenant_id=TENANT,
        idempotency_key=key,
        orchestration_semantics_version="hierarchical",
        planning_protocol_version="planning-decision-v1",
        **extra,
    )


def _snapshot_request(mission_id, **overrides):
    body = {
        "schema_version": 1, "request_id": "r-snap", "mission_id": mission_id,
        "view": "CURRENT", "at_event_seq": None, "cursor": None, "limit": 100,
    }
    body.update(overrides)
    return body


def _refused(call, code):
    with pytest.raises(FacadeError) as raised:
        call()
    assert raised.value.code == code, (raised.value.code, str(raised.value))
    wire = raised.value.wire  # type: ignore[attr-defined]
    validate("host-error-v1", wire)
    assert wire["code"] == code
    return wire


async def _deployment(root, checks):
    cfg = OrchestratorConfig(evidence_root=root / "root")
    sent = []

    def root_setup(orch):
        orch.commit.install_assurance_root(
            principal=PRINCIPAL, tenant_id=TENANT, command_id="install"
        )

    def assembly(orch):
        install_assurance(orch, AssuranceDeploymentPorts(
            tenant_id=TENANT, principal=PRINCIPAL,
            select_profile=lambda spec: AssurancePolicy() if spec.idempotency_key.startswith("assured") else None,
            notify_transport=sent.append, host_fingerprint=HOST_FINGERPRINT,
        ))

    async with Orchestrator(cfg, RoleScriptedProvider({}), assurance_root_setup=root_setup,
                            startup_assembly=assembly) as orch:
        await checks(orch, sent)


def test_snapshot_review_use_check_contracts_and_errors(tmp_path):
    async def checks(orch, sent):
        store, commit = orch.store, orch.commit
        control = MissionControlV1(orch, tenant_id=TENANT, principal=PRINCIPAL)
        assured, created = commit.create_mission(_spec("assured-1"))
        legacy, _ = commit.create_mission(MissionSpec(
            goal="legacy", success_criteria=("c",), tenant_id=TENANT, idempotency_key="legacy-1"))
        assert created

        # --- ownership, lane and contract gates -------------------------------
        _refused(lambda: control.assurance_snapshot(_snapshot_request("no-such-mission")), "NOT_FOUND")
        _refused(lambda: control.assurance_snapshot(_snapshot_request(legacy.id)), "PROFILE_UNBOUND")
        _refused(lambda: control.assurance_snapshot({**_snapshot_request(assured.id), "extra": 1}), "CONTRACT_INVALID")
        _refused(lambda: control.assurance_snapshot(_snapshot_request(assured.id, limit=0)), "CONTRACT_INVALID")
        _refused(lambda: control.assurance_snapshot(_snapshot_request(assured.id, at_event_seq=3)), "CONTRACT_INVALID")
        _refused(lambda: control.assurance_snapshot(_snapshot_request(assured.id, cursor="not-a-cursor")), "CONTRACT_INVALID")
        # Another authenticated identity has no installed read assembly: never a body-level override.
        other = MissionControlV1(orch, tenant_id=TENANT, principal=Principal("someone-else"))
        _refused(lambda: other.assurance_snapshot(_snapshot_request(assured.id)), "PROFILE_UNBOUND")
        stranger = MissionControlV1(orch, tenant_id="other-tenant", principal=PRINCIPAL)
        _refused(lambda: stranger.assurance_snapshot(_snapshot_request(assured.id)), "PROFILE_UNBOUND")

        # --- CURRENT snapshot over the real activated Mission ----------------------
        request = _snapshot_request(assured.id)
        validate("host-snapshot-request-v1", request)
        page = control.assurance_snapshot(request)
        validate("host-response-v1", page)
        assert page["mission_id"] == assured.id and page["view"] == "CURRENT"
        assert page["host_fingerprint"] == HOST_FINGERPRINT and len(page["sdk_fingerprint"]) == 64
        assert page["snapshot_seq"] == store.last_event_seq(assured.id) > 0
        assert page["next_cursor"] is None and page["truncated"] is False
        kinds = [(item["kind"], item["id"]) for item in page["items"]]
        assert kinds == sorted(kinds) and all(kind in ITEM_KINDS for kind, _ in kinds)
        by_kind = {}
        for item in page["items"]:
            by_kind.setdefault(item["kind"], []).append(item)
        assert [c["id"] for c in by_kind["CRITERION"]] == ["c-user-1", "c-user-2"]
        assert {c["history_state"] for c in by_kind["CRITERION"]} == {"UNREVIEWED"}
        assert {c["current_use"] for c in by_kind["CRITERION"]} == {"NOT_APPLICABLE"}
        assert by_kind["CLOSEOUT"] == [{
            "kind": "CLOSEOUT", "id": assured.id, "history_state": "NOT_EVALUATED",
            "current_use": "UNAVAILABLE", "reason_codes": ["NO_CLOSEOUT_ROW"],
            "evidence_count": 0, "artifact_ref": None,
        }]
        assert "REVIEW" not in by_kind and "CONTRIBUTION" not in by_kind

        # --- stable paging and SNAPSHOT_CHANGED --------------------------------------
        first = control.assurance_snapshot(_snapshot_request(assured.id, limit=1))
        validate("host-response-v1", first)
        assert first["truncated"] is True and first["next_cursor"]
        assert first["items"] == page["items"][:1]
        second = control.assurance_snapshot(_snapshot_request(assured.id, limit=1, cursor=first["next_cursor"]))
        assert second["items"] == page["items"][1:2]
        # A new event moves the head: the old cursor must not be spliced onto the new state.
        final = [e for e in store.list_events(assured.id) if e.type == "MissionCreated"][0]
        commit._emit(NOTIFICATION_EVENT, assured.id, key=final.id,
                     payload={"final_event_id": final.id, "state_version": assured.version,
                              "final_event_type": final.type})
        _refused(lambda: control.assurance_snapshot(_snapshot_request(assured.id, limit=1, cursor=first["next_cursor"])),
                 "SNAPSHOT_CHANGED")
        # Cursor of another Mission is refused the same way, not silently applied.
        _refused(lambda: control.assurance_snapshot(_snapshot_request(legacy.id, cursor=first["next_cursor"])),
                 "PROFILE_UNBOUND")

        # --- HISTORY view -----------------------------------------------------------
        events = store.list_events(assured.id)
        activation = next(e for e in events if e.type == "AssuranceProfileActivated")
        head = store.last_event_seq(assured.id)
        _refused(lambda: control.assurance_snapshot(_snapshot_request(
            assured.id, view="HISTORY", at_event_seq=activation.seq - 1)), "SOURCE_UNAVAILABLE")
        _refused(lambda: control.assurance_snapshot(_snapshot_request(
            assured.id, view="HISTORY", at_event_seq=head + 1)), "NOT_FOUND")
        _refused(lambda: control.assurance_snapshot(_snapshot_request(
            assured.id, view="HISTORY", at_event_seq=None)), "CONTRACT_INVALID")
        history = control.assurance_snapshot(_snapshot_request(assured.id, view="HISTORY", at_event_seq=activation.seq))
        validate("host-response-v1", history)
        assert history["view"] == "HISTORY" and history["snapshot_seq"] == activation.seq
        h_kinds = {(item["kind"], item["id"], item["history_state"]) for item in history["items"]}
        assert ("CRITERION", "c-user-1", "UNREVIEWED") in h_kinds
        assert ("CLOSEOUT", assured.id, "NOT_EVALUATED") in h_kinds
        # History pages are pinned to their seq: a later head does not change them.
        commit._emit(NOTIFICATION_EVENT, assured.id, key=final.id + ":again",
                     payload={"final_event_id": final.id, "state_version": assured.version,
                              "final_event_type": final.type})
        again = control.assurance_snapshot(_snapshot_request(assured.id, view="HISTORY", at_event_seq=activation.seq))
        assert again["items"] == history["items"] and again["snapshot_seq"] == activation.seq

        # --- review -------------------------------------------------------------------
        review_request = {"schema_version": 1, "request_id": "r-rev", "mission_id": assured.id,
                          "review_key": "no-such-review", "cursor": None, "limit": 10}
        validate("host-review-request-v1", review_request)
        _refused(lambda: control.assurance_review(review_request), "NOT_FOUND")
        _refused(lambda: control.assurance_review({**review_request, "limit": 101}), "CONTRACT_INVALID")

        # --- use_check: diagnostic only, never a certificate ----------------------------
        use_request = {"schema_version": 1, "request_id": "r-use", "mission_id": assured.id,
                       "subject_ref": {"kind": "result", "pin": {"id": "result-none", "revision": 0,
                                                                  "content_hash": "0" * 64}},
                       "view": "CURRENT", "at_event_seq": None}
        validate("host-use-request-v1", use_request)
        use = control.assurance_use_check(use_request)
        validate("host-use-response-v1", use)
        assert use["decision"] == "UNAVAILABLE" and use["diagnostic_only"] is True
        assert use["certificate_ref"] is None and use["coverage"] == "INCOMPLETE"
        assert "REVIEW_RESULT_SOURCE_MISSING" in use["reason_codes"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", (assured.id,)
        ).fetchone()[0] == 0
        unsupported = control.assurance_use_check({**use_request, "subject_ref": {
            "kind": "artifact", "pin": {"id": "a", "revision": 1, "content_hash": "1" * 64}}})
        assert unsupported["decision"] == "UNAVAILABLE"
        assert unsupported["reason_codes"] == ["SUBJECT_KIND_UNSUPPORTED:artifact"]
        _refused(lambda: control.assurance_use_check({**use_request, "view": "HISTORY", "at_event_seq": 1}),
                 "SOURCE_UNAVAILABLE")
        _refused(lambda: control.assurance_use_check({**use_request, "subject_ref": {"kind": "bogus"}}),
                 "CONTRACT_INVALID")
        # The read verbs never touch the four consumers' queue.
        assert not sent

    asyncio.run(_deployment(tmp_path, checks))


def test_contract_checker_rejects_drift():
    good = {"schema_version": 1, "request_id": "x", "code": "NOT_FOUND", "message": "m", "retryable": False}
    validate("host-error-v1", good)
    for bad in (
        {**good, "code": "SOMETHING_ELSE"},
        {**good, "schema_version": 2},
        {**good, "extra": True},
        {key: value for key, value in good.items() if key != "retryable"},
    ):
        with pytest.raises(ContractViolation):
            validate("host-error-v1", bad)
    with pytest.raises(ValueError):
        AssuranceReadError("NOT_A_CODE", "x")
