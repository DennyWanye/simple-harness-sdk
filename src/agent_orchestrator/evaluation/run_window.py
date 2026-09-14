# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pure UTC weekly-window admission for a request or a whole episode.

The caller must supply the actual provider's billing schedule and identity. The
DeepSeek schedule below is a reference for the official API, not a statement
about any relay or proxy. This module neither waits nor starts work. A decision
does not authorize retrying a request whose in-flight outcome is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta


@dataclass(frozen=True, slots=True)
class UtcWeeklyWindow:
    """Allowed UTC minutes on one weekday (Monday=0), with an exclusive end."""

    weekday: int
    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if type(self.weekday) is not int or not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be an integer from 0 to 6")
        if type(self.start_minute) is not int or type(self.end_minute) is not int:
            raise ValueError("window minutes must be integers")
        if not 0 <= self.start_minute < self.end_minute <= 1440:
            raise ValueError("window must fit within one UTC day")


@dataclass(frozen=True, slots=True)
class ProviderWindowSchedule:
    provider_id: str
    schedule_id: str
    allowed: tuple[UtcWeeklyWindow, ...]

    def __post_init__(self) -> None:
        for name in ("provider_id", "schedule_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        if not isinstance(self.allowed, (tuple, list)) or not self.allowed:
            raise ValueError("allowed windows must be a nonempty sequence")
        windows = tuple(self.allowed)
        if not all(isinstance(window, UtcWeeklyWindow) for window in windows):
            raise ValueError("allowed windows must contain UtcWeeklyWindow values")
        windows = tuple(sorted(windows, key=lambda item: (item.weekday, item.start_minute)))
        for previous, current in zip(windows, windows[1:]):
            if previous.weekday == current.weekday and current.start_minute < previous.end_minute:
                raise ValueError("allowed windows may not overlap")
        merged: list[UtcWeeklyWindow] = []
        for window in windows:
            if (
                merged
                and merged[-1].weekday == window.weekday
                and merged[-1].end_minute == window.start_minute
            ):
                previous = merged[-1]
                merged[-1] = UtcWeeklyWindow(
                    window.weekday, previous.start_minute, window.end_minute
                )
            else:
                merged.append(window)
        object.__setattr__(self, "allowed", tuple(merged))


DEEPSEEK_OFFICIAL_PRICING_UTC_2026_09_14 = ProviderWindowSchedule(
    provider_id="deepseek-official-api",
    schedule_id="pricing-2026-09-14-utc",
    allowed=tuple(
        UtcWeeklyWindow(day, start, end)
        for day in range(7)
        for start, end in (((0, 60), (240, 360), (600, 1440)) if day < 5 else ((0, 1440),))
    ),
)


@dataclass(frozen=True, slots=True)
class WindowDecision:
    admitted: bool
    reason: str
    next_allowed_start: datetime | None  # UTC; None means no interval can fit.
    window_end: datetime | None  # UTC; None means continuously allowed.


def _duration(value: timedelta, name: str, *, positive: bool) -> None:
    if not isinstance(value, timedelta):
        raise ValueError(f"{name} must be a timedelta")
    invalid = value <= timedelta(0) if positive else value < timedelta(0)
    if invalid:
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be a {qualifier} timedelta")


def _utc_now(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(UTC)


def _intervals(schedule: ProviderWindowSchedule, now: datetime) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    first_day = now.date() - timedelta(days=1)
    for offset in range(16):
        day = first_day + timedelta(days=offset)
        midnight = datetime.combine(day, time.min, tzinfo=UTC)
        for window in schedule.allowed:
            if window.weekday == day.weekday():
                intervals.append(
                    (
                        midnight + timedelta(minutes=window.start_minute),
                        midnight + timedelta(minutes=window.end_minute),
                    )
                )
    intervals.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if merged and start == merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def admit_run_window(
    schedule: ProviderWindowSchedule,
    *,
    provider_id: str,
    now: datetime,
    max_duration: timedelta,
    queue_wait: timedelta = timedelta(0),
    drain_margin: timedelta = timedelta(0),
) -> WindowDecision:
    """Admit only when queue + execution + drain fit one continuous allowed span.

    Use ``max_duration`` for either a request or a whole episode. A future start
    is advisory only: the caller must re-evaluate at actual admission time.
    Window ends are exclusive; finishing exactly at an end is allowed.
    """

    if not isinstance(schedule, ProviderWindowSchedule):
        raise ValueError("an explicit ProviderWindowSchedule is required")
    if not isinstance(provider_id, str) or provider_id != schedule.provider_id:
        raise ValueError("provider_id must match the explicit billing schedule")
    current = _utc_now(now)
    _duration(max_duration, "max_duration", positive=True)
    _duration(queue_wait, "queue_wait", positive=False)
    _duration(drain_margin, "drain_margin", positive=False)
    try:
        required = max_duration + queue_wait + drain_margin
        _ = current + required  # Validate datetime range before admission.
    except OverflowError as exc:
        raise ValueError("duration or finish is outside the supported datetime range") from exc

    if all(
        any(
            window.weekday == day and window.start_minute == 0 and window.end_minute == 1440
            for window in schedule.allowed
        )
        for day in range(7)
    ):
        return WindowDecision(True, "admitted", current, None)
    if required >= timedelta(days=7):
        return WindowDecision(False, "duration_exceeds_all_windows", None, None)

    try:
        intervals = _intervals(schedule, current)
    except OverflowError as exc:
        raise ValueError("now is outside the supported schedule range") from exc
    inside_window = False
    for start, end in intervals:
        if start <= current < end:
            inside_window = True
        candidate = max(current, start)
        if candidate < end and candidate + required <= end:
            if candidate == current:
                return WindowDecision(True, "admitted", current, end)
            return WindowDecision(
                False,
                "insufficient_window" if inside_window else "outside_window",
                candidate,
                end,
            )
    return WindowDecision(False, "duration_exceeds_all_windows", None, None)
