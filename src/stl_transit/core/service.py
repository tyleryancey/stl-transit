"""The API surface. Pure functions returning JSON-serializable dicts.

This module is the ONLY thing `cli/` and `mcp/` import. It never prints, never
exits, never prompts (spec 2.1).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import __version__
from ..config import Config, load_config
from ..errors import PageNotFound, SnapshotNotFound, SourceNotFound, StlError, UsageError
from ..io import http
from ..io.clock import AGENCY_TZ, now_local
from ..io.db import build_sqlite, connect_ro, run_query
from ..io.store import Snapshot, Store
from . import assertions, bundle, diffing, report, support, web
from .gtfs import calendar as cal
from .gtfs import departures as dep
from .gtfs import entities, inspect
from .models import MAX_LIST_ITEMS, MAX_QUERY_BYTES, MAX_QUERY_ROWS, Provenance, paginate
from .rt import decode as rtdecode
from .rt import merge as rtmerge
from .rt import schema as rtschema
from .rt import wire as rtwire

DEFAULT_GTFS_SOURCE = "metro_gtfs"


# ------------------------------------------------------------------ helpers --

def _ctx(store: Store | None = None, config: Config | None = None) -> tuple[Store, Config]:
    return store or Store(), config or load_config()


def _gtfs_db(snap: Snapshot, force: bool = False) -> Path:
    db = snap.path / "feed.sqlite"
    build_sqlite(snap.payload, db, force=force)
    return db


def _provenance(snap: Snapshot, conn=None, today: date | None = None) -> dict[str, Any]:
    p = Provenance(
        snapshot_id=snap.snapshot_id,
        source=snap.source,
        source_url=snap.manifest.get("source_url", ""),
        fetched_at=snap.fetched_at,
        sha256=snap.sha256,
    )
    if conn is not None:
        cov = inspect.coverage(conn, today)
        if cov["service_start"]:
            p.feed_start_date = date.fromisoformat(cov["service_start"])
        if cov["service_end"]:
            p.feed_end_date = date.fromisoformat(cov["service_end"])
            p.stale_days = -(cov["days_remaining"] or 0)
    return p.model_dump(mode="json")


def _resolve_gtfs(ref: str | None, source: str, store: Store) -> Snapshot:
    return store.get(ref) if ref else store.latest(source)


def _ok(payload: dict[str, Any], prov: dict[str, Any] | None = None,
        warnings: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "provenance": prov, "warnings": warnings or [], "notes": []}
    out.update(payload)
    return out


# --------------------------------------------------------------- snapshots --

def snapshot_sources(store: Store | None = None, config: Config | None = None) -> dict[str, Any]:
    """List configured feeds and pages with their fetch status."""
    store, config = _ctx(store, config)
    items = []
    for name, src in sorted(config.feeds.items()):
        snaps = store.list(source=name)
        items.append(
            {
                "source": name,
                "kind": src.kind,
                "agency": src.agency,
                "region": src.region,
                "url": src.url or None,
                "usable": src.usable,
                "blocked_reason": (
                    None if src.usable
                    else ("URL unresolved -- " + (src.discovery_notes or "no notes"))
                ),
                "seasonal": src.seasonal,
                "snapshots": len(snaps),
                "latest_snapshot": snaps[0].snapshot_id if snaps else None,
                "latest_fetched_at": snaps[0].manifest.get("fetched_at") if snaps else None,
                "terms_url": src.terms_url,
            }
        )
    return _ok({"items": items, "count": len(items),
                "pages": sorted(config.pages),
                "config_path": str(config.path)})


def snapshot_list(kind: str | None = None, source: str | None = None, limit: int = 50,
                  offset: int = 0, store: Store | None = None) -> dict[str, Any]:
    """List stored snapshots, newest first."""
    store, _ = _ctx(store)
    pins = {v: k for k, v in store.pins().items()}
    rows = [
        {
            "snapshot_id": s.snapshot_id,
            "kind": s.kind,
            "source": s.source,
            "fetched_at": s.manifest.get("fetched_at"),
            "bytes": s.manifest.get("bytes"),
            "sha256": s.sha256[:12],
            "pin": pins.get(s.snapshot_id),
        }
        for s in store.list(kind=kind, source=source)
    ]
    window, meta = paginate(rows, offset, limit)
    return _ok({"items": window, **meta})


def snapshot_show(ref: str, store: Store | None = None) -> dict[str, Any]:
    """Full manifest for one snapshot, including the HTTP response headers."""
    store, _ = _ctx(store)
    snap = store.get(ref)
    return _ok({"manifest": snap.manifest, "path": str(snap.path)})


def snapshot_fetch(source: str = DEFAULT_GTFS_SOURCE, force: bool = False,
                   store: Store | None = None, config: Config | None = None) -> dict[str, Any]:
    """Download a feed into the snapshot store.

    Uses conditional requests: an unchanged feed costs a 304 and returns
    `unchanged=True` without creating a duplicate snapshot.
    """
    store, config = _ctx(store, config)
    src = config.source(source)
    if not src.usable:
        raise SourceNotFound(
            f"Source {source!r} has no resolved URL.",
            remedy=src.discovery_notes or "Set `url` for this source in sources.toml.",
            source=source,
        )
    res = http.fetch(src.url, config.http, cache_dir=None if force else store.http_cache,
                     conditional=not force)
    if res.not_modified:
        existing = store.list(source=source)
        return _ok(
            {"unchanged": True, "status": 304,
             "snapshot_id": existing[0].snapshot_id if existing else None},
            warnings=["Feed unchanged since last fetch (HTTP 304)."],
        )
    from ..io.store import sha256_bytes

    digest = sha256_bytes(res.content)
    kind = "gtfs" if src.kind == "gtfs" else "rt"
    dupe = store.find_by_digest(kind, source, digest)
    if dupe and not force:
        return _ok({"unchanged": True, "status": res.status, "snapshot_id": dupe.snapshot_id},
                   warnings=["Byte-identical to an existing snapshot; not re-stored."])
    filename = "source.zip" if src.kind == "gtfs" else "feed.pb"
    snap = store.put(kind, source, res.content, filename, src.url,
                     extra={"http_headers": res.headers, "entity": src.entity})
    return _ok({"unchanged": False, "status": res.status, "snapshot_id": snap.snapshot_id,
                "bytes": len(res.content), "sha256": digest,
                "elapsed_seconds": round(res.elapsed_seconds, 2)})


def snapshot_import(path: str, source: str = DEFAULT_GTFS_SOURCE, kind: str = "gtfs",
                    source_url: str = "", store: Store | None = None) -> dict[str, Any]:
    """Ingest a feed obtained outside this tool (e.g. a Mobility Database archive)."""
    store, _ = _ctx(store)
    p = Path(path).expanduser()
    if not p.is_file():
        raise StlError(f"No file at {p}.", remedy="Check the path.")
    filename = "source.zip" if kind == "gtfs" else "feed.pb"
    snap = store.put(kind, source, p.read_bytes(), filename, source_url,
                     extra={"imported_from": str(p)})
    return _ok({"snapshot_id": snap.snapshot_id, "bytes": snap.manifest["bytes"]})


def snapshot_pin(ref: str, name: str, store: Store | None = None) -> dict[str, Any]:
    store, _ = _ctx(store)
    snap = store.get(ref)
    return _ok({"pins": store.pin(snap.snapshot_id, name)})


def snapshot_verify(ref: str, store: Store | None = None) -> dict[str, Any]:
    store, _ = _ctx(store)
    return _ok(store.verify(store.get(ref).snapshot_id))


def snapshot_gc(keep: int = 5, dry_run: bool = True, store: Store | None = None) -> dict[str, Any]:
    store, _ = _ctx(store)
    return _ok(store.gc(keep=keep, dry_run=dry_run))


# --------------------------------------------------------------- gtfs read --

def _with_conn(snapshot: str | None, source: str, store: Store, rebuild: bool = False):
    snap = _resolve_gtfs(snapshot, source, store)
    db = _gtfs_db(snap, force=rebuild)
    return snap, connect_ro(db)


def gtfs_import(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                force: bool = False, store: Store | None = None) -> dict[str, Any]:
    """Build (or rebuild) the SQLite index for a snapshot."""
    store, _ = _ctx(store)
    snap = _resolve_gtfs(snapshot, source, store)
    db = snap.path / "feed.sqlite"
    result = build_sqlite(snap.payload, db, force=force)
    conn = connect_ro(db)
    try:
        return _ok(result, _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_files(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
               store: Store | None = None) -> dict[str, Any]:
    """File inventory with row counts, plus which optional GTFS files are ABSENT."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(inspect.files(snap.payload, conn), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_stats(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
               store: Store | None = None) -> dict[str, Any]:
    """Headline counts: routes by type, stops, trips, stop_times, service_ids."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(inspect.stats(conn), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_features(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                  store: Store | None = None) -> dict[str, Any]:
    """Which GTFS features this feed declares, in Mobility Database badge terms."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(inspect.features(conn), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_schema(table: str, snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                store: Store | None = None) -> dict[str, Any]:
    """Columns, null rates, cardinality and samples for one GTFS file."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(inspect.schema(conn, table), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_coverage(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                  as_of: datetime | None = None, store: Store | None = None) -> dict[str, Any]:
    """Service date range and days-to-expiry. Surveil this: Metro's feed ends at
    the next quarterly pick and a stale cache goes silently blank."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        today = now_local(as_of).date()
        cov = inspect.coverage(conn, today)
        warn = [cov["warning"]] if cov["warning"] else []
        return _ok(cov, _provenance(snap, conn, today), warn)
    finally:
        conn.close()


