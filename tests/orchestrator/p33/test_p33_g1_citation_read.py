# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""G1 read oracle before implementation; real recorded/accepted citation receipts.

Pages are exact original Unicode/newlines of the frozen display block, not its
preview or a newly chosen active version. The index belongs to receipt.evidence_refs,
not the proposal order. Current source state is a separate read-only annotation.
Invalid receipt ownership cannot read bytes. No failed result gains a fake receipt.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import replace

import pytest
from test_p33_source_commits import PATH, accept, attach, change_source, historical, produce, submit
from test_p33_source_commits import e_scenes as source_scenes

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.contracts import SourceCitation, TaskStatus
from agent_orchestrator.contracts.models import canonical_json
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.storage.store import Store

e_scenes = source_scenes
QUOTE = "资料记录可行。"


def accepted(factory, *, newline="\n", tail="", citations=None):
    heading = "# e\u0301 条件" + newline
    block = QUOTE + tail + newline
    s = factory(sources={PATH: heading + block})
    version = s.store.get_source(s.mission.id, PATH)["version_hash"]
    e = submit(
        s, content=QUOTE, citations=citations or (SourceCitation(PATH, version, 2, 2, QUOTE),)
    )
    assert produce(e).status == "PASS"
    assert accept(e).status is TaskStatus.COMPLETED
    rows = s.store.list_criterion_assessments(s.mission.id, result_id=e.envelope.id)
    assert len(rows) == 1
    return s, e, rows[0], heading, block


