"""Tests for the `web` group.

NO NETWORK. Every fixture is an HTML string defined here, because the point of
the group is to survive Metro redesigning a page and the only way to test that
is to hand it a redesigned page. A test that fetched the live site would pass
today, fail the week Metro restyles, and tell you nothing either time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stl_transit.core import web
from stl_transit.core.web import extract as ex
from stl_transit.errors import ExtractionFailed, PageNotFound, UsageError

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ fixtures --

def page(body: str, head: str = "") -> str:
    """A page wrapped in the chrome every real Metro page carries."""
    return f"""<!DOCTYPE html>
<html><head><title>Metro Transit</title>{head}
<link rel="stylesheet" href="/style.css?ver=6.4.3">
<style>.hero {{ background: url(/hero-a.jpg); }}</style>
</head>
<body>
<nav><a href="/">Home</a> <a href="/fares-and-passes/">Fares</a></nav>
<main>{body}</main>
<footer><p>Bi-State Development</p></footer>
<script nonce="f3a9c1d2e4b5a6f7">window.__ANALYTICS_ID__="UA-9931-2";</script>
</body></html>"""


FARES_TABLE = page("""
<h2>MetroBus and MetroLink Fares</h2>
<table>
  <tr><th>Fare Type</th><th>Price</th><th>Notes</th></tr>
  <tr><td>Adult 2-Hour Pass</td><td>$2.50</td><td>Transfers included</td></tr>
  <tr><td>Reduced Fare</td><td>$1</td><td>Seniors, Medicare, disability</td></tr>
  <tr><td>Children under 5</td><td>$0.00</td><td>With a fare-paying adult</td></tr>
  <tr><td>Monthly Pass</td><td>$78.00</td><td>Calendar month</td></tr>
</table>
""")

FARES_DL = page("""
<h3>MetroLink Fares</h3>
<dl>
  <dt>Adult One-Ride</dt><dd>$2.50 valid for two hours</dd>
  <dt>Day Pass</dt><dd>$7.50</dd>
</dl>
""")

FARES_PROSE = page("""
<h2>Fares</h2>
<p>Adult 2-Hour Pass: $2.50</p>
<ul>
  <li>Reduced Fare - $1</li>
  <li>Children under 5: Free</li>
</ul>
""")

FARES_REDESIGNED = page("""
<h2>Fares and Passes</h2>
<p>Fares are now managed in the Transit app. Download it to see your options.</p>
""")

HOLIDAYS_COLUMNS = page("""
<h2>2026 Holiday Schedules</h2>
<table>
  <tr><th>Holiday</th><th>Date</th><th>MetroBus</th><th>MetroLink</th><th>Call-A-Ride</th></tr>
  <tr><td>Labor Day</td><td>September 7, 2026</td><td>Sunday Schedule</td>
      <td>Weekend Schedule</td><td>Limited Service</td></tr>
  <tr><td>Veterans Day</td><td>November 11, 2026</td><td>Regular Weekday Schedule</td>
      <td>Regular Weekday Schedule</td><td>Regular Service</td></tr>
</table>
""")

HOLIDAYS_PER_MODE = page("""
<h2>MetroBus Holiday Service</h2>
<table>
  <tr><th>Holiday</th><th>Service</th></tr>
  <tr><td>Labor Day</td><td>Sunday Schedule</td></tr>
</table>
<h2>MetroLink Holiday Service</h2>
<table>
  <tr><th>Holiday</th><th>Service</th></tr>
  <tr><td>Labor Day</td><td>Weekend Schedule</td></tr>
</table>
""")

HOLIDAYS_BUS_ONLY = page("""
<h2>MetroBus Holiday Service</h2>
<table>
  <tr><th>Holiday</th><th>Service</th></tr>
  <tr><td>Labor Day</td><td>Sunday Schedule</td></tr>
