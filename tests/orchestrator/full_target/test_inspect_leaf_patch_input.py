# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3k / defect N1: the leaf that explains the change has to be *handed* the change.

Five Grok batch-2 episodes (C1-r1, C2-r0, C2-r1, C4-r0, C4-r1) synthesised the same
method — facts → reproduce → apply → {verify, inspect} → summarize — and hung
``c-change-explained`` on ``summarize``, fed by ``inspect``.  ``inspect`` was bound to
nothing: ``code.inspect-changeset@1`` declares no input port, so the synthesiser could
not bind ``apply.patch`` to it (C1-r0's attempt to bind ``verify.report`` into
``summarize`` was refused with ``PORT_UNAVAILABLE`` for the same reason).  Every
``inspect`` leaf therefore started from the unpatched snapshot, wrote "no product code
was changed", and the root reviewer correctly failed ``c-change-explained``.

Three things change, none of them the v1 rows:

* the code catalogue publishes ``code.inspect-changeset@2`` (optional ``patch`` and
  ``report`` inputs) and ``code.summarize-review@2`` (``findings`` plus optional
  ``patch`` / ``report``) beside the @1 rows, which keep their bytes so a stored
  method or a stored reply still resolves;
* the synthesiser's operator offers list one version per task type — the latest —
  so the model is not invited to build on the row whose ports were the defect;
* ``method-synthesizer-v5`` says the step answering an "explain the change"
  requirement must be fed the change through an input port.

The fixture under ``fixtures/htn/c1_inspect_input/`` is the C1-r1 method as the
registry stored it; the first test pins the defect on it, the second binds it through
the new ports and shows the ``inspect`` leaf waiting for, then receiving, the patch.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import (  # noqa: E402
    FAILING_TEST,
    GOAL_TYPE,
    REPOSITORY,
    ROOT_DUTY,
    ROOT_TASK,
    _accept,
    _mission,
    _say,
    _task_of,
    ref,
)

from agent_orchestrator.contracts.htn import (  # noqa: E402
    ObligationId,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
)
from agent_orchestrator.contracts.obligations import Obligation  # noqa: E402
from agent_orchestrator.contracts.semantic_base import content_hash_of  # noqa: E402
from agent_orchestrator.graph.eligibility import ReadinessReason  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import HierarchicalDispatch  # noqa: E402
from agent_orchestrator.orchestrator.plan_commits import PlanPrincipal  # noqa: E402
from agent_orchestrator.planning.htn.synthesis import MethodSynthesizer  # noqa: E402
from agent_orchestrator.planning.htn.world import build_planning_world  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.storage.obligation_store import ObligationStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    method_proposal_step,
    plan_revision_proposal_step,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "htn" / "c1_inspect_input"
INSPECT = "code.inspect-changeset"
SUMMARIZE = "code.summarize-review"
#: The C1-r1 method also wrote explicit ``ordering`` rows (apply-patch before inspect,
#: inspect and verify before summarize), so the gate names whichever of the two
#: "not yet" reasons it checks first; either is "the leaf has not been dispatched".
NOT_YET = {ReadinessReason.WAITING_DATA, ReadinessReason.WAITING_ORDER}


def _c1_method() -> dict[str, Any]:
    return json.loads((FIXTURE / "method.json").read_text())


def _step(method: dict[str, Any], local_id: str) -> dict[str, Any]:
    return next(item for item in method["steps"] if item["local_id"] == local_id)


def _rebound(method: dict[str, Any]) -> dict[str, Any]:
    """The same method with ``inspect`` fed the patch and ``summarize`` fed the report."""

    bound = copy.deepcopy(method)
    bound["method_id"] = "code.fix-by-reproduce-patch-verify-explain.fed"
    inspect = _step(bound, "inspect")
    inspect["task_type_ref"] = ref(INSPECT, 2).to_json()
    inspect["arguments"] = {"patch": {"op": "output", "step": "apply-patch", "port": "patch"}}
    summarize = _step(bound, "summarize")
    summarize["task_type_ref"] = ref(SUMMARIZE, 2).to_json()
    summarize["arguments"] = {
        "findings": {"op": "output", "step": "inspect", "port": "findings"},
        "report": {"op": "output", "step": "verify", "port": "report"},
    }
    return bound


