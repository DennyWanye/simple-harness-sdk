# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""P3.2 slice C · P32-5a / P32-5b: no symlink ever carries host bytes out of a workspace,
model-written code runs in a throw-away copy, what is verified is what was recorded, and
Artifact bytes live in a content-addressed store that outlives the workspace (plan v3 D3;
plan review round 1 P0-1 / P1-1, round 2 P1-1 / P1-3 / P2-3).

The oracle is the host secret's bytes: after every copy the system makes (repair seed,
verification copy, integrated copy, execution copy, snapshot, artifact read) the secret
must appear in no file the system produced and in no refusal text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from agent_orchestrator.artifacts.store import (
    ArtifactStore,
    ArtifactStoreError,
    backfill,
    read_verified,
)
from agent_orchestrator.artifacts.workspace import EXEC_COPY_MARK, WorkspaceError, WorkspaceManager
from agent_orchestrator.contracts import Artifact
from agent_orchestrator.runtime.tool_gateway import (
    WORKER_TOOLS,
    WorkspaceBinding,
    WorkspaceToolGateway,
)
from simple_harness.contracts import CallId
from simple_harness.tools import ToolCall

SECRET = b"HOST-SECRET-P32-5a"


@pytest.fixture
def secret(tmp_path):
    path = tmp_path / "host" / "id_rsa"
    path.parent.mkdir()
    path.write_bytes(SECRET)
    return path


@pytest.fixture
def manager(tmp_path):
    return WorkspaceManager(
        tmp_path / "evidence" / "workspaces",
        artifact_store=ArtifactStore(tmp_path / "evidence" / "artifacts"),
    )


def _leaky(manager, secret, attempt="att-1", *, link="notes/leak.txt"):
    workspace = manager.create(attempt, seed={"README.md": "hi"})
    target = workspace.root / link
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(secret, target)  # a tree from before 0.10, or a planted link
    return workspace


def _no_secret_under(root: Path, *, skip=()) -> None:
    """No file the system produced holds the secret or is a symlink; ``skip`` are the
    trees the test itself planted a link in."""

    skipped = [Path(p) for p in skip]
    for dirpath, dirnames, files in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if base / d not in skipped]
        for name in (*files, *dirnames):
            if (base / name).is_symlink():
                raise AssertionError(f"a symlink survived a copy: {base / name}")
        for name in files:
            assert SECRET not in (base / name).read_bytes(), base / name


def _artifact(path: str, data: bytes, uri: str, **extra) -> Artifact:
    return Artifact(
        id=f"artifact-{hashlib.sha256(path.encode()).hexdigest()[:12]}",
        mission_id="m",
        task_id="t",
        attempt_id="att-1",
        type="file",
        path=path,
        version=1,
        content_hash=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        produced_by="att-1",
        storage_uri=uri,
        **extra,
    )


# ------------------------------------------------------------------ P32-5a symlinks
def test_p32_5a_verification_copy_of_a_live_tree_refuses_a_symlink(manager, secret):
    workspace = _leaky(manager, secret)
    with pytest.raises(WorkspaceError) as refused:
        manager.verification_copy("att-1")
    assert "workspace_symlink" in str(refused.value)
    assert "notes/leak.txt" in str(refused.value)
    assert str(secret) not in str(refused.value)  # never tells where the link points
    assert not (manager.root / "att-1-verify").exists()
    _no_secret_under(manager.root, skip=[workspace.root])


def test_p32_5a_repair_seed_refuses_a_symlink(manager, secret):
    first = _leaky(manager, secret)
    with pytest.raises(WorkspaceError) as refused:
        manager.create("att-2", seed={}, previous=first.root)
    assert "workspace_symlink" in str(refused.value)
    assert not (manager.root / "att-2").exists()  # nothing half-made is left to reuse
    _no_secret_under(manager.root, skip=[first.root])


def test_p32_5a_integrated_copy_refuses_a_symlink_source(manager, secret, tmp_path):
    link = tmp_path / "evidence" / "artifacts-link"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(secret, link)
    with pytest.raises(WorkspaceError) as refused:
        manager.integrated_copy("m-1-judge", seed={}, files={"out.txt": link})
    assert "workspace_symlink" in str(refused.value)
    assert str(secret) not in str(refused.value)
    _no_secret_under(manager.root)


def test_p32_5a_snapshot_refuses_a_symlink_instead_of_skipping_it(manager, secret):
    workspace = _leaky(manager, secret)
    with pytest.raises(WorkspaceError) as refused:
        workspace.snapshot(mission_id="m", task_id="t", produced_by="att-1")
    assert "workspace_symlink" in str(refused.value)
    _no_secret_under(manager.artifact_store.root)


def test_p32_5a_symlinked_directory_is_refused_too(manager, secret):
    workspace = manager.create("att-1", seed={"README.md": "hi"})
    os.symlink(secret.parent, workspace.root / "hostdir")
    with pytest.raises(WorkspaceError):
        manager.verification_copy("att-1")
    with pytest.raises(WorkspaceError):
        workspace.snapshot(mission_id="m", task_id="t", produced_by="att-1")
    _no_secret_under(manager.root, skip=[workspace.root])


def _gateway(manager):
    gateway = WorkspaceToolGateway(manager, test_timeout=60)
    gateway.bind("agent-w", WorkspaceBinding("att-1", "work", True, WORKER_TOOLS))
    return gateway


def _call(gateway, name, **arguments):
    return asyncio.run(
        gateway.execute(ToolCall(CallId("c1"), name, arguments), {"run_id": "agent-w"})
    )


