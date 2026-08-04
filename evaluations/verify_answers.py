"""Recompute every answer in evaluations.xml against the pinned snapshots.

The eval suite is only worth anything if its answers are true. This script is
how that stays true after the pin moves: one run says which of the ten answers
still holds and which drifted, instead of leaving a stale number to train
against.

Every question is computed TWICE, deliberately:

- the **tool route** is what an agent actually calls -- `core.service` functions,
  the same code the MCP wrappers front;
- the **raw route** re-derives the answer from `feed.sqlite` and the protobuf
  bytes without importing anything from `core.gtfs` or `core.rt`.

Grading the tool with the tool's own arithmetic proves self-consistency and
nothing else. When the two routes disagree the question is ambiguous, not the
snapshot -- rewrite the question rather than re-pinning the number.

    STL_HOME=$PWD .venv/bin/python evaluations/verify_answers.py
"""

from __future__ import annotations

import re
import sqlite3
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from stl_transit.core import service
from stl_transit.io.store import Store

# Pinned in the questions themselves, restated here so a drift run reports which
# snapshot it graded against rather than "the latest", which is a moving target.
GTFS_SNAPSHOT = "gtfs-20260803T190539Z-f2d721"
RT_SNAPSHOT = "rt-20260803T190604Z-5ceebf"

CHICAGO = ZoneInfo("America/Chicago")
XML_PATH = Path(__file__).resolve().parent / "evaluations.xml"

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ------------------------------------------------------ the independent route --

def raw_conn() -> sqlite3.Connection:
    """Read-only handle straight onto the pinned snapshot's SQLite index.

    Opened here rather than through `io.db.connect_ro` so a bug in the
    connection helper cannot make both routes wrong in the same direction.
    """
    store = Store()
    db = store.get(GTFS_SNAPSHOT).path / "feed.sqlite"
    if not db.is_file():
        raise SystemExit(
            f"No feed.sqlite under {db.parent}. Build it first: "
            f"`stl gtfs import --snapshot {GTFS_SNAPSHOT}`."
        )
    return sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)


def active_services(conn: sqlite3.Connection, day: date) -> set[str]:
    """service_ids running on `day`: the weekly pattern, then the exceptions.

    Re-derived rather than imported. Two of the ten answers turn on a
    calendar_dates row that adds a service_id absent from calendar.txt entirely,
    which is exactly the arithmetic a second opinion has to cover.
    """
    key = day.strftime("%Y%m%d")
    weekday = WEEKDAYS[day.weekday()]
    base = {
        r[0]
        for r in conn.execute(
            f'SELECT service_id FROM calendar WHERE "{weekday}" = \'1\' '
            "AND start_date <= ? AND end_date >= ?",
            (key, key),
        )
    }
    added, removed = set(), set()
    for service_id, exception in conn.execute(
        "SELECT service_id, exception_type FROM calendar_dates WHERE date = ?", (key,)
    ):
        (added if str(exception).strip() == "1" else removed).add(service_id)
    return (base | added) - removed


def service_day_start(day: date) -> datetime:
    """Noon minus twelve hours, done on the absolute instant.

    Subtracting the twelve hours in UTC is the whole point: doing it on the
    zone-aware value is wall-clock arithmetic, which collapses back to local
    midnight and silently loses the hour on both DST transition days.
    """
    noon = datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=CHICAGO)
    return (noon.astimezone(timezone.utc) - timedelta(hours=12)).astimezone(CHICAGO)


def instant(day: date, gtfs_seconds: int) -> datetime:
    """(service_date, seconds) -> the real instant. Offsets added in UTC, again."""
    start = service_day_start(day)
    return (start.astimezone(timezone.utc) + timedelta(seconds=gtfs_seconds)).astimezone(CHICAGO)


