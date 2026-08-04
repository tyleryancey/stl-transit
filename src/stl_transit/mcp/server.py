"""MCP server: `stl_transit_mcp`.

A registration file and nothing more. Every tool is a thin async wrapper over a
`core.service` function, exactly as the spec's section 10 promised.

Deliberately a CURATED subset of the CLI, not all of it. A large tool list
burns context and degrades selection accuracy; `stl_gtfs_query` is the escape
hatch that keeps the surface small without making it feel crippled.
"""

from __future__ import annotations

import json
from typing import Any

from ..core import oracle, service
from ..errors import StlError

# mcp >= 2.0 renamed FastMCP to MCPServer. Support both so this runs on
# whatever version is installed.
try:  # pragma: no cover
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = """\
Developer tooling for a Light Phone 3 St. Louis transit tool.

Data sources: Metro Transit St. Louis GTFS + GTFS-Realtime (MetroBus in Missouri
AND Illinois, MetroLink light rail). Madison County Transit is configured but its
feed URL is unresolved.

WHERE TO START, by question:
- "what is in the feed / what does it say" -> stl_gtfs_* , or stl_gtfs_query for
  anything the named tools do not cover.
- "is everything OK right now" -> stl_report_brief. One call, and it names the
  next command to run.
- "did something break" -> stl_assert_run (assumptions the app depends on), then
  stl_assert_explain on any failure.
- "what changed between two feeds" -> stl_diff_summary; stl_diff_stop_ids for the
  one number that decides whether users' saved stops survived a service change.
- "why did a rider see nothing" -> stl_support_explain_empty, then
  stl_support_repro to reconstruct the exact screen.
- "what does the app bundle" -> stl_bundle_fares / stl_bundle_holidays; these
  read Metro's website, not the GTFS feed.

Things worth knowing before you reason about results:
- Metro's GTFS expires at each quarterly service change. Always check
  stl_gtfs_coverage before trusting departure results; an empty result is often
  an expired feed, not an absent bus.
- GTFS times can exceed 24:00:00. A departure at 00:12 is usually encoded as
  24:12:00 on the PREVIOUS service date. stl_gtfs_service_day makes this visible.
  Service days are measured from noon-minus-12h, which is NOT local midnight on
  the two DST transition days each year.
- The rider-facing stop number printed on bus stop signs may live in stop_code
  or stop_id. Run stl_gtfs_stop_resolve once and rely on its verdict.
- Fares and holiday service mappings are NOT in the GTFS feed. They come from
  Metro's website, via the stl_web_* tools. MetroBus runs SUNDAY service on a
  holiday while MetroLink runs WEEKEND service -- different concepts, and the
  tools keep them apart deliberately.
- The LP3 has no protobuf runtime available, so stl_rt_reference and stl_rt_wire
  exist to support a hand-written or kotlinx-serialization-protobuf decoder.
- GTFS-RT delay is a SIGNED int32. A negative delay means the bus is early.

Start with stl_doctor or stl_snapshot_sources if you do not know what data is
already fetched.
"""

mcp = _Server("stl_transit_mcp", instructions=INSTRUCTIONS)

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
NETWORK = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
WRITES_FILES = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _call(fn, **kwargs) -> str:
    """Invoke a core function and serialize. Errors come back structured, with
    a remedy, rather than as a traceback the model cannot act on."""
    try:
        result = fn(**kwargs)
    except StlError as exc:
        result = exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
        result = {
            "ok": False,
            "error": {
                "code": "UNEXPECTED",
                "message": f"{type(exc).__name__}: {exc}",
                "remedy": "Re-run the same command in the CLI with --json for the full "
                          "traceback, or check `stl doctor`.",
            },
        }
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------- orientation --

@mcp.tool(name="stl_doctor", annotations=READ_ONLY)
async def stl_doctor() -> str:
    """Health of the local environment: store location, snapshot count, disk use,
    which configured sources are usable and which are blocked on an unresolved URL.
    Call this first if you do not know what data is available locally."""
    return _call(service.doctor)


@mcp.tool(name="stl_snapshot_sources", annotations=READ_ONLY)
async def stl_snapshot_sources() -> str:
    """List every configured feed and page: agency, region, URL, whether it is
    usable, how many snapshots exist locally, and when the latest was fetched."""
    return _call(service.snapshot_sources)


