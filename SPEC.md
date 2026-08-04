# `stl` — St. Louis Transit Data CLI

**Specification v1.0 · drafted 2026-08-03**

Developer tooling for the LP3 St. Louis transit tool. Never ships in the APK; never
vetted by Light. Its job is to answer feed questions with evidence, produce the test
oracles the Kotlin engine is graded against, and keep answering both after the tool
ships and the feed keeps moving underneath it.

Designed CLI-first, MCP-second: **every command is a thin formatting shell over a pure
function that already returns an MCP-shaped result.** Section 2 is the contract that makes
the eventual `@mcp.tool` wrapping mechanical rather than a rewrite.

> Naming note: the *dev* repo can be called whatever. The *shipped tool* cannot use
> Metro's trademarks (see the data-source doc, §0.4). Keep the two naming decisions apart.

---

## 1. What this exists to do

Five jobs, in the order they arrive:

| # | Job | Lives in |
|---|---|---|
| 1 | **Archaeology** — settle the `[VERIFY]` list: what's actually in the feed | `gtfs`, `rt` |
| 2 | **Oracle** — generate committed golden fixtures the pure-JVM tests assert against | `oracle` |
| 3 | **Surveillance** — detect when the feed moves and whether it broke an assumption | `assert`, `diff`, `history`, `web` |
| 4 | **Support** — reproduce "stop 15111 shows nothing" without a device | `support` |
| 5 | **Artifacts** — build what the app actually ships (fare table, compact DB) | `bundle` |

Job 2 is the one that pays for the whole thing. The Kotlin engine's correctness lives in
a departures calculation over GTFS calendar arithmetic and 24:xx+ service days — subtle,
easy to get plausibly wrong, and impossible to eyeball. An independent Python
implementation that produces committed expected-output JSON *is* the test suite.

---

## 2. The CLI→MCP contract

Eight rules. Break any of them and the MCP port becomes a rewrite.

### 2.1 Layering

```
stl_transit/
  core/          # pure logic. returns Pydantic models. NEVER prints, exits, or prompts.
    gtfs/        #   schedule reading, calendar math, departures
    rt/          #   protobuf decode, wire dump, RT/static join
    oracle/      #   fixture generation
    diffing/     #   snapshot comparison
    assertions/  #   the assumption suite
    models.py    #   Result envelope + all response models
  io/            # side effects, isolated
    http.py      #   httpx client, ETag/conditional, backoff, UA
    store.py     #   content-addressed snapshot store
    db.py        #   GTFS→SQLite import, read-only connections
    clock.py     #   injectable now()
  cli/           # Typer. The ONLY place that formats for humans.
  mcp/           # FastMCP. The ONLY other consumer of core.
  data/
    sources.toml #   feed + page registry
    oracle_cases.toml
    assertions.toml
```

`cli/` and `mcp/` are siblings, both thin, neither importing the other. If a behaviour
exists only in `cli/`, MCP won't have it.

### 2.2 Name mapping is a rule, not a coincidence

CLI command path joined with underscores, prefixed `stl_`:

```
stl gtfs departures        →  stl_gtfs_departures
stl rt wire                →  stl_rt_wire
stl diff stop-ids          →  stl_diff_stop_ids
stl oracle generate        →  stl_oracle_generate
```

Server name: `stl_transit_mcp`. Prefix everything — the server will sit alongside others.

### 2.3 Every result carries provenance

The entire point is verify-don't-trust. A result without provenance is a rumour.

```python
class Provenance(BaseModel):
    snapshot_id: str          # "gtfs-20260803T141200Z-a3f9c1"
    source_url: str
    fetched_at: datetime
    sha256: str
    feed_start_date: date | None
    feed_end_date: date | None
    stale_days: int | None    # days past feed_end_date, negative = still valid

class Result(BaseModel):
    ok: bool
    provenance: Provenance | list[Provenance] | None
    warnings: list[str] = []
    notes: list[str] = []

class ListResult(Result):
    total: int
    count: int
    offset: int
    has_more: bool
    next_offset: int | None
    truncated: bool           # true if a hard cap clipped results
    items: list[Any]
```

### 2.4 Bounded output, always

Every list-returning function takes `limit: int = 50` and `offset: int = 0`, and every
one has a hard ceiling regardless of what the caller asks for. An MCP client is an LLM
context window; `SELECT * FROM stop_times` is 1M+ rows and would be a denial of service
against your own conversation.

- Default `limit` 50, hard cap 500 for record lists.
- `stl gtfs query` hard-caps at 1,000 rows and 256 KB serialized, whichever hits first.
- Every capped response sets `truncated: true` and says so in `warnings`.

### 2.5 `response_format` on every data command

