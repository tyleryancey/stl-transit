"""The core calculation: scheduled departures at a stop.

This is the app's whole product, computed independently in Python so the Kotlin
implementation has something to be wrong against.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ...errors import StopNotFound
from .calendar import (
    absolute_time,
    active_services,
    candidate_service_dates,
    format_gtfs_time,
    parse_gtfs_time,
    ymd,
)

# pickup_type = 1 means "no pickup available here". A rider cannot board, so it
# is not a departure and must not be shown. Easy to miss; visible to users.
NO_PICKUP = "1"


def resolve_stop(conn: sqlite3.Connection, needle: str) -> dict[str, Any]:
    """Resolve a rider-facing number or an internal id to concrete stop rows.

    Tries stop_code first because that is the number printed on the sign, then
    stop_id. If the match is a station (location_type=1) its child platforms
    are included, since departures are recorded against children.
    """
    needle = needle.strip()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stops)")}
    has_code = "stop_code" in cols
    has_type = "location_type" in cols
    has_parent = "parent_station" in cols

    matched, matched_by = [], ""
    if has_code:
        rows = conn.execute("SELECT * FROM stops WHERE stop_code = ?", (needle,)).fetchall()
        if rows:
            matched, matched_by = [dict(r) for r in rows], "stop_code"
    if not matched:
        rows = conn.execute("SELECT * FROM stops WHERE stop_id = ?", (needle,)).fetchall()
        if rows:
            matched, matched_by = [dict(r) for r in rows], "stop_id"
    if not matched:
        raise StopNotFound(
            f"No stop matches {needle!r}.",
            remedy="Search by name with `stl gtfs stops --search <text>`, or check "
            "`stl gtfs stop-resolve <number>` to see which field holds sign numbers.",
            needle=needle,
        )

    stop_ids: list[str] = []
    for row in matched:
        stop_ids.append(row["stop_id"])
        if has_type and str(row.get("location_type") or "0").strip() == "1" and has_parent:
            children = conn.execute(
                "SELECT stop_id FROM stops WHERE parent_station = ?", (row["stop_id"],)
            ).fetchall()
            stop_ids.extend(c[0] for c in children)

    return {
        "needle": needle,
        "matched_by": matched_by,
        "ambiguous": len(matched) > 1,
        "stops": matched,
        "stop_ids": sorted(set(stop_ids)),
    }


def departures(
    conn: sqlite3.Connection,
    stop: str,
    at: datetime,
    window_minutes: int = 90,
    tz: ZoneInfo = ZoneInfo("America/Chicago"),
    limit: int = 50,
    route_filter: str | None = None,
) -> dict[str, Any]:
    """Scheduled departures from `stop` in [at, at + window_minutes]."""
    resolved = resolve_stop(conn, stop)
    stop_ids = resolved["stop_ids"]
    # Window arithmetic in UTC: adding a timedelta to a zone-aware datetime is
    # wall-clock arithmetic, so a 90-minute window spanning 02:00 on a DST
    # night would otherwise cover 30 or 150 real minutes.
    window_end = (at.astimezone(timezone.utc) + timedelta(minutes=window_minutes)).astimezone(
        at.tzinfo or tz
    )

    st_cols = {r[1] for r in conn.execute("PRAGMA table_info(stop_times)")}
    trip_cols = {r[1] for r in conn.execute("PRAGMA table_info(trips)")}
    has_pickup = "pickup_type" in st_cols
    has_headsign_trip = "trip_headsign" in trip_cols
    has_direction = "direction_id" in trip_cols
    has_st_headsign = "stop_headsign" in st_cols

    placeholders = ", ".join("?" * len(stop_ids))
    select = [
        "st.trip_id AS trip_id",
        "st.departure_time AS departure_time",
        "st.stop_id AS stop_id",
        "st.stop_sequence AS stop_sequence",
        "t.service_id AS service_id",
        "t.route_id AS route_id",
        ("t.trip_headsign AS trip_headsign" if has_headsign_trip else "'' AS trip_headsign"),
        ("t.direction_id AS direction_id" if has_direction else "'' AS direction_id"),
        ("st.stop_headsign AS stop_headsign" if has_st_headsign else "'' AS stop_headsign"),
        ("st.pickup_type AS pickup_type" if has_pickup else "'0' AS pickup_type"),
        "r.route_short_name AS route_short_name",
        "r.route_long_name AS route_long_name",
        "r.route_type AS route_type",
    ]
    sql = (
        f"SELECT {', '.join(select)} FROM stop_times st "
        "JOIN trips t ON t.trip_id = st.trip_id "
        "JOIN routes r ON r.route_id = t.route_id "
        f"WHERE st.stop_id IN ({placeholders}) AND st.departure_time <> ''"
    )

    calendars: dict[str, dict[str, Any]] = {}
    services_by_date: dict[date, set[str]] = {}
    for sd in candidate_service_dates(at, window_end):
        info = active_services(conn, sd)
        calendars[sd.isoformat()] = info
        services_by_date[sd] = set(info["active"])

    all_services = set().union(*services_by_date.values()) if services_by_date else set()
    if not all_services:
        return _empty(resolved, at, window_end, calendars, "No service_ids are active in this window.")

    rows = conn.execute(sql, stop_ids).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        if has_pickup and str(rec.get("pickup_type") or "0").strip() == NO_PICKUP:
            continue  # drop-off only; a rider cannot board here
        service_id = rec["service_id"]
        if service_id not in all_services:
            continue
        if route_filter and route_filter not in (rec["route_id"], rec["route_short_name"]):
            continue
        try:
            secs = parse_gtfs_time(rec["departure_time"])
        except ValueError:
            continue
        for sd, services in services_by_date.items():
            if service_id not in services:
                continue
            when = absolute_time(sd, secs, tz)
            if at <= when <= window_end:
                items.append(
                    {
                        "route_id": rec["route_id"],
                        "route_short_name": rec["route_short_name"],
                        "route_long_name": rec["route_long_name"],
                        "route_type": rec["route_type"],
                        "headsign": rec["stop_headsign"] or rec["trip_headsign"],
                        "direction_id": rec["direction_id"],
                        "trip_id": rec["trip_id"],
                        "stop_id": rec["stop_id"],
                        # Carried because GTFS-RT permits a producer to identify
                        # a StopTimeUpdate by stop_sequence instead of stop_id.
                        # Dropping it made half of the RT matcher dead code.
                        "stop_sequence": rec["stop_sequence"],
                        "service_id": service_id,
                        "service_date": sd.isoformat(),
                        "gtfs_time": format_gtfs_time(secs),
                        "departure_local": when.isoformat(),
                        "departure_utc": when.astimezone(timezone.utc).isoformat(),
                        "minutes_away": int((when - at).total_seconds() // 60),
                        "after_midnight": secs >= 86_400,
                    }
                )

    # Deterministic order (spec 2.8): time, then route, then trip. Never rely
    # on the order SQLite happened to return rows in.
    items.sort(key=lambda d: (d["departure_local"], d["route_short_name"] or "", d["trip_id"]))
    total = len(items)
    return {
        "stop": resolved,
        "query": {
            "at": at.isoformat(),
            "window_minutes": window_minutes,
            "window_end": window_end.isoformat(),
            "timezone": str(tz),
            "route_filter": route_filter,
        },
        "calendars": calendars,
        "total": total,
        "count": min(total, limit),
        "items": items[:limit],
        "has_more": total > limit,
    }


def _empty(resolved, at, window_end, calendars, note) -> dict[str, Any]:
    return {
        "stop": resolved,
        "query": {"at": at.isoformat(), "window_end": window_end.isoformat()},
        "calendars": calendars,
        "total": 0,
        "count": 0,
        "items": [],
        "has_more": False,
        "note": note,
    }


def explain_empty(conn: sqlite3.Connection, stop: str, at: datetime,
                  window_minutes: int, tz: ZoneInfo, feed_end: date | None) -> dict[str, Any]:
    """Walk the decision tree for 'nothing showed at this stop' and name the branch."""
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    try:
        resolved = resolve_stop(conn, stop)
        add("stop_resolves", True, f"Matched by {resolved['matched_by']}; "
                                   f"{len(resolved['stop_ids'])} platform id(s).")
    except StopNotFound as exc:
        add("stop_resolves", False, exc.message)
        return {"verdict": "STOP_NOT_FOUND", "checks": checks,
                "remedy": "The number does not exist in this feed. It may have been "
                          "retired at a service change -- compare against an older "
                          "snapshot with `stl diff stop-ids`."}

    if feed_end and at.date() > feed_end:
        add("feed_covers_date", False,
            f"Queried {at.date()} but the feed's service data ends {feed_end}.")
        return {"verdict": "FEED_EXPIRED", "checks": checks,
                "remedy": "Re-fetch the feed: `stl snapshot fetch metro_gtfs`. Metro "
                          "publishes a feed that ends at the next quarterly pick."}
    add("feed_covers_date", True, f"Feed covers {at.date()}." if feed_end else "No feed_info end date.")

    cal = active_services(conn, at.date())
    if not cal["active"]:
        add("services_active", False, f"No service_ids active on {at.date()}.")
        return {"verdict": "NO_SERVICE_THAT_DAY", "checks": checks,
                "remedy": "Check `stl gtfs calendar --date` -- the date may be "
                          "removed by a calendar_dates exception."}
    add("services_active", True, f"{len(cal['active'])} service_id(s) active.")

    stop_ids = resolved["stop_ids"]
    ph = ", ".join("?" * len(stop_ids))
    ever = conn.execute(
        f"SELECT COUNT(*) FROM stop_times WHERE stop_id IN ({ph})", stop_ids
    ).fetchone()[0]
    if ever == 0:
        add("stop_has_any_service", False, "Stop exists but appears in zero stop_times rows.")
        return {"verdict": "STOP_NEVER_SERVED", "checks": checks,
                "remedy": "The stop record survives in the feed but no trip calls at it. "
                          "Usually a retired stop awaiting cleanup."}
    add("stop_has_any_service", True, f"{ever} stop_times rows reference this stop.")

    wide = departures(conn, stop, at.replace(hour=0, minute=0, second=0),
                      window_minutes=24 * 60, tz=tz, limit=500)
    if wide["total"] == 0:
        add("service_on_this_date", False, "No departures anywhere in the 24h day.")
        return {"verdict": "NO_SERVICE_AT_STOP_TODAY", "checks": checks,
                "remedy": "The stop is served, but not on this day of the week. "
                          "Check weekend/holiday patterns."}
    add("service_on_this_date", True, f"{wide['total']} departures across the full day.")
    return {"verdict": "WINDOW_TOO_NARROW", "checks": checks,
            "remedy": f"Service exists ({wide['total']} departures today) but none fall "
                      f"inside the {window_minutes}-minute window from {at.time()}. "
                      "Widen the window or check the first/last departure times."}