</table>
""")

HOLIDAYS_PROSE = page("""
<h2>Holiday Service</h2>
<p>Labor Day: MetroBus operates a Sunday schedule; MetroLink operates a Weekend schedule.</p>
""")

HOLIDAYS_REDESIGNED = page("""
<h2>Holiday Schedules</h2>
<p>See the Transit app for holiday service.</p>
""")

SCHEDULE_CHANGES = page("""
<h1>Upcoming Schedule Changes</h1>
<ul>
  <li><a href="https://www.metrostlouis.org/wp-content/uploads/schedules/2026-08-24/11-chippewa.pdf">Route 11</a></li>
  <li><a href="https://www.metrostlouis.org/wp-content/uploads/schedules/2026-08-24/70-grand.pdf">Route 70</a></li>
  <li><a href="/documents/system-map.pdf?pickId=2026F">System Map</a></li>
</ul>
""")

SCHEDULE_CHANGES_REDESIGNED = page("""
<h1>Upcoming Schedule Changes</h1>
<p>Schedules are available in the Transit app.</p>
<a href="https://www.metrostlouis.org/rider-alerts/">Rider Alerts</a>
""")

ALERTS = page("""
<h1>Rider Alerts</h1>
<article class="rider-alert">
  <h2>Route 70 Grand Detour</h2>
  <p class="posted">Posted: August 1, 2026</p>
  <p>Northbound buses are detouring around bridge work at Grand and Chouteau.</p>
</article>
<article class="rider-alert">
  <h2>MetroLink Weekend Single-Tracking</h2>
  <p>Trains share one track between Forest Park and Central West End.</p>
