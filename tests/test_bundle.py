"""Bundle-artifact tests.

These artifacts ship inside the APK, so the assertions here are about what a
rider ends up seeing: that a fare is exact to the cent, that bus and rail keep
their own holiday timetables, that every stop number a rider can read off a
pole resolves, and that nothing was pruned out of the on-device database
without the decisions list saying so and saying why.

Every feed used here is hand-built and miniature. No network.
"""

from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import pytest

from stl_transit.core import bundle
from stl_transit.errors import UsageError
from stl_transit.io.db import build_sqlite, connect_ro

from . import fixtures

AS_OF = date(2026, 8, 3)
FARES_URL = "https://www.metrostlouis.org/fares-and-passes/"
HOLIDAYS_URL = "https://www.metrostlouis.org/holiday-schedules/"

# Shaped like the web group's fare_table extractor output: whatever Metro's
# HTML table headers happen to be, one row per printed price.
FARE_ROWS = [
    {"Mode": "MetroBus", "Category": "Adult", "Fare Type": "2-Hour Pass", "Price": "$2.50"},
    {"Mode": "MetroBus", "Category": "Child", "Fare Type": "Under 5", "Price": "$0.00"},
    {"Mode": "MetroLink", "Category": "Adult", "Fare Type": "1-Ride", "Price": "$1"},
    {"Mode": "MetroLink", "Category": "Adult", "Fare Type": "Weekly Pass",
     "Price": "$27.00", "Notes": "Valid 7 consecutive days"},
]

HOLIDAY_ROWS = [
    {"Holiday": "Labor Day", "Date": "September 7", "MetroBus": "Sunday Service",
     "MetroLink": "Weekend Service", "Call-A-Ride": "No Service"},
    {"Holiday": "Veterans Day", "Date": "2026-11-11", "MetroBus": "Regular Weekday",
     "MetroLink": "Regular Weekday"},
    {"Holiday": "Thanksgiving", "Date": "11/26", "MetroBus": "Sunday Service",
     "MetroLink": "Weekend Service"},
]


# ------------------------------------------------------------ local builders --

