"""Owned subprocess implementation of test_action_cold_backup's strict oracle."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.artifacts.store import read_verified
from agent_orchestrator.artifacts.workspace import WorkspaceManager
from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.governance.policies import DeploymentPolicy
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.connectors import ConnectorTransportError, TestConfigService
from agent_orchestrator.runtime.model_router import RoutingRules, RuntimeProfile
from agent_orchestrator.storage import offline_backup as backup
from agent_orchestrator.testing.fixtures import (
    APPROVAL_SEED,
    APPROVAL_SPEC,
    RoleScriptedProvider,
    demo_approval_action_provider,
    graph_proposal_step,
    package_of,
)
from simple_harness.agents.context.budget import ContextPolicy

TOOLS = ("workspace_read_file", "workspace_write_file", "workspace_list")
CRITERION = "CHANGE.md describes the new_ui change and its rollback"
PHASE_SECONDS = 10
CLEANUP_SECONDS = 3


def write_json(path, body):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(body, stream, sort_keys=True, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def phase(name, seconds=PHASE_SECONDS):
    """Real wall deadline also covers synchronous SQLite/backup work."""

    def expired(_signum, _frame):
        # Runtime error boundaries may catch Exception: a hard deadline must not
        # turn into another retry or wait for an executor thread to shut down.
        os.write(2, f"P35 phase exceeded {seconds}s: {name}\n".encode())
        os._exit(125)

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


class FixtureInputBound:
    fingerprint = "p35-action-fixture-input-v1"
    bound_protocol = "fixture-text-only-v1"
    requires_prior_output_reserve = True

    def estimate_input_tokens(self, request):
        return 1000  # deterministic fixture protocol; not a production tokenizer claim


class ReceiptGate(TestConfigService):
    """Drop only transport visibility; actual service ledger is never manufactured."""

    def __init__(self, external, *, crash_marker=None):
        super().__init__(external / "config.json")
        self.external = external
        self.crash_marker = crash_marker
        self.boundary = None
        self.lookup_keys = []

    def execute(self, operation, target, params, *, idempotency_key):
        # Persistent transport counter catches re-sends even if service dedup hides them.
        with (self.external / "execute.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"key": idempotency_key, "pid": os.getpid()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        receipt = super().execute(operation, target, params, idempotency_key=idempotency_key)
        if self.crash_marker is not None:
            assert self.boundary is not None
            with self.path.open("rb") as stream:
                os.fsync(stream.fileno())
            write_json(
                self.crash_marker,
                {
                    **self.boundary,
                    "pid": os.getpid(),
                    "receipt": receipt.to_json(),
                    "external_state": self.state(),
                    "applied_at_monotonic": time.monotonic(),
                },
            )
            # The parent owns SIGKILL. Timeout is a strict failure, never a receipt.
            if not threading.Event().wait(PHASE_SECONDS):
                raise ConnectorTransportError("parent did not kill the applied child in time")
        return receipt

    def lookup(self, idempotency_key):
        self.lookup_keys.append(idempotency_key)
        if not (self.external / "allow-lookup").exists():
            raise ConnectorTransportError("authoritative service lookup temporarily unavailable")
        return super().lookup(idempotency_key)


def fixture_provider():
    provider = demo_approval_action_provider(TOOLS)
    # Amend only the fixture Planner's proposal text before its actual SDK call.
    # The runtime still validates/commits the graph and chooses/dispatches Tasks.
    # Include a real Task Critic so its FIRST allowance is inherited by the Worker.
    proposal = provider.scripts["planner"][0]
    body = json.loads(
        proposal.removeprefix("<task_graph_proposal>").removesuffix("</task_graph_proposal>")
    )
    (task,) = body["tasks"]
    task["verification_policy"].append("critic_review")
    task["budget"] = {**task["budget"], "max_tokens": 150_000, "max_tool_calls": 20}
    provider.scripts["planner"][0] = graph_proposal_step(body["tasks"])

    def critic(request):
        values = [json.loads(m.content) for m in request.messages if str(m.role) == "tool"]
        content = values[-1]["value"]["content"]
        assert "feature_flags.new_ui" in content and "on" in content and "off" in content
        assert CRITERION in package_of(request)["mission_success_criteria"]
        return (
            "<critic_verdict>"
            + json.dumps(
                {
                    "verdict": "PASS",
                    "findings": [],
                    "mission_criteria": [
                        {
                            "criterion": criterion,
                            "met": not criterion.startswith("action:"),
                            "reason": "Action awaits approval and its actual receipt"
                            if criterion.startswith("action:")
                            else "Read actual CHANGE.md change and rollback",
                        }
                        for criterion in package_of(request)["mission_success_criteria"]
                    ],
                }
            )
            + "</critic_verdict>"
        )

    provider.extend("critic", [("workspace_read_file", {"path": "CHANGE.md"}), critic])
    return provider


def configuration(root):
    return OrchestratorConfig(
        evidence_root=root,
        lease_seconds=2,
        sdk_lease_ttl_seconds=0.5,
        default_max_output_tokens=1000,
        max_output_tokens_ceiling=1000,
        max_model_calls_per_turn=8,
        max_tool_calls_per_turn=8,
        turn_deadline_seconds=8,
        planner_reserve_tokens=20_000,
        critic_reserve_tokens=70_000,
        attempt_reserve_tokens=20_000,
        deployment_policy=DeploymentPolicy(
            allowed_tools=TOOLS,
            local_code_execution=False,
            enabled_connectors=("test_config",),
            connector_timeout_seconds=1,
        ),
    )


def profiles_for(provider):
    return {
        name: RuntimeProfile(
            name,
            provider,
            "agent-model",
            default_max_output_tokens=1000,
            max_output_tokens_ceiling=1000,
            context_policy=ContextPolicy(max_input_tokens=65_536, output_reserve=1000),
        )
        for name in ("default", "critic")
    }


def runtime(root, provider, service):
    return Orchestrator(
        configuration(root),
        profiles=profiles_for(provider),
        routing=RoutingRules("default", by_role={"critic": "critic"}),
        connectors={"test_config": service},
        provider_token_estimator=FixtureInputBound(),
        poll_interval=0.002,
    )


def action_identity(action):
    return {
        key: action[key]
        for key in (
            "action_key",
            "action_id",
            "version",
            "mission_id",
            "task_id",
            "attempt_id",
            "result_id",
            "artifact_id",
            "artifact_hash",
            "params_hash",
            "idempotency_key",
            "approval_request_id",
            "decision_receipts",
        )
    }


def inventory(orch):
    return {
        "attempts": [
            a.id
            for m in orch.store.list_missions()
            for t in orch.store.list_tasks(m.id)
            for a in orch.store.list_attempts(t.id)
        ],
        "invocations": {
            name: [
                list(row)
                for row in pool.runtime.uow.database.connection.execute(
                    "SELECT invocation_id,state,handoff_attempt FROM provider_invocations "
                    "ORDER BY invocation_id"
                )
            ]
            for name, pool in orch.assembled.pools.items()
        },
        "artifacts": {
            a.id: hashlib.sha256(read_verified(a)).hexdigest()
            for a in orch.store.list_all_artifacts()
        },
    }


def reservation(orch, action_key):
    row = orch.store.connection.execute(
        "SELECT state,reserved_tool_calls,settled_tool_calls FROM budget_reservations "
        "WHERE subject_id=?",
        (f"action:{action_key}",),
    ).fetchone()
    assert row is not None, "UNKNOWN external action lost its reservation"
    return list(row)


def check_external(service, original):
    assert service.state() == original["external_state"]
    assert service.state()["applied_count"] == 1
    calls = [
        json.loads(line) for line in (service.external / "execute.jsonl").read_text().splitlines()
    ]
    assert calls == [{"key": original["identity"]["idempotency_key"], "pid": original["pid"]}]


def check_unknown(orch, service, provider, original):
    key = original["identity"]["action_key"]
    (action,) = orch.store.list_actions(original["identity"]["mission_id"])
    assert action_identity(action) == original["identity"]
    assert action["state"] == "UNKNOWN" and action["reconcile"] == "STILL_UNKNOWN"
    assert action["needs_human"] and action["receipt"] is None and action["handoffs"] == 1
    held = reservation(orch, key)
    assert held[0] == "RESERVED" and held[1:] == [1, None], held
    events = orch.store.list_events(action["mission_id"])
    assert any(
        e.type == "ReservationHeld"
        and e.payload.get("subject_id") == f"action:{key}"
        and e.payload.get("reason") == "action_outcome_unknown"
        for e in events
    )
    assert any(
        e.type == "ActionReconciled"
        and e.payload.get("action_key") == key
        and e.payload.get("verdict") == "STILL_UNKNOWN"
        for e in events
    )
    assert inventory(orch) == original["inventory"]
    assert provider.calls == 0, "cold runtime called the empty Provider"
    assert service.lookup_keys and set(service.lookup_keys) == {
        original["identity"]["idempotency_key"]
    }
    check_external(service, original)


@contextmanager
def host_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".instance.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def crash(root, external, marker):
    provider = fixture_provider()
    service = ReceiptGate(external, crash_marker=marker)
    orch = runtime(root, provider, service)
    with host_lock(root), phase("submit, fixture work, human approval, actual apply"):
        await orch.__aenter__()
        mission = await orch.submit_mission(
            MissionSpec(
                goal=APPROVAL_SPEC["goal"],
                success_criteria=(*APPROVAL_SPEC["success_criteria"], CRITERION),
                tenant_id="p35-action-owner",
                idempotency_key="p35-action-cold-backup",
                allowed_tools=TOOLS,
                workspace_seed=APPROVAL_SEED,
                budget=Budget(max_tokens=300_000, max_attempts=8, max_tool_calls=20),
            )
        )
        await orch.run()
        (action,) = orch.store.list_actions(mission.id)
        assert action["receipt"] is None and action["handoffs"] == 0
        assert service.state()["applied_count"] == 0
        api = MissionControlV1(orch, tenant_id=mission.tenant_id, principal=Principal("p35-human"))
        api.decide(action["approval_request_id"], "approve", nonce="p35-human-approve")
        approved = orch.store.get_action(action["action_key"])
        assert approved["state"] == "APPROVED"
        # decision_receipts are copied into the action only by begin_handoff.
        expected_identity = action_identity(
            {
                **approved,
                "decision_receipts": [
                    d["receipt_hash"]
                    for d in orch.store.list_decisions(action["approval_request_id"])
                ],
            }
        )
        assert provider.by_role == {"planner": 1, "worker": 5, "critic": 2}, provider.by_role
        worker_intent = orch.store.get_intent_for_subject(action["attempt_id"])
        critic_intent = orch.store.get_intent_for_subject(f"{action['attempt_id']}:critic:1")
        assert worker_intent is not None and critic_intent is not None
        first = worker_intent.config["first_critic_budget"]
        assert first["minimum_tokens"] == 65_536 + 1000
        assert first["provider_input_cap"] == critic_intent.config["provider_input_cap"]
        assert first["output_ceiling"] == critic_intent.config["provider_output_ceiling"] == 1000
        actual_inventory = inventory(orch)
        assert all(actual_inventory["invocations"].values())
        assert sum(map(len, actual_inventory["invocations"].values())) == 8
        assert all(
            row[1:] == ["succeeded", 1]
            for rows in actual_inventory["invocations"].values()
            for row in rows
        )
        assert all(str(t.status) == "COMPLETED" for t in orch.store.list_tasks(mission.id))
        assert actual_inventory["artifacts"]
        service.boundary = {
            "identity": expected_identity,
            "inventory": actual_inventory,
            "provider_calls": provider.calls,
        }
        await orch.run()
        raise AssertionError("parent missed the durable external-apply crash boundary")


async def recovered(root, external, original, *, settle):
    provider = RoleScriptedProvider({})
    service = ReceiptGate(external)
    orch = runtime(root, provider, service)
    with host_lock(root):
        try:
            with phase("cold production runtime and STILL_UNKNOWN hold"):
                await orch.__aenter__()
                await orch.run()
                check_unknown(orch, service, provider, original)
                await orch.run()
                check_unknown(orch, service, provider, original)
            if settle:
                with phase("authoritative original receipt reconciliation"):
                    (external / "allow-lookup").touch()
                    await orch.run()
                    action = orch.store.get_action(original["identity"]["action_key"])
                    assert action_identity(action) == original["identity"]
                    assert action["state"] == "SUCCEEDED" and action["handoffs"] == 1
                    assert action["receipt"] == original["receipt"]
                    assert (
                        service.lookup(action["idempotency_key"]).to_json() == original["receipt"]
                    )
                    assert reservation(orch, action["action_key"]) == ["SETTLED", 1, 1]
                    assert (
                        orch.store.get_mission(action["mission_id"]).status
                        is MissionStatus.COMPLETED
                    )
                    await orch.run()
                    completed = [
                        e
                        for e in orch.store.list_events(action["mission_id"])
                        if e.type == "ActionReconciled" and e.payload.get("verdict") == "COMPLETED"
                    ]
                    assert len(completed) == 1
                    assert inventory(orch) == original["inventory"] and provider.calls == 0
                    check_external(service, original)
        finally:
            with phase("safe runtime close", CLEANUP_SECONDS):
                await orch.__aexit__(None, None, None)


def main():
    # Independent hard stop catches cancellation-resistant threads/async teardown.
    watchdog = threading.Timer(24, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    mode, base_text = sys.argv[1:]
    base = Path(base_text)
    root, external = base / "original", base / "external"
    marker = base / "crash.json"
    started = time.monotonic()
    if mode == "crash":
        asyncio.run(crash(root, external, marker))
        return
    original = json.loads(marker.read_text())
    if mode == "cold-backup":
        asyncio.run(recovered(root, external, original, settle=False))
        with phase("coordinated offline backup"):
            identity = backup.BackupSourceIdentity(
                **json.loads((base / "source-identity.json").read_text())
            )
            receipt = backup.backup_offline(
                configuration(root),
                profiles_for(RoleScriptedProvider({})),
                WorkspaceManager(configuration(root).workspaces_root),
                instance_lock_path=root / ".instance.lock",
                destination=base / "bundle",
                source_identity=identity,
            )
            write_json(base / "backup.json", receipt)
    elif mode == "restore":
        assert root.is_file(), "original root must be inaccessible before isolated restore"
        restored = base / "restored"
        with phase("isolated production restore"):
            receipt = json.loads((base / "backup.json").read_text())
            backup.restore_offline(
                base / "bundle",
                destination=restored,
                expected_manifest_sha256=receipt["manifest_sha256"],
            )
        asyncio.run(recovered(restored, external, original, settle=True))
        assert root.is_file(), "runtime unexpectedly recreated the original storage root"
    else:
        raise AssertionError(f"unknown mode {mode}")
    write_json(base / f"{mode}-done.json", {"elapsed_seconds": time.monotonic() - started})


if __name__ == "__main__":
    main()
