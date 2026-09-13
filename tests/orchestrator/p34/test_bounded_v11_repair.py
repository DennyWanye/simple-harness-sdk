"""P34 v11 repair controls: real COMPARE request contract and read-page proof."""

import asyncio
import hashlib
import json

import pytest
from test_candidate_selection_runtime import setup, until
from test_real_search_value import _ObservedProvider

from agent_orchestrator.contracts import TaskStatus
from agent_orchestrator.memory.verified_knowledge import KnowledgeIndex
from agent_orchestrator.testing.fixtures import package_of, role_of
from agent_orchestrator.verification.deterministic_checks import check_used_knowledge
from simple_harness.contracts import CallId, RequestId
from simple_harness.contracts.messages import Message, MessageRole
from simple_harness.providers import CancelToken, ProviderRequest, ProviderResponse


def test_compare_empty_catalog_in_actual_request_and_invalid_ids_still_rejected(tmp_path):
    async def exercise():
        orch, provider, task = await setup(tmp_path)
        try:
            await until(orch, lambda: orch.store.get_task(task.id).status is TaskStatus.COMPLETED)
            requests = [r for r in provider.requests if role_of(r) == "synthesizer"]
            assert requests
            for request in requests:
                package = package_of(request)
                assert package["verified_knowledge"] == []
                attempt = orch.store.get_attempt(package["attempt"]["attempt_id"])
                assert attempt.prompt_version.endswith(":compare-v2")
                assert len(package["selection_inputs"]["inputs"]) == 4
                instructions = "\n".join(
                    str(m.content) for m in request.messages if m.role is MessageRole.SYSTEM
                )
                assert (
                    "used_knowledge只能填写当前ContextPackage.verified_knowledge中的真实id"
                    in instructions
                )
                assert "若该目录为空，填写空数组[]" in instructions
                assert "fragment_id仅用于输入血缘与材料" in instructions
                index = KnowledgeIndex.load(orch.store, task.mission_id)
                assert check_used_knowledge((), index) == []
                for false_id in (
                    "unknown-id", package["selection_inputs"]["inputs"][0]["artifact_id"]
                ):
                    assert check_used_knowledge((false_id,), index)
            accepted = orch.store.get_result(orch.store.get_task(task.id).accepted_result_id)
            assert accepted.verdict == "PASS" and accepted.envelope.used_knowledge == ()
        finally:
            await orch.__aexit__(None, None, None)

    asyncio.run(exercise())


class _NoNetwork:
    target = "fixture"

    async def invoke(self, request, *, cancel):
        return ProviderResponse(request.request_id, Message(MessageRole.ASSISTANT, "ok"))


def _digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _observe(provider, attempt, path, content, *, offset, next_offset, total, sha):
    call_id = CallId(f"read-{len(provider.calls)}")
    provider.read_calls[(attempt, str(call_id))] = path
    value = {"path": path, "content": content, "page_schema": "workspace-read-v1",
             "offset": offset, "next_offset": next_offset, "total_chars": total, "sha256": sha}
    request = ProviderRequest(RequestId(f"page-{len(provider.calls)}"), (
        Message(MessageRole.TOOL, json.dumps({"outcome": "succeeded", "value": value}),
                call_id=call_id),
        Message(MessageRole.USER, '## attempt\n{"attempt_id":"' + attempt + '"}'),
    ))
    await provider.invoke(request, cancel=CancelToken())


def test_full_paginated_read_proves_exact_utf8_bytes_without_persisting_contents():
    async def exercise():
        provider = _ObservedProvider(_NoNetwork())
        body = "A" * 4000 + "中🙂" + "B" * 7287  # 11289 code points; three tool-size pages
        digest = _digest(body)
        await _observe(provider, "C", "DOCS.md", body[:4052], offset=0,
                       next_offset=4052, total=len(body), sha=digest)
        assert not provider.saw_bytes("C", "DOCS.md", digest)
        await _observe(provider, "C", "DOCS.md", body[4052:8104], offset=4052,
                       next_offset=8104, total=len(body), sha=digest)
        await _observe(provider, "C", "DOCS.md", body[4050:8104], offset=4050,
                       next_offset=8104, total=len(body), sha=digest)
        await _observe(provider, "C", "DOCS.md", body[8104:], offset=8104,
                       next_offset=None, total=len(body), sha=digest)
        assert provider.saw_bytes("C", "DOCS.md", digest)
        assert not provider.saw_bytes("S", "DOCS.md", digest)
        assert not provider.saw_bytes("C", "other.md", digest)
        assert all("content" not in row for row in provider.reads)
        assert body[:50] not in repr(provider.reads)

    asyncio.run(exercise())


@pytest.mark.parametrize("fault", [
    "gap", "mixed_attempt", "wrong_sha", "wrong_bytes", "overlap_mismatch",
    "contradictory_overlap", "missing_tail", "inconsistent_total", "false_terminal",
])
def test_paginated_read_rejects_incomplete_or_inconsistent_proof(fault):
    async def exercise():
        provider = _ObservedProvider(_NoNetwork())
        body = "abcdefghi中文"
        digest = _digest(body)
        await _observe(provider, "C", "DOCS.md", body[:5], offset=0,
                       next_offset=5, total=len(body), sha=digest)
        if fault == "missing_tail":
            assert not provider.saw_bytes("C", "DOCS.md", digest)
            return
        if fault == "contradictory_overlap":
            await _observe(provider, "C", "DOCS.md", "xxxxz", offset=0,
                           next_offset=5, total=len(body), sha=digest)
        tail = body[6:] if fault == "gap" else body[5:]
        offset = 6 if fault == "gap" else 4 if fault == "overlap_mismatch" else 5
        if fault == "overlap_mismatch":
            tail = "X" + body[5:]
        if fault == "wrong_bytes":
            tail = "X" + tail[1:]
        if fault == "inconsistent_total":
            tail += "!"  # coherent second page, but a different declared file size
        await _observe(provider, "other" if fault == "mixed_attempt" else "C", "DOCS.md",
                       tail, offset=offset,
                       next_offset=len(body) if fault == "false_terminal" else None,
                       total=len(body) + (1 if fault == "inconsistent_total" else 0),
                       sha=_digest("different") if fault == "wrong_sha" else digest)
        assert not provider.saw_bytes("C", "DOCS.md", digest)

    asyncio.run(exercise())


def test_single_complete_legacy_read_still_counts():
    async def exercise():
        provider = _ObservedProvider(_NoNetwork())
        content = "full legacy text\n"
        call_id = CallId("legacy-read")
        provider.read_calls[("C", str(call_id))] = "DOCS.md"
        request = ProviderRequest(RequestId("legacy-page"), (
            Message(MessageRole.TOOL, json.dumps({"outcome": "succeeded", "value": {
                "path": "DOCS.md", "content": content,
            }}), call_id=call_id),
            Message(MessageRole.USER, '## attempt\n{"attempt_id":"C"}'),
        ))
        await provider.invoke(request, cancel=CancelToken())
        assert provider.saw_bytes("C", "DOCS.md", _digest(content))

    asyncio.run(exercise())
