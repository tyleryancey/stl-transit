"""Feed inspection: files, schema, stats, features, coverage."""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ...io.db import zip_members
from .calendar import parse_ymd

# Optional GTFS files whose presence or absence changes the app's design.
OPTIONAL_FILES = {
    "calendar_dates.txt": "Service exceptions (holidays). Absent = holidays live only in calendar.txt.",
    "transfers.txt": "Explicit transfer rules. Absent = transfers must be derived by proximity.",
    "frequencies.txt": "Headway-based trips. PRESENT means a whole second code path in the app.",
    "fare_attributes.txt": "Fare prices. Absent = fares must be bundled from the website.",
    "fare_rules.txt": "Fare zone rules.",
    "fare_products.txt": "GTFS-Fares v2.",
    "pathways.txt": "In-station walking graph.",
    "levels.txt": "Station levels.",
    "shapes.txt": "Route geometry. Large; prune for on-device.",
    "feed_info.txt": "Feed validity dates and version.",
    "translations.txt": "Multilingual field values.",
    "attributions.txt": "Required attributions.",
}

FEATURE_CHECKS = {
    "Route Colors": ("routes", "route_color"),
    "Headsigns": ("trips", "trip_headsign"),
    "Shapes": ("shapes", None),
    "Stops Wheelchair Accessibility": ("stops", "wheelchair_boarding"),
    "Trips Wheelchair Accessibility": ("trips", "wheelchair_accessible"),
    "Bikes Allowed": ("trips", "bikes_allowed"),
    "Location Types": ("stops", "location_type"),
    "Pathways": ("pathways", None),
    "Transfers": ("transfers", None),
    "Frequency-Based Service": ("frequencies", None),
    "Fares v1": ("fare_attributes", None),
    "Fares v2": ("fare_products", None),
    "Translations": ("translations", None),
    "Feed Information": ("feed_info", None),
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def files(zip_path: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    present = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            present[info.filename] = {
                "compressed_bytes": info.compress_size,
                "uncompressed_bytes": info.file_size,
            }
    tables = _tables(conn)
    rows = []
    for name, sizes in sorted(present.items()):
        table = Path(name).stem.lower()
        count = None
        if table in tables:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        rows.append(
            {
                "file": name,
                "rows": count,
                "columns": _columns(conn, table) if table in tables else [],
                **sizes,
            }
        )
    absent = [
        {"file": f, "why_it_matters": why}
        for f, why in sorted(OPTIONAL_FILES.items())
        if f not in present
    ]
    return {
        "present": rows,
        "file_count": len(rows),
        "absent_optional": absent,
        "total_uncompressed_bytes": sum(v["uncompressed_bytes"] for v in present.values()),
        "total_compressed_bytes": sum(v["compressed_bytes"] for v in present.values()),
    }


def features(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    out = []
    for feature, (table, column) in sorted(FEATURE_CHECKS.items()):
        if table not in tables:
            out.append({"feature": feature, "present": False, "reason": f"{table}.txt absent"})
            continue
        if column is None:
            n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            out.append({"feature": feature, "present": n > 0, "reason": f"{n} rows"})
            continue
        if column not in _columns(conn, table):
            out.append({"feature": feature, "present": False, "reason": f"column {column} absent"})
            continue
        n = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" <> \'\''
        ).fetchone()[0]
        out.append({"feature": feature, "present": n > 0, "reason": f"{n} non-empty values"})
    return {"features": out,
            "note": "Lines up with the badges on the Mobility Database feed page."}


def schema(conn: sqlite3.Connection, table: str, sample: int = 3) -> dict[str, Any]:
    table = table.replace(".txt", "").lower()
    if table not in _tables(conn):
        return {"table": table, "exists": False, "columns": []}
    total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    cols = []
    for col in _columns(conn, table):
        nonempty = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" <> \'\''
        ).fetchone()[0]
        distinct = conn.execute(f'SELECT COUNT(DISTINCT "{col}") FROM "{table}"').fetchone()[0]
        samples = [
            r[0]
            for r in conn.execute(
                f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" <> \'\' LIMIT ?', (sample,)
            )
        ]
        cols.append(
            {
                "column": col,
                "non_empty": nonempty,
                "null_rate": round(1 - nonempty / total, 4) if total else None,
                "distinct": distinct,
                "samples": samples,
            }
        )
    return {"table": table, "exists": True, "rows": total, "columns": cols}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)

    def count(t: str) -> int:
        return conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] if t in tables else 0

    by_type: dict[str, int] = {}
    if "routes" in tables and "route_type" in _columns(conn, "routes"):
        for r in conn.execute("SELECT route_type, COUNT(*) FROM routes GROUP BY route_type"):
            by_type[_route_type_name(r[0])] = r[1]

    return {
        "agencies": count("agency"),
        "routes": count("routes"),
        "routes_by_type": by_type,
        "stops": count("stops"),
        "trips": count("trips"),
        "stop_times": count("stop_times"),
        "shapes_points": count("shapes"),
        "calendar_rows": count("calendar"),
        "calendar_dates_rows": count("calendar_dates"),
        "service_ids": (
            conn.execute("SELECT COUNT(DISTINCT service_id) FROM trips").fetchone()[0]
            if "trips" in tables else 0
        ),
    }


