"""`report` group tests.

The three reports are composition over other groups' output, so these tests
feed them the REAL output of `inspect.coverage`, `assertions.run`,
`entities.stop_resolve` and `diffing.summary` against the miniature feed rather
than hand-written dicts. A hand-written dict would keep passing after the shape
it imitates had moved, which is the one failure a composition layer must not
have.

`now` is injected everywhere (spec 2.7) so nothing depends on the wall clock,
and the same inputs are rendered twice in the determinism tests (spec 2.8).
No network.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from stl_transit.core import assertions, diffing, report
from stl_transit.core.gtfs import entities, inspect
from stl_transit.io.clock import AGENCY_TZ
from stl_transit.io.db import build_sqlite, connect_ro

from . import fixtures

# The mini feed runs 2026-01-01..2026-09-30. A fixed `now` well inside that
# window keeps feed_not_expiring green unless a test deliberately moves it.
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=AGENCY_TZ)
NEAR_EXPIRY = datetime(2026, 9, 28, 12, 0, tzinfo=AGENCY_TZ)
PAST_EXPIRY = datetime(2026, 10, 15, 12, 0, tzinfo=AGENCY_TZ)

SNAPSHOT_ID = "gtfs-20260803T120000Z-a3f9c1"
FEED_SHA = "d" * 64

SNAPSHOTS = [
    {"snapshot_id": SNAPSHOT_ID, "kind": "gtfs", "source": "metro_gtfs",
     "fetched_at": "2026-08-03T12:00:00+00:00", "bytes": 4096, "sha256": "a3f9c1",
     "pin": "baseline"},
    {"snapshot_id": "rt-20260803T115900Z-b12345", "kind": "rt",
     "source": "metro_rt_trips", "fetched_at": "2026-08-03T11:59:00+00:00",
     "bytes": 512, "sha256": "b12345", "pin": None},
]


# ------------------------------------------------------------------ helpers --

def coverage_for(conn: sqlite3.Connection, when: datetime = NOW) -> dict:
    return inspect.coverage(conn, when.date())


def rt_health(stale: bool = False, missing: bool = False) -> dict:
    """The shape `service.rt_health` returns. Hand-built because producing the
    real one needs a snapshot store and three protobuf fetches, neither of which
    this module touches."""
    items = [
        {"entity": "alerts", "available": True, "snapshot_id": "rt-a",
         "age_seconds": 12, "stale": False, "entity_count": 3},
        {"entity": "trip_updates", "available": True, "snapshot_id": "rt-t",
         "age_seconds": 900 if stale else 30, "stale": stale, "entity_count": 41},
    ]
    if not missing:
        items.append({"entity": "vehicle_positions", "available": True,
                      "snapshot_id": "rt-v", "age_seconds": 25, "stale": False,
                      "entity_count": 60})
    else:
        items.append({"entity": "vehicle_positions", "available": False,
                      "remedy": "stl rt fetch --entity vehicle_positions"})
    return {"ok": not (stale or missing), "items": items,
            "checked_at": NOW.isoformat(), "warnings": []}


def capture(page: str, content_hash: str, fetched_at: str,
            extractor: str = "text") -> dict:
    """The subset of a `web.capture` record that `web.check` reads."""
    return {"page": page, "url": f"https://example.org/{page}", "extractor": extractor,
            "fetched_at": fetched_at, "content_hash": content_hash,
            "extraction_ok": True, "extraction_error": None}


def web_check(drift: bool) -> dict:
    from stl_transit.core import web

    fares = [capture("fares", "f2" if drift else "f1", "2026-08-03T00:00:00+00:00",
                     "fare_table"),
             capture("fares", "f1", "2026-08-01T00:00:00+00:00", "fare_table")]
    holidays = [capture("holidays", "h1", "2026-08-03T00:00:00+00:00", "holiday_table"),
                capture("holidays", "h1", "2026-08-01T00:00:00+00:00", "holiday_table")]
    return web.check({"fares": fares, "holidays": holidays})


def broken_conn(tmp_path: Path) -> sqlite3.Connection:
    """A feed with a duplicated stop_code -- one violated assumption, on purpose."""
    stops = fixtures.STOPS.replace("S2,15112", "S2,15111")
    zip_path = fixtures.build_gtfs_zip(tmp_path / "dupe.zip", {"stops.txt": stops})
    db = tmp_path / "dupe.sqlite"
    build_sqlite(zip_path, db)
    return connect_ro(db)


def changed_conn(tmp_path: Path) -> sqlite3.Connection:
    """A later 'pick': one stop_code retired, one stop renamed."""
    stops = (fixtures.STOPS.replace("S3,15113", "S3,25113")
             .replace("Main St & 2nd", "Main Street & 2nd"))
    zip_path = fixtures.build_gtfs_zip(tmp_path / "pick.zip", {"stops.txt": stops})
    db = tmp_path / "pick.sqlite"
    build_sqlite(zip_path, db)
    return connect_ro(db)


# -------------------------------------------------------------------- brief --

def test_brief_renders_with_every_input(conn):
    out = report.brief(coverage_for(conn), assertions.run(conn, as_of=NOW),
                       rt_health(), web_check(drift=False), SNAPSHOTS, NOW)
    assert out["headline"]
    assert out["status"] in ("ok", "attention", "broken")
    assert out["feed"]["service_end"] == "2026-09-30"
    assert out["assertions"]["checked"] is True
    assert out["realtime"]["checked"] is True
    assert out["drift"]["checked"] is True
    assert out["not_checked"] == []
    assert out["snapshots"]["latest"]["gtfs"]["snapshot_id"] == SNAPSHOT_ID
    assert out["generated_at"] == NOW.isoformat()


def test_brief_renders_with_no_optional_inputs(conn):
    out = report.brief(coverage_for(conn), None, None, None, SNAPSHOTS, NOW)
    # It renders rather than refusing, and every gap is named.
    assert out["headline"]
    assert {n["area"] for n in out["not_checked"]} == {"assertions", "realtime", "drift"}
    for area in ("assertions", "realtime", "drift"):
        assert out[area]["checked"] is False
        assert out[area]["remedy"]
    assert out["next_actions"]
    # A section nobody measured is never reported as passing.
    assert "Not checked: assertions, drift, realtime." in out["headline"]


def test_brief_renders_with_a_mix_of_inputs(conn):
    out = report.brief(coverage_for(conn), assertions.run(conn, as_of=NOW), None,
                       web_check(drift=False), SNAPSHOTS, NOW)
    assert out["assertions"]["checked"] is True
    assert out["drift"]["checked"] is True
    assert out["realtime"]["checked"] is False
    assert [n["area"] for n in out["not_checked"]] == ["realtime"]
    assert any("rt fetch" in a["command"] for a in out["next_actions"])


def test_brief_is_ok_when_nothing_measured_is_wrong(conn):
    out = report.brief(coverage_for(conn), assertions.run(conn, as_of=NOW),
                       rt_health(), web_check(drift=False), SNAPSHOTS, NOW)
    assert out["status"] == "ok"
    assert out["ok"] is True
    assert out["blocking"] == []


def test_brief_is_broken_on_assertion_violations(tmp_path):
    conn = broken_conn(tmp_path)
    result = assertions.run(conn, as_of=NOW)
    assert result["violations"], "the duplicated stop_code must violate something"
    out = report.brief(coverage_for(conn), result, rt_health(), web_check(drift=False),
                       SNAPSHOTS, NOW)
    assert out["status"] == "broken"
    assert out["ok"] is False
    assert "stop_code_unique" in out["assertions"]["violated_ids"]
    assert any(f["area"] == "assertions" for f in out["blocking"])
    # The violation must produce a command, not just a verdict.
    assert out["next_actions"][0]["urgency"] == "now"
    assert "assert explain" in out["next_actions"][0]["command"]


def test_brief_is_attention_on_drift(conn):
    out = report.brief(coverage_for(conn), assertions.run(conn, as_of=NOW),
                       rt_health(), web_check(drift=True), SNAPSHOTS, NOW)
    assert out["status"] == "attention"
    assert out["drift"]["changed"] == ["fares"]
    assert "fares" in out["drift"]["alarming"]
    assert any("web diff fares" in a["command"] for a in out["next_actions"])


def test_brief_is_attention_on_stale_realtime(conn):
    out = report.brief(coverage_for(conn), assertions.run(conn, as_of=NOW),
                       rt_health(stale=True, missing=True), web_check(drift=False),
                       SNAPSHOTS, NOW)
    assert out["status"] == "attention"
    assert out["realtime"]["stale"] == ["trip_updates"]
    assert out["realtime"]["unavailable"] == ["vehicle_positions"]


def test_brief_is_broken_on_an_expired_feed(conn):
    out = report.brief(coverage_for(conn, PAST_EXPIRY), None, None, None, SNAPSHOTS,
                       PAST_EXPIRY)
    assert out["status"] == "broken"
    assert out["feed"]["expired"] is True
    assert "EXPIRED" in out["headline"]
    assert out["next_actions"][0]["command"] == "stl snapshot fetch metro_gtfs"


def test_brief_warns_before_the_feed_expires(conn):
    out = report.brief(coverage_for(conn, NEAR_EXPIRY), None, None, None, SNAPSHOTS,
                       NEAR_EXPIRY)
    assert out["status"] == "attention"
    assert out["feed"]["days_remaining"] == 2
    assert any(a["command"] == "stl snapshot fetch metro_gtfs" for a in out["next_actions"])


def test_brief_next_actions_are_ordered_and_never_empty(conn):
    broken = report.brief(coverage_for(conn, PAST_EXPIRY),
                          assertions.run(conn, as_of=PAST_EXPIRY), rt_health(stale=True),
                          web_check(drift=True), SNAPSHOTS, PAST_EXPIRY)
    ranks = [report.URGENCIES.index(a["urgency"]) for a in broken["next_actions"]]
    assert ranks == sorted(ranks), "most urgent first"
    assert [a["order"] for a in broken["next_actions"]] == list(
        range(1, len(broken["next_actions"]) + 1))
    assert all(a["why"] and a["command"] for a in broken["next_actions"])

    # And on a wholly green feed there is still something to do next: a list
    # that empties out when everything passes stops being read.
    green = report.brief(coverage_for(conn), assertions.run(conn, as_of=NOW),
                         rt_health(), web_check(drift=False), SNAPSHOTS, NOW)
    assert green["status"] == "ok"
    assert green["next_actions"]


def test_brief_deduplicates_the_same_command(conn):
    out = report.brief(coverage_for(conn), None, None, None, SNAPSHOTS, NOW)
    commands = [a["command"] for a in out["next_actions"]]
    assert len(commands) == len(set(commands))


def test_brief_is_deterministic(conn):
    args = (coverage_for(conn), assertions.run(conn, as_of=NOW), rt_health(True, True),
            web_check(drift=True), SNAPSHOTS, NOW)
    assert json.dumps(report.brief(*args), sort_keys=True) == \
        json.dumps(report.brief(*args), sort_keys=True)


# ------------------------------------------------------------------ handoff --

def handoff_for(conn, rt_census=None, now=NOW):
    return report.handoff(inspect.coverage(conn, now.date()), entities.stop_resolve(conn),
                          rt_census, inspect.stats(conn), SNAPSHOT_ID, FEED_SHA, now)


def test_handoff_markdown_cites_the_snapshot_and_the_date(conn):
    out = handoff_for(conn)
    md = out["markdown"]
    assert SNAPSHOT_ID in md
    assert "2026-08-03" in md
    assert FEED_SHA in md
    assert out["verified_on"] == "2026-08-03"
    # The house rule: every claim carries its citation, not just the preamble.
    assert out["facts"]
    for fact in out["facts"]:
        assert fact["verified_against"] == SNAPSHOT_ID
        assert fact["verified_on"] == "2026-08-03"
        assert fact["reverify_with"]
        assert f"re-verify: `{fact['reverify_with']}`" in md


def test_handoff_names_every_sharp_edge(conn):
    md = handoff_for(conn)["markdown"]
    assert len(report.SHARP_EDGES) == 5
    for edge in report.SHARP_EDGES:
        assert edge["title"] in md
        assert edge["bites"] in md
        assert edge["verify_with"] in md
    # Named individually too, so a renamed constant cannot quietly drop one.
    for phrase in ("24:xx", "DST", "stop_code vs stop_id", "protobuf", "Fares are not"):
        assert phrase in md


def test_handoff_carries_the_verified_facts(conn):
    out = handoff_for(conn)
    claims = " ".join(f["claim"] for f in out["facts"])
    assert "2026-09-30" in claims          # feed window
    assert "`stop_code`" in claims         # the rider-facing field question
    assert "15111" in claims               # Metro's own published example
    assert "5 stop(s)" in claims           # feed scale from stats


def test_handoff_without_a_realtime_census_says_so(conn):
    out = handoff_for(conn, rt_census=None)
    rt = [f for f in out["facts"] if f["id"] == "rt_field_census"][0]
    assert "NOT VERIFIED" in rt["claim"]
    assert rt["reverify_with"] == "stl rt schema --samples 5"
    assert out["warnings"], "an unverified realtime shape is worth a warning"


def test_handoff_with_a_realtime_census_reports_it(conn):
    census = {
        "samples": 5, "distinct_paths": 12, "unmodelled_paths": 1,
        "fields": [{"path": "header.timestamp", "presence_rate": 1.0},
                   {"path": "entity.trip_update.?9", "presence_rate": 0.4}],
        "unmodelled": [{"path": "entity.trip_update.?9", "presence_rate": 0.4}],
    }
    out = handoff_for(conn, rt_census=census)
    rt = [f for f in out["facts"] if f["id"] == "rt_field_census"][0]
    assert "12 distinct protobuf path(s)" in rt["claim"]
    assert "header.timestamp" in rt["evidence"]
    assert out["warnings"] == []


def test_handoff_is_deterministic(conn):
    assert handoff_for(conn)["markdown"] == handoff_for(conn)["markdown"]
    assert json.dumps(handoff_for(conn), sort_keys=True) == \
        json.dumps(handoff_for(conn), sort_keys=True)


# ---------------------------------------------------------------- changelog --

def test_changelog_over_a_no_change_diff_says_so(conn):
    out = report.changelog(diffing.summary(conn, conn), "baseline-2026-07", NOW)
    assert out["changed"] is False
    assert out["drift_detected"] is False
    assert out["sections"] == []
    assert out["entries"] == []
    assert "Nothing changed since `baseline-2026-07`" in out["headline"]
    assert "Nothing changed since `baseline-2026-07`" in out["markdown"]
    assert out["notes"], "a byte-identical answer is a real result, and says why"


def test_changelog_reports_a_real_change_as_prose(tmp_path, conn):
    later = changed_conn(tmp_path)
    out = report.changelog(diffing.summary(conn, later), "baseline-2026-07", NOW)
    assert out["changed"] is True
    titles = [s["title"] for s in out["sections"]]
    assert "Stop numbers" in titles
    assert "Stops" in titles
    text = " ".join(e["text"] for e in out["entries"])
    assert "stop_code" in text
    assert "renamed" in text
    assert "baseline-2026-07" in out["markdown"]
    assert all(line for section in out["sections"] for line in section["lines"])


def test_changelog_promotes_alarming_findings_without_repeating_them(tmp_path, conn):
    later = changed_conn(tmp_path)
    summary = diffing.summary(conn, later)
    assert summary["alarming"], "retiring a stop_code below the floor is alarming"
    out = report.changelog(summary, "baseline-2026-07", NOW)
    assert out["sections"][0]["title"] == "Read this first"
    assert out["ok"] is False
    assert out["warnings"]
    # Promoted, not duplicated: each alarming line appears exactly once.
    lines = [line for section in out["sections"] for line in section["lines"]]
    for entry in out["alarming"]:
        assert lines.count(entry["text"]) == 1


def test_changelog_tolerates_a_partial_diff(conn):
    # A caller that passes only part of a diff summary gets prose about the part
    # it passed, not a KeyError.
    out = report.changelog({"drift_detected": True, "headline": "something moved"},
                           "baseline-2026-07", NOW)
    assert out["changed"] is True
    assert "something moved" in out["headline"]


def test_changelog_is_deterministic(tmp_path, conn):
    summary = diffing.summary(conn, changed_conn(tmp_path))
    first = report.changelog(summary, "baseline-2026-07", NOW)
    second = report.changelog(summary, "baseline-2026-07", NOW)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
