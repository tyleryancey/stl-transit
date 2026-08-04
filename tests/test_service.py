"""Service-layer tests: the exact functions the MCP tools wrap."""
from __future__ import annotations

import pytest

from stl_transit.core import service
from stl_transit.errors import UnsafeQuery


def test_stats(store):
    out = service.gtfs_stats(store=store)
    assert out["ok"]
    assert out["routes"] == 2
    assert out["stops"] == 5
    assert out["routes_by_type"] == {"bus": 1, "tram/streetcar": 1}


def test_files_reports_absent_optional_files(store):
    out = service.gtfs_files(store=store)
    absent = {a["file"] for a in out["absent_optional"]}
    assert "transfers.txt" in absent
    assert "fare_attributes.txt" in absent
    assert "frequencies.txt" in absent


def test_features_detect_presence_and_absence(store):
    feats = {f["feature"]: f["present"] for f in service.gtfs_features(store=store)["features"]}
    assert feats["Route Colors"] is True
    assert feats["Fares v1"] is False
    assert feats["Frequency-Based Service"] is False


def test_coverage_and_provenance(store):
    out = service.gtfs_coverage(as_of=None, store=store)
    assert out["service_start"] == "2026-01-01"
    assert out["service_end"] == "2026-09-30"
    assert out["provenance"]["snapshot_id"].startswith("gtfs-")


def test_stop_resolve_verdict(store):
    out = service.gtfs_stop_resolve(store=store)
    assert out["verdict"]["rider_facing_field"] == "stop_code"
    assert out["published_example"]["found_in"][0]["field"] == "stop_code"


def test_query_returns_rows(store):
    out = service.gtfs_query("SELECT route_id FROM routes ORDER BY route_id", store=store)
    assert [r["route_id"] for r in out["rows"]] == ["MLR", "R11"]


def test_query_rejects_writes(store):
    with pytest.raises(UnsafeQuery):
        service.gtfs_query("DELETE FROM routes", store=store)


def test_query_rejects_attach(store):
    with pytest.raises(UnsafeQuery):
        service.gtfs_query("ATTACH DATABASE '/tmp/x.db' AS x", store=store)


def test_query_rejects_pragma(store):
    with pytest.raises(UnsafeQuery):
        service.gtfs_query("PRAGMA table_list", store=store)


def test_query_rejects_multiple_statements(store):
    with pytest.raises(UnsafeQuery):
        service.gtfs_query("SELECT 1; DROP TABLE routes", store=store)


def test_departures_through_service_layer(store):
    out = service.gtfs_departures("15111", at="2026-08-05T11:30:00", window_minutes=60,
                                  store=store)
    assert out["ok"]
    assert out["total"] == 2
    assert out["provenance"]["stale_days"] < 0  # feed still valid


def test_service_day_exposes_both_candidates():
    out = service.gtfs_service_day("2026-08-06T00:12:00")
    plausible = [c for c in out["candidates"] if c["plausible"]]
    assert {c["service_date"] for c in plausible} == {"2026-08-05", "2026-08-06"}
    prev = [c for c in out["candidates"] if c["service_date"] == "2026-08-05"][0]
    assert prev["gtfs_time"] == "24:12:00"
    assert prev["after_midnight_encoding"] is True


def test_late_night_finds_the_rollover_trip(store):
    out = service.gtfs_late_night(store=store)
    assert out["distinct_trips"] == 1
    assert out["max_departure_time"] == "24:22:00"


def test_support_explain_empty(store):
    out = service.support_explain_empty("15111", at="2026-08-05T03:00:00", window_minutes=30,
                                        store=store)
    assert out["verdict"] == "WINDOW_TOO_NARROW"
    assert any(c["check"] == "stop_resolves" and c["passed"] for c in out["checks"])


def test_pagination_metadata(store):
    out = service.gtfs_stops(limit=2, store=store)
    assert out["count"] == 2
    assert out["total"] == 5
    assert out["has_more"] is True
    assert out["next_offset"] == 2


def test_sources_reports_blocked_illinois_feed(store):
    out = service.snapshot_sources(store=store)
    by_name = {i["source"]: i for i in out["items"]}
    assert by_name["mct_gtfs"]["usable"] is False
    assert "Transitland" in by_name["mct_gtfs"]["blocked_reason"]
    assert by_name["metro_gtfs"]["usable"] is True
    assert by_name["metro_gtfs"]["region"] == "MO+IL"