class _CodeWorld:
    """The shipped code domain with the C1-r1 method admitted through the synthesiser
    path and adopted for the root — the way the real episode got it."""

    def __init__(self, tmp_path, *, method: dict[str, Any], key: str) -> None:
        self.service, self.mission = _mission(tmp_path, key=key)
        self.semantics = HtnStore(self.service.store)
        self.world = build_planning_world(
            self.mission.id,
            domains=("code",),
            semantics=self.semantics,
            deployed_layers=("code_test",),
        )
        spec = self.world.catalog.require(ref(GOAL_TYPE))
        ObligationStore(self.service.store).register(
            Obligation(
                obligation_id=ObligationId(ROOT_DUTY),
                mission_id=self.mission.id,
                requirement_refs=tuple(spec.goal_signature.coverage_criteria),
                goal_signature_id=GOAL_TYPE,
            ),
            recursion_fuel=8,
        )
        self.semantics.put_task_semantics(
            self.mission.id,
            TaskSemanticBindingV1(
                task_id=TaskRef(ROOT_TASK),
                obligation_id=ObligationId(ROOT_DUTY),
                contract_revision=1,
                contract_hash=content_hash_of([ROOT_TASK, GOAL_TYPE]),
                form=TaskForm.COMPOUND,
                goal_signature=spec.goal_signature,
                typed_parameters={"repository": REPOSITORY, "failing_test": FAILING_TEST},
                requirement_refs=tuple(spec.goal_signature.coverage_criteria),
                semantic_scope="mission",
            ),
        )
        self.service.begin_planning(self.mission.id)
        _say(self.world, self.semantics, self.mission.id, "code.repo-checked-out", {
            "repository": REPOSITORY
        })
        self.dispatch = HierarchicalDispatch(self.service.store, self.service, planning=self.world)
        receipt = self.dispatch.apply_synthesizer_reply(
            self.mission.id, method_proposal_step(method)
        )
        assert receipt.admitted, receipt.problems
        reference = receipt.method_ref
        outcome = self.dispatch.apply_planner_reply(
            self.mission.id,
            plan_revision_proposal_step(
                proposal_id="prop-synth",
                expected_plan_revision=0,
                read_set=[
                    {
                        "kind": "method",
                        "id": reference.method_id,
                        "semantic_revision": int(reference.version),
                        "content_hash": reference.content_hash,
                    }
                ],
                operations=[
                    {
                        "op": "refine",
                        "goal_id": ROOT_TASK,
                        "obligation_id": ROOT_DUTY,
                        "method_ref": {
                            "id": reference.method_id,
                            "version": int(reference.version),
                            "content_hash": reference.content_hash,
                        },
                        "bindings": {},
                    }
                ],
            ),
            principal=PlanPrincipal("manager-1", "mission", 0),
            command_id="cmd-synth",
        )
        assert outcome.committed, outcome.last_reason
        duties = ObligationStore(self.service.store)
        seen: set[str] = set()
        for occurrence in self.dispatch.network(self.mission.id).occurrences:
            duty = str(occurrence.obligation_id)
            if duty in seen or not duties.exists(self.mission.id, occurrence.obligation_id):
                continue
            seen.add(duty)
            if not duties.account(self.mission.id, occurrence.obligation_id).has_admitted_demand:
                self.service.admit_obligation_demand(
                    self.mission.id,
                    occurrence.obligation_id,
                    principal="mission-submitter",
                    requester={"kind": "mission_root"},
                    evidence={"mission_id": self.mission.id},
                )
        self.clock = 1_000_000

    def task(self, type_id: str) -> str:
        return _task_of(self.dispatch, self.mission.id, type_id)

    def accept(self, type_id: str) -> None:
        self.clock += 1_000
        _accept(self.service, self.dispatch, self.mission.id, self.task(type_id))
        self.dispatch.issue_input_witnesses(
            self.mission.id, self.dispatch.network(self.mission.id), now_ms=self.clock
        )

    def inputs(self, type_id: str) -> list[str]:
        inputs = self.dispatch.attempt_inputs(self.mission.id, self.task(type_id))
        return [item.path for item in inputs]

    def reason(self, type_id: str) -> ReadinessReason | None:
        view = self.dispatch.read(self.mission.id)
        task_id = self.task(type_id)
        for spec in view.network.occurrences:
            if str(spec.task_id) == task_id:
                report = view.reports.get(spec.occurrence_id)
                return None if report is None else report.reason
        raise AssertionError(type_id)


# ======================================================================================
# 1. The defect, on the C1-r1 method as stored
# ======================================================================================


def test_the_c1_method_as_synthesised_hands_the_inspect_leaf_nothing(tmp_path) -> None:
    method = _c1_method()
    assert _step(method, "inspect")["arguments"] == {}
    assert _step(method, "inspect")["task_type_ref"]["version"] == 1
    world = _CodeWorld(tmp_path, method=method, key="p23k-n1-before")
    for step in ("code.read-repository-facts", "code.reproduce-failure", "code.apply-patch"):
        world.accept(step)
    # Nothing waited for the patch and nothing receives it: the leaf's workspace is
    # the snapshot, exactly what its findings then described.
    assert world.inputs(INSPECT) == []
    assert world.reason(INSPECT) not in NOT_YET


def test_the_v1_rows_keep_the_bytes_the_stored_method_names() -> None:
    """A method the library holds names ``@1`` by content hash; the row must still be
    there, unchanged, or the stored plan would stop resolving (P2.3h migration rule)."""

    method = _c1_method()
    catalog = build_planning_world("m-catalog", domains=("code",)).catalog
    for local_id, type_id in (("inspect", INSPECT), ("summarize", SUMMARIZE)):
        named = _step(method, local_id)["task_type_ref"]
        assert named["version"] == 1
        resolved = catalog.require(ref(type_id, 1))
        assert resolved.task_type_ref.content_hash == named["content_hash"]
        assert [port.port_key for port in resolved.input_ports] == (
            [] if type_id == INSPECT else ["findings"]
        )