def scheduled_departures(conn: sqlite3.Connection, stop_id: str,
                         start: datetime, window_minutes: int) -> list[tuple]:
    """Every departure from `stop_id` inside the window, re-derived from scratch.

    Two days of lookback because a departure visible at 00:23 belongs to the
    PREVIOUS service date encoded as 24:23:00, and this feed reaches 25:37:00.
    The pickup_type filter is a no-op on this snapshot -- every one of the
    489,011 rows is pickup_type '0' -- but it is written down anyway, because
    the day Metro starts marking drop-off-only stops, a suite that never
    modelled the rule would report the tool as having drifted.
    """
    end = (start.astimezone(timezone.utc) + timedelta(minutes=window_minutes)).astimezone(CHICAGO)
    found: list[tuple] = []
    day = start.date() - timedelta(days=2)
    while day <= end.date():
        active = sorted(active_services(conn, day))
        if active:
            placeholders = ", ".join("?" * len(active))
            rows = conn.execute(
                "SELECT st.trip_id, st.departure_time FROM stop_times st "
                "JOIN trips t ON t.trip_id = st.trip_id "
                f"WHERE st.stop_id = ? AND t.service_id IN ({placeholders}) "
                "AND st.departure_time <> '' "
                "AND COALESCE(st.pickup_type, '0') <> '1'",
                (stop_id, *active),
            ).fetchall()
            for trip_id, value in rows:
                hours, minutes, seconds = (int(p) for p in value.split(":"))
                when = instant(day, hours * 3600 + minutes * 60 + seconds)
                if start <= when <= end:
                    found.append((when.isoformat(), trip_id, day.isoformat(), value))
        day += timedelta(days=1)
    return sorted(found)


# ------------------------------------------------- a protobuf reader of our own --

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def wire_fields(buf: bytes):
    """Yield (field_number, wire_type, payload) for one protobuf message.

    Thirty lines of varint reading so the wire questions are graded against
    something that shares no code with core/rt -- which is the only way this
    suite can catch a decoder that is confidently wrong.
    """
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, i = _varint(buf, i)
            yield number, wire_type, value
        elif wire_type == 2:
            length, i = _varint(buf, i)
            yield number, wire_type, buf[i:i + length]
            i += length
        elif wire_type == 5:
            yield number, wire_type, buf[i:i + 4]
            i += 4
        elif wire_type == 1:
            yield number, wire_type, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"unsupported wire type {wire_type} at byte {i}")


def as_int32(value: int) -> int:
    """Protobuf writes a negative int32 sign-extended to a full 64-bit varint.

    Skip this and -240 decodes as 18446744073709551376, which is the exact bug
    the delay question exists to catch.
    """
    return value - (1 << 64) if value >= (1 << 63) else value


def rt_payload() -> bytes:
    return Store().get(RT_SNAPSHOT).payload.read_bytes()


# ------------------------------------------------------------------- questions --
#
# One function per qa_pair, in the order they appear in evaluations.xml. Each
# returns (tool_route_answer, raw_route_answer, note).

def q1_late_night_rollover():
    """A 25:37:00 departure belongs to the PREVIOUS service date, so at 01:30 on
    Sunday morning the trip on offer is Saturday's."""
    latest = service.gtfs_late_night(threshold="25:00:00", snapshot=GTFS_SNAPSHOT, limit=50)
    stop_id = "16251"  # the stop carrying the feed maximum; asserted below
    conn = raw_conn()
    try:
        peak = conn.execute("SELECT MAX(departure_time) FROM stop_times "
                            "WHERE departure_time <> ''").fetchone()[0]
        stops = [r[0] for r in conn.execute(
            "SELECT DISTINCT stop_id FROM stop_times WHERE departure_time = ?", (peak,))]
        assert stops == [stop_id], f"feed maximum moved to stop(s) {stops}"
        at = datetime(2026, 8, 9, 1, 30, tzinfo=CHICAGO)
        raw = scheduled_departures(conn, stop_id, at, 15)
    finally:
        conn.close()

    result = service.gtfs_departures(stop=stop_id, at="2026-08-09T01:30:00",
                                     window_minutes=15, snapshot=GTFS_SNAPSHOT, limit=50)
    tool = result["items"][0]["trip_id"] if result["total"] == 1 else f"total={result['total']}"
    note = (f"max={peak} at {stop_id}; tool total={result['total']} "
            f"service_date={result['items'][0]['service_date'] if result['items'] else '-'}; "
            f"raw matches={len(raw)}; late_night max={latest.get('max_departure_time')}")
    return tool, (raw[0][1] if len(raw) == 1 else f"matches={len(raw)}"), note


