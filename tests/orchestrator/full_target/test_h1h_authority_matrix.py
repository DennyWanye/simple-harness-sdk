from __future__ import annotations

import asyncio
import dataclasses

import pytest
from test_h1h_commit_guard import _plan_revision_count, _setup
from test_h1i_production_entry import (
    _config,
    _events,
    _open_planner_round,
    _refine_reply,
    _seed_new_protocol,
)
from test_plan_commits import _world

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.planning_authorization import (
    PlanningLanePolicy,
    SourceUnavailable,
    StorePlanningAuthorityReader,
    build_planning_authorization,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.plan_commits import PlanCommitRejected
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.planning_admission_store import PlanningAdmissionStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def _write_counts(world) -> tuple[int, int, int]:
    """The three externally observable write surfaces guarded by §8 A02-A06."""

    return (
        _plan_revision_count(world),
        len(world.store.list_events(world.mission.id)),
        len(world.store.list_actions(world.mission.id)),
    )


def test_a02_never_authorized_request_is_authorization_required_and_writes_nothing(
    tmp_path,
) -> None:
    """A real request that was never bound has no planning authority.

    This is intentionally a red product expectation at the time it was added: the
    collector currently classifies a missing request-authority binding as the
    generic SOURCE_UNAVAILABLE.  Section 8 A02 requires the never-authorized
    branch to be AUTHORIZATION_REQUIRED.
    """

    world = _world(tmp_path, key="h1h-a02-missing-grant")
    _, _, admission = _setup(world, dispatchable=True)
    before = _write_counts(world)

    world.store.connection.execute(
        "DELETE FROM planning_request_authority_bindings WHERE request_id = ?",
        (admission.request_id,),
    )

    with pytest.raises(PlanCommitRejected, match="AUTHORIZATION_REQUIRED"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _write_counts(world) == before


def test_a02_dangling_bound_grant_is_source_unavailable_and_writes_nothing(
    tmp_path,
) -> None:
    world = _world(tmp_path, key="h1h-a02-dangling-grant")
    _, grant, admission = _setup(world)
    before = _write_counts(world)

    # Preserve the sealed request and authority binding while fault-injecting a
    # missing referenced source row.  The collector and commit path remain real.
    world.store.connection.execute("PRAGMA foreign_keys = OFF")
    try:
        world.store.connection.execute(
            "DELETE FROM planning_lane_grants WHERE grant_id = ?",
            (grant.grant_id,),
        )
    finally:
        world.store.connection.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(PlanCommitRejected, match="SOURCE_UNAVAILABLE"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _write_counts(world) == before


def test_a02_unreadable_authority_table_is_source_unavailable_and_writes_nothing(
    tmp_path,
) -> None:
    world = _world(tmp_path, key="h1h-a02-unreadable-source")
    _, _, admission = _setup(world)
    before = _write_counts(world)

    # Rename the real SQLite producer table so its production SELECT raises an
    # sqlite error.  This exercises the defensive producer boundary without a
    # fake reader or synthetic admission context.
    world.store.connection.execute(
        "ALTER TABLE planning_request_authority_bindings "
        "RENAME TO unavailable_planning_request_authority_bindings"
    )
    try:
        with pytest.raises(PlanCommitRejected, match="SOURCE_UNAVAILABLE"):
            world.service.commit_planning_revision(
                world.command,
                world.principal,
                admission=admission,
            )
    finally:
        world.store.connection.execute(
            "ALTER TABLE unavailable_planning_request_authority_bindings "
            "RENAME TO planning_request_authority_bindings"
        )

    assert _write_counts(world) == before


@pytest.mark.parametrize(
    ("table", "column", "where_column"),
    (
        ("planning_request_authority_bindings", "binding_json", "request_id"),
        ("planning_lane_grants", "allowed_decisions_json", "grant_id"),
        ("planning_lane_grants", "grant_json", "grant_id"),
    ),
)
def test_a02_malformed_authority_json_is_typed_source_unavailable_without_writes(
    tmp_path, table: str, column: str, where_column: str
) -> None:
    world = _world(tmp_path, key=f"h1h-a02-malformed-{column}")
    _, grant, admission = _setup(world)
    before = _write_counts(world)
    identity = admission.request_id if where_column == "request_id" else grant.grant_id

    # The table/column names come only from the closed parametrization above.
    world.store.connection.execute(
        f"UPDATE {table} SET {column} = ? WHERE {where_column} = ?",  # noqa: S608
        ("{not-json", identity),
    )
    authority = build_planning_authorization(
        admission.request_id,
        read=StorePlanningAuthorityReader(
            PlanningAdmissionStore(world.store), world.store
        ),
        caller=world.principal,
        policy=PlanningLanePolicy(),
        now_ms=int(world.store.now * 1000),
    )

    assert isinstance(authority, SourceUnavailable)
    assert authority.reason_code == "SOURCE_UNAVAILABLE"
    assert authority.source == "planning_authority"
    assert _write_counts(world) == before


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    (
        ("missing_binding", "AUTHORIZATION_REQUIRED"),
        ("dangling_grant", "INTERNAL_CONTRACT_ERROR"),
        ("malformed_json", "INTERNAL_CONTRACT_ERROR"),
        ("unreadable_sqlite", "INTERNAL_CONTRACT_ERROR"),
    ),
)
def test_a02_real_collector_records_authority_failures_as_closed_rejections(
    tmp_path, fault: str, expected_code: str
) -> None:
    """Decoded replies use the closed rejection vocabulary without format retry.

    Producer diagnostics remain in detail: missing authority is the existing
    AUTHORIZATION_REQUIRED code; corrupt or unreadable authority uses the existing
    INTERNAL_CONTRACT_ERROR code with SOURCE_UNAVAILABLE detail.  None is a codec
    failure, so the durable status must be REJECTED rather than UNREADABLE.
    """

    async def case() -> None:
        async with Orchestrator(
            _config(tmp_path), RoleScriptedProvider({"planner": []})
        ) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key=f"h1h-a02-collector-{fault}"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            grant = None
            if fault != "missing_binding":
                grant = PlanningAuthorizationApi(
                    loop.store,
                    tenant_id=mission.tenant_id,
                    principal=Principal(loop._owner),
                ).issue(
                    mission.id,
                    command_id=f"grant-h1h-a02-{fault}",
                    request_id=opener.intent_id,
                )

            renamed = False
            if fault == "dangling_grant":
                assert grant is not None
                loop.store.connection.execute("PRAGMA foreign_keys = OFF")
                try:
                    loop.store.connection.execute(
                        "DELETE FROM planning_lane_grants WHERE grant_id = ?",
                        (grant.grant_id,),
                    )
                finally:
                    loop.store.connection.execute("PRAGMA foreign_keys = ON")
            elif fault == "malformed_json":
                loop.store.connection.execute(
                    "UPDATE planning_request_authority_bindings SET binding_json = ? "
                    "WHERE request_id = ?",
                    ("{not-json", opener.intent_id),
                )
            elif fault == "unreadable_sqlite":
                loop.store.connection.execute(
                    "ALTER TABLE planning_request_authority_bindings "
                    "RENAME TO unavailable_planning_request_authority_bindings"
                )
                renamed = True

            before_revisions = len(HtnStore(loop.store).list_plan_revisions(mission.id))
            before_actions = len(loop.store.list_actions(mission.id))
            try:
                await loop._collect_plan_decision(
                    opener,
                    object(),
                    mission,
                    _refine_reply(opener.config["planning_package"]),
                    dispatch,
                )
            finally:
                if renamed:
                    loop.store.connection.execute(
                        "ALTER TABLE unavailable_planning_request_authority_bindings "
                        "RENAME TO planning_request_authority_bindings"
                    )

            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")[-1]
            stored = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert stored is not None
            assert evaluated.payload["status"] == str(PlanningDecisionStatus.REJECTED)
            assert evaluated.payload["rejection_codes"] == [expected_code]
            assert evaluated.payload["canonical_hash"] == stored["canonical_hash"]
            assert evaluated.payload["canonical_hash"] is not None
            if fault != "missing_binding":
                assert "SOURCE_UNAVAILABLE" in str(evaluated.payload["detail"])
            assert len(HtnStore(loop.store).list_plan_revisions(mission.id)) == before_revisions
            assert len(loop.store.list_actions(mission.id)) == before_actions
            assert loop.store.connection.execute(
                "SELECT count(*) FROM planning_requests WHERE request_id = ?",
                (opener.intent_id,),
            ).fetchone()[0] == 1

    asyncio.run(case())


def test_real_collector_success_event_uses_the_stored_canonical_hash(tmp_path) -> None:
    async def case() -> None:
        async with Orchestrator(
            _config(tmp_path), RoleScriptedProvider({"planner": []})
        ) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1h-canonical-success"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store,
                tenant_id=mission.tenant_id,
                principal=Principal(loop._owner),
            ).issue(
                mission.id,
                command_id="grant-h1h-canonical-success",
                request_id=opener.intent_id,
            )
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                _refine_reply(opener.config["planning_package"]),
                dispatch,
            )

            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")[-1]
            stored = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert stored is not None
            assert evaluated.payload["status"] == str(PlanningDecisionStatus.COMMITTED)
            assert evaluated.payload["canonical_hash"] == stored["canonical_hash"]
            assert evaluated.payload["canonical_hash"] is not None

    asyncio.run(case())


