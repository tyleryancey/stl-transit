# stl-transit MCP server audit

> **STATUS: RESOLVED, 2026-08-03.** Every bug below is fixed. Kept in the repo
> because the findings are the reason several odd-looking pieces of code exist,
> and a fix without its reason gets refactored back out by the next reader.
>
> Where each one now lives:
>
> | # | Bug | Fixed in | Guarded by |
> |---|---|---|---|
> | 1 | Negative RT delays decode as 2^64−n | `core/rt/wire.py` `as_int`/`as_uint`, `core/rt/schema.py` signedness | `test_fixes.py` (5 tests), `scripts/verify_fixes.py` |
> | 2 | `service_day_start` is local midnight on DST days | `core/gtfs/calendar.py` | `test_calendar.py`, `test_fixes.py` |
> | 3 | `oracle.verify` ignores `expected_error` | `core/oracle.py` | `test_fixes.py` |
> | 4 | Query "timeout" was an opcode counter; error codes collided | `io/db.py`, `errors.py` (`QueryTimeout`, `QueryFailed`) | `scripts/verify_fixes.py` |
> | 5 | `gtfs_stops` fabricated its `total` | `core/gtfs/entities.py` | `scripts/verify_fixes.py` |
> | 6 | Census invented unmodelled fields from strings | `core/rt/wire.py` `walk`, `schema.is_scalar_path` | `test_fixes.py` |
> | 7 | `stop_sequence` dropped, disabling half the RT matcher | `core/gtfs/departures.py` | `test_fixes.py` |
> | 8 | `truncated` reported the request, not the data | `core/models.py` | `test_fixes.py` |
> | 9 | LIKE wildcards unescaped | `core/gtfs/entities.py` `escape_like` | `test_fixes.py` |
> | 10 | Provenance used the pre-conversion date | `core/service.py` | — |
> | 11 | `match_rate` denominator was the page | `core/rt/merge.py` | `test_fixes.py` |
>
> A twelfth instance of bug 2's wall-clock-subtraction trap was later found in
> `service.gtfs_service_day` while writing the evaluations, and fixed the same
> way. That is the pattern worth remembering: this class of bug hides on 363
> days of the year.

**Date:** 2026-08-03 · **Method:** every core function behind the 29 MCP tools was
imported and called directly (`from stl_transit.core import service, oracle`),
exactly as the `_call()` wrappers in `src/stl_transit/mcp/server.py` do.

- Store: `STL_HOME=/Users/tyleryancey/Documents/stl-transit`
- GTFS snapshot: `gtfs-20260803T190539Z-f2d721` (metro_gtfs, 3.72 MB, fetched 2026-08-03T19:05:39Z)
- RT snapshot: `rt-20260803T190604Z-5ceebf` (metro_rt_trips, 180 535 B, 144 entities)
- Only one RT feed is stored; `metro_rt_vehicles` and `metro_rt_alerts` have never been fetched.
- Existing test suite: **54 passed** — none of the bugs below are covered by it.

## Tool results

