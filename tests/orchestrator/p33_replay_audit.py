"""Opt-in, tests-only O4 replay inventory; no negative-test exemptions.

Load with PYTHONPATH=tests/orchestrator and -p p33_replay_audit, then pass
--p33-replay-audit=.local-test-evidence/<run>/replay-audit.json. The current pytest
basetemp is discovered at session end, and each test's tmp_path/tmpdir at teardown.
Use --p33-replay-audit-root for a specific additional current-run evidence root.
Default observation does not change existing test outcomes. Add
--p33-replay-audit-strict to fail an otherwise green run when the audit is OPEN.
Raw unknowns and malformed fixtures are always retained. A passing pytest run in
observation mode is NOT an AC22/36 PASS. No production files or data are changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import weakref
from collections import Counter
from contextlib import nullcontext
from functools import cache
from pathlib import Path

import pytest

from agent_orchestrator.governance.promotion import DEPLOYMENT_TIMELINE, registry_consistency
from agent_orchestrator.observability.replay import (
    Projection,
    compare,
    events_from_store,
    formal_from_snapshot,
)
from agent_orchestrator.storage.store import Store

# Exact event kinds emitted by PolicyCommitsMixin._policy_event. The last two
# are recorded deployment diagnostics, not formal registry transitions. Do not
# recognize these globally by prefix, or add them to the Mission projector.
DEPLOYMENT_EVENTS = frozenset(
    {
        "PolicySeeded",
        "PolicyProposed",
        "PolicyEvaluated",
        "PolicyApproved",
        "PolicyRejected",
        "PolicyPromoted",
        "PolicyRolledBack",
        "PolicyConfigDrift",
        "PolicyProposalRefused",
    }
)


@cache
def _mission_unknown_kind(kind):
    """Ask the existing pure Mission projector to recognize a type, independently
    of a corrupt row's payload. Known handlers may reject the empty shape; unknown
    handlers increment their dedicated counter. No production whitelist is changed.
    """
    projection = Projection()
    try:
        projection.apply({"type": kind, "payload": {}})
    except (KeyError, TypeError, AttributeError, ValueError):
        pass
    return kind in projection.unknown


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _comparison(value):
    # Preserve precise field/identity detection without exporting arbitrary values.
    return {
        **value,
        "mismatches": [
            {
                **{k: v for k, v in item.items() if k not in {"replayed", "library"}},
                "replayed_sha256": _hash(item.get("replayed")),
                "library_sha256": _hash(item.get("library")),
            }
            for item in value["mismatches"]
        ],
    }


class ReplayAudit:
    """Capture open connections before close, plus connections left open by tests.

    Store identity includes the file incarnation, so reopen deduplicates while
    distinct copies remain distinct inspected databases. Memory stores get their
    own identity. Every observation retains both the observing and opening nodeid.
    Exceptions are evidence, never a reason to omit a Mission or pass the audit.
    """

    def __init__(self):
        self.nodeid = "<collection>"
        self.stores = {}
        self.observations = {}
        self.store_errors = []
        self.unknown_events = set()
        self.scoped_unknown_events = set()
        self.known_database_paths = {}
        self.discovery_roots = set()
        self.discovery_passes = []
        self.execution_databases = {}
        self.test_roots = {}
        self.serial = 0
        self.original_init = None
        self.original_close = None

    def install(self):
        if self.original_init is not None:
            raise RuntimeError("audit already installed")
        self.original_init, self.original_close = Store.__init__, Store.close
        original_init, original_close = self.original_init, self.original_close
        audit = self

        def initialize(store, *args, **kwargs):
            original_init(store, *args, **kwargs)
            audit.register(store)

        initialize._p33_replay_original_init = original_init

        def close(store):
            try:
                audit.capture(store, "before_close")
            finally:
                audit.stores.pop(id(store), None)
                original_close(store)

        Store.__init__, Store.close = initialize, close

    def uninstall(self):
        if self.original_init is not None:
            Store.__init__, Store.close = self.original_init, self.original_close
            self.original_init = self.original_close = None
        self.stores.clear()

    def register(self, store, *, acquisition="observed"):
        self.serial += 1
        path = str(store.path)
        if path == ":memory:":
            identity = f"memory:{self.serial}"
        else:
            resolved = store.path.absolute().resolve()
            info = resolved.stat()
            identity = f"{resolved}:{info.st_dev}:{info.st_ino}:{getattr(info, 'st_birthtime', '')}"
            self.known_database_paths[str(resolved)] = identity
        self.stores[id(store)] = {
            "store": store,
            "database_id": identity,
            "path": path,
            "opened_by": self.nodeid,
            "acquisition": acquisition,
        }

    def capture(self, store, phase):
        entry = self.stores.get(id(store))
        if entry is None:
            return
        meta = {k: v for k, v in entry.items() if k != "store"}
        meta.update(nodeid=self.nodeid, phase=phase)
        try:
            with store._lock:
                connection = store.connection
                entered_transaction = connection.in_transaction
                meta["producer_transaction_open"] = (
                    entered_transaction and meta["acquisition"] == "observed"
                )
                meta["observer_owned_read_snapshot"] = (
                    not entered_transaction or meta["acquisition"] == "discovered"
                )
                view = nullcontext(connection) if entered_transaction else store.read_view()
                with view:
                    tables = {
                        r[0]
                        for r in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    ids, mission_ids = set(), set()
                    for table in ("missions", "events"):
                        if table in tables:
                            found = {
                                r[0]
                                for r in connection.execute(
                                    f"SELECT DISTINCT mission_id FROM {table}"
                                )
                            }
                            ids.update(found)
                            if table == "missions":
                                mission_ids.update(found)
                    if not {"missions", "events"} & tables:
                        self.store_errors.append(
                            {**meta, "stage": "inventory", "error": "no_mission_or_event_tables"}
                        )
                    for mid in sorted(ids, key=str):
                        scope = (
                            "deployment"
                            if mid == DEPLOYMENT_TIMELINE and mid not in mission_ids
                            else "mission"
                        )
                        self._mission(store, meta, mid, tables, scope=scope)
        except Exception as error:
            self.store_errors.append({**meta, "stage": "inventory", "error": type(error).__name__})

    def _mission(self, store, meta, mid, tables, *, scope):
        row = {
            **meta,
            "mission_id": mid,
            "scope": scope,
            # Keep the entering producer state, not the audit's own read BEGIN.
            "in_transaction": meta["producer_transaction_open"],
            "audit_read_transaction": meta["observer_owned_read_snapshot"],
            "event_count": 0,
            "event_types": {},
            "event_scan_complete": False,
            "unknown_event_types": None,
            "comparison": None,
            "gaps": [],
            "errors": [],
        }
        if scope == "deployment":
            row["registry_consistency"] = None
        elif mid == DEPLOYMENT_TIMELINE:
            row["errors"].append({"stage": "scope", "error": "global_owner_has_mission_row"})
        projection = Projection()
        raw = []
        try:
            if "events" not in tables:
                raise ValueError("events table unavailable")
            raw = [
                tuple(r)
                for r in store.connection.execute(
                    "SELECT event_id,seq,type,payload_json FROM events"
                    " WHERE mission_id IS ? ORDER BY seq",
                    (mid,),
                )
            ]
            row.update(
                event_count=len(raw),
                event_types=dict(sorted(Counter(r[2] for r in raw).items())),
                event_stream_sha256=_hash(raw),
            )
            # The public iterator traverses every page. Do not use list_events' default limit.
            events = events_from_store(store, mid)
            projection.feed(events)
            projection.check_structure()
            row.update(
                event_scan_complete=True,
                unknown_event_types=dict(projection.unknown),
                gaps=[
                    {
                        k: v
                        for k, v in gap.items()
                        if k in {"rule", "object", "id", "field", "at_event", "type"}
                    }
                    for gap in projection.gaps
                ],
            )
            row["raw_projection_gaps"] = list(row["gaps"])
            if scope == "deployment":
                # The Mission projector cannot validate the deployment sentinel.
                # Keep its raw diagnostic while using registry checks for scope.
                row["gaps"] = []
                row["unknown_event_types"] = dict(
                    sorted(Counter(r[2] for r in raw if r[2] not in DEPLOYMENT_EVENTS).items())
                )
                if any(
                    e.get("task_id") is not None or e.get("attempt_id") is not None for e in events
                ):
                    row["errors"].append(
                        {"stage": "scope", "error": "global_event_has_task_or_attempt"}
                    )
        except Exception as error:
            row["errors"].append({"stage": "events", "error": type(error).__name__})
        # A partial fold may already have detected unknowns. Retain them, but never
        # represent the incomplete scan as an authoritative empty unknown set.
        row["observed_unknown_event_types"] = dict(projection.unknown)
        for eid, _seq, kind, _payload in raw:
            raw_unknown = _mission_unknown_kind(kind)
            if raw_unknown:
                self.unknown_events.add((meta["database_id"], mid, eid, kind))
            if kind not in DEPLOYMENT_EVENTS if scope == "deployment" else raw_unknown:
                self.scoped_unknown_events.add((meta["database_id"], mid, eid, kind, scope))
        try:
            if scope == "deployment":
                # Existing production registry fold/table comparison, not a fake
                # Mission snapshot or a claim of Mission FORMAL_FIELDS coverage.
                row["registry_consistency"] = [
                    {
                        "object": p["object"],
                        "id": p.get("id"),
                        "table_sha256": _hash(p.get("table")),
                        "events_sha256": _hash(p.get("events")),
                    }
                    for p in registry_consistency(store)
                ]
            else:
                formal = formal_from_snapshot(store.snapshot(mid))
                row["formal_state_sha256"] = _hash(formal)
                if row["event_scan_complete"]:
                    row["comparison"] = _comparison(compare(projection.objects, formal))
        except Exception as error:
            row["errors"].append(
                {
                    "stage": "registry" if scope == "deployment" else "snapshot",
                    "error": type(error).__name__,
                }
            )
        body = {k: v for k, v in row.items() if k not in {"phase", "nodeid", "opened_by"}}
        key = (meta["nodeid"], meta["database_id"], mid, _hash(body))
        if key in self.observations:
            previous = self.observations[key]
            previous["phases"] = sorted(set(previous["phases"]) | {meta["phase"]})
        else:
            row["phases"] = [row.pop("phase")]
            row["observation_id"] = _hash(key)
            self.observations[key] = row

    def sweep(self, phase):
        for identity, entry in list(self.stores.items()):
            store = entry["store"]
            if isinstance(store, weakref.ReferenceType):
                store = store()
            if store is None:
                self.stores.pop(identity, None)
                continue
            self.capture(store, phase)
            # Retain newly opened stores until their first teardown, so forgotten
            # closes cannot lose evidence. Then avoid keeping every old test's
            # database alive; module/session fixtures remain reachable naturally.
            entry["store"] = weakref.ref(store)

    def discover(self, roots, phase):
        """Discover SQLite headers only inside explicitly supplied run roots.

        No home/cwd fallback, symlink traversal, migration or immutable=1 shortcut
        (which would miss committed WAL data). Missing known files stay OPEN.
        """

        def problem(path, code, error=None):
            self.store_errors.append(
                {
                    "nodeid": self.nodeid,
                    "phase": phase,
                    "stage": "discovery",
                    "path": str(path),
                    "error": code,
                    **({"exception": type(error).__name__} if error else {}),
                }
            )

        for value in roots:
            declared = Path(value).absolute()
            if declared.is_symlink():
                problem(declared, "discovery_root_missing_or_symlink")
                continue
            root = declared.resolve()
            self.discovery_roots.add(str(root))
            stats = {
                "root": str(root),
                "nodeid": self.nodeid,
                "phase": phase,
                "files_checked": 0,
                "sqlite_files": 0,
            }
            self.discovery_passes.append(stats)
            for known in self.known_database_paths:
                path = Path(known)
                if path.is_relative_to(root) and not path.exists():
                    problem(path, "deleted_database")
            if root.is_symlink() or not root.is_dir():
                problem(root, "discovery_root_missing_or_symlink")
                continue

            def walk_error(error):
                problem(getattr(error, "filename", root), "directory_unreadable", error)

            for base, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
                for name in list(directories):
                    path = Path(base) / name
                    if path.is_symlink():
                        directories.remove(name)
                        problem(path, "symlink_not_followed")
                for name in files:
                    path = Path(base) / name
                    try:
                        mode = path.lstat().st_mode
                        if stat.S_ISLNK(mode):
                            problem(path, "symlink_not_followed")
                            continue
                        if not stat.S_ISREG(mode):
                            continue
                        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        with os.fdopen(descriptor, "rb") as handle:
                            header = handle.read(16)
                        stats["files_checked"] += 1
                        if header != b"SQLite format 3\x00":
                            if (
                                path.suffix in {".db", ".sqlite", ".sqlite3"}
                                or str(path) in self.known_database_paths
                            ):
                                problem(path, "invalid_sqlite_header")
                            continue
                        stats["sqlite_files"] += 1
                        self._discovered_file(path, phase)
                    except Exception as error:
                        problem(path, "file_unreadable", error)

    def _discovered_file(self, path, phase):
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=1.0
        )
        inspected = None
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            # The sibling SDK execution store has Runs, not Mission Events.
            # Inventory it explicitly; never interpret its tables as a damaged
            # orchestration library or silently omit an actual Mission table.
            if (
                path.name != "orchestrator.db"
                and not tables.intersection({"missions", "events"})
                and {"sdk_schema_migrations", "runs", "run_events", "provider_invocations"}
                <= tables
            ):
                self.execution_databases[str(path.resolve())] = {
                    "path": str(path.resolve()),
                    "scope": "SDK execution; no Mission/event tables",
                    "run_count": connection.execute("SELECT count(*) FROM runs").fetchone()[0],
                    "provider_invocation_count": connection.execute(
                        "SELECT count(*) FROM provider_invocations"
                    ).fetchone()[0],
                    "execution_replay_verified": False,
                }
                return
            # Initialize an inspection object without Store.open's migrations or
            # feeding audit-owned reader instances back into nested observers.
            initialize = Store.__init__
            while hasattr(initialize, "_p33_replay_original_init"):
                initialize = initialize._p33_replay_original_init
            inspected = object.__new__(Store)
            initialize(inspected, connection, path, lambda: 0.0)
            inspected._readonly = True
            self.register(inspected, acquisition="discovered")
            self.capture(inspected, phase)
        finally:
            if inspected is not None:
                self.stores.pop(id(inspected), None)
            connection.close()

    def report(self):
        observations = list(self.observations.values())
        findings = []
        for row in observations:
            comparison = row["comparison"]
            formal_problem = (
                row["registry_consistency"] is None or bool(row["registry_consistency"])
                if row["scope"] == "deployment"
                else comparison is None or comparison["mismatches"] or comparison["not_covered"]
            )
            if (
                row["errors"]
                or row["in_transaction"]
                or not row["event_scan_complete"]
                or row["unknown_event_types"]
                or row["gaps"]
                or formal_problem
            ):
                findings.append(
                    {
                        "nodeid": row["nodeid"],
                        "database_id": row["database_id"],
                        "mission_id": row["mission_id"],
                        "scope": row["scope"],
                        "observation_id": row["observation_id"],
                    }
                )
        missions = [row for row in observations if row["scope"] == "mission"]
        global_streams = [
            {**{k: v for k, v in row.items() if k != "mission_id"}, "stream_id": row["mission_id"]}
            for row in observations
            if row["scope"] == "deployment"
        ]
        return {
            "schema": "p33-replay-audit-v1",
            "scope": "all Mission rows/event owners; deployment streams checked separately",
            "acquisition_methods": [
                "observed Store lifecycle",
                "discovered SQLite in declared run roots",
            ],
            "discovery_roots": sorted(self.discovery_roots),
            "discovery_passes": list(self.discovery_passes),
            "execution_databases": list(self.execution_databases.values()),
            "discovery_limit": "unobserved files deleted between checkpoints cannot be recovered",
            "negative_fixture_exemptions": [],
            "gate": "PASS" if observations and not findings and not self.store_errors else "OPEN",
            "database_count": len({row["database_id"] for row in observations}),
            "observed_database_count": len(
                {row["database_id"] for row in observations if row["acquisition"] == "observed"}
            ),
            "discovered_database_count": len(
                {row["database_id"] for row in observations if row["acquisition"] == "discovered"}
            ),
            "mission_count": len({(row["database_id"], row["mission_id"]) for row in missions}),
            "global_stream_count": len(
                {(row["database_id"], row["stream_id"]) for row in global_streams}
            ),
            "observation_count": len(observations),
            "raw_unknown_event_types": dict(
                sorted(Counter(item[3] for item in self.unknown_events).items())
            ),
            "raw_unknown_basis": "Mission Projection applied to every stream before scope routing",
            "mission_unknown_event_types": dict(
                sorted(
                    Counter(
                        item[3] for item in self.scoped_unknown_events if item[4] == "mission"
                    ).items()
                )
            ),
            "global_unknown_event_types": dict(
                sorted(
                    Counter(
                        item[3] for item in self.scoped_unknown_events if item[4] == "deployment"
                    ).items()
                )
            ),
            "deployment_known_event_types": sorted(DEPLOYMENT_EVENTS),
            "unknown_scan_complete": not self.store_errors
            and all(row["event_scan_complete"] for row in observations),
            "unknown_count_unit": "distinct database incarnation / Mission / event ID / type",
            "findings": findings,
            "store_errors": list(self.store_errors),
            "observations": missions,
            "global_streams": global_streams,
        }


def pytest_addoption(parser):
    group = parser.getgroup("p33 replay audit")
    group.addoption("--p33-replay-audit", metavar="JSON", help="write all-Mission replay inventory")
    group.addoption(
        "--p33-replay-audit-strict",
        action="store_true",
        help="nonzero exit when inventory gate is OPEN",
    )
    group.addoption(
        "--p33-replay-audit-root",
        action="append",
        default=[],
        metavar="DIR",
        help="additional current-run SDK .local-test-evidence child directory",
    )


def pytest_configure(config):
    value = config.getoption("p33_replay_audit")
    if not value:
        if config.getoption("p33_replay_audit_strict"):
            raise pytest.UsageError("strict replay audit requires an output path")
        return
    root = Path(__file__).resolve().parents[2] / ".local-test-evidence"
    output = Path(value).absolute().resolve()
    if output == root or not output.is_relative_to(root.resolve()) or output.exists():
        raise pytest.UsageError("replay audit needs a new JSON path under SDK .local-test-evidence")
    observer = ReplayAudit()
    evidence_roots = []
    for value in config.getoption("p33_replay_audit_root"):
        extra = Path(value).absolute().resolve()
        if extra == root.resolve() or not extra.is_relative_to(root.resolve()):
            raise pytest.UsageError("extra audit root must be a specific SDK evidence subdirectory")
        evidence_roots.append(extra)
    observer.install()
    config._p33_replay_audit = observer
    config._p33_replay_audit_path = output
    config._p33_replay_audit_roots = evidence_roots


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    observer = getattr(item.config, "_p33_replay_audit", None)
    if observer:
        observer.test_roots[item.nodeid] = [
            Path(str(item.funcargs[name])).absolute()
            for name in ("tmp_path", "tmpdir")
            if name in item.funcargs
        ]
    yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    del nextitem
    observer = getattr(item.config, "_p33_replay_audit", None)
    old = observer.nodeid if observer else None
    if observer:
        observer.nodeid = item.nodeid
    try:
        yield
    finally:
        if observer:
            observer.sweep("after_teardown")
            observer.discover(observer.test_roots.pop(item.nodeid, []), "after_teardown")
            observer.nodeid = old


def pytest_sessionfinish(session, exitstatus):
    observer = getattr(session.config, "_p33_replay_audit", None)
    if observer is None:
        return
    try:
        observer.nodeid = "<sessionfinish>"
        observer.sweep("sessionfinish")
        roots = list(session.config._p33_replay_audit_roots)
        factory = getattr(session.config, "_tmp_path_factory", None)
        if factory is not None and getattr(factory, "_basetemp", None) is not None:
            roots.append(factory.getbasetemp())
        observer.discover(roots, "sessionfinish")
        report = observer.report()
        report["pytest_exitstatus_before_audit"] = int(exitstatus)
        report["strict"] = session.config.getoption("p33_replay_audit_strict")
        output = session.config._p33_replay_audit_path
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            output.chmod(0o600)
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        session.config._p33_replay_audit_result = report
        if report["strict"] and report["gate"] != "PASS" and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    finally:
        observer.uninstall()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    del exitstatus
    report = getattr(config, "_p33_replay_audit_result", None)
    if report:
        terminalreporter.write_sep("-", "P33 all-Mission replay audit (negative fixtures retained)")
        terminalreporter.write_line(
            f"{report['mission_count']} database/Mission identities; "
            f"{len(report['findings'])} finding observations; gate={report['gate']}; "
            f"report={config._p33_replay_audit_path}"
        )


def pytest_unconfigure(config):
    observer = getattr(config, "_p33_replay_audit", None)
    if observer:
        observer.uninstall()