def _route_type_name(value: str) -> str:
    return {
        "0": "tram/streetcar", "1": "subway", "2": "rail", "3": "bus",
        "4": "ferry", "5": "cable tram", "6": "aerial lift", "7": "funicular",
        "11": "trolleybus", "12": "monorail",
    }.get(str(value).strip(), f"type {value}")


def coverage(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """Service date range and days-to-expiry.

    Metro publishes a feed whose service data ends at the next quarterly pick.
    A tool that caches once and never refreshes goes silently blank, so this is
    the number to surveil.
    """
    tables = _tables(conn)
    today = today or datetime.now().date()
    starts, ends = [], []
    if "calendar" in tables:
        row = conn.execute("SELECT MIN(start_date), MAX(end_date) FROM calendar").fetchone()
        if row and row[0]:
            starts.append(parse_ymd(row[0]))
            ends.append(parse_ymd(row[1]))
    if "calendar_dates" in tables:
        row = conn.execute("SELECT MIN(date), MAX(date) FROM calendar_dates").fetchone()
        if row and row[0]:
            starts.append(parse_ymd(row[0]))
            ends.append(parse_ymd(row[1]))

    feed_info = {}
    if "feed_info" in tables:
        row = conn.execute("SELECT * FROM feed_info LIMIT 1").fetchone()
        if row:
            feed_info = dict(row)

    start = min(starts) if starts else None
    end = max(ends) if ends else None
    days_remaining = (end - today).days if end else None
    return {
        "today": today.isoformat(),
        "service_start": start.isoformat() if start else None,
        "service_end": end.isoformat() if end else None,
        "days_remaining": days_remaining,
        "expired": bool(days_remaining is not None and days_remaining < 0),
        "feed_info": feed_info,
        "warning": (
            None if days_remaining is None or days_remaining >= 7
            else f"Only {days_remaining} day(s) of service data remain. Re-fetch the feed."
        ),
    }


def late_night(conn: sqlite3.Connection, threshold: str = "24:00:00", limit: int = 50) -> dict[str, Any]:
    """Trips whose times cross the service-day boundary."""
    rows = conn.execute(
        "SELECT st.trip_id, st.stop_id, st.departure_time, t.route_id, t.service_id "
        "FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id "
        "WHERE st.departure_time >= ? AND st.departure_time <> '' "
        "ORDER BY st.departure_time DESC",
        (threshold,),
    ).fetchall()
    max_time = rows[0]["departure_time"] if rows else None
    return {
        "threshold": threshold,
        "total_stop_times_past_threshold": len(rows),
        "max_departure_time": max_time,
        "distinct_trips": len({r["trip_id"] for r in rows}),
        "items": [dict(r) for r in rows[:limit]],
    }
