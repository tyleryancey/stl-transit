# Evaluations — `stl_transit_mcp`

Ten question/answer pairs for the MCP server, pinned to a fixed snapshot so the
answers do not rot as Metro's feed moves underneath them.

Every question is deliberately *not* answerable in one obvious call. Each needs a
chain — resolve a stop, then resolve a date's service_ids, then read one field —
because a suite of one-call lookups measures whether the tool list is spelled
correctly, not whether an agent can navigate it.

---

## The pin

| | |
|---|---|
| GTFS | `gtfs-20260803T190539Z-f2d721` (source `metro_gtfs`, fetched 2026-08-03) |
| service window | 2026-07-30 → 2026-08-30 |
| size | 5,118 stops · 62 routes · 9,577 trips · 489,011 stop_times rows |
| Realtime | `rt-20260803T190604Z-5ceebf` (source `metro_rt_trips`, 144 TripUpdates) |

The snapshot id appears in the text of all ten questions. An agent that answers
against "the latest snapshot" is answering a different question, and will be
marked wrong the first time Metro publishes a new pick.

---

## Running them

**The evals themselves** are for grading an agent driving the MCP server. Point a
client at `stl-mcp` (stdio), feed it one `<question>`, and string-compare its
final answer against the `<answer>`. Answers are single tokens on purpose:
a number, an id, or one ISO timestamp. Nothing that needs a judge.

**Checking that the answers are still true:**

```sh
STL_HOME=$PWD .venv/bin/python evaluations/verify_answers.py
```

Prints `PASS`/`FAIL` per question and exits non-zero on any failure. It needs
`feed.sqlite` built for the pinned snapshot — `stl gtfs import --snapshot
gtfs-20260803T190539Z-f2d721` if it is missing. No network.

Each answer is recomputed **twice**: once through `core.service` (the code the
MCP tools front) and once by re-deriving it from `feed.sqlite` and the raw
protobuf bytes, importing nothing from `core.gtfs` or `core.rt` — including a
30-line varint reader written for the purpose. Grading the tool with the tool's
own arithmetic proves self-consistency and nothing else. The two routes are
reported separately, so a `ROUTES DISAGREE` line means the *question* is
ambiguous and needs rewording, which is a different repair from a drifted answer.

---

## What each question exercises

| # | Answer | Tools it needs | Capability under test |
|---|---|---|---|
| 1 | `3409236` | `gtfs_late_night` → `gtfs_departures` | The 24:xx+ rollover. A departure encoded `25:37:00` belongs to the **previous** service date, so at 01:30 on Sunday the trip on offer is Saturday's. Two trips carry `25:37:00`; picking the wrong service date returns the wrong one. |
| 2 | `2767` | `gtfs_calendar` → `gtfs_query` | `calendar_dates.txt` exceptions. 2026-08-08 both **adds** `319-T2` (a service_id that appears in no `calendar.txt` row at all) and **removes** `325-T2`. Reading `calendar.txt` alone gives 2765. |
| 3 | `19870R` | `gtfs_routes` → `gtfs_calendar` → `gtfs_query` | MetroLink vs MetroBus, and route_id churn. The feed straddles a pick, so "MetroLink Red Line" is two route_ids (`19731R`, `19870R`) split by date. Anything in the app keyed on a rail route_id has a shelf life. |
| 4 | `38` | `gtfs_routes(route_type=2)` → `gtfs_stops(route_id=…)` ×4 | `route_type` as the rail/bus discriminator. Also establishes that **no** stop is served by both rail and bus in this feed — transit centres share a name with their MetroLink platform but never a stop_id, so the oracle's `multimodal_stop` case has no material here. |
| 5 | `24` | `assert_run` → `gtfs_query` | The assumption suite's *observed* values. `stop_code_format` passes at 0.9953 against `^[0-9]{4,5}$`; the 0.0047 is 24 real three-digit stop codes that a keypad validator built on the pattern would reject. A ratio nobody converts back to whole stops is a ratio nobody acts on. |
| 6 | `72` | `gtfs_stop_resolve` → `gtfs_departures` | stop_code vs stop_id, plus rollover at the window edge. `15111` is the number on Metro's own stop-sign photo; it resolves via `stop_code`, which in this feed is byte-identical to `stop_id` on all 5,118 stops. A midnight-to-midnight window picks up **yesterday's** `24:23:00` and drops today's — a same-service-date query returns 71. |
| 7 | `14` | `rt_health` → `rt_decode` (paged past the default limit) | GTFS-RT `schedule_relationship`. All 14 cancelled TripUpdates arrive as a bare trip descriptor: no `route_id`, no `stop_time_update`. An app that keys off `stop_time_update` sees nothing rather than a cancellation. |
| 8 | `-240` | `rt_decode` (all 144 entities) | `delay` is a **signed** int32; negative means early. 2,207 of the 7,172 delay values in this snapshot are negative. Decode it unsigned and −240 becomes 1.8e19, which is how `rt_stop_arrivals` used to raise `OverflowError`. |
| 9 | `145` | `rt_decode` → `rt_wire` | Protobuf wire format. One `FeedHeader` (field 1) plus 144 `FeedEntity` (field 2). Answering 144 means the header was skipped; answering 1 means the repeated field was read as a single blob. The LP3 has no protobuf runtime, so the Kotlin decoder is hand-written against exactly this. |
| 10 | `2027-03-13T23:00:00-06:00` | `gtfs_service_day` | DST arithmetic. A GTFS service day starts at noon-minus-twelve-hours, not local midnight, so the spring-forward service day begins at 23:00 on the *previous calendar day*. On the other 363 days of the year the two definitions agree, which is why the error hides. |