def gtfs_query(sql: str, snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
               limit: int = 200, store: Store | None = None) -> dict[str, Any]:
    """Run a read-only SQL query against the imported GTFS.

    Writes, ATTACH and PRAGMA are denied at the SQLite driver, a wall-clock
    timeout is enforced, and results are capped. Tables are the GTFS filenames
    without .txt: agency, stops, routes, trips, stop_times, calendar,
    calendar_dates, shapes, feed_info.
    """
    store, _ = _ctx(store)
    snap = _resolve_gtfs(snapshot, source, store)
    db = _gtfs_db(snap)
    result = run_query(db, sql, min(limit, MAX_QUERY_ROWS), MAX_QUERY_BYTES)
    warn = ["Result truncated by the row or byte cap."] if result["truncated"] else []
    return _ok(result, _provenance(snap), warn)


def gtfs_routes(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                route_type: str | None = None, search: str | None = None,
                limit: int = 100, offset: int = 0, store: Store | None = None) -> dict[str, Any]:
    """List routes with ids, names, type and trip counts."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        rows = entities.routes(conn, route_type, search)
        window, meta = paginate(rows, offset, limit)
        return _ok({"items": window, **meta}, _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_route(route_id: str, snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
               store: Store | None = None) -> dict[str, Any]:
    """One route: directions, headsigns, service breakdown, first/last departure."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(entities.route_detail(conn, route_id), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_stops(search: str | None = None, code: str | None = None, route_id: str | None = None,
               snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
               limit: int = 50, offset: int = 0, store: Store | None = None) -> dict[str, Any]:
    """Search stops by name, rider-facing code, or serving route."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        rows = entities.stops(conn, search, code, route_id)
        window, meta = paginate(rows, offset, limit)
        return _ok({"items": window, **meta}, _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_stop(stop: str, snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
              store: Store | None = None) -> dict[str, Any]:
    """One stop resolved by code or id, with the routes that serve it."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(entities.stop_detail(conn, stop), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_stop_resolve(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                      store: Store | None = None) -> dict[str, Any]:
    """Determine which field holds the number printed on a bus stop sign.

    The app's entire input UX depends on this answer, because the Light SDK
    exposes no usable location API and 'stops near me' is not buildable.
    """
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(entities.stop_resolve(conn), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_calendar(on: str | None = None, snapshot: str | None = None,
                  source: str = DEFAULT_GTFS_SOURCE, as_of: datetime | None = None,
                  store: Store | None = None) -> dict[str, Any]:
    """service_ids active on a date, showing calendar.txt and each
    calendar_dates.txt exception separately rather than pre-merged."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        day = date.fromisoformat(on) if on else now_local(as_of).date()
        return _ok(cal.active_services(conn, day), _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_service_day(timestamp: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                     as_of: datetime | None = None) -> dict[str, Any]:
    """Which service date(s) a wall-clock instant belongs to, and the 24:xx+ offset.

    Makes the classic off-by-one-day bug inspectable: a departure at 00:12 may
    be encoded as 24:12:00 on the PREVIOUS service date.
    """
    when = datetime.fromisoformat(timestamp) if timestamp else now_local(as_of)
    if when.tzinfo is None:
        when = when.replace(tzinfo=AGENCY_TZ)
    when = when.astimezone(AGENCY_TZ)
    today, yesterday = when.date(), when.date() - timedelta(days=1)
    rows = []
    for sd in (yesterday, today):
        start = cal.service_day_start(sd, AGENCY_TZ)
        # Subtract in UTC. Python defines subtraction between two datetimes
        # carrying the SAME tzinfo object as wall-clock arithmetic, so a plain
        # `when - start` is an hour wrong on both DST transition days -- which
        # is precisely the day this tool exists to make inspectable.
        secs = int(
            (when.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
        )
        rows.append(
            {
                "service_date": sd.isoformat(),
                "service_day_start": start.isoformat(),
                "gtfs_seconds": secs,
                "gtfs_time": cal.format_gtfs_time(secs) if 0 <= secs < 48 * 3600 else None,
                "plausible": 0 <= secs < 30 * 3600,
                "after_midnight_encoding": secs >= 86_400,
            }
        )
    return _ok(
        {
            "instant_local": when.isoformat(),
            "timezone": str(AGENCY_TZ),
            "candidates": rows,
            "note": "GTFS measures from noon-minus-12h, not local midnight. On DST "
                    "transition days those differ by an hour. Port that exactly.",
        }
    )


def gtfs_late_night(threshold: str = "24:00:00", snapshot: str | None = None,
                    source: str = DEFAULT_GTFS_SOURCE, limit: int = 50,
                    store: Store | None = None) -> dict[str, Any]:
    """Trips crossing the service-day boundary, and the feed's maximum time."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(inspect.late_night(conn, threshold, min(limit, MAX_LIST_ITEMS)),
                   _provenance(snap, conn))
    finally:
        conn.close()


