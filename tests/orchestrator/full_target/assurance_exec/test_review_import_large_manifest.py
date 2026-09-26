# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""2026-09-26 Host run (seven-step book-club Mission): the Critic read evidence with
its tools, so its own provider input manifest grew to 280 KB.  The reader already
admits input manifests up to the record limit, but the review import decoded the
same body again under the 256 KB JSON limit: every import raised JSON_BYTES_LIMIT,
the recheck budget ran out and the step went to manual resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_orchestrator.assurance.codec import MAX_BYTES, MAX_RECORD_BYTES, AssuranceError, canonical
from agent_orchestrator.orchestrator.assurance_review_import import raw_package


def test_a_manifest_past_the_json_limit_is_still_read() -> None:
    body = {"messages": [{"kind": "tool_result", "message": {"content": "x" * 9000}}] * 40}
    raw = canonical(body, limit=MAX_RECORD_BYTES)
    assert len(raw.encode("utf-8")) > MAX_BYTES
    imported = SimpleNamespace(provider_manifest=SimpleNamespace(body_json=raw), binding=None)
    with pytest.raises(AssuranceError) as caught:
        raw_package(imported)
    # It is read in full; what is missing is the package, not room for the manifest.
    assert "REVIEW_PACKAGE_SOURCE_UNAVAILABLE" in str(caught.value)
