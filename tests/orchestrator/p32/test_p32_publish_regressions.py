# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 · code review round 1 regressions (P1-1, P1-2, P1-3).

Three defects the first implementation had, each with the case that would have caught it:

* **P1-1** the published file name was cut out of ``action-<hex>``, which dropped the
  ``#comp-<n>`` a compensation carries — so a compensation produced *the same file name* as
  the action it answers and could never be published (D8's whole point).
* **P1-2** ``execute`` treated "the ledger's last line for this key is not ABORTED" as "it
  was published", and returned ``applied=True`` without ever looking at the file.  A crash
  between the intent and the link would then be reported as a success with nothing on disk.
* **P1-3** the receipt of an unisolated run still claimed ``network = none`` as a hard limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from agent_orchestrator.runtime.connectors import ConnectorRejected, ConnectorTransportError
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector
from agent_orchestrator.runtime.sandbox import ProcessOnlyExecutor, SandboxSpec

REPORT = "# 周报 v1\n".encode()
REVISED = "# 周报 v2（更正后）\n".encode()
ORIGINAL_KEY = "action-2f8a1c4d9e0b7766:v1"
COMPENSATION_KEY = "action-2f8a1c4d9e0b7766#comp-1:v1"


@pytest.fixture
def connector(tmp_path):
    published = tmp_path / "published"
    published.mkdir()
    return FilePublishConnector(published, tmp_path / "evidence" / "connectors" / "file_publish")


def _stored(tmp_path, data: bytes) -> Path:
    root = tmp_path / "evidence" / "artifacts" / "sha256"
    root.mkdir(parents=True, exist_ok=True)
    path = root / hashlib.sha256(data).hexdigest()
    path.write_bytes(data)
    return path


def _params(stored: Path, data: bytes) -> dict:
    return {
        "artifact_path": "report.md",
        "artifact_id": f"artifact-{hashlib.sha256(data).hexdigest()[:8]}",
        "content_hash": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "storage_uri": str(stored),
    }


# ------------------------------------------------------------------ P1-1
def test_a_compensation_publishes_its_own_file_next_to_the_original(connector, tmp_path):
    first = connector.execute(
        "publish",
        "weekly/report.md",
        _params(_stored(tmp_path, REPORT), REPORT),
        idempotency_key=ORIGINAL_KEY,
    )
    # the compensation answers the same target with new content: a new file, not a conflict
    second = connector.execute(
        "publish",
        "weekly/report.md",
        _params(_stored(tmp_path, REVISED), REVISED),
        idempotency_key=COMPENSATION_KEY,
    )
    original, compensation = Path(first.after["path"]), Path(second.after["path"])
    assert original != compensation, "a compensation may not reuse the original's file name"
    assert original.read_bytes() == REPORT and compensation.read_bytes() == REVISED
    assert sorted(p.name for p in (connector.root / "weekly").iterdir()) == sorted(
        [original.name, compensation.name]
    )


def test_every_key_gets_its_own_name_including_the_versions(connector, tmp_path):
    stored = _stored(tmp_path, REPORT)
    names = set()
    for key in (
        ORIGINAL_KEY,
        "action-2f8a1c4d9e0b7766:v2",
        COMPENSATION_KEY,
        "action-2f8a1c4d9e0b7766#comp-2:v1",
    ):
        receipt = connector.execute(
            "publish", f"out/{key[-4:]}.md", _params(stored, REPORT), idempotency_key=key
        )
        names.add(Path(receipt.after["path"]).name.split(".", 1)[1])  # the digest and version
    assert len(names) == 4


# ------------------------------------------------------------------ P1-2
def _plant_intent(connector, tmp_path, *, key: str = ORIGINAL_KEY) -> dict:
    """What a SIGKILL between the intent and the link leaves behind."""

    entry = {
        "key": key,
        "state": "PREPARED",
        "target": "weekly/report.md",
        "final_path": "weekly/report.deadbeef1234.v1.md",
        "content_hash": hashlib.sha256(REPORT).hexdigest(),
        "bytes": len(REPORT),
        "params_hash": "irrelevant",
        "applied_at": 1.0,
    }
    path = connector.ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def test_an_intent_without_a_file_is_never_reported_as_published(connector, tmp_path):
    _plant_intent(connector, tmp_path)
    with pytest.raises(ConnectorTransportError):  # unknown, and a person decides
        connector.execute(
            "publish",
            "weekly/report.md",
            _params(_stored(tmp_path, REPORT), REPORT),
            idempotency_key=ORIGINAL_KEY,
        )
    with pytest.raises(ConnectorTransportError):
        connector.lookup(ORIGINAL_KEY)
    assert not any(connector.root.rglob("*.md"))


def test_a_published_file_that_disappeared_is_not_reported_as_published_again(connector, tmp_path):
    receipt = connector.execute(
        "publish",
        "weekly/report.md",
        _params(_stored(tmp_path, REPORT), REPORT),
        idempotency_key=ORIGINAL_KEY,
    )
    Path(receipt.after["path"]).unlink()  # the directory belongs to the user
    with pytest.raises(ConnectorTransportError):
        connector.execute(
            "publish",
            "weekly/report.md",
            _params(_stored(tmp_path, REPORT), REPORT),
            idempotency_key=ORIGINAL_KEY,
        )


def test_a_name_this_key_did_not_create_is_a_conflict(connector, tmp_path):
    stored = _stored(tmp_path, REPORT)
    receipt = connector.execute(
        "publish", "weekly/report.md", _params(stored, REPORT), idempotency_key=ORIGINAL_KEY
    )
    published = Path(receipt.after["path"])
    # someone removes the ledger (a restored backup, a wiped evidence dir) but keeps the file
    connector.ledger_path.unlink()
    with pytest.raises(ConnectorRejected) as refused:
        connector.execute(
            "publish", "weekly/report.md", _params(stored, REPORT), idempotency_key=ORIGINAL_KEY
        )
    assert "conflict" in str(refused.value)
    assert published.read_bytes() == REPORT  # and the file is left exactly as it was


# ------------------------------------------------------------------ P1-3
def test_an_unisolated_receipt_never_claims_a_network_limit(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    receipt = asyncio.run(
        ProcessOnlyExecutor().execute(
            ["python3", str(tmp_path / "hello.py")],
            cwd=str(tmp_path),
            spec=SandboxSpec(cpu_seconds=20, wall_seconds=20),
        )
    )
    assert receipt.isolated is False
    network = receipt.effective_limits["network"]
    assert network == {"value": "unrestricted", "enforcement": "none"}
    # the rlimits really are applied in both adapters, and CPU is per process
    assert receipt.effective_limits["cpu_seconds"]["enforcement"] == "hard"
    assert receipt.effective_limits["cpu_seconds"]["scope"] == "process"


def test_an_isolated_spec_still_says_the_network_is_denied():
    limits = SandboxSpec().effective_limits(isolated=True)
    assert limits["network"] == {"value": "none", "enforcement": "hard"}
