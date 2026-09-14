# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Offline sensitivity checks for the future off-peak admission gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent_orchestrator.evaluation.run_window import (
    DEEPSEEK_OFFICIAL_PRICING_UTC_2026_09_14,
    ProviderWindowSchedule,
    UtcWeeklyWindow,
    admit_run_window,
)

OFFICIAL = DEEPSEEK_OFFICIAL_PRICING_UTC_2026_09_14


def decide(schedule: ProviderWindowSchedule, **kwargs):
    return admit_run_window(schedule, provider_id=schedule.provider_id, **kwargs)


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("hour", "admitted", "next_hour"),
    [
        (0, True, 0),
        (1, False, 4),
        (4, True, 4),
        (6, False, 10),
        (10, True, 10),
    ],
)
def test_weekday_exact_peak_boundaries(hour: int, admitted: bool, next_hour: int) -> None:
    decision = decide(OFFICIAL, now=utc(14, hour), max_duration=timedelta(minutes=1))
    assert decision.admitted is admitted
    assert decision.next_allowed_start == utc(14, next_hour)


def test_queue_episode_and_drain_must_all_fit_and_skip_short_window() -> None:
    now = utc(14, 0, 30)
    decision = decide(
        OFFICIAL,
        now=now,
        max_duration=timedelta(minutes=90),
        queue_wait=timedelta(minutes=20),
        drain_margin=timedelta(minutes=11),
    )
    assert not decision.admitted
    assert decision.reason == "insufficient_window"
    assert decision.next_allowed_start == utc(14, 10)  # 121 minutes misses 04:00-06:00.
    assert decision.window_end == utc(15, 1)
    assert decide(
        OFFICIAL,
        now=utc(14, 4),
        max_duration=timedelta(minutes=90),
        queue_wait=timedelta(minutes=20),
        drain_margin=timedelta(minutes=10),
    ).admitted  # Exact finish at 06:00 is valid.


def test_weekend_is_contiguous_from_friday_to_monday() -> None:
    assert decide(OFFICIAL, now=utc(18, 10), max_duration=timedelta(hours=63)).admitted
    assert not decide(
        OFFICIAL, now=utc(18, 10), max_duration=timedelta(hours=63, minutes=1)
    ).admitted
    monday = decide(OFFICIAL, now=utc(20, 23), max_duration=timedelta(hours=2))
    assert monday.admitted
    assert monday.window_end == utc(21, 1)


def test_timezone_and_dst_inputs_are_converted_to_utc_before_admission() -> None:
    la = ZoneInfo("America/Los_Angeles")
    before_jump = datetime(2026, 3, 8, 1, 30, tzinfo=la)
    after_jump = datetime(2026, 3, 8, 3, 30, tzinfo=la)
    assert before_jump.astimezone(UTC) == datetime(2026, 3, 8, 9, 30, tzinfo=UTC)
    assert after_jump.astimezone(UTC) == datetime(2026, 3, 8, 10, 30, tzinfo=UTC)
    assert decide(OFFICIAL, now=before_jump, max_duration=timedelta(hours=2)).admitted
    peak = decide(
        OFFICIAL,
        now=datetime(2026, 3, 9, 2, 0, tzinfo=la),
        max_duration=timedelta(minutes=1),
    )
    assert not peak.admitted
    assert peak.next_allowed_start == datetime(2026, 3, 9, 10, tzinfo=UTC)


def test_provider_schedule_is_explicit_and_proxy_can_differ() -> None:
    relay = ProviderWindowSchedule(
        "relay-account-a", "contract-v2-utc", (UtcWeeklyWindow(0, 60, 120),)
    )
    assert decide(
        relay, now=utc(14, 0), max_duration=timedelta(minutes=1)
    ).next_allowed_start == utc(14, 1)
    assert decide(OFFICIAL, now=utc(14, 0), max_duration=timedelta(minutes=1)).admitted
    with pytest.raises(ValueError, match="provider_id"):
        admit_run_window(
            OFFICIAL,
            provider_id="relay-account-a",
            now=utc(14, 0),
            max_duration=timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="explicit"):
        admit_run_window(
            None, provider_id="relay-account-a", now=utc(14, 0), max_duration=timedelta(minutes=1)
        )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        {"now": datetime(2026, 9, 14)},
        {"max_duration": timedelta(0)},
        {"max_duration": timedelta(minutes=-1)},
        {"max_duration": float("nan")},
        {"queue_wait": timedelta(minutes=-1)},
        {"drain_margin": timedelta(minutes=-1)},
    ],
)
def test_invalid_inputs_fail_closed(change: dict) -> None:
    args = {"now": utc(14, 0), "max_duration": timedelta(minutes=1)}
    args.update(change)
    with pytest.raises(ValueError):
        decide(OFFICIAL, **args)


def test_invalid_schedule_fails_closed() -> None:
    with pytest.raises(ValueError):
        ProviderWindowSchedule("", "v1", (UtcWeeklyWindow(0, 0, 1),))
    with pytest.raises(ValueError):
        ProviderWindowSchedule("relay", "v1", (UtcWeeklyWindow(0, 0, 2), UtcWeeklyWindow(0, 1, 3)))
    with pytest.raises(ValueError):
        UtcWeeklyWindow(0, 60, 60)


def test_no_feasible_start_for_long_episode() -> None:
    decision = decide(OFFICIAL, now=utc(14, 0), max_duration=timedelta(hours=64))
    assert not decision.admitted
    assert decision.next_allowed_start is None
    assert decision.reason == "duration_exceeds_all_windows"


def test_adjacent_half_days_normalize_to_continuous_week():
    schedule = ProviderWindowSchedule(
        "local",
        "split-days",
        tuple(
            UtcWeeklyWindow(day, start, end)
            for day in range(7)
            for start, end in ((0, 720), (720, 1440))
        ),
    )
    decision = admit_run_window(
        schedule,
        provider_id="local",
        now=datetime(2026, 9, 14, tzinfo=UTC),
        max_duration=timedelta(days=20),
    )
    assert decision.admitted
    assert decision.window_end is None