`json` (default in MCP) and `markdown` (default in CLI). Markdown formatting lives in a
shared `core/render.py` so both consumers get the same text — otherwise the MCP server
returns raw JSON blobs where the CLI returns a readable table, and the LLM burns context
re-deriving what a `render_departures()` already knew.

### 2.6 No unbounded blocking

Anything that can exceed ~20 s gets a job handle:

- `snapshot fetch` of the 3.5 MB zip + 29 MB import
- `rt poll` (minutes by design)
- `history pull` (N archived snapshots)
- `gtfs validate` (JVM validator)

Pattern: `start` returns `{job_id, status: "running"}`; `stl jobs status <id>` /
`stl jobs result <id>` retrieve. CLI adds `--wait` to block with a progress bar. MCP never
blocks. Jobs are files in the store — no daemon.

### 2.7 Injectable clock, everywhere

Every time-dependent command takes `--as-of <iso8601>` (MCP: `as_of`). Default is real
now. This makes tests deterministic, makes support repro exact, and lets you ask "what
did the app show at 11:47 PM last Tuesday" without a time machine. Same discipline as the
seeded RNG in the sudoku engine.

### 2.8 Deterministic output

Fixed sort orders on every list. No dict iteration order dependence. Floats formatted to
fixed precision. Timestamps always ISO-8601 UTC with explicit local rendering alongside.
Two runs against the same snapshot produce byte-identical JSON — which is what makes
`oracle verify` a meaningful drift check and mirrors the deterministic-zip discipline
already in `lightbuilder`.

---

## 3. Storage layout

```
~/.local/share/stl-transit/           # or $STL_HOME
  snapshots/
    gtfs/<snapshot_id>/
      source.zip                      # verbatim bytes as fetched
      manifest.json                   # url, fetched_at, sha256, http headers, sizes
      feed.sqlite                     # built on demand, regenerable, gitignored
      extracted/                      # optional, on demand
    rt/<feed>/<snapshot_id>.pb + .json
    web/<page_key>/<snapshot_id>.{html,txt,json}
  archives/
    rt-recordings/<session_id>/       # rt record output, rotated
  jobs/<job_id>.json
  pins.json                           # snapshot_id → human name
  cache/http/                         # ETag / Last-Modified cache
```

`snapshot_id` = `<kind>-<UTC timestamp>-<sha256[:6]>`. Content-addressed suffix means
re-fetching an unchanged feed is detectable without a diff.

**Fixtures live in the tool repo, not here.** `oracle generate --out` writes into
`light-stltransit/tool/src/test/resources/fixtures/` and those files are committed. The
store is a cache; the fixtures are the product.

---

## 4. Configuration — `data/sources.toml`

Everything network-facing is declared, never hardcoded in code paths. Adding a source is
a config edit, which is what makes MCT (§D of the data doc) a one-line addition later.

```toml
[http]
user_agent = "stl-transit-dev/0.1 (+https://github.com/tyleryancey/light-stltransit)"
min_interval_seconds = 2.0
timeout_seconds = 30
max_retries = 3
respect_conditional = true

[feeds.metro_gtfs]
kind = "gtfs"
url = "https://www.metrostlouis.org/Transit/google_transit.zip"
aliases = ["http://metrostlouis.org/Transit/google_transit.zip"]
agency = "Metro Transit — St. Louis"
timezone = "America/Chicago"
terms_url = "https://www.metrostlouis.org/developer-resources/"

[feeds.metro_rt_trips]
kind = "gtfs-rt"
entity = "trip_updates"
url = "https://www.metrostlouis.org/RealTimeData/StlRealTimeTrips.pb"

[feeds.metro_rt_vehicles]
kind = "gtfs-rt"
entity = "vehicle_positions"
url = "https://www.metrostlouis.org/RealTimeData/StlRealTimeVehicles.pb"

[feeds.metro_rt_alerts]
kind = "gtfs-rt"
entity = "alerts"
url = "https://www.metrostlouis.org/RealTimeData/StlRealTimeAlerts.pb"

[mirrors.mobility_database]
feed_id = "mdb-190"
api = "https://api.mobilitydatabase.org"

[pages.fares]
url = "https://www.metrostlouis.org/fares-and-passes/"
extractor = "fare_table"
[pages.holidays]
url = "https://www.metrostlouis.org/holiday-schedules/"
extractor = "holiday_table"
[pages.purchase]
url = "https://www.metrostlouis.org/nextgenfare/"
extractor = "text"
[pages.schedule_changes]
url = "https://www.metrostlouis.org/upcoming-schedule-changes/"
extractor = "pick_id"
[pages.rider_alerts]
url = "https://www.metrostlouis.org/rider-alerts/"
extractor = "alert_list"
fetch_interval_hours = 24        # noarchive/nosnippet page — be conservative
```

---

## 5. Global conventions

