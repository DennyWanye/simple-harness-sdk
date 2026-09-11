# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice D · P32-7 / P32-9 / P32-11 (connector half): the file publish connector
(plan v3 D6; plan review round 2 P1-4 / P2-5).

Publishing is one atomic point — ``os.link`` into the user's directory, which fails if the
name is taken — and the connector writes its *intent* before it, into a ledger of its own
outside that directory.  So after any crash the answer is decidable: no intent means the
link never happened; an intent plus a file with the recorded hash means it did; an intent
whose file is gone or changed is not "not started" — it is unknown, and a person decides.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath

import pytest

from agent_orchestrator.runtime.connectors import ConnectorRejected, ConnectorTransportError
from agent_orchestrator.runtime.connectors_publish import FilePublishConnector

REPORT = "# 周报\n- 一\n- 二\n".encode()
KEY = "action-0123456789abcdef:v1"
OTHER_KEY = "action-fedcba9876543210:v2"


@pytest.fixture
def store_root(tmp_path):
    root = tmp_path / "evidence" / "artifacts" / "sha256"
    root.mkdir(parents=True)
    stored = root / hashlib.sha256(REPORT).hexdigest()
    stored.write_bytes(REPORT)
    stored.chmod(0o444)
    return stored


@pytest.fixture
def connector(tmp_path):
    published = tmp_path / "published"
    published.mkdir()
    return FilePublishConnector(published, tmp_path / "evidence" / "connectors" / "file_publish")


def _params(stored: Path, path: str = "report.md") -> dict:
    return {
        "artifact_path": path,
        "artifact_id": "artifact-1",
        "content_hash": hashlib.sha256(REPORT).hexdigest(),
        "size": len(REPORT),
        "storage_uri": str(stored),
    }


def _publish(connector, stored, *, target="weekly/report.md", key=KEY):
    return connector.execute("publish", target, _params(stored), idempotency_key=key)


def _ledger(connector) -> list[dict]:
    path = connector.ledger_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ------------------------------------------------------------------ P32-7 publish
def test_p32_7_publish_writes_the_recorded_bytes_and_reads_them_back(connector, store_root):
    receipt = _publish(connector, store_root)
    published = Path(receipt.after["path"])
    assert published.is_file() and published.read_bytes() == REPORT
    assert published.parent == connector.root / "weekly"
    # a name of this action version's own, never overwriting anything: <stem>.<digest>.v1<suffix>
    # (the digest covers the whole idempotency key — see test_p32_publish_regressions.py)
    stem, digest, version, suffix = published.name.split(".")
    assert (stem, version, suffix) == ("report", "v1", "md")
    assert len(digest) == 12 and set(digest) <= set("0123456789abcdef")
    assert receipt.after["content_hash"] == hashlib.sha256(REPORT).hexdigest()
    assert receipt.target == "weekly/report.md" and receipt.applied is True
    assert receipt.service_ref == f"weekly/{published.name}"
    states = [entry["state"] for entry in _ledger(connector)]
    assert states == ["PREPARED", "COMMITTED"]


def test_p32_7_the_operation_is_l2_state_and_authoritative(connector):
    spec = connector.operations["publish"]
    assert spec.level == "L2" and spec.mutates is True and spec.kind == "state"
    assert connector.lookup_authority == "authoritative"
    assert connector.supports_idempotency and connector.supports_reconciliation


def test_p32_7_the_same_key_never_publishes_twice(connector, store_root):
    first = _publish(connector, store_root)
    again = _publish(connector, store_root)
    assert again.receipt_hash == first.receipt_hash
    assert len(list((connector.root / "weekly").iterdir())) == 1


def test_p32_7_hardlink_support_is_probed_before_a_directory_is_authorised(connector, tmp_path):
    assert FilePublishConnector.supports_hardlinks(connector.root) is True
    missing = tmp_path / "nope"
    assert FilePublishConnector.supports_hardlinks(missing) is False


