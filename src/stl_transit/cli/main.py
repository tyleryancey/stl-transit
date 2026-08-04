"""Typer CLI. Formats results; contains no logic (spec 2.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..core import oracle as oracle_core
from ..core import service
from ..errors import StlError

console = Console()
err = Console(stderr=True)

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="St. Louis transit data tooling for the LP3 tool.")
snapshot_app = typer.Typer(no_args_is_help=True, help="Acquisition and provenance.")
gtfs_app = typer.Typer(no_args_is_help=True, help="Static feed inspection.")
rt_app = typer.Typer(no_args_is_help=True, help="GTFS-Realtime.")
oracle_app = typer.Typer(no_args_is_help=True, help="Golden fixtures for the Kotlin tests.")
support_app = typer.Typer(no_args_is_help=True, help="Reproduce reported problems.")
assert_app = typer.Typer(no_args_is_help=True, help="The assumption regression suite.")
diff_app = typer.Typer(no_args_is_help=True, help="Compare two snapshots.")
web_app = typer.Typer(no_args_is_help=True, help="Capture and watch Metro's web pages.")
bundle_app = typer.Typer(no_args_is_help=True, help="Artifacts the app ships.")
report_app = typer.Typer(no_args_is_help=True, help="Digests and handoff documents.")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(gtfs_app, name="gtfs")
app.add_typer(rt_app, name="rt")
app.add_typer(oracle_app, name="oracle")
app.add_typer(support_app, name="support")
app.add_typer(assert_app, name="assert")
app.add_typer(diff_app, name="diff")
app.add_typer(web_app, name="web")
app.add_typer(bundle_app, name="bundle")
app.add_typer(report_app, name="report")


def emit(result: dict[str, Any], as_json: bool = False, table_key: str = "items",
         columns: list[str] | None = None) -> None:
    """Render a core result and set the process exit code from it."""
    if as_json:
        console.print_json(json.dumps(result, default=str))
    else:
        _render(result, table_key, columns)
    for w in result.get("warnings", []) or []:
        err.print(f"[yellow]warning:[/yellow] {w}")
    if not result.get("ok", True):
        raise typer.Exit(code=1)


def _render(result: dict[str, Any], table_key: str, columns: list[str] | None) -> None:
    prov = result.get("provenance")
    if isinstance(prov, dict) and prov.get("snapshot_id"):
        stale = prov.get("stale_days")
        flag = ""
        if stale is not None:
            flag = f" [red](expired {stale}d)[/red]" if stale > 0 else f" ({-stale}d left)"
        console.print(f"[dim]{prov['snapshot_id']} · {prov.get('source','')}{flag}[/dim]")
    rows = result.get(table_key)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        cols = columns or list(rows[0].keys())[:8]
        table = Table(show_header=True, header_style="bold")
        for c in cols:
            table.add_column(c)
        for r in rows:
            table.add_row(*[_cell(r.get(c)) for c in cols])
        console.print(table)
        meta = {k: result[k] for k in ("total", "count", "offset", "has_more") if k in result}
        if meta:
            console.print(f"[dim]{meta}[/dim]")
        return
    payload = {k: v for k, v in result.items()
               if k not in ("ok", "provenance", "warnings", "notes")}
    console.print_json(json.dumps(payload, default=str))


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)[:60]
    return str(value)


def guard(fn, **kwargs) -> dict[str, Any]:
    try:
        return fn(**kwargs)
    except StlError as exc:
        err.print(f"[red]{exc.code}[/red]: {exc.message}")
        if exc.remedy:
            err.print(f"[cyan]remedy[/cyan]: {exc.remedy}")
        raise typer.Exit(code=exc.exit_code)


def _emit_drift(result: dict[str, Any], as_json: bool = False, table_key: str = "items",
                columns: list[str] | None = None) -> None:
    """Render, then exit 4 if the result reports drift.

    Exit codes 3/4/6 are the whole reason `assert run` and `web check` can go
    straight into cron or a GitHub Action without anyone parsing output.
    """
    if as_json:
        console.print_json(json.dumps(result, default=str))
    else:
        if result.get("headline"):
            console.print(f"[bold]{result['headline']}[/bold]")
        _render(result, table_key, columns)
    for w in result.get("warnings", []) or []:
        err.print(f"[yellow]warning:[/yellow] {w}")
    if result.get("drift_detected"):
        raise typer.Exit(code=4)


def _emit_artifact(result: dict[str, Any], out: str | None, as_json: bool = False,
                   table_key: str = "items", columns: list[str] | None = None) -> None:
    """Render, and write the artifact's `rendered` text to `out` if given.

    `rendered` is deterministic for a given snapshot, so writing it twice
    produces byte-identical files -- which is what makes the generated
    artifacts diffable in the tool repo instead of noisy.
    """
    if out:
        target = Path(out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.get("rendered", json.dumps(result, indent=2, default=str)))
        console.print(f"[green]wrote[/green] {target}")
    emit(result, as_json, table_key, columns)


# ------------------------------------------------------------------- top ---

@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """Environment, store and configuration health."""
    emit(guard(service.doctor), as_json)


@app.command("mcp")
def mcp_cmd() -> None:
    """Run the MCP server on stdio (same as `stl-mcp`)."""
    from ..mcp.server import main as run

    run()


# -------------------------------------------------------------- snapshot ---

@snapshot_app.command("sources")
def snapshot_sources(as_json: bool = typer.Option(False, "--json")) -> None:
    """Configured feeds and pages with fetch status."""
    emit(guard(service.snapshot_sources), as_json,
         columns=["source", "kind", "region", "usable", "snapshots", "latest_fetched_at"])


@snapshot_app.command("fetch")
def snapshot_fetch(source: str = typer.Argument("metro_gtfs"),
                   force: bool = typer.Option(False, "--force"),
                   as_json: bool = typer.Option(False, "--json")) -> None:
    """Download a feed into the snapshot store."""
    emit(guard(service.snapshot_fetch, source=source, force=force), as_json)


@snapshot_app.command("list")
def snapshot_list(kind: str = typer.Option(None), source: str = typer.Option(None),
                  limit: int = typer.Option(50), offset: int = typer.Option(0),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """List stored snapshots, newest first."""
    emit(guard(service.snapshot_list, kind=kind, source=source, limit=limit, offset=offset),
         as_json)


@snapshot_app.command("show")
def snapshot_show(ref: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Full manifest for one snapshot."""
    emit(guard(service.snapshot_show, ref=ref), as_json)