| Tool (core fn) | Status | Note |
|---|---|---|
| `stl_doctor` (`service.doctor`) | OK | py 3.14.6, 2 snapshots, 59.4 MB; warns on 2 unresolved sources |
| `stl_snapshot_sources` (`service.snapshot_sources`) | OK | 5 sources; `mct_gtfs` + `loop_trolley` correctly blocked |
| `stl_snapshot_list` (`service.snapshot_list`) | OK | 2 snapshots, newest-first, pins empty |
| `stl_snapshot_fetch` (`service.snapshot_fetch`) | SKIPPED | network; not exercised by instruction |
| `stl_gtfs_coverage` (`service.gtfs_coverage`) | OK | 2026-07-30 → 2026-08-30, 27 days remaining |
| `stl_gtfs_files` (`service.gtfs_files`) | OK | file inventory + absent-optional list returned |
| `stl_gtfs_stats` (`service.gtfs_stats`) | OK | 5 118 stops, 9 577 trips, 489 011 stop_times, 62 routes |
| `stl_gtfs_features` (`service.gtfs_features`) | OK | 14 feature badges evaluated |
| `stl_gtfs_schema` (`service.gtfs_schema`, `table="stops"`) | OK | per-column null rate / distinct / samples |
| `stl_gtfs_query` (`service.gtfs_query`) | **SUSPECT** | write-rejection works, but the "wall-clock timeout" is an opcode counter and kills legitimate 0.34 s aggregates — bug #4 |
| `stl_gtfs_routes` (`service.gtfs_routes`) | OK | 62 routes; `truncated` flag is misleading — bug #8 |
| `stl_gtfs_route` (`service.gtfs_route`, `19731B`) | OK | directions, headsigns, per-service trip counts |
| `stl_gtfs_stops` (`service.gtfs_stops`, `search="Union"`) | **SUSPECT** | 76 hits OK, but unfiltered listing reports a fabricated `total` — bug #5; LIKE wildcards unescaped — bug #9 |
| `stl_gtfs_stop` (`service.gtfs_stop`, `10626`) | OK | resolves via stop_code, 1 194 stop_times rows |
| `stl_gtfs_stop_resolve` (`service.gtfs_stop_resolve`) | OK | verdict `stop_code`, and it holds — see Facts Q1 |
| `stl_gtfs_departures` (`service.gtfs_departures`) | OK | 18 departures at a busy stop, weekday midday; DST latent — bug #2 |
| `stl_gtfs_calendar` (`service.gtfs_calendar`, `2026-08-05`) | OK | `319-T1`, `325-B1` active; exceptions shown unmerged |
| `stl_gtfs_service_day` (`service.gtfs_service_day`) | **SUSPECT** | correct off-DST, but returns local midnight on both transition days — bug #2 |
| `stl_gtfs_late_night` (`service.gtfs_late_night`) | OK | 14 029 rows ≥ 24:00:00, max 25:37:00, 475 trips |
| `stl_rt_health` (`service.rt_health`) | OK | trip_updates present (144 entities, 8 809 s old → stale); other two report `available:false` + remedy |
| `stl_rt_decode` (`service.rt_decode`) | **SUSPECT** | decodes, but every negative delay comes out as 2^64−n — bug #1 |
| `stl_rt_wire` (`service.rt_wire`) | OK | 180 535 B, 145 top-level fields, named paths resolved |
| `stl_rt_schema_census` (`service.rt_schema_census`) | **SUSPECT** | 24 paths, 3 "unmodelled" — all three are phantoms — bug #6 |
| `stl_rt_reference` (`service.rt_reference`) | OK | 64 field rows + enum maps |
| `stl_rt_stop_arrivals` (`service.rt_stop_arrivals`) | **ERROR** | `OverflowError` on 4 of the 5 busiest RT-covered stops — bug #1 |
| `stl_oracle_cases` (`oracle.list_cases`) | OK | 19 cases with failure modes |
| `stl_oracle_generate` (`oracle.generate`) | OK | 2 fixtures written, 17 skipped for lack of bindings |
| `stl_oracle_verify` (`oracle.verify`) | **ERROR** | reports permanent false drift for any error-expecting case — bug #3 |
| `stl_support_explain_empty` (`service.support_explain_empty`) | OK | 5-check decision tree, verdict `WINDOW_TOO_NARROW` |

**Totals:** 22 OK · 5 SUSPECT · 2 ERROR · 1 skipped.

## Bugs found

### 1. Negative GTFS-RT delays decode as 2^64 − n, and crash `rt_stop_arrivals`
`src/stl_transit/core/rt/wire.py:125-132` (`as_int`)

```python
if f.wire_type == WIRE_VARINT and f.varint is not None:
    return f.varint          # <-- no two's-complement fixup
```

Protobuf encodes negative `int32`/`int64` as the full 64-bit two's-complement
varint. `StopTimeEvent.delay` (`schema.py:61`) is declared `"int"`, so a bus
running 3 minutes early decodes as `18446744073709551436` instead of `-180`.
In the stored snapshot **2 207 of 7 172 delay values (30.8 %) are negative** and
all of them are wrong.

The consequence is a hard crash, not just a bad number.
`src/stl_transit/core/rt/merge.py:87-91` does
`predicted = base.timestamp() + delay` then `datetime.fromtimestamp(predicted, …)`,
which raises `OverflowError: timestamp out of range for platform time_t`.
Measured on the real snapshot at `2026-08-03T14:10:00`, over the five busiest
stops the RT feed covers:

