"""`support` group tests.

`repro` runs against the miniature feed and a hand-encoded protobuf frame, so
"what should the app have shown at 11:47 last Tuesday" is answered here with no
device, no network, and no waiting for Tuesday -- which is the whole claim the
group makes.

`diff_device` is tested mostly on its forgiveness. Its one job is to accept
whatever a person pastes out of a device log, so the shapes below (bare list,
`items` dict, camelCase keys, a JSON string, a single object, junk) are the
test: a support tool that rejects the paste has failed before it started.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from stl_transit.core import assertions, support
from stl_transit.core.rt import decode as rtdecode
from stl_transit.io.clock import AGENCY_TZ

from . import fixtures

# Wednesday 2026-08-05, inside the mini feed's 2026-01-01..2026-09-30 window.
# 11:30 catches route 11 at 12:00 and the rail trip at 12:05.
AT = datetime(2026, 8, 5, 11, 30, tzinfo=AGENCY_TZ)
DEAD_OF_NIGHT = datetime(2026, 8, 5, 3, 0, tzinfo=AGENCY_TZ)
AFTER_MIDNIGHT = datetime(2026, 8, 6, 0, 5, tzinfo=AGENCY_TZ)
PAST_EXPIRY = datetime(2026, 12, 1, 12, 0, tzinfo=AGENCY_TZ)
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=AGENCY_TZ)
FEED_END = date(2026, 9, 30)


def rt_frame(trip_id: str = "T_WK_1200", delay: int = 300) -> dict:
    return rtdecode.decode_feed(
        fixtures.build_trip_updates_feed(trip_id=trip_id, stop_id="S1", delay=delay)
    )


# --------------------------------------------------------------------- repro --

def test_repro_returns_the_expected_departures(conn):
    out = support.repro(conn, "15111", AT, 60, AGENCY_TZ, feed_end=FEED_END)
    assert out["empty"] is False
    assert out["count"] == 2
    assert [(r["route"], r["gtfs_time"]) for r in out["render"]] == [
        ("11", "12:00:00"), ("RED", "12:05:00")]
    assert out["reason"] is None
    assert out["query"]["realtime_supplied"] is False
    # The full structure travels too: which service_ids were active is the first
    # thing anyone asks when Python and the app disagree.
    assert "2026-08-05" in out["expected"]["calendars"]


def test_repro_render_carries_both_identifiers_for_every_row(conn):
    out = support.repro(conn, "15111", AT, 60, AGENCY_TZ)
    for row in out["render"]:
        assert row["stop_id"] == "S1"
        assert row["stop_code"] == "15111"   # the number the rider actually typed
        assert row["route"] and row["route_id"]


def test_repro_without_a_realtime_frame_degrades_and_says_so(conn):
    out = support.repro(conn, "15111", AT, 60, AGENCY_TZ)
    assert out["realtime"]["available"] is False
    assert all(row["realtime"] is False for row in out["render"])
    assert all(row["status"] == "SCHEDULED_ONLY" for row in out["render"])
    assert any("Realtime unavailable" in w for w in out["warnings"])
    assert "was not supplied" in out["verdict"]


def test_repro_with_a_realtime_frame_applies_the_prediction(conn):
    out = support.repro(conn, "15111", AT, 60, AGENCY_TZ, rt_decoded=rt_frame(delay=300))
    assert out["realtime"]["available"] is True
    assert out["realtime"]["matched_departures"] == 1
    late = [r for r in out["render"] if r["trip_id"] == "T_WK_1200"][0]
    assert late["realtime"] is True
    assert late["predicted_departure"].startswith("2026-08-05T12:05")
    assert late["departure"] == late["predicted_departure"]
    assert late["scheduled_departure"].startswith("2026-08-05T12:00")
    assert late["minutes_away"] == 35
    # The unmatched trip still renders, marked as schedule-only.
    other = [r for r in out["render"] if r["trip_id"] == "T_RAIL_1205"][0]
    assert other["realtime"] is False


def test_repro_orders_the_board_by_when_the_bus_actually_arrives(conn):
    # A 20-minute delay pushes the 12:00 bus behind the 12:05 train.
    out = support.repro(conn, "15111", AT, 60, AGENCY_TZ, rt_decoded=rt_frame(delay=1200))
    assert [r["trip_id"] for r in out["render"]] == ["T_RAIL_1205", "T_WK_1200"]
    assert [r["departure"] for r in out["render"]] == sorted(
        r["departure"] for r in out["render"])


def test_repro_on_an_empty_window_returns_the_reason(conn):
    out = support.repro(conn, "15111", DEAD_OF_NIGHT, 30, AGENCY_TZ, feed_end=FEED_END)
    assert out["empty"] is True
    assert out["render"] == []
    assert out["reason"]["verdict"] == "WINDOW_TOO_NARROW"
    assert out["remedy"] == out["reason"]["remedy"]
    assert "WINDOW_TOO_NARROW" in out["verdict"]
    assert any(c["check"] == "service_on_this_date" for c in out["reason"]["checks"])


def test_repro_on_an_expired_feed_names_that_branch(conn):
    out = support.repro(conn, "15111", PAST_EXPIRY, 60, AGENCY_TZ, feed_end=FEED_END)
    assert out["empty"] is True
    assert out["reason"]["verdict"] == "FEED_EXPIRED"
    assert any("past the feed's last service date" in w for w in out["warnings"])


def test_repro_on_an_unknown_stop_answers_instead_of_raising(conn):
    # "The number I typed showed nothing" is the complaint; a support tool that
    # refuses the input has declined the report it exists for.
    out = support.repro(conn, "99999", AT, 60, AGENCY_TZ, feed_end=FEED_END)
    assert out["ok"] is True
    assert out["empty"] is True
    assert out["reason"]["verdict"] == "STOP_NOT_FOUND"
    assert out["stop_error"]["code"] == "STOP_NOT_FOUND"
    assert out["stop_error"]["remedy"]


def test_repro_finds_a_24xx_departure_from_after_midnight(conn):
    out = support.repro(conn, "15111", AFTER_MIDNIGHT, 30, AGENCY_TZ, feed_end=FEED_END)
    assert [r["gtfs_time"] for r in out["render"]] == ["24:12:00"]
    assert out["render"][0]["service_date"] == "2026-08-05"
    assert out["render"][0]["departure"].startswith("2026-08-06T00:12")


def test_repro_is_deterministic(conn):
    args = (conn, "15111", AT, 60, AGENCY_TZ)
    first = support.repro(*args, rt_decoded=rt_frame())
    second = support.repro(*args, rt_decoded=rt_frame())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --------------------------------------------------------------- diff_device --

def oracle(conn) -> list[dict]:
    return support.repro(conn, "15111", AT, 60, AGENCY_TZ)["render"]


def test_diff_device_accepts_a_bare_list(conn):
    actual = [
        {"route": "11", "headsign": "Chippewa Eastbound", "time": "12:00",
         "trip_id": "T_WK_1200"},
        {"route": "RED", "headsign": "Shiloh-Scott", "time": "12:05",
         "trip_id": "T_RAIL_1205"},
    ]
    out = support.diff_device(oracle(conn), actual)
    assert out["match"] is True
    assert out["counts"] == {"expected": 2, "actual": 2, "matched": 2, "missing": 0,
                             "extra": 0, "differing": 0}
    assert out["key"] == "trip_id"
    assert out["field_map"]["actual"]["departure"] == "time"


def test_diff_device_accepts_a_dict_with_an_items_key(conn):
    actual = {"stop": "15111", "items": [
        {"route_short_name": "11", "headsign": "Chippewa Eastbound",
         "departure_local": "2026-08-05T12:00:00-05:00", "trip_id": "T_WK_1200"},
        {"route_short_name": "RED", "headsign": "Shiloh-Scott",
         "departure_local": "2026-08-05T12:05:00-05:00", "trip_id": "T_RAIL_1205"},
    ]}
    out = support.diff_device(oracle(conn), actual)
    assert out["match"] is True
    assert any("'items' key" in a for a in out["assumptions"])


def test_diff_device_accepts_camel_case_and_other_key_names(conn):
    actual = {"departures": [
        {"routeShortName": "R11", "tripHeadsign": "Chippewa Eastbound",
         "departureTime": "2026-08-05T12:00:00-05:00", "tripId": "T_WK_1200",
         "stopId": "S1"},
        {"routeShortName": "MLR", "tripHeadsign": "Shiloh-Scott",
         "departureTime": "2026-08-05T12:05:00-05:00", "tripId": "T_RAIL_1205",
         "stopId": "S1"},
    ]}
    out = support.diff_device(oracle(conn), actual)
    assert out["match"] is True
    # route_id against route_short_name is not a wrong route, and saying so is
    # the difference between a useful diff and a false alarm.
    assert any("alternate spelling" in a for a in out["assumptions"])
    assert out["field_map"]["actual"]["headsign"] == "tripHeadsign"


def test_diff_device_accepts_a_json_string(conn):
    actual = json.dumps([
        {"route": "11", "destination": "Chippewa Eastbound", "when": "12:00"},
        {"route": "RED", "destination": "Shiloh-Scott", "when": "12:05"},
    ])
    out = support.diff_device(oracle(conn), actual)
    assert out["match"] is True
    assert any("parsed as a JSON string" in a for a in out["assumptions"])
    # No trip_id on the device side, so it falls back to time plus route.
    assert out["key"] == "departure_route"


def test_diff_device_accepts_a_single_object(conn):
    single = {"route": "11", "headsign": "Chippewa Eastbound", "time": "12:00",
              "trip_id": "T_WK_1200"}
    out = support.diff_device(oracle(conn), single)
    assert out["counts"]["actual"] == 1
    assert out["counts"]["matched"] == 1
    assert any("wrapped it" in a for a in out["assumptions"])


def test_diff_device_accepts_the_whole_repro_result_as_expected(conn):
    whole = support.repro(conn, "15111", AT, 60, AGENCY_TZ)
    out = support.diff_device(whole, whole["render"])
    assert out["match"] is True
    assert out["counts"]["expected"] == 2


def test_diff_device_detects_a_differing_field(conn):
    actual = [
        {"route": "11", "headsign": "Chippewa WESTbound", "time": "12:00",
         "trip_id": "T_WK_1200"},
        {"route": "RED", "headsign": "Shiloh-Scott", "time": "12:05",
         "trip_id": "T_RAIL_1205"},
    ]
    out = support.diff_device(oracle(conn), actual)
    assert out["match"] is False
    assert out["counts"]["differing"] == 1
    pair = out["differing"][0]
    assert pair["differing_fields"] == ["headsign"]
    field = [f for f in pair["fields"] if f["field"] == "headsign"][0]
    assert field["expected"] == "Chippewa Eastbound"
    assert field["actual"] == "Chippewa WESTbound"
    assert "headsign" in out["verdict"]
    assert out["remedy"]


def test_diff_device_detects_a_missing_departure(conn):
    actual = [{"route": "11", "headsign": "Chippewa Eastbound", "time": "12:00",
               "trip_id": "T_WK_1200"}]
    out = support.diff_device(oracle(conn), actual)
    assert out["counts"]["missing"] == 1
    assert out["counts"]["extra"] == 0
    assert out["missing"][0]["expected"]["trip_id"] == "T_RAIL_1205"
    assert "MISSING" in out["verdict"]
    assert "service-day" in out["remedy"]


def test_diff_device_detects_an_extra_departure(conn):
    actual = oracle(conn) + [{"route": "70", "headsign": "Grand", "time": "12:07",
                              "trip_id": "T_GHOST"}]
    out = support.diff_device(oracle(conn), actual)
    assert out["counts"]["extra"] == 1
    assert out["extra"][0]["actual"]["trip_id"] == "T_GHOST"
    assert "snapshot list" in out["remedy"]


def test_diff_device_matches_a_device_that_logs_the_stop_code(conn):
    # The stop_code-vs-stop_id trap, arriving as a diff: the oracle is keyed on
    # the internal stop_id and the device logged the number the rider typed.
    actual = [{"route": "11", "stop": "15111", "time": "12:00", "trip_id": "T_WK_1200"},
              {"route": "RED", "stop": "15111", "time": "12:05", "trip_id": "T_RAIL_1205"}]
    out = support.diff_device(oracle(conn), actual)
    assert out["match"] is True
    assert any("stop_id: matched via an alternate spelling" in a for a in out["assumptions"])


def test_diff_device_treats_2412_and_0012_as_one_departure(conn):
    expected = support.repro(conn, "15111", AFTER_MIDNIGHT, 30, AGENCY_TZ)["render"]
    out = support.diff_device(expected, [{"route": "11", "time": "24:12",
                                          "trip_id": "T_WK_2412"}])
    assert out["match"] is True


def test_diff_device_reports_what_it_could_not_map(conn):
    actual = [{"route": "11", "time": "12:00", "trip_id": "T_WK_1200",
               "vehicleBatteryPercent": 71},
              {"route": "RED", "time": "12:05", "trip_id": "T_RAIL_1205"}]
    out = support.diff_device(oracle(conn), actual)
    assert out["unmapped_fields"] == ["vehicleBatteryPercent"]
    assert any("no canonical meaning" in a for a in out["assumptions"])
    assert out["match"] is True, "an unknown extra key is not a mismatch"


def test_diff_device_never_raises_on_junk(conn):
    for junk in (42, None, "not json at all", {"totally": "unrelated"}, [1, 2, 3]):
        out = support.diff_device(oracle(conn), junk)
        assert out["counts"]["actual"] == 0
        assert out["assumptions"], "every guess is stated"
        assert out["remedy"]


def test_diff_device_calls_two_empty_boards_a_match(conn):
    out = support.diff_device([], [])
    assert out["match"] is True
    assert "Both sides are empty" in out["verdict"]
    assert out["remedy"] is None


def test_diff_device_is_deterministic(conn):
    actual = [{"route": "11", "headsign": "Wrong", "time": "12:00",
               "trip_id": "T_WK_1200"}]
    first = support.diff_device(oracle(conn), actual)
    second = support.diff_device(oracle(conn), actual)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ------------------------------------------------------------ bundle_report --

STORE = {
    "snapshots": 2,
    "store_root": "/tmp/stl-store",
    "pins": {"baseline": "gtfs-20260701T000000Z-aaaaaa"},
    "items": [{"snapshot_id": "gtfs-20260803T120000Z-a3f9c1", "kind": "gtfs"},
              {"snapshot_id": "rt-20260803T115900Z-b12345", "kind": "rt"}],
}
CONFIG = {
    "config_path": "/tmp/stl-transit/data/sources.toml",
    "items": [{"source": "metro_gtfs"}, {"source": "metro_rt_trips"}],
    "pages": ["fares", "holidays"],
}
VERSIONS = {"stl": "0.1.0", "python": "3.12.4", "sqlite": "3.45.1"}


def test_bundle_report_builds_the_files_service_will_zip(conn):
    out = support.bundle_report(STORE, CONFIG, VERSIONS, assertions.run(conn, as_of=NOW),
                                NOW)
    assert out["file_names"] == ["README.md", "manifest.json", "snapshots.json",
                                 "config.json", "assertions.json"]
    for entry in out["files"]:
        assert entry["content"]
        assert entry["bytes"] == len(entry["content"].encode("utf-8"))
        assert len(entry["sha256"]) == 64
    assert out["total_bytes"] == sum(f["bytes"] for f in out["files"])
    assert out["manifest"]["snapshot_ids"] == sorted(
        s["snapshot_id"] for s in STORE["items"])
    assert out["manifest"]["versions"] == VERSIONS
    # The JSON files are canonical, so two bundles diff cleanly.
    manifest = [f for f in out["files"] if f["name"] == "manifest.json"][0]
    assert json.loads(manifest["content"])["generated_at"] == NOW.isoformat()


def test_bundle_report_says_it_is_safe_to_paste_and_checked_why(conn):
    out = support.bundle_report(STORE, CONFIG, VERSIONS, assertions.run(conn, as_of=NOW),
                                NOW)
    assert out["safe_to_publish"] is True
    assert out["redaction"]["redacted"] == []
    assert "no authentication anywhere" in out["redaction"]["why"]
    assert out["redaction"]["findings"]["suspicious_keys"] == []
    readme = out["files"][0]["content"]
    assert "safe to paste" in out["redaction"]["why"]
    assert "Is this safe to paste in public?" in readme
    # Claimed AND checked: an assertion of safety nobody verified is worth less
    # than a scan that came back empty.
    assert "checked rather than claimed" in readme


def test_bundle_report_flags_a_secret_shaped_key(conn):
    config = dict(CONFIG, http={"api_key": "should-not-exist-in-this-project"})
    out = support.bundle_report(STORE, config, VERSIONS, None, NOW)
    assert out["safe_to_publish"] is False
    assert out["redaction"]["findings"]["suspicious_keys"] == ["config.http.api_key"]
    assert any("secret-shaped" in w for w in out["warnings"])
    assert "Read the warnings before attaching this" in out["files"][0]["content"]


def test_bundle_report_flags_a_home_directory_path(conn):
    store = dict(STORE, store_root="/Users/someone/.local/share/stl-transit")
    out = support.bundle_report(store, CONFIG, VERSIONS, None, NOW)
    # Not a secret, but it publishes a username -- say so rather than let the
    # reader find out after they hit Comment.
    assert out["safe_to_publish"] is True
    assert out["redaction"]["findings"]["local_paths"]
    assert any("username" in w for w in out["warnings"])


def test_bundle_report_without_assertions_says_they_were_not_run(conn):
    out = support.bundle_report(STORE, CONFIG, VERSIONS, None, NOW)
    payload = json.loads(
        [f for f in out["files"] if f["name"] == "assertions.json"][0]["content"])
    assert payload["run"] is False
    assert payload["remedy"].startswith("Run `stl assert run`")
    assert out["manifest"]["assertions_included"] is False
    assert any("No recent assertion results" in w for w in out["warnings"])
    assert "NOT RUN" in out["files"][0]["content"]


def test_bundle_report_is_deterministic(conn):
    result = assertions.run(conn, as_of=NOW)
    first = support.bundle_report(STORE, CONFIG, VERSIONS, result, NOW)
    second = support.bundle_report(STORE, CONFIG, VERSIONS, result, NOW)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert [f["sha256"] for f in first["files"]] == [f["sha256"] for f in second["files"]]
