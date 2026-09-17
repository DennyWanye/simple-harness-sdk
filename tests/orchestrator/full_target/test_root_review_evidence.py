# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3h: the root ``MISSION_FINAL`` review package carries readable evidence.

The case is a real one — Grok acceptance run H arm, ``H-L3-C3-r0`` (2026-09-16,
grok-4.6, seed method ``code.fix-by-patch``).  Four leaves accepted, every declared
port delivered, the hidden grader PASS, and the root reviewer REJECTED on three
findings that were all *correct about the package it was shown*:

1. every ``accepted_outputs`` entry was an ``artifact_id`` and nothing else, so "the
   named test now passes" could not be read off anything;
2. every leaf's ``review.criteria`` stamped both root criteria PASS — the ``facts``
   step vouching for ``c-test-passes`` — which no reviewer may take as evidence;
3. no contribution was answerable for ``c-change-explained``: the link hung on the
   ``patch`` step, whose only port carries code;
4. leaf acceptances at requirements revisions 1–4 against a root at 5 read as
   "accepted against an older requirement".

The package before the fix and the reviewer's reply are checked in verbatim under
``fixtures/htn/c3_root_review`` (sanitised; see its README), together with the four
delivered artifacts, and the tests below rebuild the same Mission on the real code
domain and assert the package that comes out now.  The reviewer is a fixture that
**reads the evidence** — it answers PASS only when the covering excerpt says what the
link's ``evidence_requirement`` asks for — so the end-to-end test reaches
``COMPLETED`` for the right reason and the mutants below fail it for the right reason.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_deployment_wiring import (  # noqa: E402
    ROOT_DUTY as CODE_ROOT_DUTY,
)
from test_htn_deployment_wiring import (  # noqa: E402
    ROOT_TASK as CODE_ROOT_TASK,
)
from test_htn_deployment_wiring import (  # noqa: E402
    _both_lane_world,
    _task_of,
)
from test_htn_end_to_end import (  # noqa: E402
    ROOT_DUTY,
    World,
    _assembly,
    _leaf_task,
    _passing_layers,
    _review_task,
    committed,
)
from test_root_review_coordinator import (  # noqa: E402
    NOW_MS,
    ROOT_CRITERION,
    _Committed,
    _orchestrator,
    coordinator,
    offer_root,
)

from agent_orchestrator.contracts import Budget, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.models import Artifact, Attempt  # noqa: E402
from agent_orchestrator.contracts.resolution import ReviewVerdict  # noqa: E402
from agent_orchestrator.contracts.state_machines import AttemptStatus  # noqa: E402
from agent_orchestrator.orchestrator import root_review as root_review_module  # noqa: E402
from agent_orchestrator.orchestrator.leaf_acceptance import (  # noqa: E402
    LEAF_LOCAL_CRITERION,
    LeafAcceptanceAssembly,
    criteria_for,
)
from agent_orchestrator.orchestrator.root_review import (  # noqa: E402
    EXCERPT_MAX_CHARS,
    REQUIREMENTS_REVISION_SEMANTICS,
    RootReviewCoordinator,
)
from agent_orchestrator.runtime.output_blocks import PortClaim  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "htn" / "c3_root_review"
ROOT_CRITERIA = ("c-test-passes", "c-change-explained")

#: sha256 of ``root-reviewer-v1``'s instructions, frozen (§26.3): an Attempt replays
#: on the bytes it pinned, so the old words may be superseded but never edited.
FROZEN_ROOT_REVIEWER_V1 = "2b5ebe37c1701f60e955a0e71d1d682fc344f2ccf6d06ed12606e2df9b9aa59a"


# ======================================================================================
# helpers: artifacts the library really holds, with bytes behind them
# ======================================================================================


def _store_artifact(
    service: Any,
    mission_id: str,
    task_id: str,
    *,
    artifact_id: str,
    path: str,
    data: bytes,
    root: Path,
) -> Artifact:
    """An Attempt row, an Artifact row and the content-addressed file behind it.

    The accept path records ``artifact_id`` against a port; the root review then
    reads the row back and the bytes through ``read_verified`` (hash re-checked), so
    a test that wants an excerpt has to give the library all three.
    """

    store = service.store
    attempt_id = f"{task_id}:attempt-{artifact_id}"
    if store.get_attempt(attempt_id) is None:
        store.insert_attempt(
            Attempt(
                id=attempt_id,
                task_id=task_id,
                mission_id=mission_id,
                role="worker",
                model="fixture",
                prompt_version="worker-hierarchical-v2",
                context_version="ctx",
                budget_reserved=Budget(max_tokens=1),
                lease_owner=None,
                lease_expires_at=None,
                status=AttemptStatus.COMPLETED,
                retry_of=None,
                created_at=1.0,
                version=1,
                ordinal=1,
                creation_key=f"k-{attempt_id}",
                input_id="i",
                failure=None,
            )
        )
    digest = hashlib.sha256(data).hexdigest()
    blob = root / "artifacts" / "sha256" / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(data)
    artifact = Artifact(
        id=artifact_id,
        mission_id=mission_id,
        task_id=task_id,
        attempt_id=attempt_id,
        type="file",
        path=path,
        version=1,
        content_hash=digest,
        size_bytes=len(data),
        produced_by="agent-worker",
        storage_uri=str(blob),
    )
    store.upsert_artifact(artifact)
    return artifact


