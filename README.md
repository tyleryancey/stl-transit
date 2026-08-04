# stl-transit

Developer tooling for the Light Phone 3 St. Louis transit tool: GTFS and
GTFS-Realtime inspection, golden-fixture generation for the Kotlin test gate,
and feed surveillance.

**Never ships in the APK.** This is build-time tooling and Light never vets it.
The shipped tool is a separate Kotlin repo, and unlike this one it cannot use
Metro's trademarks.

Ships as one thing usable two ways:

- **CLI** — `stl ...`, 64 commands, for interactive work and cron/CI.
- **MCP server** — `stl-mcp` on stdio, 45 tools, for Claude Cowork / Desktop / Code.

Both are thin shells over `stl_transit.core.service`. See `SPEC.md` for the full
design and the CLI-to-MCP contract; `tests/test_wiring.py` enforces it.

---

## Install

```bash
git clone https://github.com/tyleryancey/stl-transit
cd stl-transit
python3 -m venv .venv
.venv/bin/pip install -e .

.venv/bin/stl doctor          # sanity check
```

Requires Python 3.12+. No API keys — every source is public and unauthenticated,
which is what keeps MCP configuration trivial.

## First run

```bash
.venv/bin/stl snapshot fetch metro_gtfs      # ~3.5 MB zip, expands to ~29 MB
.venv/bin/stl gtfs import                    # build the SQLite index
.venv/bin/stl report brief                   # is anything wrong right now?
.venv/bin/stl gtfs stop-resolve              # answers the stop_code vs stop_id question
.venv/bin/stl rt fetch --entity trip_updates
.venv/bin/stl assert run                     # the assumptions the app depends on
```

Snapshots land in `~/.local/share/stl-transit` (override with `$STL_HOME`).

## Attaching to Claude Cowork / Desktop

Add to your MCP config (Cowork: Settings → Connectors → Add local server;
Desktop: `claude_desktop_config.json`). Use **absolute paths**:

```json
{
  "mcpServers": {
    "stl-transit": {
      "command": "/absolute/path/to/stl-transit/.venv/bin/stl-mcp",
      "env": {
        "STL_HOME": "/absolute/path/to/stl-transit"
      }
    }
  }
}
```

Verify before wiring it up:

```bash
.venv/bin/stl-mcp    # should sit silently waiting on stdio; Ctrl-C to exit
```

Pointing `STL_HOME` at a directory inside the repo (gitignored) keeps every
snapshot the agent touches next to the code, which makes a Cowork session
reproducible after the fact.

### What the 45 tools cover

| Group | Tools |
|---|---|
| Orientation | `stl_doctor`, `stl_snapshot_sources`, `_list`, `_fetch` |
| Feed shape | `stl_gtfs_coverage`, `_files`, `_stats`, `_features`, `_schema` |
| Entities | `stl_gtfs_routes`, `_route`, `_stops`, `_stop`, `_stop_resolve` |
| Schedule | `stl_gtfs_departures`, `_calendar`, `_service_day`, `_late_night` |
| Realtime | `stl_rt_health`, `_decode`, `_wire`, `_schema_census`, `_reference`, `_stop_arrivals` |
| Assumptions | `stl_assert_list`, `_run`, `_explain` |
| Drift | `stl_diff_summary`, `_stop_ids` |
| Web sources | `stl_web_list`, `_capture`, `_extract`, `_check` |
| Ship artifacts | `stl_bundle_fares`, `_holidays`, `_size_report` |
| Digests | `stl_report_brief`, `_handoff` |
| Oracle | `stl_oracle_cases`, `_generate`, `_verify` |
| Support | `stl_support_explain_empty`, `_repro`, `_diff_device` |
| Escape hatch | `stl_gtfs_query` |

`stl_gtfs_query` is why the surface stays at 45 rather than 64: anything the
named tools do not cover is expressible as read-only SQL. It is also the only
tool with real blast radius, so it is constrained at the SQLite driver —
`mode=ro`, an authorizer denying `ATTACH`/`PRAGMA`/every write, a wall-clock
timeout, and row/byte caps. `DROP TABLE routes` comes back as a structured
`UNSAFE_QUERY` error with a remedy, not a stack trace.

> **A note on surface size.** `SPEC.md` §10 targets 20–25 MCP tools, arguing
> from context-window economics. This server exposes 45, which is a deliberate
> departure: every group added since answers a question no other tool can, and
> the ones that would genuinely flood a context (`bundle stops-index` at 646 KB,
> `bundle compact`, `support bundle`) are CLI-only for exactly that reason. The
> mitigation for a larger surface is the question-to-tool index at the top of
> the server instructions, so a cold-start model does not have to read 45
> descriptions to find its entry point.

## The parts that carry the most weight

**`core/gtfs/calendar.py` + `departures.py`** — the reference implementation the
Kotlin engine is graded against. GTFS measures times from noon-minus-twelve-hours,
not local midnight; a 00:12 departure is usually encoded `24:12:00` on the
*previous* service date; `pickup_type=1` means a rider cannot board. Every
result carries `service_date` and `gtfs_time` beside the resolved local time, so
an implementation that gets the instant right by luck and the service date wrong
fails visibly instead of quietly.

