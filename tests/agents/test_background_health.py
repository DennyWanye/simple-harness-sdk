"""2026-09-25 主流程优化条目 6：后台循环的错误要被记录、限频写日志，且一项出错不拖停整轮。"""

from __future__ import annotations

import logging

from simple_harness.agents.arp.errors import ArpError
from simple_harness.agents.background_health import LOG_INTERVAL_MS, BackgroundHealthBook


def test_book_counts_failures_and_logs_once_per_interval(caplog) -> None:
    now = [1_000]
    book = BackgroundHealthBook(clock_ms=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="simple_harness.agents.background"):
        book.fail("draining", ArpError("DUAL_DIRECTORY", "two dirs"), item="session-1")
        now[0] += 1_000
        book.fail("draining", ArpError("DUAL_DIRECTORY", "two dirs"), item="session-1")
        now[0] += LOG_INTERVAL_MS
        book.fail("draining", RuntimeError("boom"), item="session-2")
    rows = {row.loop: row for row in book.snapshot()}
    assert rows["draining"].consecutive_failures == 3
    assert rows["draining"].last_error_code == "RuntimeError" and rows["draining"].stuck_item == "session-2"
    assert rows["index"].consecutive_failures == 0
    # the second identical failure inside the interval is counted but not logged again
    assert [r.getMessage() for r in caplog.records if "draining" in r.getMessage()].__len__() == 2
    book.ok("draining")
    assert rows != {row.loop: row for row in book.snapshot()}
    assert {row.loop: row for row in book.snapshot()}["draining"].consecutive_failures == 0


def test_drive_draining_isolates_a_failing_session(monkeypatch) -> None:
    from types import SimpleNamespace

    from simple_harness.agents.arp import session_lifecycle, store

    calls: list[str] = []
    seen: list[tuple[str, str]] = []

    def advance(session_id: str):  # type: ignore[no-untyped-def]
        calls.append(session_id)
        if session_id == "bad":
            raise ArpError("MARKER_MISMATCH", "marker")
        if session_id == "busy":
            raise ArpError("FILE_BUSY", "busy")

    service = SimpleNamespace(connection=object(), advance=advance)
    monkeypatch.setattr(store, "list_sessions_in_state", lambda *_a, **_k: [
        SimpleNamespace(session_id="bad"), SimpleNamespace(session_id="busy"), SimpleNamespace(session_id="good")])
    driven = session_lifecycle.SessionLifecycleService.drive_draining(
        service, on_error=lambda sid, error: seen.append((sid, error.code)))  # type: ignore[arg-type]
    assert calls == ["bad", "busy", "good"] and driven == 1
    assert seen == [("bad", "MARKER_MISMATCH")]
