"""GTFS service-calendar arithmetic and service-day math.

This module and `departures.py` are the reference implementation the Kotlin
engine is graded against. Every non-obvious rule is commented with *why*,
because the comment is the porting instruction.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAY_COLUMNS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_ymd(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y%m%d").date()


def parse_gtfs_time(value: str) -> int:
    """'HH:MM:SS' -> seconds after the service day's noon-minus-12h.

    Hours may exceed 23. '24:12:00' is a 00:12 departure belonging to the
    PREVIOUS service date. This is the single most common source of
    off-by-one-day bugs in transit apps.
    """
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"malformed GTFS time: {value!r}")
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def format_gtfs_time(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def service_day_start(service_date: date, tz: ZoneInfo) -> datetime:
    """The instant GTFS times are measured from, for `service_date`.

    The GTFS spec defines this as noon minus twelve hours, NOT local midnight.
    On a DST transition day those differ by an hour, and using midnight
    silently shifts every departure on the two transition days each year.
    Port this exactly.

    The conversion to UTC before subtracting is load-bearing. Subtracting a
    timedelta from a zone-aware datetime is WALL-CLOCK arithmetic in Python
    (and in java.time's LocalDateTime): the offset is re-derived afterwards, so
    `noon - 12h` collapses back to local 00:00 and the bug this function exists
    to prevent reappears. Doing the arithmetic on the absolute instant is what
    makes it noon-minus-twelve-hours rather than midnight.

    On 2026-03-08 (spring forward) this returns 2026-03-07T23:00-06:00; on
    2026-11-01 (fall back), 2026-11-01T01:00-05:00. On every other day of the
    year it equals local midnight, which is why the error hides so well.
    """
    noon = datetime(service_date.year, service_date.month, service_date.day, 12, 0, 0, tzinfo=tz)
    return (noon.astimezone(timezone.utc) - timedelta(hours=12)).astimezone(tz)


def absolute_time(service_date: date, gtfs_seconds: int, tz: ZoneInfo) -> datetime:
    """Resolve (service_date, gtfs_seconds) to a real instant.

    Offsets are added in UTC for the same reason `service_day_start` converts:
    a departure encoded 25:30:00 on the day before a spring-forward must land
    on the absolute instant 25.5 hours after the service day began, not on the
    wall-clock reading of 01:30 the next day (which does not exist that night).
    """
    start = service_day_start(service_date, tz)
    return (start.astimezone(timezone.utc) + timedelta(seconds=gtfs_seconds)).astimezone(tz)


def active_services(conn: sqlite3.Connection, on: date) -> dict[str, Any]:
    """service_ids running on `on`, showing the calendar base and each
    calendar_dates exception separately rather than pre-merged.

    Keeping the two visible is what makes `stl gtfs calendar` diagnostic: when
    a date behaves oddly you can see whether it was the weekly pattern or an
    exception that did it.
    """
    key = ymd(on)
    weekday = WEEKDAY_COLUMNS[on.weekday()]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    base: set[str] = set()
    if "calendar" in tables:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(calendar)")}
        if weekday in cols:
            sql = (
                f'SELECT service_id FROM calendar WHERE "{weekday}"=\'1\' '
                "AND start_date <= ? AND end_date >= ?"
            )
            base = {r[0] for r in conn.execute(sql, (key, key))}

    added: set[str] = set()
    removed: set[str] = set()
    if "calendar_dates" in tables:
        for row in conn.execute(
            "SELECT service_id, exception_type FROM calendar_dates WHERE date = ?", (key,)
        ):
            (added if str(row[1]).strip() == "1" else removed).add(row[0])

    return {
        "date": on.isoformat(),
        "weekday": weekday,
        "from_calendar": sorted(base),
        "added_by_exception": sorted(added),
        "removed_by_exception": sorted(removed),
        "active": sorted((base | added) - removed),
    }


def candidate_service_dates(window_start: datetime, window_end: datetime) -> list[date]:
    """Service dates that could contribute departures inside a wall-clock window.

    A departure visible at 00:12 may belong to yesterday's service date encoded
    as 24:12:00, so the previous day is always a candidate. Feeds occasionally
    reach past 26:00, so we look two days back to be safe -- cheap, and it
    turns a silent miss into a non-issue.
    """
    first = (window_start.date() - timedelta(days=2))
    last = window_end.date()
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur += timedelta(days=1)
    return out
