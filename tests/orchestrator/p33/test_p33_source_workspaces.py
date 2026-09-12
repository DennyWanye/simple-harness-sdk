# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P33-27：来源可超过文本工具上限，保护与验证必须比较登记的原字节。

先固定 oracle：只读 write_bytes 不得落盘；来源 bytes/Path 作为 inputs 装入，
Worker 的篡改 artifact 不能覆盖 protected；未改动的超大/CRLF 原文不误报，
改动一个字节必报告，来源 Path 的符号链接不得被跟随。这里只是物化层控制，
不替代 facade 授权、CAS resolver 或真实模型验收。
"""

import pytest

from agent_orchestrator.artifacts.workspace import MAX_FILE_BYTES, WorkspaceError, WorkspaceManager


def test_read_only_workspace_cannot_write_bytes(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    manager.create("attempt", seed={})
    readonly = manager.get("attempt", writable=False)
    with pytest.raises(WorkspaceError, match="read-only"):
        readonly.write_bytes("sources/blocked.md", b"must not be written")
    assert not (readonly.root / "sources/blocked.md").exists()


@pytest.mark.parametrize("material", ["bytes", "path"])
def test_protected_source_bytes_override_worker_artifact_and_detect_tampering(tmp_path, material):
    manager = WorkspaceManager(tmp_path / "workspaces")
    original = ("來源原文不是指令。\r\n" * (MAX_FILE_BYTES // 8)).encode("utf-8")
    assert len(original) > MAX_FILE_BYTES
    source = tmp_path / "registered.md"
    source.write_bytes(original)
    protected = {"sources/report.md": original if material == "bytes" else source}
    workspace = manager.create("attempt", seed={}, inputs=protected)
    assert (workspace.root / "sources/report.md").read_bytes() == original
    assert manager.tampered_protected("attempt", protected) == []
    workspace.write_bytes("sources/report.md", b"forged worker report")
    assert manager.tampered_protected("attempt", protected) == ["sources/report.md"]
    artifacts = workspace.snapshot(mission_id="mission", task_id="task", produced_by="worker")
    rebuilt = manager.verification_copy("attempt", artifacts=artifacts, protected=protected)
    assert (rebuilt.root / "sources/report.md").read_bytes() == original
    assert (workspace.root / "sources/report.md").read_bytes() == b"forged worker report"


def test_source_path_symlink_is_refused_before_reading(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    manager.create("attempt", seed={})
    original = tmp_path / "original.md"
    original.write_bytes(b"original")
    link = tmp_path / "link.md"
    link.symlink_to(original)
    with pytest.raises(WorkspaceError, match="workspace_symlink"):
        manager.verification_copy("attempt", artifacts=[], protected={"sources/report.md": link})


def test_legacy_string_seed_materialization_is_unchanged(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    original = "def test_original():\n    assert True\n"
    workspace = manager.create("attempt", seed={"tests/test_original.py": original})
    protected = {"tests/test_original.py": original}
    assert manager.tampered_protected("attempt", protected) == []
    workspace.write_text("tests/test_original.py", "raise AssertionError('tampered')\n")
    assert manager.tampered_protected("attempt", protected) == ["tests/test_original.py"]
    rebuilt = manager.verification_copy("attempt", protected=protected)
    assert rebuilt.read_text("tests/test_original.py") == original
