"""Regression tests for the bugs found in the 2026-08-03 audit.

Every test here exists because the bug it covers shipped and the previous
54-test suite did not catch it. Each names the failure mode in its docstring so
a future reader knows what breaking it would mean.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from stl_transit.core.gtfs.calendar import absolute_time, service_day_start
from stl_transit.core.gtfs.entities import escape_like
from stl_transit.core.models import paginate
from stl_transit.core.rt import merge as rtmerge
from stl_transit.core.rt import schema as S
from stl_transit.core.rt import wire

CHI = ZoneInfo("America/Chicago")


# ------------------------------------------------ bug 1: signed protobuf ints

def _varint(value: int) -> bytes:
    """Encode `value` as protobuf does: negatives as 64-bit two's complement."""
    v = value & ((1 << 64) - 1)
    out = bytearray()
    while True:
        byte = v & 0x7F
        v >>= 7
        if v:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes, wire_type: int = 0) -> bytes:
    return _varint((number << 3) | wire_type) + payload


def test_negative_varint_decodes_as_negative():
    """A bus running early carries a negative delay. Read unsigned, -180 becomes
    18446744073709551436 and every downstream time calculation overflows."""
    msg = wire.parse(_field(1, _varint(-180)))
    assert wire.as_int(msg.fields[0]) == -180


def test_unsigned_reader_does_not_apply_the_fixup():
    """Timestamps are uint64. Applying the signed fixup to them would turn a
    legitimate large value negative -- the mirror image of the same mistake."""
    big = (1 << 63) + 5
    msg = wire.parse(_field(1, _varint(big)))
    assert wire.as_uint(msg.fields[0]) == big
    assert wire.as_int(msg.fields[0]) == big - (1 << 64)


@pytest.mark.parametrize("value", [0, 1, -1, 60, -60, 3600, -3600, 2**31 - 1, -(2**31)])
def test_signed_varint_roundtrip(value):
    msg = wire.parse(_field(1, _varint(value)))
    assert wire.as_int(msg.fields[0]) == value


def test_stop_time_event_delay_is_declared_signed():
    """The schema doubles as the Kotlin porting table. If delay is not marked
    signed there, the port repeats this bug on the device."""
    assert S.STOP_TIME_EVENT[1] == ("delay", "int32", False)
    assert S.TRIP_UPDATE[5] == ("delay", "int32", False)
    assert S.FEED_HEADER[3] == ("timestamp", "uint64", False)


# ------------------------------------------- bug 2: service day is not midnight

def test_service_day_start_on_spring_forward():
    """2026-03-08 loses an hour at 02:00. Noon minus twelve hours is 23:00 on
    the 7th, not midnight on the 8th."""
    got = service_day_start(date(2026, 3, 8), CHI)
    assert got == datetime(2026, 3, 7, 23, 0, tzinfo=CHI)
    assert got.utcoffset() == timedelta(hours=-6)


def test_service_day_start_on_fall_back():
    """2026-11-01 repeats an hour. Noon minus twelve hours is 01:00 that day."""
    got = service_day_start(date(2026, 11, 1), CHI)
    assert got.utcoffset() == timedelta(hours=-5)
    assert (got.hour, got.day) == (1, 1)


def test_service_day_start_is_midnight_on_ordinary_days():
    """Every other day of the year the two agree, which is why the bug hid."""
    got = service_day_start(date(2026, 8, 5), CHI)
    assert got == datetime(2026, 8, 5, 0, 0, tzinfo=CHI)


def _elapsed(a, b) -> timedelta:
    """Absolute time between two aware datetimes.

    Plain `a - b` is NOT this. When both operands carry the same tzinfo object
    Python skips the offset adjustment and subtracts wall-clock readings, so a
    span crossing a DST boundary comes out an hour wrong. Converting to UTC
    first is the only way to measure real elapsed time -- the same trap the
    production code has to avoid, which is why it is spelled out here.
    """
    return a.astimezone(timezone.utc) - b.astimezone(timezone.utc)


def test_absolute_time_across_spring_forward():
    """A 26:30 departure on 2026-03-07 is 26.5 real hours after that service day
    began, and lands at 03:30 CDT -- not 02:30, an hour that does not exist."""
    got = absolute_time(date(2026, 3, 7), 26 * 3600 + 30 * 60, CHI)
    start = service_day_start(date(2026, 3, 7), CHI)
    assert _elapsed(got, start) == timedelta(hours=26, minutes=30)
    assert got.utcoffset() == timedelta(hours=-5)  # now on CDT
    assert (got.date(), got.hour, got.minute) == (date(2026, 3, 8), 3, 30)


