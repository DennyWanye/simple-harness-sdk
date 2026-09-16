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


def test_the_older_worker_prompts_keep_their_bytes() -> None:
    """The hierarchical prompt is a *new version*, not an edit of the shipped ones.

    ``worker-v3`` and ``worker-v2`` are what every replayable Mission ran on, so the
    check is a byte comparison against the file as it stands at ``HEAD``: the module
    is loaded from ``git show`` into a throwaway namespace and the two templates'
    instructions are compared verbatim.
    """

    import subprocess
    import sys
    import types

    head = subprocess.run(
        ["git", "show", "HEAD:src/agent_orchestrator/runtime/role_templates.py"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[3]),
    ).stdout
    name = "agent_orchestrator.runtime._role_templates_at_head"
    module = types.ModuleType(name)
    module.__file__ = "role_templates_at_head.py"
    module.__package__ = "agent_orchestrator.runtime"
    sys.modules[name] = module
    try:
        exec(compile(head, "role_templates_at_head.py", "exec"), module.__dict__)  # noqa: S102
        assert module.WORKER.instructions == WORKER.instructions
        assert module.WORKER_V2.instructions == WORKER_V2.instructions
        assert module.WORKER.prompt_version == WORKER.prompt_version == "worker-v3"
        assert not hasattr(module, "WORKER_HIERARCHICAL"), "the new version is new"
    finally:
        sys.modules.pop(name, None)


def test_the_hierarchical_worker_version_is_registered_and_pinnable() -> None:
    versions = registered_versions()
    assert WORKER_HIERARCHICAL_VERSION in versions["worker"]
    assert HIERARCHICAL_WORKER_VERSIONS == frozenset({WORKER_HIERARCHICAL_VERSION})
    assert "worker-v3" not in HIERARCHICAL_WORKER_VERSIONS, (
        "a DAG-mode pin must not be honoured in the hierarchical mode: worker-v3 "
        "never asks for outputs, and every leaf would then be refused as unclaimed"
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
