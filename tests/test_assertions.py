"""Assumption-suite tests.

The suite's whole value is that a red run tells you the size of the problem,
so these tests assert on the OBSERVED VALUES as much as on pass/fail. Every
failure below is induced by building a deliberately broken miniature feed --
small enough to know exactly which assumption should notice and which should
stay quiet. No network.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from stl_transit.core import assertions
from stl_transit.errors import UsageError
from stl_transit.io.clock import AGENCY_TZ
from stl_transit.io.db import build_sqlite, connect_ro

from . import fixtures

# The mini feed runs 2026-01-01..2026-09-30, so a fixed as_of keeps
# feed_not_expiring deterministic instead of depending on the wall clock.
AS_OF = datetime(2026, 8, 3, 12, 0, tzinfo=AGENCY_TZ)
NEAR_EXPIRY = datetime(2026, 9, 28, 12, 0, tzinfo=AGENCY_TZ)

# Every id in spec 6.10, written out rather than derived from the TOML: the
# spec table is the requirement and the config is the implementation of it.
SPEC_IDS = {
    "stop_code_present", "stop_code_unique", "stop_code_format", "stop_ids_stable",
    "feed_not_expiring", "no_frequencies_file", "max_time_bounded", "timezone_unchanged",
    "rail_route_ids_stable", "rt_fresh", "rt_join_rate", "rt_wire_shape",
    "fares_unchanged", "holidays_unchanged", "terms_unchanged", "no_fare_files",
}

WEB_HASHES = {"fares": "f" * 64, "holidays": "h" * 64, "developer_terms": "t" * 64}


def rt_sample(age: float = 30.0, trip_ids=("T_WK_1200", "T_SA_1200"),
              unmodelled=(), entities=("trip_updates", "vehicle_positions", "alerts")):
    return {
        entity: {
            "header_timestamp": 1_754_000_000,
            "age_seconds": age,
            "trip_ids": list(trip_ids),
            "unmodelled": list(unmodelled),
        }
        for entity in entities
    }


def feed(tmp_path: Path, name: str, overrides: dict[str, str]) -> sqlite3.Connection:
    """A miniature feed with one thing deliberately wrong."""
    zip_path = fixtures.build_gtfs_zip(tmp_path / f"{name}.zip", overrides)
    db = tmp_path / f"{name}.sqlite"
    build_sqlite(zip_path, db)
    return connect_ro(db)


# ------------------------------------------------------------ broken feeds --

STOPS_DUPLICATE_CODE = fixtures.STOPS.replace("S2,15112", "S2,15111")
STOPS_MISSING_CODE = fixtures.STOPS.replace("S3,15113", "S3,")
STOPS_ALPHA_CODE = fixtures.STOPS.replace("S3,15113", "S3,ABC12")
STOPS_RENUMBERED = (
    fixtures.STOPS.replace("S2,15112", "S2,25112")
    .replace("S3,15113", "S3,25113")
    .replace("ST1,90001", "ST1,80001")
    .replace("ST1P,90002", "ST1P,80002")
)
ROUTES_RENUMBERED = fixtures.ROUTES.replace("MLR,RED", "MLR2,RED")
AGENCY_MOVED = fixtures.AGENCY.replace("America/Chicago", "America/New_York")
STOP_TIMES_2900 = fixtures.STOP_TIMES.replace(
    "T_WK_2412,24:22:00,24:22:00,S2,2", "T_WK_2412,29:00:00,29:00:00,S2,2"
)
# "9:05:00" sorts ABOVE "24:22:00" as text and below it numerically. This row
# is the trap max_time_bounded exists to step around.
STOP_TIMES_UNPADDED = fixtures.STOP_TIMES + "T_WK_1200,9:05:00,9:05:00,S3,4,0,0\n"
FREQUENCIES = "trip_id,start_time,end_time,headway_secs\nT_WK_1200,06:00:00,09:00:00,600\n"
FARE_ATTRIBUTES = (
    "fare_id,price,currency_type,payment_method,transfers\nF1,2.50,USD,0,0\n"
)


def armed_registry(tmp_path: Path, page_hash: str) -> Path:
    """The packaged registry with the three web hashes filled in.

    They ship empty on purpose (see the TOML), so arming them is the only way
    to exercise the compare-and-fail path.
    """
    text = assertions._PKG_ASSERTIONS.read_text(encoding="utf-8")
    assert text.count('expected_hash = ""') == 3
    path = tmp_path / "armed.toml"
    path.write_text(text.replace('expected_hash = ""', f'expected_hash = "{page_hash}"'),
                    encoding="utf-8")
    return path


def by_id(result: dict) -> dict[str, dict]:
    return {item["id"]: item for item in result["items"]}


# --------------------------------------------------------------- inventory --

def test_registry_covers_the_spec_table():
    out = assertions.list_assumptions()
    assert out["total"] == 16
    assert {i["id"] for i in out["items"]} == SPEC_IDS


def test_list_says_what_breaks_for_every_assumption():
    for item in assertions.list_assumptions()["items"]:
        assert item["title"] and item["checks"] and item["breaks"]
        assert item["severity"] in assertions.SEVERITIES
        assert item["category"] in assertions.CATEGORY_ORDER


def test_every_assumption_produces_a_result(conn):
    out = assertions.run(conn, baseline_conn=conn, web_hashes=WEB_HASHES,
                         rt_feeds=rt_sample(), as_of=AS_OF)
    assert {i["id"] for i in out["items"]} == SPEC_IDS
    assert out["total"] == 16
    for item in out["items"]:
        assert item["status"] in ("pass", "fail", "skip", "opportunity")
        assert item["detail"], f"{item['id']} reported no actionable detail"
        assert "observed" in item and "expected" in item
        assert item["breaks"]
        # A check that blew up is reported as a failure carrying `error`.
        # None should here, and a bare pass/fail count would hide it.
        assert "error" not in item, item["detail"]


def test_clean_feed_has_no_violations(conn):
    out = assertions.run(conn, baseline_conn=conn, web_hashes=WEB_HASHES,
                         rt_feeds=rt_sample(), as_of=AS_OF)
    assert out["ok"] is True
    assert out["failed"] == 0
    assert out["violations"] == []
    # The three page hashes ship unarmed, so they skip rather than pass.
    assert out["passed"] == 13
    assert out["skipped"] == 3


# ------------------------------------------------------------- skip is not pass --

def test_missing_baseline_skips_the_stability_assumptions(conn):
    out = by_id(assertions.run(conn, as_of=AS_OF))
    for aid in ("stop_ids_stable", "rail_route_ids_stable"):
        assert out[aid]["status"] == "skip"
        assert "baseline" in out[aid]["detail"]


def test_missing_web_hashes_skip_the_page_assumptions(conn):
    out = by_id(assertions.run(conn, as_of=AS_OF))
    for aid in ("fares_unchanged", "holidays_unchanged", "terms_unchanged"):
        assert out[aid]["status"] == "skip"
        assert "web capture" in out[aid]["detail"]


def test_missing_rt_sample_skips_the_realtime_assumptions(conn):
    out = by_id(assertions.run(conn, as_of=AS_OF))
    for aid in ("rt_fresh", "rt_join_rate", "rt_wire_shape"):
        assert out[aid]["status"] == "skip"
        assert "rt fetch" in out[aid]["detail"]


def test_skips_are_counted_apart_from_passes(conn):
    out = assertions.run(conn, as_of=AS_OF)
    assert out["skipped"] == 8  # 2 baseline + 3 web + 3 realtime
    assert out["passed"] == 8
    assert out["passed"] + out["failed"] + out["skipped"] + out["opportunities"] == 16
    assert out["ok"] is True  # nothing measured failed


def test_unarmed_page_hash_skips_and_hands_back_the_hash_to_paste(conn):
    item = by_id(assertions.run(conn, web_hashes=WEB_HASHES, as_of=AS_OF))["fares_unchanged"]
    assert item["status"] == "skip"
    assert item["observed"] == WEB_HASHES["fares"]
    assert "expected_hash" in item["detail"]


# ------------------------------------------------------- induced failures ----

def test_duplicate_stop_code_fails_with_the_count(tmp_path, conn):
    broken = feed(tmp_path, "dup", {"stops.txt": STOPS_DUPLICATE_CODE})
    out = assertions.run(broken, as_of=AS_OF)
    item = by_id(out)["stop_code_unique"]
    assert item["status"] == "fail"
    assert item["observed"] == 1        # one code shared by two stops
    assert item["expected"] == 0
    assert "15111" in item["detail"]
    assert out["ok"] is False
    assert [v["id"] for v in out["violations"]] == ["stop_code_unique"]
    assert out["violations"][0]["breaks"]


def test_empty_stop_code_fails_coverage_with_the_ratio(tmp_path):
    broken = feed(tmp_path, "gap", {"stops.txt": STOPS_MISSING_CODE})
    out = by_id(assertions.run(broken, as_of=AS_OF))
    item = out["stop_code_present"]
    assert item["status"] == "fail"
    # 3 of 4 boarding locations; the station parent is excluded by design.
    assert item["observed"] == 0.75
    assert item["expected"] == 0.99
    assert "0.75" in item["detail"]
    # The format assumption looks only at non-empty codes, so it stays quiet.
    assert out["stop_code_format"]["status"] == "pass"


def test_non_numeric_stop_code_fails_format_only(tmp_path):
    broken = feed(tmp_path, "alpha", {"stops.txt": STOPS_ALPHA_CODE})
    out = by_id(assertions.run(broken, as_of=AS_OF))
    assert out["stop_code_format"]["status"] == "fail"
    assert out["stop_code_format"]["observed"] == 0.8
    assert "'ABC12'" in out["stop_code_format"]["detail"]
    assert out["stop_code_present"]["status"] == "pass"


def test_late_stop_time_fails_and_names_the_value(tmp_path):
    broken = feed(tmp_path, "late", {"stop_times.txt": STOP_TIMES_2900})
    item = by_id(assertions.run(broken, as_of=AS_OF))["max_time_bounded"]
    assert item["status"] == "fail"
    assert item["observed"] == "29:00:00"
    assert item["expected"] == "< 28:00:00"
    assert "29:00:00" in item["detail"]


def test_max_time_is_compared_numerically_not_lexicographically(tmp_path):
    """'9:05:00' sorts above '24:22:00' as TEXT. The feed's real maximum is
    still 24:22:00, and reporting the text maximum would hide a genuine 29:00
    behind an unpadded morning departure."""
    broken = feed(tmp_path, "unpadded", {"stop_times.txt": STOP_TIMES_UNPADDED})
    item = by_id(assertions.run(broken, as_of=AS_OF))["max_time_bounded"]
    assert item["status"] == "pass"
    assert item["observed"] == "24:22:00"


def test_frequencies_file_fails_with_its_row_count(tmp_path):
    broken = feed(tmp_path, "freq", {"frequencies.txt": FREQUENCIES})
    item = by_id(assertions.run(broken, as_of=AS_OF))["no_frequencies_file"]
    assert item["status"] == "fail"
    assert item["observed"] == 1
    assert item["expected"] == 0


def test_timezone_change_fails_with_both_zones(tmp_path):
    broken = feed(tmp_path, "tz", {"agency.txt": AGENCY_MOVED})
    item = by_id(assertions.run(broken, as_of=AS_OF))["timezone_unchanged"]
    assert item["status"] == "fail"
    assert item["observed"] == "America/New_York"
    assert item["expected"] == "America/Chicago"


def test_expiring_feed_fails_with_days_remaining(conn):
    item = by_id(assertions.run(conn, as_of=NEAR_EXPIRY))["feed_not_expiring"]
    assert item["status"] == "fail"
    assert item["observed"] == 2      # 2026-09-28 -> 2026-09-30
    assert item["expected"] == 7
    assert "2026-09-30" in item["detail"]


def test_healthy_feed_reports_days_remaining_on_a_pass(conn):
    item = by_id(assertions.run(conn, as_of=AS_OF))["feed_not_expiring"]
    assert item["status"] == "pass"
    assert item["observed"] == 58     # 2026-08-03 -> 2026-09-30


# ------------------------------------------------------- baseline failures --

def test_renumbered_stops_fail_survival(tmp_path, conn):
    current = feed(tmp_path, "renum", {"stops.txt": STOPS_RENUMBERED})
    item = by_id(assertions.run(current, baseline_conn=conn, as_of=AS_OF))["stop_ids_stable"]
    assert item["status"] == "fail"
    assert item["observed"] == 0.2    # only 15111 survived, of five
    assert item["expected"] == 0.98
    assert "15112" in item["detail"]


def test_renamed_rail_route_fails_with_both_id_sets(tmp_path, conn):
    current = feed(tmp_path, "rail", {"routes.txt": ROUTES_RENUMBERED})
    item = by_id(assertions.run(current, baseline_conn=conn,
                                as_of=AS_OF))["rail_route_ids_stable"]
    assert item["status"] == "fail"
    assert item["observed"] == ["MLR2"]
    assert item["expected"] == ["MLR"]


def test_identical_baseline_passes_both_stability_assumptions(conn):
    out = by_id(assertions.run(conn, baseline_conn=conn, as_of=AS_OF))
    assert out["stop_ids_stable"]["status"] == "pass"
    assert out["stop_ids_stable"]["observed"] == 1.0
    assert out["rail_route_ids_stable"]["status"] == "pass"


# ------------------------------------------------------- realtime failures --

def test_stale_realtime_fails_with_the_worst_age(conn):
    item = by_id(assertions.run(conn, rt_feeds=rt_sample(age=900.0),
                                as_of=AS_OF))["rt_fresh"]
    assert item["status"] == "fail"
    assert item["observed"] == 900.0
    assert item["expected"] == 300.0


def test_absent_realtime_entity_fails_rather_than_skipping(conn):
    """A feed that could not be fetched is not fresh -- a 404 on the alerts
    endpoint must not read as a healthy suite."""
    partial = rt_sample(entities=("trip_updates", "vehicle_positions"))
    item = by_id(assertions.run(conn, rt_feeds=partial, as_of=AS_OF))["rt_fresh"]
    assert item["status"] == "fail"
    assert "alerts" in item["detail"]


def test_unresolvable_trip_ids_fail_the_join_rate(conn):
    feeds = rt_sample(trip_ids=("T_WK_1200", "GHOST_A", "GHOST_B", "GHOST_C"))
    item = by_id(assertions.run(conn, rt_feeds=feeds, as_of=AS_OF))["rt_join_rate"]
    assert item["status"] == "fail"
    assert item["observed"] == 0.25
    assert item["expected"] == 0.95
    assert "GHOST_A" in item["detail"]


def test_empty_trip_id_sample_skips_rather_than_scoring_zero(conn):
    item = by_id(assertions.run(conn, rt_feeds=rt_sample(trip_ids=()),
                                as_of=AS_OF))["rt_join_rate"]
    assert item["status"] == "skip"


def test_common_unmodelled_proto_field_fails(conn):
    feeds = rt_sample(unmodelled=[
        {"path": "entity.trip_update.?9", "presence_rate": 0.9},
        {"path": "entity.?7", "presence_rate": 0.01},
    ])
    item = by_id(assertions.run(conn, rt_feeds=feeds, as_of=AS_OF))["rt_wire_shape"]
    assert item["status"] == "fail"
    assert item["observed"] == 0.9
    assert "entity.trip_update.?9" in item["detail"]


def test_rare_unmodelled_proto_field_passes(conn):
    feeds = rt_sample(unmodelled=[{"path": "entity.?7", "presence_rate": 0.01}])
    item = by_id(assertions.run(conn, rt_feeds=feeds, as_of=AS_OF))["rt_wire_shape"]
    assert item["status"] == "pass"
    assert item["observed"] == 0.01


def test_missing_census_skips_the_wire_shape_assumption(conn):
    feeds = {"trip_updates": {"age_seconds": 10.0, "trip_ids": ["T_WK_1200"]}}
    item = by_id(assertions.run(conn, rt_feeds=feeds, as_of=AS_OF))["rt_wire_shape"]
    assert item["status"] == "skip"
    assert "field_census" in item["detail"]


# ------------------------------------------------------------ web failures --

def test_matching_page_hash_passes(tmp_path, conn):
    path = armed_registry(tmp_path, WEB_HASHES["fares"])
    out = by_id(assertions.run(conn, web_hashes={"fares": WEB_HASHES["fares"]},
                               path=path, as_of=AS_OF))
    assert out["fares_unchanged"]["status"] == "pass"
    # The other two pages were not captured in this run, so they skip.
    assert out["holidays_unchanged"]["status"] == "skip"


def test_changed_page_hash_fails_with_both_hashes(tmp_path, conn):
    path = armed_registry(tmp_path, "a" * 64)
    item = by_id(assertions.run(conn, web_hashes={"developer_terms": "b" * 64},
                                path=path, as_of=AS_OF))["terms_unchanged"]
    assert item["status"] == "fail"
    assert item["observed"] == "b" * 64
    assert item["expected"] == "a" * 64
    assert "web diff developer_terms" in item["detail"]


# ------------------------------------------------------------ opportunity ----

def test_fare_files_appearing_is_an_opportunity_not_a_failure(tmp_path):
    broken = feed(tmp_path, "fares", {"fare_attributes.txt": FARE_ATTRIBUTES})
    out = assertions.run(broken, as_of=AS_OF)
    item = by_id(out)["no_fare_files"]
    assert item["status"] == "opportunity"
    assert item["observed"] == ["fare_attributes"]
    assert "fare_attributes.txt (1 rows)" in item["detail"]
    # The whole point: CI stays green when upstream improves (spec 5, exit 3).
    assert out["failed"] == 0
    assert out["opportunities"] == 1
    assert out["ok"] is True
    assert out["violations"] == []


def test_absent_fare_files_is_a_plain_pass(conn):
    item = by_id(assertions.run(conn, as_of=AS_OF))["no_fare_files"]
    assert item["status"] == "pass"
    assert item["observed"] == []


# ---------------------------------------------------------- determinism -----

def test_two_runs_are_byte_identical(conn):
    kwargs = dict(baseline_conn=conn, web_hashes=WEB_HASHES,
                  rt_feeds=rt_sample(), as_of=AS_OF)
    first = json.dumps(assertions.run(conn, **kwargs), sort_keys=True)
    second = json.dumps(assertions.run(conn, **kwargs), sort_keys=True)
    assert first == second


def test_order_is_category_rank_then_id(conn):
    ids = [i["id"] for i in assertions.run(conn, as_of=AS_OF)["items"]]
    rank = {c: i for i, c in enumerate(assertions.CATEGORY_ORDER)}
    specs = {s["id"]: s for s in assertions.load_assumptions()}
    keys = [(rank[specs[i]["category"]], i) for i in ids]
    assert keys == sorted(keys)
    # And the categories arrive grouped, not interleaved.
    assert [i["category"] for i in assertions.run(conn, as_of=AS_OF)["items"]] == sorted(
        (i["category"] for i in assertions.run(conn, as_of=AS_OF)["items"]),
        key=lambda c: rank[c],
    )


def test_listing_and_running_agree_on_order(conn):
    listed = [i["id"] for i in assertions.list_assumptions()["items"]]
    ran = [i["id"] for i in assertions.run(conn, as_of=AS_OF)["items"]]
    assert listed == ran


# --------------------------------------------------------------- selection --

def test_only_filters_the_suite(conn):
    out = assertions.run(conn, only=["stop_code_unique", "max_time_bounded"], as_of=AS_OF)
    assert [i["id"] for i in out["items"]] == ["max_time_bounded", "stop_code_unique"]
    assert out["total"] == 2


def test_only_with_an_unknown_id_lists_the_valid_ones(conn):
    with pytest.raises(UsageError) as exc:
        assertions.run(conn, only=["stop_code_uniq"], as_of=AS_OF)
    assert "stop_code_unique" in exc.value.remedy
    assert exc.value.exit_code == 2


# ----------------------------------------------------------------- explain --

def test_explain_returns_the_full_record():
    out = assertions.explain("max_time_bounded")
    assert out["ok"] is True
    assert out["why"] and out["code_path"] and out["remediation"]
    assert out["thresholds"] == {"max_hour_exclusive": 28}


def test_explain_unknown_id_raises_with_the_valid_ids():
    with pytest.raises(UsageError) as exc:
        assertions.explain("stop_codes_present")
    assert "stop_code_present" in exc.value.remedy
    assert exc.value.context["available"] == sorted(SPEC_IDS)


def test_thresholds_are_config_not_code():
    """Every tunable in the TOML reaches the result, so a red run can print the
    threshold it was compared against without the reader opening the file."""
    for spec in assertions.load_assumptions():
        for key in spec["thresholds"]:
            assert key not in ("title", "breaks", "severity", "category")
    assert assertions.explain("stop_code_present")["thresholds"]["min_coverage"] == 0.99


# ------------------------------------------------------------ config loading --

VALID_TOML = """
[assumptions.stop_code_unique]
title = "t"
checks = "c"
breaks = "b"
severity = "critical"
category = "static"
why = "w"
code_path = "p"
remediation = "r"
max_duplicates = 0
"""


def write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "custom.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_env_var_overrides_the_packaged_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("STL_ASSERTIONS", str(write_toml(tmp_path, VALID_TOML)))
    assert [s["id"] for s in assertions.load_assumptions()] == ["stop_code_unique"]


def test_explicit_path_beats_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("STL_ASSERTIONS", "/nonexistent/assertions.toml")
    path = write_toml(tmp_path, VALID_TOML)
    assert len(assertions.load_assumptions(path)) == 1


def test_missing_registry_reports_a_remedy(tmp_path):
    with pytest.raises(UsageError) as exc:
        assertions.load_assumptions(tmp_path / "nope.toml")
    assert exc.value.remedy


def test_missing_key_is_a_config_error_naming_the_key(tmp_path):
    path = write_toml(tmp_path, VALID_TOML.replace('breaks = "b"\n', ""))
    with pytest.raises(UsageError) as exc:
        assertions.load_assumptions(path)
    assert "breaks" in exc.value.message


def test_unknown_severity_is_rejected(tmp_path):
    path = write_toml(tmp_path, VALID_TOML.replace('"critical"', '"catastrophic"'))
    with pytest.raises(UsageError) as exc:
        assertions.load_assumptions(path)
    assert "critical" in exc.value.remedy


def test_assumption_without_a_measurement_is_rejected(tmp_path):
    """Adding an assumption is a config edit plus one function. A table with no
    function must say so loudly rather than look like a passing check."""
    path = write_toml(tmp_path, VALID_TOML.replace("stop_code_unique", "vibes_unchanged"))
    with pytest.raises(UsageError) as exc:
        assertions.load_assumptions(path)
    assert "measurement function" in exc.value.message


def test_unknown_required_input_is_rejected(tmp_path):
    path = write_toml(tmp_path, VALID_TOML + 'requires = ["telepathy"]\n')
    with pytest.raises(UsageError) as exc:
        assertions.load_assumptions(path)
    assert "telepathy" in exc.value.message