def _accept_with(
    service: Any,
    dispatch: Any,
    mission_id: str,
    task_id: str,
    *,
    artifacts: tuple[Artifact, ...],
    now_ms: int,
) -> Any:
    declared = dispatch.declared_output_ports_for(mission_id, task_id)
    claims = tuple(
        PortClaim(port_key=item["port"], path=artifacts[index].path)
        for index, item in enumerate(declared)
        if index < len(artifacts)
    )
    assembly = LeafAcceptanceAssembly(service.store, service, dispatch=dispatch)
    return assembly.accept(
        mission_id,
        task_id,
        result_id=f"result-{task_id}",
        layers=_passing_layers(),
        artifacts=artifacts,
        producer_agent_ids=("agent-worker",),
        reviewer_agent_id="agent-critic",
        now_ms=now_ms,
        port_claims=claims,
    )


# ======================================================================================
# the C3 Mission, rebuilt on the real code domain with the real delivered artifacts
# ======================================================================================


class CodeWorld:
    """``code.fix-by-patch`` on the shipped code domain, its four leaves accepted with
    the artifacts the Grok C3 run really delivered, and the root review cut."""

    def __init__(self, tmp_path: Path) -> None:
        self.service, self.mission, self.semantics, self.world, self.dispatch = _both_lane_world(
            tmp_path
        )
        self.root = Path(tmp_path)
        self.tasks = {
            step: _task_of(self.dispatch, self.mission.id, type_id)
            for step, type_id in (
                ("facts", "code.read-repository-facts"),
                ("reproduce", "code.reproduce-failure"),
                ("patch", "code.apply-patch"),
                ("verify", "code.verify-tests"),
            )
        }
        self.acceptances: dict[str, str] = {}

    def deliver(self, step: str, *, path: str, data: bytes, now_ms: int) -> None:
        task_id = self.tasks[step]
        artifact = _store_artifact(
            self.service,
            self.mission.id,
            task_id,
            artifact_id=f"artifact-{step}",
            path=path,
            data=data,
            root=self.root,
        )
        receipt = _accept_with(
            self.service,
            self.dispatch,
            self.mission.id,
            task_id,
            artifacts=(artifact,),
            now_ms=now_ms,
        )
        self.acceptances[step] = str(receipt.acceptance_id)

    def coordinator(self) -> RootReviewCoordinator:
        return RootReviewCoordinator(self.service.store, self.service, self.dispatch)

    def request(self) -> Any:
        coordination = self.coordinator()
        package = coordination.live_package(self.mission.id)
        assert package is not None
        return coordination.request(self.mission.id, package)

    def contribution(self, step: str) -> dict[str, Any]:
        request = self.request()
        for item in request.contributions:
            if item["acceptance_id"] == self.acceptances[step]:
                return dict(item)
        raise AssertionError(f"no contribution for {step}")


def _c3_artifacts() -> dict[str, tuple[str, bytes]]:
    return {
        "facts": ("facts.json", (FIXTURE / "artifacts" / "facts.json").read_bytes()),
        "reproduce": ("diagnosis.md", (FIXTURE / "artifacts" / "diagnosis.md").read_bytes()),
        "patch": ("stats/window.py", (FIXTURE / "artifacts" / "window.py").read_bytes()),
        "verify": ("REPORT.md", (FIXTURE / "artifacts" / "REPORT.md").read_bytes()),
    }


@pytest.fixture
def c3(tmp_path) -> CodeWorld:
    world = CodeWorld(tmp_path)
    for index, (step, (path, data)) in enumerate(_c3_artifacts().items()):
        world.deliver(step, path=path, data=data, now_ms=1_000_000 + index * 1_000)
    world.dispatch.issue_input_witnesses(
        world.mission.id, world.dispatch.network(world.mission.id), now_ms=1_100_000
    )
    world.coordinator().cut(world.mission.id, now_ms=NOW_MS)
    return world


# ======================================================================================
# 0. The package before the fix, as the reviewer saw it
# ======================================================================================


