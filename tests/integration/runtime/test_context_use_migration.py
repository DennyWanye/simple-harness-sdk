"""Official migration against a nonempty DB produced by the frozen H073 binary.

H073_PYTHON names an existing isolated interpreter, never a new environment.
The old child exits after durable completion without checkpoint/close to preserve
WAL-only commits. No application/native userdata is read or changed.
"""

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from simple_harness import migrate_execution_v7_to_v8
from simple_harness.execution.sqlite import Database

VERIFY_OLD = r"""
import hashlib, importlib.metadata, json, os, pathlib, sys, zipfile
import simple_harness
assert pathlib.Path(simple_harness.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
wheel=pathlib.Path(os.environ['H073_WHEEL'])
expected='1a9ed5c95e6cddd4e0ccd85124320a6001008740a53213712fc89f3467cb4cd7'
assert hashlib.sha256(wheel.read_bytes()).hexdigest()==expected
owner=importlib.metadata.distribution('simple-harness-sdk')
origin=json.loads(owner.read_text('direct_url.json'))
from urllib.parse import urlparse,unquote
parsed=urlparse(origin['url'])
assert parsed.scheme=='file' and pathlib.Path(unquote(parsed.path)).resolve()==wheel.resolve()
# This frozen uv install has an empty archive_info; compare the actual wheel and all installed bytes.
if origin.get('archive_info',{}).get('hashes'):
    assert origin['archive_info']['hashes']['sha256']==expected
with zipfile.ZipFile(wheel) as archive:
    for member in archive.namelist():
        if member.startswith('simple_harness/') and not member.endswith('/'):
            assert owner.locate_file(member).read_bytes()==archive.read(member),member
"""

OLD_SEED = r"""
import asyncio, os, sys
import simple_harness as h
from simple_harness.providers import ProviderResponse,ProviderUsage
assert h.__version__ == '0.7.3'
class Provider:
    async def invoke(self, request, *, cancel):
        return ProviderResponse(request.request_id,h.Message(h.MessageRole.ASSISTANT,'old completed'),
            model='consumer-model',usage=ProviderUsage(10,2,12),finish_reason='stop')
class Tools:
    async def execute(self,*args,**kwargs): raise AssertionError('no tools')
class Auth:
    async def request_authorization(self,*args,**kwargs): raise AssertionError('no auth')
async def main():
    runtime=await h.build_consumer_runtime(h.ConsumerRuntimePorts(provider=Provider(),tool_executor=Tools(),
        authorization=Auth(),database_path=sys.argv[1]))
    await runtime.__aenter__()
    # Test-owned source setup to retain WAL-only commits on abrupt process loss.
    # There is no live old Runtime after os._exit.
    runtime._uow.database.connection.execute('PRAGMA journal_mode=WAL')
    runtime._uow.database.connection.execute('PRAGMA wal_autocheckpoint=0')
    client=h.RunClient(runtime)
    value=h.RunStart(h.ExecutionSessionId('legacy-session'),h.RunId('legacy-run'),h.RequestId('legacy-request'),
        turn_id='legacy-turn',tool_catalog_generation=1,
        input={'messages':[{'role':'user','content':'legacy fixture'}],
               'capability_snapshot':{'tools':[]},'max_output_tokens':128})
    await client.start(value)
    await runtime.wait_idle(value.run_id)
    assert client.query(value.run_id).state.value=='completed'
asyncio.run(main())
os._exit(0)
"""


def old_python():
    path = os.environ.get("H073_PYTHON")
    assert path and Path(path).is_file(), (
        "H073_PYTHON must name the existing exact H073 interpreter"
    )
    return path


def run_old(source, *args):
    return subprocess.run(
        [old_python(), "-I", "-c", VERIFY_OLD + source, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def legacy(tmp_path):
    path = tmp_path / "old.sqlite"
    result = run_old(OLD_SEED, path)
    assert result.returncode == 0, result.stderr
    assert Path(str(path) + "-wal").stat().st_size > 0
    return path


def hash_bytes(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return {
            table: sorted(tuple(r) for r in db.execute("SELECT * FROM " + table))
            for table in (
                "runs",
                "run_start_snapshots",
                "provider_invocations",
                "workflow_checkpoints",
                "sdk_stage_audit_events",
            )
        }
    finally:
        db.close()


def test_nonempty_wal_migrate_reopen_exact_backup_and_old_binary_reject(tmp_path):
    path = legacy(tmp_path)
    original = rows(path)
    assert original["provider_invocations"] and original["workflow_checkpoints"]
    backup = tmp_path / "old.pre8.backup"
    receipt = migrate_execution_v7_to_v8(path, backup_path=backup)
    assert receipt is not None
    assert receipt.from_version == 7 and receipt.to_version == 8
    assert receipt.backup_sha256 == hash_bytes(backup)
    assert rows(backup) == original == rows(path)
    before = hash_bytes(backup)
    assert migrate_execution_v7_to_v8(path, backup_path=backup) == receipt
    with Database.open(path) as current:
        assert current.schema_version == 8
    assert rows(path) == original and hash_bytes(backup) == before
    old = run_old(
        """import sys
from simple_harness.execution.sqlite import Database
try: Database.open(sys.argv[1])
except Exception as error:
    assert getattr(error,'code',None)=='execution_schema_incompatible'
else: raise AssertionError('old binary accepted execution8')
""",
        path,
    )
    assert old.returncode == 0, old.stderr
    assert rows(path) == original


def test_fresh_current_noop_and_unknown_catalog_readonly_refusal(tmp_path):
    fresh = tmp_path / "fresh.sqlite"
    with Database.open(fresh):
        pass
    assert migrate_execution_v7_to_v8(fresh, backup_path=tmp_path / "unused.backup") is None
    assert not (tmp_path / "unused.backup").exists()
    path = legacy(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unexpected_extra (value TEXT)")
    prior = hash_bytes(path)
    with pytest.raises(Exception, match="unknown_catalog"):
        migrate_execution_v7_to_v8(path, backup_path=tmp_path / "refused.backup")
    assert hash_bytes(path) == prior and not (tmp_path / "refused.backup").exists()


def test_retained_backup_after_rollback_is_not_overwritten(tmp_path, monkeypatch):
    path = legacy(tmp_path)
    backup = tmp_path / "retained.backup"
    from simple_harness.execution.sqlite import context_use_migration as implementation

    original = implementation._statements

    def fail_after_ddl(db, sql):
        original(db, sql)
        raise RuntimeError("source-test-crash-before-commit")

    monkeypatch.setattr(implementation, "_statements", fail_after_ddl)
    with pytest.raises(RuntimeError, match="source-test-crash"):
        migrate_execution_v7_to_v8(path, backup_path=backup)
    saved = hash_bytes(backup)
    monkeypatch.setattr(implementation, "_statements", original)
    receipt = migrate_execution_v7_to_v8(path, backup_path=backup)
    assert receipt.backup_sha256 == saved == hash_bytes(backup)