| stop | result |
|---|---|
| 7855 14TH ST @ MARKET SB | OK — 7/10 matched, delays +60…+240 s |
| 13330 NORTH HANLEY TRANSIT CENTER | **OverflowError** |
| 14792 CENTRAL WEST END TRANSIT CENTER | **OverflowError** |
| 15073 RIVERVIEW @ BROADWAY EB | **OverflowError** |
| 3693 PERSHALL @ METRO PLAZA EB | **OverflowError** |

Fix: `v = f.varint; return v - (1 << 64) if v >= (1 << 63) else v`.

### 2. `service_day_start` is local midnight, not noon-minus-12h, on DST days
`src/stl_transit/core/gtfs/calendar.py:46-55`, and by extension `absolute_time` at `:58-60`

```python
noon = datetime(y, m, d, 12, 0, 0, tzinfo=tz)
return noon - timedelta(hours=12)
```

Subtracting a `timedelta` from a `ZoneInfo`-aware datetime is *wall-clock*
arithmetic in Python, not absolute-instant arithmetic. The offset is re-derived
after the subtraction, so the result is always local 00:00 — precisely the
behaviour the docstring says to avoid ("The GTFS spec defines this as noon minus
twelve hours, NOT local midnight… Port this exactly").

Measured:

| service_date | code returns | true noon-minus-12h | Δ |
|---|---|---|---|
| 2026-03-08 (spring forward) | `2026-03-08T00:00:00-06:00` | `2026-03-07T23:00:00-06:00` | +3600 s |
| 2026-11-01 (fall back) | `2026-11-01T00:00:00-05:00` | `2026-11-01T01:00:00-05:00` | −3600 s |
| 2026-08-05 (normal) | `2026-08-05T00:00:00-05:00` | same | 0 |

Downstream, `absolute_time(date(2026,3,7), 26:30:00)` returns
`2026-03-08T02:30:00-06:00` where the spec instant is `2026-03-08T03:30:00-05:00`
— a one-hour error on the exact case `oracle.CASES["dst_spring_forward"]` exists
to pin down. `stl_gtfs_service_day` inherits it. Not observable in the current
snapshot (2026-07-30 → 2026-08-30 contains no transition), which is why the test
suite passes.

Fix: `return (noon.astimezone(timezone.utc) - timedelta(hours=12)).astimezone(tz)`,
and add gtfs_seconds in UTC before converting back.

### 3. `oracle.verify` ignores `expected_error`, so error cases drift forever
`src/stl_transit/core/oracle.py:143-154`

`generate` records `expected_error` (`:113`) and writes `expected: []` for a case
that legitimately raises. `verify` never reads `expected_error`; it catches the
exception into `actual = [{"error": str(exc)}]` (`:152-153`) and compares that to
`golden["expected"]` (`:154`). A list of one is never equal to a list of zero.

Reproduced against the *same snapshot the fixture was generated from*:
`unknown_stop_code` → `matches: false, expected_count: 0, actual_count: 1`,
`drifted: 1`, `ok: false`. Six of the 19 declared cases
(`unknown_stop_code`, `no_service_at_stop`, `feed_expired`, …) are structurally
un-verifiable. This makes the drift check — described as the product — cry wolf
on every scheduled run.

Related, same file: `:148-150` filters `actual` by the keys of
`golden["expected"][0]`, so if the golden list is empty but the fresh result is
not, the comparison silently compares full items against `[]`; and `:102` swallows
every exception type into a fixture field with no re-raise.

### 4. The `gtfs_query` "wall-clock timeout" is an opcode counter, not a clock
`src/stl_transit/io/db.py:160-167`

```python
deadline = [0]
budget = int(timeout_seconds * 1000)      # 5000
def progress() -> int:
    deadline[0] += 1
    return 1 if deadline[0] > budget else 0
conn.set_progress_handler(progress, 1000)
```

This aborts after 5 000 × 1 000 = 5 M VM instructions, which has nothing to do
with 5 seconds. Measured: `SELECT length(departure_time), COUNT(*) FROM
stop_times WHERE departure_time <> '' GROUP BY 1` runs in **0.341 s** on a raw
connection and is **aborted** through `gtfs_query`. Since `stop_times` has 489 011
rows, most aggregate queries over it fail — and `gtfs_query` is documented as
"the general-purpose escape hatch".

Compounding it, `db.py:189-196` maps *every* `sqlite3.DatabaseError` to
`UnsafeQuery`, so a timeout is reported to the model as
`UNSAFE_QUERY: Query rejected or failed: interrupted` with a remedy about
read-only queries. The model is told it wrote a dangerous query when it merely
wrote a slow one. Timeouts, syntax errors and authorizer denials all need
distinct codes.

### 5. `gtfs_stops` with no filter reports a fabricated `total`
`src/stl_transit/core/gtfs/entities.py:89`

```python
rows = conn.execute("SELECT * FROM stops LIMIT 2000").fetchall()
```

The hardcoded `LIMIT 2000` is invisible to `paginate`, which computes
`total = len(rows)`. Measured: `gtfs_stops(limit=600)` returns
`total: 2000, has_more: True, next_offset: 500` for a feed with **5 118** stops.
A client paging on `next_offset` reaches offset 2000 and stops, having silently
lost 3 118 stops. Either push the offset/limit into SQL or return the real
`COUNT(*)`.

### 6. The RT field census recurses into strings, inventing "unmodelled" fields
`src/stl_transit/core/rt/wire.py:229-235` (`walk`), consumed by
`core/rt/decode.py:96-101`

`walk` descends into every length-delimited field whose bytes happen to re-parse
as protobuf, including plain strings. Measured on the real snapshot,
`rt_schema_census` reports `unmodelled_paths: 3`, and all three are phantoms:

```
entity.trip_update.stop_time_update.stop_id.?6
entity.trip_update.stop_time_update.stop_id.?7
entity.id.?7
```

`stop_id` and `id` are `str` in `schema.py`; those are ASCII digits being
mis-read as nested submessages. The tool's own docstring says any unmodelled
path "needs investigating before porting", so this sends the port on a wild goose
chase. `walk` should consult the schema and refuse to descend into `str`-kinded
fields.

### 7. `stop_sequence` is dropped from departures, disabling half the RT matcher
`src/stl_transit/core/gtfs/departures.py:103` selects `st.stop_sequence`, but the
item dict built at `:154-173` never copies it. `core/rt/merge.py:80` then calls
`_stop_delay(tu, rec["stop_id"], rec.get("stop_sequence"))` — always `None` —
so the `stop_sequence` branch at `merge.py:37` is dead code. Verified: the emitted
item keys contain no `stop_sequence`. Any producer that identifies a
`StopTimeUpdate` by sequence rather than `stop_id` silently gets no delay.

### 8. `paginate`'s `truncated` flag means the wrong thing
`src/stl_transit/core/models.py:78` — `truncated = limit > hard_cap` reports on the
caller's *request*, not on whether anything was actually clipped. Measured:
`gtfs_routes(limit=1000)` returns all 62 routes with `truncated: True`. It should
be `len(rows) > offset + effective` or similar.

### 9. `gtfs_stops(search=…)` does not escape LIKE wildcards
`src/stl_transit/core/gtfs/entities.py:85-87` interpolates the user string into
`%…%` without an `ESCAPE` clause. Parameterised, so not SQL injection, but
`search="%"` and `search="_"` each return all 5 118 stops. A rider searching for
a stop name containing `_` gets nonsense.

### 10. Provenance/coverage use the pre-conversion date for tz-aware input
`src/stl_transit/core/service.py:441` calls `_provenance(snap, conn, when.date())`
*before* `when.astimezone(AGENCY_TZ)` on the next line; `support_explain_empty`
does the same at `:466-468`. With `at="2026-08-05T02:00:00+00:00"` the departures
engine correctly works in `2026-08-04T21:00:00-05:00`, while expiry and the
`feed_covers_date` check are evaluated against 2026-08-05. Harmless today, wrong
at a feed boundary.

### 11. `merge`'s `match_rate` denominator is the page, not the result
`src/stl_transit/core/rt/merge.py:98-107` sets `total = len(items)` where `items`
is the already-limited page, while `out["total"]` stays at the full count.
Measured: stop 7855 with `limit=10` reports `total: 20` and `match_rate: 0.7`
(7/10), not 7/20 = 0.35.

### Lower-severity / latent

- `core/gtfs/inspect.py:231` compares `departure_time >= '24:00:00'` as TEXT. Safe
  in *this* feed (verified: every value is exactly 8 chars, hours `03`…`25`), but
  a feed with unpadded `9:30:00` would sort above `24:00:00` and be misreported.
- `core/gtfs/departures.py:256` uses `at.replace(hour=0, minute=0, second=0)`
  without clearing `microsecond`.
- `io/db.py:179-183` rejects any query containing `;`, including inside a string
  literal or a comment. Verified: `SELECT ';' AS semi` and `SELECT 1 -- ; SELECT 2`
  are both refused. Conservative, but it is a false positive on valid SQL.
- `core/rt/merge.py:89-90` rebuilds the predicted time with `base.tzinfo`, which
  `fromisoformat` gives as a fixed offset, not `ZoneInfo` — a prediction that
  crosses a DST boundary keeps the stale offset.
- `core/rt/decode.py:64,74` treats `timestamp == 0` as absent (`if ts`).
- `core/service.py:585` passes a *list* of provenance dicts where `_ok` is typed
  for a single dict. Serializes fine; the annotation is a lie.

## Facts about the feed

**Q1 — Does `gtfs_stop_resolve`'s verdict hold? What is stop_code coverage, and does 15111 resolve?**
Yes, the verdict is correct. `stop_code` is non-empty on **5 118 / 5 118 stops
(coverage 1.0)**, **5 118 distinct** values (unique), 100 % numeric, all exactly
**5 digits**. `stop_id` is also 5 118/5 118 and unique but only 3–4 characters in
the sampled window, so it is clearly not the sign number. Metro's published
example **15111 resolves in both fields** — one row, `stop_id='15111'`,
`stop_code='15111'`, "14TH STREET @ CHOUTEAU SB", 181 stop_times rows — and
`gtfs_stop("15111")` reports `matched_by: "stop_code"`. Caveat: `stop_id` and
`stop_code` are identical for a large part of this feed, so the example does not
by itself discriminate; the coverage/uniqueness/length evidence does.

**Q2 — Does `gtfs_departures` return non-empty results at a busy stop, weekday midday?**
Yes. Stop **10626 (Forest Park–DeBaliviere MetroLink, 1 194 stop_times rows)** at
`2026-08-05T12:00:00` returns **18 departures**, no warnings, first one
MetroLink Blue Line trip `3389803`, `gtfs_time 12:05:00`,
`departure_local 2026-08-05T12:05:00-05:00`, `minutes_away 5`,
`service_id 319-T1`, `service_date 2026-08-05`. Same at `2026-08-03T12:00` (18)
and `2026-08-04T08:00` (18). No duplicate `(trip_id, stop_id, gtfs_time)` triples.
`support_explain_empty` on the same inputs walks all five checks and returns
`WINDOW_TOO_NARROW` (i.e. "service exists"), which is the right branch for a
non-empty query.

**Q3 — service_start / service_end / days_remaining. Is the feed expired?**
`service_start 2026-07-30`, `service_end 2026-08-30`, `days_remaining 27`,
`expired false`, `warning null`. Provenance carries `stale_days: -27`.
Derived from `calendar.txt` (`20260730`…`20260830`); `calendar_dates.txt` holds
just **2 rows, both dated 20260808**. `feed_info.txt` is absent, so `feed_info`
is `{}`. **The feed is not expired as of 2026-08-03** — 27 days of service data
remain, and the quarterly pick lands 2026-08-30.

**Q4 — Does the 24:xx rollover work, and what is the max departure_time?**
It works. `gtfs_late_night(threshold="24:00:00")` finds **14 029 stop_times rows
across 475 distinct trips**, **max departure_time `25:37:00`** (trip 3409236,
stop 16251, route 19845). Cross-checked two ways — lexicographic and
`CAST(substr(...) AS INTEGER) >= 24` — both give 14 029; min is `03:51:00`;
distinct leading hours run `03`…`25`.
End-to-end: querying stop **16149 (Cortex MetroLink)** at `2026-08-06T00:05:00`
returns 6 departures whose first three are `gtfs_time 24:20:00 / 24:21:00 /
24:25:00`, each with `service_date 2026-08-05` (the *previous* service date),
`departure_local 2026-08-06T00:20…00:25-05:00`, `after_midnight: true`.
`gtfs_service_day("2026-08-05T00:12:00")` offers both candidates:
2026-08-04 → `24:12:00` (`after_midnight_encoding: true`) and
2026-08-05 → `00:12:00`. Correct — but see bug #2 for DST days.

**Q5 — Do the RT tools work against the stored snapshot?**
Partly. Only `metro_rt_trips` has ever been fetched, so:
- `rt_health` — works. trip_updates: 144 entities, header ts 1785783946
  (`2026-08-03T19:05:46Z`), `age_seconds 8809`, `stale: true`, warning emitted.
  vehicle_positions and alerts report `available: false` with a remedy string.
- `rt_decode(trip_updates)` — decodes; header `gtfs_realtime_version 2.0`,
  144 entities, all `trip_update`, `unknown_top_level: []`. **But negative delays
  are corrupt** (bug #1).
- `rt_decode(vehicle_positions)` / `(alerts)` — raise `SNAPSHOT_NOT_FOUND`
  (correct, with remedy).
- `rt_wire` — works: 180 535 B, 145 top-level fields, 144 entities, named paths
  resolved (`header.gtfs_realtime_version`, …).
- `rt_schema_census` — runs, 24 paths from 1 sample, but the 3 "unmodelled"
  paths are phantoms (bug #6).
- `rt_reference` — works, 64 rows plus enums.
- `rt_stop_arrivals` — **broken in practice.** All 144 RT trip_ids exist in the
  GTFS `trips` table, so the join is sound, but MetroLink is not in the RT feed
  (stop 10626 is absent from the 4 118 RT stop_ids), so the obvious test stop
  yields `matched_departures: 0` and looks fine. On the bus stops the feed does
  cover, 4 of the 5 busiest raise `OverflowError` (bug #1). The one that works,
  stop 7855, correctly reports 7 matched departures with `delay 60…240 s` and
  `predicted_local` / `minutes_away_predicted`.

**Q6 — Does `gtfs_query` reject writes?**
Yes, both cases are rejected, both with code **`UNSAFE_QUERY`**:

| SQL | Result |
|---|---|
| `DROP TABLE stops` | `UNSAFE_QUERY: Query rejected or failed: not authorized` |
| `SELECT 1; SELECT 2` | `UNSAFE_QUERY: Only a single statement is permitted.` |
| `PRAGMA table_list` | `UNSAFE_QUERY: … not authorized` |
| `ATTACH DATABASE '/tmp/x.db' AS x` | `UNSAFE_QUERY: … not authorized` |
| `INSERT INTO stops VALUES ('a')` | `UNSAFE_QUERY: … not authorized` |
| `UPDATE stops SET stop_name='x'` | `UNSAFE_QUERY: … not authorized` |
| `DELETE FROM stops` | `UNSAFE_QUERY: … not authorized` |
| `CREATE TABLE z (a TEXT)` | `UNSAFE_QUERY: … not authorized` |
| `SELECT load_extension('x')` | `UNSAFE_QUERY: … not authorized` |
| `WITH q AS (SELECT 1 AS a) SELECT * FROM q` | ALLOWED, 1 row — correct |

Two mechanisms are in play and both fire: the multi-statement guard
(`db.py:179-183`) and the SQLite authorizer plus `mode=ro`
(`db.py:132-146, 156`). Defence in depth is intact. The weaknesses are
false positives (`SELECT ';' AS semi` is refused) and error-code collision —
timeouts and syntax errors also surface as `UNSAFE_QUERY` (bug #4).

Row/byte capping is correct: `limit=5` over 5 118 rows returns 5 with
`truncated: true`; a query that returns exactly 5 rows with `limit=5` returns
`truncated: false`; `limit=99999` is clamped to the 1 000-row hard cap.