def test_the_c3_package_before_the_fix_is_what_the_reviewer_rejected() -> None:
    """The fixture is the defect, verbatim: no excerpt anywhere, every leaf stamping
    both root criteria PASS, nobody answerable for ``c-change-explained``, and the
    reviewer's three findings — so the assertions that follow are about a real
    package and not about a shape this test invented."""

    before = json.loads((FIXTURE / "package_before.json").read_text())
    verdict = json.loads((FIXTURE / "verdict_before.json").read_text())["verdict"]
    assert [item["criterion_id"] for item in before["criteria"]] == list(ROOT_CRITERIA)
    assert len(before["contributions"]) == 4
    for item in before["contributions"]:
        assert item["artifacts"] == []
        assert all("excerpt" not in output for output in item["accepted_outputs"])
        assert item["review"]["criteria"] == {name: "PASS" for name in ROOT_CRITERIA}
        assert "covered_by" not in item and "carries_root_criteria" not in item
        assert item["requirements_revision"] < before["requirements_revision"]
    assert "requirements_revision_semantics" not in before
    assert verdict["verdict"] == "FAIL"
    assert [item["severity"] for item in verdict["findings"]] == ["blocker", "blocker", "major"]
    assert [item["met"] for item in verdict["mission_criteria"]] == [False, False]


# ======================================================================================
# 1. The package carries readable evidence
# ======================================================================================


def test_the_verify_report_travels_in_the_request_as_text(c3: CodeWorld) -> None:
    """Finding 1: the reviewer could not read the report.  Now it can."""

    outputs = c3.contribution("verify")["accepted_outputs"]
    assert [item["port"] for item in outputs] == ["report"]
    excerpt = outputs[0]["excerpt"]
    assert excerpt["kind"] == "text"
    assert excerpt["text"] == (FIXTURE / "artifacts" / "REPORT.md").read_text()
    assert excerpt["truncated"] is False
    assert "2 passed" in excerpt["text"], "the reviewer can now read that the suite passed"


def test_every_accepted_output_carries_its_hash_and_the_bytes_behind_it(c3: CodeWorld) -> None:
    request = c3.request()
    for step, (path, data) in _c3_artifacts().items():
        outputs = c3.contribution(step)["accepted_outputs"]
        assert len(outputs) == 1
        excerpt = outputs[0]["excerpt"]
        assert excerpt["content_hash"] == hashlib.sha256(data).hexdigest()
        assert excerpt["path"] == path
        assert excerpt["size_bytes"] == len(data)
        assert excerpt["text"] == data.decode("utf-8")
    for item in request.contributions:
        assert item["evidence"] == {"kind": "accepted_outputs", "count": 1, "readable": 1}


def test_each_output_says_which_root_criteria_it_is_evidence_for(c3: CodeWorld) -> None:
    assert c3.contribution("verify")["accepted_outputs"][0]["covers_root_criteria"] == list(
        ROOT_CRITERIA
    )
    for step in ("facts", "reproduce", "patch"):
        assert c3.contribution(step)["accepted_outputs"][0]["covers_root_criteria"] == []


def test_the_request_hash_covers_the_excerpt_and_is_reproducible(c3: CodeWorld) -> None:
    """The intent's ``context_version`` is the request hash: two reads of the same
    package hash the same, and a package whose evidence bytes differ hashes
    differently — the hash covers what the reviewer read."""

    first = c3.request().content_hash()
    assert c3.request().content_hash() == first
    blob = c3.service.store.get_artifact("artifact-verify")
    assert blob is not None
    Path(blob.storage_uri).chmod(0o644)
    Path(blob.storage_uri).write_bytes(b"# tampered\n")
    tampered = c3.request()
    excerpt = c3.contribution("verify")["accepted_outputs"][0]["excerpt"]
    assert excerpt["kind"] == "unavailable" and "hash_mismatch" in excerpt["reason"], (
        "bytes that do not match the artifact's hash are never quoted as its content"
    )
    assert tampered.content_hash() != first


def test_a_long_artifact_is_truncated_at_the_per_artifact_cap(tmp_path) -> None:
    world = CodeWorld(tmp_path)
    long_report = ("line of report\n" * 1000).encode()
    assert len(long_report) > EXCERPT_MAX_CHARS
    artifacts = _c3_artifacts()
    for index, step in enumerate(("facts", "reproduce", "patch")):
        path, data = artifacts[step]
        world.deliver(step, path=path, data=data, now_ms=1_000_000 + index)
    world.deliver("verify", path="REPORT.md", data=long_report, now_ms=1_000_010)
    world.coordinator().cut(world.mission.id, now_ms=NOW_MS)
    excerpt = world.contribution("verify")["accepted_outputs"][0]["excerpt"]
    assert excerpt["kind"] == "text"
    assert excerpt["truncated"] is True
    assert len(excerpt["text"]) == EXCERPT_MAX_CHARS
    assert excerpt["total_chars"] == len(long_report)