**Flags present on every data command:**
`--json` / `--format {json,markdown,table,csv}` · `--limit` · `--offset` · `--snapshot`
(id or pin name; default = latest) · `--as-of` · `--quiet` · `--no-network` (fail rather
than fetch — proves a command works offline).

**Exit codes:** `0` ok · `1` unexpected error · `2` bad usage · `3` assertion violated ·
`4` data drift detected · `5` network unavailable and `--no-network` implied · `6` stale
feed (past `feed_end_date`).

Codes 3/4/6 exist so `assert run` and `web check` drop straight into a cron job or a
GitHub Action without parsing output.

**Error model** — structured, never a traceback:

```json
{ "ok": false,
  "error": { "code": "SNAPSHOT_NOT_FOUND",
             "message": "No GTFS snapshot matching 'baseline-2026-07'.",
             "remedy": "Run `stl snapshot list --kind gtfs`, or fetch one with `stl snapshot fetch metro_gtfs`.",
             "context": {"requested": "baseline-2026-07", "available": 4} } }
```

Every error carries a `remedy`. In MCP that's the difference between the model
recovering and the model giving up.

---

## 6. Command reference

Tier **0** = build first, unblocks the `[VERIFY]` list. **1** = needed before the tool
ships. **2** = maintenance-era. **MCP** = exposed as a tool in the eventual server
(§10 explains why the MCP surface is deliberately smaller than the CLI).

### 6.1 `snapshot` — acquisition and provenance

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `snapshot fetch <source>` | `--force`, `--wait` | snapshot_id, sha256, bytes, `unchanged: bool` (via ETag) | 0 | ✓ |
| `snapshot list` | `--kind`, `--pinned` | snapshots with dates, sizes, pins, feed validity | 0 | ✓ |
| `snapshot show <id>` | | full manifest incl. HTTP response headers | 0 | ✓ |
| `snapshot pin <id> --as <name>` | | pins.json entry | 0 | |
| `snapshot unpin <name>` | | | 1 | |
| `snapshot verify [<id>]` | `--all` | re-hash vs manifest, report corruption | 1 | |
| `snapshot import <path>` | `--kind`, `--source-url` | ingest an externally-fetched zip (Mobility Database archive, a file someone emailed you) | 1 | |
| `snapshot gc` | `--keep`, `--dry-run` | reclaimed bytes | 2 | |
| `snapshot sources` | | configured sources + last-fetch status + staleness | 0 | ✓ |

### 6.2 `gtfs` — static feed

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `gtfs files` | | per-file: name, bytes, rows, columns; plus **which optional GTFS files are absent** | 0 | ✓ |
| `gtfs schema <file>` | | columns, inferred types, null rate, distinct count, 3 sample values | 0 | ✓ |
| `gtfs stats` | | headline counts: agencies, routes by type, stops, trips, stop_times, shapes, service_ids, calendar_dates | 0 | ✓ |
| `gtfs features` | | which GTFS features are present, formatted to line up with Mobility Database's badges | 0 | ✓ |
| `gtfs coverage` | | feed_start/end, days remaining, per-service_id date spans, **days-to-expiry** | 0 | ✓ |
| `gtfs query <sql>` | `--limit`, `--explain` | rows + column names + row count. **Read-only enforced** (§9) | 0 | ✓ |
| `gtfs routes` | `--type`, `--search` | route_id, short/long name, type, color, direction count, trip count | 0 | ✓ |
| `gtfs route <route_id>` | `--service` | directions, headsigns, stop sequence per direction, first/last departure per service | 0 | ✓ |
| `gtfs stops` | `--search`, `--near`, `--route`, `--code` | matching stops with code, id, name, coords, routes served | 0 | ✓ |
| `gtfs stop <id-or-code>` | | one stop: both identifiers, parent_station, location_type, wheelchair, routes, coords | 0 | ✓ |
| `gtfs stop-resolve <number>` | | **the stop_id-vs-stop_code question**: what a number from a sign resolves to, in which field, and whether it's unique. Validate against Metro's own published example, 15111 | 0 | ✓ |
| `gtfs departures <stop> ` | `--date`, `--from`, `--window`, `--limit`, `--route` | **the core calculation**: ordered scheduled departures with route, headsign, trip_id, direction, and explicit service_id attribution | 0 | ✓ |
| `gtfs calendar` | `--date`, `--service` | active service_ids on a date, showing the `calendar.txt` base and each `calendar_dates.txt` exception applied, separately | 0 | ✓ |
| `gtfs service-day <timestamp>` | | which service date(s) a wall-clock instant belongs to, and the 24:xx+ offset — the off-by-one-day bug, made inspectable | 0 | ✓ |
| `gtfs late-night` | `--threshold 24:00:00` | every trip crossing the threshold; max time observed in the feed | 0 | ✓ |
| `gtfs holidays` | `--year` | dates where `calendar_dates` deviates from the weekday pattern, cross-checked against the published holiday table | 1 | ✓ |
| `gtfs transfers` | | `transfers.txt` if present; otherwise **derive** candidate transfers by stop proximity + shared-corridor heuristic, clearly labelled as derived | 1 | ✓ |
| `gtfs stations` | | `location_type=1` parents and their children; falls back to name-clustering if absent | 1 | ✓ |
| `gtfs validate` | `--wait` | MobilityData validator notices, grouped by severity | 1 | ✓ |
| `gtfs import` | `--force` | (re)build `feed.sqlite`; report timings and index sizes | 0 | |
| `gtfs profile` | | row counts, table sizes, index sizes, query timings — **feeds the on-device import budget** | 1 | ✓ |