def gtfs_departures(stop: str, at: str | None = None, window_minutes: int = 90,
                    route: str | None = None, limit: int = 20,
                    snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                    as_of: datetime | None = None, store: Store | None = None) -> dict[str, Any]:
    """Scheduled departures at a stop for a time window. Schedule only."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        when = datetime.fromisoformat(at) if at else now_local(as_of)
        if when.tzinfo is None:
            when = when.replace(tzinfo=AGENCY_TZ)
        # Convert BEFORE deriving the date. An instant given in UTC can fall on
        # a different calendar day in Chicago, and expiry must be judged on the
        # agency's day -- the same day the departures engine works in.
        when = when.astimezone(AGENCY_TZ)
        prov = _provenance(snap, conn, when.date())
        result = dep.departures(conn, stop, when, window_minutes,
                                AGENCY_TZ, min(limit, MAX_LIST_ITEMS), route)
        warns = []
        if prov.get("stale_days") is not None and prov["stale_days"] > 0:
            warns.append(f"Feed expired {prov['stale_days']} day(s) ago; results may be empty.")
        if result["total"] == 0:
            warns.append("No departures in window. Use stl_support_explain_empty to diagnose.")
        return _ok(result, prov, warns)
    finally:
        conn.close()


def support_explain_empty(stop: str, at: str | None = None, window_minutes: int = 90,
                          snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                          as_of: datetime | None = None,
                          store: Store | None = None) -> dict[str, Any]:
    """Diagnose why a stop shows no departures: unknown stop, expired feed, no
    service that day, retired stop, or simply too narrow a window."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        when = datetime.fromisoformat(at) if at else now_local(as_of)
        if when.tzinfo is None:
            when = when.replace(tzinfo=AGENCY_TZ)
        when = when.astimezone(AGENCY_TZ)  # see gtfs_departures: agency day, not caller's
        cov = inspect.coverage(conn, when.date())
        feed_end = date.fromisoformat(cov["service_end"]) if cov["service_end"] else None
        result = dep.explain_empty(conn, stop, when, window_minutes, AGENCY_TZ, feed_end)
        return _ok(result, _provenance(snap, conn, when.date()))
    finally:
        conn.close()


# --------------------------------------------------------------------- rt --

RT_SOURCES = {
    "trip_updates": "metro_rt_trips",
    "vehicle_positions": "metro_rt_vehicles",
    "alerts": "metro_rt_alerts",
}


def rt_fetch(entity: str = "trip_updates", store: Store | None = None,
             config: Config | None = None) -> dict[str, Any]:
    """Fetch one realtime feed (trip_updates | vehicle_positions | alerts)."""
    source = RT_SOURCES.get(entity, entity)
    return snapshot_fetch(source, store=store, config=config)


def rt_decode(snapshot: str | None = None, entity: str = "trip_updates",
              limit: int = 10, store: Store | None = None) -> dict[str, Any]:
    """Decode a realtime snapshot into normalized JSON via the hand-rolled reader."""
    store, _ = _ctx(store)
    snap = store.get(snapshot) if snapshot else store.latest(RT_SOURCES.get(entity, entity))
    decoded = rtdecode.decode_feed(snap.payload.read_bytes())
    entities_ = decoded.pop("entities", [])
    window, meta = paginate(entities_, 0, limit)
    warn = []
    if decoded.get("unknown_top_level"):
        warn.append("Feed contains top-level fields absent from core/rt/schema.py.")
    return _ok({**decoded, "items": window, **meta}, _provenance(snap), warn)