### Two surfaces with no question, on purpose

- **`pickup_type = 1`** (rider cannot board). All 489,011 `stop_times` rows are
  `pickup_type = '0'`, so the departures engine's filter drops nothing and any
  question about it answers `0` without exercising anything. The rule is still
  written into `verify_answers.scheduled_departures`, so the day Metro starts
  marking drop-off-only stops the raw route tracks the tool instead of diverging.
- **Realtime join rate.** All 144 RT `trip_id`s and all 4,118 RT `stop_id`s
  resolve into the static snapshot — 1.000 both ways. A perfect rate is worth
  asserting (`assert_run --only rt_join_rate`) but makes a poor eval question,
  since the answer is the same whether or not the agent did the join.

### One thing question 10 deliberately does not ask

`stl_gtfs_service_day` returns `service_day_start`, `gtfs_seconds` and
`gtfs_time` on the same response. Only `service_day_start` is graded.
`gtfs_seconds` is computed as `when - start` where both operands carry the *same*
`tzinfo` object, and Python defines that subtraction as wall-clock — offsets are
ignored — so the value is an hour out on both transition days each year. Pinning
it would train the Kotlin port to reproduce a bug. Fix `service.gtfs_service_day`
to subtract in UTC (`core.gtfs.calendar.service_day_start` and
`absolute_time` already do), then this suite gains an eleventh question.

---

## When the pin is replaced

Metro publishes a feed that ends at the next quarterly pick, so this snapshot
expires. When a new one is pinned:

1. Fetch and import the new snapshot, then edit `GTFS_SNAPSHOT` / `RT_SNAPSHOT`
   at the top of `verify_answers.py`.
2. Run it. Expect failures — that is the point. The `FAIL` lines name exactly
   which answers moved and print the new value beside the old one.
3. For each failure, decide which kind it is before touching anything:
   - **The world moved.** Trip ids, stop counts and route ids all churn at a
     pick. Update the `<answer>` in `evaluations.xml` and the number in the table
     above. The *question* stays as written.
   - **The question no longer has an answer.** Some questions name a date inside
     the old service window (2026-08-08, 2026-08-17, 2026-08-03/09). Those dates
     will fall outside the new feed. Re-anchor each to the equivalent date in the
     new window — a Saturday carrying a `calendar_dates` exception, a weekday
     after the pick boundary, a stop with a post-24:00 departure — rather than
     deleting the question. The capability being tested is what matters, not the
     date.
   - **`ROUTES DISAGREE`.** Neither of the above. The tool route and the raw
     route reached different answers, which means the question admits two
     readings. Reword it until they agree, then re-pin.
4. Rewrite the snapshot ids inside the ten `<question>` bodies. They are prose,
   so nothing enforces this — and an eval that names a snapshot the store no
   longer holds fails in a way that looks like a wrong answer.
