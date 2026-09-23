# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""ARP codec against the frozen contract fixtures (ARP-EXEC-1.1.1 reference oracle).

Ports the delivered ``tests/test_contracts.py`` of the plan package onto the
production codec: every typed fixture is structurally and semantically valid, every
strict object refuses an unknown field, and each named counter-example is refused
with the catalogue code the contract names.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from simple_harness.agents.arp import codec
from simple_harness.agents.arp.errors import ArpError, is_known
from simple_harness.agents.arp.strict import canonical

DATA = Path(__file__).parent / "data"
FIXTURES = json.loads((DATA / "typed-fixtures.json").read_text(encoding="utf-8"))["fixtures"]


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_typed_fixture_is_valid(name: str) -> None:
    codec.check(name, FIXTURES[name])
    # Round trip through bytes takes the same path production storage takes.
    codec.decode(name, codec.encode(name, FIXTURES[name]))


@pytest.mark.parametrize(
    "name",
    sorted(
        n
        for n, v in FIXTURES.items()
        if isinstance(v, dict) and codec.definition(n).get("additionalProperties") is False
    ),
)
def test_unknown_field_refused_for_every_strict_object(name: str) -> None:
    with pytest.raises(ArpError) as info:
        codec.decode(name, canonical(dict(FIXTURES[name], unexpected=True)))
    assert info.value.code == "UNKNOWN_FIELD"


def test_every_schema_type_has_a_fixture() -> None:
    assert set(codec.definitions()) == set(FIXTURES)


def test_all_reported_pin_kinds_exist() -> None:
    for kind in (
        "profile",
        "journal_record",
        "input_manifest",
        "agent_turn",
        "receipt",
        "context",
        "retrieval",
        "tool_snapshot",
        "skill_use",
        "occurrence",
        "completion_scope",
    ):
        codec.validate(
            {"kind": kind, "id": "x", "revision": 1, "content_hash": "a" * 64},
            codec.definition("Pin"),
        )


def _refused(name: str, value: object, code: str | None = None) -> None:
    with pytest.raises(ArpError) as info:
        codec.decode(name, canonical(value))
    assert is_known(info.value.code), info.value.code
    if code is not None:
        assert info.value.code == code, info.value


def test_cross_kind_pin_refused() -> None:
    manifest = copy.deepcopy(FIXTURES["ContextManifest"])
    manifest["profile_ref"]["kind"] = "agent"
    _refused("ContextManifest", manifest, "ENUM")


def test_pagination_progress_page_is_expressible() -> None:
    page = copy.deepcopy(FIXTURES["SearchPage"])
    page.update(next_cursor="token", has_more=True, page_semantics="PROGRESS")
    page["receipt"].update(status="PARTIAL", query_result_count=None, has_more=True)
    page["receipt"]["coverage"].update(
        phase="SCANNING", rank_scope="NONE", ranking_final=False, snapshot_chunks=1
    )
    codec.decode("SearchPage", canonical(page))


def test_history_cursor_flags_inconsistent_refused() -> None:
    page = copy.deepcopy(FIXTURES["HistoryReadPage"])
    page["has_more"] = not page["has_more"]
    _refused("HistoryReadPage", page, "STATE_COMBINATION_INVALID")


def test_search_cursor_flags_inconsistent_refused() -> None:
    page = copy.deepcopy(FIXTURES["SearchPage"])
    page["has_more"] = True
    _refused("SearchPage", page)


def test_scanning_cannot_publish_final_rank() -> None:
    page = copy.deepcopy(FIXTURES["SearchPage"])
    page["receipt"]["coverage"]["phase"] = "SCANNING"
    _refused("SearchPage", page, "RETRIEVAL_STATE_INVALID")


def test_complete_index_cannot_be_partial() -> None:
    coverage = copy.deepcopy(FIXTURES["SearchCoverage"])
    coverage.update(expected_groups=2, indexed_groups=1)
    _refused("SearchCoverage", coverage, "RETRIEVAL_STATE_INVALID")


def test_history_slice_hash_required() -> None:
    piece = copy.deepcopy(FIXTURES["HistorySlice"])
    piece["slice_hash"] = "a" * 64
    _refused("HistorySlice", piece, "REF_IDENTITY_MISMATCH")


