from stl_transit.core import service
from stl_transit.core.rt import decode, merge

from . import fixtures


def test_merge_applies_delay(store):
    sched = service.gtfs_departures("15111", at="2026-08-05T11:30:00", window_minutes=60,
                                    store=store)
    blob = fixtures.build_trip_updates_feed(trip_id="T_WK_1200", stop_id="S1", delay=300)
    merged = merge.merge(sched, decode.decode_feed(blob))
    tracked = [i for i in merged["items"] if i["trip_id"] == "T_WK_1200"][0]
    assert tracked["realtime"] is True
    assert tracked["delay_seconds"] == 300
    assert tracked["minutes_away_predicted"] == tracked["minutes_away"] + 5
    assert merged["realtime"]["matched_departures"] == 1


def test_untracked_trip_marked_scheduled_only(store):
    sched = service.gtfs_departures("15111", at="2026-08-05T11:30:00", window_minutes=60,
                                    store=store)
    blob = fixtures.build_trip_updates_feed(trip_id="T_WK_1200")
    merged = merge.merge(sched, decode.decode_feed(blob))
    rail = [i for i in merged["items"] if i["route_id"] == "MLR"][0]
    assert rail["status"] == "SCHEDULED_ONLY"


def test_absent_realtime_degrades_loudly(store):
    sched = service.gtfs_departures("15111", at="2026-08-05T11:30:00", window_minutes=60,
                                    store=store)
    merged = merge.merge(sched, None)
    assert merged["realtime"]["available"] is False
    assert any("scheduled" in w.lower() for w in merged["warnings"])
    assert all(i["status"] == "SCHEDULED_ONLY" for i in merged["items"])
