# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""FULL-TARGET P2.3c part 2d, decision 4: the Worker declares its output ports.

Which of an Attempt's files is the ``repository_facts`` a downstream step asked for
is a **local key** — TG design §3.2 gives the model local keys, business goals and
parameters and binds everything else (Mission identity, accounts, scope, execution
generation) itself.  So the port↔artifact pairing is *declared* by the producer and
*checked* here, and the two old fallbacks — "the port name appears somewhere in the
path" and "one port, one artifact, so they go together" — are gone: §10.2 forbids
settling "which file is this" by name or by position.

The tests below are the parser's four structural refusals plus the template
property: the prompt asks the model for the local key and for nothing the system
binds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_orchestrator.runtime.output_blocks import (
    REPAIR_HINTS,
    BlockError,
    PortClaim,
    parse_port_claims,
    repair_hint,
)
from agent_orchestrator.runtime.role_templates import (
    HIERARCHICAL_WORKER_VERSIONS,
    WORKER,
    WORKER_HIERARCHICAL,
    WORKER_HIERARCHICAL_VERSION,
    WORKER_V2,
    registered_versions,
)

PORTS = ("repository_facts", "review_notes")
PATHS = ("out/facts.json", "out/notes.md", "out/debug.log")


def test_a_claim_names_a_declared_port_and_a_file_this_attempt_wrote() -> None:
    claims = parse_port_claims(
        {"repository_facts": "out/facts.json"},
        declared_ports=PORTS,
        attempt_paths=PATHS,
    )
    assert claims == (PortClaim(port_key="repository_facts", path="out/facts.json"),)


def test_an_absent_outputs_map_is_no_claim_at_all() -> None:
    """A leaf nothing consumes declares no port and says nothing about outputs."""

    assert parse_port_claims(None, declared_ports=(), attempt_paths=PATHS) == ()


def test_a_claim_for_a_port_the_plan_never_declared_is_refused() -> None:
    """Memo test 2.  A port nobody declared is a claim about a contract that does
    not exist — not a typo for the parser to correct."""

    with pytest.raises(BlockError) as refused:
        parse_port_claims({"summary": "out/facts.json"}, declared_ports=PORTS, attempt_paths=PATHS)
    assert refused.value.reason == "output_port_not_declared"
    assert "summary" in str(refused.value)


def test_a_claim_for_a_file_this_attempt_never_wrote_is_refused() -> None:
    """Memo test 3.  The producer may name which of *its* files went where; it may
    not name a file nobody saw it write."""

    with pytest.raises(BlockError) as refused:
        parse_port_claims(
            {"repository_facts": "out/imagined.json"},
            declared_ports=PORTS,
            attempt_paths=PATHS,
        )
    assert refused.value.reason == "output_path_not_produced"


def test_a_value_that_is_not_a_path_is_refused() -> None:
    with pytest.raises(BlockError) as refused:
        parse_port_claims(
            {"repository_facts": {"path": "out/facts.json", "schema": "x"}},
            declared_ports=PORTS,
            attempt_paths=PATHS,
        )
    assert refused.value.reason == "output_path_not_a_string"


def test_a_model_supplied_schema_ref_is_ignored() -> None:
    """Memo test 9 — decision 4's second mutation self-check.

    ``schema`` is a system-bound field: the schema of an edge comes from the
    ``DataRequirement`` that declares it, never from the producer, or a producer
    could relabel its own output and every downstream compatibility check would be
    made against the label rather than the contract.

    The parser only ever reads "port name → path", so a ``schema`` key can arrive in
    exactly two shapes and both are refused: as a *port* (not declared) or inside a
    value (not a string).

    **Mutation**: teach the codec to read it and hand it to
    ``AcceptedOutput.schema_ref``; ``check_against_ports``'s schema comparison in
    ``test_the_indexed_schema_is_the_edges_and_not_the_producers_claim`` goes red.
    """

    with pytest.raises(BlockError) as as_port:
        parse_port_claims({"schema": "out/facts.json"}, declared_ports=PORTS, attempt_paths=PATHS)
    assert as_port.value.reason == "output_port_not_declared"
    assert PortClaim.__slots__ == ("port_key", "path"), "two fields, and no third"