### 6.3 `rt` — realtime

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `rt fetch [<feed>]` | `--all` | snapshot ids, sizes, header timestamps, HTTP headers | 0 | ✓ |
| `rt decode <snapshot>` | `--entity`, `--limit` | normalized JSON via official bindings | 0 | ✓ |
| `rt wire <snapshot>` | `--depth`, `--path` | **raw wire dump**: field number, wire type, length, hex bytes, nesting. The ground truth your Kotlin decoder is graded against | 0 | ✓ |
| `rt schema` | `--samples N` | **field-usage census** across N samples: which proto fields Metro actually populates, at what rate, with observed value ranges. Tells you exactly what to model in Kotlin and what to skip | 0 | ✓ |
| `rt health` | | staleness (now − header.timestamp), entity counts, content-type, ETag behaviour, whether the three feeds agree on time | 0 | ✓ |
| `rt poll` | `--seconds`, `--minutes`, `--wait` | cadence histogram, payload-size series, entity churn, **the measured refresh interval** | 0 | ✓ |
| `rt alerts` | `--route`, `--stop`, `--active-only` | decoded alerts with informed_entity resolved against a static snapshot; flags whether bodies carry detour detail or only headers | 0 | ✓ |
| `rt trip <trip_id>` | | TripUpdate joined to the scheduled trip; per-stop delay, schedule_relationship, propagation behaviour | 1 | ✓ |
| `rt stop-arrivals <stop>` | `--window` | **scheduled ⊕ realtime merged** — precisely what the app should render. The RT-aware sibling of `gtfs departures` | 0 | ✓ |
| `rt vehicles` | `--route`, `--trip` | positions, bearings, timestamps, occupancy if present | 1 | ✓ |
| `rt record` | `--interval`, `--duration`, `--session` | continuous capture to `archives/rt-recordings/` | 1 | |
| `rt replay <session>` | `--speed`, `--at` | emit recorded frames at a virtual clock — **how you test "bus is 8 minutes late" without waiting for a late bus** | 1 | ✓ |
| `rt join-rate` | `--samples` | % of RT trip_ids that resolve into the current static snapshot. A collapse here is the classic post-pick breakage | 1 | ✓ |

### 6.4 `oracle` — golden fixtures for the pure-JVM test gate

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `oracle cases` | | the case list from `oracle_cases.toml` with descriptions and what each pins down | 0 | ✓ |
| `oracle generate` | `--out`, `--case`, `--pretty` | writes fixture JSON; returns files written + hashes | 0 | ✓ |
| `oracle verify` | `--fixtures` | re-computes against the current snapshot and diffs vs committed goldens. **Exit 4 on drift** | 1 | ✓ |
| `oracle explain <case>` | | step-by-step derivation: service_ids resolved → trips selected → times normalized → sorted. The thing you read when Kotlin and Python disagree | 0 | ✓ |
| `oracle case-add` | interactive/args | append a case (e.g. after a bug — every bug becomes a fixture) | 1 | |

**Fixture format** (committed to the tool repo, read by `kotlin.test`):

```json
{ "case_id": "late_night_rollover",
  "generated_by": "stl 0.3.1",
  "snapshot_id": "gtfs-20260803T141200Z-a3f9c1",
  "feed_sha256": "…",
  "input":  { "stop_code": "15111", "as_of": "2026-08-04T23:50:00-05:00", "window_minutes": 90 },
  "expected": [
    { "route_short_name": "11", "headsign": "Chippewa Eastbound",
      "departure_local": "2026-08-05T00:12:00-05:00",
      "gtfs_time": "24:12:00", "service_date": "2026-08-04",
      "trip_id": "…", "direction_id": 0 }
  ] }
```

`service_date` and `gtfs_time` alongside the resolved local time is the whole point — a
Kotlin implementation that gets the instant right by luck and the service date wrong will
fail on the next test and you'll know immediately which of the two it botched.

