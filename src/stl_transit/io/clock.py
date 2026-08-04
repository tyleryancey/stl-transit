"""Injectable clock (spec 2.7).

Every time-dependent entry point takes `as_of`. Default is real now. This makes
tests deterministic, makes support repro exact, and lets you ask what the app
showed at 23:47 last Tuesday without a time machine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AGENCY_TZ = ZoneInfo("America/Chicago")


def now_utc(as_of: datetime | None = None) -> datetime:
    if as_of is not None:
        return as_of.astimezone(timezone.utc) if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def now_local(as_of: datetime | None = None, tz: ZoneInfo = AGENCY_TZ) -> datetime:
    return now_utc(as_of).astimezone(tz)


def parse_as_of(value: str | None, tz: ZoneInfo = AGENCY_TZ) -> datetime | None:
    """Parse an --as-of string. Naive input is interpreted in agency-local time."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt
