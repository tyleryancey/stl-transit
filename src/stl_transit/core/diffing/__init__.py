"""Snapshot comparison -- what moved between two GTFS feeds.

Metro republishes the feed at every quarterly pick, so "did anything change" is
always yes. The question worth asking is whether anything changed that breaks an
assumption the app is built on, and every result here is shaped to answer that
rather than to dump a delta.

The two snapshots are two separate SQLite files. Every comparison therefore
pulls id sets into Python and does the set arithmetic there: a cross-database
join would need ATTACH, which the read-only connection denies at the driver
(spec 9), and the largest set involved is ~5,100 stops.

Pure logic: never prints, never exits, never prompts (spec 2.1). `drift_detected`
is returned as a boolean; mapping it to exit code 4 is the CLI's job.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from datetime import date
from typing import Any

from ...errors import UsageError
from ..gtfs.calendar import WEEKDAY_COLUMNS, active_services, parse_ymd
from ..gtfs.inspect import OPTIONAL_FILES, _columns, _tables
from ..models import paginate

# Samples embedded in results that take no limit/offset. Two snapshots can
# differ by thousands of stops and an MCP client is a context window (spec 2.4).
SAMPLE_LIMIT = 20

# `summary` is meant to be cheap enough to call routinely, so it embeds far less.
SUMMARY_SAMPLE_LIMIT = 5

# The `stop_ids_stable` assumption (spec 6.10). Below this, users' saved stops
# start vanishing and the saved-stops feature stops being trustworthy.
STOP_CODE_SURVIVAL_FLOOR = 0.98

# IUGG mean Earth radius. One degree of latitude is R*pi/180 = 111,195 m.
EARTH_RADIUS_M = 6_371_008.8

ROUTE_FIELDS = ("route_short_name", "route_long_name", "route_type")
CALENDAR_FIELDS = (*WEEKDAY_COLUMNS, "start_date", "end_date")

# route_types that mean rail. Retiring one of these is the `rail_route_ids_stable`
# assumption breaking, which is a different order of problem from a bus renumber.
RAIL_ROUTE_TYPES = {"0", "1", "2", "5", "7", "12"}

SEVERITY_RANK = {"alarming": 0, "notable": 1, "opportunity": 2, "routine": 3}


# ------------------------------------------------------------------ helpers --

def haversine_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Great-circle distance in metres between two stop positions.

    Comparing raw degree deltas is wrong at this latitude. At 38.6 N one degree
    of longitude spans ~86.9 km against ~111.2 km for one degree of latitude, so
    the same degree delta is 22% fewer metres east-west than north-south. A
    threshold expressed in degrees and calibrated on latitude would flag a stop
    shifted 26 m north but ignore the same stop shifted 32 m east -- and Metro's
    street grid runs east-west, which is precisely where the error would land.
    The threshold is in metres because metres is the unit the question is asked
    in: "did this stop move far enough that a rider would walk to the wrong pole".
    """
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lon_b - lon_a)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def _select_rows(conn: sqlite3.Connection, table: str,
                 columns: tuple[str, ...]) -> list[dict[str, str]]:
    """Fetch `columns` from `table`, tolerating a feed that has neither.

    A table (or a column) present in one snapshot and absent from the other is a
    finding, not a crash: it is exactly what `diff files` exists to report.
    Absent columns read back as empty strings, matching the all-TEXT import.
    """
    if table not in _tables(conn):
        return []
    have = set(_columns(conn, table))
    present = [c for c in columns if c in have]
    if not present:
        return []
    quoted = ", ".join(f'"{c}"' for c in present)
    out = []
    for row in conn.execute(f'SELECT {quoted} FROM "{table}"'):
        rec = {c: "" for c in columns}
        for i, col in enumerate(present):
            rec[col] = row[i] if row[i] is not None else ""
        out.append(rec)
    return out


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in sorted(_tables(conn))}


def _section(rows: list[Any], offset: int, limit: int) -> dict[str, Any]:
    """One diff list plus the pagination block every list result carries."""
    window, meta = paginate(rows, offset, limit)
    return {"items": window, **meta}


def _bag(ordered: list[Any], limit: int) -> dict[str, Any]:
    """A count plus a bounded sample of what is behind it.

    For results whose signature takes no limit/offset. `ordered` must already be
    sorted -- the sample has to be the same sample on every run (spec 2.8).
    """
    return {"count": len(ordered), "sample": ordered[:limit],
            "truncated": len(ordered) > limit}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _field_changes(a: dict[str, str], b: dict[str, str],
                   fields: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"field": f, "a": a.get(f, ""), "b": b.get(f, "")}
            for f in fields if a.get(f, "") != b.get(f, "")]


def _coord(rec: dict[str, str]) -> tuple[float, float] | None:
    try:
        return float(rec["stop_lat"]), float(rec["stop_lon"])
    except (TypeError, ValueError):
        return None


def _service_window(conn: sqlite3.Connection) -> tuple[date | None, date | None]:
    """Earliest and latest service date the feed covers.

    Deliberately not `inspect.coverage`: that takes a `today` and reports
    days-to-expiry, which would make a comparison of two static files depend on
    when it was run (spec 2.8).
    """
    tables = _tables(conn)
    starts: list[date] = []
    ends: list[date] = []
    for table, lo, hi in (("calendar", "start_date", "end_date"),
                          ("calendar_dates", "date", "date")):
        if table not in tables:
            continue
        cols = set(_columns(conn, table))
        if lo not in cols or hi not in cols:
            continue
        row = conn.execute(f'SELECT MIN("{lo}"), MAX("{hi}") FROM "{table}"').fetchone()
        if not row or not row[0]:
            continue
        try:
            starts.append(parse_ymd(row[0]))
            ends.append(parse_ymd(row[1]))
        except ValueError:
            continue  # a malformed date is a validator's problem, not a diff's
    return (min(starts) if starts else None), (max(ends) if ends else None)


def _shift_days(a: date | None, b: date | None) -> int | None:
    return (b - a).days if a and b else None


# ------------------------------------------------------------------- files --

def files(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection) -> dict[str, Any]:
    """Tables added/removed between two feeds, and row-count deltas per table.

    No limit/offset: the list is one row per GTFS file, and a GTFS feed tops out
    around twenty of them. Bounded by the format rather than by a cap.
    """
    counts_a, counts_b = _row_counts(conn_a), _row_counts(conn_b)
    rows = []
    for name in sorted(set(counts_a) | set(counts_b)):
        a, b = counts_a.get(name), counts_b.get(name)
        if a is None:
            status = "added"
        elif b is None:
            status = "removed"
        else:
            status = "unchanged" if a == b else "changed"
        rows.append(
            {
                "table": name,
                "file": f"{name}.txt",
                "rows_a": a,
                "rows_b": b,
                "delta": (b - a) if (a is not None and b is not None) else None,
                "pct_change": (round((b - a) / a, 4)
                               if (a and b is not None) else None),
                "status": status,
            }
        )

    def presence(names: list[str], counts: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"table": n, "file": f"{n}.txt", "rows": counts[n],
             # The optional-file catalogue already says what each file's presence
             # costs or buys the app; a diff that omits it makes the reader look
             # it up elsewhere.
             "why_it_matters": OPTIONAL_FILES.get(f"{n}.txt", "")}
            for n in names
        ]

    added = sorted(set(counts_b) - set(counts_a))
    removed = sorted(set(counts_a) - set(counts_b))
    return {
        "tables_added": presence(added, counts_b),
        "tables_removed": presence(removed, counts_a),
        "tables_common": sorted(set(counts_a) & set(counts_b)),
        "rows": rows,
        "totals": {
            "tables_a": len(counts_a),
            "tables_b": len(counts_b),
            "rows_a": sum(counts_a.values()),
            "rows_b": sum(counts_b.values()),
            "delta": sum(counts_b.values()) - sum(counts_a.values()),
        },
        "drift_detected": any(r["status"] != "unchanged" for r in rows),
    }


# ------------------------------------------------------------------ routes --

def routes(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection,
           limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """route_ids added/removed; short_name/long_name/type changes on survivors."""
    keys = ("route_id", *ROUTE_FIELDS)
    a = {r["route_id"]: r for r in _select_rows(conn_a, "routes", keys)}
    b = {r["route_id"]: r for r in _select_rows(conn_b, "routes", keys)}

    added = [b[i] for i in sorted(set(b) - set(a))]
    removed = [a[i] for i in sorted(set(a) - set(b))]
    changed = []
    for route_id in sorted(set(a) & set(b)):
        diffs = _field_changes(a[route_id], b[route_id], ROUTE_FIELDS)
        if diffs:
            changed.append(
                {
                    "route_id": route_id,
                    "route_short_name": b[route_id].get("route_short_name", ""),
                    "changes": diffs,
                    # A rename is routine; a route_type change means the app's
                    # bus/rail special-casing now applies to a different set.
                    "type_changed": any(d["field"] == "route_type" for d in diffs),
                }
            )
    return {
        "table_present": {"a": "routes" in _tables(conn_a), "b": "routes" in _tables(conn_b)},
        "added": _section(added, offset, limit),
        "removed": _section(removed, offset, limit),
        "changed": _section(changed, offset, limit),
        "counts": {
            "a": len(a), "b": len(b),
            "added": len(added), "removed": len(removed),
            "changed": len(changed), "unchanged": len(set(a) & set(b)) - len(changed),
        },
        "drift_detected": bool(added or removed or changed),
    }


# ------------------------------------------------------------------- stops --

STOP_FIELDS = ("stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon")


def stops(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection,
          moved_threshold_m: float = 25.0, limit: int = 50,
          offset: int = 0) -> dict[str, Any]:
    """Stops added/removed/renamed/moved.

    `renamed` and `moved` are independent facts about the same stop_id, so a
    stop that was both renamed and relocated appears in both lists.
    """
    a = {r["stop_id"]: r for r in _select_rows(conn_a, "stops", STOP_FIELDS)}
    b = {r["stop_id"]: r for r in _select_rows(conn_b, "stops", STOP_FIELDS)}

    def brief(r: dict[str, str]) -> dict[str, str]:
        return {k: r.get(k, "") for k in STOP_FIELDS}

    added = [brief(b[i]) for i in sorted(set(b) - set(a))]
    removed = [brief(a[i]) for i in sorted(set(a) - set(b))]

    renamed: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    no_coords: list[str] = []
    for stop_id in sorted(set(a) & set(b)):
        before, after = a[stop_id], b[stop_id]
        if before.get("stop_name", "") != after.get("stop_name", ""):
            renamed.append(
                {"stop_id": stop_id, "stop_code": after.get("stop_code", ""),
                 "name_a": before.get("stop_name", ""), "name_b": after.get("stop_name", "")}
            )
        pa, pb = _coord(before), _coord(after)
        if pa is None or pb is None:
            no_coords.append(stop_id)
            continue
        distance = haversine_m(pa[0], pa[1], pb[0], pb[1])
        if distance > moved_threshold_m:
            moved.append(
                {
                    "stop_id": stop_id,
                    "stop_code": after.get("stop_code", ""),
                    "stop_name": after.get("stop_name", ""),
                    # Fixed precision: two runs against the same pair of
                    # snapshots must produce byte-identical JSON (spec 2.8).
                    "moved_m": round(distance, 1),
                    "from": {"stop_lat": pa[0], "stop_lon": pa[1]},
                    "to": {"stop_lat": pb[0], "stop_lon": pb[1]},
                }
            )
    # Largest displacement first: the reader wants the worst case, not stop S1.
    moved.sort(key=lambda d: (-d["moved_m"], d["stop_id"]))

    return {
        "moved_threshold_m": moved_threshold_m,
        "distance": "haversine over stop_lat/stop_lon, metres",
        "added": _section(added, offset, limit),
        "removed": _section(removed, offset, limit),
        "renamed": _section(renamed, offset, limit),
        "moved": _section(moved, offset, limit),
        "counts": {
            "a": len(a), "b": len(b),
            "added": len(added), "removed": len(removed),
            "renamed": len(renamed), "moved": len(moved),
            "survived": len(set(a) & set(b)),
            "coordinates_missing": len(no_coords),
        },
        "coordinates_missing_sample": _bag(no_coords, SAMPLE_LIMIT),
        "drift_detected": bool(added or removed or renamed or moved),
    }


# ---------------------------------------------------------------- stop ids --

def stop_ids(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection) -> dict[str, Any]:
    """Survival of stop_id AND stop_code across a service change.

    The app's saved-stops feature lives or dies on this number: a rider saves the
    number printed on the pole, and if that number is reassigned or retired at
    the next pick their saved stop silently points somewhere else or nowhere.
    Reported as a rate with the raw counts beside it, because "97.9%" and "108
    codes retired" are the same fact and only one of them tells you how many
    people it happens to.
    """
    return _stop_ids(conn_a, conn_b, SAMPLE_LIMIT)


def _stop_ids(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection,
              sample_limit: int) -> dict[str, Any]:
    rows_a = _select_rows(conn_a, "stops", STOP_FIELDS)
    rows_b = _select_rows(conn_b, "stops", STOP_FIELDS)
    has_code = {
        "a": "stops" in _tables(conn_a) and "stop_code" in set(_columns(conn_a, "stops")),
        "b": "stops" in _tables(conn_b) and "stop_code" in set(_columns(conn_b, "stops")),
    }

    def block(field: str) -> dict[str, Any]:
        # Empty values are not identifiers. Counting them would inflate the
        # denominator with stops that never had a number to survive.
        names_a = {r[field]: r.get("stop_name", "") for r in rows_a if r.get(field)}
        names_b = {r[field]: r.get("stop_name", "") for r in rows_b if r.get(field)}
        keys_a, keys_b = set(names_a), set(names_b)
        lost = sorted(keys_a - keys_b)
        gained = sorted(keys_b - keys_a)
        survived = len(keys_a & keys_b)
        return {
            "in_a": len(keys_a),
            "in_b": len(keys_b),
            "survived": survived,
            "lost": len(lost),
            "gained": len(gained),
            # Denominator is A, not the union: the question is what share of the
            # identifiers a rider could already have saved still resolve.
            "survival_rate": _rate(survived, len(keys_a)),
            "lost_sample": [{field: k, "stop_name": names_a[k]} for k in lost[:sample_limit]],
            "gained_sample": [{field: k, "stop_name": names_b[k]} for k in gained[:sample_limit]],
            "sample_truncated": len(lost) > sample_limit or len(gained) > sample_limit,
        }

    by_id, by_code = block("stop_id"), block("stop_code")
    id_rate, code_rate = by_id["survival_rate"], by_code["survival_rate"]
    worst = min([r for r in (id_rate, code_rate) if r is not None], default=None)
    meets = worst is None or worst >= STOP_CODE_SURVIVAL_FLOOR

    if not has_code["a"] or not has_code["b"]:
        verdict = ("One of these feeds has no stop_code column at all. The Stop ID "
                   "entry screen has nothing to resolve against in that snapshot.")
    elif meets and worst == 1.0:
        verdict = "Every stop_id and stop_code in the older feed still resolves."
    elif meets:
        verdict = (f"Routine: {_pct(code_rate)} of stop_codes and {_pct(id_rate)} of "
                   f"stop_ids survive, clearing the "
                   f"{_pct(STOP_CODE_SURVIVAL_FLOOR)} floor the saved-stops feature assumes.")
    else:
        verdict = (f"ALARMING: {_pct(code_rate)} of stop_codes and {_pct(id_rate)} of "
                   f"stop_ids survive, below the {_pct(STOP_CODE_SURVIVAL_FLOOR)} floor. "
                   f"{by_code['lost']} code(s) and {by_id['lost']} id(s) retired -- every "
                   "saved stop pointing at one of them breaks at this pick.")

    return {
        "survival_rate_stop_id": id_rate,
        "survival_rate_stop_code": code_rate,
        "stop_id": by_id,
        "stop_code": by_code,
        "stop_code_column_present": has_code,
        "floor": STOP_CODE_SURVIVAL_FLOOR,
        "meets_assumption": meets,
        "assumption": "stop_ids_stable (spec 6.10)",
        "verdict": verdict,
        "drift_detected": bool(by_id["lost"] or by_id["gained"]
                               or by_code["lost"] or by_code["gained"]),
    }


# ---------------------------------------------------------------- calendar --

def calendar(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection) -> dict[str, Any]:
    """service_id churn and date-range shifts between the two feeds."""
    return _calendar(conn_a, conn_b, SAMPLE_LIMIT)


def _calendar(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection,
              sample_limit: int) -> dict[str, Any]:
    keys = ("service_id", *CALENDAR_FIELDS)
    cal_a = {r["service_id"]: r for r in _select_rows(conn_a, "calendar", keys)}
    cal_b = {r["service_id"]: r for r in _select_rows(conn_b, "calendar", keys)}

    exc_keys = ("service_id", "date", "exception_type")
    exc_a = {tuple(r[k] for k in exc_keys) for r in _select_rows(conn_a, "calendar_dates", exc_keys)}
    exc_b = {tuple(r[k] for k in exc_keys) for r in _select_rows(conn_b, "calendar_dates", exc_keys)}

    # service_ids live in calendar.txt, calendar_dates.txt, or both.
    ids_a = set(cal_a) | {e[0] for e in exc_a}
    ids_b = set(cal_b) | {e[0] for e in exc_b}

    changed = []
    for service_id in sorted(set(cal_a) & set(cal_b)):
        diffs = _field_changes(cal_a[service_id], cal_b[service_id], CALENDAR_FIELDS)
        if diffs:
            changed.append({"service_id": service_id, "changes": diffs})

    start_a, end_a = _service_window(conn_a)
    start_b, end_b = _service_window(conn_b)

    def exc_rows(entries: set[tuple[str, ...]]) -> list[dict[str, str]]:
        return [dict(zip(exc_keys, e)) for e in sorted(entries)]

    return {
        "service_ids": {
            "added": _bag(sorted(ids_b - ids_a), sample_limit),
            "removed": _bag(sorted(ids_a - ids_b), sample_limit),
            "counts": {"a": len(ids_a), "b": len(ids_b),
                       "common": len(ids_a & ids_b), "changed": len(changed)},
        },
        "changed": _bag(changed, sample_limit),
        "date_range": {
            "a": {"start": start_a.isoformat() if start_a else None,
                  "end": end_a.isoformat() if end_a else None},
            "b": {"start": start_b.isoformat() if start_b else None,
                  "end": end_b.isoformat() if end_b else None},
            "start_shift_days": _shift_days(start_a, start_b),
            "end_shift_days": _shift_days(end_a, end_b),
        },
        "exceptions": {
            "count_a": len(exc_a),
            "count_b": len(exc_b),
            "added": _bag(exc_rows(exc_b - exc_a), sample_limit),
            "removed": _bag(exc_rows(exc_a - exc_b), sample_limit),
        },
        "drift_detected": bool(
            ids_a != ids_b or changed or exc_a != exc_b
            or (start_a, end_a) != (start_b, end_b)
        ),
    }


# ---------------------------------------------------------------- schedule --

def _stop_ids_for(conn: sqlite3.Connection, needle: str) -> list[str]:
    """Stop ids matching a rider-facing code or an internal id, or [].

    Deliberately not `departures.resolve_stop`: that raises StopNotFound, and a
    stop present in one snapshot and gone from the other is the ANSWER a diff is
    looking for, not an error in it. Station children are included for the same
    reason resolve_stop includes them -- departures are recorded against the
    platform rows, not the parent.
    """
    if "stops" not in _tables(conn):
        return []
    cols = set(_columns(conn, "stops"))
    matched: list[str] = []
    if "stop_code" in cols:
        matched = [r[0] for r in conn.execute(
            "SELECT stop_id FROM stops WHERE stop_code = ?", (needle,))]
    if not matched:
        matched = [r[0] for r in conn.execute(
            "SELECT stop_id FROM stops WHERE stop_id = ?", (needle,))]
    out = set(matched)
    if matched and "parent_station" in cols:
        ph = ", ".join("?" * len(matched))
        out |= {r[0] for r in conn.execute(
            f"SELECT stop_id FROM stops WHERE parent_station IN ({ph})", matched)}
    return sorted(out)


def _calls_at_stop(conn: sqlite3.Connection, stop_ids_: list[str], route_id: str | None,
                   services: set[str] | None) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """Every scheduled call at these stops, as (key, record) pairs."""
    tables = _tables(conn)
    if not stop_ids_ or "stop_times" not in tables or "trips" not in tables:
        return []
    trip_cols = set(_columns(conn, "trips"))
    st_cols = set(_columns(conn, "stop_times"))
    has_dir = "direction_id" in trip_cols
    has_head = "trip_headsign" in trip_cols
    has_routes = "routes" in tables
    select = [
        "st.departure_time", "t.route_id",
        "t.direction_id" if has_dir else "''",
        "t.trip_headsign" if has_head else "''",
        "t.service_id", "st.stop_id", "t.trip_id",
        "r.route_short_name" if has_routes else "''",
        "st.pickup_type" if "pickup_type" in st_cols else "'0'",
    ]
    # LEFT JOIN: a trip whose route_id is missing from routes.txt is a broken
    # feed, but dropping the row would hide the departure the diff is about.
    join = "LEFT JOIN routes r ON r.route_id = t.route_id " if has_routes else ""
    ph = ", ".join("?" * len(stop_ids_))
    sql = (f"SELECT {', '.join(select)} FROM stop_times st "
           "JOIN trips t ON t.trip_id = st.trip_id " + join
           + f"WHERE st.stop_id IN ({ph}) AND st.departure_time <> ''")
    params: list[str] = list(stop_ids_)
    if route_id:
        sql += " AND t.route_id = ?"
        params.append(route_id)

    out = []
    for row in conn.execute(sql, params):
        if services is not None and row[4] not in services:
            continue
        rec = {
            "departure_time": row[0], "route_id": row[1],
            "direction_id": row[2] or "", "headsign": row[3] or "",
            "service_id": row[4], "stop_id": row[5], "trip_id": row[6],
            "route_short_name": row[7] or "", "pickup_type": row[8] or "0",
        }
        key = (rec["departure_time"], rec["route_id"], rec["direction_id"], rec["stop_id"])
        out.append((key, rec))
    return out


def _trips_on_route(conn: sqlite3.Connection, route_id: str,
                    services: set[str] | None) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """Every trip on a route, summarised, as (key, record) pairs."""
    tables = _tables(conn)
    if "trips" not in tables or "stop_times" not in tables:
        return []
    trip_cols = set(_columns(conn, "trips"))
    dir_expr = "t.direction_id" if "direction_id" in trip_cols else "''"
    head_expr = "t.trip_headsign" if "trip_headsign" in trip_cols else "''"
    # MIN/MAX over the TEXT times is correct because GTFS times are zero-padded
    # HH:MM:SS, so lexical order is chronological -- including 24:12 > 23:59.
    sql = (
        f"SELECT t.trip_id, {dir_expr}, {head_expr}, t.service_id, "
        "MIN(st.departure_time), MAX(st.departure_time), COUNT(*) "
        "FROM trips t JOIN stop_times st ON st.trip_id = t.trip_id "
        "WHERE t.route_id = ? AND st.departure_time <> '' "
        f"GROUP BY t.trip_id, {dir_expr}, {head_expr}, t.service_id"
    )
    out = []
    for row in conn.execute(sql, (route_id,)):
        if services is not None and row[3] not in services:
            continue
        rec = {
            "trip_id": row[0], "direction_id": row[1] or "", "headsign": row[2] or "",
            "service_id": row[3], "first_departure": row[4], "last_departure": row[5],
            "calls": row[6],
        }
        # Keyed on (first departure, direction), NOT trip_id or service_id: both
        # are regenerated at every pick, so keying on them reports 100% churn on
        # a timetable that did not move a minute.
        out.append(((rec["first_departure"], rec["direction_id"]), rec))
    return out


def _representatives(pairs: list[tuple[tuple[str, ...], dict[str, Any]]],
                     ) -> dict[tuple[str, ...], dict[str, Any]]:
    """One record per key, chosen deterministically.

    A key can legitimately repeat -- two trips leave the same stop at the same
    minute on the same route more often than you would think. Taking whichever
    row SQLite happened to return last would make the displayed record depend on
    storage order, and two runs would disagree (spec 2.8).
    """
    out: dict[tuple[str, ...], dict[str, Any]] = {}
    for key, rec in sorted(pairs, key=lambda p: (p[0], str(p[1].get("trip_id", "")))):
        out.setdefault(key, rec)
    return out


def schedule(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection,
             stop: str | None = None, route_id: str | None = None,
             on: date | None = None, limit: int = 50) -> dict[str, Any]:
    """Timetable deltas for one stop or route on a date."""
    if not stop and not route_id:
        raise UsageError(
            "A schedule diff needs a stop or a route to compare.",
            remedy="Add one: `stl diff schedule <a> <b> --stop 15111 --date 2026-08-05`, "
                   "or `--route MLR`. Unfiltered, this is every stop_times row in both "
                   "feeds -- roughly a million each -- so it is refused rather than "
                   "silently truncated.",
            stop=stop,
            route_id=route_id,
        )

    notes: list[str] = []
    services_a = services_b = None
    active: dict[str, list[str]] = {}
    if on is not None:
        active = {"a": active_services(conn_a, on)["active"],
                  "b": active_services(conn_b, on)["active"]}
        services_a, services_b = set(active["a"]), set(active["b"])
        if not services_a or not services_b:
            notes.append(f"No service is active on {on.isoformat()} in "
                         f"{'feed A' if not services_a else 'feed B'}; that side is empty "
                         "by definition, not by deletion.")
    else:
        notes.append("No date given, so every service_id in each feed is pooled. Across a "
                     "pick boundary that over-reports: pass a date both feeds cover.")

    mode = "stop" if stop else "route"
    ids: dict[str, list[str]] = {}
    if mode == "stop":
        ids = {"a": _stop_ids_for(conn_a, stop or ""), "b": _stop_ids_for(conn_b, stop or "")}
        pairs_a = _calls_at_stop(conn_a, ids["a"], route_id, services_a)
        pairs_b = _calls_at_stop(conn_b, ids["b"], route_id, services_b)
        key_desc = ("(departure_time, route_id, direction_id, stop_id) -- trip_id is "
                    "excluded because it is regenerated at every pick")
        fields = ("departure_time", "route_id", "route_short_name", "direction_id", "stop_id")
        # pickup_type flipping to 1 is why a stop that still exists shows nothing:
        # the trip still calls there, but a rider can no longer board.
        compared = ("headsign", "pickup_type")
        if not ids["a"] or not ids["b"]:
            notes.append(f"Stop {stop!r} resolves in "
                         f"{'feed A only' if ids['a'] else 'feed B only' if ids['b'] else 'neither feed'}. "
                         "Check `stl diff stop-ids` before reading the timetable delta.")
    else:
        pairs_a = _trips_on_route(conn_a, route_id or "", services_a)
        pairs_b = _trips_on_route(conn_b, route_id or "", services_b)
        key_desc = ("(first_departure, direction_id) per trip -- trip_id and service_id "
                    "are excluded because both are regenerated at every pick")
        fields = ("first_departure", "last_departure", "direction_id", "calls")
        compared = ("headsign", "last_departure", "calls")

    counts_a: Counter = Counter(k for k, _ in pairs_a)
    counts_b: Counter = Counter(k for k, _ in pairs_b)
    rec_a, rec_b = _representatives(pairs_a), _representatives(pairs_b)

    def emit(rec: dict[str, Any], n: int) -> dict[str, Any]:
        out = {f: rec.get(f, "") for f in fields}
        out["headsign"] = rec.get("headsign", "")
        out["trips"] = n
        return out

    only_b = sorted((counts_b - counts_a).items())
    only_a = sorted((counts_a - counts_b).items())
    added = [emit(rec_b[k], n) for k, n in only_b]
    removed = [emit(rec_a[k], n) for k, n in only_a]

    # Same slot in the timetable, different detail: a new sign on the front of
    # the bus, or a call that quietly stopped accepting boardings. Kept out of
    # added/removed because a reader reads those as "the bus moved".
    changed = []
    for key in sorted(set(counts_a) & set(counts_b)):
        diffs = _field_changes(rec_a[key], rec_b[key], compared)
        if diffs:
            changed.append({**{f: rec_b[key].get(f, "") for f in fields}, "changes": diffs})

    return {
        "mode": mode,
        "filter": {"stop": stop, "route_id": route_id,
                   "date": on.isoformat() if on else None},
        "key": key_desc,
        "stop_ids": ids or None,
        "services_active": active or None,
        # `offset` is not in this signature: a timetable for one stop on one date
        # is tens of rows, and the caller filters by stop or route instead.
        "added": _section(added, 0, limit),
        "removed": _section(removed, 0, limit),
        "changed": _section(changed, 0, limit),
        "compared_fields": list(compared),
        "counts": {
            "a": len(pairs_a), "b": len(pairs_b),
            "added": sum(n for _, n in only_b),
            "removed": sum(n for _, n in only_a),
            "common": len(set(counts_a) & set(counts_b)),
            "changed": len(changed),
        },
        "notes": notes,
        "drift_detected": bool(added or removed or changed),
    }


# ----------------------------------------------------------------- summary --

def summary(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection) -> dict[str, Any]:
    """One-screen digest across every dimension above.

    Cheap enough to call routinely: it embeds SUMMARY_SAMPLE_LIMIT examples per
    dimension and nothing more. Call the per-dimension functions when you need
    the full lists. `headline` is the one line a human reads; `findings` is the
    part that says whether the change is alarming or routine, because a pick that
    renames three headsigns and a pick that retires 400 stop_codes produce the
    same shape of diff and very different Mondays.
    """
    f = files(conn_a, conn_b)
    r = routes(conn_a, conn_b, limit=SUMMARY_SAMPLE_LIMIT)
    s = stops(conn_a, conn_b, limit=SUMMARY_SAMPLE_LIMIT)
    ids = _stop_ids(conn_a, conn_b, SUMMARY_SAMPLE_LIMIT)
    c = _calendar(conn_a, conn_b, SUMMARY_SAMPLE_LIMIT)

    findings: list[dict[str, str]] = []

    def note(severity: str, dimension: str, detail: str) -> None:
        findings.append({"severity": severity, "dimension": dimension, "detail": detail})

    for t in f["tables_added"]:
        severity = "alarming" if t["table"] == "frequencies" else (
            "opportunity" if t["table"].startswith("fare_") else "notable")
        note(severity, "files", f"{t['file']} appeared ({t['rows']} rows). "
                                f"{t['why_it_matters']}".strip())
    for t in f["tables_removed"]:
        note("alarming" if t["table"] in {"stops", "trips", "stop_times", "calendar"} else "notable",
             "files", f"{t['file']} disappeared (was {t['rows']} rows). "
                      f"{t['why_it_matters']}".strip())

    code_rate = ids["survival_rate_stop_code"]
    id_rate = ids["survival_rate_stop_id"]
    if not ids["meets_assumption"]:
        note("alarming", "stop_ids", ids["verdict"])
    elif ids["stop_code"]["lost"] or ids["stop_id"]["lost"]:
        note("routine", "stop_ids",
             f"{ids['stop_code']['lost']} stop_code(s) and {ids['stop_id']['lost']} stop_id(s) "
             f"retired; survival {_pct(code_rate)}/{_pct(id_rate)} clears the "
             f"{_pct(STOP_CODE_SURVIVAL_FLOOR)} floor.")

    rail_gone = [x for x in r["removed"]["items"] if x.get("route_type") in RAIL_ROUTE_TYPES]
    if rail_gone:
        note("alarming", "routes",
             f"{len(rail_gone)} rail route_id(s) removed "
             f"({', '.join(x['route_id'] for x in rail_gone)}). The app's rail "
             "special-casing keys on these -- assumption rail_route_ids_stable.")
    elif r["counts"]["removed"]:
        note("notable", "routes", f"{r['counts']['removed']} route_id(s) removed.")
    if r["counts"]["added"]:
        note("notable", "routes", f"{r['counts']['added']} route_id(s) added.")
    type_changes = [x for x in r["changed"]["items"] if x.get("type_changed")]
    if type_changes:
        note("alarming", "routes",
             f"{len(type_changes)} route(s) changed route_type; bus/rail handling now "
             "applies to a different set.")
    elif r["counts"]["changed"]:
        note("routine", "routes",
             f"{r['counts']['changed']} route(s) renamed. Renames are what a pick does.")

    if s["counts"]["moved"]:
        share = _rate(s["counts"]["moved"], s["counts"]["survived"]) or 0
        note("notable" if share > 0.01 else "routine", "stops",
             f"{s['counts']['moved']} stop(s) moved more than "
             f"{s['moved_threshold_m']:g} m; furthest "
             f"{s['moved']['items'][0]['moved_m']:g} m.")

    end_shift = c["date_range"]["end_shift_days"]
    if end_shift is not None and end_shift > 0:
        note("routine", "calendar",
             f"Service window ends {end_shift} day(s) later: {c['date_range']['a']['end']} "
             f"-> {c['date_range']['b']['end']}. That is a pick.")
    elif end_shift is not None and end_shift < 0:
        note("alarming", "calendar",
             f"Service window ends {abs(end_shift)} day(s) EARLIER than the older feed "
             f"({c['date_range']['a']['end']} -> {c['date_range']['b']['end']}). "
             "The newer snapshot covers less than the one it replaces.")
    if c["service_ids"]["removed"]["count"] or c["service_ids"]["added"]["count"]:
        note("routine", "calendar",
             f"{c['service_ids']['removed']['count']} service_id(s) removed, "
             f"{c['service_ids']['added']['count']} added. service_ids are regenerated "
             "every pick; only their date coverage matters.")

    findings.sort(key=lambda d: (SEVERITY_RANK.get(d["severity"], 9), d["dimension"], d["detail"]))

    drift = any(part["drift_detected"] for part in (f, r, s, ids, c))
    return {
        "headline": _headline(drift, f, r, s, ids, c),
        "drift_detected": drift,
        "findings": findings,
        "alarming": [x for x in findings if x["severity"] == "alarming"],
        "files": {
            "tables_added": f["tables_added"],
            "tables_removed": f["tables_removed"],
            "row_deltas": [row for row in f["rows"] if row["status"] != "unchanged"][
                :SUMMARY_SAMPLE_LIMIT],
            "totals": f["totals"],
            "drift_detected": f["drift_detected"],
        },
        "routes": {"counts": r["counts"], "added": r["added"]["items"],
                   "removed": r["removed"]["items"], "changed": r["changed"]["items"],
                   "drift_detected": r["drift_detected"]},
        "stops": {"counts": s["counts"], "moved_threshold_m": s["moved_threshold_m"],
                  "moved": s["moved"]["items"], "renamed": s["renamed"]["items"],
                  "removed": s["removed"]["items"], "drift_detected": s["drift_detected"]},
        "stop_ids": ids,
        "calendar": {"service_ids": c["service_ids"], "date_range": c["date_range"],
                     "exceptions": {"count_a": c["exceptions"]["count_a"],
                                    "count_b": c["exceptions"]["count_b"],
                                    "added": c["exceptions"]["added"]["count"],
                                    "removed": c["exceptions"]["removed"]["count"]},
                     "drift_detected": c["drift_detected"]},
        "note": "drift_detected is any difference at all. Read `findings` for whether "
                "the difference matters; the CLI maps this flag to exit code 4.",
    }


def _headline(drift: bool, f: dict[str, Any], r: dict[str, Any], s: dict[str, Any],
              ids: dict[str, Any], c: dict[str, Any]) -> str:
    """One line a human can read in one breath."""
    if not drift:
        return ("No drift: the two snapshots agree on files, routes, stops, stop_ids "
                "and calendar.")
    parts = [
        f"stops {s['counts']['a']:,} -> {s['counts']['b']:,} "
        f"(+{s['counts']['added']} / -{s['counts']['removed']}, "
        f"{s['counts']['renamed']} renamed, {s['counts']['moved']} moved)",
        f"stop_code survival {_pct(ids['survival_rate_stop_code'])}"
        + ("" if ids["meets_assumption"] else " -- BELOW the "
           f"{_pct(STOP_CODE_SURVIVAL_FLOOR)} floor"),
        f"routes {r['counts']['a']} -> {r['counts']['b']} "
        f"(+{r['counts']['added']} / -{r['counts']['removed']}, "
        f"{r['counts']['changed']} changed)",
    ]
    if c["date_range"]["a"]["end"] != c["date_range"]["b"]["end"]:
        parts.append(f"service ends {c['date_range']['a']['end']} -> "
                     f"{c['date_range']['b']['end']}")
    if f["tables_added"] or f["tables_removed"]:
        parts.append(f"{len(f['tables_added'])} file(s) added, "
                     f"{len(f['tables_removed'])} removed")
    return "; ".join(parts) + "."
