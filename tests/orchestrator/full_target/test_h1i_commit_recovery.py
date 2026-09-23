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
from agent_orchestrator.contracts import ContractError
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

_CHILD = r"""
import asyncio
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
mode = sys.argv[2]
full_target = Path.cwd() / "tests" / "orchestrator" / "full_target"
sys.path.insert(0, str(full_target))

from test_h1i_production_entry import (
    _config, _open_planner_round, _refine_reply, _seed_new_protocol,
)
from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider

async def main():
    async with Orchestrator(
        _config(root), RoleScriptedProvider({"planner": []})
    ) as loop:
        mission, _env, _contract, dispatch = _seed_new_protocol(
            loop, root, key="h1i-i05-process"
        )
        opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
        PlanningAuthorizationApi(
            loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
        ).issue(
            mission.id,
            command_id="grant-h1i-i05-process",
            request_id=opener.intent_id,
        )
        raw = _refine_reply(opener.config["planning_package"])
        (root / "recovery-fixture.json").write_text(
            json.dumps({
                "mission_id": mission.id,
                "intent_id": opener.intent_id,
                "raw": raw,
                "repository": str(root / "repo"),
                "issuer_id": loop._owner,
            }),
            encoding="utf-8",
        )

        if mode == "before_final_row":
            original = PlanningDecisionStore.record_planning_decision
            def crash_before_final_row(self, *args, **kwargs):
                if kwargs.get("status") == PlanningDecisionStatus.COMMITTED:
                    os._exit(91)
                return original(self, *args, **kwargs)
            PlanningDecisionStore.record_planning_decision = crash_before_final_row
        await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)
        if mode == "after_durable_commit":
            os._exit(92)
        raise AssertionError("COMMITTED fault point was not reached")

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
            ).encode()
        ).hexdigest()
        for row in rows
    )


@pytest.mark.parametrize("revoke_after_crash", (False, True))
def test_i05_commit_then_process_exit_replays_from_frozen_request(
    tmp_path: Path, revoke_after_crash: bool
) -> None:
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), "before_final_row"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 91, (child.stdout, child.stderr)

    fixture = json.loads((tmp_path / "recovery-fixture.json").read_text(encoding="utf-8"))

    async def recover() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission = loop.store.get_mission(fixture["mission_id"])
            intent = loop.store.get_intent(fixture["intent_id"])
            assert mission is not None and intent is not None

            semantics = HtnStore(loop.store)
            world = build_planning_world(
                mission.id,
                domains=("code",),
                semantics=semantics,
                deployed_layers=deployed_layers(loop._config.deployment_policy),
                observers=code_observers(fixture["repository"], allow_test_execution=True),
            )
            dispatch = loop.install_hierarchical(planning=world)
            before_hashes = _revision_hashes(loop.store, mission.id)
            assert len(before_hashes) in {0, 1}
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
                    command_id="revoke-h1i-i05-after-crash",
                    reason="recovery must reread current authority",
                )

            await loop._collect_plan_decision(intent, object(), mission, fixture["raw"], dispatch)

            after_hashes = _revision_hashes(loop.store, mission.id)
            if revoke_after_crash:
                assert after_hashes == before_hashes
            else:
                assert len(after_hashes) == 1
                assert (
                    sum(
                        event.type == "PlanRevisionCommitted"
                        for event in loop.store.list_events(mission.id)
                    )
                    == 1
                )
            if before_hashes and not revoke_after_crash:
                assert after_hashes == before_hashes
            decision = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                intent.intent_id, 0
            )
            assert decision is not None
            assert decision["status"] == str(
                PlanningDecisionStatus.COMMIT_REJECTED
                if revoke_after_crash
                else PlanningDecisionStatus.COMMITTED
            )
            assert decision["canonical_hash"] is not None
            assert provider.calls == 0

    asyncio.run(recover())


def test_i05_recovery_source_failure_is_commit_rejected_without_retry_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), "before_final_row"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 91, (child.stdout, child.stderr)
    fixture = json.loads((tmp_path / "recovery-fixture.json").read_text(encoding="utf-8"))

    async def recover() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission = loop.store.get_mission(fixture["mission_id"])
            intent = loop.store.get_intent(fixture["intent_id"])
            assert mission is not None and intent is not None
            world = build_planning_world(
                mission.id,
                domains=("code",),
                semantics=HtnStore(loop.store),
                deployed_layers=deployed_layers(loop._config.deployment_policy),
                observers=code_observers(fixture["repository"], allow_test_execution=True),
            )
            dispatch = loop.install_hierarchical(planning=world)
            before_hashes = _revision_hashes(loop.store, mission.id)
            before_intents = tuple(
                row[0]
                for row in loop.store.connection.execute(
                    "SELECT intent_id FROM dispatch_intents ORDER BY intent_id"
                )
            )

            def unavailable(**_kwargs):
                raise ContractError("producer contract unavailable")

            monkeypatch.setattr(loop, "_hierarchical_admission_context", unavailable)
            await loop._collect_plan_decision(intent, object(), mission, fixture["raw"], dispatch)

            assert _revision_hashes(loop.store, mission.id) == before_hashes
            assert (
                tuple(
                    row[0]
                    for row in loop.store.connection.execute(
                        "SELECT intent_id FROM dispatch_intents ORDER BY intent_id"
                    )
                )
                == before_intents
            )
            decision = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                intent.intent_id, 0
            )
            assert decision is not None
            assert decision["status"] == str(PlanningDecisionStatus.COMMIT_REJECTED)
            assert decision["rejection_codes"] == ["INTERNAL_CONTRACT_ERROR"]
            assert decision["canonical_hash"] is not None
            assert provider.calls == 0

    asyncio.run(recover())


@pytest.mark.parametrize("revoke_before_replay", (False, True))
def test_i05_exit_after_durable_commit_replays_exactly_without_new_writes(
    tmp_path: Path, revoke_before_replay: bool
) -> None:
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), "after_durable_commit"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 92, (child.stdout, child.stderr)
    fixture = json.loads((tmp_path / "recovery-fixture.json").read_text(encoding="utf-8"))

    async def recover() -> None:
        provider = RoleScriptedProvider({"planner": []})
        async with Orchestrator(_config(tmp_path), provider) as loop:
            mission = loop.store.get_mission(fixture["mission_id"])
            intent = loop.store.get_intent(fixture["intent_id"])
            assert mission is not None and intent is not None
            world = build_planning_world(
                mission.id,
                domains=("code",),
                semantics=HtnStore(loop.store),
                deployed_layers=deployed_layers(loop._config.deployment_policy),
                observers=code_observers(fixture["repository"], allow_test_execution=True),
            )
            dispatch = loop.install_hierarchical(planning=world)
            if revoke_before_replay:
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
                    command_id="revoke-h1i-i05-durable",
                    reason="terminal replay must not reauthorize",
                )

            before = (
                _revision_hashes(loop.store, mission.id),
                HtnStore(loop.store).list_commit_receipts(mission.id),
                tuple(loop.store.list_events(mission.id)),
                tuple(
                    tuple(row)
                    for row in loop.store.connection.execute(
                        "SELECT * FROM dispatch_intents ORDER BY intent_id"
                    )
                ),
                tuple(loop.store.list_actions(mission.id)),
            )
            stored_before = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                intent.intent_id, 0
            )
            assert stored_before is not None
            assert stored_before["status"] == str(PlanningDecisionStatus.COMMITTED)

            await loop._collect_plan_decision(intent, object(), mission, fixture["raw"], dispatch)

            after = (
                _revision_hashes(loop.store, mission.id),
                HtnStore(loop.store).list_commit_receipts(mission.id),
                tuple(loop.store.list_events(mission.id)),
                tuple(
                    tuple(row)
                    for row in loop.store.connection.execute(
                        "SELECT * FROM dispatch_intents ORDER BY intent_id"
                    )
                ),
                tuple(loop.store.list_actions(mission.id)),
            )
            assert before[0] and before[1] and before[3]
            assert after == before
            assert (
                PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(
                    intent.intent_id, 0
                )
                == stored_before
            )
            assert provider.calls == 0

    asyncio.run(recover())