def test_a_binary_artifact_is_named_by_hash_and_size_only(tmp_path) -> None:
    world = CodeWorld(tmp_path)
    artifacts = _c3_artifacts()
    for index, step in enumerate(("facts", "reproduce", "verify")):
        path, data = artifacts[step]
        world.deliver(step, path=path, data=data, now_ms=1_000_000 + index)
    world.deliver("patch", path="stats/window.py", data=b"\x00\xff\xfe binary", now_ms=1_000_010)
    world.coordinator().cut(world.mission.id, now_ms=NOW_MS)
    excerpt = world.contribution("patch")["accepted_outputs"][0]["excerpt"]
    assert excerpt["kind"] == "binary"
    assert "text" not in excerpt
    assert excerpt["size_bytes"] == len(b"\x00\xff\xfe binary")
    assert world.contribution("patch")["evidence"]["readable"] == 0


def test_the_excerpt_budget_is_spent_in_package_order(c3: CodeWorld, monkeypatch) -> None:
    """A Mission with many leaves cannot turn the request into an unbounded prompt:
    once the package-wide budget is spent, later artifacts are stated as omitted
    (hash and size kept), and which ones is a deterministic function of the package."""

    monkeypatch.setattr(root_review_module, "EXCERPT_BUDGET_CHARS", 3000)
    request = c3.request()
    kinds = [item["accepted_outputs"][0]["excerpt"]["kind"] for item in request.contributions]
    assert "omitted" in kinds and "text" in kinds
    assert kinds.index("text") < kinds.index("omitted")
    omitted = next(
        item["accepted_outputs"][0]["excerpt"]
        for item in request.contributions
        if item["accepted_outputs"][0]["excerpt"]["kind"] == "omitted"
    )
    assert omitted["content_hash"] and omitted["size_bytes"] > 0 and "budget" in omitted["reason"]


def test_an_artifact_the_library_does_not_hold_is_stated_as_unavailable(tmp_path) -> None:
    """The shared fixture accepts leaves with artifact objects the store never saw:
    the request says so instead of inventing an empty excerpt."""

    from test_root_review_coordinator import _seeded

    world = _seeded(tmp_path, key="p23h-unavailable")
    coordinator(world).cut(world.mission.id, now_ms=NOW_MS)
    package = coordinator(world).live_package(world.mission.id)
    request = coordinator(world).request(world.mission.id, package)
    for item in request.contributions:
        for output in item["accepted_outputs"]:
            assert output["excerpt"]["kind"] == "unavailable"
            assert output["excerpt"]["reason"]
        assert item["evidence"]["readable"] == 0


# ======================================================================================
# 2. Leaf criteria are the leaf's own
# ======================================================================================


def test_the_facts_leaf_no_longer_stamps_the_root_criteria(c3: CodeWorld) -> None:
    """Finding 2: a ``facts`` step's PASS on ``c-test-passes`` is not evidence, and
    now it is not there to be misread."""

    facts = c3.contribution("facts")
    assert facts["review"]["criteria"] == {LEAF_LOCAL_CRITERION: "PASS"}
    assert facts["carries_root_criteria"] == []
    for step in ("reproduce", "patch"):
        assert set(c3.contribution(step)["review"]["criteria"]) == {LEAF_LOCAL_CRITERION}
        assert not set(c3.contribution(step)["review"]["criteria"]) & set(ROOT_CRITERIA)


def test_the_verify_leaf_is_judged_under_the_criteria_the_plan_linked_to_it(c3: CodeWorld) -> None:
    verify = c3.contribution("verify")
    assert verify["review"]["criteria"] == {"c-green": "PASS", "c-explained": "PASS"}
    carries = {item["root_criterion_id"]: item for item in verify["carries_root_criteria"]}
    assert set(carries) == set(ROOT_CRITERIA)
    assert carries["c-test-passes"]["leaf_criterion_id"] == "c-green"
    assert carries["c-change-explained"]["leaf_criterion_id"] == "c-explained"
    assert all(item["leaf_review_verdict"] == "PASS" for item in carries.values())


def test_the_leaf_package_itself_carries_only_the_leaf_criteria(c3: CodeWorld) -> None:
    """Not only the request: the stored ``ReviewPackage`` / ``ReviewRecord`` anchors."""

    for step, expected in (
        ("facts", {LEAF_LOCAL_CRITERION}),
        ("verify", {"c-green", "c-explained"}),
    ):
        acceptance = c3.semantics.get_acceptance(c3.acceptances[step])
        record = c3.semantics.get_review_record(str(acceptance.review_record_id)).record
        package = c3.semantics.get_review_package(str(record.package_id))
        assert {str(item.criterion_id) for item in package.criteria} == expected
        assert {str(item.criterion_id) for item in record.criteria} == expected
    statements = {
        str(item.criterion_id): str(item.statement)
        for item in c3.semantics.get_review_package(
            str(
                c3.semantics.get_review_record(
                    str(c3.semantics.get_acceptance(c3.acceptances["verify"]).review_record_id)
                ).record.package_id
            )
        ).criteria
    }
    assert "c-change-explained" in statements["c-explained"]
    assert "explains what the patch changed" in statements["c-explained"]


