"""Regression tests for the second review pass, 2026-08-03.

An adversarial re-read after the first round of fixes found three high-severity
defects the 403-test suite did not cover. Each is pinned here, because a fix
without a test is a fix waiting to be refactored back out.

The theme is worth naming: two of the three are cases where something reported
success on a measurement it never took. That failure mode does not announce
itself -- it looks exactly like good news.
"""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stl_transit.core import oracle
from stl_transit.core.diffing import stop_ids as diff_stop_ids
from stl_transit.core.gtfs.departures import departures
from stl_transit.core.rt import merge as rtmerge
from stl_transit.errors import EmptyFeed, UsageError
from stl_transit.io.db import build_sqlite, connect_ro

from . import fixtures

CHI = ZoneInfo("America/Chicago")

# 2026-11-01 is US fall back: 02:00 CDT rewinds to 01:00 CST, so 01:00-02:00
# happens twice. This is the transition that breaks a wall-clock comparison,
# because the clock READING goes backwards while real time moves forwards. On
# spring-forward the reading only jumps forward, and the comparison survives by
# luck -- which is why a test written against March would have passed.
def _dst_feed(tmp_path: Path, name: str = "dst.zip") -> Path:
    """A feed whose service window contains a fall-back night.

    The shipped feed only ever covers about a month, so no snapshot in the
    store can exercise this. That is exactly why the bug survived: the data
    needed to see it does not exist until November.
    """
    return fixtures.build_gtfs_zip(
        tmp_path / name,
        overrides={
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                "start_date,end_date\n"
                "WK,1,1,1,1,1,0,0,20261001,20261130\n"
                "SA,0,0,0,0,0,1,0,20261001,20261130\n"
                "SU,0,0,0,0,0,0,1,20261001,20261130\n"
            ),
            # Header-only, deliberately: a pick with no exceptions is normal,
            # and reading such a feed used to raise OperationalError.
            "calendar_dates.txt": "service_id,date,exception_type\n",
            "routes.txt": (
                "route_id,route_short_name,route_long_name,route_type,route_color\n"
                "R1,1,Late Night Line,3,000000\n"
            ),
            "trips.txt": (
                "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                "R1,SA,T_LATE,Late Night,0\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence,"
                "pickup_type,drop_off_type\n"
                # 26:15 measured from Saturday 2026-10-31's service day start
                # lands at 01:15 CST -- the SECOND pass through 01:00-02:00.
                "T_LATE,26:15:00,26:15:00,S1,1,0,0\n"
            ),
        },
    )


# 01:30 CDT: the FIRST pass through the repeated hour. fold=0 is what makes it
# the first rather than the second, and the distinction is the whole test.
QUERY_AT = datetime(2026, 11, 1, 1, 30, tzinfo=CHI, fold=0)


def _conn(zip_path: Path, tmp_path: Path, name: str) -> sqlite3.Connection:
    db = tmp_path / name
    build_sqlite(zip_path, db)
    return connect_ro(db)


# ---------------------------- 1: the window comparison was still wall-clock --

def test_departure_inside_a_dst_window_is_not_dropped(tmp_path):
    """`window_end` was computed in UTC, but the comparison consuming it was
    not. Two datetimes sharing a tzinfo object compare by WALL CLOCK, so when
    the clock rewinds at 02:00 the reading of a LATER departure (01:15 CST) is
    smaller than the reading of the query time (01:30 CDT). `at <= when` is
    then false and a bus 45 real minutes away vanishes from the board -- no
    error, no warning, just an empty row where a rider expected a bus."""
    conn = _conn(_dst_feed(tmp_path), tmp_path, "dst.sqlite")
    try:
        result = departures(conn, "S1", QUERY_AT, window_minutes=90, tz=CHI)
        assert result["total"] == 1, "the departure 45 real minutes out was dropped"
        item = result["items"][0]
        assert item["gtfs_time"] == "26:15:00"
        assert item["service_date"] == "2026-10-31"
        assert item["departure_local"].startswith("2026-11-01T01:15")
        # The second 01:15, on CST -- not the first, which already passed.
        assert datetime.fromisoformat(item["departure_local"]).utcoffset() == timedelta(hours=-6)
    finally:
        conn.close()


def test_minutes_away_is_real_minutes_across_a_dst_boundary(tmp_path):
    """Wall-clock subtraction made this NEGATIVE: 01:15 minus 01:30 is -15
    minutes, for a bus that has not left yet and is 45 minutes out."""
    conn = _conn(_dst_feed(tmp_path), tmp_path, "dst2.sqlite")
    try:
        item = departures(conn, "S1", QUERY_AT, window_minutes=180, tz=CHI)["items"][0]
        assert item["minutes_away"] == 45

        depart = datetime.fromisoformat(item["departure_local"])
        real = depart.astimezone(timezone.utc) - QUERY_AT.astimezone(timezone.utc)
        assert real == timedelta(minutes=45)
    finally:
        conn.close()