</article>
""")

ALERTS_EMPTY = page("""
<h1>Rider Alerts</h1>
<p>There are no active alerts at this time.</p>
""", head='<meta name="robots" content="noarchive, nosnippet">')

ALERTS_REDESIGNED = page("<p>Sign up for text notifications.</p>")

PURCHASE = page("""
<h1>How to Pay Your Fare</h1>
<p>Tap your Gateway Card at the validator before boarding.</p>
<p>Reload online, by phone, or at a MetroRide store.</p>
""")

EMPTY_SHELL = """<!DOCTYPE html><html><head><title>Metro</title>
<style>body{color:#333}</style></head>
<body><div id="root"></div><script>hydrate();</script></body></html>"""


# ----------------------------------------------------------------- normalize --

def test_normalize_drops_scripts_styles_and_head():
    text = ex.normalize(FARES_TABLE)
    assert "__ANALYTICS_ID__" not in text
    assert "stylesheet" not in text
    assert "background" not in text
    assert "Adult 2-Hour Pass" in text


def test_normalize_is_line_oriented():
    # One line per block element is what makes `web diff` readable; a single
    # collapsed line would diff as "everything changed".
    lines = ex.normalize(FARES_TABLE).split("\n")
    assert "Adult 2-Hour Pass" in lines
    assert "$2.50" in lines


def test_normalize_collapses_whitespace():
    text = ex.normalize("<p>Adult\n\n   2-Hour\tPass</p>")
    assert text == "Adult 2-Hour Pass"


def test_normalize_strips_nonce_shaped_tokens():
    html = """<p>build 9f2c4ab7e1d0c3b5a6 session
    3f2504e0-4f89-11d3-9a0c-0305e82c3301 at 2026-08-03T12:00:00Z
    id AKfycbx9QpZ1rT4mN8vLd2Ws7Yy build 1754236800000</p>"""
    text = ex.normalize(html)
    assert "9f2c4ab7e1d0c3b5a6" not in text
    assert "3f2504e0" not in text
    assert "2026-08-03T12:00:00Z" not in text
    assert "AKfycbx9QpZ1rT4mN8vLd2Ws7Yy" not in text
    assert "1754236800000" not in text
    assert "[nonce]" in text and "[timestamp]" in text


def test_normalize_keeps_human_dates_and_phone_numbers():
    # The nonce filter must not eat content. "September 7, 2026" is the holiday
    # table, and a 10-digit number is Metro's phone number, not an epoch.
    text = ex.normalize("<p>September 7, 2026. Call 3149822000 or 314-982-1400.</p>")
    assert "September 7, 2026" in text
    assert "3149822000" in text


def test_normalize_stable_across_cosmetically_different_renderings():
    a = page("""<h2>Fares</h2><div class="wrap"><p>Adult 2-Hour Pass: $2.50</p></div>""")
    b = """<!DOCTYPE html>
<html><head><title>Metro Transit</title>
<link rel="stylesheet" href="/style.css?ver=6.5.1">
<style>.hero { background: url(/hero-b.jpg); }</style></head>
<body>
<nav><a href="/">Home</a> <a href="/fares-and-passes/">Fares</a></nav>
<main>
  <h2   class="title" data-render="8c1f9b2a4d6e0f3a">Fares</h2>
  <div class="wrap" id="wrap-7d3f1a9c2b4e6081">
     <p>Adult 2-Hour   Pass: $2.50</p>
  </div>
</main>
<footer><p>Bi-State Development</p></footer>
<script nonce="0a1b2c3d4e5f6a7b">window.__ANALYTICS_ID__="UA-1111-9";</script>
</body></html>"""
    assert ex.normalize(a) == ex.normalize(b)


def test_content_hash_stable_under_cosmetic_change_and_moves_on_real_change():
    a = page("<h2>Fares</h2><p>Adult 2-Hour Pass: $2.50</p>")
    cosmetic = page('<h2 class="t">Fares</h2>\n\n  <p>Adult 2-Hour   Pass: $2.50</p>')
    real = page("<h2>Fares</h2><p>Adult 2-Hour Pass: $2.75</p>")
    assert ex.content_hash(ex.normalize(a)) == ex.content_hash(ex.normalize(cosmetic))
    assert ex.content_hash(ex.normalize(a)) != ex.content_hash(ex.normalize(real))


def test_main_text_excludes_site_chrome():
    # Hash the content region, not the page: a site-wide footer edit must not
    # read as a fare change (spec 6.7).
    result = ex.main_text(FARES_TABLE)
    assert result["strategy"] == "main_element"
    assert "Bi-State Development" not in result["text"]
    assert "Adult 2-Hour Pass" in result["text"]


# ---------------------------------------------------------------- fare_table --

def test_fare_table_from_html_table():
    out = ex.extract(FARES_TABLE, "fare_table")
    assert out["strategy"] == "table"
    by_type = {r["fare_type"]: r for r in out["rows"]}
    assert by_type["Adult 2-Hour Pass"]["price_cents"] == 250
    assert by_type["Adult 2-Hour Pass"]["category"] == "MetroBus and MetroLink Fares"
    assert "Transfers included" in by_type["Adult 2-Hour Pass"]["notes"]
    assert by_type["Monthly Pass"]["price_cents"] == 7800


def test_fare_prices_parse_to_integer_cents():
    rows = {r["fare_type"]: r for r in ex.extract(FARES_TABLE, "fare_table")["rows"]}
    assert rows["Adult 2-Hour Pass"]["price_cents"] == 250
    assert rows["Adult 2-Hour Pass"]["price_usd"] == "2.50"
    # "$1" with no decimal part is one hundred cents, not one.
    assert rows["Reduced Fare"]["price_cents"] == 100
    assert rows["Reduced Fare"]["price_usd"] == "1.00"
    assert rows["Children under 5"]["price_cents"] == 0
    assert rows["Children under 5"]["price_usd"] == "0.00"
    assert all(isinstance(r["price_cents"], int) for r in rows.values())


def test_parse_money_handles_free_ranges_and_commas():
    assert ex.parse_money("Free")["price_cents"] == 0
    assert ex.parse_money("Free")["basis"] == "free_text"
    assert ex.parse_money("$1,250.75")["price_cents"] == 125075
    ranged = ex.parse_money("$2.50 - $5.00")
    assert ranged["price_cents"] == 250
    assert ranged["prices_cents"] == [250, 500]
    assert ranged["is_range"] is True
    assert ex.parse_money("no price here") is None


def test_fare_table_from_definition_list():
    out = ex.extract(FARES_DL, "fare_table")
    assert out["strategy"] == "definition_list"
    rows = {r["fare_type"]: r for r in out["rows"]}
    assert rows["Adult One-Ride"]["price_cents"] == 250
    assert rows["Adult One-Ride"]["notes"] == "valid for two hours"
    assert rows["Day Pass"]["price_cents"] == 750
    assert rows["Day Pass"]["category"] == "MetroLink Fares"


def test_fare_table_falls_back_to_labelled_lines():
    out = ex.extract(FARES_PROSE, "fare_table")
    assert out["strategy"] == "labelled_lines"
    rows = {r["fare_type"]: r["price_cents"] for r in out["rows"]}
    assert rows["Adult 2-Hour Pass"] == 250
    assert rows["Reduced Fare"] == 100
    assert rows["Children under 5"] == 0
    # Every strategy is reported, so a future reader knows which one to fix.
    assert [a["strategy"] for a in out["attempts"]] == [
        "table", "definition_list", "labelled_lines"
    ]


def test_fare_table_raises_when_the_structure_is_gone():
    with pytest.raises(ExtractionFailed) as caught:
        ex.extract(FARES_REDESIGNED, "fare_table")
    assert caught.value.code == "EXTRACTION_FAILED"
    assert caught.value.exit_code == 4
    assert "bundle fares" in caught.value.remedy
    assert caught.value.context["strategies_tried"]


# ------------------------------------------------------------- holiday_table --

def test_holiday_table_keeps_bus_and_rail_distinct():
    out = ex.extract(HOLIDAYS_COLUMNS, "holiday_table")
    assert out["strategy"] == "mode_columns"
    labor = out["rows"][0]
    assert labor["holiday"] == "Labor Day"
    assert labor["date_iso"] == "2026-09-07"
    # The bug this guards: MetroBus maps to Sunday, MetroLink to Weekend. They
    # are different concepts and must never be collapsed into one value.
    assert labor["bus_service"] == "Sunday Schedule"
    assert labor["rail_service"] == "Weekend Schedule"
    assert labor["bus_service"] != labor["rail_service"]


def test_holiday_table_keeps_a_third_mode_rather_than_dropping_it():
    labor = ex.extract(HOLIDAYS_COLUMNS, "holiday_table")["rows"][0]
    assert labor["other_services"]["Call-A-Ride"] == "Limited Service"


def test_holiday_table_records_a_non_service_holiday():
    veterans = ex.extract(HOLIDAYS_COLUMNS, "holiday_table")["rows"][1]
    assert veterans["holiday"] == "Veterans Day"
    assert veterans["date_iso"] == "2026-11-11"
    assert veterans["bus_service"] == "Regular Weekday Schedule"
    assert veterans["rail_service"] == "Regular Weekday Schedule"


def test_holiday_table_merges_one_table_per_mode():
    out = ex.extract(HOLIDAYS_PER_MODE, "holiday_table")
    assert out["strategy"] == "mode_per_table"
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["bus_service"] == "Sunday Schedule"
    assert row["rail_service"] == "Weekend Schedule"


def test_holiday_table_never_copies_bus_service_into_rail():
    out = ex.extract(HOLIDAYS_BUS_ONLY, "holiday_table")
    row = out["rows"][0]
    assert row["bus_service"] == "Sunday Schedule"
    assert row["rail_service"] is None
    assert out["with_rail_service"] == 0
    assert any("NOT assumed to match bus" in w for w in out["warnings"])


def test_holiday_table_falls_back_to_prose():
    out = ex.extract(HOLIDAYS_PROSE, "holiday_table")
    assert out["strategy"] == "prose_lines"
    row = out["rows"][0]
    assert row["holiday"] == "Labor Day"
    assert row["bus_service"].lower() == "sunday schedule"
    assert row["rail_service"].lower() == "weekend schedule"


def test_holiday_table_leaves_date_iso_null_without_a_year():
    # The year is never guessed: a mapping that assumes one is wrong for four
    # months a year and silently right the rest of the time.
    out = ex.extract(page("""
      <h2>Holidays</h2>
      <table><tr><th>Holiday</th><th>MetroBus</th><th>MetroLink</th></tr>
      <tr><td>Labor Day</td><td>Sunday Schedule</td><td>Weekend Schedule</td></tr></table>
    """), "holiday_table")
    assert out["rows"][0]["date_iso"] is None


def test_holiday_table_raises_when_the_structure_is_gone():
    with pytest.raises(ExtractionFailed) as caught:
        ex.extract(HOLIDAYS_REDESIGNED, "holiday_table")
    assert caught.value.exit_code == 4
    assert "bundle holidays" in caught.value.remedy


# ------------------------------------------------------------------- pick_id --

def test_pick_id_finds_ids_in_schedule_pdf_urls():
    out = ex.extract(SCHEDULE_CHANGES, "pick_id")
    assert out["pick_ids"] == ["2026-08-24", "2026F"]
    assert out["pdf_urls_seen"] == 3


def test_pick_id_reports_every_url_an_id_came_from():
    items = {i["pick_id"]: i for i in ex.extract(SCHEDULE_CHANGES, "pick_id")["items"]}
    assert items["2026-08-24"]["url_count"] == 2
    assert items["2026-08-24"]["strategy"] == "schedule_path"
    assert items["2026F"]["strategy"] == "pick_query"
    assert all(u.endswith(".pdf") for u in items["2026-08-24"]["urls"])


def test_pick_id_raises_when_no_schedule_links_remain():
    with pytest.raises(ExtractionFailed) as caught:
        ex.extract(SCHEDULE_CHANGES_REDESIGNED, "pick_id")
    assert caught.value.exit_code == 4
    assert "PICK_PATTERNS" in caught.value.remedy


# ---------------------------------------------------------------- alert_list --

def test_alert_list_reads_articles():
    out = ex.extract(ALERTS, "alert_list")
    assert out["strategy"] == "article"
    assert out["count"] == 2
    first = out["alerts"][0]
    assert first["title"] == "Route 70 Grand Detour"
    assert "detouring around bridge work" in first["body"]
    assert first["posted"] == "August 1, 2026"
    assert first["posted_iso"] == "2026-08-01"
    assert out["alerts"][1]["posted"] is None


def test_alert_list_empty_page_is_not_a_failure():
    # "No active alerts" is the page's own claim, and evidence is not silence.
    out = ex.extract(ALERTS_EMPTY, "alert_list")
    assert out["alerts"] == []
    assert out["count"] == 0
    assert out["strategy"] == "declared_empty"
    assert "no active alerts" in out["empty_reason"].lower()


def test_alert_list_raises_when_empty_without_saying_so():
    with pytest.raises(ExtractionFailed) as caught:
        ex.extract(ALERTS_REDESIGNED, "alert_list")
    assert caught.value.exit_code == 4
    assert "NO_ALERTS" in caught.value.remedy


# ---------------------------------------------------------------------- text --

def test_text_reports_word_count_and_first_heading():
    out = ex.extract(PURCHASE, "text")
    assert out["first_heading"] == "How to Pay Your Fare"
    assert out["word_count"] > 10
    assert "Gateway Card" in out["text"]
    assert "Bi-State Development" not in out["text"]  # chrome excluded


def test_text_raises_on_a_javascript_shell():
    with pytest.raises(ExtractionFailed) as caught:
        ex.extract(EMPTY_SHELL, "text")
    assert caught.value.exit_code == 4
    assert "headless browser" in caught.value.remedy


# ------------------------------------------------------------------ dispatch --

def test_unknown_extractor_is_a_usage_error_not_an_extraction_failure():
    # A typo in sources.toml and a Metro redesign need different remedies.
    with pytest.raises(UsageError) as caught:
        ex.extract(PURCHASE, "fare_tables")
    assert caught.value.exit_code == 2
    assert "sources.toml" in caught.value.remedy


# ------------------------------------------------------------------- capture --

def test_capture_record_fields():
    rec = web.capture("fares", FARES_TABLE, "https://example.org/fares/", NOW, "fare_table")
    assert rec["ok"] is True
    assert rec["page"] == "fares"
    assert rec["extractor"] == "fare_table"
    assert rec["fetched_at"] == "2026-08-03T12:00:00+00:00"
    assert rec["content_hash"] == ex.content_hash(rec["normalized_text"])
    assert rec["bytes_raw"] == len(FARES_TABLE.encode("utf-8"))
    assert 0 < rec["bytes_normalized"] < rec["bytes_raw"]
    assert rec["extraction"]["rows"][0]["price_cents"] == 250


def test_capture_reads_robots_directives_off_the_page():
    rec = web.capture("rider_alerts", ALERTS_EMPTY, "https://example.org/rider-alerts/",
                      NOW, "alert_list")
    assert rec["robots"]["noarchive"] is True
    assert rec["robots"]["nosnippet"] is True
    assert any("spec 9" in n for n in rec["notes"])


def test_capture_records_extraction_failure_instead_of_raising():
    # The redesign is exactly when you want the bytes on disk.
    rec = web.capture("fares", FARES_REDESIGNED, "https://example.org/fares/", NOW,
                      "fare_table")
    assert rec["ok"] is False
    assert rec["extraction_ok"] is False
    assert rec["extraction"] is None
    assert rec["extraction_error"]["code"] == "EXTRACTION_FAILED"
    assert rec["extraction_error"]["remedy"]
    assert rec["content_hash"]  # still hashed, still comparable


def test_capture_rejects_an_unknown_extractor_name():
    with pytest.raises(UsageError):
        web.capture("fares", FARES_TABLE, "https://example.org/fares/", NOW, "nope")


# ------------------------------------------------------------------- compare --

def _capture(html: str, when: datetime, page_key: str = "fares",
             extractor: str = "fare_table") -> dict:
    return web.capture(page_key, html, "https://example.org/fares/", when, extractor)


def test_compare_reports_no_change():
    before = _capture(FARES_TABLE, NOW)
    after = _capture(FARES_TABLE, NOW + timedelta(days=1))
    out = web.compare(before, after)
    assert out["changed"] is False
    assert out["diff"]["unified"] == []
    assert "No change" in out["verdict"]


def test_compare_names_the_extracted_fields_that_moved():
    after_html = FARES_TABLE.replace("$2.50", "$2.75")
    out = web.compare(_capture(FARES_TABLE, NOW), _capture(after_html, NOW + timedelta(days=1)))
    assert out["changed"] is True
    assert any("price_cents" in f for f in out["fields_changed"])
    change = next(c for c in out["extraction"]["changes"] if c["field"].endswith("price_cents"))
    assert (change["before"], change["after"]) == (250, 275)
    assert any(line.startswith("+$2.75") for line in out["diff"]["unified"])


def test_compare_flags_an_extraction_that_stopped_working():
    out = web.compare(_capture(FARES_TABLE, NOW),
                      _capture(FARES_REDESIGNED, NOW + timedelta(days=1)))
    assert out["changed"] is True
    assert out["extraction"]["comparable"] is False
    assert out["extraction"]["state"] == "extraction_failed_after"


def test_compare_bounds_the_diff_and_says_so():
    # Scattered changes so difflib cannot merge them into one hunk.
    before_body = "".join(f"<p>Line {i} unchanged text</p>" for i in range(250))
    after_body = "".join(
        f"<p>Line {i} {'CHANGED' if i % 10 == 0 else 'unchanged'} text</p>"
        for i in range(250)
    )
    before = web.capture("purchase", page(before_body), "u", NOW, "text")
    after = web.capture("purchase", page(after_body), "u", NOW + timedelta(days=1), "text")
    out = web.compare(before, after)
    diff = out["diff"]
    assert diff["truncated"] is True
    assert diff["hunks"] > web.MAX_DIFF_HUNKS
    assert diff["hunks_shown"] <= web.MAX_DIFF_HUNKS
    assert len(diff["unified"]) <= web.MAX_DIFF_LINES
    assert "hunk(s) shown" in diff["note"]
    # Counts describe the whole diff, not the shown part: a bounded diff that
    # also under-reported the size would be worse than none.
    assert diff["lines_added"] == 25


def test_compare_refuses_two_different_pages():
    with pytest.raises(UsageError) as caught:
        web.compare(_capture(FARES_TABLE, NOW),
                    _capture(PURCHASE, NOW, page_key="purchase", extractor="text"))
    assert "same page" in caught.value.remedy


def test_compare_refuses_something_that_is_not_a_capture():
    with pytest.raises(UsageError):
        web.compare(FARES_TABLE, FARES_TABLE)  # type: ignore[arg-type]


# --------------------------------------------------------------------- check --

def test_check_reports_no_drift():
    older = _capture(FARES_TABLE, NOW - timedelta(days=1))
    newer = _capture(FARES_TABLE, NOW)
    out = web.check({"fares": [newer, older]})
    assert out["ok"] is True
    assert out["drift_detected"] is False
    assert out["items"][0]["status"] == "unchanged"
    assert "No drift" in out["headline"]


def test_check_detects_drift_and_says_what_it_breaks():
    older = _capture(FARES_TABLE, NOW - timedelta(days=1))
    newer = _capture(FARES_TABLE.replace("$2.50", "$2.75"), NOW)
    out = web.check({"fares": [newer, older]})
    assert out["drift_detected"] is True
    assert out["ok"] is False
    item = out["items"][0]
    assert item["changed"] is True
    assert item["severity"] == "alarming"
    assert item["assertion"] == "fares_unchanged"
    assert "lying to users" in item["breaks"]
    assert item["current_hash"] == newer["content_hash"]
    assert item["previous_hash"] == older["content_hash"]


def test_check_single_capture_cannot_answer_changed_yet():
    out = web.check({"fares": [_capture(FARES_TABLE, NOW)]})
    assert out["items"][0]["status"] == "single_capture"
    assert out["items"][0]["changed"] is False
    assert out["drift_detected"] is False
    assert out["ok"] is True


def test_check_reports_a_page_that_was_never_captured():
    out = web.check({"fares": []})
    item = out["items"][0]
    assert item["status"] == "never_captured"
    assert item["captures"] == 0
    assert out["ok"] is False
    assert out["drift_detected"] is False  # nothing to drift from


def test_check_reports_an_extraction_that_broke():
    out = web.check({
        "fares": [_capture(FARES_REDESIGNED, NOW), _capture(FARES_REDESIGNED,
                                                            NOW - timedelta(days=1))],
    })
    assert out["items"][0]["status"] == "extraction_failed"
    assert out["ok"] is False


def test_check_orders_alarming_pages_first():
    fares = _capture(FARES_TABLE, NOW)
    alerts = web.capture("rider_alerts", ALERTS, "u", NOW, "alert_list")
    out = web.check({"rider_alerts": [alerts], "fares": [fares]})
    assert [i["page"] for i in out["items"]] == ["fares", "rider_alerts"]
    assert out["items"][1]["severity"] == "routine"


def test_check_with_nothing_on_record_raises():
    with pytest.raises(PageNotFound) as caught:
        web.check({})
    assert "web capture" in caught.value.remedy


def test_check_tolerates_captures_given_oldest_first():
    older = _capture(FARES_TABLE, NOW - timedelta(days=1))
    newer = _capture(FARES_TABLE.replace("$2.50", "$2.75"), NOW)
    out = web.check({"fares": [older, newer]})
    assert out["items"][0]["current_hash"] == newer["content_hash"]


# -------------------------------------------------------------- should_fetch --

def test_should_fetch_allows_the_first_capture():
    allowed, reason = web.should_fetch("fares", None, 24, NOW)
    assert allowed is True
    assert "first fetch" in reason


def test_should_fetch_blocks_inside_the_interval():
    allowed, reason = web.should_fetch("fares", NOW - timedelta(hours=3), 24, NOW)
    assert allowed is False
    assert "3.0 h since" in reason
    assert "21.0 h" in reason


def test_should_fetch_allows_once_the_interval_has_passed():
    allowed, reason = web.should_fetch("holidays", NOW - timedelta(hours=169), 168, NOW)
    assert allowed is True
    assert "169.0 h" in reason


def test_should_fetch_honours_a_longer_configured_interval():
    # The holidays page is on a weekly interval; 48 hours is not enough.
    allowed, _ = web.should_fetch("holidays", NOW - timedelta(hours=48), 168, NOW)
    assert allowed is False


def test_should_fetch_clamps_to_the_one_fetch_per_day_floor():
    # Spec 9 caps HTML pages at one fetch per day, whatever config says.
    allowed, reason = web.should_fetch("fares", NOW - timedelta(hours=2), 1, NOW)
    assert allowed is False
    assert "floor" in reason
    assert f"{web.MIN_FETCH_INTERVAL_HOURS:g} h" in reason


def test_should_fetch_treats_a_naive_timestamp_as_utc():
    naive = datetime(2026, 8, 2, 12, 0)
    allowed, _ = web.should_fetch("fares", naive, 24, NOW)
    assert allowed is True
    allowed, _ = web.should_fetch("fares", datetime(2026, 8, 3, 6, 0), 24, NOW)
    assert allowed is False


def test_should_fetch_refuses_on_a_clock_running_backwards():
    allowed, reason = web.should_fetch("fares", NOW + timedelta(hours=5), 24, NOW)
    assert allowed is False
    assert "future" in reason