def test_a_single_valued_port_takes_one_file() -> None:
    """§24.1 decision 3: a single-valued port has exactly one binding.

    A JSON object cannot repeat a key, so the duplicate arrives as a list — which is
    refused for not being a path, with the same effect and a clearer message.
    """

    with pytest.raises(BlockError) as refused:
        parse_port_claims(
            {"repository_facts": ["out/facts.json", "out/notes.md"]},
            declared_ports=PORTS,
            attempt_paths=PATHS,
            single_valued=("repository_facts",),
        )
    assert refused.value.reason == "output_path_not_a_string"


def test_outputs_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(BlockError) as refused:
        parse_port_claims(["out/facts.json"], declared_ports=PORTS, attempt_paths=PATHS)
    assert refused.value.reason == "outputs_not_an_object"


def test_every_refusal_has_a_repair_hint_the_model_can_act_on() -> None:
    """§18.5 C8: a malformed block is repaired on the same Attempt, not relaunched."""

    for reason in (
        "outputs_not_an_object",
        "output_port_not_declared",
        "output_port_claimed_twice",
        "output_path_not_a_string",
        "output_path_not_produced",
    ):
        assert reason in REPAIR_HINTS
        hint = repair_hint(BlockError(reason, "detail"), "result_envelope")
        assert "detail" in hint and hint != reason


# ================================================================== the role template
def test_the_worker_template_never_asks_the_model_for_a_system_bound_field() -> None:
    """Memo test 7.

    The model is asked for the local key and nothing else.  Every field an
    ``AcceptedOutput`` carries beyond that — the acceptance id, the content hash, the
    schema reference, the support revision, the producer result — is bound by the
    system (TG design §3.2, §18.5), and a prompt that asked for one would be inviting
    the model to admit its own work.
    """

    instructions = WORKER_HIERARCHICAL.instructions
    for bound in (
        "acceptance_id",
        "content_hash",
        "schema_ref",
        "support_revision",
        "producer_result_id",
        "producer_occurrence",
    ):
        assert bound not in instructions, bound
    assert '"outputs"' in instructions
    assert "declared_output_ports" in instructions