def read(s, e, row, **overrides):
    return s.api.citation_read(
        s.mission.id,
        **{
            "result_id": e.envelope.id,
            "receipt_id": row["receipt_id"],
            "citation_index": 0,
            **overrides,
        },
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_full_block_pages_preserve_original_newlines_unicode_and_parent_heading(e_scenes, newline):
    # >256KiB, a decomposed accent and emoji crossing character page boundaries.
    s, e, row, heading, block = accepted(e_scenes, newline=newline, tail="e\u0301😀" * 70000 + "。")
    before = historical(s)
    assert row["evidence_refs"][0]["display_block"]["truncated"] is True
    offset, pages, identity = 0, [], None
    while True:
        page = read(s, e, row, offset=offset, limit=65536)
        assert page["text"] == block[offset : offset + 65536]
        assert page["offset"] == offset and page["total_chars"] == len(block)
        assert page["historical_verdict"] == "PASS"
        assert page["source_trust"] == "untrusted_external"
        assert page["scope_limited_to_source"] is True
        assert page["trust_marker"] and page["scope_marker"]
        assert page["parent_headings"][0]["text"] == heading
        key = (page["path"], page["version_hash"], page["block_id"], page["receipt_id"])
        if identity is not None:
            assert key == identity
        identity = key
        pages.append(page["text"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(page["text"])
        offset = page["next_offset"]
    assert "".join(pages).encode() == block.encode()
    assert historical(s) == before


def test_index_is_sorted_receipt_reference_order_not_original_proposal_order(e_scenes):
    second = "sources/b.md"
    s = e_scenes(sources={PATH: QUOTE, second: "乙资料。"})
    a = SourceCitation(PATH, s.store.get_source(s.mission.id, PATH)["version_hash"], 1, 1, QUOTE)
    b = SourceCitation(
        second, s.store.get_source(s.mission.id, second)["version_hash"], 1, 1, "乙资料。"
    )
    e = submit(s, citations=(b, a), content=QUOTE)
    assert produce(e).status == "PASS"
    assert accept(e).status is TaskStatus.COMPLETED
    [row] = s.store.list_criterion_assessments(s.mission.id, result_id=e.envelope.id)
    for index, ref in enumerate(row["evidence_refs"]):
        page = read(s, e, row, citation_index=index)
        assert page["path"] == ref["target"]
        assert page["version_hash"] == ref["source_version"]
        assert page["text"] == ref["ref"]["quote"]


@pytest.mark.parametrize("mode", ["supersede", "revoke"])
def test_current_state_changes_without_rewriting_historical_pages_or_receipts(e_scenes, mode):
    s, e, row, _, block = accepted(e_scenes)
    first = read(s, e, row)
    original = historical(s)
    change_source(s, mode=mode)
    page = read(s, e, row)
    assert page["text"] == block and page["block_id"] == first["block_id"]
    assert page["version_hash"] == first["version_hash"]
    assert page["historical_verdict"] == first["historical_verdict"] == "PASS"
    state = page["source_state"]
    assert state["revision"] > first["source_state"]["revision"]
    assert state["revoked"] is (mode == "revoke")
    if mode == "supersede":
        assert state["superseded_by"] == state["active_version_hash"] != page["version_hash"]
    else:
        assert state["active_version_hash"] is None
    assert page["through_seq"] > first["through_seq"]
    assert historical(s) == original


def test_historical_claim_revision_does_not_become_current_revision_binding(e_scenes):
    s, e, row, _, block = accepted(e_scenes)
    claim = s.store.get_claim(row["claim_id"])
    s.store.upsert_claim(replace(claim, version=claim.version + 1))
    assert read(s, e, row)["text"] == block


def test_citation_page_reopens_from_receipt_after_source_revocation(e_scenes):
    s, e, row, _, block = accepted(e_scenes, newline="\r\n", tail="e\u0301😀\u0085后续。")
    first = read(s, e, row, limit=5)
    change_source(s, mode="revoke")
    before = historical(s)
    with closing(Store.open(s.store.path)) as store:
        reopened = attach(s, store)
        second = read(reopened, e, row, offset=first["next_offset"])
        assert first["text"] + second["text"] == block
        assert second["block_id"] == first["block_id"]
        assert second["source_state"]["revoked"] is True
        assert second["source_state"]["active_version_hash"] is None
        assert second["historical_verdict"] == "PASS"
        assert historical(reopened) == before


@pytest.mark.parametrize(
    "damage", ["tenant", "mission", "result", "receipt", "index", "negative_index", "bool_index"]
)
def test_unknown_or_foreign_receipt_identity_is_uniform_not_found(e_scenes, damage):
    s, e, row, _, _ = accepted(e_scenes)
    api, mid = s.api, s.mission.id
    args = dict(result_id=e.envelope.id, receipt_id=row["receipt_id"], citation_index=0)
    if damage == "tenant":
        api = MissionControlV1(s.host, tenant_id="foreign", principal=Principal("foreign"))
    elif damage == "mission":
        mid = "missing"
    elif damage in {"result", "receipt"}:
        args[damage + "_id"] = "missing"
    else:
        args["citation_index"] = {"index": 999, "negative_index": -1, "bool_index": True}[damage]
    before = historical(s)
    with pytest.raises(FacadeError) as error:
        api.citation_read(mid, **args)
    assert (error.value.code, str(error.value)) == ("not_found", "no such object for this caller")
    assert historical(s) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("offset", -1),
        ("offset", True),
        ("offset", 10**9),
        ("limit", 0),
        ("limit", 65537),
        ("limit", True),
    ],
)
def test_character_pagination_rejects_invalid_bounds(e_scenes, field, value):
    s, e, row, _, _ = accepted(e_scenes)
    with pytest.raises(FacadeError) as error:
        read(s, e, row, **{field: value})
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize(
    "damage", ["source_bytes", "source_missing", "output_bytes", "receipt_body"]
)
def test_integrity_failure_never_falls_back_to_preview_or_active_source(e_scenes, damage):
    s, e, row, _, _ = accepted(e_scenes)
    if damage == "receipt_body":
        changed = deepcopy(row)
        changed["evidence_refs"][0]["display_block"]["start_line"] = 1
        with s.store.transaction() as db:
            db.execute(
                "UPDATE criterion_assessments SET json=? WHERE receipt_id=?",
                (canonical_json(changed), row["receipt_id"]),
            )
        assert s.store.list_criterion_assessments(s.mission.id, result_id=e.envelope.id)[0] != row
    else:
        version = (
            e.artifact.content_hash
            if damage == "output_bytes"
            else row["evidence_refs"][0]["source_version"]
        )
        path = s.cas.path_for(version)
        if damage == "source_missing":
            path.unlink()
        else:
            path.chmod(0o600)
            path.write_bytes(b"tampered")
    before = historical(s)
    with pytest.raises(FacadeError) as error:
        read(s, e, row)
    assert error.value.code == ("not_found" if damage == "receipt_body" else "integrity_error")
    assert historical(s) == before


def test_failed_result_does_not_gain_an_accepted_receipt(e_scenes):
    s = e_scenes()
    version = s.store.get_source(s.mission.id, PATH)["version_hash"]
    e = submit(s, citations=(SourceCitation(PATH, version, 1, 1, "虚构引文。"),))
    layer = produce(e)
    assert layer.status == "FAIL"
    s.commit.fail_result(e.envelope.id, failures=(layer.to_json(),))
    assert s.store.list_criterion_assessments(s.mission.id, result_id=e.envelope.id) == []
    before = historical(s)
    with pytest.raises(FacadeError) as error:
        s.api.citation_read(
            s.mission.id, result_id=e.envelope.id, receipt_id="fake", citation_index=0
        )
    assert error.value.code == "not_found"
    assert historical(s) == before
