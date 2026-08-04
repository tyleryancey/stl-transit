"""Miniature synthetic GTFS + protobuf builders.

Small enough to reason about completely, which is the only way to be sure the
calendar math is right rather than merely self-consistent.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

AGENCY = "agency_id,agency_name,agency_url,agency_timezone\nMET,Test Metro,https://example.org,America/Chicago\n"

STOPS = """stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station,wheelchair_boarding
S1,15111,Main St & 1st,38.6270,-90.1994,0,,1
S2,15112,Main St & 2nd,38.6280,-90.1984,0,,1
S3,15113,Quiet Ln,38.6290,-90.1974,0,,0
ST1,90001,Union Station,38.6290,-90.2000,1,,1
ST1P,90002,Union Station Platform,38.6291,-90.2001,0,ST1,1
"""

ROUTES = """route_id,route_short_name,route_long_name,route_type,route_color
R11,11,Chippewa,3,000000
MLR,RED,Red Line,0,FF0000
"""

# WK weekday, SA saturday, SU sunday, XH a holiday-only service
CALENDAR = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WK,1,1,1,1,1,0,0,20260101,20260930
SA,0,0,0,0,0,1,0,20260101,20260930
SU,0,0,0,0,0,0,1,20260101,20260930
"""

# Labor Day 2026-09-07 (a Monday): weekday removed, Sunday service added.
CALENDAR_DATES = """service_id,date,exception_type
WK,20260907,2
SU,20260907,1
"""

TRIPS = """route_id,service_id,trip_id,trip_headsign,direction_id
R11,WK,T_WK_1200,Chippewa Eastbound,0
R11,WK,T_WK_2412,Chippewa Eastbound,0
R11,SA,T_SA_1200,Chippewa Eastbound,0
R11,SU,T_SU_1200,Chippewa Eastbound,0
MLR,WK,T_RAIL_1205,Shiloh-Scott,0
"""

# T_WK_2412 departs at 24:12:00 -- i.e. 00:12 the following calendar day,
# belonging to the PREVIOUS service date. This is the rollover case.
STOP_TIMES = """trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type
T_WK_1200,12:00:00,12:00:00,S1,1,0,0
T_WK_1200,12:10:00,12:10:00,S2,2,0,0
T_WK_1200,12:20:00,12:20:00,S3,3,1,0
T_WK_2412,24:12:00,24:12:00,S1,1,0,0
T_WK_2412,24:22:00,24:22:00,S2,2,0,0
T_SA_1200,12:30:00,12:30:00,S1,1,0,0
T_SU_1200,12:45:00,12:45:00,S1,1,0,0
T_RAIL_1205,12:05:00,12:05:00,S1,1,0,0
T_RAIL_1205,12:15:00,12:15:00,ST1P,2,0,0
"""

FEED_INFO = """feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version
Test Metro,https://example.org,en,20260101,20260930,test-1
"""

FILES = {
    "agency.txt": AGENCY,
    "stops.txt": STOPS,
    "routes.txt": ROUTES,
    "calendar.txt": CALENDAR,
    "calendar_dates.txt": CALENDAR_DATES,
    "trips.txt": TRIPS,
    "stop_times.txt": STOP_TIMES,
    "feed_info.txt": FEED_INFO,
}


def build_gtfs_zip(path: Path, overrides: dict[str, str] | None = None) -> Path:
    files = dict(FILES)
    files.update(overrides or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return path


# ------------------------------------------------------------- protobuf -----

def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def pb_varint(field: int, value: int) -> bytes:
    return tag(field, 0) + varint(value)


def pb_bytes(field: int, value: bytes) -> bytes:
    return tag(field, 2) + varint(len(value)) + value


def pb_string(field: int, value: str) -> bytes:
    return pb_bytes(field, value.encode())


def build_trip_updates_feed(timestamp: int = 1_754_000_000,
                            trip_id: str = "T_WK_1200",
                            stop_id: str = "S1",
                            delay: int = 300) -> bytes:
    """A minimal but structurally real GTFS-RT FeedMessage."""
    header = pb_string(1, "2.0") + pb_varint(2, 0) + pb_varint(3, timestamp)
    trip_desc = pb_string(1, trip_id) + pb_string(5, "R11")
    departure_evt = pb_varint(1, delay)
    stu = pb_varint(1, 1) + pb_string(4, stop_id) + pb_bytes(3, departure_evt) + pb_varint(5, 0)
    trip_update = pb_bytes(1, trip_desc) + pb_bytes(2, stu) + pb_varint(4, timestamp)
    entity = pb_string(1, "entity-1") + pb_bytes(3, trip_update)
    return pb_bytes(1, header) + pb_bytes(2, entity)