def test_absolute_time_elapsed_is_exact_on_ordinary_days():
    start = service_day_start(date(2026, 8, 5), CHI)
    got = absolute_time(date(2026, 8, 5), 25 * 3600, CHI)
    assert _elapsed(got, start) == timedelta(hours=25)


def test_absolute_time_across_fall_back():
    """2026-11-01 lives 01:00-02:00 twice. A 25:00 departure on 10-31 is 25 real
    hours after that service day began, which lands on the FIRST pass through
    the repeated hour -- 01:00 still on CDT, not the 01:00 an hour later on CST.

    Getting this wrong shows a rider a bus that already left. Wall-clock
    arithmetic picks the wrong one of the two 01:00s.
    """
    got = absolute_time(date(2026, 10, 31), 25 * 3600, CHI)
    start = service_day_start(date(2026, 10, 31), CHI)
    assert _elapsed(got, start) == timedelta(hours=25)
    assert (got.date(), got.hour) == (date(2026, 11, 1), 1)
    assert got.utcoffset() == timedelta(hours=-5)  # first 01:00, still CDT


def test_absolute_time_reaches_the_second_repeated_hour():
    """26:00 on 10-31 is the SECOND 01:00 -- same wall clock, CST, an hour of
    real time later. A decoder that cannot tell these apart cannot be right."""
    first = absolute_time(date(2026, 10, 31), 25 * 3600, CHI)
    second = absolute_time(date(2026, 10, 31), 26 * 3600, CHI)
    assert (second.date(), second.hour) == (date(2026, 11, 1), 1)
    assert second.utcoffset() == timedelta(hours=-6)  # now CST
    assert _elapsed(second, first) == timedelta(hours=1)


# ------------------------------------------ bug 5/8: pagination tells the truth

def test_truncated_reports_clipping_not_the_request():
    """limit above the hard cap over a short list is not truncation. Flagging it
    trains a reader to ignore the flag where it matters."""
    _, meta = paginate(list(range(10)), offset=0, limit=1000)
    assert meta["truncated"] is False
    assert meta["total"] == 10
    assert meta["has_more"] is False


def test_truncated_is_true_when_rows_remain():
    _, meta = paginate(list(range(100)), offset=0, limit=10)
    assert meta["truncated"] is True
    assert meta["next_offset"] == 10


def test_limit_clamped_is_reported_separately():
    _, meta = paginate(list(range(2000)), offset=0, limit=9999)
    assert meta["limit_clamped_to"] == 500
    assert meta["count"] == 500


# --------------------------------------------------- bug 9: LIKE wildcards

@pytest.mark.parametrize(
    "raw,expected",
    [("Union", "Union"), ("100%", "100\\%"), ("a_b", "a\\_b"), ("\\", "\\\\")],
)
def test_escape_like(raw, expected):
    assert escape_like(raw) == expected


# ------------------------------------- bug 6: the census must not invent fields

def test_walk_refuses_to_descend_into_declared_strings():
    """A stop_id of "15111" re-parses as plausible protobuf. Descending into it
    reports fields that do not exist, and the census flags them as unmodelled --
    the exact signal that is supposed to mean 'investigate before porting'."""
    stop_time_update = _field(4, _varint(5) + b"15111", wire_type=2)
    trip_update = _field(2, _varint(len(stop_time_update)) + stop_time_update, wire_type=2)
    entity = _field(3, _varint(len(trip_update)) + trip_update, wire_type=2)
    feed = _field(2, _varint(len(entity)) + entity, wire_type=2)

    naive = {p for p, _, _ in wire.walk(feed)}
    guarded = {p for p, _, _ in wire.walk(feed, is_scalar=S.is_scalar_path)}
    assert guarded <= naive
    assert all(not S.path_names(p).endswith("15111") for p in guarded)
    assert "?" not in " ".join(S.path_names(p) for p in guarded)


def test_is_scalar_path_classifies_correctly():
    assert S.is_scalar_path("2.3.2.4")           # entity.trip_update.stop_time_update.stop_id
    assert S.is_scalar_path("1.3")               # header.timestamp
    assert not S.is_scalar_path("2.3")           # entity.trip_update -- a real submessage
    assert not S.is_scalar_path("2.3.2")         # ...stop_time_update


