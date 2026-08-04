"""Routes, stops, and the stop_id-vs-stop_code question."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .inspect import _columns, _route_type_name, _tables


def routes(conn: sqlite3.Connection, route_type: str | None = None,
           search: str | None = None) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM routes").fetchall()
    out = []
    for r in rows:
        rec = dict(r)
        if route_type and str(rec.get("route_type", "")).strip() != str(route_type):
            continue
        if search:
            hay = " ".join(str(v) for v in rec.values()).lower()
            if search.lower() not in hay:
                continue
        trips = conn.execute(
            "SELECT COUNT(*) FROM trips WHERE route_id = ?", (rec["route_id"],)
        ).fetchone()[0]
        rec["trip_count"] = trips
        rec["route_type_name"] = _route_type_name(rec.get("route_type", ""))
        out.append(rec)
    out.sort(key=lambda d: (d.get("route_type", ""), d.get("route_short_name") or "", d["route_id"]))
    return out


def route_detail(conn: sqlite3.Connection, route_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM routes WHERE route_id = ?", (route_id,)).fetchone()
    if row is None:
        return {"route_id": route_id, "exists": False}
    trip_cols = _columns(conn, "trips")
    has_dir = "direction_id" in trip_cols
    has_head = "trip_headsign" in trip_cols
    directions: list[dict[str, Any]] = []
    dir_values = (
        [r[0] for r in conn.execute(
            "SELECT DISTINCT direction_id FROM trips WHERE route_id = ? ORDER BY direction_id",
            (route_id,))]
        if has_dir else [""]
    )
    for d in dir_values:
        where = "route_id = ?" + (" AND direction_id = ?" if has_dir else "")
        params = (route_id, d) if has_dir else (route_id,)
        n = conn.execute(f"SELECT COUNT(*) FROM trips WHERE {where}", params).fetchone()[0]
        heads = (
            [r[0] for r in conn.execute(
                f"SELECT DISTINCT trip_headsign FROM trips WHERE {where} LIMIT 10", params)]
            if has_head else []
        )
        span = conn.execute(
            "SELECT MIN(st.departure_time), MAX(st.departure_time) FROM stop_times st "
            f"JOIN trips t ON t.trip_id = st.trip_id WHERE t.{where} AND st.departure_time <> ''",
            params,
        ).fetchone()
        directions.append(
            {"direction_id": d, "trips": n, "headsigns": heads,
             "first_departure": span[0], "last_departure": span[1]}
        )
    services = [
        {"service_id": r[0], "trips": r[1]}
        for r in conn.execute(
            "SELECT service_id, COUNT(*) FROM trips WHERE route_id = ? "
            "GROUP BY service_id ORDER BY 2 DESC", (route_id,))
    ]
    return {"route": dict(row), "exists": True, "directions": directions, "services": services}


def escape_like(value: str, escape: str = "\\") -> str:
    """Neutralise LIKE wildcards in a user-supplied search term.

    Without this, searching for "_" matches every stop and searching for a
    name that genuinely contains "%" silently returns the wrong set. The value
    is already parameterised, so this is a correctness fix, not an injection
    one.
    """
    out = value.replace(escape, escape + escape)
    return out.replace("%", escape + "%").replace("_", escape + "_")


def stops(conn: sqlite3.Connection, search: str | None = None, code: str | None = None,
          route_id: str | None = None) -> list[dict[str, Any]]:
    """Every matching stop. No hidden cap -- callers paginate the full list.

    An earlier version capped the unfiltered branch at 2000 rows, which
    `paginate` then reported as the true total. A client paging through it
    stopped at 2000 of 5118 stops and had no way to tell.
    """
    if route_id:
        rows = conn.execute(
            "SELECT DISTINCT s.* FROM stops s JOIN stop_times st ON st.stop_id = s.stop_id "
            "JOIN trips t ON t.trip_id = st.trip_id WHERE t.route_id = ?", (route_id,)
        ).fetchall()
    elif code:
        rows = conn.execute("SELECT * FROM stops WHERE stop_code = ?", (code,)).fetchall()
    elif search:
        rows = conn.execute(
            "SELECT * FROM stops WHERE lower(stop_name) LIKE ? ESCAPE '\\'",
            (f"%{escape_like(search.lower())}%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM stops").fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda d: (d.get("stop_name") or "", d["stop_id"]))
    return out


def stop_detail(conn: sqlite3.Connection, needle: str) -> dict[str, Any]:
    from .departures import resolve_stop

    resolved = resolve_stop(conn, needle)
    ids = resolved["stop_ids"]
    ph = ", ".join("?" * len(ids))
    serving = [
        dict(r)
        for r in conn.execute(
            "SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name, r.route_type "
            "FROM routes r JOIN trips t ON t.route_id = r.route_id "
            f"JOIN stop_times st ON st.trip_id = t.trip_id WHERE st.stop_id IN ({ph})", ids
        )
    ]
    calls = conn.execute(
        f"SELECT COUNT(*) FROM stop_times WHERE stop_id IN ({ph})", ids
    ).fetchone()[0]
    return {**resolved, "routes": serving, "stop_times_rows": calls}


def stop_resolve(conn: sqlite3.Connection, sample_size: int = 200) -> dict[str, Any]:
    """Which field holds the number printed on a bus stop sign?

    Metro's own documentation shows stop ID 15111 on a stop sign photo. This
    reports where such a value lives, whether it is unique, and what format it
    takes -- the three things the app's input screen depends on.
    """
    cols = _columns(conn, "stops")
    total = conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
    report: dict[str, Any] = {"total_stops": total, "columns": cols}

    for field in ("stop_code", "stop_id"):
        if field not in cols:
            report[field] = {"present": False}
            continue
        nonempty = conn.execute(
            f"SELECT COUNT(*) FROM stops WHERE {field} <> ''"
        ).fetchone()[0]
        distinct = conn.execute(f"SELECT COUNT(DISTINCT {field}) FROM stops").fetchone()[0]
        samples = [
            r[0] for r in conn.execute(
                f"SELECT {field} FROM stops WHERE {field} <> '' LIMIT ?", (sample_size,))
        ]
        numeric = [s for s in samples if re.fullmatch(r"\d+", s or "")]
        lengths = sorted({len(s) for s in numeric}) if numeric else []
        report[field] = {
            "present": True,
            "non_empty": nonempty,
            "coverage": round(nonempty / total, 4) if total else None,
            "distinct": distinct,
            "unique": distinct == nonempty,
            "numeric_share": round(len(numeric) / len(samples), 4) if samples else None,
            "observed_lengths": lengths,
            "samples": samples[:8],
        }

    known = "15111"  # Metro's own published example, from the stop-sign photo
    hits = []
    for field in ("stop_code", "stop_id"):
        if field in cols:
            n = conn.execute(f"SELECT COUNT(*) FROM stops WHERE {field} = ?", (known,)).fetchone()[0]
            if n:
                hits.append({"field": field, "matches": n})
    report["published_example"] = {"value": known, "found_in": hits}

    code = report.get("stop_code", {})
    if code.get("present") and (code.get("coverage") or 0) > 0.9 and code.get("unique"):
        verdict = "stop_code"
        rationale = "stop_code is populated on >90% of stops and is unique."
    elif hits:
        verdict = hits[0]["field"]
        rationale = f"Metro's published example {known} resolves via {hits[0]['field']}."
    else:
        verdict = "unknown"
        rationale = ("Neither field is a clean match. Resolve manually before building "
                     "the Stop ID entry screen -- the whole UX depends on it.")
    report["verdict"] = {"rider_facing_field": verdict, "rationale": rationale}
    return report