def rt_wire(snapshot: str | None = None, entity: str = "trip_updates", depth: int = 5,
            max_entities: int = 2, store: Store | None = None) -> dict[str, Any]:
    """Raw protobuf wire dump: field number, wire type, length, bytes, nesting.

    This is the ground-truth artifact for validating the Kotlin decoder. Point
    the Kotlin implementation at the same snapshot and compare trees. The named
    path for each numeric path is included so the mapping is unambiguous.
    """
    store, _ = _ctx(store)
    snap = store.get(snapshot) if snapshot else store.latest(RT_SOURCES.get(entity, entity))
    data = snap.payload.read_bytes()
    msg = rtwire.parse(data)

    header = [f for f in msg.fields if f.number == 1]
    entities_ = [f for f in msg.fields if f.number == 2][:max_entities]
    subset = b"".join(_reencode(f) for f in header + entities_)
    tree = rtwire.dump(subset, max_depth=depth)
    for node in _flatten(tree):
        node["named_path"] = rtschema.path_names(node["path"])
    return _ok(
        {
            "total_bytes": len(data),
            "top_level_fields": len(msg.fields),
            "entities_in_feed": sum(1 for f in msg.fields if f.number == 2),
            "entities_dumped": len(entities_),
            "tree": tree,
        },
        _provenance(snap),
    )


def _reencode(f: rtwire.Field_) -> bytes:
    """Re-emit one field as wire bytes so a subset can be dumped standalone."""
    out = bytearray()
    key = (f.number << 3) | f.wire_type
    out += _varint(key)
    if f.wire_type == rtwire.WIRE_VARINT:
        out += _varint(f.varint or 0)
    elif f.wire_type == rtwire.WIRE_LEN:
        out += _varint(len(f.raw))
        out += f.raw
    else:
        out += f.raw
    return bytes(out)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _flatten(tree: list[dict[str, Any]]):
    for node in tree:
        yield node
        if isinstance(node.get("submessage"), list):
            yield from _flatten(node["submessage"])


def rt_schema_census(entity: str = "trip_updates", samples: int = 1,
                     snapshot: str | None = None, store: Store | None = None) -> dict[str, Any]:
    """Which proto fields does Metro actually populate, and at what rate?

    Decides what to model in Kotlin. Any path reported as unmodelled is a field
    present in the bytes but missing from core/rt/schema.py.
    """
    store, _ = _ctx(store)
    source = RT_SOURCES.get(entity, entity)
    snaps = [store.get(snapshot)] if snapshot else store.list(source=source)[:samples]
    if not snaps:
        raise SnapshotNotFound(
            f"No realtime snapshots for {entity}.",
            remedy=f"Fetch one with `stl rt fetch --entity {entity}`.",
        )
    blobs = [s.payload.read_bytes() for s in snaps]
    census = rtdecode.field_census(blobs)
    return _ok(census, [_provenance(s) for s in snaps])


def rt_reference() -> dict[str, Any]:
    """The GTFS-Realtime field map as a flat table, for porting to Kotlin."""
    rows = rtschema.kotlin_reference()
    return _ok(
        {"items": rows, "count": len(rows), "total": len(rows),
         "enums": rtschema.ENUMS,
         "note": "Transcribe into Kotlin. The LP3 has no protobuf runtime on the "
                 "dependency allow-list, so the decoder is either "
                 "kotlinx-serialization-protobuf or hand-written from this table."}
    )


def rt_health(entity: str | None = None, store: Store | None = None,
              config: Config | None = None, as_of: datetime | None = None) -> dict[str, Any]:
    """Staleness, entity counts and header agreement across the realtime feeds."""
    store, config = _ctx(store, config)
    entities_ = [entity] if entity else list(RT_SOURCES)
    now = now_local(as_of)
    rows = []
    for ent in entities_:
        source = RT_SOURCES.get(ent, ent)
        snaps = store.list(source=source)
        if not snaps:
            rows.append({"entity": ent, "available": False,
                         "remedy": f"stl rt fetch --entity {ent}"})
            continue
        snap = snaps[0]
        decoded = rtdecode.decode_feed(snap.payload.read_bytes())
        ts = decoded["header"].get("timestamp")
        age = None
        if ts:
            age = int(now.timestamp() - ts)
        rows.append(
            {
                "entity": ent,
                "available": True,
                "snapshot_id": snap.snapshot_id,
                "bytes": snap.manifest.get("bytes"),
                "header_timestamp": ts,
                "header_timestamp_iso": decoded["header_timestamp_iso"],
                "age_seconds": age,
                "stale": bool(age is not None and age > 300),
                "entity_count": decoded["entity_count"],
                "entity_kinds": decoded["entity_kinds"],
                "gtfs_realtime_version": decoded["header"].get("gtfs_realtime_version"),
                "incrementality": decoded["header"].get("incrementality"),
            }
        )
    stamps = {r.get("header_timestamp") for r in rows if r.get("header_timestamp")}
    warn = []
    if len(stamps) > 1:
        warn.append(f"Feeds disagree on header timestamp by up to "
                    f"{max(stamps) - min(stamps)}s.")
    for r in rows:
        if r.get("stale"):
            warn.append(f"{r['entity']} is {r['age_seconds']}s old.")
    return _ok({"items": rows, "checked_at": now.isoformat()}, None, warn)


def rt_stop_arrivals(stop: str, at: str | None = None, window_minutes: int = 90,
                     limit: int = 20, snapshot: str | None = None,
                     rt_snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                     as_of: datetime | None = None, store: Store | None = None) -> dict[str, Any]:
    """Scheduled departures with realtime predictions merged in.

    This is exactly what the app should render. When realtime is unavailable it
    degrades to scheduled-only and says so, which the app must also do.
    """
    store, _ = _ctx(store)
    scheduled = gtfs_departures(stop, at, window_minutes, None, limit, snapshot, source,
                                as_of, store)
    decoded = None
    try:
        snap = store.get(rt_snapshot) if rt_snapshot else store.latest("metro_rt_trips")
        decoded = rtdecode.decode_feed(snap.payload.read_bytes())
    except SnapshotNotFound:
        decoded = None
    merged = rtmerge.merge(scheduled, decoded)
    return merged


