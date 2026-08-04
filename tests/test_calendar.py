from datetime import date, datetime
from zoneinfo import ZoneInfo

from stl_transit.core.gtfs import calendar as cal

CHI = ZoneInfo("America/Chicago")


def test_parse_and_format_roundtrip():
    assert cal.parse_gtfs_time("24:12:00") == 87_120
    assert cal.format_gtfs_time(87_120) == "24:12:00"
    assert cal.parse_gtfs_time("00:00:00") == 0


def test_service_day_start_is_noon_minus_twelve_not_midnight():
    # 2026-03-08 is US spring-forward, so noon-minus-12h is 23:00 on the 7th --
    # an hour BEFORE local midnight. This test previously asserted hour == 0,
    # which is the bug the function's own docstring warns against; it passed
    # because the implementation had it too.
    start = cal.service_day_start(date(2026, 3, 8), CHI)
    assert (start.date(), start.hour) == (date(2026, 3, 7), 23)
    assert start.utcoffset().total_seconds() == -6 * 3600  # still CST


def test_service_day_start_is_midnight_on_a_normal_day():
    start = cal.service_day_start(date(2026, 8, 5), CHI)
    assert (start.date(), start.hour) == (date(2026, 8, 5), 0)


def test_dst_spring_forward_offsets_correctly():
    # A trip at 03:00 GTFS time on spring-forward day is 03:00 local only if
    # you measure from noon-12h; naive midnight arithmetic lands an hour off.
    when = cal.absolute_time(date(2026, 3, 8), 3 * 3600, CHI)
    assert when.hour == 3
    assert when.utcoffset().total_seconds() == -5 * 3600  # CDT


def test_dst_fall_back_offsets_correctly():
    when = cal.absolute_time(date(2026, 11, 1), 3 * 3600, CHI)
    assert when.hour == 3
    assert when.utcoffset().total_seconds() == -6 * 3600  # CST


def test_after_midnight_resolves_to_next_calendar_day():
    when = cal.absolute_time(date(2026, 8, 5), cal.parse_gtfs_time("24:12:00"), CHI)
    assert when.date() == date(2026, 8, 6)
    assert (when.hour, when.minute) == (0, 12)


def test_active_services_weekday(conn):
    info = cal.active_services(conn, date(2026, 8, 5))  # a Wednesday
    assert info["active"] == ["WK"]
    assert info["weekday"] == "wednesday"


def test_active_services_weekend(conn):
    assert cal.active_services(conn, date(2026, 8, 8))["active"] == ["SA"]
    assert cal.active_services(conn, date(2026, 8, 9))["active"] == ["SU"]


def test_calendar_dates_exception_swaps_service(conn):
    # Labor Day 2026-09-07 is a Monday running SUNDAY service.
    info = cal.active_services(conn, date(2026, 9, 7))
    assert info["from_calendar"] == ["WK"]
    assert info["added_by_exception"] == ["SU"]
    assert info["removed_by_exception"] == ["WK"]
    assert info["active"] == ["SU"]


def test_candidate_service_dates_includes_previous_days():
    start = datetime(2026, 8, 6, 0, 15, tzinfo=CHI)
    dates = cal.candidate_service_dates(start, start)
    assert date(2026, 8, 5) in dates
    assert date(2026, 8, 6) in dates
