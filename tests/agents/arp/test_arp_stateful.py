# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""RP-E2: a small stateful random test (TEST-PLAN §3, reduced by the user's 2026-09-23 decision).

Six actions on up to three Agents — create, turn, settings update, destroy, tick, crash /
restart — are drawn at random; after every step the real library is compared with a tiny
reference ledger that only knows each Session's state, generation, adoption revision and
effective policy.  Any divergence prints the seed and the action history so the sequence can
be frozen into an ordinary regression test.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from arp_fixture import build, trusted_caller
from provider_fixture import ScriptedProvider

from simple_harness.agents import AgentConfig
from simple_harness.agents.arp import store
from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.contracts import AgentTurnState
from simple_harness.api import RuntimePlaneService

CONFIG = AgentConfig(name="w", instructions="你是助手。", model_profile_ref="p")
ORDER = {"ACTIVE": 0, "DRAINING": 1, "PURGING": 2, "PURGED": 3}
WEIGHTED = ["create"] + ["turn"] * 3 + ["settings"] * 2 + ["destroy"] + ["tick"] * 2 + ["restart"]
SLOTS = 3
STEPS = 400
SEEDS = (11, 23, 37, 41, 59)


class Slot:
    def __init__(self, key: str) -> None:
        self.key = key
        self.agent = None
        self.agent_id: str | None = None
        self.session_id: str | None = None
        self.state = "ACTIVE"
        self.generation = 0
        self.adoption = 0
        self.effective: dict | None = None

    @property
    def exists(self) -> bool:
        return self.session_id is not None


def _req(verb: str, payload, *, subject: str, command_id: str | None = None, expected_revision: int | None = None) -> dict:  # type: ignore[no-untyped-def]
    return {"schema_version": 1, "verb": verb, "command_id": command_id, "subject_id": subject, "expected_revision": expected_revision, "cursor": None, "limit": 8, "payload_ref": None, "payload": payload}