def test_p32_5a_model_written_code_runs_in_a_throw_away_copy(manager, secret):
    test_file = (
        "import os\n"
        "def test_plant():\n"
        f"    os.symlink({str(secret)!r}, 'leak.txt')\n"
        "    open('junk.txt', 'w').write('x')\n"
        "    os.makedirs('.home', exist_ok=True)\n"
    )
    workspace = manager.create("att-1", seed={"test_plant.py": test_file})
    run = _call(_gateway(manager), "run_tests")
    assert run.value["passed"] is True, run.value["stdout"]
    assert workspace.list_files() == ["test_plant.py"]  # nothing the code wrote came back
    assert workspace.symlinks() == []
    assert not [p for p in manager.root.iterdir() if EXEC_COPY_MARK in p.name]
    _no_secret_under(manager.root)


def test_p32_5a_an_execution_copy_of_a_tree_with_a_symlink_is_refused(manager, secret):
    workspace = _leaky(manager, secret)
    (workspace.root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    run = _call(_gateway(manager), "run_tests")
    assert run.error_code == "workspace_error"
    assert "workspace_symlink" in str(
        run.value or run.error_message if hasattr(run, "error_message") else run
    )
    assert not [p for p in manager.root.iterdir() if EXEC_COPY_MARK in p.name]
    _no_secret_under(manager.root, skip=[workspace.root])


# ------------------------------------------------------------------ P32-5b content-addressed store
def test_p32_5b_snapshot_puts_bytes_in_the_store(manager):
    workspace = manager.create("att-1", seed={"report.md": "# 报告\n"})
    [artifact] = workspace.snapshot(mission_id="m", task_id="t", produced_by="att-1")
    stored = Path(artifact.storage_uri)
    assert stored.is_file() and not stored.is_relative_to(workspace.root)
    assert stored.name == artifact.content_hash
    assert stored.read_bytes() == "# 报告\n".encode()
    assert not stored.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def test_p32_5b_bytes_outlive_the_workspace(manager):
    workspace = manager.create("att-1", seed={"report.md": "# 报告\n"})
    [artifact] = workspace.snapshot(mission_id="m", task_id="t", produced_by="att-1")
    shutil.rmtree(workspace.root)
    assert read_verified(artifact) == "# 报告\n".encode()
    assert manager.artifact_store.read(artifact.content_hash) == "# 报告\n".encode()


def test_p32_5b_what_is_verified_is_what_was_recorded(manager):
    workspace = manager.create("att-1", seed={"report.md": "种子\n"})
    workspace.write_text("report.md", "登记的内容 X\n")
    artifacts = workspace.snapshot(mission_id="m", task_id="t", produced_by="att-1")
    workspace.write_text("report.md", "登记之后换成的 Y\n")  # a late change to the live tree
    copy = manager.verification_copy(
        "att-1", seed={"report.md": "种子\n"}, inputs={}, artifacts=artifacts, protected={}
    )
    assert copy.read_text("report.md") == "登记的内容 X\n"


def test_p32_5b_store_is_write_once_and_checks_the_hash(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "a.txt"
    source.write_bytes(b"one")
    first = store.put_file(source)
    again = store.put_file(source)
    assert first == again  # same bytes, same address, no second copy
    path = store.path_for(first)
    os.chmod(path, 0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactStoreError) as changed:
        store.read(first)  # a read re-checks the address
    assert changed.value.reason == "hash_mismatch"
    store.put_file(source)  # putting the bytes again repairs the address
    assert store.read(first) == b"one"


def test_p32_5b_store_read_refuses_a_symlink(tmp_path, secret):
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "a.txt"
    source.write_bytes(b"one")
    address = store.put_file(source)
    path = store.path_for(address)
    os.chmod(path, 0o600)
    path.unlink()
    os.symlink(secret, path)
    with pytest.raises(ArtifactStoreError) as refused:
        store.read(address)
    assert refused.value.reason == "symlink"


def test_p32_5b_an_unavailable_artifact_is_refused_not_guessed():
    artifact = _artifact("report.md", b"x", "")
    with pytest.raises(ArtifactStoreError) as refused:
        read_verified(artifact)
    assert refused.value.reason == "unavailable"


def test_p32_5b_backfill_moves_intact_bytes_and_marks_the_rest_unavailable(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    legacy = tmp_path / "workspaces" / "att-1"
    legacy.mkdir(parents=True)
    (legacy / "ok.md").write_bytes(b"intact")
    (legacy / "changed.md").write_bytes(b"after")
    in_store = store.path_for(store.put_bytes(b"stored"))
    artifacts = [
        _artifact("ok.md", b"intact", str(legacy / "ok.md")),
        _artifact("missing.md", b"gone", str(legacy / "missing.md")),
        _artifact("changed.md", b"before", str(legacy / "changed.md")),
        _artifact("stored.md", b"stored", str(in_store)),
    ]
    changes = dict(backfill(artifacts, store))
    ok, missing, changed, stored = artifacts
    assert changes[ok.id] == str(store.path_for(ok.content_hash))
    assert store.read(ok.content_hash) == b"intact"
    assert changes[missing.id] == "" and changes[changed.id] == ""
    assert stored.id not in changes
    updated = [
        dataclasses.replace(a, storage_uri=changes.get(a.id, a.storage_uri)) for a in artifacts
    ]
    assert backfill(updated, store) == []  # re-running changes nothing