def test_settings_expected_revision_pair() -> None:
    request = copy.deepcopy(FIXTURES["HostRequest"])
    request.update(
        verb="agent_context_settings_update",
        payload=FIXTURES["ContextSettingsCommand"],
        expected_revision=4,
    )
    _refused("HostRequest", request, "EXPECTED_REVISION_MISMATCH")


def test_settings_command_flow_decodes() -> None:
    flow = json.loads((DATA / "host-settings-flow.json").read_text(encoding="utf-8"))["flow"]
    for item in flow:
        codec.decode("HostRequest", canonical(item["request"]))


def test_host_response_with_wrong_result_type_refused() -> None:
    response = copy.deepcopy(FIXTURES["HostResponse"])
    response["items"] = [FIXTURES["SessionView"]]
    _refused("HostResponse", response)


def test_request_cannot_supply_two_payloads() -> None:
    request = copy.deepcopy(FIXTURES["HostRequest"])
    request["payload_ref"] = dict(FIXTURES["Pin"], kind="artifact")
    _refused("HostRequest", request, "HOST_PAYLOAD_MISMATCH")


def test_catalogue_800_files_summary_and_pages() -> None:
    files = [
        dict(FIXTURES["SkillFile"], relative_path=f"references/doc-{i:04}.md") for i in range(800)
    ]
    definition = copy.deepcopy(FIXTURES["Skill"])
    definition["files"] = files
    codec.decode("Skill", canonical(definition))
    summary = copy.deepcopy(FIXTURES["SkillCatalogueItem"])
    summary.update(file_count=800, total_payload_bytes=123548)
    codec.decode("SkillCatalogueItem", canonical(summary))
    assert len(canonical(summary)) < 4096
    seen: list[dict] = []
    for offset in range(0, 800, 64):
        page = copy.deepcopy(FIXTURES["SkillDetailsPage"])
        page.update(
            files=files[offset : offset + 64],
            has_more=offset + 64 < 800,
            next_cursor="next" if offset + 64 < 800 else None,
        )
        codec.decode("SkillDetailsPage", canonical(page))
        assert len(canonical(page)) < 65536
        seen.extend(page["files"])
    assert seen == files


def test_summary_jobs_not_enabled() -> None:
    body = copy.deepcopy(FIXTURES["RuntimeJobChangedBody"])
    body["kind"] = "SUMMARY"
    _refused("RuntimeJobChangedBody", body, "ENUM")


def test_no_replacement_ordinal_for_unknown() -> None:
    disposition = copy.deepcopy(FIXTURES["PreparedRequestDisposition"])
    disposition.update(action="RECONCILE_ORIGINAL", replacement_ordinal=2)
    _refused("PreparedRequestDisposition", disposition)


def test_deletion_requires_all_seven_collections() -> None:
    disposal = copy.deepcopy(FIXTURES["CompleteSessionDisposal"])
    disposal["collections"].pop()
    _refused("CompleteSessionDisposal", disposal)


def test_context_and_meter_hashes_must_match() -> None:
    manifest = copy.deepcopy(FIXTURES["ContextManifest"])
    manifest["planned_request_hash"] = "0" * 64
    _refused("ContextManifest", manifest, "REQUEST_HASH_MISMATCH")


def test_bool_is_never_an_integer_and_duplicate_keys_refused() -> None:
    pin = dict(FIXTURES["Pin"], revision=True)
    _refused("Pin", pin, "TYPE")
    raw = b'{"kind":"agent","kind":"agent","id":"x","revision":1,"content_hash":"' + b"a" * 64 + b'"}'
    with pytest.raises(ArpError) as info:
        codec.decode("Pin", raw)
    assert info.value.code == "DUPLICATE_JSON_KEY"


def test_document_size_limits_are_per_type() -> None:
    assert codec.max_bytes_for("ContextManifest") == 8 * 1024 * 1024
    assert codec.max_bytes_for("PurgeProgress") == 16 * 1024
    with pytest.raises(ArpError) as info:
        codec.decode("PurgeProgress", b" " * (16 * 1024 + 1))
    assert info.value.code == "DOCUMENT_TOO_LARGE"