**The case list — every one exists because it can break independently:**

- [ ] `weekday_midday` — the boring baseline
- [ ] `saturday` and `sunday` — separate `calendar.txt` patterns
- [ ] `late_night_rollover` — departures at 24:xx/25:xx, queried before and after midnight
- [ ] `first_departure_of_day` — window starting at 03:30
- [ ] `last_departure_of_day` — window where the answer is "nothing more today"
- [ ] `calendar_exception_added` — a `calendar_dates` type-1 date
- [ ] `calendar_exception_removed` — a type-2 date
- [ ] `holiday_bus_sunday` — Labor Day 2026-09-07: MetroBus should resolve to Sunday service
- [ ] `holiday_rail_weekend` — same date, MetroLink resolves to "Weekend" (a *different* concept)
- [ ] `holiday_normal_weekday` — Veterans Day 2026-11-11, which is **not** a service change
- [ ] `dst_spring_forward` — 2027-03-14, the 02:00–03:00 gap
- [ ] `dst_fall_back` — 2026-11-01, the repeated hour
- [ ] `multimodal_stop` — a stop served by both bus and rail
- [ ] `rail_station_both_lines` — a stop in the Red/Blue shared corridor
- [ ] `no_service_at_stop` — valid stop, zero departures in window: must return empty, not error
- [ ] `unknown_stop_code` — a number that resolves to nothing
- [ ] `feed_expired` — `as_of` past `feed_end_date`: must be a distinct, visible state
- [ ] `rt_delayed` — replayed RT, bus running late, merged output
- [ ] `rt_cancelled` — `schedule_relationship = CANCELED`
- [ ] `rt_absent` — RT fetch fails: must degrade to scheduled-only and *say so*

### 6.5 `diff` — snapshot comparison

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `diff summary <a> <b>` | | one-screen digest across all dimensions below | 1 | ✓ |
| `diff files <a> <b>` | | files added/removed, row-count deltas per file | 1 | ✓ |
| `diff routes <a> <b>` | | route_ids added/removed/renamed; short_name changes | 1 | ✓ |
| `diff stops <a> <b>` | `--moved-threshold-m` | stops added/removed/renamed/moved | 1 | ✓ |
| `diff stop-ids <a> <b>` | | **stop_id and stop_code survival across a pick.** The app's saved-stops feature lives or dies on this number | 1 | ✓ |
| `diff schedule <a> <b>` | `--stop`, `--route`, `--date` | timetable deltas for one stop or route | 2 | ✓ |
| `diff calendar <a> <b>` | | service_id churn and date-range shifts | 2 | ✓ |

### 6.6 `history` — longitudinal, via Mobility Database archives

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `history snapshots` | `--since` | archived versions available upstream, with dates and sizes | 1 | ✓ |
| `history pull` | `--count`, `--since`, `--wait` | fetch N archived snapshots into the local store | 1 | |
| `history stop-id-churn` | `--months` | **survival rate of stop_ids and stop_codes over time.** Answers "will a user's saved stop still work in six months?" with data instead of a guess | 1 | ✓ |
| `history pick-boundaries` | | inferred service-change dates from `feed_end_date` movement — gives you the real pick cadence | 1 | ✓ |
| `history feed-size` | | zipped/unzipped size and row counts over time — the on-device budget trendline | 2 | ✓ |

### 6.7 `web` — human-readable page capture

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `web capture [<page>]` | `--all` | fetch, extract main content, normalize (strip nonces/analytics/timestamps), hash, store | 1 | ✓ |
| `web list` | | configured pages, last capture, content hash, drift status | 1 | ✓ |
| `web diff <page> <a> <b>` | | normalized text diff between captures | 1 | ✓ |
| `web extract <page>` | | structured extraction: `fare_table` → rows; `holiday_table` → rows; `pick_id` → the `{pickId}` from the schedule-PDF URLs; `alert_list` → active alerts | 1 | ✓ |
| `web check` | | drift check across all pages. **Exit 4 on change** — cron this | 1 | ✓ |

Normalization matters more than it sounds: raw HTML changes on every request (analytics
IDs, CSRF nonces, rotating hero images). Hash the *extracted content*, or every check is a
false positive and you'll stop reading them.

### 6.8 `bundle` — artifacts the app ships

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `bundle fares` | `--format {json,kotlin}`, `--out` | fare table with an `as_of` date and source URL baked in | 1 | ✓ |
| `bundle holidays` | `--year`, `--out` | holiday → service-type mapping, bus and rail kept distinct | 1 | ✓ |
| `bundle stops-index` | `--out` | stop_code → (stop_id, name, routes) lookup for fast on-device resolution | 1 | ✓ |
| `bundle compact` | `--out`, `--strategy`, `--routes`, `--days` | build the pruned on-device dataset; report before/after sizes and every pruning decision taken | 1 | ✓ |
| `bundle size-report` | | size budget breakdown by table, with the compression strategy's contribution isolated | 1 | ✓ |
| `bundle manifest` | | what was generated, from which snapshot, with hashes — for the tool's README and vetting defense | 1 | ✓ |