def test_a_departure_outside_the_window_is_still_excluded(tmp_path):
    """The fix must not simply widen the window -- verify the other side."""
    conn = _conn(_dst_feed(tmp_path), tmp_path, "dst3.sqlite")
    try:
        assert departures(conn, "S1", QUERY_AT, window_minutes=30, tz=CHI)["total"] == 0
    finally:
        conn.close()


# --------------------------------------- 2: the import left a poisoned cache --

def test_a_failed_import_leaves_no_database_behind(tmp_path):
    """`sqlite3.connect` creates the file before a single row is read, so a
    corrupt archive left an EMPTY database on disk. Every later call then took
    the `db_path.exists()` fast path and served it as a complete feed. Nothing
    errored; `diff` reported that all 5,118 stops had been deleted."""
    bad = tmp_path / "corrupt.zip"
    bad.write_bytes(b"PK\x03\x04 this is not really a zip file at all")
    db = tmp_path / "out.sqlite"

    with pytest.raises((zipfile.BadZipFile, EmptyFeed)):
        build_sqlite(bad, db)
    assert not db.exists(), "a failed import published a database anyway"
    assert not list(tmp_path.glob("*.building-*")), "temp build file left behind"


def test_an_archive_with_no_gtfs_files_is_refused(tmp_path):
    """An empty result is a broken import, not an agency that deleted every
    stop, and the two must not look the same downstream."""
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.md", "no gtfs here")
    db = tmp_path / "empty.sqlite"

    with pytest.raises(EmptyFeed) as exc:
        build_sqlite(empty, db)
    assert exc.value.remedy
    assert not db.exists()


def test_a_good_import_still_publishes_and_is_reused(mini_zip, tmp_path):
    db = tmp_path / "good.sqlite"
    first = build_sqlite(mini_zip, db)
    assert first["rebuilt"] is True and db.exists()
    assert first["tables"]["stops"] > 0
    second = build_sqlite(mini_zip, db)
    assert second["rebuilt"] is False, "an existing database should be reused"
    assert not list(tmp_path.glob("*.building-*"))


def test_a_failed_rebuild_does_not_destroy_the_existing_database(mini_zip, tmp_path):
    """force=True on a broken archive must not take the good database with it."""
    db = tmp_path / "keep.sqlite"
    build_sqlite(mini_zip, db)
    before = db.read_bytes()

    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(Exception):
        build_sqlite(bad, db, force=True)
    # force unlinks first, so the file may be gone -- but it must never be a
    # zero-byte impostor that later reads treat as a complete feed.
    assert not db.exists() or db.read_bytes() == before


# ------------------------------ 3: absolute-time predictions were discarded --

def _scheduled():
    return {
        "items": [{
            "trip_id": "T1", "stop_id": "S1", "stop_sequence": "3",
            "departure_local": "2026-08-05T12:05:00-05:00", "minutes_away": 5,
        }],
        "total": 1, "warnings": [],
    }


def _feed(event: dict):
    return {
        "header": {"timestamp": 1785783946},
        "entities": [{"trip_update": {
            "trip": {"trip_id": "T1"},
            "stop_time_update": [
                {"stop_id": "S1", "departure": event, "schedule_relationship": "SCHEDULED"}],
        }}],
    }


def test_an_absolute_time_prediction_is_used():
    """GTFS-RT lets a producer give an absolute POSIX `time` instead of a
    `delay`, and the spec treats them as equally valid. The old code recognised
    `time`, returned nothing, and left a comment claiming the caller handled it.
    The caller had no such branch, so the row went out as realtime:true carrying
    the SCHEDULED time -- a scheduled departure labelled live."""
    epoch = int(datetime(2026, 8, 5, 12, 8, tzinfo=CHI).timestamp())  # 3 min late
    out = rtmerge.merge(_scheduled(), _feed({"time": epoch}))
    item = out["items"][0]
    assert item["predicted_local"].startswith("2026-08-05T12:08")
    assert item["delay_seconds"] == 180
    assert item["delay_basis"] == "absolute_time"
    assert item["minutes_away_predicted"] == 8


def test_an_absolute_time_can_be_earlier_than_schedule():
    epoch = int(datetime(2026, 8, 5, 12, 2, tzinfo=CHI).timestamp())
    item = rtmerge.merge(_scheduled(), _feed({"time": epoch}))["items"][0]
    assert item["delay_seconds"] == -180
    assert item["predicted_local"].startswith("2026-08-05T12:02")


def test_an_absurd_absolute_time_is_rejected_like_an_absurd_delay():
    item = rtmerge.merge(_scheduled(), _feed({"time": 4102444800}))["items"][0]
    assert item["status"] == "IMPLAUSIBLE_DELAY"
    assert item["predicted_local"] is None


