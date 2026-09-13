"""Crashed SQLite evidence is inspected without recovering the original files."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_process_kill_recovery import _cleanup, _kill, _marker, _rows

CHILD = """
import json, sqlite3, sys, time
from pathlib import Path
path, marker, mode = sys.argv[1:]
connection = sqlite3.connect(path)
connection.execute('PRAGMA journal_mode=' + mode)
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA cache_size=2')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE evidence SET payload=?', ('pending-' * 512,))
if mode == 'WAL':
    connection.commit()
Path(marker).write_text(json.dumps({'mode': mode}))
while True:
    time.sleep(1)
"""


def _identity(path):
    return {
        suffix: hashlib.sha256(Path(str(path) + suffix).read_bytes()).hexdigest()
        for suffix in ("", "-journal", "-wal", "-shm")
        if Path(str(path) + suffix).is_file()
    }


@pytest.mark.parametrize("mode", ["DELETE", "WAL"])
def test_observation_recovers_crashed_copy_and_preserves_original_for_cold_owner(tmp_path, mode):
    path = tmp_path / "evidence.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA page_size=1024")
        connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, payload TEXT)")
        connection.executemany(
            "INSERT INTO evidence VALUES (?, ?)",
            [(number, "original" * 512) for number in range(64)],
        )
    connection.close()
    marker = tmp_path / "boundary.json"
    log = tmp_path / "child.log"
    with log.open("wb") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(path), str(marker), mode],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
        )
    try:
        assert _marker(child, marker, log) == {"mode": mode}
        _kill(child)
    finally:
        _cleanup(child)
    suffix = "-journal" if mode == "DELETE" else "-wal"
    assert Path(str(path) + suffix).stat().st_size > 512
    if mode == "DELETE":
        assert Path(str(path) + suffix).read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
        readonly = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                readonly.execute("SELECT * FROM evidence").fetchall()
        finally:
            readonly.close()
    before = _identity(path)
    expected = "original" * 512 if mode == "DELETE" else "pending-" * 512
    query = "SELECT DISTINCT payload FROM evidence"
    assert _rows(path, query) == [(expected,)]
    assert _identity(path) == before
    assert not list(tmp_path.glob("sqlite-observation-*"))
    # Only now does the real owner recover the original crash state.
    with sqlite3.connect(path) as cold:
        assert cold.execute(query).fetchall() == [(expected,)]
        assert cold.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    cold.close()