### 6.9 `support` — reproduce reported problems

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `support repro` | `--stop`, `--at`, `--rt-session`, `--snapshot` | exactly what the app *should* have shown, given a stop, an instant, and optionally a recorded RT frame | 1 | ✓ |
| `support explain-empty` | `--stop`, `--at` | **why** nothing showed: no service that day? outside the window? stop retired at the last pick? feed past `feed_end_date`? Walks the decision tree and names the branch | 1 | ✓ |
| `support diff-device` | `--expected-json`, `--actual-json` | diff an on-device capture against the oracle | 2 | ✓ |
| `support bundle` | `--out` | zip of snapshot ids, RT sample, config, tool versions, recent assertion results — attach to a GitHub issue | 2 | |

### 6.10 `assert` — the assumption regression suite

The app depends on facts about the feed that Metro never promised. Encode each one, run
them on a schedule, and find out from a cron job instead of from a user.

| Command | Args | Returns | T | MCP |
|---|---|---|---|---|
| `assert list` | | each assumption: id, what it checks, what breaks if it fails | 1 | ✓ |
| `assert run` | `--only`, `--baseline` | pass/fail per assumption. **Exit 3 on violation** | 1 | ✓ |
| `assert explain <id>` | | why it matters, which code path depends on it, remediation | 1 | ✓ |

**The assumptions** (each becomes a row in `assertions.toml`):

| id | Check | Breaks |
|---|---|---|
| `stop_code_present` | `stop_code` populated on ≥99% of bus stops | The entire Stop-ID entry UX |
| `stop_code_unique` | no duplicate `stop_code` | Wrong stop silently returned |
| `stop_code_format` | matches the printed-sign format (numeric, 4–5 digits) | Input validation and keypad design |
| `stop_ids_stable` | ≥98% of stop_codes survive vs. pinned baseline | Users' saved stops |
| `feed_not_expiring` | ≥7 days remain before `feed_end_date` | Silent blank screens |
| `no_frequencies_file` | `frequencies.txt` absent | Headway-based trips need a whole second code path |
| `max_time_bounded` | no `stop_times` value ≥ 28:00:00 | Service-day arithmetic assumptions |
| `timezone_unchanged` | agency timezone is `America/Chicago` | All time math |
| `rail_route_ids_stable` | rail `route_id`s match baseline | Any rail special-casing |
| `rt_fresh` | all three RT feeds within N seconds of now | Live arrivals |
| `rt_join_rate` | ≥95% of RT trip_ids resolve into static | Post-pick RT/static desync |
| `rt_wire_shape` | no unmodeled proto field appears above X% frequency | The hand-rolled/kotlinx decoder |
| `fares_unchanged` | fare page content hash matches | Bundled fare table is now lying to users |
| `holidays_unchanged` | holiday table hash matches | Bundled holiday mapping |
| `terms_unchanged` | developer-resources page hash matches | Your redistribution rights |
| `no_fare_files` | `fare_*.txt` still absent | *Opportunity*, not breakage — you'd want to know |

That last one is the pattern worth generalizing: an assertion suite should also watch for
things getting *better*, not just worse.

### 6.11 `report`, `jobs`, `doctor`, `config`

| Command | Returns | T | MCP |
|---|---|---|---|
| `report brief` | one-screen state of the feed: version, expiry, assertion status, RT health, drift | 1 | ✓ |
| `report handoff` | markdown block for pasting into `CLAUDE.md` — verified facts with citations and dates, in the house format | 1 | ✓ |
| `report changelog --since <pin>` | everything that changed since a pinned baseline | 2 | ✓ |
| `jobs list` / `status <id>` / `result <id>` / `cancel <id>` | job control | 0 | ✓ |
| `doctor` | network reachability, store integrity, sqlite/validator availability, config validity, disk use | 0 | ✓ |
| `config show` / `config path` | resolved config with sources | 0 | |

---

## 7. Stack