def test_real_collector_codec_failure_has_no_canonical_hash(tmp_path) -> None:
    async def case() -> None:
        async with Orchestrator(
            _config(tmp_path), RoleScriptedProvider({"planner": []})
        ) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1h-canonical-unreadable"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            await loop._collect_plan_decision(
                opener,
                object(),
                mission,
                "<planning_decision>{not-json}</planning_decision>",
                dispatch,
            )

            evaluated = _events(loop, mission.id, "PlanningDecisionEvaluated")[-1]
            stored = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                opener.intent_id, 0
            )
            assert stored is not None
            assert evaluated.payload["status"] == str(PlanningDecisionStatus.UNREADABLE)
            assert evaluated.payload["canonical_hash"] is None
            assert stored["canonical_hash"] is None

    asyncio.run(case())


@pytest.mark.parametrize(
    ("principal_changes", "reason"),
    (
        ({"principal_id": "other-tenant-planner"}, "PRINCIPAL_MISMATCH"),
        ({"scope_id": "other-mission"}, "SCOPE_NOT_AUTHORIZED"),
    ),
)
def test_a03_wrong_bound_principal_or_scope_is_refused_without_leak_or_writes(
    tmp_path, principal_changes: dict[str, str], reason: str
) -> None:
    world = _world(tmp_path, key="h1h-a03-wrong-principal")
    _, _, admission = _setup(world, dispatchable=True)
    before = _write_counts(world)
    other = dataclasses.replace(world.principal, **principal_changes)

    with pytest.raises(PlanCommitRejected, match=reason) as refused:
        world.service.commit_planning_revision(
            world.command,
            other,
            admission=admission,
        )

    assert admission.authority.grant_id not in str(refused.value)
    assert _write_counts(world) == before


@pytest.mark.parametrize("change", ("revoke", "renew"))
def test_a04_reply_bound_before_grant_change_is_stale_and_never_rebound(
    tmp_path, change: str
) -> None:
    world = _world(tmp_path, key=f"h1h-a04-{change}")
    api, grant, admission = _setup(world)
    before = _write_counts(world)

    if change == "revoke":
        api.revoke(
            grant.grant_id,
            expected_revision=grant.revision,
            command_id="h1h-a04-revoke",
            reason="replace authority",
        )
    else:
        api.renew(
            grant.grant_id,
            expected_revision=grant.revision,
            command_id="h1h-a04-renew",
        )

    with pytest.raises(PlanCommitRejected, match="REQUEST_BINDING_STALE"):
        world.service.commit_planning_revision(
            world.command,
            world.principal,
            admission=admission,
        )

    assert _write_counts(world) == before