class World:
    def __init__(self, tmp_path, seed: int) -> None:  # type: ignore[no-untyped-def]
        self.tmp_path = tmp_path
        self.rng = random.Random(seed)
        self.seed = seed
        self.provider = ScriptedProvider(["好的。"] * (STEPS + 8))
        self.slots = [Slot(f"k{i}") for i in range(SLOTS)]
        self.policies: list[dict] | None = None  # [p1, p2] once the second revision was submitted
        self.history: list[str] = []
        self.runtime = None
        self.service = None
        self.caller = trusted_caller("host")

    # ---- runtime lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        self.runtime = build(self.tmp_path, self.provider)
        await self.runtime.__aenter__()
        self.service = RuntimePlaneService(self.runtime)
        for slot in self.slots:
            if slot.exists:
                try:
                    slot.agent = await self.runtime.open(slot.agent_id)
                except Exception:  # noqa: BLE001  (a purged Agent may not reopen; that is fine)
                    slot.agent = None

    async def stop(self) -> None:
        await self.runtime.__aexit__(None, None, None)
        try:
            self.runtime.uow.database.close()
        except Exception:  # noqa: BLE001
            pass
        self.runtime = None

    @property
    def connection(self):  # type: ignore[no-untyped-def]
        return self.runtime.uow.database.connection

    def fail(self, message: str) -> None:
        raise AssertionError(f"seed {self.seed} step {len(self.history)} history {self.history[-12:]}: {message}")

    # ---- reference ledger check ----------------------------------------------------------------

    def check(self) -> None:
        for slot in self.slots:
            if not slot.exists:
                continue
            row = store.read_session(self.connection, slot.session_id)
            if row is None:
                self.fail(f"{slot.key}: session row vanished")
            # The runtime's background pump ticks the native plane between awaits, so a
            # DRAINING / PURGING session may move forward at any step; it never moves back,
            # an ACTIVE session never leaves ACTIVE without a destroy, PURGED is final.
            if slot.state in ("DRAINING", "PURGING"):
                if ORDER[row.state] < ORDER[slot.state]:
                    self.fail(f"{slot.key}: {slot.state} went backwards to {row.state}")
                slot.state = row.state
            elif row.state != slot.state:
                self.fail(f"{slot.key}: state {row.state} != ledger {slot.state}")
            if row.generation != slot.generation:
                self.fail(f"{slot.key}: generation {row.generation} != ledger {slot.generation}")
            adoption = store.latest_adoption(self.connection, slot.session_id)
            if adoption is None or adoption.adoption_revision != slot.adoption or adoption.policy_ref.to_json() != slot.effective:
                self.fail(f"{slot.key}: adoption {adoption and adoption.adoption_revision} != ledger {slot.adoption}")
            if row.state == "PURGED" and (self.runtime.arp.root.directory / row.relative_directory).exists():
                self.fail(f"{slot.key}: PURGED but the directory still exists")

    # ---- actions -----------------------------------------------------------------------------

    async def create(self, slot: Slot) -> None:
        try:
            agent = await self.runtime.create(CONFIG, creation_key=slot.key, caller=trusted_caller())
        except ArpError as error:
            if not slot.exists or slot.state == "ACTIVE":
                self.fail(f"{slot.key}: create refused ({error.code}) although the key is free or ACTIVE")
            return
        if slot.exists:
            if slot.state != "ACTIVE":
                self.fail(f"{slot.key}: create re-used the key of a {slot.state} session")
            if agent.agent_id != slot.agent_id:
                self.fail(f"{slot.key}: same creation key gave another agent")
            return
        row = store.read_live_session(self.connection, agent.agent_id)
        adoption = store.latest_adoption(self.connection, row.session_id)
        slot.agent, slot.agent_id, slot.session_id = agent, agent.agent_id, row.session_id
        slot.state, slot.generation = row.state, row.generation
        slot.adoption, slot.effective = adoption.adoption_revision, adoption.policy_ref.to_json()

    async def turn(self, slot: Slot, step: int) -> None:
        if not slot.exists or slot.agent is None:
            return
        try:
            receipt = await slot.agent.submit(f"第 {step} 步。", input_id=f"s{self.seed}-{step}")
            result = await slot.agent.wait_turn(receipt.turn_id, timeout=10)
        except Exception:  # noqa: BLE001
            if slot.state == "ACTIVE":
                self.fail(f"{slot.key}: an ACTIVE session refused a turn")
            return
        if slot.state == "ACTIVE":
            if result.state is not AgentTurnState.COMMITTED:
                self.fail(f"{slot.key}: turn ended {result.state} on an ACTIVE session")
        elif result.state is AgentTurnState.COMMITTED:
            self.fail(f"{slot.key}: a {slot.state} session committed a new turn")

    async def settings(self, slot: Slot, step: int) -> None:
        if not slot.exists:
            return
        if self.policies is None:
            got = await self.service.handle(_req("agent_context_settings_get", {"session_id": slot.session_id}, subject=slot.agent_id), caller=self.caller)
            if got["error"] is not None:
                return  # a non-ACTIVE session cannot bootstrap the second policy revision
            view = got["items"][0]
            policy = dict(view["configured_policy"])
            policy["recall_max_tokens"] += 8
            submitted = await self.service.handle(
                _req("agent_context_policy_submit", {"schema_version": 1, "policy": policy, "expected_policy_ref": view["effective_policy_ref"]}, subject=slot.agent_id, command_id=f"p{self.seed}", expected_revision=0),
                caller=self.caller,
            )
            if submitted["error"] is not None:
                self.fail(f"policy submit refused: {submitted['error']}")
            self.policies = [view["effective_policy_ref"], submitted["items"][0]["policy_ref"]]
        candidate = self.policies[1] if slot.effective == self.policies[0] else self.policies[0]
        payload = {"schema_version": 1, "session_id": slot.session_id, "candidate_policy_ref": candidate, "expected_effective_policy_ref": slot.effective, "expected_adoption_revision": slot.adoption}
        response = await self.service.handle(_req("agent_context_settings_update", payload, subject=slot.agent_id, command_id=f"u{self.seed}-{step}", expected_revision=slot.adoption), caller=self.caller)
        if slot.state == "ACTIVE":
            if response["error"] is not None:
                self.fail(f"{slot.key}: settings update refused on ACTIVE: {response['error']}")
            slot.adoption += 1
            slot.effective = candidate
            if response["items"][0]["adoption_revision"] != slot.adoption:
                self.fail(f"{slot.key}: adoption {response['items'][0]['adoption_revision']} != {slot.adoption}")
        elif response["error"] is None:
            self.fail(f"{slot.key}: settings update applied to a {slot.state} session")

    async def destroy(self, slot: Slot, step: int) -> None:
        if not slot.exists:
            return
        command = {
            "schema_version": 1, "session_id": slot.session_id, "expected_generation": slot.generation, "command_id": f"d{self.seed}-{step}",
            "reason": "USER_DESTROY", "retention_policy_ref": self.runtime.arp.ports.profile.refs.retention_policy_ref.to_json(),
        }
        try:
            after = await self.runtime.arp.sessions.destroy(command, caller=trusted_caller(f"d{step}"), command_id=command["command_id"])
        except ArpError:
            if slot.state == "ACTIVE":
                self.fail(f"{slot.key}: destroy of an ACTIVE session was refused")
            return
        if slot.state != "ACTIVE":
            self.fail(f"{slot.key}: a second destroy command was accepted on a {slot.state} session")
        if after.state not in ("DRAINING", "PURGING") or after.generation != slot.generation + 1:
            self.fail(f"{slot.key}: destroy produced {after.state} gen {after.generation}")
        slot.state, slot.generation = after.state, after.generation
        slot.agent = None

    def tick(self) -> None:
        self.runtime.arp.tick()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ---- driver ------------------------------------------------------------------------------

    async def run(self) -> None:
        await self.start()
        try:
            for step in range(STEPS):
                action = self.rng.choice(WEIGHTED)
                slot = self.rng.choice(self.slots)
                self.history.append(f"{action}:{slot.key}")
                if action == "create":
                    await self.create(slot)
                elif action == "turn":
                    await self.turn(slot, step)
                elif action == "settings":
                    await self.settings(slot, step)
                elif action == "destroy":
                    await self.destroy(slot, step)
                elif action == "tick":
                    self.tick()
                else:
                    await self.restart()
                self.check()
        finally:
            if self.runtime is not None:
                await self.stop()


@pytest.mark.parametrize("seed", SEEDS)
def test_random_action_sequences_agree_with_the_reference_ledger(tmp_path, seed) -> None:
    asyncio.run(World(tmp_path, seed).run())