| Concern | Choice | Why |
|---|---|---|
| Python | 3.12+ | `datetime` improvements; `zoneinfo` stdlib |
| CLI | **Typer** | Derives its interface from type hints and docstrings — the *same* inputs FastMCP uses. Argparse would mean writing the interface twice |
| Models | **Pydantic v2** | FastMCP's native validation layer. Write the models once, get CLI validation and MCP `inputSchema` free |
| HTTP | **httpx** | Sync + async from one API; conditional requests are straightforward |
| GTFS parse | stdlib `zipfile` + `csv` | No dependency; also keeps you honest about the quoting edge cases the Kotlin side will hit |
| Store/query | stdlib `sqlite3` | Read-only URI mode is the security boundary (§9) |
| GTFS-RT | `gtfs-realtime-bindings` **+ a hand-rolled wire reader** | The official bindings give correctness; the hand-rolled reader gives the byte-level ground truth your Kotlin decoder needs. Both, deliberately |
| Human output | **rich** | CLI layer only. Never imported by `core/` |
| Tests | **pytest** + `pytest-httpx` | |
| Env | **uv** | |
| Package | `pyproject.toml`, scripts `stl` and later `stl-mcp` | |

**Deliberately not used:** `pandas` (a 40 MB dependency to do `GROUP BY`, which SQLite
already does), `partridge`/`gtfs-kit` (they'd hide exactly the parsing decisions you need
to see in order to mirror them in Kotlin), any ORM.

---

## 8. Testing the CLI itself

The oracle can't be the only untested thing in the chain.

- `core/` is unit-tested against **hand-built miniature GTFS feeds** — 3 stops, 2 routes,
  a calendar exception, a 25:xx trip. Small enough to reason about completely, which is
  the only way to be sure the calendar math is right rather than merely self-consistent.
- Every oracle case gets a mirrored Python test asserting the same expected values, so a
  Kotlin/Python disagreement isolates immediately to one side.
- HTTP is always mocked in tests. One opt-in `--integration` suite hits the live feeds;
  it is allowed to fail (upstream is not your dependency to control).
- Golden-output tests on the markdown renderers so MCP text output doesn't silently drift.
- `rt wire` is tested against a hand-encoded protobuf message with known bytes.

---

## 9. Security, politeness, and the SQL escape hatch

**`gtfs query` executes model-authored SQL.** This is the single highest-leverage tool in
the MCP surface and the only one with real blast radius. Constrain it at the connection,
not with a regex:

- Open with `sqlite3.connect("file:...?mode=ro&immutable=1", uri=True)` — writes are
  impossible at the driver level, not merely discouraged.
- `set_authorizer` denying `ATTACH`, `PRAGMA`, and any function that touches the filesystem.
- `set_progress_handler` enforcing a wall-clock timeout (~5 s) so no runaway scan.
- Hard row cap (1,000) and serialized-size cap (256 KB), both reported in the result.
- Never interpolate parameters into SQL strings anywhere else in the codebase.

**Politeness toward Metro** — you are an unpaid guest on a public agency's infrastructure,
and this relationship has to survive past submission:

- Identifying User-Agent with a repo URL. Metro explicitly invites developer contact; be
  identifiable when they look at their logs.
- Conditional requests (`If-None-Match`, `If-Modified-Since`) on everything. An unchanged
  feed should cost a 304.
- Minimum interval between requests to the same host; exponential backoff on 5xx.
- `rt poll` defaults to a conservative interval and requires an explicit `--minutes`.
- HTML pages capped at one fetch per day. The Rider Alerts page carries
  `noarchive, nosnippet` — treat it as capture-for-reference, never as a live dependency.
- No secrets anywhere; nothing here needs auth, which also makes MCP config trivial.

---

## 10. MCP migration

When the time comes, `mcp/server.py` is a registration file and nothing more:

```python
from mcp.server.fastmcp import FastMCP
from stl_transit.core.gtfs import departures as core_departures
from stl_transit.core.models import DeparturesInput, DeparturesResult

mcp = FastMCP("stl_transit_mcp")

@mcp.tool(
    name="stl_gtfs_departures",
    annotations={"readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": False},
)
async def stl_gtfs_departures(params: DeparturesInput) -> DeparturesResult:
    """Scheduled departures at a St. Louis Metro stop for a given date and time window.

    Resolves the stop by stop_code (the number printed on the bus stop sign) or stop_id,
    resolves active service_ids for the date through calendar.txt and calendar_dates.txt,
    and returns departures sorted by time. Handles GTFS times past 24:00:00 by attributing
    them to the correct service date. Schedule only — use stl_rt_stop_arrivals for
    realtime-adjusted times.
    """
    return await core_departures(params)
```

If §2 was honoured, every tool is this shape and nothing in `core/` changes.

**Expose a curated subset, not all ~70 commands.** A large tool list burns context and
degrades selection accuracy. Target ~20–25 MCP tools:

- The workflow tools that answer questions in one call: `gtfs_departures`,
  `gtfs_stop_resolve`, `gtfs_coverage`, `rt_stop_arrivals`, `rt_health`, `rt_schema`,
  `assert_run`, `diff_summary`, `report_brief`, `support_explain_empty`, `oracle_explain`.