def q2_calendar_exception_trip_count():
    """2026-08-08 is the one date in the window where calendar_dates.txt both adds
    a service_id that appears nowhere in calendar.txt and removes one that does."""
    active = service.gtfs_calendar(on="2026-08-08", snapshot=GTFS_SNAPSHOT)
    ids = active["active"]
    quoted = ", ".join(f"'{s}'" for s in ids)
    counted = service.gtfs_query(
        sql=f"SELECT COUNT(*) AS n FROM trips WHERE service_id IN ({quoted})",
        snapshot=GTFS_SNAPSHOT, limit=5)
    # gtfs_query returns rows as {column: value}, not tuples.
    tool = int(counted["rows"][0]["n"])

    conn = raw_conn()
    try:
        raw_ids = sorted(active_services(conn, date(2026, 8, 8)))
        placeholders = ", ".join("?" * len(raw_ids))
        raw = conn.execute(
            f"SELECT COUNT(*) FROM trips WHERE service_id IN ({placeholders})", raw_ids
        ).fetchone()[0]
        naive = conn.execute(
            "SELECT COUNT(*) FROM trips WHERE service_id IN "
            "(SELECT service_id FROM calendar WHERE saturday = '1' "
            "AND start_date <= '20260808' AND end_date >= '20260808')"
        ).fetchone()[0]
    finally:
        conn.close()
    note = (f"active={ids} added={active['added_by_exception']} "
            f"removed={active['removed_by_exception']}; calendar.txt alone would say {naive}")
    return tool, raw, note


def q3_metrolink_route_id_after_the_pick():
    """The feed carries both sides of a mid-window pick, so 'MetroLink Red Line'
    is two route_ids and only one of them runs after 2026-08-10."""
    routes = service.gtfs_routes(route_type="2", snapshot=GTFS_SNAPSHOT, limit=50)
    red = sorted(r["route_id"] for r in routes["items"]
                 if r["route_long_name"] == "MetroLink Red Line")
    active = service.gtfs_calendar(on="2026-08-17", snapshot=GTFS_SNAPSHOT)["active"]
    quoted_services = ", ".join(f"'{s}'" for s in active)
    quoted_routes = ", ".join(f"'{r}'" for r in red)
    hit = service.gtfs_query(
        sql=f"SELECT DISTINCT route_id FROM trips WHERE service_id IN ({quoted_services}) "
            f"AND route_id IN ({quoted_routes}) ORDER BY route_id",
        snapshot=GTFS_SNAPSHOT, limit=10)
    tool = (hit["rows"][0]["route_id"] if hit["row_count"] == 1
            else f"rows={hit['row_count']}")

    conn = raw_conn()
    try:
        raw_services = sorted(active_services(conn, date(2026, 8, 17)))
        placeholders = ", ".join("?" * len(raw_services))
        rows = [r[0] for r in conn.execute(
            "SELECT DISTINCT t.route_id FROM trips t JOIN routes r ON r.route_id = t.route_id "
            f"WHERE r.route_long_name = 'MetroLink Red Line' AND t.service_id IN ({placeholders})",
            raw_services)]
    finally:
        conn.close()
    note = f"candidates={red}; services on 2026-08-17={active}"
    return tool, (rows[0] if len(rows) == 1 else f"rows={len(rows)}"), note


def q4_metrolink_stop_footprint():
    """Rail and bus share transit centres by name but never by stop_id in this
    feed, so the rail footprint is a clean 38 and no stop is multimodal."""
    routes = service.gtfs_routes(route_type="2", snapshot=GTFS_SNAPSHOT, limit=50)
    served: set[str] = set()
    for row in routes["items"]:
        stops = service.gtfs_stops(route_id=row["route_id"], snapshot=GTFS_SNAPSHOT, limit=500)
        served |= {s["stop_id"] for s in stops["items"]}
    tool = len(served)

    conn = raw_conn()
    try:
        raw = conn.execute(
            "SELECT COUNT(DISTINCT st.stop_id) FROM stop_times st "
            "JOIN trips t ON t.trip_id = st.trip_id "
            "JOIN routes r ON r.route_id = t.route_id WHERE TRIM(r.route_type) = '2'"
        ).fetchone()[0]
        both = conn.execute(
            "SELECT COUNT(*) FROM (SELECT st.stop_id FROM stop_times st "
            "JOIN trips t ON t.trip_id = st.trip_id JOIN routes r ON r.route_id = t.route_id "
            "GROUP BY st.stop_id HAVING SUM(r.route_type = '2') > 0 "
            "AND SUM(r.route_type = '3') > 0)"
        ).fetchone()[0]
    finally:
        conn.close()
    note = f"{len(routes['items'])} rail routes; stops served by both rail and bus: {both}"
    return tool, raw, note