def test_criteria_for_owes_the_local_criterion_and_never_the_parents_refs() -> None:
    """Unit: the fallback that produced finding 2 is gone."""

    from agent_orchestrator.contracts.htn import (
        GoalSignature,
        ObligationId,
        TaskForm,
        TaskRef,
        TaskSemanticBindingV1,
    )
    from agent_orchestrator.contracts.semantic_base import VersionedRef, content_hash_of
    from agent_orchestrator.orchestrator.accepted_outputs import CarriedCriterion

    schema = VersionedRef(id="x.params", version=1, content_hash=content_hash_of("x"))
    binding = TaskSemanticBindingV1(
        task_id=TaskRef("task-leaf"),
        obligation_id=ObligationId("obl-root"),
        contract_revision=1,
        contract_hash=content_hash_of("leaf"),
        form=TaskForm.PRIMITIVE,
        goal_signature=GoalSignature(
            signature_id="x.leaf",
            version=1,
            parameter_schema_ref=schema,
            output_schema_ref=schema,
            statement="a leaf",
            coverage_criteria=(),
        ),
        requirement_refs=("c-test-passes", "c-change-explained"),
        semantic_scope="mission",
        operator_ref=VersionedRef(id="x.leaf", version=1, content_hash=content_hash_of("op")),
    )
    plain = criteria_for(binding, _passing_layers())
    assert [item.criterion_id for item in plain] == [LEAF_LOCAL_CRITERION]
    linked = criteria_for(
        binding,
        _passing_layers(),
        carried=(
            CarriedCriterion(
                parent_task_id="task-root",
                parent_criterion_id="c-test-passes",
                occurrence_id="occ-leaf",  # type: ignore[arg-type]
                task_id="task-leaf",
                leaf_criterion_id="c-green",
                evidence_requirement="the report shows the test passing",
            ),
        ),
    )
    assert [item.criterion_id for item in linked] == ["c-green"]
    assert "c-test-passes" in linked[0].statement
    assert "the report shows the test passing" in linked[0].statement


# ======================================================================================
# 3. Every root criterion names who is answerable for it
# ======================================================================================


def test_c_change_explained_has_a_committer_and_it_is_the_verify_report(c3: CodeWorld) -> None:
    """Finding 3: nobody was answerable.  The seed method now hangs it on the report."""

    criteria = {item["criterion_id"]: item for item in c3.request().criteria}
    covered = criteria["c-change-explained"]["covered_by"]
    assert [item["acceptance_id"] for item in covered] == [c3.acceptances["verify"]]
    assert covered[0]["task_id"] == c3.tasks["verify"]
    assert covered[0]["leaf_criterion_id"] == "c-explained"
    assert covered[0]["ports"] == ["report"]
    assert "explains what the patch changed" in covered[0]["evidence_requirement"]


def test_c_test_passes_is_covered_by_the_same_report(c3: CodeWorld) -> None:
    criteria = {item["criterion_id"]: item for item in c3.request().criteria}
    covered = criteria["c-test-passes"]["covered_by"]
    assert [item["acceptance_id"] for item in covered] == [c3.acceptances["verify"]]
    assert covered[0]["leaf_criterion_id"] == "c-green"
    assert "now passes" in covered[0]["evidence_requirement"]


def test_the_seed_fix_methods_hang_the_explanation_on_the_verify_report() -> None:
    """The three ``code.fix-*`` methods: ``c-change-explained`` → ``verify``, whose
    ``report`` port is prose; ``patch``/``revert`` deliver code, which explains nothing."""

    methods = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "src"
            / "agent_orchestrator"
            / "planning"
            / "htn"
            / "seed_methods"
            / "code"
            / "methods.json"
        ).read_text()
    )
    fixes = {
        item["method_id"]: item for item in methods if item["method_id"].startswith("code.fix-")
    }
    assert set(fixes) == {"code.fix-by-patch", "code.fix-by-revert", "code.fix-by-assessed-revert"}
    for method in fixes.values():
        links = {
            item["parent_criterion_id"]: item for item in method["composition"]["criterion_links"]
        }
        assert links["c-change-explained"]["child_step"] == "verify"
        assert links["c-test-passes"]["child_step"] == "verify"
        assert "report" in links["c-change-explained"]["evidence_requirement"]
        assert "report" in links["c-test-passes"]["evidence_requirement"]