#: The sha256 of every shipped prompt whose bytes a replayable Mission depends on.
#: Frozen **here**, as literals, for the reason the third-round review found the hard
#: way: the previous guard compared the templates against ``git show HEAD:``, and the
#: moment the hierarchical worker was committed ``HEAD`` became the new file — so the
#: test was comparing the templates with themselves, went permanently red on the
#: ``hasattr`` line, and the invariant it stood for ("a shipped prompt never changes
#: its bytes") had no guard left at all.  A digest in the test file cannot move with
#: the tree, needs no ``git`` to check, and says exactly which byte changed when it
#: fails.  This is the same technique the migration checksums use.
#:
#: **Changing a value here is never the fix for a failing assertion.**  An Attempt
#: freezes its ``prompt_version`` and replays on those bytes (§26.3); editing a
#: shipped template in place silently rewrites the past.  A new wording is a new
#: version, registered beside the old one, with its own line below.
FROZEN_PROMPT_DIGESTS: dict[str, tuple[str, str]] = {
    # name: (prompt_version, sha256 of instructions)
    "WORKER": ("worker-v3", "c587ce55ff9a01e38ba5b362f8bb9de518b99404f712e63409f871d2d3f0d285"),
    "WORKER_V2": ("worker-v2", "c0c35d2d2639ea6655c66bf7b30b6f46cf04ffbb458a79caba63b6477ae46f37"),
    "PLANNER": ("planner-v4", "13537f0abf6322c7075af9b5ddb3c0b7316c0271830311f49c3d6c195f5c9aad"),
    "CRITIC": ("critic-v3", "427fb096fc0c4cf6acc67358cd631d3f3c4c39ce2fea60b768a529f6290b7120"),
    "CRITIC_V2": ("critic-v2", "8eb51a32c06bfa16da88e4e89a28f48b50ce2e06969aec467abd803078a1c5ce"),
    # review P1-2: v1 stays frozen because pinned Attempts replay on it; v2 is what a
    # new hierarchical Mission gets, and the only difference is the sentence D3 made
    # untrue ("且下游确有消费者").
    "WORKER_HIERARCHICAL_V1": (
        "worker-hierarchical-v1",
        "e82e74aff9b37d4746da0e982b38855e3cb639efe848fb1a15116a18023e7de2",
    ),
    "WORKER_HIERARCHICAL": (
        "worker-hierarchical-v2",
        "120372b8a49162ab1d96c6cf2725d6fcf7adec1988c7c8646f378c21649870b7",
    ),
    # P2.3g: the synthesiser's v1 is what the Grok episode ran on and stays pinnable;
    # v2 spells the codec's field list.  The hierarchical Planner's v3 is frozen for
    # the same reason, and v4 is v3 minus "write a <method_proposal> instead".
    "METHOD_SYNTHESIZER_V1": (
        "method-synthesizer-v1",
        "9341ab10390015fb45d528d95dac0af5b29658f0ee9b70060369d607f8f0ae32",
    ),
    "METHOD_SYNTHESIZER_V2": (
        "method-synthesizer-v2",
        "27ccb23492ef00b73404735a03f51439d2bf0eb1339a6a6b4320950ee8beaccb",
    ),
    # P2.3i: v3 = v2 plus "schema_feedback may carry the admission protocol's refusal
    # lines, and here is what may change on one"; v2 keeps its bytes above.
    # P2.3i: v3 (protocol refusals travel in schema_feedback); P2.3j merge: the default
    # is v4, revised from v3 with ``review_feedback``.  v3 keeps its bytes.
    "METHOD_SYNTHESIZER_V3": (
        "method-synthesizer-v3",
        "a38309fdb328c929d6ddefa37ff4de294628f0f508dfad82c7a27bb1cd0e6c9c",
    ),
    "METHOD_SYNTHESIZER_V4": (
        "method-synthesizer-v4",
        "8d457abe7a74d614642ac7e2446e656aea9c46e4a7f509820353dd04f93f39b6",
    ),
    # P2.3k / N1: v5 = v4 plus "the step that explains the change must be fed the
    # change through an input port; operators list the latest version only".
    "METHOD_SYNTHESIZER_V5": (
        "method-synthesizer-v5",
        "6973e125b9cc3b02b8af77190a9b0ff4b5a1ddd3cdfe8cc1f60900304fe21e6b",
    ),
    # P2.3m: v6 = v5 plus "a code-change method must contain a write/patch step;
    # read_only_leaf_needs_write in review_feedback".
    "METHOD_SYNTHESIZER": (
        "method-synthesizer-v6",
        "75a8a4a1a888e2ac165d16a711df9e981ad44da92c15b652bbc29bbc97774c42",
    ),
    "PLANNER_HIERARCHICAL_V3": (
        "planner-hierarchical-v3",
        "ba244a12bf504051d7ebc954f462cf23f9c7734dff980c539187834a0a670acb",
    ),
    "PLANNER_HIERARCHICAL_V4": (
        "planner-hierarchical-v4",
        "5ae3b39acf9326888e20bab934848ed6e1d21482884ccda932ac693b31122223",
    ),
    # P2.3j: the repair-round Planner prompt.  v5 reads ``rejected_refinements`` and
    # knows the retire_method + refine repair.
    "PLANNER_HIERARCHICAL_V5": (
        "planner-hierarchical-v5",
        "2517d5fe727ba27ffffa105d72e786e72109893c342e56adebeaa033794f7608",
    ),
}

#: P2.3d / defect D1: the same freeze for the versions a *domain module* registers.
#: They are not module attributes, so they are looked up in ``TEMPLATE_VERSIONS`` —
#: the base is in the table too, because "the hierarchical one is the AppWorld one
#: plus an ``outputs`` field" is only true while the base does not move.
FROZEN_REGISTERED_DIGESTS: dict[str, str] = {
    "worker-appworld-v3": "8fbea8282c1e8f4814e75fc9943bc41da21016db0af2026fe037f001b81bdd88",
    "worker-appworld-hierarchical-v1": (
        "9ad842af04458dd7f57d1935fb669a57fdb4b63d850427b5974716e3da994917"
    ),
}


@pytest.mark.parametrize("version", sorted(FROZEN_REGISTERED_DIGESTS))
def test_a_registered_domain_prompt_keeps_its_bytes(version: str) -> None:
    import hashlib

    from agent_orchestrator.runtime.role_templates import TEMPLATE_VERSIONS

    template = TEMPLATE_VERSIONS["worker"][version]
    digest = hashlib.sha256(template.instructions.encode("utf-8")).hexdigest()
    assert digest == FROZEN_REGISTERED_DIGESTS[version], (
        f"{version} changed its bytes; an Attempt replays on the prompt it froze, so a "
        "new wording is a new version registered beside this one — never an edit of it"
    )