def q5_stop_code_format_shortfall():
    """The assumption reports the rate; the app's keypad needs the count. Both
    have to agree or the suite is quoting a ratio nobody can act on."""
    suite = service.assert_run(only=["stop_code_format"], snapshot=GTFS_SNAPSHOT)
    item = suite["items"][0]
    observed = float(item["observed"])
    total = service.gtfs_query(sql="SELECT COUNT(*) AS n FROM stops WHERE stop_code <> ''",
                               snapshot=GTFS_SNAPSHOT, limit=5)["rows"][0]["n"]
    # observed is rounded to 4dp, so recover the count and round back to an int.
    tool = int(round(total - observed * total))

    conn = raw_conn()
    try:
        pattern = re.compile(str(item["thresholds"].get("pattern", "^[0-9]{4,5}$")))
        codes = [r[0] for r in conn.execute("SELECT stop_code FROM stops WHERE stop_code <> ''")]
        bad = sorted({c for c in codes if not pattern.search(c)})
    finally:
        conn.close()
    note = (f"observed={observed} over {total} codes; "
            f"{len(bad)} fail {pattern.pattern}, shortest examples {bad[:3]}")
    return tool, len(bad), note


def q6_published_example_full_day():
    """15111 is the number on Metro's own stop-sign photo. A whole-day window
    starting at midnight catches yesterday's 24:23:00 and drops today's, which
    is the difference between 72 and the 71 a same-day-only query returns."""
    resolve = service.gtfs_stop_resolve(snapshot=GTFS_SNAPSHOT)
    stop = resolve["published_example"]["value"]
    result = service.gtfs_departures(stop=stop, at="2026-08-03T00:00:00", window_minutes=1440,
                                     snapshot=GTFS_SNAPSHOT, limit=500)
    tool = result["total"]

    conn = raw_conn()
    try:
        raw_rows = scheduled_departures(conn, stop, datetime(2026, 8, 3, tzinfo=CHICAGO), 1440)
        rolled = [r for r in raw_rows if r[2] != "2026-08-03"]
    finally:
        conn.close()
    note = (f"stop {stop} via {resolve['verdict']['rider_facing_field']}; "
            f"{len(rolled)} of {len(raw_rows)} carried over from the previous service date")
    return tool, len(raw_rows), note


def q7_cancelled_trip_updates():
    """A cancelled TripUpdate arrives as a bare trip descriptor: no route_id, no
    stop_time_update. An app that reads stop_time_update first sees nothing at all."""
    decoded = service.rt_decode(snapshot=RT_SNAPSHOT, entity="trip_updates", limit=500)
    tool = sum(
        1 for e in decoded["items"]
        if ((e.get("trip_update") or {}).get("trip") or {}).get("schedule_relationship")
        == "CANCELED"
    )

    raw = bare = 0
    for number, _wt, payload in wire_fields(rt_payload()):
        if number != 2:  # FeedMessage.entity
            continue
        for enum, _w2, entity_body in wire_fields(payload):
            if enum != 3:  # FeedEntity.trip_update
                continue
            cancelled = False
            updates = 0
            for tnum, _w3, tu_body in wire_fields(entity_body):
                if tnum == 2:  # TripUpdate.stop_time_update
                    updates += 1
                if tnum != 1:  # TripUpdate.trip
                    continue
                for dnum, _w4, value in wire_fields(tu_body):
                    # TripDescriptor.schedule_relationship, CANCELED = 3
                    if dnum == 4 and value == 3:
                        cancelled = True
            raw += cancelled
            bare += cancelled and updates == 0
    note = (f"{decoded['total']} entities decoded; {bare} of the {raw} cancelled "
            f"updates carry zero stop_time_update entries")
    return tool, raw, note


def q8_most_negative_delay():
    """delay is a signed int32. Read it unsigned and 'four minutes early' becomes
    1.8e19, which is how rt_stop_arrivals used to raise OverflowError."""
    decoded = service.rt_decode(snapshot=RT_SNAPSHOT, entity="trip_updates", limit=500)
    values = [
        event["delay"]
        for entity in decoded["items"]
        for update in ((entity.get("trip_update") or {}).get("stop_time_update") or [])
        for event in (update.get("arrival"), update.get("departure"))
        if event and "delay" in event
    ]
    tool = min(values)

    raw_values: list[int] = []
    for number, _wt, payload in wire_fields(rt_payload()):
        if number != 2:
            continue
        for enum, _w2, entity_body in wire_fields(payload):
            if enum != 3:
                continue
            for tnum, _w3, tu_body in wire_fields(entity_body):
                if tnum != 2:  # TripUpdate.stop_time_update
                    continue
                for snum, _w4, stu_body in wire_fields(tu_body):
                    if snum not in (2, 3):  # arrival, departure
                        continue
                    for enum2, _w5, value in wire_fields(stu_body):
                        if enum2 == 1:  # StopTimeEvent.delay
                            raw_values.append(as_int32(value))
    negatives = sum(1 for v in raw_values if v < 0)
    note = (f"{len(raw_values)} delay values, {negatives} negative "
            f"({negatives / len(raw_values):.1%}), range "
            f"{min(raw_values)}..{max(raw_values)}")
    return tool, min(raw_values), note