The time arithmetic is done in UTC throughout, and that is not stylistic.
Subtracting or adding a `timedelta` on a zone-aware datetime is *wall-clock*
arithmetic in Python — and in `java.time.LocalDateTime` — so `noon - 12h`
collapses back to local midnight and every departure on the two DST transition
days each year shifts by an hour. Port the UTC conversion, not just the formula.

**`core/rt/wire.py` + `schema.py`** — a hand-rolled protobuf reader, deliberately
not `gtfs-realtime-bindings`. The LP3 has no protobuf runtime on the Light SDK
dependency allow-list, so the on-device decoder is either
`kotlinx-serialization-protobuf` or hand-written. `schema.py` is the porting
table and it marks signedness explicitly: `delay` is `int32`, and a decoder that
reads it unsigned turns "three minutes early" into 18446744073709551436. About
31% of the delay values in Metro's feed are negative, so this is the common case,
not the edge case.

**`core/assertions/`** — 16 things the app depends on that Metro never promised:
that `stop_code` is populated and unique, that no trip runs past 28:00, that the
agency timezone does not move, that ≥95% of realtime trip ids resolve into the
static feed. Every result reports the **observed value** beside the threshold,
because "coverage 0.982, threshold 0.99" is actionable and "FAIL" is not. Three
outcomes, not two: `skip` means the measurement could not be taken, and a
stability check with no baseline has not been performed.

**`core/oracle.py`** — 19 golden-fixture cases, each present because it can break
independently: DST spring-forward and fall-back, 24:xx rollover queried from both
sides of midnight, Labor Day (MetroBus → Sunday, MetroLink → Weekend — different
concepts), an ordinary Monday that is *not* a holiday, expired feed as a distinct
state, realtime absent. A case that legitimately raises is a first-class
expectation compared on error *type*, so it does not read as permanent drift.

## Coverage

`metro_gtfs` covers MetroBus in **both Missouri and Illinois** — St. Clair County
service is operated by Metro under contract and lives in this feed — plus
MetroLink. Two Illinois-side sources are configured but **blocked on unresolved
URLs**; `stl snapshot sources` reports them with discovery notes:

- **`mct_gtfs`** — Madison County Transit, own buses, not in Metro's feed.
  Resolve via Transitland (`f-madison~county~transit~il~us`) or Mobility Database.
- **`loop_trolley`** — seasonal streetcar. Check whether its trips are already
  inside `metro_gtfs` before adding it separately.

Resolving either is a one-line edit to `src/stl_transit/data/sources.toml`.

## Exit codes

`0` ok · `1` error · `2` usage · `3` assertion violated · `4` drift detected ·
`5` network unavailable · `6` feed expired. Codes 3, 4 and 6 exist so
`stl assert run`, `stl web check`, `stl oracle verify` and `stl report brief`
drop into cron or a GitHub Action without anyone parsing output.

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q     # 403 tests, no network
```

Everything runs against miniature synthetic feeds in `tests/fixtures.py` — three
stops, two routes, a calendar exception, a 24:12 rollover trip — small enough to
reason about completely, which is the only way to be sure the calendar math is
right rather than merely self-consistent. Protobuf tests run against
hand-encoded bytes with known field numbers.

Two suites go further than unit coverage:

- `tests/test_wiring.py` enforces the CLI-to-MCP contract itself: tool naming,
  the four annotations on every tool, that `core/` never imports `cli/` or
  `mcp/` and never prints, and that no MCP tool exists without a CLI equivalent
  to debug it from.
- `scripts/verify_fixes.py` re-runs the 2026-08-03 audit's failures against a
  real snapshot. The synthetic feeds have no negative RT delays and no
  489k-row table to time out on, so these checks cannot be unit tests.

## Evaluations

`evaluations/` holds 10 question/answer pairs pinned to a specific snapshot,
each requiring several tool calls to answer. `evaluations/verify_answers.py`
recomputes all ten by two independent routes and tells you which have moved when
the pinned snapshot is eventually replaced.

## Not yet built

Per `SPEC.md`: job handles for long operations (`snapshot fetch`, `rt poll`,
`history pull`, `gtfs validate` still block rather than returning a job id), the
`history` group over Mobility Database archives, and `rt record` / `rt replay`.
`rt replay` is a file emitter when built; the local HTTP server that mimics
Metro's endpoints for on-device testing is backlogged.

## Terms

Metro's data is licensed non-exclusively, limited, and **revocably**, with no
trademark use permitted. Full terms:
<https://www.metrostlouis.org/developer-resources/>. Be a good guest — the HTTP
layer sends an identifying User-Agent, uses conditional requests, rate-limits
per host, and caps web-page fetches at one per day. `stl assert run` watches the
terms page for changes, because the redistribution right this project rests on
is one Metro can withdraw.