@mcp.tool(name="stl_snapshot_list", annotations=READ_ONLY)
async def stl_snapshot_list(kind: str | None = None, source: str | None = None,
                            limit: int = 20, offset: int = 0) -> str:
    """List stored snapshots newest-first, with pins.

    Args:
        kind: 'gtfs' or 'rt'. Omit for all.
        source: source name, e.g. 'metro_gtfs'. Omit for all.
    """
    return _call(service.snapshot_list, kind=kind, source=source, limit=limit, offset=offset)


@mcp.tool(name="stl_snapshot_fetch", annotations=NETWORK)
async def stl_snapshot_fetch(source: str = "metro_gtfs", force: bool = False) -> str:
    """Download a feed from the network into the local snapshot store.

    Uses conditional requests, so an unchanged feed returns unchanged=true and
    costs a 304 rather than re-downloading. The GTFS zip is ~3.5 MB and expands
    to ~29 MB, so this can take a few seconds.

    Args:
        source: 'metro_gtfs', 'metro_rt_trips', 'metro_rt_vehicles', 'metro_rt_alerts'.
        force: bypass the conditional-request cache.
    """
    return _call(service.snapshot_fetch, source=source, force=force)


# -------------------------------------------------------------------- gtfs --

@mcp.tool(name="stl_gtfs_coverage", annotations=READ_ONLY)
async def stl_gtfs_coverage(snapshot: str | None = None) -> str:
    """Service date range and days remaining before the feed expires.

    Metro publishes a feed whose service data ends at the next quarterly pick.
    Check this before trusting any departure result: an empty departures list is
    frequently an expired feed rather than an absent bus."""
    return _call(service.gtfs_coverage, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_files", annotations=READ_ONLY)
async def stl_gtfs_files(snapshot: str | None = None) -> str:
    """Inventory of files in the GTFS zip with row counts and columns, plus a list
    of optional GTFS files that are ABSENT and why each absence matters
    (transfers, fares, frequencies, pathways...)."""
    return _call(service.gtfs_files, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_stats", annotations=READ_ONLY)
async def stl_gtfs_stats(snapshot: str | None = None) -> str:
    """Headline counts: agencies, routes broken down by route type, stops, trips,
    stop_times rows, shape points, service_ids."""
    return _call(service.gtfs_stats, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_features", annotations=READ_ONLY)
async def stl_gtfs_features(snapshot: str | None = None) -> str:
    """Which GTFS features this feed provides, phrased to line up with the badges on
    the Mobility Database feed page (route colors, shapes, headsigns, wheelchair
    accessibility, fares, pathways, transfers, frequencies)."""
    return _call(service.gtfs_features, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_schema", annotations=READ_ONLY)
async def stl_gtfs_schema(table: str, snapshot: str | None = None) -> str:
    """Columns, null rates, distinct counts and sample values for one GTFS file.

    Args:
        table: GTFS file name with or without .txt, e.g. 'stops' or 'stop_times.txt'.
    """
    return _call(service.gtfs_schema, table=table, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_query", annotations=READ_ONLY)
async def stl_gtfs_query(sql: str, snapshot: str | None = None, limit: int = 100) -> str:
    """Run one read-only SQL query against the imported GTFS feed.

    Tables are the GTFS filenames without .txt: agency, stops, routes, trips,
    stop_times, calendar, calendar_dates, shapes, feed_info. All columns are TEXT,
    including numeric-looking ids -- leading zeros are meaningful in GTFS.

    Writes, ATTACH and PRAGMA are denied at the database driver; a wall-clock
    timeout and row/byte caps are enforced. Only a single statement is accepted.

    This is the general-purpose escape hatch: anything the named tools do not
    cover can be expressed here.

    Args:
        sql: a single SELECT or WITH statement.
        limit: max rows (hard cap 1000).
    """
    return _call(service.gtfs_query, sql=sql, snapshot=snapshot, limit=limit)


@mcp.tool(name="stl_gtfs_routes", annotations=READ_ONLY)
async def stl_gtfs_routes(route_type: str | None = None, search: str | None = None,
                          limit: int = 50, offset: int = 0, snapshot: str | None = None) -> str:
    """List routes with ids, short and long names, type and trip counts.

    Args:
        route_type: GTFS route_type as a string. '3' is bus, '0' tram/streetcar,
            '1' subway, '2' rail.
        search: case-insensitive substring match across all route fields.
    """
    return _call(service.gtfs_routes, route_type=route_type, search=search,
                 limit=limit, offset=offset, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_route", annotations=READ_ONLY)
async def stl_gtfs_route(route_id: str, snapshot: str | None = None) -> str:
    """One route in detail: directions, headsigns, per-service trip counts, and the
    first and last departure time in each direction.

    Args:
        route_id: the GTFS route_id, NOT the number on the front of the bus.
            Metro's route_ids look like '19731B'; run stl_gtfs_routes with a
            search term to map a rider-facing number onto one.
    """
    return _call(service.gtfs_route, route_id=route_id, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_stops", annotations=READ_ONLY)
async def stl_gtfs_stops(search: str | None = None, code: str | None = None,
                         route_id: str | None = None, limit: int = 25, offset: int = 0,
                         snapshot: str | None = None) -> str:
    """Search stops by name substring, rider-facing stop code, or serving route.

    Args:
        search: substring of stop_name, case-insensitive.
        code: exact stop_code match.
        route_id: return every stop served by this route.
    """
    return _call(service.gtfs_stops, search=search, code=code, route_id=route_id,
                 limit=limit, offset=offset, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_stop", annotations=READ_ONLY)
async def stl_gtfs_stop(stop: str, snapshot: str | None = None) -> str:
    """One stop resolved by rider-facing code or internal id, with both identifiers,
    parent station, accessibility flags, coordinates, and the routes serving it.

    Args:
        stop: a stop_code (the number on the sign) or a stop_id.
    """
    return _call(service.gtfs_stop, stop=stop, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_stop_resolve", annotations=READ_ONLY)
async def stl_gtfs_stop_resolve(snapshot: str | None = None) -> str:
    """Determine which GTFS field holds the number printed on a bus stop sign.

    Reports coverage, uniqueness and observed format for both stop_code and
    stop_id, checks Metro's own published example (15111), and returns a verdict.

    This matters more than it looks: the Light SDK exposes no usable location API,
    so 'stops near me' is not buildable and the app's entire input UX is
    stop-number entry. Run this once and rely on the verdict."""
    return _call(service.gtfs_stop_resolve, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_departures", annotations=READ_ONLY)
async def stl_gtfs_departures(stop: str, at: str | None = None, window_minutes: int = 90,
                              route: str | None = None, limit: int = 20,
                              snapshot: str | None = None) -> str:
    """Scheduled departures at a stop for a time window. Schedule only, no realtime.

    Correctly attributes departures encoded past 24:00:00 to the previous service
    date, and resolves service_ids through both calendar.txt and calendar_dates.txt.
    Each result carries its service_date and raw gtfs_time alongside the resolved
    local time, so a wrong service-date attribution is visible rather than hidden.

    Args:
        stop: stop_code (number on the sign) or stop_id.
        at: ISO-8601 instant. Naive values are read as America/Chicago local time.
            Defaults to now.
        window_minutes: how far ahead to look.
        route: optional route_id or route_short_name filter.
    """
    return _call(service.gtfs_departures, stop=stop, at=at, window_minutes=window_minutes,
                 route=route, limit=limit, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_calendar", annotations=READ_ONLY)
async def stl_gtfs_calendar(on: str | None = None, snapshot: str | None = None) -> str:
    """service_ids active on a date, showing the calendar.txt weekly pattern and each
    calendar_dates.txt exception SEPARATELY rather than pre-merged, so you can see
    whether a date's behaviour came from the weekly pattern or from an exception.

    Args:
        on: ISO date (YYYY-MM-DD). Defaults to today.
    """
    return _call(service.gtfs_calendar, on=on, snapshot=snapshot)


@mcp.tool(name="stl_gtfs_service_day", annotations=READ_ONLY)
async def stl_gtfs_service_day(timestamp: str | None = None) -> str:
    """Which GTFS service date(s) a wall-clock instant could belong to, with the
    corresponding gtfs_time for each.

    Use this whenever a departure time looks off by a day. GTFS measures times from
    noon-minus-twelve-hours, not local midnight -- on DST transition days those
    differ by an hour.

    Args:
        timestamp: ISO-8601 instant. Naive values read as America/Chicago.
    """
    return _call(service.gtfs_service_day, timestamp=timestamp)


@mcp.tool(name="stl_gtfs_late_night", annotations=READ_ONLY)
async def stl_gtfs_late_night(threshold: str = "24:00:00", limit: int = 25,
                              snapshot: str | None = None) -> str:
    """Trips whose stop times cross the service-day boundary, plus the maximum
    departure_time anywhere in the feed. Use it to find edge-case test material.

    Args:
        threshold: GTFS time string; departures at or after it are returned.
    """
    return _call(service.gtfs_late_night, threshold=threshold, limit=limit, snapshot=snapshot)


# ---------------------------------------------------------------------- rt --

@mcp.tool(name="stl_rt_health", annotations=READ_ONLY)
async def stl_rt_health(entity: str | None = None) -> str:
    """Staleness and entity counts for the locally stored realtime feeds, and
    whether the three feeds agree on their header timestamp.

    Args:
        entity: 'trip_updates', 'vehicle_positions', or 'alerts'. Omit for all.
    """
    return _call(service.rt_health, entity=entity)


@mcp.tool(name="stl_rt_decode", annotations=READ_ONLY)
async def stl_rt_decode(entity: str = "trip_updates", limit: int = 5,
                        snapshot: str | None = None) -> str:
    """Decode a stored GTFS-Realtime snapshot into normalized JSON.

    Fields present in the bytes but absent from the schema map are preserved under
    '_unknown' rather than dropped, because silently discarding fields is how you
    ship a decoder that is wrong in ways nobody notices.

    Args:
        entity: 'trip_updates', 'vehicle_positions', or 'alerts'.
    """
    return _call(service.rt_decode, entity=entity, limit=limit, snapshot=snapshot)


@mcp.tool(name="stl_rt_wire", annotations=READ_ONLY)
async def stl_rt_wire(entity: str = "trip_updates", depth: int = 5, max_entities: int = 2,
                      snapshot: str | None = None) -> str:
    """Raw protobuf wire-format dump of a realtime snapshot: field number, wire type,
    length, bytes and nesting, with the named path for each numeric path.

    This is the ground-truth artifact for validating a hand-written decoder. Point
    the Kotlin implementation at the same snapshot and compare trees.

    Args:
        depth: how deep to recurse into submessages.
        max_entities: how many feed entities to dump (keeps output bounded).
    """
    return _call(service.rt_wire, entity=entity, depth=depth,
                 max_entities=max_entities, snapshot=snapshot)


@mcp.tool(name="stl_rt_schema_census", annotations=READ_ONLY)
async def stl_rt_schema_census(entity: str = "trip_updates", samples: int = 3) -> str:
    """Which protobuf fields this feed actually populates, and at what rate, across
    N stored snapshots.

    Decides what to model in Kotlin: low-rate fields can be skipped in v1, and any
    path reported as unmodelled is present in the bytes but missing from the schema
    map, which needs investigating before porting.

    Args:
        samples: how many recent snapshots to census.
    """
    return _call(service.rt_schema_census, entity=entity, samples=samples)


@mcp.tool(name="stl_rt_reference", annotations=READ_ONLY)
async def stl_rt_reference() -> str:
    """The full GTFS-Realtime field map as a flat table (message, field number, name,
    kind, repeated) plus all enum value mappings.

    This is the porting reference for the on-device decoder. The Light SDK
    dependency allow-list contains no protobuf runtime, so the Kotlin decoder is
    either kotlinx-serialization-protobuf or hand-written from this table."""
    return _call(service.rt_reference)


@mcp.tool(name="stl_rt_stop_arrivals", annotations=READ_ONLY)
async def stl_rt_stop_arrivals(stop: str, at: str | None = None, window_minutes: int = 90,
                               limit: int = 20, snapshot: str | None = None) -> str:
    """Scheduled departures with realtime predictions merged in -- exactly what the
    app should render.

    When no realtime snapshot is available it degrades to scheduled-only and says
    so explicitly, which is the behaviour the app must also have.

    Args:
        stop: stop_code or stop_id.
        at: ISO-8601 instant, America/Chicago if naive. Defaults to now.
    """
    return _call(service.rt_stop_arrivals, stop=stop, at=at,
                 window_minutes=window_minutes, limit=limit, snapshot=snapshot)


# ---------------------------------------------------------- oracle/support --

@mcp.tool(name="stl_oracle_cases", annotations=READ_ONLY)
async def stl_oracle_cases() -> str:
    """The golden-fixture case list for the Kotlin test gate, each with the specific
    failure mode it pins down (DST transitions, 24:xx rollover, holiday service
    mapping, expired feed, realtime absent, and so on)."""
    return _call(oracle.list_cases)


@mcp.tool(name="stl_oracle_generate", annotations=WRITES_FILES)
async def stl_oracle_generate(spec_path: str, out_dir: str = "fixtures",
                              case: str | None = None, snapshot: str | None = None) -> str:
    """Compute expected departure outputs and write committed fixture JSON files.

    Output is byte-stable for a given snapshot (sorted keys, fixed indent) so that
    a later verify run is a meaningful drift check.

    Args:
        spec_path: JSON file binding each case id to concrete inputs, e.g.
            {"weekday_midday": {"stop": "15111", "at": "2026-08-05T12:00:00"}}.
        out_dir: directory to write fixtures into.
        case: generate only this case id.
    """
    return _call(oracle.generate, spec_path=spec_path, out_dir=out_dir,
                 case=case, snapshot=snapshot)


@mcp.tool(name="stl_oracle_verify", annotations=READ_ONLY)
async def stl_oracle_verify(fixtures_dir: str = "fixtures", snapshot: str | None = None) -> str:
    """Recompute every committed fixture against the current feed and report which
    ones no longer match. Drift means either the feed changed or the fixtures are
    stale -- both are things you want to learn from a scheduled run, not a user.

    A case that legitimately raises (unknown stop code, expired feed) is a
    first-class expectation, compared on error type rather than message, so it
    does not read as permanent drift.

    Args:
        fixtures_dir: directory of committed fixture JSON, normally the tool
            repo's test resources rather than anywhere in this store.
    """
    return _call(oracle.verify, fixtures_dir=fixtures_dir, snapshot=snapshot)


@mcp.tool(name="stl_support_explain_empty", annotations=READ_ONLY)
async def stl_support_explain_empty(stop: str, at: str | None = None,
                                    window_minutes: int = 90,
                                    snapshot: str | None = None) -> str:
    """Diagnose why a stop shows no departures, by walking the decision tree and
    naming the branch: unknown stop code, expired feed, no service that date,
    stop present but never served, or simply too narrow a window.

    Use this whenever stl_gtfs_departures returns an empty list, instead of
    guessing at the cause.

    Args:
        stop: stop_code or stop_id.
        at: ISO-8601 instant, America/Chicago if naive.
    """
    return _call(service.support_explain_empty, stop=stop, at=at,
                 window_minutes=window_minutes, snapshot=snapshot)


# ------------------------------------------------------------------ assert --

@mcp.tool(name="stl_assert_list", annotations=READ_ONLY)
async def stl_assert_list() -> str:
    """The assumptions this app makes about the feed that Metro never promised.

    Each one names what it checks and, more usefully, what breaks in the app if
    it stops holding. Read this before adding a feature that depends on feed
    behaviour, so the dependency gets encoded rather than discovered later by a
    user."""
    return _call(service.assert_list)


@mcp.tool(name="stl_assert_run", annotations=READ_ONLY)
async def stl_assert_run(only: list[str] | None = None, baseline: str | None = None,
                         snapshot: str | None = None) -> str:
    """Evaluate the assumption suite against the current feed.

    Every result carries the OBSERVED value beside the threshold, so a failure
    is actionable without a second call: "stop_code coverage 0.982, threshold
    0.99" tells you how bad it is, "FAIL" does not.

    Three outcomes, not two. `skip` means the measurement could not be taken --
    a stability check with no baseline to compare against has not been
    performed, and reporting that as a pass would be a lie.

    Args:
        only: assumption ids to run. Omit for all.
        baseline: snapshot id or pin name for the stability assumptions
            (stop_ids_stable, rail_route_ids_stable). Without it those skip.
    """
    return _call(service.assert_run, only=only, baseline=baseline, snapshot=snapshot)


@mcp.tool(name="stl_assert_explain", annotations=READ_ONLY)
async def stl_assert_explain(assumption_id: str) -> str:
    """One assumption in full: why it matters, which code path depends on it,
    and how to remediate a failure. Call this on anything stl_assert_run
    reports as failing, before deciding what to do about it.

    Args:
        assumption_id: an id from stl_assert_list, e.g. 'stop_code_unique' or
            'rt_join_rate'.
    """
    return _call(service.assert_explain, assumption_id=assumption_id)


# -------------------------------------------------------------------- diff --

@mcp.tool(name="stl_diff_summary", annotations=READ_ONLY)
async def stl_diff_summary(a: str, b: str) -> str:
    """Everything that changed between two GTFS snapshots, in one screen.

    Findings are graded, because a pick that renames three headsigns is routine
    and one that retires four hundred stop codes is not, and an ungraded list
    of deltas makes the reader do that triage themselves.

    Args:
        a: the earlier snapshot id or pin name.
        b: the later one. Direction matters and is never normalized.
    """
    return _call(service.diff_summary, a=a, b=b)


@mcp.tool(name="stl_diff_stop_ids", annotations=READ_ONLY)
async def stl_diff_stop_ids(a: str, b: str) -> str:
    """Survival rate of stop_id and stop_code across a service change.

    The single most consequential number in this whole tool. The app's saved-
    stops feature lives or dies on it: every code that does not survive a pick
    is a user whose saved stop silently stops working, with no error and no
    way for them to tell what happened.

    Args:
        a: the earlier snapshot id or pin name.
        b: the later one. Run stl_snapshot_list to see what is stored.
    """
    return _call(service.diff_stop_ids, a=a, b=b)


# --------------------------------------------------------------------- web --

@mcp.tool(name="stl_web_list", annotations=READ_ONLY)
async def stl_web_list() -> str:
    """Metro web pages configured for capture, with their last capture and
    content hash. Fares, holiday schedules, the developer terms, and the
    upcoming-schedule-changes page -- everything the app needs that is not in
    the GTFS feed."""
    return _call(service.web_list)


@mcp.tool(name="stl_web_capture", annotations=NETWORK)
async def stl_web_capture(page: str | None = None, force: bool = False) -> str:
    """Fetch, normalize, extract and store a Metro web page.

    Hashes the EXTRACTED content, never the raw HTML: raw HTML changes on every
    request (analytics ids, nonces, rotating images), so hashing it would make
    every later drift check a false positive.

    Rate-limited to one fetch per page per day by default. Metro is a public
    agency whose infrastructure this tool is an unpaid guest on.

    Args:
        page: 'fares', 'holidays', 'purchase', 'schedule_changes',
            'developer_terms', 'rider_alerts'. Omit to capture all.
        force: bypass the interval gate and the conditional-request cache.
    """
    return _call(service.web_capture, page=page, force=force)


@mcp.tool(name="stl_web_extract", annotations=READ_ONLY)
async def stl_web_extract(page: str, snapshot: str | None = None) -> str:
    """Structured data pulled out of a stored page capture.

    fares -> fare rows with prices in integer cents; holidays -> holiday rows
    with BUS and RAIL service kept separate; schedule_changes -> the pick id;
    others -> normalized text.

    Args:
        page: the page key. Run stl_web_list for valid values.
    """
    return _call(service.web_extract, page=page, snapshot=snapshot)


@mcp.tool(name="stl_web_check", annotations=READ_ONLY)
async def stl_web_check() -> str:
    """Has any watched page changed since its last capture?

    This is the surveillance job. A changed fares page means the app's bundled
    fare table is now lying to riders; a changed developer-terms page means the
    redistribution rights this whole project rests on may have moved."""
    return _call(service.web_check)


# ------------------------------------------------------------------ bundle --

@mcp.tool(name="stl_bundle_fares", annotations=READ_ONLY)
async def stl_bundle_fares(fmt: str = "json") -> str:
    """The fare table the app ships, with its as_of date and source URL baked in.

    Fares are NOT in the GTFS feed -- this reads the latest capture of Metro's
    fares page, so run stl_web_capture first if it reports nothing. Prices are
    integer cents; a fare table carrying 2.4999999 is a bug that reaches riders.

    Args:
        fmt: 'json', or 'kotlin' to emit compilable Kotlin source. Hand-copying
            a fare table into Kotlin is how a stale fare reaches a rider.
    """
    return _call(service.bundle_fares, fmt=fmt)


@mcp.tool(name="stl_bundle_holidays", annotations=READ_ONLY)
async def stl_bundle_holidays(year: int | None = None) -> str:
    """Holiday to service-type mapping, bus and rail kept distinct.

    On a holiday MetroBus runs SUNDAY service while MetroLink runs WEEKEND
    service. Those are different concepts that happen to coincide most of the
    time, and merging them produces a wrong answer on exactly the days a rider
    is most likely to check.

    Args:
        year: the calendar year to resolve holiday dates against. Defaults to
            the current year, which is usually but not always what you want
            near a year boundary.
    """
    return _call(service.bundle_holidays, year=year)


@mcp.tool(name="stl_bundle_size_report", annotations=READ_ONLY)
async def stl_bundle_size_report(compact_path: str | None = None,
                                 snapshot: str | None = None) -> str:
    """On-device size budget: bytes per table, with index cost isolated.

    The raw feed is ~29 MB expanded and the LP3 is a minimalist device, so this
    is what decides which pruning strategy the shipped app uses.

    Args:
        compact_path: a database built by `stl bundle compact`, to compare
            against the full feed.
    """
    return _call(service.bundle_size_report, compact_path=compact_path, snapshot=snapshot)


# ------------------------------------------------------------------ report --

@mcp.tool(name="stl_report_brief", annotations=READ_ONLY)
async def stl_report_brief(snapshot: str | None = None) -> str:
    """The state of the feed right now, in one call, with the next command to run.

    Composes coverage, the assumption suite, realtime health and web drift.
    Every input is optional and absences are reported rather than silently
    passed, so this still works on a machine that has only ever fetched the
    static feed. Start here if you do not know what is wrong."""
    return _call(service.report_brief, snapshot=snapshot)


@mcp.tool(name="stl_report_handoff", annotations=READ_ONLY)
async def stl_report_handoff(snapshot: str | None = None) -> str:
    """Verified facts about the feed as a markdown block, with citations.

    Written for pasting into a CLAUDE.md handoff document. Every claim carries
    the snapshot id and date it was verified against, so a later reader can
    re-verify rather than trust -- the feed moves, and an uncited fact in a
    handoff doc silently becomes a lie. Includes the sharp edges that bite a
    Kotlin port: 24:xx encoding, DST arithmetic, stop_code vs stop_id, the
    absent protobuf runtime, fares living off-feed."""
    return _call(service.report_handoff, snapshot=snapshot)


# ----------------------------------------------------------------- support --

@mcp.tool(name="stl_support_repro", annotations=READ_ONLY)
async def stl_support_repro(stop: str, at: str | None = None, window_minutes: int = 90,
                            rt_snapshot: str | None = None,
                            snapshot: str | None = None) -> str:
    """Reconstruct exactly what the app should have shown at a stop and instant.

    This is how "stop 15111 showed nothing at 11:47 last Tuesday" gets answered
    without a device and without waiting for Tuesday. When the answer is empty
    it also returns WHY it is empty, rather than leaving you to guess.

    Args:
        stop: stop_code (the number on the sign) or stop_id.
        at: ISO-8601 instant; naive values read as America/Chicago.
        rt_snapshot: a stored realtime snapshot id to merge in, for
            reproducing a complaint about a wrong prediction.
    """
    return _call(service.support_repro, stop=stop, at=at, window_minutes=window_minutes,
                 rt_snapshot=rt_snapshot, snapshot=snapshot)


@mcp.tool(name="stl_support_diff_device", annotations=READ_ONLY)
async def stl_support_diff_device(expected_json: str, actual_json: str) -> str:
    """Diff what a device actually rendered against what it should have.

    Deliberately forgiving about the shape of `actual_json`: it accepts a bare
    list, several wrapper shapes, and differing key names, and reports what it
    assumed. The realistic input is something pasted out of a bug report, and a
    support tool that rejects the user's paste over a key name has failed at
    its one job.

    Args:
        expected_json: a file path or inline JSON -- typically the output of
            stl_support_repro.
        actual_json: a file path or inline JSON captured from the device.
    """
    return _call(service.support_diff_device, expected_json=expected_json,
                 actual_json=actual_json)


def main() -> None:
    """stdio entry point. Local only; there is nothing here worth exposing over HTTP."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