def q9_top_level_wire_fields():
    """One FeedHeader plus one field per entity. Getting 144 means the header was
    skipped; getting 1 means the entities were read as a single repeated blob."""
    dump = service.rt_wire(snapshot=RT_SNAPSHOT, entity="trip_updates", depth=2, max_entities=1)
    tool = dump["top_level_fields"]

    payload = rt_payload()
    numbers = [n for n, _wt, _p in wire_fields(payload)]
    note = (f"{len(payload)} bytes; field 1 x{numbers.count(1)} (header), "
            f"field 2 x{numbers.count(2)} (entity); tool reports "
            f"entities_in_feed={dump['entities_in_feed']}")
    return tool, len(numbers), note


def q10_spring_forward_service_day_start():
    """On 2027-03-14 the service day begins at 23:00 on the PREVIOUS calendar day.
    Local midnight is wrong by an hour exactly twice a year, which is why it hides."""
    reported = service.gtfs_service_day(timestamp="2027-03-14T12:00:00")
    tool = next(c["service_day_start"] for c in reported["candidates"]
                if c["service_date"] == "2027-03-14")
    raw = service_day_start(date(2027, 3, 14)).isoformat()
    ordinary = service_day_start(date(2027, 3, 21)).isoformat()
    note = f"an ordinary Sunday a week later starts {ordinary}"
    return tool, raw, note


CHECKS = [
    ("late-night rollover attributes to the previous service date", q1_late_night_rollover),
    ("calendar_dates exception changes the 2026-08-08 trip count", q2_calendar_exception_trip_count),
    ("MetroLink Red Line route_id after the mid-window pick", q3_metrolink_route_id_after_the_pick),
    ("MetroLink stop footprint by route_type", q4_metrolink_stop_footprint),
    ("stop_code_format shortfall in whole stops", q5_stop_code_format_shortfall),
    ("published example stop over a full service day", q6_published_example_full_day),
    ("cancelled TripUpdates in the realtime snapshot", q7_cancelled_trip_updates),
    ("most negative delay decodes as signed", q8_most_negative_delay),
    ("top-level protobuf fields on the wire", q9_top_level_wire_fields),
    ("spring-forward service day start", q10_spring_forward_service_day_start),
]


def load_expected() -> list[tuple[str, str]]:
    pairs = [
        ((p.findtext("question") or "").strip(), (p.findtext("answer") or "").strip())
        for p in ET.parse(XML_PATH).findall("qa_pair")
    ]
    if len(pairs) != len(CHECKS):
        raise SystemExit(
            f"{XML_PATH} has {len(pairs)} qa_pairs but this script implements "
            f"{len(CHECKS)} checks. They are positional -- add or remove the "
            "matching function in CHECKS."
        )
    return pairs


if __name__ == "__main__":
    expected = load_expected()
    results: list[tuple[str, bool, str]] = []
    for (name, fn), (_question, answer) in zip(CHECKS, expected):
        try:
            tool, raw, note = fn()
        except Exception as exc:  # noqa: BLE001 - a check that blew up has not passed
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
            traceback.print_exc(file=sys.stderr)
            continue
        # Both routes must land on the pinned string. A tool/raw disagreement is
        # an ambiguous question; a matched pair that misses the XML is drift.
        agree = str(tool) == str(raw)
        correct = str(tool) == answer
        detail = f"want {answer!r}, tool {str(tool)!r}, raw {str(raw)!r} | {note}"
        if not agree:
            detail = "ROUTES DISAGREE -- rewrite the question. " + detail
        results.append((name, agree and correct, detail))

    width = max(len(n) for n, _, _ in results)
    failed = sum(not ok for _, ok, _ in results)
    print(f"snapshots: {GTFS_SNAPSHOT} / {RT_SNAPSHOT}\n")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print(f"\n{len(results) - failed}/{len(results)} answers verified")
    sys.exit(1 if failed else 0)
