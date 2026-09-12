# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""G handoff oracle: an earlier effect cannot lend its source snapshot to the next.

Real recorded verification/acceptance, action approvals, ActionExecutor and the
test connector's durable effects are used. An explicit execute gate permits a real
source-revocation approval between the two handoffs. DOC4 must refuse the second
outbox entry and effect, retaining the already-started first effect and historical
assessment. Frozen DOC3 and code retain their existing action behavior. The code
control revokes an unrelated document Mission's source in the same library; it
does not pretend a code Mission has document citation provenance.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
import test_p33_source_commits as source_helpers
from helpers_step07 import ENABLED, candidate
from test_p33_source_commits import PATH, TEXT, accept, change_source, historical, submit, verify
from test_p33_source_commits import e_scenes as source_scenes

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import MissionStatus, TaskStatus
from agent_orchestrator.governance.domains import CODE_DOMAIN, DOC_DOMAIN, DOC_PROFILE_V3
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator import commit_service
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.runtime.actions import ActionExecutor
from agent_orchestrator.runtime.connectors import TestConfigService
from agent_orchestrator.verification.mission_coverage import mission_coverage

e_scenes = source_scenes
PERSON = Principal("g-handoff-reviewer")
ACTION_CRITERIA = ("action:test_config.set:a", "action:test_config.set:b")


class GatedConfigService(TestConfigService):
    def __init__(self, path):
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.started_targets = []

    def execute(self, operation, target, params, *, idempotency_key):
        self.started_targets.append(target)
        if target == "a":
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("the test did not release the first real connector call")
        return super().execute(operation, target, params, idempotency_key=idempotency_key)


def scene(e_scenes, monkeypatch, profile):
    domain = CODE_DOMAIN if profile == "code" else DOC_DOMAIN
    criteria = (("file:REPORT.md",) if profile == "code" else (TEXT.strip(),)) + ACTION_CRITERIA
    # Bind the actual old profile at Mission creation, before any Task/Attempt;
    # never rewrite an existing intent, assessment or accepted historical record.
    with monkeypatch.context() as patch:
        if profile == "v3":
            original = commit_service.resolve_domain
            patch.setattr(
                commit_service,
                "resolve_domain",
                lambda identifier: (
                    DOC_PROFILE_V3 if identifier == DOC_DOMAIN else original(identifier)
                ),
            )
            patch.setattr(source_helpers, "DOC_PROFILE", DOC_PROFILE_V3)
        s = e_scenes(paths=(PATH,), domain=domain, mission_criteria=criteria)
    assert s.commit.domain_for(s.mission.id).version == ("1" if profile == "code" else profile[1:])
    e = submit(s)
    verdict = verify(e)  # real router; only the model Critic's response is scripted
    assert verdict.passed, verdict.to_json()
    assert accept(e).status is TaskStatus.COMPLETED
    assert s.store.get_result(e.envelope.id).verdict == "PASS"
    if profile != "code":
        coverage = mission_coverage(s.store, s.mission, s.profile, artifact_store=s.cas)
        assert coverage["criteria"][0]["verdict"] == "PASS"
        assert s.store.list_criterion_assessments(s.mission.id, result_id=e.envelope.id)
        return s, e, s

    # An ordinary code Mission has no source roots. Keep the source-change control
    # in another Mission on the exact same Store/CAS instead of faking mixed input.
    other, _ = s.commit.create_mission(
        MissionSpec(
            goal="独立文档来源控制",
            success_criteria=("file:notes.md",),
            tenant_id=s.mission.tenant_id,
            idempotency_key="unrelated-source-owner",
            domain=DOC_DOMAIN,
        )
    )
    s.api.register_source(
        dict(
            mission_id=other.id, path=PATH, content=TEXT, kind="markdown", idempotency_key="source"
        )
    )
    return s, e, SimpleNamespace(**{**vars(s), "mission": other})


@pytest.mark.parametrize("profile", ["v4", "v3", "code"])
def test_second_real_action_rechecks_source_after_first_execute_await(
    e_scenes, monkeypatch, profile
):
    s, e, source = scene(e_scenes, monkeypatch, profile)
    connector = GatedConfigService(s.root / "effects.json")
    connectors = {"test_config": connector}
    host = SimpleNamespace(
        store=s.store, commit=s.commit, config=SimpleNamespace(deployment_policy=ENABLED)
    )
    api = MissionControlV1(host, tenant_id=s.mission.tenant_id, principal=PERSON)
    actions = []
    for target in ("a", "b"):
        action = s.commit.propose_action(
            candidate(target),
            mission_id=s.mission.id,
            task_id=e.task.id,
            result_id=e.envelope.id,
            attempt_id=e.attempt.id,
            artifact_id=e.artifact.id,
            artifact_hash=e.artifact.content_hash,
            connectors=connectors,
            deployment=ENABLED,
        )
        api.decide(action["approval_request_id"], "approve", nonce="approve:" + target)
        assert s.store.get_action(action["action_key"])["state"] == "APPROVED"
        actions.append(action)
    before = historical(s)
    executor = ActionExecutor(s.commit, connectors, ENABLED, owner="g-handoff")

    async def run():
        async def two_effects():
            # This deliberately keeps the same pre-approval action list across
            # await, as the runtime's two-action loop does. The commit fence owns
            # currentness; neither the test nor connector substitutes a verdict.
            return [await executor.hand_off(action["action_key"]) for action in actions]

        pending = asyncio.create_task(two_effects())
        try:
            assert await asyncio.to_thread(connector.entered.wait, 5)
            assert connector.started_targets == ["a"]
            assert s.store.get_action(actions[0]["action_key"])["state"] == "HANDED_OFF"
            assert s.store.get_action(actions[1]["action_key"])["state"] == "APPROVED"
            assert s.store.get_source(source.mission.id, PATH) is not None
            change_source(source, mode="revoke", key="between-effects")
            assert s.store.get_source(source.mission.id, PATH) is None
        finally:
            connector.release.set()
            results = await asyncio.wait_for(pending, 10)
        return results

    results = asyncio.run(run())
    assert results[0]["state"] == "SUCCEEDED"
    assert connector.lookup(actions[0]["idempotency_key"]) is not None
    events = [
        event for event in s.store.list_events(s.mission.id) if event.type == "ActionHandedOff"
    ]
    assert sum(event.payload["action_key"] == actions[0]["action_key"] for event in events) == 1
    second = s.store.get_action(actions[1]["action_key"])
    if profile == "v4":
        assert results[1] is None
        assert connector.started_targets == ["a"]
        assert connector.lookup(actions[1]["idempotency_key"]) is None
        assert not any(event.payload["action_key"] == actions[1]["action_key"] for event in events)
        assert int(second.get("handoffs") or 0) == 0
        assert s.store.get_mission(s.mission.id).status is MissionStatus.FAILED
        assert s.store.get_mission(s.mission.id).stop_reason == "mission_criteria_unmet"
        # No budget was reserved for the refused second effect either.
        with s.store.read_view() as db:
            assert (
                db.execute(
                    "SELECT 1 FROM budget_reservations WHERE subject_id=?",
                    ("action:" + actions[1]["action_key"],),
                ).fetchone()
                is None
            )
    else:
        assert results[1]["state"] == "SUCCEEDED"
        assert connector.started_targets == ["a", "b"]
        assert connector.lookup(actions[1]["idempotency_key"]) is not None
        assert sum(event.payload["action_key"] == actions[1]["action_key"] for event in events) == 1
        assert second["handoffs"] == 1
        assert s.store.get_mission(s.mission.id).status is MissionStatus.ACTIVE
    assert historical(s) == before
