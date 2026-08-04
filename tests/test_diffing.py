"""Diffing against two variants of the miniature feed.

Every feed here is `fixtures.build_gtfs_zip` called with different overrides, so
the two snapshots differ ONLY where the test says they do. That is the whole
point: a diff test built from two independently written feeds proves nothing,
because everything differs and you cannot tell which difference the assertion
caught.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from stl_transit.core import diffing
from stl_transit.errors import UsageError
from stl_transit.io.db import build_sqlite, connect_ro

from . import fixtures

# ------------------------------------------------------------- feed variants --

S1_LINE = "S1,15111,Main St & 1st,38.6270,-90.1994,0,,1"
S3_LINE = "S3,15113,Quiet Ln,38.6290,-90.1974,0,,0"

# One degree of latitude is 111,195 m, so these are 24.0 m and 26.0 m due north
# of S1 -- either side of the 25 m default threshold. Computed from the radius,
# not from the module under test.
STOPS_MOVED_24M = fixtures.STOPS.replace(
    S1_LINE, "S1,15111,Main St & 1st,38.6272158,-90.1994,0,,1")
STOPS_MOVED_26M = fixtures.STOPS.replace(
    S1_LINE, "S1,15111,Main St & 1st,38.6272338,-90.1994,0,,1")

# The SAME degree delta applied east instead of north. At 38.6 N that is only
# ~20 m, so it must NOT count as moved -- which is exactly what a naive
# degree-delta comparison would get wrong.
STOPS_MOVED_EAST = fixtures.STOPS.replace(
    S1_LINE, "S1,15111,Main St & 1st,38.6270,-90.1991662,0,,1")

# Same stop, same location, new number on the pole. The saved-stops nightmare.
STOPS_RECODED = fixtures.STOPS.replace(S3_LINE, "S3,25113,Quiet Ln,38.6290,-90.1974,0,,0")
STOPS_RENAMED = fixtures.STOPS.replace(S3_LINE, "S3,15113,Quiet Lane,38.6290,-90.1974,0,,0")
STOPS_RETIRED = fixtures.STOPS.replace(S3_LINE + "\n", "")

ROUTES_RENAMED = fixtures.ROUTES.replace(
    "R11,11,Chippewa,3,000000", "R11,11,Chippewa Trunk,3,000000")
ROUTES_RAIL_GONE = fixtures.ROUTES.replace("MLR,RED,Red Line,0,FF0000\n", "")

CALENDAR_EXTENDED = fixtures.CALENDAR.replace("20260930", "20261231")
CALENDAR_EXTRA_SERVICE = fixtures.CALENDAR + "XH,0,0,0,0,0,0,0,20260101,20260930\n"

# frequencies.txt is absent from the fixture feed, and its appearance is the
# `no_frequencies_file` assumption breaking (spec 6.10).
FREQUENCIES = ("trip_id,start_time,end_time,headway_secs\n"
               "T_WK_1200,12:00:00,13:00:00,600\n")

# T_WK_1200 leaves S1 five minutes later; nothing else changes.
STOP_TIMES_RETIMED = fixtures.STOP_TIMES.replace(
    "T_WK_1200,12:00:00,12:00:00,S1,1,0,0", "T_WK_1200,12:05:00,12:05:00,S1,1,0,0")

TRIPS_REHEADSIGNED = fixtures.TRIPS.replace(
    "R11,WK,T_WK_1200,Chippewa Eastbound,0", "R11,WK,T_WK_1200,Chippewa via Broadway,0")


def _reorder(csv_text: str) -> str:
    """Same rows, different order. Nothing about a diff may depend on it."""
    head, *rows = csv_text.strip().splitlines()
    return "\n".join([head, *reversed(rows)]) + "\n"


@pytest.fixture
def build(tmp_path: Path):
    """Factory for read-only connections to variants of the miniature feed."""
    opened: list[sqlite3.Connection] = []
    counter = itertools.count()

    def _build(overrides: dict[str, str] | None = None) -> sqlite3.Connection:
        n = next(counter)
        zip_path = fixtures.build_gtfs_zip(tmp_path / f"feed{n}.zip", overrides)
        build_sqlite(zip_path, tmp_path / f"feed{n}.sqlite")
        conn = connect_ro(tmp_path / f"feed{n}.sqlite")
        opened.append(conn)
        return conn

    yield _build
    for conn in opened:
        conn.close()


# ------------------------------------------------------------- no-drift base --

def test_identical_feeds_report_zero_drift(build):
    a, b = build(), build()
    assert diffing.files(a, b)["drift_detected"] is False
    assert diffing.routes(a, b)["drift_detected"] is False
    assert diffing.stops(a, b)["drift_detected"] is False
    assert diffing.stop_ids(a, b)["drift_detected"] is False
    assert diffing.calendar(a, b)["drift_detected"] is False

    digest = diffing.summary(a, b)
    assert digest["drift_detected"] is False
    assert digest["findings"] == []
    assert digest["headline"].startswith("No drift")


def test_identical_feeds_survive_everything(build):
    ids = diffing.stop_ids(build(), build())
    assert ids["survival_rate_stop_id"] == 1.0
    assert ids["survival_rate_stop_code"] == 1.0
    assert ids["meets_assumption"] is True


# -------------------------------------------------------------------- routes --

def test_renamed_route_is_a_change_not_an_add_and_a_remove(build):
    r = diffing.routes(build(), build({"routes.txt": ROUTES_RENAMED}))
    assert r["counts"] == {"a": 2, "b": 2, "added": 0, "removed": 0,
                           "changed": 1, "unchanged": 1}
    changed = r["changed"]["items"][0]
    assert changed["route_id"] == "R11"
    assert changed["changes"] == [
        {"field": "route_long_name", "a": "Chippewa", "b": "Chippewa Trunk"}]
    assert changed["type_changed"] is False


def test_a_rename_reads_as_routine_in_the_summary(build):
    digest = diffing.summary(build(), build({"routes.txt": ROUTES_RENAMED}))
    assert digest["drift_detected"] is True
    assert digest["alarming"] == []
    assert any(x["dimension"] == "routes" and x["severity"] == "routine"
               for x in digest["findings"])


def test_a_retired_rail_route_reads_as_alarming(build):
    digest = diffing.summary(build(), build({"routes.txt": ROUTES_RAIL_GONE}))
    assert [x["dimension"] for x in digest["alarming"]] == ["routes"]
    assert "rail_route_ids_stable" in digest["alarming"][0]["detail"]


# --------------------------------------------------------------------- stops --

def test_stop_moved_just_under_threshold_is_not_moved(build):
    s = diffing.stops(build(), build({"stops.txt": STOPS_MOVED_24M}))
    assert s["counts"]["moved"] == 0
    assert s["drift_detected"] is False


def test_stop_moved_just_over_threshold_is_moved(build):
    s = diffing.stops(build(), build({"stops.txt": STOPS_MOVED_26M}))
    assert s["counts"]["moved"] == 1
    item = s["moved"]["items"][0]
    assert item["stop_id"] == "S1"
    # 26 m by construction. A wrong Earth radius or a flat-Earth degree delta
    # would land outside this band.
    assert 25.5 < item["moved_m"] < 26.5


def test_threshold_is_metres_not_degrees(build):
    # The same degree delta east is ~20 m at this latitude, so it stays under a
    # 25 m threshold while the northward version of it does not.
    east = diffing.stops(build(), build({"stops.txt": STOPS_MOVED_EAST}))
    assert east["counts"]["moved"] == 0
    loosened = diffing.stops(build(), build({"stops.txt": STOPS_MOVED_EAST}),
                             moved_threshold_m=15.0)
    assert 20.0 < loosened["moved"]["items"][0]["moved_m"] < 20.6


def test_renamed_stop_is_separate_from_moved(build):
    s = diffing.stops(build(), build({"stops.txt": STOPS_RENAMED}))
    assert (s["counts"]["renamed"], s["counts"]["moved"]) == (1, 0)
    assert (s["counts"]["added"], s["counts"]["removed"]) == (0, 0)
    assert s["renamed"]["items"][0]["name_b"] == "Quiet Lane"


def test_retired_stop_is_removed_not_renamed(build):
    s = diffing.stops(build(), build({"stops.txt": STOPS_RETIRED}))
    assert [x["stop_id"] for x in s["removed"]["items"]] == ["S3"]
    assert s["counts"]["renamed"] == 0


def test_lists_are_hard_capped_regardless_of_requested_limit(build):
    s = diffing.stops(build(), build({"stops.txt": STOPS_RETIRED}), limit=10_000)
    assert s["removed"]["limit_clamped_to"] == 500


# ------------------------------------------------------------------ stop ids --

def test_retired_stop_code_drops_the_survival_rate(build):
    ids = diffing.stop_ids(build(), build({"stops.txt": STOPS_RECODED}))
    # 5 codes in, 4 survive: the stop itself is fine, its number is not.
    assert ids["survival_rate_stop_code"] == 0.8
    assert ids["survival_rate_stop_id"] == 1.0
    assert ids["stop_code"]["lost"] == 1
    assert ids["stop_code"]["gained"] == 1
    assert ids["stop_code"]["lost_sample"] == [
        {"stop_code": "15113", "stop_name": "Quiet Ln"}]
    assert ids["meets_assumption"] is False
    assert ids["verdict"].startswith("ALARMING")


def test_survival_rate_counts_are_reported_beside_the_rate(build):
    ids = diffing.stop_ids(build(), build({"stops.txt": STOPS_RETIRED}))
    assert ids["stop_id"]["in_a"] == 5
    assert ids["stop_id"]["in_b"] == 4
    assert ids["stop_id"]["survived"] == 4
    assert ids["survival_rate_stop_id"] == 0.8


def test_stop_id_churn_is_alarming_in_the_summary(build):
    digest = diffing.summary(build(), build({"stops.txt": STOPS_RECODED}))
    assert any(x["dimension"] == "stop_ids" for x in digest["alarming"])
    assert "BELOW" in digest["headline"]


# ------------------------------------------------------------------ calendar --

def test_extended_service_window_is_a_routine_pick(build):
    c = diffing.calendar(build(), build({"calendar.txt": CALENDAR_EXTENDED}))
    assert c["date_range"]["a"]["end"] == "2026-09-30"
    assert c["date_range"]["b"]["end"] == "2026-12-31"
    assert c["date_range"]["end_shift_days"] == 92
    assert c["date_range"]["start_shift_days"] == 0

    digest = diffing.summary(build(), build({"calendar.txt": CALENDAR_EXTENDED}))
    assert digest["alarming"] == []


def test_shrinking_service_window_is_alarming(build):
    # B is the newer snapshot and covers LESS than the one it replaces.
    digest = diffing.summary(build({"calendar.txt": CALENDAR_EXTENDED}), build())
    assert any(x["dimension"] == "calendar" for x in digest["alarming"])


def test_service_id_churn(build):
    c = diffing.calendar(build(), build({"calendar.txt": CALENDAR_EXTRA_SERVICE}))
    assert c["service_ids"]["added"]["sample"] == ["XH"]
    assert c["service_ids"]["removed"]["count"] == 0
    assert c["service_ids"]["counts"]["common"] == 3


def test_calendar_exceptions_are_compared(build):
    without = fixtures.CALENDAR_DATES.replace("SU,20260907,1\n", "")
    c = diffing.calendar(build(), build({"calendar_dates.txt": without}))
    assert c["exceptions"]["removed"]["sample"] == [
        {"service_id": "SU", "date": "20260907", "exception_type": "1"}]
    assert c["drift_detected"] is True


# --------------------------------------------------------------------- files --

def test_table_present_in_one_feed_only(build):
    a, b = build(), build({"frequencies.txt": FREQUENCIES})
    f = diffing.files(a, b)
    assert [t["table"] for t in f["tables_added"]] == ["frequencies"]
    assert f["tables_added"][0]["rows"] == 1
    # The catalogue entry travels with the finding, so the reader is not left to
    # look up why a new file matters.
    assert "second code path" in f["tables_added"][0]["why_it_matters"]
    assert f["tables_removed"] == []

    # Same pair, other direction.
    assert [t["table"] for t in diffing.files(b, a)["tables_removed"]] == ["frequencies"]


def test_missing_table_does_not_crash_the_other_dimensions(build):
    a, b = build(), build({"frequencies.txt": FREQUENCIES})
    assert diffing.stops(a, b)["drift_detected"] is False
    assert diffing.summary(a, b)["files"]["tables_added"][0]["table"] == "frequencies"


def test_row_count_deltas(build):
    f = diffing.files(build(), build({"stops.txt": STOPS_RETIRED}))
    row = next(r for r in f["rows"] if r["table"] == "stops")
    assert (row["rows_a"], row["rows_b"], row["delta"]) == (5, 4, -1)
    assert row["pct_change"] == -0.2
    assert row["status"] == "changed"
    assert f["totals"]["delta"] == -1


# ------------------------------------------------------------------ schedule --

def test_schedule_without_a_filter_is_a_usage_error(build):
    a, b = build(), build()
    with pytest.raises(UsageError) as exc:
        diffing.schedule(a, b, on=date(2026, 8, 5))
    assert exc.value.exit_code == 2
    assert "--stop" in exc.value.remedy


def test_schedule_for_one_stop_on_one_date(build):
    a = build()
    b = build({"stop_times.txt": STOP_TIMES_RETIMED})
    s = diffing.schedule(a, b, stop="15111", on=date(2026, 8, 5))
    assert s["mode"] == "stop"
    assert s["services_active"] == {"a": ["WK"], "b": ["WK"]}
    assert [x["departure_time"] for x in s["removed"]["items"]] == ["12:00:00"]
    assert [x["departure_time"] for x in s["added"]["items"]] == ["12:05:00"]
    assert s["drift_detected"] is True


def test_schedule_honours_the_date_it_was_asked_about(build):
    # Saturday runs one trip through S1, not the weekday three.
    a, b = build(), build()
    s = diffing.schedule(a, b, stop="15111", on=date(2026, 8, 8))
    assert s["counts"]["a"] == s["counts"]["b"] == 1
    assert s["drift_detected"] is False


def test_schedule_reports_a_reheadsign_as_a_change_not_a_move(build):
    s = diffing.schedule(build(), build({"trips.txt": TRIPS_REHEADSIGNED}),
                         stop="15111", on=date(2026, 8, 5))
    assert s["counts"]["added"] == s["counts"]["removed"] == 0
    assert s["changed"]["items"][0]["changes"] == [
        {"field": "headsign", "a": "Chippewa Eastbound", "b": "Chippewa via Broadway"}]
    assert s["drift_detected"] is True


def test_schedule_catches_a_call_that_stops_accepting_boardings(build):
    # The support case: the stop still exists, the trip still calls, and the app
    # correctly shows nothing because pickup_type flipped to 1.
    no_boarding = fixtures.STOP_TIMES.replace(
        "T_WK_1200,12:00:00,12:00:00,S1,1,0,0", "T_WK_1200,12:00:00,12:00:00,S1,1,1,0")
    s = diffing.schedule(build(), build({"stop_times.txt": no_boarding}),
                         stop="15111", on=date(2026, 8, 5))
    assert s["changed"]["items"][0]["changes"] == [
        {"field": "pickup_type", "a": "0", "b": "1"}]


def test_schedule_by_route_keys_on_time_not_trip_id(build):
    s = diffing.schedule(build(), build({"stop_times.txt": STOP_TIMES_RETIMED}),
                         route_id="R11", on=date(2026, 8, 5))
    assert s["mode"] == "route"
    assert [x["first_departure"] for x in s["removed"]["items"]] == ["12:00:00"]
    assert [x["first_departure"] for x in s["added"]["items"]] == ["12:05:00"]


def test_schedule_says_so_when_the_stop_is_gone(build):
    s = diffing.schedule(build(), build({"stops.txt": STOPS_RETIRED}),
                         stop="15113", on=date(2026, 8, 5))
    assert s["stop_ids"] == {"a": ["S3"], "b": []}
    assert any("feed A only" in n for n in s["notes"])


def test_schedule_without_a_date_says_what_it_pooled(build):
    a, b = build(), build()
    s = diffing.schedule(a, b, stop="15111")
    assert s["filter"]["date"] is None
    assert any("No date given" in n for n in s["notes"])
    assert s["drift_detected"] is False


# ---------------------------------------------------------------- determinism --

def test_output_is_byte_identical_across_runs(build):
    a = build()
    b = build({"stops.txt": STOPS_RECODED, "routes.txt": ROUTES_RENAMED})
    first = json.dumps(diffing.summary(a, b), sort_keys=False)
    second = json.dumps(diffing.summary(a, b), sort_keys=False)
    assert first == second


def test_output_does_not_depend_on_row_order(build):
    # Same feed, rows written in reverse. Any dict-iteration or SQLite-row-order
    # dependence would surface here as spurious drift.
    shuffled = {"stops.txt": _reorder(fixtures.STOPS),
                "routes.txt": _reorder(fixtures.ROUTES),
                "calendar.txt": _reorder(fixtures.CALENDAR)}
    baseline = json.dumps(diffing.summary(build(), build()))
    assert json.dumps(diffing.summary(build(), build(shuffled))) == baseline


def test_moved_list_is_ordered_by_distance(build):
    two_moved = fixtures.STOPS.replace(
        S1_LINE, "S1,15111,Main St & 1st,38.6280,-90.1994,0,,1").replace(
        S3_LINE, "S3,15113,Quiet Ln,38.6295,-90.1974,0,,0")
    s = diffing.stops(build(), build({"stops.txt": two_moved}))
    distances = [x["moved_m"] for x in s["moved"]["items"]]
    assert distances == sorted(distances, reverse=True)
    assert [x["stop_id"] for x in s["moved"]["items"]] == ["S1", "S3"]