# ----------------------------------------------------------------- assert --
#
# The app depends on facts about the feed that Metro never promised: that
# stop_code is populated and unique, that no trip runs past 28:00, that the
# agency timezone does not move. Encode each one, run them on a schedule, and
# find out from a cron job instead of from a user.

def assert_list() -> dict[str, Any]:
    """Every assumption: what it checks and what breaks if it fails."""
    return _ok(assertions.list_assumptions())


def assert_explain(assumption_id: str) -> dict[str, Any]:
    """One assumption in full: why it matters and how to remediate it."""
    return _ok(assertions.explain(assumption_id))


def _web_hashes(store: Store) -> dict[str, str]:
    """Latest captured content hash per page, for the *_unchanged assumptions."""
    out: dict[str, str] = {}
    for snap in store.list(kind="web"):
        page = snap.source
        if page not in out:  # store.list is newest-first
            digest = snap.manifest.get("content_hash")
            if digest:
                out[page] = digest
    return out


def _rt_feeds_for_assertions(store: Store, now: datetime) -> dict[str, dict[str, Any]]:
    """Shape the stored realtime snapshots the way assertions.run expects.

    Absent feeds are simply omitted rather than passed as empty: the suite
    treats a missing input as `skip`, and a skip is honest where a zero would
    be a fabricated measurement.
    """
    feeds: dict[str, dict[str, Any]] = {}
    for entity, source in RT_SOURCES.items():
        snaps = store.list(source=source)
        if not snaps:
            continue
        snap = snaps[0]
        blob = snap.payload.read_bytes()
        decoded = rtdecode.decode_feed(blob)
        ts = decoded["header"].get("timestamp")
        fetched = snap.fetched_at
        trip_ids = [
            (e.get("trip_update") or {}).get("trip", {}).get("trip_id")
            for e in decoded["entities"]
            if e.get("trip_update")
        ]
        feeds[entity] = {
            "snapshot_id": snap.snapshot_id,
            "header_timestamp": ts,
            # Two different ages, because they answer two different questions.
            # `age_at_fetch` is how far behind Metro's feed was when we took the
            # sample -- the property the app actually depends on. `age_seconds`
            # is how old our copy is now, which is a "go fetch" signal and not a
            # statement about Metro at all. Reporting only the second made this
            # assumption fail every morning on a machine that slept.
            "age_at_fetch_seconds": (
                (fetched.timestamp() - ts) if (ts and fetched) else None),
            "age_seconds": (now.timestamp() - ts) if ts else None,
            "fetched_ago_seconds": (
                (now.timestamp() - fetched.timestamp()) if fetched else None),
            "trip_ids": [t for t in trip_ids if t],
            "entity_count": decoded["entity_count"],
            "unmodelled": rtdecode.field_census([blob])["unmodelled"],
        }
    return feeds


def assert_run(only: list[str] | None = None, baseline: str | None = None,
               snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
               as_of: datetime | None = None, store: Store | None = None) -> dict[str, Any]:
    """Evaluate the assumption suite. Exit code 3 on violation.

    `baseline` is a snapshot id or pin name for the stability assumptions.
    Without it those two skip rather than pass -- a stability check with
    nothing to compare against has not been performed.
    """
    store, _ = _ctx(store)
    now = now_local(as_of)
    snap, conn = _with_conn(snapshot, source, store)
    base_conn = None
    try:
        if baseline:
            base_snap = store.get(baseline)
            base_conn = connect_ro(_gtfs_db(base_snap))
        result = assertions.run(
            conn,
            only=only,
            baseline_conn=base_conn,
            web_hashes=_web_hashes(store) or None,
            rt_feeds=_rt_feeds_for_assertions(store, now) or None,
            as_of=now,
        )
        warns = [f"{v['id']}: {v['detail']}" for v in result.get("violations", [])]
        return _ok(result, _provenance(snap, conn, now.date()), warns)
    finally:
        conn.close()
        if base_conn is not None:
            base_conn.close()


# ------------------------------------------------------------------- diff --

def _two_conns(a: str, b: str, source: str, store: Store):
    """Open two snapshots for comparison, oldest-first by convention.

    Callers pass them in the order they mean; we do not reorder, because
    'what changed from A to B' and 'from B to A' have different answers and
    silently normalising would make the direction of every delta a guess.
    """
    snap_a = store.get(a)
    snap_b = store.get(b)
    return snap_a, snap_b, connect_ro(_gtfs_db(snap_a)), connect_ro(_gtfs_db(snap_b))


def _diff(fn, a: str, b: str, source: str, store: Store | None, **kwargs) -> dict[str, Any]:
    store, _ = _ctx(store)
    snap_a, snap_b, conn_a, conn_b = _two_conns(a, b, source, store)
    try:
        result = fn(conn_a, conn_b, **kwargs)
        prov = [_provenance(snap_a, conn_a), _provenance(snap_b, conn_b)]
        warns = ["Drift detected between these snapshots."] if result.get("drift_detected") else []
        return _ok(result, prov, warns)
    finally:
        conn_a.close()
        conn_b.close()


def diff_summary(a: str, b: str, source: str = DEFAULT_GTFS_SOURCE,
                 store: Store | None = None) -> dict[str, Any]:
    """One-screen digest of everything that changed between two snapshots."""
    return _diff(diffing.summary, a, b, source, store)


def diff_files(a: str, b: str, source: str = DEFAULT_GTFS_SOURCE,
               store: Store | None = None) -> dict[str, Any]:
    """Files added or removed, and row-count deltas per file."""
    return _diff(diffing.files, a, b, source, store)


def diff_routes(a: str, b: str, limit: int = 50, offset: int = 0,
                source: str = DEFAULT_GTFS_SOURCE, store: Store | None = None) -> dict[str, Any]:
    """route_ids added, removed or renamed between two snapshots."""
    return _diff(diffing.routes, a, b, source, store, limit=limit, offset=offset)


def diff_stops(a: str, b: str, moved_threshold_m: float = 25.0, limit: int = 50,
               offset: int = 0, source: str = DEFAULT_GTFS_SOURCE,
               store: Store | None = None) -> dict[str, Any]:
    """Stops added, removed, renamed or physically moved."""
    return _diff(diffing.stops, a, b, source, store, moved_threshold_m=moved_threshold_m,
                 limit=limit, offset=offset)


