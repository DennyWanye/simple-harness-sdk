# SPDX-License-Identifier: Apache-2.0
"""Local drone simulation with durable episode state and idempotent command receipts.

No hardware, network or shell is used. The simulator mutates only its own SQLite
world. Mission IDs and call IDs come from the gateway's trusted binding.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..runtime.domain_tools import DomainTool

DOMAIN_ID = "drone-sim-v1"
PREDICATES = ("connected", "battery-safe", "zone-clear", "airborne", "landed", "capture-exists")


class DroneSimulator:
    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    mission_id TEXT PRIMARY KEY, initial_hash TEXT NOT NULL, state_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                    mission_id TEXT NOT NULL REFERENCES episodes(mission_id), call_id TEXT NOT NULL,
                    command_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
                    PRIMARY KEY(mission_id, call_id));
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_episode(self, mission_id: str, *, vehicle: str = "sim-1", battery: int = 100,
                       zone_clear: bool = True) -> dict[str, Any]:
        if not mission_id.strip() or not vehicle.strip() or type(battery) is not int or not 0 <= battery <= 100 or type(zone_clear) is not bool:
            raise ContractError("invalid simulator episode")
        initial: dict[str, Any] = {"vehicle": vehicle, "battery": battery, "zone_clear": zone_clear,
                   "connected": True, "airborne": False, "x": 0, "y": 0, "z": 0,
                   "captures": [], "version": 1, "fault": None}
        digest = content_hash_of(initial)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT initial_hash,state_json FROM episodes WHERE mission_id=?", (mission_id,)).fetchone()
            if row is not None:
                if row["initial_hash"] != digest:
                    raise ContractError("simulator episode initialization changed on replay")
                return json.loads(row["state_json"])
            db.execute("INSERT INTO episodes VALUES(?,?,?)", (mission_id, digest, canonical_json(initial)))
        return initial

    def read(self, mission_id: str, vehicle: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute("SELECT state_json FROM episodes WHERE mission_id=?", (mission_id,)).fetchone()
        if row is None:
            raise ContractError("simulator episode is not bound to this Mission")
        state = json.loads(row["state_json"])
        if state["vehicle"] != vehicle:
            raise ContractError("vehicle is outside the bound simulator episode")
        return state

    def set_fault(self, mission_id: str, fault: str | None) -> None:
        """Trusted experiment control; deliberately absent from model tools."""
        if fault not in {None, "unreachable", "blocked_zone", "camera_failure", "motor_failure"}:
            raise ContractError("unknown simulation fault")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json FROM episodes WHERE mission_id=?", (mission_id,)).fetchone()
            if row is None:
                raise ContractError("simulator episode does not exist")
            state = json.loads(row[0]); state["fault"] = fault; state["version"] += 1
            db.execute("UPDATE episodes SET state_json=? WHERE mission_id=?", (canonical_json(state), mission_id))

    def command(self, arguments: Mapping[str, Any], mission_id: str, call_id: str) -> Mapping[str, Any]:
        if not call_id:
            raise ContractError("simulation command requires its actual gateway call ID")
        fields = {"vehicle", "command", "expected_version", "x", "y", "z"}
        if set(arguments) - fields or not {"vehicle", "command", "expected_version"} <= set(arguments):
            raise ContractError("invalid simulation command fields")
        command = arguments["command"]
        if command not in {"takeoff", "move", "capture", "land"}:
            raise ContractError("unknown simulation command")
        digest = content_hash_of(dict(arguments))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM commands WHERE mission_id=? AND call_id=?", (mission_id, call_id)).fetchone()
            if prior is not None:
                if prior["command_hash"] != digest:
                    raise ContractError("simulation call ID was reused with different arguments")
                return json.loads(prior["receipt_json"])
            row = db.execute("SELECT state_json FROM episodes WHERE mission_id=?", (mission_id,)).fetchone()
            if row is None:
                raise ContractError("simulator episode is unavailable")
            state = json.loads(row[0])
            if state["vehicle"] != arguments["vehicle"]:
                raise ContractError("command names another vehicle")
            if type(arguments["expected_version"]) is not int or arguments["expected_version"] != state["version"]:
                raise ContractError("read telemetry again: simulator version changed")
            if state["fault"] == "unreachable" or not state["connected"]:
                raise RuntimeError("simulator transport unavailable")
            before = content_hash_of(state)
            if command == "land":
                state.update(airborne=False, z=0)
            else:
                if state["fault"] == "motor_failure" or state["battery"] < 10:
                    raise ContractError("simulated vehicle cannot fly; land or repair the episode")
                if not state["zone_clear"] or state["fault"] == "blocked_zone":
                    raise ContractError("simulated zone is blocked")
                if command == "takeoff":
                    if state["airborne"]:
                        raise ContractError("simulated vehicle is already airborne")
                    state.update(airborne=True, z=1)
                elif command == "move":
                    if not state["airborne"]:
                        raise ContractError("take off before moving")
                    coords: dict[str, Any] = {axis: arguments.get(axis) for axis in ("x", "y", "z")}
                    if any(type(v) not in (int, float) or not math.isfinite(v) for v in coords.values()):
                        raise ContractError("move requires finite x/y/z coordinates")
                    if abs(coords["x"]) > 20 or abs(coords["y"]) > 20 or not 1 <= coords["z"] <= 5:
                        raise ContractError("move leaves the frozen simulation volume")
                    state.update(coords)
                elif command == "capture":
                    if not state["airborne"] or state["fault"] == "camera_failure":
                        raise ContractError("simulated camera cannot capture")
                    state["captures"].append({"capture_id": call_id, "x": state["x"], "y": state["y"], "z": state["z"]})
                state["battery"] -= 5
            state["version"] += 1
            receipt = {"call_id": call_id, "mission_id": mission_id, "command_hash": digest,
                       "command": command, "before_hash": before, "after_hash": content_hash_of(state),
                       "state": state, "simulated": True}
            receipt["receipt_hash"] = content_hash_of(receipt)
            db.execute("UPDATE episodes SET state_json=? WHERE mission_id=?", (canonical_json(state), mission_id))
            db.execute("INSERT INTO commands VALUES(?,?,?,?)", (mission_id, call_id, digest, canonical_json(receipt)))
            return receipt

    def telemetry(self, arguments: Mapping[str, Any], mission_id: str, call_id: str) -> Mapping[str, Any]:
        del call_id
        if set(arguments) != {"vehicle"}:
            raise ContractError("telemetry requires only vehicle")
        state = self.read(mission_id, str(arguments["vehicle"]))
        if state["fault"] == "unreachable":
            raise RuntimeError("simulator transport unavailable")
        return {"state": state, "state_hash": content_hash_of(state), "simulated": True}

    def tools(self) -> dict[str, DomainTool]:
        return {
            "drone_sim_telemetry": DomainTool({"type": "object", "properties": {
                "vehicle": {"type": "string"}}, "required": ["vehicle"], "additionalProperties": False}, self.telemetry, True),
            "drone_sim_command": DomainTool({"type": "object", "properties": {
                "vehicle": {"type": "string"}, "command": {"type": "string", "enum": ["takeoff", "move", "capture", "land"]},
                "expected_version": {"type": "integer", "minimum": 1},
                **{axis: {"type": "number"} for axis in ("x", "y", "z")}},
                "required": ["vehicle", "command", "expected_version"], "additionalProperties": False}, self.command),
        }

    def observer(self, mission_id: str) -> DroneObserver:
        return DroneObserver(self, mission_id)


class DroneObserver:
    observer_id = "drone-sim-telemetry"

    def __init__(self, simulator: DroneSimulator, mission_id: str):
        self.simulator, self.mission_id = simulator, mission_id

    def predicate_ids(self) -> tuple[str, ...]:
        return tuple(f"drone-sim.{name}" for name in PREDICATES)

    def observe(self, signature: Any, arguments: Mapping[str, Any], *, now_ms: int) -> Any:
        from ..planning.htn.observers import COMPLETE_COVERAGE, observed, unavailable
        try:
            state = self.simulator.read(self.mission_id, str(arguments["vehicle"]))
            if state["fault"] == "unreachable":
                raise RuntimeError("simulator telemetry unavailable")
            values = {"connected": state["connected"], "battery-safe": state["battery"] >= 25,
                      "zone-clear": state["zone_clear"] and state["fault"] != "blocked_zone",
                      "airborne": state["airborne"], "landed": not state["airborne"] and state["z"] == 0,
                      "capture-exists": bool(state["captures"])}
            name = signature.predicate_ref.id.removeprefix("drone-sim.")
            if name not in values:
                raise ContractError("unknown simulator predicate")
            return observed(signature, arguments, polarity=bool(values[name]),
                observer_id=self.observer_id, now_ms=now_ms, coverage=COMPLETE_COVERAGE,
                coverage_scope="mission", query_watermark_ms=now_ms,
                observer_version=f"1:{state['version']}:{content_hash_of(state)}")
        except (KeyError, ContractError, RuntimeError, OSError, sqlite3.Error) as error:
            return unavailable(self.observer_id, signature.predicate_ref.id, str(error))


def deployment(config: Any, simulator: DroneSimulator) -> Any:
    """Explicit simulator deployment; default tool permissions include its tools."""
    from dataclasses import replace
    tools = simulator.tools()
    names = tuple(tools)
    policy = replace(config.deployment_policy,
        domain_tools=(*config.deployment_policy.domain_tools, *names),
        domain_read_only_tools=(*config.deployment_policy.domain_read_only_tools, "drone_sim_telemetry"),
        allowed_tools=(*config.deployment_policy.allowed_tools, *names))
    return replace(config, domain_tools={**config.domain_tools, **tools}, deployment_policy=policy)


def planning_world(simulator: DroneSimulator, mission_id: str, *, semantics: Any) -> Any:
    from ..planning.htn.world import build_planning_world
    return build_planning_world(mission_id, domains=(DOMAIN_ID,), semantics=semantics,
        observers=(simulator.observer(mission_id),),
        capability_layers={"drone-sim.read": None, "drone-sim.command": None},
        deployed_layers=("format_check", "rule_check", "critic_review", "human_review"))