@snapshot_app.command("pin")
def snapshot_pin(ref: str, name: str = typer.Option(..., "--as"),
                 as_json: bool = typer.Option(False, "--json")) -> None:
    """Pin a snapshot under a stable name."""
    emit(guard(service.snapshot_pin, ref=ref, name=name), as_json)


@snapshot_app.command("verify")
def snapshot_verify(ref: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Re-hash a snapshot against its manifest."""
    emit(guard(service.snapshot_verify, ref=ref), as_json)


@snapshot_app.command("import")
def snapshot_import(path: str, source: str = typer.Option("metro_gtfs"),
                    kind: str = typer.Option("gtfs"),
                    as_json: bool = typer.Option(False, "--json")) -> None:
    """Ingest a feed obtained outside this tool."""
    emit(guard(service.snapshot_import, path=path, source=source, kind=kind), as_json)


@snapshot_app.command("gc")
def snapshot_gc(keep: int = typer.Option(5), apply: bool = typer.Option(False, "--apply"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Prune old unpinned snapshots (dry-run unless --apply)."""
    emit(guard(service.snapshot_gc, keep=keep, dry_run=not apply), as_json)


# ------------------------------------------------------------------ gtfs ---

@gtfs_app.command("import")
def gtfs_import(snapshot: str = typer.Option(None), force: bool = typer.Option(False, "--force"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Build or rebuild the SQLite index."""
    emit(guard(service.gtfs_import, snapshot=snapshot, force=force), as_json)


@gtfs_app.command("files")
def gtfs_files(snapshot: str = typer.Option(None),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """File inventory, row counts, and which optional files are ABSENT."""
    emit(guard(service.gtfs_files, snapshot=snapshot), as_json, "present",
         ["file", "rows", "uncompressed_bytes"])


@gtfs_app.command("stats")
def gtfs_stats(snapshot: str = typer.Option(None),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Headline counts."""
    emit(guard(service.gtfs_stats, snapshot=snapshot), as_json)


@gtfs_app.command("features")
def gtfs_features(snapshot: str = typer.Option(None),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Declared GTFS features, in Mobility Database badge terms."""
    emit(guard(service.gtfs_features, snapshot=snapshot), as_json, "features")


@gtfs_app.command("schema")
def gtfs_schema(table: str, snapshot: str = typer.Option(None),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Columns, null rates and samples for one GTFS file."""
    emit(guard(service.gtfs_schema, table=table, snapshot=snapshot), as_json, "columns")


@gtfs_app.command("coverage")
def gtfs_coverage(snapshot: str = typer.Option(None),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Service date range and days-to-expiry."""
    result = guard(service.gtfs_coverage, snapshot=snapshot)
    emit(result, as_json)
    if result.get("expired"):
        raise typer.Exit(code=6)


@gtfs_app.command("query")
def gtfs_query(sql: str, snapshot: str = typer.Option(None), limit: int = typer.Option(200),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Run a read-only SQL query against the feed."""
    emit(guard(service.gtfs_query, sql=sql, snapshot=snapshot, limit=limit), as_json, "rows")


@gtfs_app.command("routes")
def gtfs_routes(route_type: str = typer.Option(None, "--type"),
                search: str = typer.Option(None), limit: int = typer.Option(100),
                snapshot: str = typer.Option(None),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """List routes."""
    emit(guard(service.gtfs_routes, route_type=route_type, search=search, limit=limit,
               snapshot=snapshot), as_json,
         columns=["route_id", "route_short_name", "route_long_name", "route_type_name",
                  "trip_count"])


@gtfs_app.command("route")
def gtfs_route(route_id: str, snapshot: str = typer.Option(None),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """One route in detail."""
    emit(guard(service.gtfs_route, route_id=route_id, snapshot=snapshot), as_json, "directions")


@gtfs_app.command("stops")
def gtfs_stops(search: str = typer.Option(None), code: str = typer.Option(None),
               route_id: str = typer.Option(None, "--route"), limit: int = typer.Option(50),
               snapshot: str = typer.Option(None),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Search stops."""
    emit(guard(service.gtfs_stops, search=search, code=code, route_id=route_id,
               limit=limit, snapshot=snapshot), as_json,
         columns=["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"])


@gtfs_app.command("stop")
def gtfs_stop(stop: str, snapshot: str = typer.Option(None),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """One stop with the routes serving it."""
    emit(guard(service.gtfs_stop, stop=stop, snapshot=snapshot), as_json, "routes")


@gtfs_app.command("stop-resolve")
def gtfs_stop_resolve(snapshot: str = typer.Option(None),
                      as_json: bool = typer.Option(False, "--json")) -> None:
    """Which field holds the number printed on a bus stop sign?"""
    emit(guard(service.gtfs_stop_resolve, snapshot=snapshot), as_json)


@gtfs_app.command("departures")
def gtfs_departures(stop: str, at: str = typer.Option(None, "--at"),
                    window: int = typer.Option(90, "--window"),
                    route: str = typer.Option(None), limit: int = typer.Option(20),
                    snapshot: str = typer.Option(None),
                    as_json: bool = typer.Option(False, "--json")) -> None:
    """Scheduled departures at a stop."""
    emit(guard(service.gtfs_departures, stop=stop, at=at, window_minutes=window,
               route=route, limit=limit, snapshot=snapshot), as_json,
         columns=["departure_local", "route_short_name", "headsign", "gtfs_time",
                  "service_date", "minutes_away"])


@gtfs_app.command("calendar")
def gtfs_calendar(on: str = typer.Option(None, "--date"), snapshot: str = typer.Option(None),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Active service_ids on a date, with exceptions shown separately."""
    emit(guard(service.gtfs_calendar, on=on, snapshot=snapshot), as_json)


@gtfs_app.command("service-day")
def gtfs_service_day(timestamp: str = typer.Argument(None),
                     as_json: bool = typer.Option(False, "--json")) -> None:
    """Which service date(s) an instant belongs to."""
    emit(guard(service.gtfs_service_day, timestamp=timestamp), as_json, "candidates")


@gtfs_app.command("late-night")
def gtfs_late_night(threshold: str = typer.Option("24:00:00"), limit: int = typer.Option(25),
                    snapshot: str = typer.Option(None),
                    as_json: bool = typer.Option(False, "--json")) -> None:
    """Trips crossing the service-day boundary."""
    emit(guard(service.gtfs_late_night, threshold=threshold, limit=limit, snapshot=snapshot),
         as_json)


# -------------------------------------------------------------------- rt ---

@rt_app.command("fetch")
def rt_fetch(entity: str = typer.Option("trip_updates", "--entity"),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Fetch a realtime feed."""
    emit(guard(service.rt_fetch, entity=entity), as_json)


@rt_app.command("decode")
def rt_decode(entity: str = typer.Option("trip_updates", "--entity"),
              limit: int = typer.Option(5), snapshot: str = typer.Option(None),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """Decode a realtime snapshot to JSON."""
    emit(guard(service.rt_decode, entity=entity, limit=limit, snapshot=snapshot), as_json)


@rt_app.command("wire")
def rt_wire(entity: str = typer.Option("trip_updates", "--entity"),
            depth: int = typer.Option(5), max_entities: int = typer.Option(2),
            snapshot: str = typer.Option(None),
            as_json: bool = typer.Option(True, "--json/--no-json")) -> None:
    """Raw protobuf wire dump -- the Kotlin decoder's ground truth."""
    emit(guard(service.rt_wire, entity=entity, depth=depth, max_entities=max_entities,
               snapshot=snapshot), as_json)


@rt_app.command("schema")
def rt_schema(entity: str = typer.Option("trip_updates", "--entity"),
              samples: int = typer.Option(3),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """Field-usage census: what Metro actually populates."""
    emit(guard(service.rt_schema_census, entity=entity, samples=samples), as_json, "fields",
         ["path", "samples_present", "presence_rate", "wire_types", "modelled"])


@rt_app.command("reference")
def rt_reference(as_json: bool = typer.Option(False, "--json")) -> None:
    """The GTFS-RT field map, for porting to Kotlin."""
    emit(guard(service.rt_reference), as_json, "items",
         ["message", "field", "name", "kind", "repeated"])


@rt_app.command("health")
def rt_health(entity: str = typer.Option(None, "--entity"),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """Staleness and entity counts across the realtime feeds."""
    emit(guard(service.rt_health, entity=entity), as_json, "items",
         ["entity", "available", "age_seconds", "stale", "entity_count"])


@rt_app.command("stop-arrivals")
def rt_stop_arrivals(stop: str, at: str = typer.Option(None, "--at"),
                     window: int = typer.Option(90, "--window"), limit: int = typer.Option(20),
                     snapshot: str = typer.Option(None),
                     as_json: bool = typer.Option(False, "--json")) -> None:
    """Scheduled ⊕ realtime -- what the app should render."""
    emit(guard(service.rt_stop_arrivals, stop=stop, at=at, window_minutes=window,
               limit=limit, snapshot=snapshot), as_json,
         columns=["departure_local", "route_short_name", "headsign", "status",
                  "delay_seconds", "predicted_local"])


# ---------------------------------------------------------------- oracle ---

@oracle_app.command("cases")
def oracle_cases(as_json: bool = typer.Option(False, "--json")) -> None:
    """The golden-fixture case list."""
    emit(guard(oracle_core.list_cases), as_json, "items", ["id", "why", "window_minutes"])


@oracle_app.command("generate")
def oracle_generate(spec: str = typer.Option(..., "--spec"),
                    out: str = typer.Option("fixtures", "--out"),
                    case: str = typer.Option(None), snapshot: str = typer.Option(None),
                    as_json: bool = typer.Option(False, "--json")) -> None:
    """Write committed fixture JSON."""
    emit(guard(oracle_core.generate, spec_path=spec, out_dir=out, case=case, snapshot=snapshot),
         as_json, "written")


@oracle_app.command("verify")
def oracle_verify(fixtures: str = typer.Option("fixtures", "--fixtures"),
                  snapshot: str = typer.Option(None),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Recompute fixtures and report drift. Exit 4 on drift."""
    result = guard(oracle_core.verify, fixtures_dir=fixtures, snapshot=snapshot)
    _render(result, "items", ["case_id", "matches", "expected_count", "actual_count"])
    for w in result.get("warnings", []):
        err.print(f"[yellow]warning:[/yellow] {w}")
    if result.get("drifted"):
        raise typer.Exit(code=4)


# --------------------------------------------------------------- support ---

@support_app.command("explain-empty")
def support_explain_empty(stop: str, at: str = typer.Option(None, "--at"),
                          window: int = typer.Option(90, "--window"),
                          snapshot: str = typer.Option(None),
                          as_json: bool = typer.Option(False, "--json")) -> None:
    """Diagnose why a stop shows nothing."""
    emit(guard(service.support_explain_empty, stop=stop, at=at, window_minutes=window,
               snapshot=snapshot), as_json, "checks")


@support_app.command("repro")
def support_repro(stop: str, at: str = typer.Option(None, "--at"),
                  window: int = typer.Option(90, "--window"),
                  rt_session: str = typer.Option(None, "--rt-session"),
                  snapshot: str = typer.Option(None),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """What the app should have shown, at a stop, at an instant."""
    emit(guard(service.support_repro, stop=stop, at=at, window_minutes=window,
               rt_snapshot=rt_session, snapshot=snapshot), as_json, "render",
         ["departure_local", "route", "headsign", "status", "minutes_away"])


@support_app.command("diff-device")
def support_diff_device(expected: str = typer.Option(..., "--expected-json"),
                        actual: str = typer.Option(..., "--actual-json"),
                        as_json: bool = typer.Option(False, "--json")) -> None:
    """Diff an on-device capture against the oracle."""
    emit(guard(service.support_diff_device, expected_json=expected, actual_json=actual),
         as_json, "differing")


@support_app.command("bundle")
def support_bundle(out: str = typer.Option(None, "--out"),
                   as_json: bool = typer.Option(False, "--json")) -> None:
    """Collect everything worth attaching to a GitHub issue."""
    result = guard(service.support_bundle)
    if out:
        import zipfile

        target = Path(out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in result.get("files", []):
                zf.writestr(f["name"], f["content"])
        console.print(f"[green]wrote[/green] {target}")
    emit(result, as_json, "files", ["name", "bytes", "sha256"])


# ---------------------------------------------------------------- assert ---

@assert_app.command("list")
def assert_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """Every assumption the app depends on, and what breaks if it fails."""
    emit(guard(service.assert_list), as_json, "items",
         ["id", "title", "severity", "category", "breaks"])


@assert_app.command("explain")
def assert_explain(assumption_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """One assumption in full: why it matters and how to fix it."""
    emit(guard(service.assert_explain, assumption_id=assumption_id), as_json)


@assert_app.command("run")
def assert_run(only: list[str] = typer.Option(None, "--only"),
               baseline: str = typer.Option(None, "--baseline"),
               snapshot: str = typer.Option(None),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Evaluate the assumption suite. Exit 3 on violation -- cron this."""
    result = guard(service.assert_run, only=list(only or []) or None, baseline=baseline,
                   snapshot=snapshot)
    if as_json:
        console.print_json(json.dumps(result, default=str))
    else:
        _render(result, "items", ["id", "status", "observed", "expected", "detail"])
    for w in result.get("warnings", []):
        err.print(f"[yellow]warning:[/yellow] {w}")
    if result.get("failed"):
        raise typer.Exit(code=3)


# ------------------------------------------------------------------ diff ---

@diff_app.command("summary")
def diff_summary(a: str, b: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """One-screen digest of everything that changed between two snapshots."""
    _emit_drift(guard(service.diff_summary, a=a, b=b), as_json, "findings",
                ["severity", "dimension", "detail"])


@diff_app.command("files")
def diff_files(a: str, b: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Files added or removed, and row-count deltas."""
    _emit_drift(guard(service.diff_files, a=a, b=b), as_json, "changed")


@diff_app.command("routes")
def diff_routes(a: str, b: str, limit: int = typer.Option(50), offset: int = typer.Option(0),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """route_ids added, removed or renamed."""
    _emit_drift(guard(service.diff_routes, a=a, b=b, limit=limit, offset=offset), as_json,
                "changed")


@diff_app.command("stops")
def diff_stops(a: str, b: str, moved_threshold_m: float = typer.Option(25.0, "--moved-threshold-m"),
               limit: int = typer.Option(50), offset: int = typer.Option(0),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Stops added, removed, renamed or physically moved."""
    _emit_drift(guard(service.diff_stops, a=a, b=b, moved_threshold_m=moved_threshold_m,
                      limit=limit, offset=offset), as_json, "moved")


@diff_app.command("stop-ids")
def diff_stop_ids(a: str, b: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Survival of stop_id and stop_code across a pick. Saved stops depend on it."""
    _emit_drift(guard(service.diff_stop_ids, a=a, b=b), as_json)


@diff_app.command("calendar")
def diff_calendar(a: str, b: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """service_id churn and date-range shifts."""
    _emit_drift(guard(service.diff_calendar, a=a, b=b), as_json)


@diff_app.command("schedule")
def diff_schedule(a: str, b: str, stop: str = typer.Option(None), route: str = typer.Option(None),
                  on: str = typer.Option(None, "--date"), limit: int = typer.Option(50),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Timetable deltas for one stop or route."""
    _emit_drift(guard(service.diff_schedule, a=a, b=b, stop=stop, route=route, on=on,
                      limit=limit), as_json, "changed")


# ------------------------------------------------------------------- web ---

@web_app.command("capture")
def web_capture(page: str = typer.Argument(None),
                all_pages: bool = typer.Option(False, "--all"),
                force: bool = typer.Option(False, "--force"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Fetch, normalize, extract and store a page. One fetch per day by default."""
    if not page and not all_pages:
        err.print("[red]USAGE[/red]: name a page or pass --all.")
        err.print("[cyan]remedy[/cyan]: run `stl web list` to see configured pages.")
        raise typer.Exit(code=2)
    emit(guard(service.web_capture, page=page, force=force), as_json, "captured",
         ["page", "content_hash", "ok", "bytes"])


@web_app.command("list")
def web_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """Configured pages, last capture, content hash."""
    emit(guard(service.web_list), as_json, "items",
         ["page", "extractor", "captures", "last_captured", "content_hash"])


@web_app.command("extract")
def web_extract(page: str, snapshot: str = typer.Option(None),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Structured extraction from the latest capture."""
    emit(guard(service.web_extract, page=page, snapshot=snapshot), as_json)


@web_app.command("diff")
def web_diff(page: str, a: str = typer.Argument(None), b: str = typer.Argument(None),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Normalized-text diff between two captures."""
    _emit_drift(guard(service.web_diff, page=page, a=a, b=b), as_json)


@web_app.command("check")
def web_check(as_json: bool = typer.Option(False, "--json")) -> None:
    """Drift check across every page. Exit 4 on change -- cron this."""
    _emit_drift(guard(service.web_check), as_json, "items",
                ["page", "changed", "content_hash", "breaks"])


# ---------------------------------------------------------------- bundle ---

@bundle_app.command("fares")
def bundle_fares(fmt: str = typer.Option("json", "--format"), out: str = typer.Option(None),
                 as_json: bool = typer.Option(False, "--json")) -> None:
    """Fare table with as_of and source URL baked in."""
    _emit_artifact(guard(service.bundle_fares, fmt=fmt), out, as_json, "rows")


@bundle_app.command("holidays")
def bundle_holidays(year: int = typer.Option(None), out: str = typer.Option(None),
                    as_json: bool = typer.Option(False, "--json")) -> None:
    """Holiday to service-type map. Bus and rail kept distinct."""
    _emit_artifact(guard(service.bundle_holidays, year=year), out, as_json, "rows")


@bundle_app.command("stops-index")
def bundle_stops_index(out: str = typer.Option(None), snapshot: str = typer.Option(None),
                       as_json: bool = typer.Option(False, "--json")) -> None:
    """stop_code lookup for fast on-device resolution."""
    _emit_artifact(guard(service.bundle_stops_index, snapshot=snapshot), out, as_json)


@bundle_app.command("compact")
def bundle_compact(out: str = typer.Option(..., "--out"),
                   strategy: str = typer.Option("balanced", "--strategy"),
                   routes: list[str] = typer.Option(None, "--routes"),
                   days: int = typer.Option(None, "--days"),
                   snapshot: str = typer.Option(None),
                   as_json: bool = typer.Option(False, "--json")) -> None:
    """Build the pruned on-device database and report every pruning decision."""
    emit(guard(service.bundle_compact, out=out, strategy=strategy,
               routes=list(routes or []) or None, days=days, snapshot=snapshot), as_json,
         "decisions", ["decision", "rationale", "rows_removed", "bytes_saved"])


@bundle_app.command("size-report")
def bundle_size_report(compact_path: str = typer.Option(None, "--compact"),
                       snapshot: str = typer.Option(None),
                       as_json: bool = typer.Option(False, "--json")) -> None:
    """Size budget by table, index cost isolated."""
    emit(guard(service.bundle_size_report, compact_path=compact_path, snapshot=snapshot),
         as_json, "tables")


@bundle_app.command("manifest")
def bundle_manifest(snapshot: str = typer.Option(None), out: str = typer.Option(None),
                    as_json: bool = typer.Option(False, "--json")) -> None:
    """What was generated, from which snapshot, with hashes."""
    _emit_artifact(guard(service.bundle_manifest, snapshot=snapshot), out, as_json, "artifacts")


# ---------------------------------------------------------------- report ---

@report_app.command("brief")
def report_brief(snapshot: str = typer.Option(None),
                 as_json: bool = typer.Option(False, "--json")) -> None:
    """One screen: the state of the feed, and what to do next."""
    result = guard(service.report_brief, snapshot=snapshot)
    if as_json:
        console.print_json(json.dumps(result, default=str))
    else:
        console.print(f"[bold]{result.get('headline','')}[/bold]")
        _render(result, "next_actions", ["urgency", "why", "command"])
    for w in result.get("warnings", []):
        err.print(f"[yellow]warning:[/yellow] {w}")
    status = result.get("status")
    if status == "broken":
        raise typer.Exit(code=3)
    if status == "attention":
        raise typer.Exit(code=4)


@report_app.command("handoff")
def report_handoff(snapshot: str = typer.Option(None), out: str = typer.Option(None)) -> None:
    """Markdown of verified facts with citations, for pasting into CLAUDE.md."""
    result = guard(service.report_handoff, snapshot=snapshot)
    markdown = result.get("markdown", "")
    if out:
        target = Path(out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown)
        console.print(f"[green]wrote[/green] {target}")
    else:
        # Plain print, not rich: this is meant to be copied verbatim into
        # another document, and rich would inject markup into the paste.
        print(markdown)


@report_app.command("changelog")
def report_changelog(since: str = typer.Option(..., "--since"), to: str = typer.Option(None),
                     as_json: bool = typer.Option(False, "--json")) -> None:
    """Everything that changed since a pinned baseline."""
    _emit_drift(guard(service.report_changelog, since=since, to=to), as_json, "entries")


if __name__ == "__main__":
    app()
