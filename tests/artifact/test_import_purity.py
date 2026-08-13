# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_import_has_no_host_or_runtime_side_effects(tmp_path: Path) -> None:
    audit_program = textwrap.dedent(
        """
        import asyncio
        import collections.abc
        import json
        import os
        import pathlib
        import socket
        import sys
        import threading

        source_root = pathlib.Path(sys.argv[1]).resolve()
        work_root = pathlib.Path(sys.argv[2]).resolve()
        sys.path.insert(0, str(source_root))
        sys.dont_write_bytecode = True

        class GuardedEnvironment(collections.abc.MutableMapping):
            def __init__(self, original):
                self._original = dict(original)
            def __getitem__(self, key):
                raise AssertionError(f"environment read during import: {key}")
            def __setitem__(self, key, value):
                raise AssertionError(f"environment write during import: {key}")
            def __delitem__(self, key):
                raise AssertionError(f"environment delete during import: {key}")
            def __iter__(self):
                raise AssertionError("environment scan during import")
            def __len__(self):
                raise AssertionError("environment scan during import")

        original_environment = os.environ
        original_socket = socket.socket
        original_create_connection = socket.create_connection
        before_threads = {thread.ident for thread in threading.enumerate()}
        before_files = sorted(str(path.relative_to(work_root)) for path in work_root.rglob("*"))

        def blocked_network(*args, **kwargs):
            raise AssertionError("network access during import")

        socket.socket = blocked_network
        socket.create_connection = blocked_network
        os.environ = GuardedEnvironment(original_environment)
        try:
            import simple_harness
        finally:
            os.environ = original_environment
            socket.socket = original_socket
            socket.create_connection = original_create_connection

        after_threads = {thread.ident for thread in threading.enumerate()}
        after_files = sorted(str(path.relative_to(work_root)) for path in work_root.rglob("*"))
        forbidden = {
            "fastapi", "torch", "whisper", "playwright", "deskpet", "tauri"
        }.intersection(name.split(".", 1)[0] for name in sys.modules)
        assert before_threads == after_threads
        assert before_files == after_files
        assert not forbidden, forbidden
        assert tuple(simple_harness.__all__)
        assert simple_harness.__all__[0] == "__version__"
        assert len(simple_harness.__all__) == len(set(simple_harness.__all__))
        print(json.dumps({"version": simple_harness.__version__, "status": "IMPORT_PURITY_PASS"}))
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            audit_program,
            str(REPOSITORY_ROOT / "src"),
            str(tmp_path),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result == {"status": "IMPORT_PURITY_PASS", "version": "0.1.0"}


def test_public_import_does_not_eagerly_load_forbidden_dependencies() -> None:
    forbidden = {"fastapi", "torch", "whisper", "playwright", "deskpet", "tauri"}
    command = (
        "import json,sys; import simple_harness; "
        "print(json.dumps(sorted(set(m.split('.')[0] for m in sys.modules) & "
        + repr(forbidden)
        + ")))"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []
