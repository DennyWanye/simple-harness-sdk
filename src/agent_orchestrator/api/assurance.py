# SPDX-License-Identifier: Apache-2.0
"""Tenant-bound read-only Assurance API for the Host (ASSURANCE-EXEC-1.1 §13, S25).

Three read verbs over the original Store: ``snapshot`` (criteria / reviews /
effects / closeout / contributions of one Mission at the current head or at a
fixed historical event ``seq``), ``review`` (one review key with its official
record's per-criterion grades and disclosure labels) and ``use_check`` (a
read-only current-use diagnostic that never signs an executable certificate).

The caller identity is fixed by the deployment (tenant and authenticated
principal come from the facade construction, never from a request body).
Historical facts are rebuilt from the Mission's immutable events and records;
``current_use`` is always computed under the *current* authority, root and
validity epochs, so a historical page never claims that an old record may be
used now. Wire shapes are the ``assurance/contracts/host-*-v1.schema.json``
documents shipped with this package.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from importlib import metadata
from typing import Any, NoReturn

from ..assurance.codec import AssuranceError, canonical, decode, fingerprint
from ..assurance.refs import AssuranceRef, Pin
from ..governance.permissions import Principal
from ..storage.assurance_reads import EpochSnapshot, read_epochs_locked
from ..storage.assurance_store import AssuranceStore
from ..storage.htn_store import HtnStore

SCHEMA_VERSION = 1
MAX_ITEMS = 100
MAX_TEXT = 2048
MAX_HISTORY_EVENTS = 20_000
ITEM_KINDS = ("CLOSEOUT", "CONTRIBUTION", "CRITERION", "EFFECT", "REVIEW")
ERROR_CODES = frozenset(
    {
        "NOT_FOUND",
        "PROFILE_UNBOUND",
        "RESTORE_QUARANTINED",
        "SNAPSHOT_CHANGED",
        "SOURCE_UNAVAILABLE",
        "LIMIT_REACHED",
        "CONTRACT_INVALID",
    }
)
_USE_PURPOSES = frozenset({"PLAN", "START", "MAINTAIN", "ACCEPT", "CONTEXT", "DISCLOSE", "RECOVERY"})
_QUARANTINE_CODES = frozenset(
    {
        "ROOT_QUARANTINED",
        "ROOT_MANIFEST_MISMATCH",
        "ROOT_MANIFEST_INVALID",
        "RESTORED_ROOT_REQUIRES_REAUTHORIZATION",
        "RESTORE_DATABASE_INTEGRITY",
        "RESTORE_DATABASE_INCOMPLETE",
        "RESTORE_DATABASE_INVENTORY_MISSING",
        "RESTORE_CONTEXT_INVENTORY_MISSING",
        "RESTORE_CONTEXT_INCOMPLETE",
        "ROOT_STATE_INVALID",
        "ROOT_INSTALL_COMMAND_REQUIRED",
    }
)


class AssuranceReadError(Exception):
    """A refused read; ``to_json(request_id)`` is the ``host-error-v1`` body."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown host error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message[:2000]
        self.retryable = bool(retryable)

    def to_json(self, request_id: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def _fail(code: str, message: str, *, retryable: bool = False) -> NoReturn:
    raise AssuranceReadError(code, message, retryable=retryable)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        _fail("CONTRACT_INVALID", f"{name} must be a nonempty string of at most {MAX_TEXT} characters")
    return value


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail("CONTRACT_INVALID", f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _fields(body: object, required: set[str]) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        _fail("CONTRACT_INVALID", "request body must be an object")
    if set(body) != required:
        missing = sorted(required - set(body))
        extra = sorted(set(body) - required)
        _fail("CONTRACT_INVALID", f"request fields mismatch (missing {missing}, unexpected {extra})")
    if body["schema_version"] != SCHEMA_VERSION:
        _fail("CONTRACT_INVALID", "schema_version must be 1")
    return dict(body)


def _view(body: Mapping[str, Any]) -> tuple[str, int | None]:
    view = body["view"]
    if view not in ("CURRENT", "HISTORY"):
        _fail("CONTRACT_INVALID", "view must be CURRENT or HISTORY")
    at_seq = body["at_event_seq"]
    if view == "CURRENT":
        if at_seq is not None:
            _fail("CONTRACT_INVALID", "CURRENT view does not take at_event_seq")
        return view, None
    return view, _integer(at_seq, "at_event_seq")


def _cursor_encode(body: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(canonical(body).encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_decode(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        _fail("CONTRACT_INVALID", "cursor must be a nonempty string")
    try:
        padded = value + "=" * (-len(value) % 4)
        body = decode(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:  # noqa: BLE001 - any malformed cursor is a contract error
        _fail("CONTRACT_INVALID", "cursor is not a valid page token")
    if not isinstance(body, dict) or body.get("version") != 1:
        _fail("CONTRACT_INVALID", "cursor is not a valid page token")
    return body


def sdk_fingerprint() -> str:
    """Bytes identity of the installed SDK: the wheel RECORD when installed, else versions."""
    from .. import __version__ as orchestrator_version

    record = None
    try:
        record = metadata.distribution("simple-harness-sdk").read_text("RECORD")
    except metadata.PackageNotFoundError:
        record = None
    if record:
        return hashlib.sha256(record.encode("utf-8")).hexdigest()
    try:
        from simple_harness import __version__ as sdk_version
    except ImportError:  # pragma: no cover - the SDK ships both packages
        sdk_version = "unknown"
    return fingerprint({"sdk": sdk_version, "orchestrator": orchestrator_version, "source": True})


class AssuranceApi:
    """Fixed-caller read verbs; one instance per (Store, tenant, principal)."""

    def __init__(
        self,
        commit: Any,
        *,
        tenant_id: str,
        principal: Principal,
        validity: Any,
        host_fingerprint: str,
    ) -> None:
        if not isinstance(principal, Principal) or not str(tenant_id).strip():
            raise ValueError("an authenticated principal and tenant are required")
        digest = str(host_fingerprint).lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("host_fingerprint must be a SHA-256 hex digest")
        self._commit = commit
        self._store = commit.store
        self._tenant = str(tenant_id)
        self._principal = principal
        self._validity = validity
        self._host_fingerprint = digest
        self._sdk_fingerprint = sdk_fingerprint()

    # ------------------------------------------------------------ identity
    def matches_binding(self, commit: Any, tenant_id: str, principal: Any) -> bool:
        return commit is self._commit and tenant_id == self._tenant and principal == self._principal

    @property
    def tenant_id(self) -> str:
        return self._tenant

    # ------------------------------------------------------------- gates
    def _root(self) -> str:
        gate = self._commit._assurance_root_gate
        if gate is None:
            _fail("PROFILE_UNBOUND", "assurance root is not installed on this deployment")
        try:
            return gate.require_execution().root_incarnation_id
        except AssuranceError as error:
            if error.code in _QUARANTINE_CODES:
                _fail("RESTORE_QUARANTINED", "restored root requires current re-authorization")
            _fail("PROFILE_UNBOUND", f"assurance root unavailable: {error.code}")

    def _mission_locked(self, mission_id: str) -> Any:
        mission = self._store.get_mission(mission_id)
        if mission is None or mission.tenant_id != self._tenant:
            _fail("NOT_FOUND", "no such object for this caller")
        try:
            lane = AssuranceStore(self._store).lane(mission.id)
        except AssuranceError as error:
            _fail("PROFILE_UNBOUND", f"mission has no assured profile: {error.code}")
        if lane != "ASSURANCE_1_1":
            _fail("PROFILE_UNBOUND", f"mission lane is {lane}, not ASSURANCE_1_1")
        return mission

    def _epochs_locked(self, mission_id: str) -> EpochSnapshot:
        try:
            return read_epochs_locked(self._store.connection, mission_id)
        except AssuranceError as error:
            _fail("SOURCE_UNAVAILABLE", f"validity epochs unavailable: {error.code}", retryable=True)

    def _envelope(
        self, request_id: str, mission_id: str, view: str, snapshot_seq: int, root: str
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "mission_id": mission_id,
            "view": view,
            "snapshot_seq": snapshot_seq,
            "root_incarnation_id": root,
            "sdk_fingerprint": self._sdk_fingerprint,
            "host_fingerprint": self._host_fingerprint,
        }

    # ------------------------------------------------------ certificates
    def _certificate_use(
        self, rows: list[Any], *, now_ms: int, epochs: EpochSnapshot, root: str
    ) -> tuple[str, list[str]]:
        """Classify the newest committed certificate under the current epochs."""
        if not rows:
            return "UNAVAILABLE", ["NO_COMMITTED_CERTIFICATE"]
        row = max(rows, key=lambda r: r["rowid"])
        body = decode(row["certificate_json"])
        reasons: list[str] = []
        if body["decision"] == "BLOCKED":
            return "BLOCKED", ["CERTIFICATE_BLOCKED"]
        if body["root_incarnation_id"] != root:
            reasons.append("ROOT_CHANGED")
        if (body["mission_epoch"], body["environment_epoch"], body["clock_generation"]) != (
            epochs.mission,
            epochs.environment,
            epochs.clock_generation,
        ):
            reasons.append("SOURCE_CHANGED")
        if epochs.clock_state != "STABLE":
            reasons.append("TIME_DISCONTINUITY")
        if row["not_after_ms"] is not None and now_ms >= row["not_after_ms"]:
            reasons.append("EXPIRED")
        if body["decision"] != "USABLE":
            reasons.append("CERTIFICATE_" + str(body["decision"]))
        return ("USABLE" if not reasons else "STALE"), reasons

    def _certificates_for_consumer(self, mission_id: str, kind: str, consumer_id: str | None) -> list[Any]:
        query = (
            "SELECT rowid,* FROM assurance_use_certificates WHERE mission_id=? AND consumer_kind=?"
        )
        params: tuple[Any, ...] = (mission_id, kind)
        if consumer_id is not None:
            query += " AND consumer_id=?"
            params += (consumer_id,)
        return self._store.connection.execute(query + " ORDER BY rowid", params).fetchall()

    def _certificates_for_record(self, mission_id: str, record_id: str) -> list[Any]:
        ids = [
            row[0]
            for row in self._store.connection.execute(
                "SELECT subject_id FROM commit_receipts WHERE kind='AssuranceUseCertified' "
                "AND json_extract(receipt_json,'$.mission_id')=? "
                "AND json_extract(receipt_json,'$.record_id')=?",
                (mission_id, record_id),
            ).fetchall()
        ]
        rows = []
        for certificate_id in ids:
            rows.extend(
                self._store.connection.execute(
                    "SELECT rowid,* FROM assurance_use_certificates WHERE mission_id=? "
                    "AND certificate_id=?",
                    (mission_id, certificate_id),
                ).fetchall()
            )
        return rows

    # ----------------------------------------------------------- reviews
    def _review_rows(self, mission_id: str) -> list[Any]:
        return self._store.connection.execute(
            "SELECT * FROM assurance_review_bindings WHERE mission_id=? ORDER BY review_key",
            (mission_id,),
        ).fetchall()

    def _official(self, htn: HtnStore, package_id: str) -> Any:
        try:
            return htn.official_review_record(package_id)
        except Exception:  # noqa: BLE001 - an undecodable record is reported, not raised
            return None

    def _manifest(self, mission_id: str, manifest_hash: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT manifest_json,origin_mission_id FROM input_manifests WHERE manifest_hash=?",
            (manifest_hash,),
        ).fetchone()
        if row is None:
            return None
        body = decode(row["manifest_json"])
        if not isinstance(body, dict) or "criteria" not in body:
            return None
        return body

    def _rejected_source(self, mission_id: str, review_key: str) -> bool:
        return (
            self._store.connection.execute(
                "SELECT 1 FROM commit_receipts WHERE kind='AssuranceReviewImportRejected' "
                "AND json_extract(receipt_json,'$.mission_id')=? "
                "AND json_extract(receipt_json,'$.review_key')=? LIMIT 1",
                (mission_id, review_key),
            ).fetchone()
            is not None
        )

    def _root_review(self, mission_id: str, htn: HtnStore) -> tuple[Any, dict[str, Any] | None]:
        """The official MISSION_FINAL record and its evidence manifest, if any."""
        for row in self._review_rows(mission_id):
            body = decode(row["binding_json"])
            if body["subject"]["purpose"] != "MISSION_FINAL":
                continue
            record = self._official(htn, row["package_id"])
            if record is not None:
                return record, self._manifest(mission_id, record.evidence_manifest_hash)
        return None, None

    # --------------------------------------------------------- item builders
    @staticmethod
    def _item(
        kind: str,
        identifier: str,
        history_state: str,
        current_use: str,
        reasons: list[str],
        evidence_count: int = 0,
        artifact_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        unique: list[str] = []
        for reason in reasons:
            if reason not in unique:
                unique.append(str(reason)[:MAX_TEXT])
        return {
            "kind": kind,
            "id": identifier[:MAX_TEXT],
            "history_state": history_state[:MAX_TEXT],
            "current_use": current_use,
            "reason_codes": unique[:64],
            "evidence_count": int(evidence_count),
            "artifact_ref": artifact_ref,
        }

    @staticmethod
    def _artifact_ref(acceptance: Any) -> dict[str, Any] | None:
        for ref in acceptance.artifact_refs:
            if str(getattr(ref, "kind", "")) == "artifact":
                return {
                    "kind": "artifact",
                    "pin": {"id": ref.id, "revision": int(ref.revision), "content_hash": ref.content_hash},
                }
        return None

    def _current_items(
        self, mission: Any, *, now_ms: int, epochs: EpochSnapshot, root: str
    ) -> list[dict[str, Any]]:
        from ..orchestrator.completion_status import read_current_effect
        from ..orchestrator.scoped_content_review import uses_completion_protocol

        store, mission_id = self._store, mission.id
        htn = HtnStore(store)
        items: list[dict[str, Any]] = []
        # CRITERION: latest approved requirements, graded by the official root review.
        requirements = htn.latest_requirements_revision(mission_id)
        root_record, manifest = self._root_review(mission_id, htn)
        graded = {} if manifest is None else {row["criterion_id"]: row for row in manifest["criteria"]}
        root_use, root_reasons = ("NOT_APPLICABLE", ["ROOT_REVIEW_NOT_OFFICIAL"])
        if root_record is not None:
            root_use, root_reasons = self._certificate_use(
                self._certificates_for_consumer(mission_id, "ROOT_RESOLUTION", None),
                now_ms=now_ms, epochs=epochs, root=root,
            )
        if requirements is not None:
            for criterion in requirements.criteria:
                detail = graded.get(criterion.criterion_id)
                reasons = list(root_reasons)
                if detail is not None and detail.get("check_reason"):
                    reasons.append("check:" + str(detail["check_reason"]))
                items.append(self._item(
                    "CRITERION", criterion.criterion_id,
                    "UNREVIEWED" if detail is None else str(detail["effective_grade"]),
                    root_use, reasons,
                    0 if detail is None else len(detail.get("evidence_refs", ())),
                ))
        # REVIEW: every prepared review key of the Mission.
        for row in self._review_rows(mission_id):
            record = self._official(htn, row["package_id"])
            if record is not None:
                state = "OFFICIAL_" + str(record.verdict)
                use, reasons = self._certificate_use(
                    self._certificates_for_record(mission_id, str(record.record_id)),
                    now_ms=now_ms, epochs=epochs, root=root,
                )
                count = len(record.criteria)
            elif self._rejected_source(mission_id, row["review_key"]):
                state, use, reasons, count = "REJECTED_SOURCE", "NOT_APPLICABLE", ["IMPORT_REJECTED"], 0
            else:
                state, use, reasons, count = "PENDING", "NOT_APPLICABLE", ["NO_OFFICIAL_RECORD"], 0
            items.append(self._item("REVIEW", row["review_key"], state, use, reasons, count))
        # EFFECT: required effects of each completion scope under the protocol.
        if uses_completion_protocol(store, mission_id):
            scopes = store.connection.execute(
                "SELECT scope_id,spec_hash,document_json FROM operation_completion_scopes "
                "WHERE mission_id=? ORDER BY scope_id",
                (mission_id,),
            ).fetchall()
            for scope in scopes:
                document = decode(scope["document_json"])
                for effect_key in document.get("required_effect_keys", ()):
                    try:
                        current = read_current_effect(store, mission_id, scope["spec_hash"], str(effect_key))
                        state, reasons = str(current.get("state")), []
                    except Exception as error:  # noqa: BLE001 - reported per item, never raised
                        state, reasons = "UNAVAILABLE", ["EFFECT_READ_FAILED:" + type(error).__name__]
                    items.append(self._item(
                        "EFFECT", f"{scope['scope_id']}:{effect_key}", state, "NOT_APPLICABLE", reasons,
                    ))
        # CLOSEOUT: the consumer's projected row.
        closeout = store.connection.execute(
            "SELECT * FROM assurance_closeouts WHERE mission_id=?", (mission_id,)
        ).fetchone()
        if closeout is None:
            items.append(self._item("CLOSEOUT", mission_id, "NOT_EVALUATED", "UNAVAILABLE", ["NO_CLOSEOUT_ROW"]))
        else:
            body = decode(closeout["check_body_json"])
            reasons = [str(r) for r in body.get("reasons", ())]
            use = "USABLE"
            if body.get("root_incarnation_id") != root:
                use, reasons = "STALE", [*reasons, "ROOT_CHANGED"]
            captured = body.get("epochs") or {}
            if (captured.get("mission"), captured.get("environment"), captured.get("clock_generation")) != (
                epochs.mission, epochs.environment, epochs.clock_generation,
            ):
                use, reasons = "STALE", [*reasons, "SOURCE_CHANGED"]
            items.append(self._item("CLOSEOUT", mission_id, str(closeout["state"]), use, reasons))
        # CONTRIBUTION: every Acceptance, licensed by its ACCEPTANCE certificate.
        for acceptance in htn.list_acceptances(mission_id):
            use, reasons = self._certificate_use(
                self._certificates_for_consumer(mission_id, "ACCEPTANCE", str(acceptance.acceptance_id)),
                now_ms=now_ms, epochs=epochs, root=root,
            )
            items.append(self._item(
                "CONTRIBUTION", str(acceptance.acceptance_id), str(acceptance.validity), use, reasons,
                len(acceptance.artifact_refs), self._artifact_ref(acceptance),
            ))
        return items

    def _history_items(
        self, mission: Any, *, at_seq: int, now_ms: int, epochs: EpochSnapshot, root: str
    ) -> list[dict[str, Any]]:
        """Immutable facts up to ``at_seq``; ``current_use`` still under current authority."""
        store, mission_id = self._store, mission.id
        htn = HtnStore(store)
        events: list[Any] = []
        after = 0
        while True:
            page = store.list_events(mission_id, after_seq=after, limit=5000)
            for event in page:
                if event.seq is None or event.seq > at_seq:
                    break
                events.append(event)
            if not page or page[-1].seq is None or page[-1].seq > at_seq:
                break
            after = page[-1].seq
            if len(events) > MAX_HISTORY_EVENTS:
                _fail("LIMIT_REACHED", "history rebuild exceeds the supported event count")
        activation = next((e for e in events if e.type == "AssuranceProfileActivated"), None)
        if activation is None:
            _fail("SOURCE_UNAVAILABLE", "HISTORY_UNAVAILABLE: no assured activation at or before this seq")
        items: list[dict[str, Any]] = []
        # Reviews and the root grades from prepared/imported events.
        reviews: dict[str, tuple[str, Any]] = {}
        root_manifest = None
        for event in events:
            payload = dict(event.payload)
            if event.type == "AssuranceReviewPrepared" and "review_key" in payload:
                reviews.setdefault(str(payload["review_key"]), ("PENDING", None))
            elif event.type == "AssuranceReviewImportRejected" and "review_key" in payload:
                reviews[str(payload["review_key"])] = ("REJECTED_SOURCE", None)
            elif event.type == "AssuranceReviewImported" and "record_id" in payload:
                side = store.connection.execute(
                    "SELECT b.review_key AS review_key, r.package_id AS package_id "
                    "FROM assurance_review_record_bindings b "
                    "JOIN assurance_review_bindings r ON r.review_key=b.review_key AND r.mission_id=b.mission_id "
                    "WHERE b.record_id=? AND b.mission_id=?",
                    (str(payload["record_id"]), mission_id),
                ).fetchone()
                if side is None:
                    continue
                record = self._official(htn, side["package_id"])
                if record is None or str(record.record_id) != str(payload["record_id"]):
                    continue
                reviews[str(side["review_key"])] = ("OFFICIAL_" + str(record.verdict), record)
                if str(record.purpose) == "MISSION_FINAL":
                    root_manifest = self._manifest(mission_id, record.evidence_manifest_hash)
        # Criteria of the activated requirements revision (immutable by content hash).
        requirements = None
        wanted = dict(activation.payload).get("requirements_hash")
        for revision in htn.list_requirements_revisions(mission_id):
            if revision.content_hash() == wanted:
                requirements = revision
        graded = {} if root_manifest is None else {r["criterion_id"]: r for r in root_manifest["criteria"]}
        root_use, root_reasons = ("NOT_APPLICABLE", ["ROOT_REVIEW_NOT_OFFICIAL_AT_SEQ"])
        if root_manifest is not None:
            root_use, root_reasons = self._certificate_use(
                self._certificates_for_consumer(mission_id, "ROOT_RESOLUTION", None),
                now_ms=now_ms, epochs=epochs, root=root,
            )
        if requirements is not None:
            for criterion in requirements.criteria:
                detail = graded.get(criterion.criterion_id)
                items.append(self._item(
                    "CRITERION", criterion.criterion_id,
                    "UNREVIEWED" if detail is None else str(detail["effective_grade"]),
                    root_use, list(root_reasons),
                    0 if detail is None else len(detail.get("evidence_refs", ())),
                ))
        for review_key, (state, record) in reviews.items():
            if record is None:
                use, reasons, count = "NOT_APPLICABLE", ["NO_OFFICIAL_RECORD_AT_SEQ"], 0
            else:
                use, reasons = self._certificate_use(
                    self._certificates_for_record(mission_id, str(record.record_id)),
                    now_ms=now_ms, epochs=epochs, root=root,
                )
                count = len(record.criteria)
            items.append(self._item("REVIEW", review_key, state, use, reasons, count))
        for event in events:
            if event.type == "OperationOutcomeAccepted":
                payload = dict(event.payload)
                items.append(self._item(
                    "EFFECT", str(payload.get("effect_key", event.id)), "ACCEPTED", "NOT_APPLICABLE", [],
                ))
        closeouts = [e for e in events if e.type == "AssuranceCloseoutEvaluated"]
        if closeouts:
            payload = dict(closeouts[-1].payload)
            items.append(self._item(
                "CLOSEOUT", mission_id, str(payload.get("state")), "NOT_APPLICABLE",
                [str(r) for r in payload.get("reasons", ())],
            ))
        else:
            items.append(self._item("CLOSEOUT", mission_id, "NOT_EVALUATED", "NOT_APPLICABLE", ["NO_CLOSEOUT_AT_SEQ"]))
        seen: set[str] = set()
        for event in events:
            if event.type != "AssuranceUseCertified":
                continue
            payload = dict(event.payload)
            if payload.get("consumer_kind") != "ACCEPTANCE" or payload.get("consumer_id") in seen:
                continue
            acceptance_id = str(payload["consumer_id"])
            seen.add(acceptance_id)
            use, reasons = self._certificate_use(
                self._certificates_for_consumer(mission_id, "ACCEPTANCE", acceptance_id),
                now_ms=now_ms, epochs=epochs, root=root,
            )
            artifact = None
            try:
                acceptance = htn.get_acceptance(acceptance_id)
                artifact, count = self._artifact_ref(acceptance), len(acceptance.artifact_refs)
            except Exception:  # noqa: BLE001 - the event alone witnesses the acceptance
                count = 0
            items.append(self._item("CONTRIBUTION", acceptance_id, "ACCEPTED", use, reasons, count, artifact))
        return items

    # -------------------------------------------------------------- paging
    @staticmethod
    def _page(
        items: list[dict[str, Any]], *, limit: int, cursor: Mapping[str, Any] | None,
        mission_id: str, view: str, at_seq: int, filter_hash: str,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        ordered = sorted(items, key=lambda item: (item["kind"], item["id"]))
        if cursor is not None:
            if (
                cursor.get("mission_id") != mission_id
                or cursor.get("view") != view
                or cursor.get("at_seq") != at_seq
                or cursor.get("filter_hash") != filter_hash
            ):
                _fail("SNAPSHOT_CHANGED", "snapshot changed since the first page; read again from the start", retryable=True)
            last = cursor.get("last_sort_key")
            if not isinstance(last, list) or len(last) != 2:
                _fail("CONTRACT_INVALID", "cursor is not a valid page token")
            ordered = [item for item in ordered if (item["kind"], item["id"]) > (str(last[0]), str(last[1]))]
        page = ordered[:limit]
        more = len(ordered) > limit
        next_cursor = None
        if more:
            next_cursor = _cursor_encode({
                "version": 1, "mission_id": mission_id, "view": view, "at_seq": at_seq,
                "filter_hash": filter_hash, "last_sort_key": [page[-1]["kind"], page[-1]["id"]],
            })
        return page, next_cursor, more

    # -------------------------------------------------------------- verbs
    def snapshot(self, body: Mapping[str, Any]) -> dict[str, Any]:
        request = _fields(body, {"schema_version", "request_id", "mission_id", "view", "at_event_seq", "cursor", "limit"})
        request_id = _text(request["request_id"], "request_id")
        mission_id = _text(request["mission_id"], "mission_id")
        view, at_seq = _view(request)
        limit = _integer(request["limit"], "limit", minimum=1, maximum=MAX_ITEMS)
        cursor = None if request["cursor"] is None else _cursor_decode(request["cursor"])
        root = self._root()
        with self._store.read_view():
            mission = self._mission_locked(mission_id)
            epochs = self._epochs_locked(mission.id)
            head = self._store.last_event_seq(mission.id)
            now_ms = int(self._store.now * 1000)
            if view == "CURRENT":
                snapshot_seq = head
                filter_hash = fingerprint({"epochs": epochs.to_json(), "root": root, "seq": head})
                items = self._current_items(mission, now_ms=now_ms, epochs=epochs, root=root)
            else:
                assert at_seq is not None
                if at_seq > head:
                    _fail("NOT_FOUND", "at_event_seq is beyond this Mission's last event")
                snapshot_seq = at_seq
                filter_hash = fingerprint({"history": at_seq})
                items = self._history_items(mission, at_seq=at_seq, now_ms=now_ms, epochs=epochs, root=root)
        page, next_cursor, truncated = self._page(
            items, limit=limit, cursor=cursor, mission_id=mission.id, view=view,
            at_seq=snapshot_seq, filter_hash=filter_hash,
        )
        return {
            **self._envelope(request_id, mission.id, view, snapshot_seq, root),
            "items": page,
            "next_cursor": next_cursor,
            "truncated": truncated,
        }

    def review(self, body: Mapping[str, Any]) -> dict[str, Any]:
        request = _fields(body, {"schema_version", "request_id", "mission_id", "review_key", "cursor", "limit"})
        request_id = _text(request["request_id"], "request_id")
        mission_id = _text(request["mission_id"], "mission_id")
        review_key = _text(request["review_key"], "review_key")
        limit = _integer(request["limit"], "limit", minimum=1, maximum=MAX_ITEMS)
        cursor = None if request["cursor"] is None else _cursor_decode(request["cursor"])
        root = self._root()
        with self._store.read_view():
            mission = self._mission_locked(mission_id)
            epochs = self._epochs_locked(mission.id)
            head = self._store.last_event_seq(mission.id)
            now_ms = int(self._store.now * 1000)
            row = self._store.connection.execute(
                "SELECT * FROM assurance_review_bindings WHERE mission_id=? AND review_key=?",
                (mission.id, review_key),
            ).fetchone()
            if row is None:
                _fail("NOT_FOUND", "no such object for this caller")
            binding = decode(row["binding_json"])
            htn = HtnStore(self._store)
            record = self._official(htn, row["package_id"])
            assessments: list[dict[str, Any]] = []
            record_ref = None
            verdict = None
            if record is not None:
                official_status = "OFFICIAL"
                verdict = str(record.verdict)
                verdict = {"REJECTED": "REJECT"}.get(verdict, verdict)
                record_ref = AssuranceRef("review", Pin(str(record.record_id), 0, fingerprint(record.to_json()))).to_json()
                manifest = self._manifest(mission.id, record.evidence_manifest_hash)
                if manifest is None:
                    official_status = "UNAVAILABLE"
                else:
                    for detail in manifest["criteria"]:
                        labels = sorted({
                            str(label) for label in detail.get("evidence_ids", ())
                            if isinstance(label, str) and label.startswith("ev-") and len(label) == 67
                        })
                        reasons = []
                        if detail.get("check_reason"):
                            reasons.append("check:" + str(detail["check_reason"]))
                        reasons.extend("limitation:" + str(limit) for limit in detail.get("limitations", ()))
                        gate = detail.get("check_gate")
                        assessments.append({
                            "criterion_id": str(detail["criterion_id"]),
                            "model_grade": str(detail["model_grade"]),
                            "effective_grade": str(detail["effective_grade"]),
                            "check_gate": "NOT_APPLICABLE" if gate is None else str(gate),
                            "reason_codes": [r[:MAX_TEXT] for r in reasons][:64],
                            "evidence_labels": labels[:256],
                        })
                use, _ = self._certificate_use(
                    self._certificates_for_record(mission.id, str(record.record_id)),
                    now_ms=now_ms, epochs=epochs, root=root,
                )
            elif self._rejected_source(mission.id, review_key):
                official_status, use = "REJECTED_SOURCE", "UNAVAILABLE"
            else:
                official_status, use = "PENDING", "UNAVAILABLE"
        filter_hash = fingerprint({"review": review_key, "record": None if record is None else str(record.record_id)})
        ordered = sorted(assessments, key=lambda a: a["criterion_id"])
        start = 0
        if cursor is not None:
            if cursor.get("mission_id") != mission.id or cursor.get("view") != "REVIEW" or cursor.get("filter_hash") != filter_hash:
                _fail("SNAPSHOT_CHANGED", "review changed since the first page; read again from the start", retryable=True)
            last = cursor.get("last_sort_key")
            if not isinstance(last, list) or len(last) != 2:
                _fail("CONTRACT_INVALID", "cursor is not a valid page token")
            ordered = [a for a in ordered if a["criterion_id"] > str(last[1])]
        page = ordered[start:limit]
        truncated = len(ordered) > limit
        next_cursor = None
        if truncated:
            next_cursor = _cursor_encode({
                "version": 1, "mission_id": mission.id, "view": "REVIEW", "at_seq": head,
                "filter_hash": filter_hash, "last_sort_key": ["ASSESSMENT", page[-1]["criterion_id"]],
            })
        return {
            **self._envelope(request_id, mission.id, "CURRENT", head, root),
            "review_key": review_key,
            "purpose": str(binding["subject"]["purpose"]),
            "package_ref": {"kind": "review_package", "pin": dict(binding["package_ref"])},
            "record_ref": record_ref,
            "official_status": official_status,
            "verdict": verdict,
            "assessments": page,
            "next_cursor": next_cursor,
            "truncated": truncated,
            "current_use": use,
        }

    def use_check(self, body: Mapping[str, Any]) -> dict[str, Any]:
        request = _fields(body, {"schema_version", "request_id", "mission_id", "subject_ref", "view", "at_event_seq"})
        request_id = _text(request["request_id"], "request_id")
        mission_id = _text(request["mission_id"], "mission_id")
        view, at_seq = _view(request)
        try:
            subject = AssuranceRef.from_json(request["subject_ref"])
        except AssuranceError as error:
            _fail("CONTRACT_INVALID", f"subject_ref is invalid: {error.code}")
        root = self._root()
        with self._store.read_view():
            mission = self._mission_locked(mission_id)
            head = self._store.last_event_seq(mission.id)
        if view == "HISTORY":
            _fail("SOURCE_UNAVAILABLE", "HISTORY_UNAVAILABLE: use diagnostics are computed for the current head only")
        now_ms = int(self._store.now * 1000)
        decision, coverage, reasons, expires = "UNAVAILABLE", "INCOMPLETE", [], None
        if self._validity is None:
            reasons.append("VALIDITY_NOT_INSTALLED")
        elif subject.kind not in ("result", "task"):
            reasons.append("SUBJECT_KIND_UNSUPPORTED:" + subject.kind)
        else:
            candidate = None
            try:
                if subject.kind == "result":
                    candidate = self._validity.prepare_accept_use_for_result(mission.id, subject.pin.id)
                else:
                    with self._store.read_view():
                        record, _ = self._root_review(mission.id, HtnStore(self._store))
                    if record is None:
                        raise AssuranceError("REVIEW_NOT_OFFICIAL")
                    if str(record.binding.subject_ref.id) != subject.pin.id:
                        raise AssuranceError("USE_SUBJECT_INVALID")
                    candidate = self._validity.prepare_root_use(record, resolution_id="host-diagnostic:" + request_id)
                certificate = candidate.certificate
                decision = {"USABLE": "USABLE", "NEEDS_REVIEW": "RECHECK_REQUIRED", "BLOCKED": "BLOCKED"}.get(
                    certificate.decision, "UNAVAILABLE",
                )
                coverage = certificate.coverage
                expires = certificate.not_after_ms
                reasons.extend(certificate.reasons)
                if certificate.root_incarnation_id != root:
                    decision, reasons = "RECHECK_REQUIRED", [*reasons, "ROOT_CHANGED"]
            except AssuranceError as error:
                reasons.append(str(error.code))
                if error.code in _QUARANTINE_CODES:
                    _fail("RESTORE_QUARANTINED", "restored root requires current re-authorization")
            finally:
                if candidate is not None:
                    try:
                        self._validity.forget(mission.id, str(candidate.record.record_id))
                    except Exception:  # noqa: BLE001 - a diagnostic leaves no cached candidate
                        pass
        return {
            **self._envelope(request_id, mission.id, view, head, root),
            "subject_ref": subject.to_json(),
            "purpose": "ACCEPT",
            "decision": decision,
            "diagnostic_only": True,
            "coverage": coverage,
            "reason_codes": [str(r)[:MAX_TEXT] for r in reasons][:64],
            "checked_at_ms": now_ms,
            "expires_at_ms": expires,
            "certificate_ref": None,
        }


__all__ = ["AssuranceApi", "AssuranceReadError", "ITEM_KINDS", "sdk_fingerprint"]