def test_the_worker_is_told_which_root_criteria_it_carries(c3: CodeWorld) -> None:
    """The context block beside ``declared_output_ports``: the verify leaf learns it
    owes both root criteria on its ``report`` port; the facts leaf learns nothing,
    because it owes nothing."""

    carried = c3.dispatch.carried_root_criteria_for(c3.mission.id, c3.tasks["verify"])
    assert {item["root_criterion_id"] for item in carried} == set(ROOT_CRITERIA)
    for item in carried:
        assert item["ports"] == ["report"]
        assert item["root_task_id"] == CODE_ROOT_TASK
        assert (
            item["root_goal_statement"] == "make the named failing test pass and explain the change"
        )
        assert item["evidence_requirement"]
    assert c3.dispatch.carried_root_criteria_for(c3.mission.id, c3.tasks["facts"]) == ()
    assert c3.dispatch.carried_root_criteria_for(c3.mission.id, c3.tasks["patch"]) == ()


def test_the_worker_context_package_carries_the_block(c3: CodeWorld) -> None:
    """The block reaches the context package by the same seal every other block
    uses, so it is part of the Attempt's ``context_version``."""

    from agent_orchestrator.context.context_builder import _seal

    carried = c3.dispatch.carried_root_criteria_for(c3.mission.id, c3.tasks["verify"])
    package = _seal(
        {
            "task": {"id": c3.tasks["verify"]},
            "carried_root_criteria": {
                "data_not_instruction": True,
                "version": "carried-root-criteria-v1",
                "criteria": [dict(item) for item in carried],
            },
        }
    )
    assert "carried_root_criteria" in package.package
    assert "c-change-explained" in package.text
    assert package.context_version != _seal({"task": {"id": c3.tasks["verify"]}}).context_version


# ======================================================================================
# 4. Revision numbers are explained
# ======================================================================================


def test_the_revision_numbers_are_explained_rather_than_left_to_be_misread(c3: CodeWorld) -> None:
    """Finding 4: leaves at 1–4, root at 5, read as staleness."""

    payload = c3.request().to_json()
    assert payload["requirements_revision_semantics"] == REQUIREMENTS_REVISION_SEMANTICS
    assert "monotone" in payload["requirements_revision_semantics"]
    root = payload["requirements_revision"]
    for item in payload["contributions"]:
        assert "requirements_revision" not in item, "the misread field is gone"
        assert item["accepted_at_requirements_revision"] < root
    assert sorted(
        item["accepted_at_requirements_revision"] for item in payload["contributions"]
    ) == [1, 2, 3, 4]
    assert root == 5, "exactly the C3 shape"


def test_the_prompt_v2_names_the_new_fields_and_v1_is_frozen() -> None:
    from agent_orchestrator.runtime.role_templates import (
        ROOT_REVIEWER,
        ROOT_REVIEWER_V1,
        ROOT_REVIEWER_V1_VERSION,
        ROOT_REVIEWER_VERSION,
        TEMPLATE_VERSIONS,
        template_for,
    )

    assert ROOT_REVIEWER.prompt_version == ROOT_REVIEWER_VERSION == "root-reviewer-v2"
    for field in (
        "excerpt",
        "covered_by",
        "carries_root_criteria",
        "covers_root_criteria",
        "accepted_at_requirements_revision",
        "requirements_revision_semantics",
        "c-leaf-verified",
    ):
        assert field in ROOT_REVIEWER.instructions, field
        assert field not in ROOT_REVIEWER_V1.instructions, field
    assert "不得据此判 false" in ROOT_REVIEWER.instructions
    assert ROOT_REVIEWER_V1.prompt_version == ROOT_REVIEWER_V1_VERSION == "root-reviewer-v1"
    assert TEMPLATE_VERSIONS["root_reviewer"].keys() >= {"root-reviewer-v1", "root-reviewer-v2"}
    assert template_for(ROOT_REVIEWER, {"root_reviewer": "root-reviewer-v1"}) is ROOT_REVIEWER_V1
    assert (
        hashlib.sha256(ROOT_REVIEWER_V1.instructions.encode("utf-8")).hexdigest()
        == FROZEN_ROOT_REVIEWER_V1
    )


# ======================================================================================
# 5. End to end with a reviewer that reads the evidence
# ======================================================================================


def _evidence_reviewer(request: dict[str, Any], *, must_contain: str) -> str:
    """A scripted MISSION_FINAL reviewer that judges only what it can read.

    A criterion is met when a contribution the plan made answerable for it delivered
    an output covering it whose text excerpt contains ``must_contain`` and whose leaf
    review passed the linked leaf criterion.  Nothing else counts — not a PASS on
    an unlinked leaf, not an artifact id without bytes.
    """

    by_id = {item["acceptance_id"]: item for item in request["contributions"]}
    judged: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for criterion in request["criteria"]:
        name = criterion["criterion_id"]
        met = False
        for cover in criterion["covered_by"]:
            contribution = by_id.get(cover["acceptance_id"])
            if contribution is None:
                continue
            carries = {
                item["root_criterion_id"]: item["leaf_review_verdict"]
                for item in contribution["carries_root_criteria"]
            }
            if carries.get(name) != "PASS":
                continue
            for output in contribution["accepted_outputs"]:
                excerpt = output.get("excerpt") or {}
                if (
                    name in output.get("covers_root_criteria", [])
                    and excerpt.get("kind") == "text"
                    and must_contain in excerpt.get("text", "")
                ):
                    met = True
        judged.append({"criterion": name, "met": met, "reason": "read from excerpts"})
        if not met:
            findings.append({"severity": "blocker", "detail": f"{name}: no readable evidence"})
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": "FAIL" if findings else "PASS",
                "findings": findings,
                "mission_criteria": judged,
            }
        )
        + "</critic_verdict>"
    )