# ------------------------------------------------------------------ P32-9 crash points
@pytest.mark.parametrize("point", ["intent", "link", "commit"])
def test_p32_9_every_crash_point_is_decidable(connector, store_root, point):
    connector.fail_after = point
    with pytest.raises(ConnectorTransportError):
        _publish(connector, store_root)
    found = connector.lookup(KEY)
    files = (
        list((connector.root / "weekly").iterdir()) if (connector.root / "weekly").is_dir() else []
    )
    if point == "intent":  # the link never happened: it is safe to try again
        assert found is None and files == []
        connector.fail_after = None
        receipt = _publish(connector, store_root)
        assert len(list((connector.root / "weekly").iterdir())) == 1
        assert Path(receipt.after["path"]).read_bytes() == REPORT
    else:  # the link happened; the answer is the same whether the ledger got its last line
        assert found is not None and found.idempotency_key == KEY
        assert len(files) == 1 and files[0].read_bytes() == REPORT
        connector.fail_after = None
        repeat = _publish(connector, store_root)
        assert repeat.receipt_hash == found.receipt_hash
        assert len(list((connector.root / "weekly").iterdir())) == 1


def test_p32_9_a_published_file_the_user_deleted_is_unknown_not_not_started(connector, store_root):
    receipt = _publish(connector, store_root)
    Path(receipt.after["path"]).unlink()  # the directory belongs to the user
    with pytest.raises(ConnectorTransportError):
        connector.lookup(KEY)  # reconciliation keeps it UNKNOWN: never published again


def test_p32_9_a_changed_published_file_is_unknown_too(connector, store_root):
    receipt = _publish(connector, store_root)
    Path(receipt.after["path"]).write_bytes(b"the user edited it")
    with pytest.raises(ConnectorTransportError):
        connector.lookup(KEY)


def test_p32_9_a_half_written_ledger_line_is_ignored(connector, store_root):
    # the half line must be what decides this, so publish first: the key's last *whole*
    # line is COMMITTED, and a torn line after it may not take that away
    published = _publish(connector, store_root)
    with connector.ledger_path.open("a", encoding="utf-8") as handle:
        handle.write('{"key": "action-0123456789abcdef:v1", "state": "PREP')
    found = connector.lookup(KEY)
    assert found is not None and found.receipt_hash == published.receipt_hash


def test_p32_9_a_torn_intent_line_reads_as_never_started(connector):
    # a line that was not written whole is no intent at all: the link never happened
    connector.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with connector.ledger_path.open("a", encoding="utf-8") as handle:
        handle.write('{"key": "action-0123456789abcdef:v1", "state": "PREP')
    assert connector.lookup(KEY) is None


def test_p32_9_an_unknown_key_never_started(connector, store_root):
    _publish(connector, store_root)
    assert connector.lookup(OTHER_KEY) is None


# ------------------------------------------------------------------ P32-11 refusals
@pytest.mark.parametrize(
    "target",
    ["../escape.md", "/etc/passwd", "weekly/../../escape.md", "", "  "],
    ids=["parent", "absolute", "traversal", "empty", "blank"],
)
def test_p32_11_a_target_outside_the_authorised_directory_is_refused(connector, store_root, target):
    with pytest.raises((ConnectorRejected, ValueError)):
        _publish(connector, store_root, target=target)
    assert not any(connector.root.rglob("*.md"))


def test_p32_11_a_symlinked_directory_on_the_way_is_refused(connector, store_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, connector.root / "linked")
    with pytest.raises(ConnectorRejected) as refused:
        _publish(connector, store_root, target="linked/report.md")
    assert "symlink" in str(refused.value)
    assert not any(outside.iterdir())


def test_p32_11_bytes_that_do_not_match_the_bound_hash_are_refused(connector, store_root):
    params = _params(store_root)
    params["content_hash"] = "0" * 64
    with pytest.raises(ConnectorRejected):
        connector.execute("publish", "weekly/report.md", params, idempotency_key=KEY)
    assert not any(connector.root.rglob("*.md"))


def test_p32_11_a_missing_artifact_file_is_refused(connector, tmp_path):
    params = _params(tmp_path / "gone" / "missing")
    with pytest.raises(ConnectorRejected):
        connector.execute("publish", "weekly/report.md", params, idempotency_key=KEY)


def test_p32_11_a_taken_name_with_other_content_is_a_conflict(connector, store_root):
    from agent_orchestrator.runtime.connectors_publish import _name_for

    taken = _name_for(KEY, PurePosixPath("weekly/report.md"))
    (connector.root / "weekly").mkdir()
    (connector.root / "weekly" / taken).write_bytes(b"someone else's file")
    with pytest.raises(ConnectorRejected) as refused:
        _publish(connector, store_root)
    assert "conflict" in str(refused.value)
