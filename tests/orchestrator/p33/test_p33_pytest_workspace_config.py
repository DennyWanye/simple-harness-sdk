# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""子 pytest 仅使用 workspace 内配置；父目录配置和 conftest 均不可加载。

真实子进程验证配置生效、搜索优先级、空配置回退和 Seatbelt 权限不扩大。
不 mock pytest 配置发现，也不把沙箱执行失败当成隔离成功。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from agent_orchestrator.runtime.sandbox import ProcessOnlyExecutor, SeatbeltExecutor
from agent_orchestrator.runtime.tool_gateway import run_pytest


def _run(workspace, *, path=None, executor=None):
    return asyncio.run(
        asyncio.wait_for(
            run_pytest(str(workspace), path=path, timeout=30, executor=executor), timeout=45
        )
    )


def _workspace(tmp_path):
    parent = tmp_path.resolve() / "parent-project"
    parent.mkdir()
    (parent / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--parent-config-must-not-load"\n',
        encoding="utf-8",
    )
    (parent / "conftest.py").write_text(
        'raise AssertionError("parent conftest must not load")\n', encoding="utf-8"
    )
    workspace = parent / "ws"
    workspace.mkdir()
    (workspace / "conftest.py").write_text(
        'import pytest\n@pytest.fixture\ndef local_value():\n    return "workspace"\n',
        encoding="utf-8",
    )
    return workspace


def _probe(workspace, *, name="test_config.py", selected=None):
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from pathlib import Path\n"
        "def test_config(pytestconfig, local_value):\n"
        '    assert local_value == "workspace"\n'
        "    assert pytestconfig.rootpath == Path.cwd()\n"
        f"    assert str(pytestconfig.inipath) == {str(selected or Path('/dev/null'))!r}\n"
        '    Path("executed.txt").write_text("executed", encoding="utf-8")\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("adapter", ["process_only", "seatbelt"])
def test_parent_config_and_conftest_are_not_loaded(tmp_path, adapter):
    if adapter == "seatbelt" and (
        sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists()
    ):
        pytest.skip("seatbelt needs macOS sandbox-exec")
    workspace = _workspace(tmp_path)
    _probe(workspace)
    executor = (
        SeatbeltExecutor.for_interpreter() if adapter == "seatbelt" else ProcessOnlyExecutor()
    )
    run = _run(workspace, executor=executor)
    assert run.passed, run.stdout
    assert (workspace / "executed.txt").read_text() == "executed"
    assert run.receipt.kind == adapter
    assert run.receipt.isolated is (adapter == "seatbelt")


CONFIGS = {
    "pytest.ini": "[pytest]\npython_files = check_*.py\n",
    ".pytest.ini": "[pytest]\npython_files = check_*.py\n",
    "pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n',
    "tox.ini": "[pytest]\npython_files = check_*.py\n",
    "setup.cfg": "[tool:pytest]\npython_files = check_*.py\n",
}


@pytest.mark.parametrize("filename", CONFIGS)
def test_workspace_configuration_is_effective(tmp_path, filename):
    workspace = _workspace(tmp_path)
    selected = workspace / filename
    selected.write_text(CONFIGS[filename], encoding="utf-8")
    # 非默认文件名：无条件 -c /dev/null 会收集不到，不能误通过。
    _probe(workspace, name="check_config.py", selected=selected)
    run = _run(workspace)
    assert run.passed, run.stdout
    assert (workspace / "executed.txt").exists()


@pytest.mark.parametrize(
    "files,selected",
    [
        (
            {"pytest.ini": "", **{k: v for k, v in CONFIGS.items() if k != "pytest.ini"}},
            "pytest.ini",
        ),
        ({k: v for k, v in CONFIGS.items() if k != "pytest.ini"}, ".pytest.ini"),
        (
            {k: v for k, v in CONFIGS.items() if k not in {"pytest.ini", ".pytest.ini"}},
            "pyproject.toml",
        ),
        (
            {"pyproject.toml": '[project]\nname = "unrelated"\n', "tox.ini": CONFIGS["tox.ini"]},
            "tox.ini",
        ),
        ({"tox.ini": "[tox]\nenvlist = py\n", "setup.cfg": CONFIGS["setup.cfg"]}, "setup.cfg"),
    ],
)
def test_workspace_config_selection_preserves_pytest_priority(tmp_path, files, selected):
    workspace = _workspace(tmp_path)
    for name, content in files.items():
        (workspace / name).write_text(content, encoding="utf-8")
    _probe(workspace, selected=workspace / selected)
    run = _run(workspace, path="test_config.py")
    assert run.passed, run.stdout


def test_nested_target_uses_nearest_config_and_workspace_conftest(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    selected = workspace / "pkg" / "pytest.ini"
    selected.parent.mkdir()
    selected.write_text(CONFIGS["pytest.ini"], encoding="utf-8")
    _probe(workspace, name="pkg/tests/check_config.py", selected=selected)
    run = _run(workspace, path="pkg/tests/check_config.py::test_config")
    assert run.passed, run.stdout
    assert (workspace / "executed.txt").exists()


@pytest.mark.parametrize(
    "name,content", [("pytest.ini", "not an ini section\n"), ("pyproject.toml", "[broken\n")]
)
def test_malformed_workspace_config_is_not_silently_ignored(tmp_path, name, content):
    workspace = _workspace(tmp_path)
    (workspace / name).write_text(content, encoding="utf-8")
    _probe(workspace)
    run = _run(workspace)
    assert not run.passed
    assert name in run.stdout
    assert not (workspace / "executed.txt").exists()


def test_workspace_config_symlink_cannot_select_parent_file(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "pyproject.toml").symlink_to(workspace.parent / "pyproject.toml")
    _probe(workspace)
    run = _run(workspace)
    assert not run.passed
    assert "workspace config symlink" in run.stdout
    assert not (workspace / "executed.txt").exists()