# ======================================================================================
# 2. Bound through the @2 ports, the leaf waits for the patch and then receives it
# ======================================================================================


def test_bound_to_the_patch_port_the_inspect_leaf_waits_and_then_receives_it(tmp_path) -> None:
    world = _CodeWorld(tmp_path, method=_rebound(_c1_method()), key="p23k-n1-after")
    world.accept("code.read-repository-facts")
    world.accept("code.reproduce-failure")
    assert world.reason(INSPECT) in NOT_YET
    assert world.inputs(INSPECT) == []
    world.accept("code.apply-patch")
    assert world.inputs(INSPECT) == ["out/patch.json"], "the apply step's patch port"
    assert world.reason(INSPECT) not in NOT_YET


def test_the_summarize_leaf_is_fed_the_findings_and_the_verification_report(tmp_path) -> None:
    world = _CodeWorld(tmp_path, method=_rebound(_c1_method()), key="p23k-n1-summarize")
    for step in ("code.read-repository-facts", "code.reproduce-failure", "code.apply-patch"):
        world.accept(step)
    world.accept("code.verify-tests")
    assert world.reason(SUMMARIZE) in NOT_YET, "findings still owed"
    assert world.inputs(SUMMARIZE) == [], "a half-resolved manifest places nothing"
    world.accept(INSPECT)
    assert sorted(world.inputs(SUMMARIZE)) == ["out/findings.json", "out/report.json"]
    assert world.reason(SUMMARIZE) not in NOT_YET


def test_the_v2_rows_declare_the_ports_as_optional() -> None:
    catalog = build_planning_world("m-catalog-2", domains=("code",)).catalog
    inspect = catalog.require(ref(INSPECT, 2))
    assert {port.port_key: port.required for port in inspect.input_ports} == {
        "patch": False,
        "report": False,
    }
    summarize = catalog.require(ref(SUMMARIZE, 2))
    assert {port.port_key: port.required for port in summarize.input_ports} == {
        "findings": True,
        "patch": False,
        "report": False,
    }
    for spec in (inspect, summarize):
        assert str(spec.side_effect_kind) == "external_read"
        assert spec.operator_ref == catalog.require(ref(spec.task_type_ref.id, 1)).operator_ref


# ======================================================================================
# 3. The synthesiser is offered the latest version only, and told to bind it
# ======================================================================================


def test_the_operator_offers_list_one_version_per_task_type_the_latest() -> None:
    world = build_planning_world("m-offers", domains=("code",))
    synthesizer = MethodSynthesizer(world.registry, world.catalog)
    offers = synthesizer._offers(world.capabilities(), domain="code")
    by_id: dict[str, list[Any]] = {}
    for offer in offers:
        by_id.setdefault(offer.task_type_id, []).append(offer)
    assert all(len(items) == 1 for items in by_id.values()), {
        key: [item.version for item in items] for key, items in by_id.items() if len(items) > 1
    }
    inspect = by_id[INSPECT][0]
    assert inspect.version == 2
    assert inspect.content_hash == ref(INSPECT, 2).content_hash
    assert set(inspect.input_ports) == {"patch", "report"}
    summarize = by_id[SUMMARIZE][0]
    assert summarize.version == 2
    assert set(summarize.input_ports) == {"findings", "patch", "report"}
    # The @1 rows are not offered but still resolve (a stored reply still replays).
    assert world.catalog.resolve(ref(INSPECT, 1)) is not None


def test_the_prompt_v5_binds_the_explaining_step_and_v4_is_frozen() -> None:
    from agent_orchestrator.runtime.role_templates import (
        METHOD_SYNTHESIZER,
        METHOD_SYNTHESIZER_V4,
        METHOD_SYNTHESIZER_V4_VERSION,
        METHOD_SYNTHESIZER_VERSION,
        template_for,
    )

    assert METHOD_SYNTHESIZER.prompt_version == METHOD_SYNTHESIZER_VERSION
    assert METHOD_SYNTHESIZER_VERSION == "method-synthesizer-v5"
    v5 = METHOD_SYNTHESIZER.instructions
    v4 = METHOD_SYNTHESIZER_V4.instructions
    for sentence in (
        "c-change-explained",
        "patch 输入端口",
        "report 输入端口",
        "未修改的仓库快照",
        "最高版本",
    ):
        assert sentence in v5, sentence
        assert sentence not in v4, sentence
    assert METHOD_SYNTHESIZER_V4.prompt_version == METHOD_SYNTHESIZER_V4_VERSION
    assert METHOD_SYNTHESIZER_V4_VERSION == "method-synthesizer-v4"
    assert hashlib.sha256(v4.encode("utf-8")).hexdigest() == (
        "8d457abe7a74d614642ac7e2446e656aea9c46e4a7f509820353dd04f93f39b6"
    )
    assert (
        template_for(METHOD_SYNTHESIZER, {"method_synthesizer": METHOD_SYNTHESIZER_V4_VERSION})
        is METHOD_SYNTHESIZER_V4
    )
    # v5 is a revision: every v4 sentence survives.
    assert "review_feedback" in v5 and "拒绝码" in v5