def test_a_matched_trip_with_no_prediction_says_so():
    """Matching a TripUpdate is not the same as having a prediction. Reporting
    realtime:true with no predicted time invites the caller to render the
    scheduled time as live."""
    item = rtmerge.merge(_scheduled(), _feed({}))["items"][0]
    assert item["status"] == "NO_PREDICTION"
    assert item["predicted_local"] is None
    assert "delay_seconds" not in item


def test_a_plain_delay_still_reports_its_basis():
    item = rtmerge.merge(_scheduled(), _feed({"delay": 120}))["items"][0]
    assert item["delay_basis"] == "delay"
    assert item["predicted_local"].startswith("2026-08-05T12:07")


def test_a_zero_delay_is_a_prediction_not_an_absence():
    """`if event.get("delay")` treated an on-time bus as no data. Zero is the
    single most common delay value there is."""
    item = rtmerge.merge(_scheduled(), _feed({"delay": 0}))["items"][0]
    assert item["delay_seconds"] == 0
    assert item["status"] == "SCHEDULED"
    assert item["predicted_local"].startswith("2026-08-05T12:05")


# ------------------------------------------- 4: green on nothing measured --

def test_a_stopless_feed_cannot_reach_the_diff_at_all(tmp_path):
    """The original repro for the unearned green light was a feed with zero
    stops. That state is now unreachable -- build_sqlite refuses it -- which is
    a better fix than reporting it well downstream. Pinned so a future
    loosening of the import guard does not quietly reopen the path.

    The surviving in-band case, a feed WITH stops but no stop_code column, is
    covered by test_stop_id_survival_needs_stop_code_on_both_sides.
    """
    empty = fixtures.build_gtfs_zip(
        tmp_path / "nostops.zip",
        overrides={"stops.txt": (
            "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,"
            "parent_station,wheelchair_boarding\n")},
    )
    with pytest.raises(EmptyFeed):
        build_sqlite(empty, tmp_path / "a.sqlite")


def test_stop_id_survival_still_passes_on_two_real_feeds(mini_zip, tmp_path):
    a = _conn(mini_zip, tmp_path, "c.sqlite")
    b = _conn(mini_zip, tmp_path, "d.sqlite")
    try:
        result = diff_stop_ids(a, b)
        assert result["measured"] is True
        assert result["meets_assumption"] is True
        assert result["survival_rate_stop_code"] == 1.0
    finally:
        a.close()
        b.close()


def test_oracle_verify_refuses_an_empty_fixtures_directory(tmp_path):
    """`ok: true, checked: 0` meant a scheduled drift check pointed at the wrong
    directory stayed green forever while verifying nothing."""
    empty = tmp_path / "fixtures"
    empty.mkdir()
    with pytest.raises(UsageError) as exc:
        oracle.verify(fixtures_dir=str(empty))
    assert exc.value.remedy


def test_oracle_generate_reports_a_missing_spec_as_usage_not_crash(tmp_path):
    with pytest.raises(UsageError) as exc:
        oracle.generate(spec_path=str(tmp_path / "nope.json"), out_dir=str(tmp_path))
    assert "oracle cases" in exc.value.remedy


# ================= second review pass: what the first round's fixes missed ==

def test_an_unrepresentable_absolute_time_does_not_crash():
    """The epoch was turned into a datetime BEFORE being range-checked, so a
    garbage int64 raised OverflowError straight out of the tool -- the very
    crash the delay guard exists to prevent, reintroduced on the new branch."""
    for epoch in (2**63 - 1, -(2**63), 2**64 - 180, 10**18):
        out = rtmerge.merge(_scheduled(), _feed({"time": epoch}))
        item = out["items"][0]
        assert item["status"] == "IMPLAUSIBLE_DELAY"
        assert item["predicted_local"] is None
        assert out["warnings"]


def test_absolute_time_wins_when_both_time_and_delay_are_present():
    """gtfs-realtime.proto, StopTimeEvent: 'If both time and delay are
    specified, time will take precedence.' Preferring delay because it was
    written first is a silent spec violation."""
    epoch = int(datetime(2026, 8, 5, 12, 20, tzinfo=CHI).timestamp())
    item = rtmerge.merge(_scheduled(), _feed({"time": epoch, "delay": 60}))["items"][0]
    assert item["delay_basis"] == "absolute_time"
    assert item["delay_seconds"] == 900  # from `time`, not the 60 in `delay`
    assert item["predicted_local"].startswith("2026-08-05T12:20")