@pytest.mark.parametrize("name", sorted(FROZEN_PROMPT_DIGESTS))
def test_a_shipped_prompt_keeps_its_bytes(name: str) -> None:
    """Every shipped template still hashes to the digest frozen beside it.

    The hierarchical prompt is a *new version*, not an edit of the shipped ones, and
    this is what says so: ``worker-v3`` and ``worker-v2`` are the bytes every
    replayable Mission ran on, and ``worker-hierarchical-v1`` is now one of them too.
    """

    import hashlib

    from agent_orchestrator.runtime import role_templates

    template = getattr(role_templates, name)
    version, digest = FROZEN_PROMPT_DIGESTS[name]
    assert template.prompt_version == version
    assert hashlib.sha256(template.instructions.encode("utf-8")).hexdigest() == digest, (
        f"{name} ({version}) changed its bytes; an Attempt replays on the prompt it froze, "
        "so a new wording is a new version registered beside this one — never an edit of it"
    )


def test_the_frozen_digests_cover_the_prompts_this_slice_depends_on() -> None:
    """A digest table nobody extends stops guarding what the deployment added.

    The hierarchical worker is the one this slice introduced, and it is in the table;
    the four DAG-mode templates it must not have touched are in it too.
    """

    assert {"WORKER", "WORKER_V2", "WORKER_HIERARCHICAL"} <= set(FROZEN_PROMPT_DIGESTS)
    assert "WORKER_HIERARCHICAL_V1" in FROZEN_PROMPT_DIGESTS, (
        "a superseded version is still replayed by every Attempt that pinned it"
    )
    assert {"PLANNER", "CRITIC", "CRITIC_V2"} <= set(FROZEN_PROMPT_DIGESTS)
    assert FROZEN_PROMPT_DIGESTS["WORKER_HIERARCHICAL"][0] == WORKER_HIERARCHICAL_VERSION
    assert WORKER.prompt_version == "worker-v3"
    assert WORKER_V2.prompt_version == "worker-v2"


def test_the_hierarchical_worker_version_is_registered_and_pinnable() -> None:
    """P2.3d / defect D1 widened this set; what it must never hold is unchanged.

    It used to be a one-element frozenset, and that was the defect's other half:
    ``_hierarchical_worker_template`` treated "not in this set" as "replace with the
    code-domain prompt", so an AppWorld Worker holding ``worker-appworld-v3`` lost its
    domain tools and its domain words on every hierarchical Mission.
    """

    versions = registered_versions()
    assert WORKER_HIERARCHICAL_VERSION in versions["worker"]
    assert HIERARCHICAL_WORKER_VERSIONS == frozenset(
        {
            WORKER_HIERARCHICAL_VERSION,
            "worker-hierarchical-v1",
            "worker-appworld-hierarchical-v1",
        }
    ), "every version a deployment may pin, superseded ones included"
    assert "worker-v3" not in HIERARCHICAL_WORKER_VERSIONS, (
        "a DAG-mode pin must not be honoured in the hierarchical mode: worker-v3 "
        "never asks for outputs, and every leaf would then be refused as unclaimed"
    )
    assert "worker-appworld-v3" not in HIERARCHICAL_WORKER_VERSIONS, (
        "the AppWorld DAG-mode prompt is not a hierarchical one either: it never asks "
        "for outputs, and a pin naming it belongs to the other mode"
    )


# ============================================== the paths a claim is checked against
def test_a_claim_is_checked_against_the_files_this_envelope_declares() -> None:
    """Part 2d smoke, round 1 — a real defect, kept as a test.

    The first wiring checked the claimed path against ``store.list_artifacts(attempt)``.
    Those rows are written from ``envelope.artifacts`` **after** the result is accepted,
    so at parse time the list is empty and every honest claim was refused with
    ``produced: []``; the real model spent all three attempts being told its own file
    did not exist.  The paths a claim may name are the ones this envelope declares —
    and each of those is separately checked against the real workspace before it
    becomes an artifact, so nothing is taken on trust that was not.
    """

    declared_in_envelope = ("facts.json", "notes.md")
    claims = parse_port_claims(
        {"repository_facts": "facts.json"},
        declared_ports=("repository_facts",),
        attempt_paths=declared_in_envelope,
    )
    assert claims == (PortClaim(port_key="repository_facts", path="facts.json"),)
    with pytest.raises(BlockError) as refused:
        parse_port_claims(
            {"repository_facts": "facts.json"},
            declared_ports=("repository_facts",),
            attempt_paths=(),
        )
    assert refused.value.reason == "output_path_not_produced"