def _plan_world_with_evidence(tmp_path, *, review_text: str, key: str) -> World:
    """The shared ``plan.goal`` fixture with artifacts the library really holds."""

    world = committed(tmp_path, key=key, demand=True)
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    leaf = _store_artifact(
        world.service,
        world.mission.id,
        _leaf_task(world),
        artifact_id="artifact-leaf",
        path="out/result.json",
        data=b'{"result": "alpha"}',
        root=Path(tmp_path),
    )
    _accept_with(
        world.service,
        world.dispatch,
        world.mission.id,
        _leaf_task(world),
        artifacts=(leaf,),
        now_ms=1_000_000,
    )
    verdict = _store_artifact(
        world.service,
        world.mission.id,
        _review_task(world),
        artifact_id="artifact-verdict",
        path="out/verdict.json",
        data=review_text.encode(),
        root=Path(tmp_path),
    )
    _assembly(world).accept(
        world.mission.id,
        _review_task(world),
        result_id="result-review",
        layers=_passing_layers(),
        artifacts=(verdict,),
        producer_agent_ids=("agent-worker",),
        reviewer_agent_id="agent-critic",
        now_ms=1_100_000,
        port_claims=(PortClaim(port_key="verdict", path="out/verdict.json"),),
    )
    return world


def _run_to_verdict(world: World, tmp_path, *, must_contain: str) -> tuple[Any, Any, bool]:
    """Cut and ask through the loop, answer with the evidence reviewer, collect, resolve."""

    import asyncio

    from simple_harness.agents import AgentTurnState

    async def case():
        async with _orchestrator(tmp_path) as loop:
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            new_mode = loop._new_mode(mission)
            assert await loop._advance_root_review(mission, new_mode) is True, "cut and asked"
            coordination = loop._root_review(mission, new_mode)
            package = coordination.live_package(mission.id)
            assert package is not None
            subject = f"{mission.id}:root-review:{package.package_id}:1"
            intent = loop.store.get_intent_for_subject(subject)
            assert intent is not None
            shown = json.loads(intent.config["message"]["content"])
            assert (
                intent.config["context_version"]
                == coordination.request(mission.id, package).content_hash()
            )
            reply = _evidence_reviewer(shown, must_contain=must_contain)
            await loop._collect_root_review(
                intent, _Committed(AgentTurnState.COMMITTED), mission, reply
            )
            record = coordination.semantics.official_review_record(str(package.package_id))
            formed = await loop._root_resolution_formed(mission, new_mode)
            return shown, record, formed

    return asyncio.run(case())


def test_a_reviewer_that_reads_the_evidence_takes_the_mission_to_completed(tmp_path) -> None:
    world = _plan_world_with_evidence(
        tmp_path,
        review_text="verdict: PASS — c-root is satisfied by out/result.json",
        key="p23h-e2e",
    )
    shown, record, formed = _run_to_verdict(world, tmp_path, must_contain="verdict: PASS")
    assert record is not None and record.verdict is ReviewVerdict.ACCEPT
    assert formed is True
    judgments = [{"criterion": item, "met": True} for item in world.mission.success_criteria]
    judged = world.service.judge_mission(world.mission.id, judgments=judgments, summary="done")
    assert judged.status is MissionStatus.COMPLETED
    # And it got there on evidence: the covering excerpt is the verdict file's bytes.
    covered = {item["criterion_id"]: item["covered_by"] for item in shown["criteria"]}
    assert covered[ROOT_CRITERION][0]["ports"] == ["verdict"]
    reviewed = next(
        item
        for item in shown["contributions"]
        if item["acceptance_id"] == covered[ROOT_CRITERION][0]["acceptance_id"]
    )
    assert reviewed["accepted_outputs"][0]["excerpt"]["text"].startswith("verdict: PASS")
    assert reviewed["review"]["criteria"] == {"c-reviewed": "PASS"}
    assert shown["requirements_revision_semantics"]


