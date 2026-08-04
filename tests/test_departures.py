from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stl_transit.core.gtfs import departures as dep
from stl_transit.errors import StopNotFound

CHI = ZoneInfo("America/Chicago")


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=CHI)


def test_resolves_by_stop_code_first(conn):
    r = dep.resolve_stop(conn, "15111")
    assert r["matched_by"] == "stop_code"
    assert r["stop_ids"] == ["S1"]


def test_resolves_by_stop_id_fallback(conn):
    assert dep.resolve_stop(conn, "S2")["matched_by"] == "stop_id"


def test_station_includes_child_platforms(conn):
    r = dep.resolve_stop(conn, "90001")
    assert set(r["stop_ids"]) == {"ST1", "ST1P"}


def test_unknown_stop_raises_with_remedy(conn):
    with pytest.raises(StopNotFound) as exc:
        dep.resolve_stop(conn, "99999")
    assert exc.value.remedy


def test_weekday_departures(conn):
    r = dep.departures(conn, "15111", at(2026, 8, 5, 11, 30), 60, CHI)
    times = [(i["route_short_name"], i["gtfs_time"]) for i in r["items"]]
    assert ("RED", "12:05:00") in times
    assert ("11", "12:00:00") in times
    assert r["total"] == 2


def test_results_are_time_sorted(conn):
    r = dep.departures(conn, "15111", at(2026, 8, 5, 11, 30), 60, CHI)
    stamps = [i["departure_local"] for i in r["items"]]
    assert stamps == sorted(stamps)


def test_no_pickup_stop_is_excluded(conn):
    # S3 has pickup_type=1 on the only trip calling there: a rider cannot board.
    r = dep.departures(conn, "15113", at(2026, 8, 5, 12, 0), 60, CHI)
    assert r["total"] == 0


def test_after_midnight_query_finds_previous_service_date(conn):
    # Queried at 00:05 on Aug 6, the 24:12 departure belongs to Aug 5.
    r = dep.departures(conn, "15111", at(2026, 8, 6, 0, 5), 30, CHI)
    assert r["total"] == 1
    item = r["items"][0]
    assert item["gtfs_time"] == "24:12:00"
    assert item["service_date"] == "2026-08-05"
    assert item["after_midnight"] is True
    assert item["departure_local"].startswith("2026-08-06T00:12")


def test_late_evening_window_spanning_midnight(conn):
    r = dep.departures(conn, "15111", at(2026, 8, 5, 23, 50), 60, CHI)
    assert [i["gtfs_time"] for i in r["items"]] == ["24:12:00"]


def test_saturday_uses_saturday_service(conn):
    r = dep.departures(conn, "15111", at(2026, 8, 8, 12, 0), 60, CHI)
    assert [i["service_id"] for i in r["items"]] == ["SA"]


def test_labor_day_monday_runs_sunday_service(conn):
    # The holiday case: a Monday, but calendar_dates swaps WK out for SU.
    r = dep.departures(conn, "15111", at(2026, 9, 7, 12, 0), 120, CHI)
    assert r["total"] == 1
    assert r["items"][0]["service_id"] == "SU"
    assert r["items"][0]["gtfs_time"] == "12:45:00"


def test_ordinary_monday_still_runs_weekday_service(conn):
    # Guards against over-eager holiday logic swallowing normal Mondays.
    r = dep.departures(conn, "15111", at(2026, 9, 14, 11, 30), 60, CHI)
    assert {i["service_id"] for i in r["items"]} == {"WK"}


def test_empty_window_returns_empty_not_error(conn):
    r = dep.departures(conn, "15111", at(2026, 8, 5, 3, 0), 30, CHI)
    assert r["total"] == 0
    assert r["items"] == []


def test_route_filter(conn):
    r = dep.departures(conn, "15111", at(2026, 8, 5, 11, 30), 60, CHI, route_filter="MLR")
    assert {i["route_id"] for i in r["items"]} == {"MLR"}


def test_calendars_are_exposed_for_diagnosis(conn):
    r = dep.departures(conn, "15111", at(2026, 9, 7, 12, 0), 120, CHI)
    assert "2026-09-07" in r["calendars"]
    assert r["calendars"]["2026-09-07"]["removed_by_exception"] == ["WK"]


def test_explain_empty_names_the_branch(conn):
    from datetime import date

    out = dep.explain_empty(conn, "15111", at(2026, 8, 5, 3, 0), 30, CHI,
                            date(2026, 9, 30))
    assert out["verdict"] == "WINDOW_TOO_NARROW"

    out = dep.explain_empty(conn, "15111", at(2026, 12, 1, 12, 0), 60, CHI,
                            date(2026, 9, 30))
    assert out["verdict"] == "FEED_EXPIRED"