def build_zip(path: Path, files: dict[str, str]) -> Path:
    """A GTFS zip containing exactly `files`.

    `fixtures.build_gtfs_zip` can only add or replace members, and several
    tests below need a file to be ABSENT -- that is the whole point of the
    graceful-degradation cases.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return path


def open_feed(tmp_path: Path, name: str, files: dict[str, str]) -> sqlite3.Connection:
    zip_path = build_zip(tmp_path / f"{name}.zip", files)
    db = tmp_path / f"{name}.sqlite"
    build_sqlite(zip_path, db)
    return connect_ro(db)


def variant(tmp_path: Path, name: str, overrides: dict[str, str],
            drop: tuple[str, ...] = ()) -> sqlite3.Connection:
    files = {k: v for k, v in fixtures.FILES.items() if k not in drop}
    files.update(overrides)
    return open_feed(tmp_path, name, files)


def bulky_files(trips: int = 100, stops: int = 50, shape_points: int = 900) -> dict[str, str]:
    """A feed big enough that pruning shows up in the FILE, not just the rows.

    The miniature feed is nine stop_times rows; SQLite's minimum allocation is
    a couple of pages, so a pruned copy of it is byte-for-byte the same size as
    the original and a "compact shrinks" assertion over it would be vacuous.
    This one carries 5,000 stop_times rows and a shapes table -- the same shape
    as the real feed's 489,011 rows, four orders of magnitude smaller.
    """
    stop_rows = ["stop_id,stop_code,stop_name,stop_desc,stop_lat,stop_lon,wheelchair_boarding"]
    for i in range(stops):
        stop_rows.append(
            f"S{i:03d},2{i:04d},Stop Number {i} At Some Long Cross Street,"
            f"Northbound side of the street opposite the shelter,"
            f"38.{600000 + i * 37},-90.{190000 + i * 41},1"
        )
    trip_rows = ["route_id,service_id,trip_id,trip_headsign,direction_id,block_id,shape_id"]
    time_rows = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence,"
                 "pickup_type,drop_off_type,timepoint,shape_dist_traveled"]
    for t in range(trips):
        route = "R11" if t % 2 == 0 else "MLR"
        service = ("WK", "SA", "SU")[t % 3]
        trip_id = f"T{t:04d}"
        trip_rows.append(
            f"{route},{service},{trip_id},Destination Headsign {t % 7},{t % 2},"
            f"B{t % 9},SH{t % 2}"
        )
        for s in range(stops):
            minute = (t * 7 + s) % 60
            hour = 5 + ((t * 7 + s) // 60) % 19
            clock = f"{hour:02d}:{minute:02d}:00"
            time_rows.append(
                f"{trip_id},{clock},{clock},S{s:03d},{s + 1},0,0,1,{s * 250}"
            )
    shape_rows = ["shape_id,shape_pt_sequence,shape_pt_lat,shape_pt_lon"]
    for sh in range(2):
        for p in range(shape_points):
            shape_rows.append(f"SH{sh},{p},38.{600000 + p * 3},-90.{190000 + p * 7}")
    return {
        "agency.txt": fixtures.AGENCY,
        "routes.txt": fixtures.ROUTES,
        "calendar.txt": fixtures.CALENDAR,
        "calendar_dates.txt": fixtures.CALENDAR_DATES,
        "feed_info.txt": fixtures.FEED_INFO,
        "stops.txt": "\n".join(stop_rows) + "\n",
        "trips.txt": "\n".join(trip_rows) + "\n",
        "stop_times.txt": "\n".join(time_rows) + "\n",
        "shapes.txt": "\n".join(shape_rows) + "\n",
    }


@pytest.fixture(scope="module")
def bulky_db(tmp_path_factory) -> Path:
    """Built once: it is the same read-only feed for every test that uses it."""
    root = tmp_path_factory.mktemp("bulky")
    zip_path = build_zip(root / "bulky.zip", bulky_files())
    db = root / "bulky.sqlite"
    build_sqlite(zip_path, db)
    return db


@pytest.fixture
def bulky(bulky_db: Path) -> sqlite3.Connection:
    c = connect_ro(bulky_db)
    yield c
    c.close()


@pytest.fixture(scope="module")
def built(bulky_db: Path, tmp_path_factory) -> dict[str, dict]:
    """One build per strategy, shared by every read-only assertion below.

    The builds are deterministic, so sharing them is free: any test that would
    observe a difference is a test that mutates the file or changes the inputs,
    and those build their own.
    """
    root = tmp_path_factory.mktemp("built")
    conn = connect_ro(bulky_db)
    try:
        return {s: bundle.compact(conn, root / f"{s}.sqlite", strategy=s)
                for s in bundle.STRATEGIES}
    finally:
        conn.close()


def by_target(result: dict) -> dict[str, dict]:
    return {d["target"]: d for d in result["decisions"]}


def table_rows(db: Path, table: str) -> int:
    conn = connect_ro(db)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()


def tables_in(db: Path) -> set[str]:
    conn = connect_ro(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# ----------------------------------------------------------------- the money --

@pytest.mark.parametrize(
    "published,cents",
    [
        ("$2.50", 250),      # the headline fare
        ("2.50", 250),
        ("$1", 100),         # no decimal point at all
        ("1", 100),
        ("$0.00", 0),        # free, printed as a price
        ("Free", 0),         # free, printed as a word
        ("$27.00", 2700),
        ("$1,234.00", 123400),
        ("$0.75", 75),
    ],
)
def test_prices_parse_to_integer_cents(published, cents):
    parsed = bundle.parse_cents(published)
    assert parsed == cents
    assert isinstance(parsed, int)


def test_cents_are_exact_where_a_float_would_not_be():
    """int(float("19.99") * 100) is 1998. That is the bug this parser exists to
    avoid, and it is a real dollar off on a real fare."""
    assert int(float("19.99") * 100) == 1998    # the wrong way, demonstrated
    assert bundle.parse_cents("19.99") == 1999  # the right way
    assert bundle.parse_cents("$2.50") == 250
    assert int(float("2.50") * 100) == 250      # this one happens to work, which is why it hides


def test_price_is_rendered_from_the_cents_not_from_the_page():
    out = bundle.fares([{"Fare Type": "Ride", "Price": "2.5"}], AS_OF, FARES_URL)
    fare = out["fares"][0]
    assert fare["price_cents"] == 250
    assert fare["price"] == "$2.50"             # canonicalised, not echoed
    assert fare["price_as_published"] == "2.5"  # the page's own text, kept for audit


def test_format_cents_round_trips_every_parsed_price():
    for row in FARE_ROWS:
        cents = bundle.parse_cents(row["Price"])
        assert bundle.parse_cents(bundle.format_cents(cents)) == cents


def test_a_third_decimal_place_is_refused_rather_than_rounded():
    with pytest.raises(UsageError) as exc:
        bundle.parse_cents("$2.4999999")
    assert "decimal" in exc.value.message
    assert exc.value.remedy


def test_an_unreadable_price_names_the_value_and_a_remedy():
    with pytest.raises(UsageError) as exc:
        bundle.parse_cents("$1-$3")
    assert "$1-$3" in exc.value.message
    assert "extract" in exc.value.remedy


# ----------------------------------------------------------------- fares json --

def test_fares_bake_in_as_of_and_source_url():
    out = bundle.fares(FARE_ROWS, AS_OF, FARES_URL)
    assert out["as_of"] == "2026-08-03"
    assert out["source_url"] == FARES_URL
    assert out["currency"] == "USD"
    assert out["count"] == 4
    # Sorted by (mode, category, label, price) -- a fixed order, so a diff means
    # the page changed rather than that the generator ran again.
    assert [(f["mode"], f["label"], f["price_cents"]) for f in out["fares"]] == [
        ("MetroBus", "2-Hour Pass", 250),
        ("MetroBus", "Under 5", 0),
        ("MetroLink", "1-Ride", 100),
        ("MetroLink", "Weekly Pass", 2700),
    ]
    assert out["fares"][3]["notes"] == "Valid 7 consecutive days"


def test_fares_are_sorted_not_source_ordered():
    """Metro reorders its own table between redesigns. A bundled artifact that
    reshuffles for that reason produces a diff nobody can read."""
    forwards = bundle.fares(FARE_ROWS, AS_OF, FARES_URL)["rendered"]
    backwards = bundle.fares(list(reversed(FARE_ROWS)), AS_OF, FARES_URL)["rendered"]
    assert forwards == backwards


def test_a_row_with_no_price_is_warned_about_not_silently_dropped():
    out = bundle.fares(FARE_ROWS + [{"Fare Type": "Mystery Pass"}], AS_OF, FARES_URL)
    assert out["count"] == 4
    assert any("Mystery Pass" in w for w in out["warnings"])


def test_empty_fare_table_warns_before_it_ships():
    out = bundle.fares([], AS_OF, FARES_URL)
    assert out["count"] == 0
    assert any("web extract fares" in w for w in out["warnings"])


def test_unknown_format_is_a_usage_error_listing_the_real_ones():
    with pytest.raises(UsageError) as exc:
        bundle.fares(FARE_ROWS, AS_OF, FARES_URL, fmt="yaml")
    assert exc.value.context["available"] == ["json", "kotlin"]
    assert exc.value.exit_code == 2


def test_fare_ids_stay_unique_when_two_rows_slug_the_same():
    rows = [{"Fare Type": "1 Ride", "Price": "$1"}, {"Fare Type": "1-Ride", "Price": "$2"}]
    ids = [f["id"] for f in bundle.fares(rows, AS_OF, FARES_URL)["fares"]]
    assert len(set(ids)) == 2


# --------------------------------------------------------------- kotlin emitter --

def test_kotlin_output_is_syntactically_plausible():
    src = bundle.fares(FARE_ROWS, AS_OF, FARES_URL, fmt="kotlin")["rendered"]
    assert "data class Fare(" in src
    assert "val FARES: List<Fare> = listOf(" in src
    assert src.count("    Fare(") == len(FARE_ROWS)
    assert src.count("(") == src.count(")")
    assert src.count("{") == src.count("}")
    assert "priceCents = 250," in src
    assert 'const val FARES_AS_OF: String = "2026-08-03"' in src
    assert f'const val FARES_SOURCE_URL: String = "{FARES_URL}"' in src
    # No floating-point money anywhere in the emitted source.
    assert not re.search(r"priceCents\s*=\s*\d+\.\d", src)
    assert not re.search(r"\bval\s+price\s*:\s*Double", src)


def test_kotlin_escapes_quotes_dollars_and_backslashes():
    """An unescaped `$` opens a string template in Kotlin, and a fare table is
    full of dollar signs -- so the naive emitter produces a file that either
    does not compile or interpolates a variable."""
    rows = [{"Fare Type": 'The "Best" $5 Deal\\Value', "Price": "$5.00"}]
    src = bundle.fares(rows, AS_OF, FARES_URL, fmt="kotlin")["rendered"]
    assert r'label = "The \"Best\" \$5 Deal\\Value"' in src
    assert '"The "Best"' not in src


def test_kotlin_helper_formats_cents_without_floating_point():
    src = bundle.fares(FARE_ROWS, AS_OF, FARES_URL, fmt="kotlin")["rendered"]
    assert "fun formatCents(cents: Int): String" in src
    assert "(cents / 100)" in src and "(cents % 100)" in src
    assert "toDouble" not in src and "Float" not in src


def test_kotlin_says_where_the_numbers_came_from():
    src = bundle.fares(FARE_ROWS, AS_OF, FARES_URL, fmt="kotlin")["rendered"]
    assert "DO NOT EDIT" in src
    assert FARES_URL in src
    assert "fares_unchanged" in src  # the assertion that catches a stale table


# ------------------------------------------------------------------ holidays --

def test_bus_and_rail_stay_distinct():
    """MetroBus runs Sunday service on Labor Day; MetroLink runs Weekend
    service. Different words, different timetables, and merging them is a wrong
    answer on the one day a rider is least able to check."""
    out = bundle.holidays(HOLIDAY_ROWS, 2026, HOLIDAYS_URL)
    labor = next(h for h in out["holidays"] if h["name"] == "Labor Day")
    assert labor["date"] == "2026-09-07"
    assert labor["weekday"] == "Monday"
    assert labor["services"]["bus"]["service_type"] == "sunday"
    assert labor["services"]["rail"]["service_type"] == "weekend"
    assert labor["services"]["bus"]["published"] == "Sunday Service"
    assert labor["services"]["rail"]["published"] == "Weekend Service"
    assert labor["bus_rail_differ"] is True
    # And the third mode is not folded into either of them.
    assert labor["services"]["paratransit"]["service_type"] == "no_service"


def test_a_holiday_where_the_two_agree_still_stores_both():
    """Storing one value when they happen to match is how the next pick's
    divergence becomes invisible."""
    out = bundle.holidays(HOLIDAY_ROWS, 2026, HOLIDAYS_URL)
    vets = next(h for h in out["holidays"] if h["name"] == "Veterans Day")
    assert set(vets["services"]) == {"bus", "rail"}
    assert vets["bus_rail_differ"] is False
    assert vets["services"]["bus"]["service_type"] == "weekday"


def test_only_one_mode_captured_is_a_loud_warning():
    rows = [{"Holiday": "Labor Day", "Date": "2026-09-07", "MetroBus": "Sunday Service"}]
    out = bundle.holidays(rows, 2026, HOLIDAYS_URL)
    assert out["modes"] == ["bus"]
    assert any("MetroLink" in w and "Weekend" in w for w in out["warnings"])


def test_rail_only_capture_warns_the_other_way():
    rows = [{"Holiday": "Labor Day", "Date": "2026-09-07", "MetroLink": "Weekend"}]
    out = bundle.holidays(rows, 2026, HOLIDAYS_URL)
    assert any("bus riders" in w for w in out["warnings"])


@pytest.mark.parametrize(
    "published,expected",
    [
        ("2026-09-07", "2026-09-07"),
        ("20260907", "2026-09-07"),
        ("September 7", "2026-09-07"),
        ("Sept. 7, 2026", "2026-09-07"),
        ("Sep 7", "2026-09-07"),
        ("9/7", "2026-09-07"),
        ("9/7/2026", "2026-09-07"),
    ],
)
def test_holiday_dates_parse_in_every_form_metro_has_published(published, expected):
    rows = [{"Holiday": "Labor Day", "Date": published, "Bus": "Sunday", "Rail": "Weekend"}]
    out = bundle.holidays(rows, 2026, HOLIDAYS_URL)
    assert [h["date"] for h in out["holidays"]] == [expected]


def test_a_row_from_another_year_is_excluded_and_recorded():
    rows = HOLIDAY_ROWS + [{"Holiday": "New Year's Day", "Date": "2027-01-01",
                            "Bus": "Sunday", "Rail": "Weekend"}]
    out = bundle.holidays(rows, 2026, HOLIDAYS_URL)
    assert "New Year's Day" not in [h["name"] for h in out["holidays"]]
    assert out["skipped"][0]["reason"] == "belongs to 2027, not 2026"


def test_an_unparseable_date_is_skipped_with_a_warning_not_guessed_at():
    rows = [{"Holiday": "Some Day", "Date": "the second Tuesday", "Bus": "Sunday",
             "Rail": "Weekend"}]
    out = bundle.holidays(rows, 2026, HOLIDAYS_URL)
    assert out["count"] == 0
    assert any("the second Tuesday" in w for w in out["warnings"])


def test_unrecognised_service_wording_is_flagged_not_coerced():
    rows = [{"Holiday": "X", "Date": "2026-07-04", "Bus": "Snow Schedule", "Rail": "Sunday"}]
    out = bundle.holidays(rows, 2026, HOLIDAYS_URL)
    holiday = out["holidays"][0]
    assert holiday["services"]["bus"]["service_type"] == "unrecognised"
    assert holiday["services"]["bus"]["published"] == "Snow Schedule"


def test_holidays_are_sorted_by_date():
    out = bundle.holidays(list(reversed(HOLIDAY_ROWS)), 2026, HOLIDAYS_URL)
    dates = [h["date"] for h in out["holidays"]]
    assert dates == sorted(dates)


# --------------------------------------------------------------- stops index --

def test_every_stop_code_in_the_feed_round_trips(conn):
    expected = {
        row["stop_code"]: row["stop_id"]
        for row in conn.execute("SELECT stop_code, stop_id FROM stops WHERE stop_code <> ''")
    }
    index = bundle.stops_index(conn)["stops"]
    assert set(index) == set(expected)
    for code, stop_id in expected.items():
        assert index[code]["stop_id"] == stop_id
    assert index["15111"]["name"] == "Main St & 1st"


def test_routes_are_attached_and_ordered_the_way_a_rider_reads_them(conn):
    index = bundle.stops_index(conn)["stops"]
    assert index["15111"]["routes"] == ["11", "RED"]
    assert index["90002"]["routes"] == ["RED"]
    # A station parent is served by no trip directly; its platform carries them.
    assert index["90001"]["routes"] == []


def test_numeric_routes_sort_numerically_not_lexicographically(tmp_path):
    routes = "route_id,route_short_name,route_long_name,route_type\nR11,11,A,3\nR2,2,B,3\n"
    trips = ("route_id,service_id,trip_id,trip_headsign,direction_id\n"
             "R11,WK,T1,X,0\nR2,WK,T2,Y,0\n")
    times = ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
             "T1,12:00:00,12:00:00,S1,1\nT2,12:05:00,12:05:00,S1,1\n")
    c = variant(tmp_path, "numeric", {"routes.txt": routes, "trips.txt": trips,
                                      "stop_times.txt": times})
    try:
        assert bundle.stops_index(c)["stops"]["15111"]["routes"] == ["2", "11"]
    finally:
        c.close()


def test_include_routes_false_leaves_the_lists_empty(conn):
    out = bundle.stops_index(conn, include_routes=False)
    assert all(entry["routes"] == [] for entry in out["stops"].values())
    assert out["includes_routes"] is False


def test_missing_route_tables_degrade_gracefully(tmp_path):
    """An optional table absent is a smaller index, not a crash: the numbers
    still resolve, they just carry no badges."""
    c = open_feed(tmp_path, "nostoptimes",
                  {k: v for k, v in fixtures.FILES.items()
                   if k not in ("stop_times.txt", "trips.txt", "routes.txt")})
    try:
        out = bundle.stops_index(c)
        assert out["count"] == 5
        assert any("Route badges omitted" in w for w in out["warnings"])
        assert all(entry["routes"] == [] for entry in out["stops"].values())
    finally:
        c.close()


def test_duplicate_stop_code_keeps_one_deterministically_and_reports_it(tmp_path):
    dupe = fixtures.STOPS.replace("S2,15112", "S2,15111")
    c = variant(tmp_path, "dupe", {"stops.txt": dupe})
    try:
        out = bundle.stops_index(c)
        assert out["stops"]["15111"]["stop_id"] == "S1"          # lowest id wins
        assert out["duplicate_codes"] == [{"stop_code": "15111", "kept": "S1", "also": "S2"}]
        assert any("stop_code_unique" in w for w in out["warnings"])
    finally:
        c.close()


def test_a_stop_without_a_code_is_counted_not_hidden(tmp_path):
    gap = fixtures.STOPS.replace("S3,15113", "S3,")
    c = variant(tmp_path, "gap", {"stops.txt": gap})
    try:
        out = bundle.stops_index(c)
        assert out["stops_without_code"] == ["S3"]
        assert any("cannot be reached by number" in w for w in out["warnings"])
    finally:
        c.close()


def test_a_feed_with_no_stop_code_column_raises_with_a_remedy(tmp_path):
    stops = "stop_id,stop_name,stop_lat,stop_lon\nS1,Main St,38.6,-90.1\n"
    c = variant(tmp_path, "nocode", {"stops.txt": stops})
    try:
        with pytest.raises(UsageError) as exc:
            bundle.stops_index(c)
        assert "stop-resolve" in exc.value.remedy
    finally:
        c.close()


# ------------------------------------------------------------------- compact --

def test_compact_actually_shrinks_the_database(built):
    out = built["balanced"]
    assert Path(out["out_path"]).is_file()
    assert out["compact"]["bytes"] < out["source"]["bytes"]
    assert 0 < out["ratio"] < 1
    assert out["reduction_pct"] > 0
    assert out["saved_bytes"] == out["source"]["bytes"] - out["compact"]["bytes"]
    assert out["compact"]["human"].endswith(("B", "KiB", "MiB"))


def test_every_decision_names_its_rationale(built):
    out = built["balanced"]
    assert out["decisions"]
    assert out["decision_count"] == len(out["decisions"])
    for d in out["decisions"]:
        assert d["rationale"].strip(), d
        assert d["detail"].strip(), d
        assert d["costs"].strip(), d
        assert isinstance(d["visible_to_user"], bool), d
        assert d["action"] in {"drop_table", "keep_table", "drop_column", "drop_index",
                               "prune_rows", "vacuum", "order"}


@pytest.mark.parametrize("strategy", ["minimal", "balanced", "full"])
def test_each_strategy_builds_and_says_what_it_drops(built, strategy):
    out = built[strategy]
    assert out["strategy"] == strategy
    assert out["strategy_drops"]["drops"] and out["strategy_drops"]["costs"]
    db = Path(out["out_path"])
    assert db.is_file() and db.stat().st_size > 0
    # Whatever else it drops, the departures path survives intact.
    assert {"stops", "routes", "trips", "stop_times", "calendar", "calendar_dates"} <= tables_in(db)


def test_minimal_is_smaller_than_balanced_is_smaller_than_full(built):
    sizes = {s: built[s]["compact"]["bytes"] for s in ("minimal", "balanced", "full")}
    assert sizes["minimal"] < sizes["balanced"] < sizes["full"]


def test_minimal_drops_the_accessibility_flag_and_says_what_that_costs(built):
    """The one drop in `minimal` a real rider notices, so it has to be labelled
    visible rather than buried among the invisible ones."""
    out = built["minimal"]
    decision = by_target(out)["stops.wheelchair_boarding"]
    assert decision["action"] == "drop_column"
    assert decision["visible_to_user"] is True
    assert "wheelchair" in decision["costs"].lower()
    assert decision in out["visible_decisions"]
    # And balanced keeps it, which is the reason balanced is the default.
    assert "stops.wheelchair_boarding" not in by_target(built["balanced"])


def test_shapes_is_dropped_and_the_drop_is_invisible(built):
    out = built["balanced"]
    decision = by_target(out)["shapes"]
    assert decision["action"] == "drop_table"
    assert decision["visible_to_user"] is False
    assert "map" in decision["rationale"].lower()
    assert decision["bytes_saved_estimate"] > 0
    assert "shapes" not in tables_in(Path(out["out_path"]))


@pytest.mark.parametrize("strategy", ["minimal", "balanced", "full"])
def test_calendar_dates_is_never_pruned_at_any_strategy(bulky, built, strategy):
    """Pruning shapes.txt is invisible. Pruning a calendar exception is a wrong
    departure time on Labor Day."""
    before = bulky.execute("SELECT COUNT(*) FROM calendar_dates").fetchone()[0]
    out = built[strategy]
    db = Path(out["out_path"])
    assert "calendar_dates" in tables_in(db)
    assert table_rows(db, "calendar_dates") == before
    kept = by_target(out)["calendar_dates"]
    assert kept["action"] == "keep_table"
    assert "Labor Day" in kept["rationale"]


def test_a_column_empty_on_every_row_is_dropped_as_measured(tmp_path):
    """Dropped because it was counted, not because someone assumed it. That is
    what makes 'this drop is invisible' a claim rather than a hope."""
    files = bulky_files()
    files["stops.txt"] = files["stops.txt"].replace(
        "Northbound side of the street opposite the shelter", "")
    c = open_feed(tmp_path, "emptycol", files)
    try:
        out = bundle.compact(c, tmp_path / "compact.sqlite", strategy="balanced")
        decision = by_target(out)["stops.stop_desc"]
        assert "empty on all 50 rows" in decision["detail"]
        assert decision["visible_to_user"] is False
        # `full` is the faithful copy, so it keeps even a useless column.
        full = bundle.compact(c, tmp_path / "full.sqlite", strategy="full")
        assert "stops.stop_desc" not in by_target(full)
    finally:
        c.close()


def test_vacuum_is_run_and_the_reclaimed_bytes_reported(built):
    out = built["balanced"]
    vacuum = by_target(out)[Path(out["out_path"]).name]
    assert vacuum["action"] == "vacuum"
    assert out["compact"]["reclaimed_by_vacuum_bytes"] >= 0
    assert out["compact"]["bytes"] <= out["compact"]["before_vacuum_bytes"]


def test_index_bytes_are_reported_apart_from_row_data(built):
    out = built["balanced"]
    assert out["compact"]["index_bytes"] > 0
    assert out["compact"]["row_data_bytes"] > 0
    assert (out["compact"]["index_bytes"] + out["compact"]["row_data_bytes"]
            == out["compact"]["bytes"])
    assert out["indices"], "the app's hot-path indices must survive into the bundle"
    assert all(i["why"] for i in out["indices"])


def test_minimal_builds_only_the_indices_the_departures_path_uses(built):
    """Indices are 19.7 MiB of the 52.9 MiB real feed, so which ones get rebuilt
    is a size decision like any other -- and it needs the same rationale."""
    minimal = built["minimal"]
    kept = {i["name"] for i in minimal["indices"]}
    assert "idx_st_stop" in kept, "the one query the app actually makes"
    assert "idx_st_trip" not in kept
    skipped = by_target(minimal)["idx_st_trip"]
    assert skipped["action"] == "drop_index"
    assert "489k" in skipped["costs"] or "scan" in skipped["costs"]
    # balanced keeps the lot: it is not trying to be the smallest thing possible.
    assert "idx_st_trip" in {i["name"] for i in built["balanced"]["indices"]}


def test_the_compressed_size_is_reported_because_that_is_the_shipping_cost(built):
    out = built["balanced"]
    assert 0 < out["compact"]["compressed_bytes"] < out["compact"]["bytes"]
    assert out["compact"]["compression_ratio"] < 1
    assert any("APK" in n for n in out["notes"])


def test_the_compact_database_still_answers_the_only_question_the_app_asks(built):
    out = built["minimal"]
    conn = connect_ro(Path(out["out_path"]))
    try:
        rows = conn.execute(
            "SELECT st.departure_time, r.route_short_name, t.trip_headsign "
            "FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id "
            "JOIN routes r ON r.route_id = t.route_id "
            "WHERE st.stop_id = ? AND st.departure_time >= ? "
            "ORDER BY st.departure_time LIMIT 5",
            ("S007", "12:00:00"),
        ).fetchall()
        assert rows and all(r["route_short_name"] and r["trip_headsign"] for r in rows)
    finally:
        conn.close()


def test_routes_filter_keeps_only_those_routes_and_flags_the_cost(bulky, tmp_path):
    out = bundle.compact(bulky, tmp_path / "compact.sqlite", routes=["MLR"])
    db = Path(out["out_path"])
    conn = connect_ro(db)
    try:
        assert {r[0] for r in conn.execute("SELECT DISTINCT route_id FROM trips")} == {"MLR"}
    finally:
        conn.close()
    prune = next(d for d in out["decisions"] if d["action"] == "prune_rows")
    assert prune["visible_to_user"] is True
    assert "does not exist" in prune["costs"]


def test_an_unknown_route_id_is_refused_with_a_remedy(bulky, tmp_path):
    with pytest.raises(UsageError) as exc:
        bundle.compact(bulky, tmp_path / "compact.sqlite", routes=["NOPE"])
    assert "NOPE" in exc.value.message
    assert "gtfs routes" in exc.value.remedy


def test_days_filter_truncates_coverage_and_says_so_loudly(bulky, tmp_path):
    out = bundle.compact(bulky, tmp_path / "compact.sqlite", days=3)
    window = out["filters"]["window"]
    assert window["from"] == "2026-01-01" and window["to"] == "2026-01-03"
    prune = next(d for d in out["decisions"] if d["action"] == "prune_rows")
    assert prune["visible_to_user"] is True
    assert "NO data" in prune["costs"]
    assert any("truncates coverage" in w for w in out["warnings"])
    assert table_rows(Path(out["out_path"]), "trips") < 100


def test_days_below_one_is_a_usage_error(bulky, tmp_path):
    with pytest.raises(UsageError):
        bundle.compact(bulky, tmp_path / "compact.sqlite", days=0)


def test_unknown_strategy_lists_the_real_ones(bulky, tmp_path):
    with pytest.raises(UsageError) as exc:
        bundle.compact(bulky, tmp_path / "compact.sqlite", strategy="tiny")
    assert exc.value.context["available"] == ["minimal", "balanced", "full"]


def test_an_absent_optional_table_is_handled_and_reported(tmp_path):
    """A feed missing calendar_dates and feed_info must still compact -- and
    must say that the exception table was missing, because a feed with no
    exceptions and a feed whose exceptions were lost look identical afterwards."""
    files = {k: v for k, v in bulky_files().items()
             if k not in ("calendar_dates.txt", "feed_info.txt", "shapes.txt")}
    c = open_feed(tmp_path, "sparse", files)
    try:
        out = bundle.compact(c, tmp_path / "compact.sqlite")
        assert out["ok"] is True
        assert any("calendar_dates.txt is absent" in w for w in out["warnings"])
        assert "shapes" not in by_target(out)  # nothing to drop, so nothing claimed
        assert "calendar_dates" not in tables_in(Path(out["out_path"]))
    finally:
        c.close()


def test_an_unrecognised_table_is_kept_and_flagged_rather_than_dropped(tmp_path):
    files = dict(bulky_files())
    files["mystery.txt"] = "a,b\n1,2\n"
    c = open_feed(tmp_path, "mystery", files)
    try:
        balanced = bundle.compact(c, tmp_path / "b.sqlite", strategy="balanced")
        assert "mystery" in tables_in(Path(balanced["out_path"]))
        minimal = bundle.compact(c, tmp_path / "m.sqlite", strategy="minimal")
        dropped = by_target(minimal)["mystery"]
        assert dropped["review"] is True
        assert dropped["visible_to_user"] is True
    finally:
        c.close()


def test_frequencies_appearing_is_a_warning_and_the_table_survives(tmp_path):
    files = dict(bulky_files())
    files["frequencies.txt"] = ("trip_id,start_time,end_time,headway_secs\n"
                                "T0000,06:00:00,09:00:00,600\n")
    c = open_feed(tmp_path, "freq", files)
    try:
        out = bundle.compact(c, tmp_path / "compact.sqlite", strategy="minimal")
        assert "frequencies" in tables_in(Path(out["out_path"]))
        assert any("second code path" in w for w in out["warnings"])
    finally:
        c.close()


# --------------------------------------------------------------- size report --

def test_size_report_isolates_indices_from_row_data(bulky, tmp_path):
    out = bundle.size_report(bulky)
    assert out["source"]["index_bytes"] > 0
    assert out["source"]["row_data_bytes"] > 0
    assert out["source"]["index_bytes"] + out["source"]["row_data_bytes"] == out["source"]["bytes"]
    assert 0 < out["source"]["index_share"] < 1


def test_size_report_names_the_table_worth_pruning(bulky):
    out = bundle.size_report(bulky)
    assert out["biggest_table"] == "stop_times"
    assert [t["table"] for t in out["tables"]][0] == "stop_times"
    assert out["tables"][0]["bytes_per_row"] > 0
    # Per-column, because with 5,000 rows a single column is measurable and
    # with 489,011 it is megabytes.
    columns = {c["column"]: c for c in out["tables"][0]["column_detail"]}
    assert columns["departure_time"]["text_bytes"] == 5000 * len("12:00:00")


def test_size_report_calls_out_the_compression_contribution_separately(bulky):
    out = bundle.size_report(bulky)
    source = out["source"]
    assert source["compressed_bytes"] < source["bytes"]
    assert source["compression_saves_bytes"] == source["bytes"] - source["compressed_bytes"]
    assert 0 < source["compression_ratio"] < 1


def test_size_report_compares_against_a_compact_build(bulky, built):
    out = bundle.size_report(bulky, Path(built["balanced"]["out_path"]))
    assert out["comparison"]["ratio"] < 1
    assert out["comparison"]["reduction_pct"] > 0
    assert out["comparison"]["saved_bytes"] > 0
    assert out["compact"]["index_bytes"] is not None


def test_size_report_flags_columns_that_are_empty_on_every_row(tmp_path):
    files = bulky_files()
    files["stops.txt"] = files["stops.txt"].replace(
        "Northbound side of the street opposite the shelter", "")
    c = open_feed(tmp_path, "emptycol", files)
    try:
        stops = next(t for t in bundle.size_report(c)["tables"] if t["table"] == "stops")
        desc = next(col for col in stops["column_detail"] if col["column"] == "stop_desc")
        assert desc["empty_on_every_row"] is True
        assert desc["non_empty"] == 0
    finally:
        c.close()


def test_size_report_survives_a_feed_missing_optional_tables(tmp_path):
    files = {k: v for k, v in fixtures.FILES.items() if k not in ("calendar_dates.txt",)}
    c = open_feed(tmp_path, "nocd", files)
    try:
        out = bundle.size_report(c)
        assert "calendar_dates" not in {t["table"] for t in out["tables"]}
        assert out["total_rows"] > 0
    finally:
        c.close()


# ------------------------------------------------------------------ manifest --

def test_manifest_hashes_are_stable(conn, tmp_path):
    artifacts = [
        bundle.fares(FARE_ROWS, AS_OF, FARES_URL),
        bundle.holidays(HOLIDAY_ROWS, 2026, HOLIDAYS_URL),
        bundle.stops_index(conn),
    ]
    first = bundle.manifest(artifacts, "gtfs-20260803T190539Z-f2d721", "a" * 64)
    second = bundle.manifest(artifacts, "gtfs-20260803T190539Z-f2d721", "a" * 64)
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["rendered"] == second["rendered"]
    assert len(first["manifest_sha256"]) == 64
    assert [a["name"] for a in first["artifacts"]] == [
        "fares.json", "holidays-2026.json", "stops_index.json"]
    assert all(len(a["sha256"]) == 64 for a in first["artifacts"])


def test_manifest_carries_no_wall_clock_timestamp(conn):
    """A generated-at field would make two runs of the same build differ. The
    snapshot id already carries the fetch time to the second."""
    out = bundle.manifest([bundle.stops_index(conn)], "gtfs-20260803T190539Z-f2d721", "b" * 64)
    assert "generated_at" not in out["rendered"]
    assert "gtfs-20260803T190539Z-f2d721" in out["rendered"]


def test_manifest_hashes_the_file_that_shipped_not_the_claim(built):
    out = bundle.manifest([built["balanced"]], "gtfs-x", "c" * 64)
    record = out["artifacts"][0]
    path = Path(built["balanced"]["out_path"])
    assert record["kind"] == "compact_db"
    assert record["sha256"] == bundle.sha256_file(path)
    assert record["bytes"] == path.stat().st_size


def test_a_tampered_artifact_changes_the_manifest_hash(bulky, tmp_path):
    built = bundle.compact(bulky, tmp_path / "compact.sqlite")
    before = bundle.manifest([built], "gtfs-x", "c" * 64)["manifest_sha256"]
    with Path(built["out_path"]).open("ab") as fh:
        fh.write(b"\x00")
    after = bundle.manifest([built], "gtfs-x", "c" * 64)["manifest_sha256"]
    assert before != after


def test_an_artifact_with_no_hash_is_warned_about(tmp_path):
    out = bundle.manifest([{"name": "handwritten.json"}], "gtfs-x", "d" * 64)
    assert any("cannot be traced" in w for w in out["warnings"])
    assert out["artifacts"][0]["sha256"] == ""


def test_manifest_sorts_artifacts_regardless_of_argument_order(conn):
    a = bundle.fares(FARE_ROWS, AS_OF, FARES_URL)
    b = bundle.stops_index(conn)
    forwards = bundle.manifest([a, b], "gtfs-x", "e" * 64)
    backwards = bundle.manifest([b, a], "gtfs-x", "e" * 64)
    assert forwards["rendered"] == backwards["rendered"]


# ---------------------------------------------------------------- determinism --

def test_json_artifacts_are_byte_identical_across_two_runs(conn):
    """Spec 2.8. Byte-identity is what makes these files diffable in review: a
    change in the diff means the FEED moved, not that the generator ran again."""
    for build in (
        lambda: bundle.fares(FARE_ROWS, AS_OF, FARES_URL),
        lambda: bundle.fares(FARE_ROWS, AS_OF, FARES_URL, fmt="kotlin"),
        lambda: bundle.holidays(HOLIDAY_ROWS, 2026, HOLIDAYS_URL),
        lambda: bundle.stops_index(conn),
        lambda: bundle.stops_index(conn, include_routes=False),
    ):
        first, second = build(), build()
        assert first["rendered"] == second["rendered"]
        assert first["artifact"]["sha256"] == second["artifact"]["sha256"]


def test_the_compact_database_is_byte_identical_across_two_runs(bulky, tmp_path):
    first = bundle.compact(bulky, tmp_path / "one.sqlite")
    second = bundle.compact(bulky, tmp_path / "two.sqlite")
    assert Path(first["out_path"]).read_bytes() == Path(second["out_path"]).read_bytes()
    assert first["artifact"]["sha256"] == second["artifact"]["sha256"]


def test_the_compact_report_itself_is_deterministic(bulky, tmp_path):
    """No elapsed-time field, no timestamps: the report is committed alongside
    the file it describes and must not churn."""
    # Same filename in two directories: the only thing allowed to differ
    # between the two reports is where the file was written.
    first = bundle.compact(bulky, tmp_path / "a" / "compact.sqlite")
    second = bundle.compact(bulky, tmp_path / "b" / "compact.sqlite")
    for out in (first, second):
        out.pop("out_path")
        out["source"].pop("path")
        out["artifact"].pop("path")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_rebuilding_over_an_existing_file_replaces_it(bulky, tmp_path):
    path = tmp_path / "compact.sqlite"
    path.write_bytes(b"stale bytes that are not a database")
    out = bundle.compact(bulky, path)
    assert path.stat().st_size == out["compact"]["bytes"]
    assert "stop_times" in tables_in(path)


# --------------------------------------------------------------- small stuff --

@pytest.mark.parametrize(
    "value,rendered",
    [(0, "0 B"), (512, "512 B"), (1024, "1.0 KiB"), (1536, "1.5 KiB"),
     (5 * 1024 ** 2, "5.0 MiB"), (None, "n/a")],
)
def test_human_bytes_is_unambiguous(value, rendered):
    assert bundle.human_bytes(value) == rendered


def test_canonical_json_sorts_keys_and_ends_with_a_newline():
    text = bundle.canonical_json({"b": 1, "a": {"d": 2, "c": 3}})
    assert text.endswith("\n")
    assert text.index('"a"') < text.index('"b"')
    assert text.index('"c"') < text.index('"d"')
    assert json.loads(text) == {"b": 1, "a": {"d": 2, "c": 3}}