def test_a_header_only_feed_is_refused(tmp_path):
    """Testing only for the presence of .txt members let an archive of
    header-only CSVs -- what a truncated download produces -- publish a
    zero-row database that the fast path then served as a complete feed."""
    headers = tmp_path / "headers.zip"
    with zipfile.ZipFile(headers, "w") as zf:
        zf.writestr("stops.txt", "stop_id,stop_code,stop_name\n")
        zf.writestr("routes.txt", "route_id,route_short_name\n")
    db = tmp_path / "headers.sqlite"
    with pytest.raises(EmptyFeed) as exc:
        build_sqlite(headers, db)
    assert "zero rows" in str(exc.value)
    assert not db.exists()


def test_a_feed_with_no_stops_is_refused(tmp_path):
    """Every rider-facing feature resolves through stops; a feed without them
    is unusable rather than partially useful."""
    nostops = tmp_path / "nostops.zip"
    with zipfile.ZipFile(nostops, "w") as zf:
        zf.writestr("agency.txt", "agency_id,agency_name\nA,Test\n")
        zf.writestr("stops.txt", "stop_id,stop_code,stop_name\n")
    with pytest.raises(EmptyFeed) as exc:
        build_sqlite(nostops, tmp_path / "ns.sqlite")
    assert "no stops" in str(exc.value)


def test_a_failed_force_rebuild_preserves_the_good_database(mini_zip, tmp_path):
    """The old code unlinked before building, so a failed --force destroyed a
    working database on the way to not producing a new one. replace() is
    atomic; the unlink was never needed."""
    db = tmp_path / "keep2.sqlite"
    build_sqlite(mini_zip, db)
    before = db.read_bytes()

    bad = tmp_path / "bad2.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(Exception):
        build_sqlite(bad, db, force=True)
    assert db.exists(), "a failed rebuild deleted the working database"
    assert db.read_bytes() == before
    assert not list(tmp_path.glob("*.building-*"))


def test_explain_empty_covers_a_twenty_five_hour_day(tmp_path):
    """A fixed 1440-minute probe from midnight stops an hour early on the
    fall-back night, so a stop whose only departure is late in the day reads as
    NO_SERVICE_AT_STOP_TODAY -- a diagnostic tool giving the wrong diagnosis on
    the one night it is hardest to debug."""
    from stl_transit.core.gtfs.departures import explain_empty

    conn = _conn(_dst_feed(tmp_path, "late.zip"), tmp_path, "late.sqlite")
    try:
        # Query a narrow window that legitimately contains nothing; the verdict
        # must be WINDOW_TOO_NARROW, because service does exist that day.
        at = datetime(2026, 11, 1, 5, 0, tzinfo=CHI)
        out = explain_empty(conn, "S1", at, 30, CHI, date(2026, 11, 30))
        assert out["verdict"] == "WINDOW_TOO_NARROW", out["verdict"]
    finally:
        conn.close()


def test_coverage_uses_the_agency_date_not_the_machines(conn, monkeypatch):
    """`datetime.now().date()` is naive local time, so on a UTC host every
    evening after 19:00 Chicago reported tomorrow -- days_remaining, stale_days
    and the expiry warning all off by one for five hours a day."""
    from stl_transit.core.gtfs import inspect as gi

    explicit = gi.coverage(conn, date(2026, 8, 5))
    implicit = gi.coverage(conn)
    # Both must derive their date the same way; the implicit one must not be
    # reading a different clock than the one --as-of plumbs through.
    assert set(explicit) == set(implicit)
    assert "days_remaining" in implicit


@pytest.mark.parametrize(
    "a,b",
    [
        ("2026-06-15T14:00:00-05:00", "2026-06-15T19:00:00+00:00"),
        ("2026-06-15T14:00:00-05:00", "2026-06-15T14:00:00"),
        ("2026-01-15T08:30:00-06:00", "2026-01-15T14:30:00Z"),
    ],
)
def test_device_time_keys_normalise_across_zones(a, b):
    """A device logging UTC writes 19:00Z for the departure the oracle calls
    14:00-05:00. Reading .hour off whichever form arrived made every such row a
    mismatch -- and the function's own docstring promised the opposite."""
    from stl_transit.core.support import _time_key

    assert _time_key(a) == _time_key(b) is not None


def test_stop_id_survival_needs_stop_code_on_both_sides(mini_zip, tmp_path):
    """stop_code is what the app resolves against, so passing on the stop_id
    rate alone while the code rate is null is the same unearned green light,
    one level narrower."""
    nocode = fixtures.build_gtfs_zip(
        tmp_path / "nocode.zip",
        overrides={"stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "S1,First,38.6,-90.2\n"
            "S2,Second,38.7,-90.3\n")},
    )
    a = _conn(nocode, tmp_path, "nc_a.sqlite")
    b = _conn(nocode, tmp_path, "nc_b.sqlite")
    try:
        result = diff_stop_ids(a, b)
        assert result["survival_rate_stop_code"] is None
        assert result["measured"] is False
        assert result["meets_assumption"] is False
    finally:
        a.close()
        b.close()
