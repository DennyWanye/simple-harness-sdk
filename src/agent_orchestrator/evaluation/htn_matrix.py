# SPDX-License-Identifier: Apache-2.0
"""Frozen H8 episode definitions, physical budget context and resumable run ledger.

The old boolean matrix helper is not an acceptance runner. This ledger requires
actual episode receipts and file hashes; an interruption never becomes a pass or
silently starts a fresh episode over an existing world.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from simple_harness.contracts import canonical_json
from ..contracts.models import ContractError
from ..contracts.semantic_base import content_hash_of
from ..planning.htn.cross_domain_acceptance import FourArm, ScenarioKind
from .experiment import ExecutionCounters, ExperimentBudget

DOMAINS = ("code-v1", "appworld-v1", "drone-sim-v1")
COUNTS = {ScenarioKind.NORMAL: 8, ScenarioKind.REPAIR: 4,
          ScenarioKind.EVIDENCE_CONFLICT: 2, ScenarioKind.RECOVERY: 2}


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    domain: str
    kind: ScenarioKind
    fixture: Mapping[str, Any]
    oracle: Mapping[str, Any]
    intervention: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.domain not in DOMAINS or not self.scenario_id.strip():
            raise ContractError("invalid H8 scenario identity")
        object.__setattr__(self, "kind", ScenarioKind(self.kind))
        if not self.fixture or not self.oracle:
            raise ContractError("an H8 scenario requires actual fixture and independent oracle definitions")
        if self.kind != ScenarioKind.NORMAL and not self.intervention:
            raise ContractError("repair/conflict/recovery scenarios require a concrete intervention")
        if self.kind == ScenarioKind.NORMAL and self.intervention:
            raise ContractError("normal scenario cannot hide an intervention")
        if self.domain == "appworld-v1" and not self.fixture.get("task_id"):
            raise ContractError("AppWorld scenario must bind a real dataset task")

    def to_json(self) -> dict[str, Any]:
        return json.loads(canonical_json(asdict(self)))


@dataclass(frozen=True, slots=True)
class H8Manifest:
    experiment_id: str
    provider: str
    model: str
    budget: ExperimentBudget
    physical_slots: int
    scenarios: tuple[ScenarioDefinition, ...]
    domain_deployments: Mapping[str, Mapping[str, Any]]
    source_fingerprint: str
    solver_identity: Mapping[str, Any]
    repetitions: int = 3
    seed: int = 100

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v.strip() for v in
                   (self.experiment_id, self.provider, self.model, self.source_fingerprint)):
            raise ContractError("H8 needs explicit experiment/provider/model/source identities")
        if type(self.physical_slots) is not int or self.physical_slots < 1:
            raise ContractError("H8 physical concurrency must be positive")
        if type(self.repetitions) is not int or self.repetitions < 3 or type(self.seed) is not int or self.seed < 0:
            raise ContractError("H8 requires at least three frozen repetitions and a nonnegative seed")
        if not isinstance(self.budget, ExperimentBudget):
            raise ContractError("H8 needs the real physical provider budget")
        if len({s.scenario_id for s in self.scenarios}) != len(self.scenarios):
            raise ContractError("H8 scenario IDs must be unique")
        if Counter((s.domain, s.kind) for s in self.scenarios) != Counter({
            (domain, kind): count for domain in DOMAINS for kind, count in COUNTS.items()}):
            raise ContractError("H8 requires 8 normal/4 repair/2 conflict/2 recovery in each of three domains")
        if set(self.domain_deployments) != set(DOMAINS):
            raise ContractError("H8 needs a frozen deployment for each domain")
        for deployed in self.domain_deployments.values():
            if not all(deployed.get(key) for key in ("tool_schemas", "domain_package_hash", "environment_identity",
                    "executor_version", "runtime_config_hash", "context_profile_identity", "completion_contract")):
                raise ContractError("freeze actual tools, domain, environment, runtime, context and completion contracts")
            if type(deployed.get("recursion_fuel")) is not int or deployed["recursion_fuel"] < 1:
                raise ContractError("freeze positive domain recursion fuel")
        if not self.solver_identity.get("backend_id") or not self.solver_identity.get("limits"):
            raise ContractError("H-solver requires its actual backend identity and limits")
        # Freeze the nested inputs too: serialization at construction detaches the
        # caller; assert_unchanged detects subsequent mutation before every run.
        for name in ("domain_deployments", "solver_identity"):
            object.__setattr__(self, name, json.loads(canonical_json(dict(getattr(self, name)))))

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": 1, "experiment_id": self.experiment_id,
                "provider": self.provider, "model": self.model, "budget": asdict(self.budget),
                "physical_slots": self.physical_slots, "source_fingerprint": self.source_fingerprint,
                "domain_deployments": dict(self.domain_deployments), "solver_identity": dict(self.solver_identity),
                "repetitions": self.repetitions, "seed": self.seed,
                "scenarios": [s.to_json() for s in self.scenarios], "arms": [str(a) for a in FourArm]}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> H8Manifest:
        """Load a frozen manifest without filling in any deployment facts.

        The batch entry point deliberately accepts only the complete serialized
        form emitted by :meth:`to_json`; provider, model, budgets, benchmark
        tasks, tool schemas, environments, source and solver identity all stay
        caller-owned frozen inputs.
        """
        body = dict(raw)
        if body.pop("schema_version", None) != 1:
            raise ContractError("unsupported H8 manifest schema")
        if body.pop("arms", None) != [str(arm) for arm in FourArm]:
            raise ContractError("H8 manifest must freeze the complete four-arm order")
        expected = {
            "experiment_id", "provider", "model", "budget", "physical_slots",
            "source_fingerprint", "domain_deployments", "solver_identity",
            "repetitions", "seed", "scenarios",
        }
        if set(body) != expected:
            raise ContractError("H8 manifest fields are incomplete or unknown")
        budget = body.pop("budget")
        scenarios = body.pop("scenarios")
        if not isinstance(budget, Mapping) or not isinstance(scenarios, list):
            raise ContractError("H8 manifest budget/scenarios are malformed")
        try:
            frozen_budget = ExperimentBudget(**dict(budget))
            frozen_scenarios = tuple(
                ScenarioDefinition(
                    scenario_id=item["scenario_id"],
                    domain=item["domain"],
                    kind=ScenarioKind(item["kind"]),
                    fixture=item["fixture"],
                    oracle=item["oracle"],
                    intervention=item.get("intervention"),
                )
                for item in scenarios
                if isinstance(item, Mapping)
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError("H8 manifest contains malformed frozen values") from error
        if len(frozen_scenarios) != len(scenarios):
            raise ContractError("H8 manifest scenario entry is malformed")
        for scenario in frozen_scenarios:
            if scenario.domain != "appworld-v1":
                continue
            if not all(
                isinstance(scenario.fixture.get(key), str) and scenario.fixture[key].strip()
                for key in ("task_id", "goal", "dataset_hash", "app")
            ):
                raise ContractError("H8 AppWorld scenarios must retain real frozen dataset facts")
        try:
            return cls(budget=frozen_budget, scenarios=frozen_scenarios, **body)
        except TypeError as error:
            raise ContractError("H8 manifest contains malformed frozen values") from error

    @property
    def fingerprint(self) -> str:
        return content_hash_of(self.to_json())

    def runs(self) -> tuple[H8Run, ...]:
        runs = []
        arms = tuple(FourArm)
        for index, scenario in enumerate(self.scenarios):
            for trial in range(self.repetitions):
                offset = (index + trial) % len(arms)
                for arm in arms[offset:] + arms[:offset]:
                    run_id = content_hash_of([self.fingerprint, scenario.scenario_id, str(arm), trial])
                    runs.append(H8Run(run_id, scenario, arm, trial, self.seed + trial))
        return tuple(runs)


@dataclass(frozen=True, slots=True)
class H8Run:
    run_id: str
    scenario: ScenarioDefinition
    arm: FourArm
    trial: int
    seed: int


@dataclass(frozen=True, slots=True)
class H8MeterContext:
    manifest: H8Manifest
    run: H8Run
    report_usage: Callable[[ExecutionCounters], None]


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    relative_path: str
    sha256: str

    def verify(self, root: Path) -> None:
        import hashlib
        path = (root / self.relative_path).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ContractError("episode evidence must be a local file within its run directory")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != self.sha256:
            raise ContractError("episode evidence file differs from the recorded hash")


@dataclass(frozen=True, slots=True)
class EpisodeReceipt:
    run_id: str
    manifest_hash: str
    terminal_status: str
    domain_success: bool
    domain_score_ref: EvidenceFile
    runtime_receipt_ref: EvidenceFile
    actual_provider: str
    actual_model: str
    tools_hash: str
    counters: ExecutionCounters
    unresolved_operations: int
    critical_effect_failures: int
    unknown_usage_calls: int
    elapsed_seconds: float
    formal_completion_id: str | None
    intervention_receipt_ref: EvidenceFile | None = None
    solver_statuses: tuple[str, ...] = ()
    intervention_triggered: bool = False

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> EpisodeReceipt:
        body = dict(raw)
        for key in ("domain_score_ref", "runtime_receipt_ref", "intervention_receipt_ref"):
            if body.get(key) is not None:
                body[key] = EvidenceFile(**body[key])
        body["counters"] = ExecutionCounters(**body["counters"])
        body["solver_statuses"] = tuple(body.get("solver_statuses", ()))
        return cls(**body)

    def verify(self, manifest: H8Manifest, run: H8Run, root: Path) -> None:
        if self.run_id != run.run_id or self.manifest_hash != manifest.fingerprint:
            raise ContractError("episode receipt is bound to a different frozen run")
        if (self.actual_provider, self.actual_model) != (manifest.provider, manifest.model):
            raise ContractError("actual model/provider differs from the frozen four-arm deployment")
        if (self.counters.provider, self.counters.model) != (manifest.provider, manifest.model):
            raise ContractError("physical counters disagree with the provider identity")
        if self.tools_hash != content_hash_of(manifest.domain_deployments[run.scenario.domain]["tool_schemas"]):
            raise ContractError("one arm used a different domain tool surface")
        for count in (self.unresolved_operations, self.critical_effect_failures, self.unknown_usage_calls):
            if type(count) is not int or count < 0:
                raise ContractError("episode failure counts must be actual nonnegative integers")
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0 or type(self.domain_success) is not bool:
            raise ContractError("episode result is malformed")
        for evidence in (self.domain_score_ref, self.runtime_receipt_ref, self.intervention_receipt_ref):
            if evidence is not None:
                evidence.verify(root)
        if run.scenario.kind != ScenarioKind.NORMAL and self.intervention_receipt_ref is None:
            raise ContractError("non-normal scenario has no actual intervention evidence")
        if type(self.intervention_triggered) is not bool:
            raise ContractError("intervention trigger must be an actual boolean")
        if self.intervention_receipt_ref is not None:
            actual = json.loads((root / self.intervention_receipt_ref.relative_path).read_text())
            if actual.get("triggered") is not self.intervention_triggered:
                raise ContractError("intervention trigger disagrees with its evidence")
            if run.scenario.kind == ScenarioKind.RECOVERY and self.intervention_triggered:
                if (actual.get("kind") != "process_kill_recovery"
                        or actual.get("kill_returncode") != -9
                        or not actual.get("killed_pid") or not actual.get("resumed_pid")
                        or actual["killed_pid"] == actual["resumed_pid"]
                        or actual.get("manifest_hash") != manifest.fingerprint):
                    raise ContractError("recovery requires an original SIGKILL/cold-process receipt")

    def passed(self, manifest: H8Manifest, run: H8Run) -> bool:
        budget = manifest.budget
        return (self.domain_success and self.terminal_status == "COMPLETED"
            and (run.scenario.kind == ScenarioKind.NORMAL or self.intervention_triggered)
            and (run.arm == FourArm.STRONG_SINGLE_AGENT or bool(self.formal_completion_id))
            and self.counters.calls > 0 and self.counters.calls <= budget.calls
            and self.counters.input_tokens <= budget.input_tokens
            and self.counters.output_tokens <= budget.output_tokens
            and self.counters.total_tokens <= budget.total_tokens
            and self.counters.peak_physical_slots <= manifest.physical_slots
            and self.elapsed_seconds <= budget.seconds
            and not (self.unresolved_operations or self.critical_effect_failures or self.unknown_usage_calls)
            and (run.arm != FourArm.H_SOLVER or bool(self.solver_statuses)
                 and all(status == "SOLVED" for status in self.solver_statuses)))


class EpisodeExecutor(Protocol):
    async def execute(self, manifest: H8Manifest, run: H8Run, root: Path, *, resume: bool) -> EpisodeReceipt: ...


class H8RunLedger:
    """Persist running identity before execution; never reuse a failed run as a pass."""
    def __init__(self, root: Path, manifest: H8Manifest):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        self.fingerprint = manifest.fingerprint
        self.db = sqlite3.connect(self.root / "matrix.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS manifest (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                receipt_json TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 1);
        """)
        with self.db:
            prior = self.db.execute("SELECT hash FROM manifest WHERE id=1").fetchone()
            if prior and prior[0] != self.fingerprint:
                raise ContractError("cannot change a running H8 experiment's frozen manifest")
            self.db.execute("INSERT OR IGNORE INTO manifest VALUES(1,?,?)",
                            (self.fingerprint, canonical_json(manifest.to_json())))

    def close(self) -> None:
        self.db.close()

    async def run(self, executor: EpisodeExecutor, *, resume_interrupted: bool = False,
                  on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, int]:
        # A process lock guards the entire run; SQLite alone cannot protect an
        # external AppWorld environment or an in-flight physical provider call.
        from .experiment import _exclusive
        with _exclusive(self.root / "runner.lock"):
            for run in self.manifest.runs():
                if self.manifest.fingerprint != self.fingerprint:
                    raise ContractError("H8 manifest changed during execution")
                row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run.run_id,)).fetchone()
                if row and row["state"] in {"PASS", "FAIL"}:
                    prior = EpisodeReceipt.from_json(json.loads(row["receipt_json"]))
                    prior.verify(self.manifest, run, self.root / "episodes" / run.run_id)
                    if ("PASS" if prior.passed(self.manifest, run) else "FAIL") != row["state"]:
                        raise ContractError("stored H8 outcome disagrees with its original receipt")
                    continue
                resume = row is not None
                if resume and not resume_interrupted:
                    raise ContractError("an interrupted episode requires explicit recovery of its original runtime")
                directory = self.root / "episodes" / run.run_id
                directory.mkdir(parents=True, exist_ok=resume)
                with self.db:
                    self.db.execute("INSERT INTO runs(run_id,state) VALUES(?,'RUNNING') "
                        "ON CONFLICT(run_id) DO UPDATE SET state='RUNNING',attempts=attempts+1", (run.run_id,))
                try:
                    receipt = await executor.execute(self.manifest, run, directory, resume=resume)
                    receipt.verify(self.manifest, run, directory)
                    state = "PASS" if receipt.passed(self.manifest, run) else "FAIL"
                    with self.db:
                        self.db.execute("UPDATE runs SET state=?,receipt_json=?,error=NULL WHERE run_id=?",
                            (state, canonical_json(json.loads(json.dumps(asdict(receipt), allow_nan=False))), run.run_id))
                except BaseException as error:
                    # Persist type only: exception text can contain a provider URL,
                    # credentials or private response content. Original state remains.
                    with self.db:
                        self.db.execute("UPDATE runs SET state='INTERRUPTED',error=? WHERE run_id=?",
                                        (type(error).__name__, run.run_id))
                    raise
                if on_progress:
                    on_progress({"run_id": run.run_id, "scenario": run.scenario.scenario_id,
                                 "arm": str(run.arm), "trial": run.trial, "status": state})
        return {row[0]: row[1] for row in self.db.execute("SELECT state,COUNT(*) FROM runs GROUP BY state")}