def diff_stop_ids(a: str, b: str, source: str = DEFAULT_GTFS_SOURCE,
                  store: Store | None = None) -> dict[str, Any]:
    """Survival rate of stop_id and stop_code across a service change.

    The app's saved-stops feature lives or dies on this number: every code
    that does not survive is a user whose saved stop silently stops working.
    """
    return _diff(diffing.stop_ids, a, b, source, store)


def diff_calendar(a: str, b: str, source: str = DEFAULT_GTFS_SOURCE,
                  store: Store | None = None) -> dict[str, Any]:
    """service_id churn and date-range shifts."""
    return _diff(diffing.calendar, a, b, source, store)


def diff_schedule(a: str, b: str, stop: str | None = None, route: str | None = None,
                  on: str | None = None, limit: int = 50,
                  source: str = DEFAULT_GTFS_SOURCE, store: Store | None = None) -> dict[str, Any]:
    """Timetable deltas for one stop or route. Requires a filter."""
    return _diff(diffing.schedule, a, b, source, store, stop=stop, route_id=route,
                 on=date.fromisoformat(on) if on else None, limit=limit)


# -------------------------------------------------------------------- web --

def _page(config: Config, page_key: str) -> dict[str, Any]:
    try:
        return config.pages[page_key]
    except KeyError:
        raise PageNotFound(
            f"No configured page named {page_key!r}.",
            remedy="Run `stl web list` to see configured pages, or add one to "
            "the [pages] table in sources.toml.",
            available=sorted(config.pages),
        ) from None


def web_capture(page: str | None = None, force: bool = False, store: Store | None = None,
                config: Config | None = None, as_of: datetime | None = None) -> dict[str, Any]:
    """Fetch, normalize, extract and store one page (or all of them).

    Hashes the EXTRACTED content, never the raw HTML. Raw HTML changes on
    every request -- analytics ids, nonces, rotating hero images -- so hashing
    it makes every drift check a false positive, and a check that always fires
    is a check nobody reads.
    """
    store, config = _ctx(store, config)
    now = now_local(as_of)
    keys = [page] if page else sorted(config.pages)
    captured, skipped, warns = [], [], []
    for key in keys:
        cfg = _page(config, key)
        existing = store.list(kind="web", source=key)
        last = existing[0].fetched_at if existing else None
        allowed, reason = web.should_fetch(
            key, last, float(cfg.get("fetch_interval_hours", 24)), now)
        if not allowed and not force:
            skipped.append({"page": key, "reason": reason})
            continue
        res = http.fetch(cfg["url"], config.http,
                         cache_dir=None if force else store.http_cache, conditional=not force)
        if res.not_modified:
            skipped.append({"page": key, "reason": "HTTP 304 -- unchanged since last capture."})
            continue
        html = res.content.decode("utf-8", errors="replace")
        record = web.capture(key, html, cfg["url"], now, cfg.get("extractor", "text"))
        text = record.pop("normalized_text", "")
        snap = store.put(
            "web", key, text.encode("utf-8"), "content.txt", cfg["url"],
            extra={"content_hash": record["content_hash"], "extractor": cfg.get("extractor"),
                   "extraction": record.get("extraction"), "http_headers": res.headers,
                   "raw_bytes": len(res.content)},
        )
        if not record.get("ok", True):
            warns.append(f"{key}: extraction failed -- {record.get('extraction_error')}")
        captured.append({**record, "snapshot_id": snap.snapshot_id})
    return _ok({"captured": captured, "skipped": skipped,
                "count": len(captured), "checked_at": now.isoformat()}, None, warns)


def web_list(store: Store | None = None, config: Config | None = None) -> dict[str, Any]:
    """Configured pages with their last capture, hash and drift status."""
    store, config = _ctx(store, config)
    items = []
    for key in sorted(config.pages):
        cfg = config.pages[key]
        snaps = store.list(kind="web", source=key)
        items.append({
            "page": key,
            "url": cfg.get("url"),
            "extractor": cfg.get("extractor"),
            "fetch_interval_hours": cfg.get("fetch_interval_hours"),
            "captures": len(snaps),
            "last_captured": snaps[0].manifest.get("fetched_at") if snaps else None,
            "content_hash": snaps[0].manifest.get("content_hash") if snaps else None,
        })
    return _ok({"items": items, "count": len(items)})


def _captures(store: Store, page: str) -> list[dict[str, Any]]:
    """Stored captures for a page, newest first, in web.check's shape."""
    out = []
    for snap in store.list(kind="web", source=page):
        out.append({
            "page_key": page,
            "snapshot_id": snap.snapshot_id,
            "fetched_at": snap.manifest.get("fetched_at"),
            "url": snap.manifest.get("source_url", ""),
            "content_hash": snap.manifest.get("content_hash", ""),
            "extraction": snap.manifest.get("extraction"),
            "normalized_text": snap.payload.read_text(encoding="utf-8", errors="replace"),
        })
    return out


def web_extract(page: str, snapshot: str | None = None, store: Store | None = None,
                config: Config | None = None) -> dict[str, Any]:
    """Structured extraction from the latest (or a named) capture of a page."""
    store, config = _ctx(store, config)
    _page(config, page)
    snaps = store.list(kind="web", source=page)
    if not snaps:
        raise PageNotFound(
            f"No capture stored for page {page!r}.",
            remedy=f"Capture it first with `stl web capture {page}`.",
            page=page,
        )
    snap = store.get(snapshot) if snapshot else snaps[0]
    return _ok({
        "page": page,
        "snapshot_id": snap.snapshot_id,
        "fetched_at": snap.manifest.get("fetched_at"),
        "content_hash": snap.manifest.get("content_hash"),
        "extractor": snap.manifest.get("extractor"),
        "extraction": snap.manifest.get("extraction"),
    })


