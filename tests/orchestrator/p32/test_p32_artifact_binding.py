# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice D · P32-7 (binding half): a candidate names which Artifact to publish, the
system binds its identity (plan v3 D6; plan review round 2 P2-5).

The model writes ``artifact_path`` — one file of its own Result.  It never writes the hash,
the id, the size or where the bytes are stored: the system fills those in from the
Result's accepted Artifacts before the params are hashed, so the approval a person grants
is bound to those exact bytes and to nothing else.
"""

from __future__ import annotations

import hashlib

import pytest

from agent_orchestrator.contracts import Artifact
from agent_orchestrator.orchestrator.action_commits import (
    CandidateRejected,
    bind_artifact_params,
)

REPORT = "# 周报\n".encode()


def _artifact(path: str, data: bytes = REPORT) -> Artifact:
    content_hash = hashlib.sha256(data).hexdigest()
    return Artifact(
        id=f"artifact-{content_hash[:12]}",
        mission_id="m",
        task_id="t",
        attempt_id="att-1",
        type="file",
        path=path,
        version=1,
        content_hash=content_hash,
        size_bytes=len(data),
        produced_by="att-1",
        storage_uri=f"/evidence/artifacts/sha256/{content_hash}",
    )


def _accepted(*paths: str) -> dict[str, Artifact]:
    return {path: _artifact(path) for path in paths}


def test_the_system_binds_the_identity_of_the_named_artifact():
    artifact = _artifact("report.md")
    bound = bind_artifact_params({"artifact_path": "report.md"}, {"report.md": artifact})
    assert bound == {
        "artifact_path": "report.md",
        "artifact_id": artifact.id,
        "content_hash": artifact.content_hash,
        "size": artifact.size_bytes,
        "storage_uri": artifact.storage_uri,
    }


def test_params_without_an_artifact_path_are_left_alone():
    params = {"value": "on"}
    assert bind_artifact_params(params, _accepted("report.md")) == params


def test_a_path_outside_this_result_is_refused():
    with pytest.raises(CandidateRejected) as refused:
        bind_artifact_params({"artifact_path": "../secrets.md"}, _accepted("report.md"))
    assert refused.value.reason == "artifact_not_in_result"


def test_an_artifact_of_another_result_is_refused():
    with pytest.raises(CandidateRejected) as refused:
        bind_artifact_params({"artifact_path": "other.md"}, _accepted("report.md"))
    assert refused.value.reason == "artifact_not_in_result"


@pytest.mark.parametrize("field", ["artifact_id", "content_hash", "size", "storage_uri"])
def test_a_candidate_may_not_state_the_identity_itself(field):
    params = {"artifact_path": "report.md", field: "whatever the model wants"}
    with pytest.raises(CandidateRejected) as refused:
        bind_artifact_params(params, _accepted("report.md"))
    assert refused.value.reason == "invalid_candidate"
    assert field in str(refused.value)


@pytest.mark.parametrize("path", ["", "   ", 7, None.__class__])
def test_an_unusable_artifact_path_is_refused(path):
    if path is None.__class__:  # a non-string value
        path = {"nested": "object"}
    with pytest.raises(CandidateRejected):
        bind_artifact_params({"artifact_path": path}, _accepted("report.md"))
