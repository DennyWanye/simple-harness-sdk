"""Real old binaries produce nonempty WAL stores; source-only SQL is forensic."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest
import simple_harness as h
from simple_harness.execution.sqlite import Database
from .test_context_use_migration import OLD_SEED


def rows(path):
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                 if r[0] not in {"sdk_schema_migrations", "short_context_upgrade_receipt"}]
        return {name: sorted((tuple(r) for r in db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr) for name in names}



def old(version, code, *args):
    prefix = f'H0{version.replace(".", "")[-2:]}'  # H073/H074
    python = os.environ[prefix + "_PYTHON"]
    wheel = os.environ[prefix + "_WHEEL"]
    verify = '''
import importlib.metadata,json,pathlib,sys,zipfile,hashlib
import simple_harness as h
owner=importlib.metadata.distribution('simple-harness-sdk')
assert h.__version__==VERSION
assert pathlib.Path(h.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
assert hashlib.sha256(pathlib.Path(WHEEL).read_bytes()).hexdigest()==SHA
with zipfile.ZipFile(WHEEL) as z:
 for name in z.namelist():
  if name.startswith('simple_harness/') and not name.endswith('/'):
   assert owner.locate_file(name).read_bytes()==z.read(name),name
'''
    sha = {'0.7.3': '1a9ed5c95e6cddd4e0ccd85124320a6001008740a53213712fc89f3467cb4cd7',
           '0.7.4': '6caf9defd02bc44bcebb7981f04ce96c86fc1fc805ea314a53313921d3085030'}[version]
    code = f'VERSION={version!r}\nWHEEL={wheel!r}\nSHA={sha!r}\n' + verify + code
    result = subprocess.run([python, '-I', '-c', code, *map(str, args)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def seed(tmp_path, version):
    path = tmp_path / 'old.sqlite'
    old(version, OLD_SEED.replace("'0.7.3'", repr(version)), path)
    assert Path(str(path)+'-wal').stat().st_size > 0
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_old_refusal(path):
    old('0.7.4', '''
from simple_harness.execution.sqlite import Database
try: Database.open(sys.argv[1])
except Exception as e: assert getattr(e,'code',None)=='execution_schema_incompatible'
else: raise AssertionError('H074 accepted schema9')
''', path)


@pytest.mark.parametrize('prior', ['0.7.3', '0.7.4', '7_to_8'])
def test_nonempty_wal_and_upgrade_replay_preserve_all_old_rows(tmp_path, prior):
    path = seed(tmp_path, '0.7.3' if prior == '7_to_8' else prior)
    old_receipt = None
    if prior == '7_to_8':
        old_receipt = h.migrate_execution_v7_to_v8(path, backup_path=tmp_path/'pre8.backup')
        assert old_receipt.to_version == 8
        old('0.7.4', 'from simple_harness.execution.sqlite import Database\nwith Database.open(sys.argv[1]) as db: assert db.schema_version==8', path)
    original = rows(path)
    assert original['workflow_checkpoints'] and original['provider_invocations']
    backup = tmp_path / 'pre9.backup'
    receipt = h.migrate_execution_to_v9(path, backup_path=backup)
    assert receipt.from_version == (7 if prior == '0.7.3' else 8)
    assert receipt.to_version == 9 and receipt.backup_sha256 == sha(backup)
    assert original == {key: rows(path)[key] for key in original} == rows(backup)
    if old_receipt:
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            stored = db.execute('SELECT receipt_json,receipt_hash FROM context_use_upgrade_receipt').fetchone()
        assert json.loads(stored[0]) == old_receipt.to_json() and stored[1] == old_receipt.receipt_hash
    with Database.open(path) as db:
        assert db.schema_version == 9
    assert h.migrate_execution_to_v9(path, backup_path=backup) == receipt
    before = sha(path), sha(backup)
    require_old_refusal(path)
    assert before == (sha(path), sha(backup))
    assert h.migrate_execution_to_v9(path, backup_path=backup) == receipt


def test_fresh9_and_unknown_catalog_and_retained_backup_crash(tmp_path, monkeypatch):
    fresh = tmp_path/'fresh.sqlite'
    with Database.open(fresh): pass
    assert h.migrate_execution_to_v9(fresh, backup_path=tmp_path/'unused.backup') is None
    assert not (tmp_path/'unused.backup').exists()
    path = seed(tmp_path, '0.7.4')
    from simple_harness.execution.sqlite import short_context_migration as impl
    original = impl._statements
    def fail(db, sql):
        original(db, sql)
        raise RuntimeError('before_commit')
    monkeypatch.setattr(impl, '_statements', fail)
    backup = tmp_path/'pre9.backup'
    original_rows = rows(path)
    with pytest.raises(RuntimeError, match='before_commit'):
        h.migrate_execution_to_v9(path, backup_path=backup)
    backup_hash = sha(backup)
    assert rows(path) == original_rows
    monkeypatch.setattr(impl, '_statements', original)
    receipt = h.migrate_execution_to_v9(path, backup_path=backup)
    assert receipt.backup_sha256 == backup_hash == sha(backup)
    # Caller losing the acknowledgement after commit repeats the same public call.
    assert h.migrate_execution_to_v9(path, backup_path=backup) == receipt
    with closing(sqlite3.connect(fresh, isolation_level=None)) as db:
        db.execute('CREATE TABLE unexpected(value TEXT)')
    before = sha(fresh)
    with pytest.raises(Exception, match='unknown_catalog'):
        h.migrate_execution_to_v9(fresh, backup_path=tmp_path/'refused.backup')
    assert sha(fresh)==before and not (tmp_path/'refused.backup').exists()


@pytest.mark.parametrize('location', ['attempt', 'checkpoint'])
def test_legacy_fake_short_revision_refuses_before_backup(tmp_path, location):
    path = seed(tmp_path, '0.7.4')
    # H074 itself serializes its old positive-revision short carrier. The source
    # fixture inserts it in a known durable slot; no real grant is fabricated.
    from tests.unit.execution.test_context_use_contract import attempt
    from tests.unit.execution.test_short_context_revision import fragment
    raw_fragment = fragment().to_json() | {'source_revision': 1}
    old('0.7.4', 'h.ContextFragmentV2.from_json(' + repr(raw_fragment) + ')')
    raw = attempt().to_json() | {'intents': [{'schema_version':1, 'fragments':[raw_fragment],
        'message_bindings':[{'ordinal':1,'message_hash':'c'*64}]}]}
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        if location == 'attempt':
            raw.update(run_id='legacy-run')
            from simple_harness.execution.context_use import use_hash
            db.execute('INSERT INTO provider_context_use_attempts VALUES (?,?,?,?,?,?,?)',
                ('legacy-invocation','legacy-run',1,use_hash('simple-harness/provider-context-use-intent/v1',raw),json.dumps(raw),None,None))
        else:
            row = db.execute("SELECT checkpoint_id,checkpoint_json FROM workflow_checkpoints WHERE namespace='react.termination.v1' ORDER BY version LIMIT 1").fetchone()
            payload = json.loads(row[1]); payload['context_use_attempt'] = raw
            encoded = h.canonical_json(payload)
            db.execute('UPDATE workflow_checkpoints SET checkpoint_json=?,checkpoint_hash=? WHERE checkpoint_id=?',
                (encoded,hashlib.sha256(encoded.encode()).hexdigest(),row[0]))
    before = sha(path)
    with pytest.raises(Exception, match='carrier_incompatible'):
        h.migrate_execution_to_v9(path, backup_path=tmp_path/'refused.backup')
    assert sha(path)==before and not (tmp_path/'refused.backup').exists()


def test_real_h074_long_grants_and_wire_bytes_preserved(tmp_path):
    old('0.7.4', """
sys.path.insert(0, '/Users/denny/projects/simple-harness-sdk-recall-use-reservation')
from tests.integration.runtime.test_context_use_public_memory import test_actual_two_item_public_consumer_and_reopen
test_actual_two_item_public_consumer_and_reopen(pathlib.Path(sys.argv[1]), 'receipt_first')
""", tmp_path)
    path = tmp_path/'execution.sqlite'
    original = rows(path)
    assert original['provider_context_use_attempts'] and original['provider_context_use_receipt_bindings']
    receipt = h.migrate_execution_to_v9(path, backup_path=tmp_path/'long.pre9.backup')
    assert original == rows(path) == rows(tmp_path/'long.pre9.backup')
    assert h.migrate_execution_to_v9(path, backup_path=tmp_path/'long.pre9.backup') == receipt
    from tests.unit.execution.test_short_context_revision import fragment
    value = fragment(h.ContextFragmentType.RECALLED_MEMORY, 1)
    raw = value.to_json()
    output = old('0.7.4', 'f=h.ContextFragmentV2.from_json(' + repr(raw) + ')\nprint(h.canonical_json(f.to_json()))\nprint(f.fragment_hash)')
    assert output.splitlines() == [h.canonical_json(raw), value.fragment_hash]