# ======================================================================================
# The two wiring steps decision 4 rests on (third-round review P1-B)
#
# The parsing side, the pairing side and the refusing side all had tests.  The two
# steps *between* them did not, and both mutants survived the whole suite: stop
# handing the model the port names, and stop swapping in the prompt that asks for
# them.  Either one on its own reproduces part 2c's round-9 blocker — every leaf
# refused with ``OUTPUT_PORT_UNCLAIMED`` and the Mission stopped with no dispatchable
# work — so both are asserted here against a real dispatch intent.
# ======================================================================================

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_end_to_end import (  # noqa: E402
    HIERARCHICAL_SEMANTICS,
    build_world,
    committed,
)

from agent_orchestrator.orchestrator.accepted_outputs import (  # noqa: E402
    declared_ports_in_revision,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402


def _leaf_intent(tmp_path, *, mode: str = HIERARCHICAL_SEMANTICS, pin: str | None = None):
    """Drive one real ``_decide`` and return the worker intent it created.

    The intent is *created* by ``_decide`` and only dispatched by a later phase of
    the cycle, so nothing here needs a model to answer — which is what makes this a
    test of the wiring rather than of a reply.
    """

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = (
        committed(evidence, key=f"p23c-ports-{mode}-{pin}", mode=mode, demand=True)
        if mode == HIERARCHICAL_SEMANTICS
        else build_world(evidence, key=f"p23c-ports-{mode}-{pin}", mode=mode)
    )
    world.store.close()
    config = OrchestratorConfig(evidence_root=evidence, max_concurrency=1, test_timeout_seconds=5)

    async def case():
        async with Orchestrator(config, RoleScriptedProvider({"worker": []})) as loop:
            if mode == HIERARCHICAL_SEMANTICS:
                world.env.semantics = HtnStore(loop.store)
                loop.install_hierarchical(planning=world.env)
            else:
                loop.install_hierarchical(planning=None)
            if pin is not None:
                # The deployment's frozen ``prompt_versions``, injected where the loop
                # reads it.  A pin is a real deployment fact (§26.3), and the question
                # this test asks is what the hierarchical branch does with one.
                version = loop.policy_version_of(world.mission.id)
                policy = dict(loop.policy_for(world.mission.id))
                policy["prompt_versions"] = {
                    **dict(policy.get("prompt_versions") or {}),
                    "worker": pin,
                }
                loop._policies[version] = policy
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            await loop._decide(mission)
            intents = [
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == world.mission.id and item.kind == "attempt"
            ]
            ports = {}
            if mode == HIERARCHICAL_SEMANTICS:
                semantics = HtnStore(loop.store)
                active = semantics.active_plan_revision(world.mission.id)
                assert active is not None
                task_id = None if not intents else intents[0].config.get("task_id")
                occurrence = next(
                    str(item.occurrence_id)
                    for item in semantics.list_plan_memberships(
                        world.mission.id, int(active.revision)
                    )
                    if task_id is None or str(item.task_id) == str(task_id)
                )
                ports = declared_ports_in_revision(
                    semantics.list_data_requirements(world.mission.id, int(active.revision)),
                    occurrence,
                )
            return intents, ports

    return asyncio.run(case())


def _content(intent) -> str:
    """The text the model is actually handed for this intent."""

    message = intent.config.get("message")
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(message or "")


def _section(message: str, name: str) -> dict:
    """The typed context section the loop sealed into the model's message.

    The message is a rendered package, so the section is found by its heading and
    decoded from the JSON that follows it — which also proves the model is handed the
    ports as *data* with a heading, not as prose.
    """

    heading = f"## {name}\n"
    assert heading in message, name
    tail = message[message.index(heading) + len(heading) :]
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(tail.lstrip())
    return value


def test_the_leaf_is_told_which_output_ports_its_occurrence_declares(tmp_path) -> None:
    """Mutation M18: stop putting ``declared_output_ports`` in the typed context.

    Without it the model is never told a port name exists, so every honest envelope
    omits ``outputs`` and ``accept_review`` refuses the leaf with
    ``OUTPUT_PORT_UNCLAIMED``.  The set is the **plan's**, so it is compared against
    ``declared_ports_in_revision`` rather than against a literal.
    """

    intents, ports = _leaf_intent(tmp_path)
    assert intents, "a demanded leaf reaches the allocator and gets an intent"
    message = _content(intents[0])
    assert "declared_output_ports" in message
    section = _section(message, "declared_output_ports")
    assert section["data_not_instruction"] is True
    assert {item["port"] for item in section["ports"]} == set(ports)
    assert set(ports), "the fixture's leaf really does declare a consumed port"


def test_a_legacy_leaf_is_never_handed_a_declared_output_ports_section(tmp_path) -> None:
    """§18.5 rule 1: ``ResultEnvelope`` refuses unknown keys, so the DAG mode must not
    be shown a key its contract has no field for."""

    intents, _ = _leaf_intent(tmp_path, mode="legacy")
    for intent in intents:
        assert "declared_output_ports" not in _content(intent)


def test_a_dag_mode_pin_does_not_reach_a_hierarchical_leaf(tmp_path) -> None:
    """Mutation M19: stop swapping in the hierarchical Worker template.

    ``worker-v3`` never asks the model which port its files belong to.  A deployment
    that pins it is pinning the *other mode's* prompt, and honouring that pin here is
    the same round-9 blocker by another route.
    """

    intents, _ = _leaf_intent(tmp_path, pin="worker-v3")
    assert intents
    assert intents[0].config["prompt_version"] == WORKER_HIERARCHICAL_VERSION


def test_a_dag_mode_pin_is_honoured_on_a_legacy_mission(tmp_path) -> None:
    """The other half: the pin is not ignored, it belongs to the mode that has it."""

    intents, _ = _leaf_intent(tmp_path, mode="legacy", pin="worker-v3")
    for intent in intents:
        assert intent.config["prompt_version"] == "worker-v3"


# ======================================================================================
# review P1-2: the prompt a Mission actually gets must agree with D3's rule
# ======================================================================================


def test_no_hierarchical_worker_prompt_makes_the_claim_conditional_on_a_consumer() -> None:
    """The words the Worker reads and the rule Acceptance applies are one rule.

    ``worker-hierarchical-v1`` says a port must be claimed when it is ``required=true``
    **and has a downstream consumer**.  That was true when the port set *was* "consumed
    by a DataRequirement" — and D3 is precisely the change that made it false: the
    finalizer's port has no consumer by construction, is declared anyway, and a leaf
    that skips it is refused with ``OUTPUT_PORT_UNCLAIMED``.  The code-domain path is
    the one that lost ten episodes, and its Worker was reading the older rule.

    The context package carries no "has a consumer" field either
    (``declared_output_ports_for`` gives port/required/cardinality/schema), so the
    sentence asks the model to apply a test it cannot run.
    """

    from agent_orchestrator.governance.domains import DOMAINS
    from agent_orchestrator.runtime.role_templates import (
        TEMPLATE_VERSIONS,
        WORKER_HIERARCHICAL,
        hierarchical_worker_for_domain,
    )

    selected = {WORKER_HIERARCHICAL.prompt_version: WORKER_HIERARCHICAL.instructions}
    for profile in DOMAINS.values():
        chosen = hierarchical_worker_for_domain(profile)
        selected[chosen.prompt_version] = chosen.instructions
    offenders = [
        version for version, text in selected.items() if "下游确有消费者" in text
    ]
    assert not offenders, (
        f"{offenders} still condition the claim on a downstream consumer, which D3 "
        "removed from the rule Acceptance applies"
    )
    for version, text in selected.items():
        # The AppWorld text is written without the spaces the code-domain one uses.
        assert "required=true的端口必须被认领" in text.replace(" ", ""), version
    # v1 is kept registered and frozen: an Attempt replays on the bytes it pinned, so
    # the old wording has to stay readable — it just is not what a new Mission gets.
    assert "下游确有消费者" in TEMPLATE_VERSIONS["worker"]["worker-hierarchical-v1"].instructions
