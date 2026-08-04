"""Re-run the audit's failing cases against the real snapshot.

Kept in the repo because these are live-data checks the unit suite cannot make:
the synthetic feeds in tests/fixtures.py have no negative RT delays and no
489k-row table to time out on.

    STL_HOME=$PWD .venv/bin/python scripts/verify_fixes.py
"""

from __future__ import annotations

import json
import sys
import traceback

from stl_transit.core import service
from stl_transit.core.rt import decode as rtdecode
from stl_transit.io.store import Store

BUSY_STOPS = ["13330", "14792", "15073", "3693", "7855"]
AT = "2026-08-03T14:10:00"

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        ok, note = fn()
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))
        traceback.print_exc(file=sys.stderr)
        return
    results.append((name, ok, note))


def rt_arrivals_no_overflow():
    """Bug 1: 4 of 5 busy stops raised OverflowError on a negative delay."""
    bad = []
    for stop in BUSY_STOPS:
        try:
            service.rt_stop_arrivals(stop=stop, at=AT, window_minutes=90, limit=10)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{stop}:{type(exc).__name__}")
    return not bad, f"{len(BUSY_STOPS) - len(bad)}/{len(BUSY_STOPS)} stops clean" + (
        f"; failed {bad}" if bad else ""
    )


def negative_delays_decode_negative():
    """Bug 1: 30.8% of delay values in the snapshot are negative."""
    store = Store()
    snap = store.latest("metro_rt_trips")
    decoded = rtdecode.decode_feed(snap.payload.read_bytes())
    delays = []
    for e in decoded["entities"]:
        tu = e.get("trip_update") or {}
        for u in tu.get("stop_time_update", []) or []:
            for ev in ("arrival", "departure"):
                d = (u.get(ev) or {}).get("delay")
                if d is not None:
                    delays.append(d)
    neg = [d for d in delays if d < 0]
    absurd = [d for d in delays if abs(d) > 12 * 3600]
    return (
        bool(neg) and not absurd,
        f"{len(delays)} delays, {len(neg)} negative "
        f"({len(neg) / max(1, len(delays)):.1%}), {len(absurd)} absurd, "
        f"range {min(delays, default=0)}..{max(delays, default=0)}",
    )


def query_aggregate_completes():
    """Bug 4: a 0.34s GROUP BY over stop_times was aborted as UNSAFE_QUERY."""
    r = service.gtfs_query(
        sql="SELECT length(departure_time) AS n, COUNT(*) AS c FROM stop_times "
            "WHERE departure_time <> '' GROUP BY 1",
        limit=20,
    )
    return r.get("ok") is True, f"rows={r.get('row_count')} in {r.get('elapsed_seconds')}s"


def _code(sql: str) -> str:
    """The error code a query produces, or 'OK'.

    core functions RAISE StlError; the MCP layer's _call() is what turns that
    into a structured dict. Mirror that here rather than assuming a return.
    """
    from stl_transit.errors import StlError

    try:
        service.gtfs_query(sql=sql)
    except StlError as exc:
        return exc.code
    return "OK"


def query_errors_are_distinct():
    """Bug 4: a timeout, a typo and a write attempt all said UNSAFE_QUERY, so
    the model was told it wrote something dangerous when it wrote a typo."""
    got = {
        "write": _code("DROP TABLE stops"),
        "typo": _code("SELECT nope FROM stops"),
        "attach": _code("ATTACH DATABASE '/tmp/x.db' AS x"),
        "multi": _code("SELECT 1; SELECT 2"),
        "semicolon_literal": _code("SELECT ';' AS semi"),
        "trailing_comment": _code("SELECT 1 AS a -- ; not a statement"),
        "valid_cte": _code("WITH q AS (SELECT 1 AS a) SELECT * FROM q"),
    }
    want = {
        "write": "UNSAFE_QUERY",
        "typo": "QUERY_FAILED",
        "attach": "UNSAFE_QUERY",
        "multi": "UNSAFE_QUERY",
        "semicolon_literal": "OK",
        "trailing_comment": "OK",
        "valid_cte": "OK",
    }
    diff = {k: (want[k], got[k]) for k in want if want[k] != got[k]}
    return not diff, (f"all {len(want)} distinct" if not diff else f"mismatch {diff}")


def stops_total_is_real():
    """Bug 5: unfiltered listing reported total=2000 for a 5118-stop feed."""
    listed = service.gtfs_stops(limit=5)
    stats = service.gtfs_stats()
    real = stats["counts"]["stops"] if "counts" in stats else stats.get("stops")
    return listed["total"] == real, f"listed total={listed['total']} actual={real}"


def stops_wildcard_is_literal():
    """Bug 9: search='_' matched every stop."""
    underscore = service.gtfs_stops(search="_", limit=5)
    percent = service.gtfs_stops(search="%", limit=5)
    return (
        underscore["total"] < 100 and percent["total"] < 100,
        f"'_' -> {underscore['total']}, '%' -> {percent['total']}",
    )


def census_has_no_phantoms():
    """Bug 6: 3 'unmodelled' paths were ASCII digits misread as submessages."""
    r = service.rt_schema_census(entity="trip_updates", samples=1)
    return r["unmodelled_paths"] == 0, (
        f"{r['distinct_paths']} paths, {r['unmodelled_paths']} unmodelled "
        f"{[u['path'] for u in r['unmodelled']][:5]}"
    )


def departures_carry_stop_sequence():
    """Bug 7: dropped, making the RT sequence-matching branch dead code."""
    r = service.gtfs_departures(stop="10626", at="2026-08-05T12:00:00", limit=3)
    items = r.get("items") or []
    return bool(items) and all("stop_sequence" in i for i in items), (
        f"{len(items)} departures, keys ok"
    )


def routes_truncated_flag_is_honest():
    """Bug 8: limit=1000 over 62 routes returned truncated=True."""
    r = service.gtfs_routes(limit=1000)
    return r["truncated"] is False and r["total"] == r["count"], (
        f"total={r['total']} count={r['count']} truncated={r['truncated']}"
    )


CHECKS = [
    ("rt_stop_arrivals survives negative delays", rt_arrivals_no_overflow),
    ("negative delays decode as negative", negative_delays_decode_negative),
    ("aggregate over stop_times completes", query_aggregate_completes),
    ("query error codes are distinct", query_errors_are_distinct),
    ("gtfs_stops total is the real total", stops_total_is_real),
    ("LIKE wildcards are literal", stops_wildcard_is_literal),
    ("RT census reports no phantom fields", census_has_no_phantoms),
    ("departures carry stop_sequence", departures_carry_stop_sequence),
    ("routes truncated flag is honest", routes_truncated_flag_is_honest),
]

if __name__ == "__main__":
    for name, fn in CHECKS:
        check(name, fn)
    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, note in results:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {note}")
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)