# ------------------------------- bug 1 (downstream) + 11: merge is crash-proof

def _scheduled(delay_free: bool = False):
    return {
        "items": [
            {
                "trip_id": "T1",
                "stop_id": "S1",
                "stop_sequence": "3",
                "departure_local": "2026-08-05T12:05:00-05:00",
                "minutes_away": 5,
            }
        ],
        "total": 20,
        "warnings": [],
    }


def _decoded(delay: int):
    return {
        "header": {"timestamp": 1785783946},
        "entities": [
            {
                "trip_update": {
                    "trip": {"trip_id": "T1"},
                    "stop_time_update": [
                        {"stop_id": "S1", "departure": {"delay": delay},
                         "schedule_relationship": "SCHEDULED"}
                    ],
                }
            }
        ],
    }


def test_merge_handles_a_negative_delay():
    out = rtmerge.merge(_scheduled(), _decoded(-180))
    item = out["items"][0]
    assert item["delay_seconds"] == -180
    assert item["predicted_local"] == "2026-08-05T12:02:00-05:00"
    assert item["minutes_away_predicted"] == 2


def test_merge_rejects_an_implausible_delay_without_crashing():
    """The unsigned-decode failure mode produced 1.8e19, which raised
    OverflowError out of datetime.fromtimestamp and took the whole tool down."""
    out = rtmerge.merge(_scheduled(), _decoded(18446744073709551436))
    item = out["items"][0]
    assert item["status"] == "IMPLAUSIBLE_DELAY"
    assert item["predicted_local"] is None
    assert any("outside" in w for w in out["warnings"])


def test_match_rate_denominator_is_the_page_inspected():
    """Reporting 7/10 as a rate over a 20-row total invited the reader to think
    30% of departures had realtime when 70% of the page did."""
    out = rtmerge.merge(_scheduled(), _decoded(60))
    rt = out["realtime"]
    assert rt["departures_inspected"] == 1
    assert rt["matched_departures"] == 1
    assert rt["match_rate"] == 1.0
    assert "this page only" in rt["match_rate_basis"]


def test_merge_matches_on_stop_sequence_when_stop_id_differs():
    """GTFS-RT lets a producer identify a StopTimeUpdate by sequence instead of
    stop_id. departures() dropped stop_sequence, making this branch dead code."""
    decoded = {
        "header": {"timestamp": 1785783946},
        "entities": [
            {
                "trip_update": {
                    "trip": {"trip_id": "T1"},
                    "stop_time_update": [
                        {"stop_sequence": 3, "departure": {"delay": 120},
                         "schedule_relationship": "SCHEDULED"}
                    ],
                }
            }
        ],
    }
    out = rtmerge.merge(_scheduled(), decoded)
    assert out["items"][0]["delay_seconds"] == 120


def test_service_day_gtfs_seconds_are_right_on_a_dst_day():
    """The same wall-clock-subtraction trap, one layer up. 00:30 on a
    spring-forward morning is 01:30 of GTFS time measured from the previous
    service day, because an hour of the night did not happen."""
    from stl_transit.core import service

    r = service.gtfs_service_day(timestamp="2027-03-14T00:30:00")
    prev = next(c for c in r["candidates"] if c["service_date"] == "2027-03-13")
    assert prev["service_day_start"].startswith("2027-03-13T00:00")
    assert prev["gtfs_time"] == "24:30:00"
    assert prev["gtfs_seconds"] == 24 * 3600 + 30 * 60


def test_service_day_gtfs_seconds_are_right_on_an_ordinary_day():
    from stl_transit.core import service

    r = service.gtfs_service_day(timestamp="2026-08-06T00:12:00")
    prev = next(c for c in r["candidates"] if c["service_date"] == "2026-08-05")
    assert prev["gtfs_time"] == "24:12:00"
    assert prev["after_midnight_encoding"] is True
    same = next(c for c in r["candidates"] if c["service_date"] == "2026-08-06")
    assert same["gtfs_time"] == "00:12:00"


def test_merge_without_realtime_says_so():
    out = rtmerge.merge(_scheduled(), None)
    assert out["realtime"]["available"] is False
    assert out["items"][0]["status"] == "SCHEDULED_ONLY"
    assert any("scheduled" in w.lower() for w in out["warnings"])