def web_diff(page: str, a: str | None = None, b: str | None = None,
             store: Store | None = None) -> dict[str, Any]:
    """Normalized-text diff between two captures. Defaults to the latest two."""
    store, _ = _ctx(store)
    caps = _captures(store, page)
    if len(caps) < 2 and not (a and b):
        raise PageNotFound(
            f"Need two captures of {page!r} to diff; found {len(caps)}.",
            remedy=f"Capture it again later, or run `stl web capture {page} --force`.",
            page=page, captures=len(caps),
        )
    by_id = {c["snapshot_id"]: c for c in caps}
    before = by_id[a] if a else caps[1]
    after = by_id[b] if b else caps[0]
    return _ok({"page": page, **web.compare(before, after)})


def web_check(store: Store | None = None, config: Config | None = None) -> dict[str, Any]:
    """Drift check across every configured page. Exit code 4 on change."""
    store, config = _ctx(store, config)
    caps = {key: _captures(store, key) for key in sorted(config.pages)}
    caps = {k: v for k, v in caps.items() if v}
    if not caps:
        raise PageNotFound(
            "No pages have been captured yet.",
            remedy="Run `stl web capture --all` to establish a baseline, then "
            "re-run this to detect drift against it.",
        )
    result = web.check(caps)
    warns = [f"{i['page']}: {i.get('breaks', 'content changed')}"
             for i in result["items"] if i.get("changed")]
    return _ok(result, None, warns)


# ----------------------------------------------------------------- bundle --

def _fare_rows(store: Store) -> tuple[list[dict[str, Any]], str, date | None]:
    """The latest captured fare table, with the URL and date it came from."""
    snaps = store.list(kind="web", source="fares")
    if not snaps:
        raise PageNotFound(
            "No fare page has been captured.",
            remedy="Fares are NOT in the GTFS feed -- they live on Metro's "
            "website. Run `stl web capture fares` first.",
        )
    snap = snaps[0]
    extraction = snap.manifest.get("extraction") or {}
    fetched = snap.fetched_at
    return extraction.get("rows", []), snap.manifest.get("source_url", ""), (
        fetched.date() if fetched else None)


def bundle_fares(fmt: str = "json", store: Store | None = None,
                 as_of: datetime | None = None) -> dict[str, Any]:
    """Fare table with its as_of date and source URL baked in.

    Fares are not in the GTFS feed, so this reads the latest web capture. The
    baked-in date is what lets a reviewer -- and a rider -- tell whether the
    bundled table is still current.
    """
    store, _ = _ctx(store)
    rows, url, captured_on = _fare_rows(store)
    return _ok(bundle.fares(rows, captured_on or now_local(as_of).date(), url, fmt=fmt))


def bundle_holidays(year: int | None = None, store: Store | None = None,
                    as_of: datetime | None = None) -> dict[str, Any]:
    """Holiday to service-type mapping, bus and rail kept distinct.

    MetroBus runs Sunday service on a holiday; MetroLink runs "Weekend"
    service. Those are different concepts and merging them is a real bug.
    """
    store, _ = _ctx(store)
    snaps = store.list(kind="web", source="holidays")
    if not snaps:
        raise PageNotFound(
            "No holiday page has been captured.",
            remedy="Run `stl web capture holidays` first.",
        )
    snap = snaps[0]
    extraction = snap.manifest.get("extraction") or {}
    return _ok(bundle.holidays(extraction.get("rows", []),
                               year or now_local(as_of).year,
                               snap.manifest.get("source_url", "")))