- The exploration primitives: `gtfs_files`, `gtfs_stats`, `gtfs_routes`, `gtfs_stops`,
  `snapshot_list`, `snapshot_fetch`, `jobs_status`.
- **`gtfs_query`** as the escape hatch — it's what lets the other 45 commands stay
  CLI-only without the MCP surface feeling crippled.

Transport: **stdio**, local only. No reason for HTTP; no auth to manage.

**Evaluations** (mcp-builder Phase 4) write themselves here, because the answers are
stable and verifiable against a pinned snapshot: "Which MetroLink route_id serves both
Lambert Airport Terminal 1 and Shiloh-Scott?" · "How many trips in the pinned snapshot
have a departure_time at or after 25:00:00?" · "What is the latest departure from stop
15111 on a Sunday?" Pin the snapshot in the eval fixture and the answers never rot.

---

## 11. Build order

**Phase 0 — skeleton and provenance** (unblocks everything)
- [ ] `pyproject.toml`, `uv` env, package layout per §2.1, `stl` entry point
- [ ] `core/models.py`: `Result`, `ListResult`, `Provenance`, error model
- [ ] `io/http.py` with UA, conditional requests, backoff, rate limit
- [ ] `io/store.py`: content-addressed snapshots, manifests, pins
- [ ] `io/clock.py`: injectable now, `--as-of` plumbed globally
- [ ] `snapshot fetch|list|show|pin|sources`, `doctor`, `jobs`
- [ ] `--format` dispatch and `core/render.py`

**Phase 1 — settle the `[VERIFY]` list** (the whole reason this exists)
- [ ] GTFS→SQLite import with the hot-path indices; `gtfs import|files|schema|stats|features`
- [ ] `gtfs query` with the §9 read-only enforcement — **do this before, not after**
- [ ] `gtfs coverage`, `routes`, `route`, `stops`, `stop`
- [ ] `gtfs stop-resolve` → **answer the stop_id-vs-stop_code question and write it down**
- [ ] `gtfs calendar`, `service-day`, `late-night`
- [ ] `gtfs departures` — the core calculation, with its own unit tests on miniature feeds
- [ ] `rt fetch|decode|wire|schema|health|poll` → **answer the protobuf question**
- [ ] `rt stop-arrivals`
- [ ] Update the data-source doc: every `[VERIFY]` resolved, with dates and snapshot ids

**Phase 2 — the oracle** (gate: nothing goes into Kotlin before this is green)
- [ ] `oracle_cases.toml` with the full §6.4 case list
- [ ] `oracle generate|explain`, fixture format frozen
- [ ] Python-side mirror tests for every case
- [ ] Commit fixtures into the tool repo; wire `kotlin.test` to read them
- [ ] `oracle verify` + exit code 4

**Phase 3 — artifacts** (as the Kotlin tool takes shape)
- [ ] `bundle fares|holidays|stops-index|manifest`
- [ ] `bundle compact` + `size-report` → settles the 29 MB question empirically
- [ ] `gtfs profile` → the on-device import budget

**Phase 4 — surveillance** (before submission, ideally in CI)
- [ ] `web capture|extract|diff|check`
- [ ] `diff` group, `history` group
- [ ] `assert` group with the full §6.10 table
- [ ] GitHub Action: weekly `assert run` + `web check` + `oracle verify`, opening an issue
      on exit 3/4/6

**Phase 5 — support and MCP**
- [ ] `rt record|replay`, `support` group
- [ ] `report brief|handoff|changelog`
- [ ] `mcp/server.py`, curated tool subset, stdio, MCP Inspector smoke test
- [ ] 10 evaluation Q/A pairs against a pinned snapshot

---

## 12. Decisions that are yours, not the spec's

1. **Repo placement.** Its own repo, or a `tools/` directory inside `light-stltransit`?
   Same-repo keeps fixtures and their generator in one commit — appealing, since a fixture
   without its generator is unreproducible. But it puts Python in a repo a Light reviewer
   will read as a Kotlin tool. A `dev/` directory with a README explaining it's build-time
   tooling probably resolves that, but it is a vetting-surface judgement call.
2. **Whether `bundle compact` output is committed.** Committing the compact DB makes tool
   builds reproducible without Python; regenerating keeps the repo small. Depends on how
   small `compact` actually gets — measure first.
3. **How far to take `rt replay`.** A file emitter is cheap. A local HTTP server that
   mimics Metro's endpoints so the *actual APK* can be pointed at it is much more useful
   for on-device testing, and meaningfully more work.
4. **Illinois scope.** MCT is a config addition once its feed URL is known — but only if
   Illinois coverage is in the product at all. That's a product decision that changes the
   assertion suite and the compact-bundle size.
5. **Whether `assert run` in CI opens issues or just fails.** Issue-opening is better for
   a tool you'll maintain for years and check on rarely; noisier in the short term.
