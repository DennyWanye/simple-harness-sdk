from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_h1i_production_entry import _config

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.artifacts.store import read_nofollow
from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import deployed_layers
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.planning.htn.observers.code import code_observers
from agent_orchestrator.planning.htn.world import build_planning_world
from agent_orchestrator.storage.htn_store import HtnStore
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from simple_harness.contracts import canonical_json

# This is deliberately a fresh child process: os._exit must bypass all context-manager
# cleanup after COMPILED has been durably recorded and before the plan write begins.
_I04_CHILD = r"""
import asyncio
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
full_target = Path.cwd() / "tests" / "orchestrator" / "full_target"
sys.path.insert(0, str(full_target))

from test_h1i_production_entry import (
    _config, _open_planner_round, _refine_reply, _seed_new_protocol,
)
from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

async def main():
    async with Orchestrator(_config(root), RoleScriptedProvider({"planner": []})) as loop:
        mission, _env, _contract, dispatch = _seed_new_protocol(
            loop, root, key="h1i-i04-compiled-exit"
        )
        opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
        PlanningAuthorizationApi(
            loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
        ).issue(
            mission.id,
            command_id="grant-h1i-i04-compiled-exit",
            request_id=opener.intent_id,
        )
        raw = _refine_reply(opener.config["planning_package"])
        # Never persist raw here: recovery must retrieve it by the decision's
        # raw_artifact_ref from the ArtifactStore after this process dies.
        (root / "recovery-fixture.json").write_text(
            json.dumps({
                "mission_id": mission.id,
                "intent_id": opener.intent_id,
                "repository": str(root / "repo"),
                "issuer_id": loop._owner,
            }),
            encoding="utf-8",
        )

        def crash_at_real_plan_write_entry(self, *args, **kwargs):
            # event_handler has persisted COMPILED immediately before opening its
            # transaction and calling this exact production method.
            os._exit(93)

        HierarchicalDispatch.commit_preview_plan_proposal = crash_at_real_plan_write_entry
        await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)
        raise AssertionError("I04 forced-exit point was not reached")

asyncio.run(main())
"""


def _revision_hashes(store, mission_id: str) -> tuple[str, ...]:
    rows = store.connection.execute(
        "SELECT mission_id,revision,state,base_revision,snapshot_hash,delta_id,read_set_json "
        "FROM plan_revisions WHERE mission_id = ? ORDER BY revision",
        (mission_id,),
    ).fetchall()
    return tuple(
        hashlib.sha256(
            canonical_json(
                {
                    "mission_id": row["mission_id"],
                    "revision": row["revision"],
                    "state": row["state"],
                    "base_revision": row["base_revision"],
                    "snapshot_hash": row["snapshot_hash"],
                    "delta_id": row["delta_id"],
                    "read_set": json.loads(row["read_set_json"]),
                }
            ).encode("utf-8")
        ).hexdigest()
        for row in rows
    )


def _rows(connection, sql: str, parameters: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in connection.execute(sql, parameters).fetchall())


@pytest.mark.parametrize("revoke_after_crash", (False, True))
def test_i04_compiled_exit_recovers_only_from_durable_raw_artifact(
    tmp_path: Path, revoke_after_crash: bool
) -> None:
    child = subprocess.run(
        [sys.executable, "-c", _I04_CHILD, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 93, (child.stdout, child.stderr)

    # This fixture intentionally holds only stable identifiers. In particular it has
    # no raw reply, raw hash, artifact path, decision state, or authority snapshot.
    fixture = json.loads((tmp_path / "recovery-fixture.json").read_text(encoding="utf-8"))
    assert "raw" not in fixture

    async def recover() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission = loop.store.get_mission(fixture["mission_id"])
            intent = loop.store.get_intent(fixture["intent_id"])
            assert mission is not None and intent is not None
            decisions = PlanningDecisionStore(loop.store)
            compiled = decisions.get_planning_decision_by_attempt(intent.intent_id, 0)
            assert compiled is not None
            assert compiled["status"] == str(PlanningDecisionStatus.COMPILED)
            assert compiled["raw_artifact_ref"]
            assert _revision_hashes(loop.store, mission.id) == ()

            # raw_artifact_ref is a content address, not an Artifact database id.
            # Re-open the actual configured ArtifactStore and verify its persisted bytes.
            raw_ref = str(compiled["raw_artifact_ref"])
            raw_bytes = read_nofollow(loop.assembled.workspaces.artifact_store.path_for(raw_ref))
            assert hashlib.sha256(raw_bytes).hexdigest() == raw_ref
            assert hashlib.sha256(raw_bytes).hexdigest() == compiled["raw_output_hash"]
            raw = raw_bytes.decode("utf-8")
            frozen_raw = (raw_ref, str(compiled["raw_output_hash"]), raw_bytes)

            world = build_planning_world(
                mission.id,
                domains=("code",),
                semantics=HtnStore(loop.store),
                deployed_layers=deployed_layers(loop._config.deployment_policy),
                observers=code_observers(fixture["repository"], allow_test_execution=True),
            )
            dispatch = loop.install_hierarchical(planning=world)

            if revoke_after_crash:
                grant = loop.store.connection.execute(
                    "SELECT grant_id,revision FROM planning_lane_grants "
                    "WHERE mission_id = ? ORDER BY revision DESC LIMIT 1",
                    (mission.id,),
                ).fetchone()
                assert grant is not None
                PlanningAuthorizationApi(
                    loop.store,
                    tenant_id=mission.tenant_id,
                    principal=Principal(fixture["issuer_id"]),
                ).revoke(
                    str(grant["grant_id"]),
                    expected_revision=int(grant["revision"]),
                    command_id="revoke-h1i-i04-after-crash",
                    reason="recovery must reread current authority",
                )

            before_grants = _rows(
                loop.store.connection,
                "SELECT * FROM planning_lane_grants WHERE mission_id = ? "
                "ORDER BY grant_id,revision",
                (mission.id,),
            )
            before_actions = tuple(loop.store.list_actions(mission.id))
            await loop._collect_plan_decision(intent, object(), mission, raw, dispatch)

            after = decisions.get_planning_decision_by_attempt(intent.intent_id, 0)
            assert after is not None
            assert (after["raw_artifact_ref"], after["raw_output_hash"]) == frozen_raw[:2]
            persisted_again = read_nofollow(
                loop.assembled.workspaces.artifact_store.path_for(str(after["raw_artifact_ref"]))
            )
            assert persisted_again == frozen_raw[2]
            assert provider.calls == 0

            if revoke_after_crash:
                assert after["status"] == str(PlanningDecisionStatus.COMMIT_REJECTED)
                assert _revision_hashes(loop.store, mission.id) == ()
                # Recovery may write its rejection audit, but must neither mint nor renew authority
                # and must not create an action/connector call.
                assert (
                    _rows(
                        loop.store.connection,
                        "SELECT * FROM planning_lane_grants WHERE mission_id = ? "
                        "ORDER BY grant_id,revision",
                        (mission.id,),
                    )
                    == before_grants
                )
                assert tuple(loop.store.list_actions(mission.id)) == before_actions
            else:
                assert after["status"] == str(PlanningDecisionStatus.COMMITTED)
                assert len(_revision_hashes(loop.store, mission.id)) == 1
                assert (
                    sum(
                        event.type == "PlanRevisionCommitted"
                        for event in loop.store.list_events(mission.id)
                    )
                    == 1
                )

    asyncio.run(recover())