def bundle_stops_index(include_routes: bool = True, snapshot: str | None = None,
                       source: str = DEFAULT_GTFS_SOURCE,
                       store: Store | None = None) -> dict[str, Any]:
    """stop_code to (stop_id, name, routes) lookup for on-device resolution."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(bundle.stops_index(conn, include_routes=include_routes),
                   _provenance(snap, conn))
    finally:
        conn.close()


def bundle_compact(out: str, strategy: str = "balanced", routes: list[str] | None = None,
                   days: int | None = None, snapshot: str | None = None,
                   source: str = DEFAULT_GTFS_SOURCE,
                   store: Store | None = None) -> dict[str, Any]:
    """Build the pruned on-device database and report every pruning decision.

    The decisions list is as much the deliverable as the file: it is what a
    reviewer reads to judge whether the app is being honest about its coverage.
    """
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(bundle.compact(conn, Path(out).expanduser(), strategy=strategy,
                                  routes=routes, days=days),
                   _provenance(snap, conn))
    finally:
        conn.close()


def bundle_size_report(compact_path: str | None = None, snapshot: str | None = None,
                       source: str = DEFAULT_GTFS_SOURCE,
                       store: Store | None = None) -> dict[str, Any]:
    """Size budget by table, with index cost isolated from row data."""
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        return _ok(bundle.size_report(conn, Path(compact_path).expanduser()
                                      if compact_path else None),
                   _provenance(snap, conn))
    finally:
        conn.close()


def bundle_manifest(artifacts: list[dict[str, Any]] | None = None, snapshot: str | None = None,
                    source: str = DEFAULT_GTFS_SOURCE,
                    store: Store | None = None) -> dict[str, Any]:
    """What was generated, from which snapshot, with hashes -- for the tool's
    README and its vetting defense."""
    store, _ = _ctx(store)
    snap = _resolve_gtfs(snapshot, source, store)
    return _ok(bundle.manifest(artifacts or [], snap.snapshot_id, snap.sha256))


# ----------------------------------------------------------------- report --

def report_brief(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                 as_of: datetime | None = None, store: Store | None = None,
                 config: Config | None = None) -> dict[str, Any]:
    """One screen: the state of the feed right now, and what to do next.

    Every input is optional. A developer who has not fetched realtime or
    captured pages still gets a brief that says so, because a report that
    refuses to render without complete inputs is a report nobody runs.
    """
    store, config = _ctx(store, config)
    now = now_local(as_of)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        coverage = inspect.coverage(conn, now.date())
        try:
            asserts = assertions.run(
                conn, web_hashes=_web_hashes(store) or None,
                rt_feeds=_rt_feeds_for_assertions(store, now) or None, as_of=now)
        except StlError:
            asserts = None
        try:
            rt = rt_health(store=store, config=config, as_of=as_of)
        except StlError:
            rt = None
        try:
            drift = web_check(store=store, config=config)
        except StlError:
            drift = None
        snaps = snapshot_list(limit=10, store=store)["items"]
        result = report.brief(coverage, asserts, rt, drift, snaps, now)
        return _ok(result, _provenance(snap, conn, now.date()),
                   [f["detail"] for f in result.get("blocking", [])])
    finally:
        conn.close()


def report_handoff(snapshot: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                   as_of: datetime | None = None, store: Store | None = None) -> dict[str, Any]:
    """A markdown block of verified facts with citations, for pasting into
    CLAUDE.md. Every claim carries the snapshot and date it was verified
    against, so a future reader can re-verify rather than trust."""
    store, _ = _ctx(store)
    now = now_local(as_of)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        census = None
        try:
            census = rt_schema_census(store=store)
        except StlError:
            census = None
        return _ok(
            report.handoff(inspect.coverage(conn, now.date()),
                           entities.stop_resolve(conn),
                           census,
                           inspect.stats(conn),
                           snap.snapshot_id, snap.sha256, now),
            _provenance(snap, conn, now.date()),
        )
    finally:
        conn.close()


def report_changelog(since: str, to: str | None = None, source: str = DEFAULT_GTFS_SOURCE,
                     as_of: datetime | None = None, store: Store | None = None) -> dict[str, Any]:
    """Everything that changed since a pinned baseline, as readable prose."""
    store, _ = _ctx(store)
    latest = store.latest(source).snapshot_id
    summary = diff_summary(since, to or latest, source, store)
    return _ok(report.changelog(summary, since, now_local(as_of)),
               summary.get("provenance"))


# ---------------------------------------------------------------- support --

def support_repro(stop: str, at: str | None = None, window_minutes: int = 90,
                  rt_snapshot: str | None = None, snapshot: str | None = None,
                  source: str = DEFAULT_GTFS_SOURCE, as_of: datetime | None = None,
                  store: Store | None = None) -> dict[str, Any]:
    """Exactly what the app should have shown, given a stop and an instant.

    This is how "stop 15111 showed nothing at 11:47 last Tuesday" gets
    answered without a device and without waiting for Tuesday.
    """
    store, _ = _ctx(store)
    snap, conn = _with_conn(snapshot, source, store)
    try:
        when = datetime.fromisoformat(at) if at else now_local(as_of)
        if when.tzinfo is None:
            when = when.replace(tzinfo=AGENCY_TZ)
        when = when.astimezone(AGENCY_TZ)
        decoded = None
        if rt_snapshot is not None:
            try:
                rt_snap = store.get(rt_snapshot) if rt_snapshot else store.latest("metro_rt_trips")
                decoded = rtdecode.decode_feed(rt_snap.payload.read_bytes())
            except SnapshotNotFound:
                decoded = None
        cov = inspect.coverage(conn, when.date())
        feed_end = date.fromisoformat(cov["service_end"]) if cov["service_end"] else None
        result = support.repro(conn, stop, when, window_minutes, AGENCY_TZ,
                               rt_decoded=decoded, feed_end=feed_end)
        return _ok(result, _provenance(snap, conn, when.date()))
    finally:
        conn.close()


def support_diff_device(expected_json: str, actual_json: str) -> dict[str, Any]:
    """Diff an on-device capture against the oracle.

    Both arguments may be a file path or inline JSON, because the realistic
    input is something pasted out of a bug report.
    """
    def _load(value: str) -> Any:
        p = Path(value).expanduser()
        try:
            text = p.read_text() if p.is_file() else value
        except OSError as exc:
            raise UsageError(
                f"Could not read {p}: {exc}",
                remedy="Check the path, or paste the JSON inline instead.",
            ) from None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise UsageError(
                f"Could not parse as JSON: {exc}",
                remedy="Pass a path to a .json file or a valid JSON string. If you "
                "meant a path, note that this argument accepts inline JSON too, so a "
                "typo'd path is read as JSON text rather than reported as missing.",
            ) from None

    return _ok(support.diff_device(_load(expected_json), _load(actual_json)))


def support_bundle(as_of: datetime | None = None, store: Store | None = None,
                   config: Config | None = None) -> dict[str, Any]:
    """Everything a maintainer needs attached to a GitHub issue.

    Nothing here is sensitive -- this project has no auth anywhere -- but the
    bundle says so explicitly, so a reader knows it is safe to paste in public.
    """
    store, config = _ctx(store, config)
    now = now_local(as_of)
    snaps = snapshot_list(limit=25, store=store)["items"]
    try:
        recent = assert_run(store=store, as_of=as_of)
    except StlError:
        recent = None
    return _ok(support.bundle_report(
        {"root": str(store.root), "snapshots": snaps, "pins": store.pins()},
        {"path": str(config.path), "feeds": sorted(config.feeds), "pages": sorted(config.pages)},
        {"stl_transit": __version__, "python": sys.version.split()[0]},
        recent,
        now,
    ))


def doctor(store: Store | None = None, config: Config | None = None) -> dict[str, Any]:
    """Environment, store and configuration health."""
    import shutil

    store, config = _ctx(store, config)
    snaps = store.list()
    total_bytes = 0
    if (store.root / "snapshots").is_dir():
        total_bytes = sum(f.stat().st_size for f in (store.root / "snapshots").rglob("*")
                          if f.is_file())
    usable = [n for n, s in config.feeds.items() if s.usable]
    blocked = [n for n, s in config.feeds.items() if not s.usable]
    return _ok(
        {
            "python": sys.version.split()[0],
            "store_root": str(store.root),
            "store_exists": store.root.exists(),
            "snapshots": len(snaps),
            "store_bytes": total_bytes,
            "disk_free_bytes": shutil.disk_usage(store.root.parent).free
            if store.root.parent.exists() else None,
            "config_path": str(config.path),
            "sources_usable": usable,
            "sources_blocked": blocked,
            "pins": store.pins(),
        },
        None,
        [f"Source {n} has no resolved URL." for n in blocked],
    )