def test_the_same_reviewer_rejects_a_report_that_does_not_show_the_evidence(tmp_path) -> None:
    """The control: the words the link asks for are absent, so the reviewer says so."""

    world = _plan_world_with_evidence(
        tmp_path, review_text="the suite was run; see logs", key="p23h-e2e-fail"
    )
    _shown, record, formed = _run_to_verdict(world, tmp_path, must_contain="verdict: PASS")
    assert record is not None and record.verdict is ReviewVerdict.REJECTED
    assert formed is False
    assert offer_root(world).committed is False


# ======================================================================================
# 6. Mutation self-proofs
# ======================================================================================


def test_mutant_a_package_without_excerpts_is_rejected_by_a_reviewer_that_reads(
    tmp_path, monkeypatch
) -> None:
    """Revert finding 1 — hand the reviewer artifact ids only — and the Mission that
    reaches COMPLETED above is refused, exactly as Grok refused C3."""

    monkeypatch.setattr(
        root_review_module,
        "excerpt_of",
        lambda artifact, **kwargs: {"kind": "unavailable", "reason": "mutant"},
    )
    world = _plan_world_with_evidence(
        tmp_path, review_text="verdict: PASS — c-root is satisfied", key="p23h-m1"
    )
    shown, record, formed = _run_to_verdict(world, tmp_path, must_contain="verdict: PASS")
    assert all(
        output["excerpt"]["kind"] == "unavailable"
        for item in shown["contributions"]
        for output in item["accepted_outputs"]
    ), "the mutant is in place"
    assert record is not None and record.verdict is ReviewVerdict.REJECTED
    assert formed is False


def test_mutant_links_forgotten_leave_every_root_criterion_without_a_committer(
    tmp_path, monkeypatch
) -> None:
    """Revert finding 3 — no criterion names who is answerable — and the reviewer that
    reads ``covered_by`` finds nobody to read."""

    monkeypatch.setattr(
        root_review_module, "carried_criteria_in_revision", lambda *args, **kwargs: ()
    )
    world = _plan_world_with_evidence(
        tmp_path, review_text="verdict: PASS — c-root is satisfied", key="p23h-m2"
    )
    shown, record, formed = _run_to_verdict(world, tmp_path, must_contain="verdict: PASS")
    assert all(item["covered_by"] == [] for item in shown["criteria"]), "the mutant is in place"
    assert record is not None and record.verdict is ReviewVerdict.REJECTED
    assert formed is False


def test_mutant_a_leaf_that_stamps_every_root_criterion_is_caught_at_the_anchor(
    tmp_path, monkeypatch
) -> None:
    """Revert finding 2 at the leaf — carry nothing, so ``criteria_for`` falls to the
    local criterion for the review leaf too — and the leaf's stored anchors no longer
    name ``c-reviewed``; the reviewer's ``leaf_review_verdict`` reads ABSENT and it
    refuses."""

    monkeypatch.setattr(LeafAcceptanceAssembly, "carried_criteria", lambda self, m, b: ())
    world = _plan_world_with_evidence(
        tmp_path, review_text="verdict: PASS — c-root is satisfied", key="p23h-m3"
    )
    shown, record, formed = _run_to_verdict(world, tmp_path, must_contain="verdict: PASS")
    reviewed = next(item for item in shown["contributions"] if item["carries_root_criteria"])
    assert reviewed["review"]["criteria"] == {LEAF_LOCAL_CRITERION: "PASS"}, (
        "the mutant is in place"
    )
    assert reviewed["carries_root_criteria"][0]["leaf_review_verdict"] == "ABSENT"
    assert record is not None and record.verdict is ReviewVerdict.REJECTED
    assert formed is False


def test_mutant_a_blank_revision_explanation_is_visible_in_the_request(
    c3: CodeWorld, monkeypatch
) -> None:
    """Revert finding 4 — send the numbers with no semantics — and the request no longer
    carries the sentence the reviewer is told to read; the assertion in
    ``test_the_revision_numbers_are_explained…`` is what catches it."""

    monkeypatch.setattr(root_review_module, "REQUIREMENTS_REVISION_SEMANTICS", "")
    payload = c3.request().to_json()
    assert payload["requirements_revision_semantics"] == ""
    assert "monotone" not in payload["requirements_revision_semantics"]


def test_the_root_resolution_still_needs_the_reviewers_pass(c3: CodeWorld) -> None:
    """None of the above moved the verdict into the system's hands (AER I05): the C3
    package, evidence and all, resolves nothing until a reviewer says PASS."""

    from agent_orchestrator.orchestrator.plan_commits import PlanPrincipal

    outcome = c3.dispatch.attempt_root_resolution(
        c3.mission.id,
        principal=PlanPrincipal(
            "manager-1", "mission", c3.semantics.epoch(c3.mission.id, "mission")
        ),
        command_id=f"{c3.mission.id}:root-resolution",
    )
    assert outcome.committed is False
    assert c3.semantics.adopted_goal_resolution(c3.mission.id, CODE_ROOT_DUTY) is None
    assert ROOT_DUTY == CODE_ROOT_DUTY
