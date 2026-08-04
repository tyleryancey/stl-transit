"""The assumption regression suite (spec 6.10).

The app depends on facts about the feed that Metro never promised: that
stop_code is populated, that no trip is encoded past 28:00, that the three
realtime feeds stay fresh. Encode each one, run it on a schedule, and find out
from a cron job instead of from a user.

Two properties decide whether such a suite gets read or muted:

1. **Every assumption reports the observed value**, not just a verdict.
   "stop_code coverage 0.982, threshold 0.99" says how big the problem is and
   which way it is moving. "FAIL" says go and measure it yourself, which
   nobody does at 03:00 from a CI email.
2. **`skip` is a first-class outcome.** Seven of the sixteen assumptions need
   an input this process may not have -- a pinned baseline, a web capture, a
   realtime sample. Reporting "pass" when nothing was measured would be worse
   than having no suite.

The assumptions themselves live in `data/assertions.toml`: thresholds, titles,
severities, remediation, and which inputs each needs are all config. Only the
measurement is code, and each measurement is one function registered in
`_CHECKS` under the assumption's id.

`observed` is deliberately not one type across assumptions -- a coverage ratio,
a count, a GTFS time string and a list of route_ids are all "the measured
value" -- but `expected` always arrives in the same shape as its `observed`, so
the two can be printed side by side without knowing which assumption produced
them.

Pure logic (spec 2.1): never prints, never exits, never fetches. Connections
and hashes arrive as arguments.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from ...errors import StlError, UsageError
from ...io.clock import now_local
from ..gtfs.calendar import parse_gtfs_time
from ..gtfs.inspect import _columns, _tables, coverage

_PKG_ASSERTIONS = Path(__file__).resolve().parents[2] / "data" / "assertions.toml"

# The four outcomes. `skip` is not "pass with an excuse" and `opportunity` is
# not "fail we do not mind" -- each is counted separately in the result.
PASS = "pass"
FAIL = "fail"
SKIP = "skip"
OPPORTUNITY = "opportunity"

SEVERITIES = ("critical", "high", "medium", "opportunity")

# Ranked, not alphabetised: a report should read in the order a human debugs
# in -- what the feed itself says, then what changed since the baseline, then
# the two things that come from outside the feed.
CATEGORY_ORDER = ("static", "stability", "realtime", "web")
_CATEGORY_RANK = {name: i for i, name in enumerate(CATEGORY_ORDER)}

# Optional inputs to `run`. Named here so a typo in a `requires` list is a
# config error with a remedy, rather than an assumption that never skips and
# never runs.
INPUTS = ("baseline", "web_hashes", "rt_feeds")

_INPUT_REMEDY = {
    "baseline": "pass an earlier pinned snapshot as `baseline_conn` "
                "(`stl assert run --baseline <pin>`)",
    "web_hashes": "capture the pages first with `stl web capture --all`",
    "rt_feeds": "fetch a realtime sample first with `stl rt fetch --all`",
}

# Keys that describe an assumption. Everything else in its table is a tunable
# threshold, collected under `thresholds` and echoed beside the observed value.
_META_KEYS = frozenset(
    {"id", "title", "checks", "breaks", "severity", "category", "requires",
     "why", "code_path", "remediation", "thresholds"}
)

# All eight are required. An assumption with an empty `remediation` turns
# `assert explain` -- one of the group's three commands -- into a stub.
_REQUIRED_KEYS = ("title", "checks", "breaks", "severity", "category",
                  "why", "code_path", "remediation")


# ----------------------------------------------------------------- loading --

def load_assumptions(path: Path | None = None) -> list[dict[str, Any]]:
    """Parse assertions.toml. Packaged copy by default; $STL_ASSERTIONS overrides.

    Deliberately not lru_cached, unlike `config.load_config`: tests and
    $STL_ASSERTIONS swap the file underneath a live process, and a stale cache
    there would be a genuinely baffling failure for a file this cheap to read.
    """
    if path is None:
        env = os.environ.get("STL_ASSERTIONS")
        path = Path(env) if env else _PKG_ASSERTIONS
    path = Path(path).expanduser()
    if not path.is_file():
        raise UsageError(
            f"No assumption registry at {path}.",
            remedy="Point $STL_ASSERTIONS at a copy of data/assertions.toml, or "
            "pass `path=` explicitly.",
            path=str(path),
        )
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for aid, body in raw.get("assumptions", {}).items():
        spec: dict[str, Any] = {"id": aid, **body}
        spec.setdefault("requires", [])
        spec["thresholds"] = {k: v for k, v in body.items() if k not in _META_KEYS}
        _validate(spec, path)
        out.append(spec)
    if not out:
        raise UsageError(
            f"{path} defines no assumptions.",
            remedy="Each assumption is a `[assumptions.<id>]` table. See the "
            "packaged data/assertions.toml for the schema.",
            path=str(path),
        )
    out.sort(key=_sort_key)
    return out


def _sort_key(spec: dict[str, Any]) -> tuple[int, str]:
    """Fixed order: category rank, then id. Two runs must be byte-identical
    (spec 2.8), so nothing may depend on the order keys happen to sit in the
    TOML file."""
    return (_CATEGORY_RANK.get(spec["category"], len(CATEGORY_ORDER)), spec["id"])


def _validate(spec: dict[str, Any], path: Path) -> None:
    """Reject a malformed registry at load time, with the fix in the remedy.

    Config-driven only helps if a typo in the config says so; a missing key
    surfacing later as a KeyError inside one check would look like a tool bug.
    """
    aid = spec["id"]
    missing = [k for k in _REQUIRED_KEYS if not str(spec.get(k, "")).strip()]
    if missing:
        raise UsageError(
            f"Assumption {aid!r} is missing: {', '.join(missing)}.",
            remedy=f"Add the key(s) to [assumptions.{aid}] in {path}.",
            assumption=aid, missing=missing,
        )
    if spec["severity"] not in SEVERITIES:
        raise UsageError(
            f"Assumption {aid!r} has severity {spec['severity']!r}.",
            remedy=f"Use one of: {', '.join(SEVERITIES)}.",
            assumption=aid,
        )
    if spec["category"] not in CATEGORY_ORDER:
        raise UsageError(
            f"Assumption {aid!r} has category {spec['category']!r}.",
            remedy=f"Use one of: {', '.join(CATEGORY_ORDER)}.",
            assumption=aid,
        )
    unknown_inputs = [r for r in spec["requires"] if r not in INPUTS]
    if unknown_inputs:
        raise UsageError(
            f"Assumption {aid!r} requires unknown input(s): {', '.join(unknown_inputs)}.",
            remedy=f"`requires` accepts only: {', '.join(INPUTS)}.",
            assumption=aid,
        )
    if aid not in _CHECKS:
        # The one thing config alone cannot add. Say so plainly rather than
        # skipping the assumption, which would look like it had passed.
        raise UsageError(
            f"Assumption {aid!r} has no measurement function.",
            remedy="Add a check to _CHECKS in core/assertions/__init__.py under "
            f"that id, or remove [assumptions.{aid}] from {path}.",
            assumption=aid, implemented=sorted(_CHECKS),
        )


# ------------------------------------------------------------------ report --

def list_assumptions(path: Path | None = None) -> dict[str, Any]:
    """Every assumption: what it checks and what breaks if it fails."""
    items = [
        {
            "id": s["id"],
            "title": s["title"],
            "checks": s["checks"],
            "breaks": s["breaks"],
            "severity": s["severity"],
            "category": s["category"],
            "requires": list(s["requires"]),
        }
        for s in load_assumptions(path)
    ]
    return {"ok": True, "items": items, "count": len(items), "total": len(items)}


def explain(assumption_id: str, path: Path | None = None) -> dict[str, Any]:
    """One assumption in full: why it matters, what depends on it, how to fix it."""
    specs = load_assumptions(path)
    for s in specs:
        if s["id"] == assumption_id:
            return {
                "ok": True,
                "id": s["id"],
                "title": s["title"],
                "checks": s["checks"],
                "breaks": s["breaks"],
                "severity": s["severity"],
                "category": s["category"],
                "requires": list(s["requires"]),
                "why": s["why"],
                "code_path": s["code_path"],
                "remediation": s["remediation"],
                "thresholds": s["thresholds"],
            }
    ids = sorted(s["id"] for s in specs)
    raise UsageError(
        f"No assumption named {assumption_id!r}.",
        remedy="Run `stl assert list`. Valid ids: " + ", ".join(ids) + ".",
        requested=assumption_id,
        available=ids,
    )


# --------------------------------------------------------------- evaluation --

@dataclass(frozen=True)
class _Ctx:
    """Everything a check may read. Connections are read-only and never closed
    here -- the caller owns their lifetime."""

    conn: sqlite3.Connection
    baseline: sqlite3.Connection | None
    web_hashes: dict[str, str] | None
    rt_feeds: dict[str, dict[str, Any]] | None
    today: date


def _result(ok: bool, observed: Any, expected: Any, detail: str) -> dict[str, Any]:
    return {"status": PASS if ok else FAIL, "observed": observed,
            "expected": expected, "detail": detail}


def _skipped(expected: Any, detail: str, observed: Any = None) -> dict[str, Any]:
    """A measurement that could not be taken. Used both for a missing input and
    for an empty denominator -- a rate over zero rows is not evidence of
    anything, and rounding it to 1.0 would be a lie with a decimal point."""
    return {"status": SKIP, "observed": observed, "expected": expected, "detail": detail}


def run(
    conn: sqlite3.Connection,
    *,
    only: list[str] | None = None,
    baseline_conn: sqlite3.Connection | None = None,
    web_hashes: dict[str, str] | None = None,
    rt_feeds: dict[str, dict[str, Any]] | None = None,
    path: Path | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the assumptions against an imported GTFS snapshot.

    `conn` is the current feed, read-only. The optional inputs each unlock a
    subset of the suite and their absence produces `skip`, never `pass`:

    - `baseline_conn`: an earlier pinned snapshot, for stop_ids_stable and
      rail_route_ids_stable.
    - `web_hashes`: `{page_key: content_hash}` from `stl web capture`, for the
      *_unchanged assumptions.
    - `rt_feeds`: `{entity: {...}}` from a realtime sample. Each entry may
      carry `age_seconds` (rt_fresh), `trip_ids` (rt_join_rate) and
      `unmodelled` (rt_wire_shape -- the rows from
      `core.rt.decode.field_census(...)["unmodelled"]`, each with `path` and
      `presence_rate`).

    `as_of` injects the clock (spec 2.7) so feed_not_expiring is reproducible;
    it defaults to real now.

    Returns pass/fail/skip/opportunity per assumption, each carrying the value
    actually observed alongside the threshold it was compared against.
    """
    specs = load_assumptions(path)
    if only is not None:
        wanted = list(dict.fromkeys(only))  # dedupe, preserve the caller's order
        known = {s["id"] for s in specs}
        unknown = [i for i in wanted if i not in known]
        if unknown:
            raise UsageError(
                f"Unknown assumption id(s): {', '.join(unknown)}.",
                remedy="Run `stl assert list`. Valid ids: "
                + ", ".join(sorted(known)) + ".",
                requested=wanted,
                available=sorted(known),
            )
        selected = set(wanted)
        specs = [s for s in specs if s["id"] in selected]

    ctx = _Ctx(
        conn=conn,
        baseline=baseline_conn,
        web_hashes=web_hashes,
        rt_feeds=rt_feeds,
        today=now_local(as_of).date(),
    )
    available = {
        "baseline": baseline_conn is not None,
        "web_hashes": web_hashes is not None,
        "rt_feeds": rt_feeds is not None,
    }

    items: list[dict[str, Any]] = []
    for spec in specs:
        missing = [r for r in spec["requires"] if not available[r]]
        if missing:
            outcome = _skipped(
                spec["thresholds"] or None,
                "Skipped: needs " + ", ".join(missing) + ". To run it, "
                + "; ".join(_INPUT_REMEDY[r] for r in missing) + ".",
            )
        else:
            outcome = _evaluate(spec, ctx)

        if spec["severity"] == "opportunity" and outcome["status"] == FAIL:
            # An assumption that watches for the feed getting BETTER must never
            # trip exit 3. Demoting here rather than inside the check keeps the
            # rule general -- any assumption declared `opportunity` in config
            # inherits it, and the check stays a plain measurement.
            outcome = {**outcome, "status": OPPORTUNITY}

        items.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "status": outcome["status"],
                "observed": outcome["observed"],
                "expected": outcome["expected"],
                "detail": outcome["detail"],
                "breaks": spec["breaks"],
                "severity": spec["severity"],
                "category": spec["category"],
                "thresholds": spec["thresholds"],
                **({"error": outcome["error"]} if "error" in outcome else {}),
            }
        )

    counts = Counter(i["status"] for i in items)
    violations = [
        {"id": i["id"], "severity": i["severity"], "observed": i["observed"],
         "expected": i["expected"], "detail": i["detail"], "breaks": i["breaks"]}
        for i in items
        if i["status"] == FAIL
    ]
    return {
        "ok": not violations,
        "items": items,
        "total": len(items),
        "passed": counts[PASS],
        "failed": counts[FAIL],
        "skipped": counts[SKIP],
        "opportunities": counts[OPPORTUNITY],
        "violations": violations,
    }


def _evaluate(spec: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    """Run one check, containing its blast radius.

    One feed with an unexpected shape must not blind the other fifteen
    assumptions, so an unhandled error becomes that assumption's result rather
    than the whole run's. It is reported as a failure, not a skip: a check that
    could not run is not a check that passed, and the exception type is carried
    on the item so the cause is visible without a rerun.
    """
    try:
        return _CHECKS[spec["id"]](ctx, spec)
    except StlError:
        raise  # ours, deliberate, and already carries a remedy
    except Exception as exc:  # noqa: BLE001 - the failure itself is the datum
        return {
            "status": FAIL,
            "observed": None,
            "expected": spec["thresholds"] or None,
            "detail": f"Check could not be evaluated: {type(exc).__name__}: {exc}. "
                      "This is a fault in the tool or an unexpected feed shape, "
                      "not a measured violation -- inspect the feed with "
                      "`stl gtfs files` before trusting the rest of this run.",
            "error": type(exc).__name__,
        }


# ------------------------------------------------------------ static checks --

def _boarding_filter(conn: sqlite3.Connection) -> str:
    """SQL restricting `stops` to places a rider can actually board.

    A station parent (location_type=1) carries no sign to read a number off,
    so counting it would depress a coverage number that is entirely about
    signs -- and the depression would deepen every time Metro added a station,
    which is not a regression anyone should be paged about.
    """
    if "location_type" not in _columns(conn, "stops"):
        return ""
    return "WHERE COALESCE(location_type, '') IN ('', '0')"


def _stop_code_present(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    floor = float(spec.get("min_coverage", 0.99))
    if "stop_code" not in _columns(ctx.conn, "stops"):
        return _result(
            False, 0.0, floor,
            "stops.txt has no stop_code column at all. Every rider-facing lookup is "
            "broken until the resolver is repointed -- run `stl gtfs stop-resolve` to "
            "see which field now carries the numbers printed on the signs.",
        )
    total, filled = ctx.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN stop_code <> '' THEN 1 ELSE 0 END), 0) "
        f"FROM stops {_boarding_filter(ctx.conn)}"
    ).fetchone()
    if not total:
        return _skipped(floor, "The feed defines no boarding locations, so coverage "
                               "is not measurable. Check `stl gtfs stats` first.")
    observed = round(filled / total, 4)
    if observed >= floor:
        return _result(True, observed, floor,
                       f"stop_code is present on {filled} of {total} boarding locations "
                       f"({observed}); threshold {floor}.")
    return _result(
        False, observed, floor,
        f"Only {filled} of {total} boarding locations carry a stop_code ({observed}, "
        f"threshold {floor}). List the gaps with `stl gtfs query \"SELECT stop_id, "
        "stop_name FROM stops WHERE stop_code = ''\"`.",
    )


def _stop_code_unique(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    limit = int(spec.get("max_duplicates", 0))
    if "stop_code" not in _columns(ctx.conn, "stops"):
        return _skipped(limit, "stops.txt has no stop_code column; see stop_code_present.")
    rows = ctx.conn.execute(
        "SELECT stop_code, COUNT(*) FROM stops WHERE stop_code <> '' "
        "GROUP BY stop_code HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC, stop_code"
    ).fetchall()
    observed = len(rows)
    if observed <= limit:
        return _result(True, observed, limit,
                       f"No stop_code is shared by more than one stop (threshold "
                       f"{limit} duplicate value(s)).")
    worst = ", ".join(f"{r[0]} on {r[1]} stops" for r in rows[:3])
    return _result(
        False, observed, limit,
        f"{observed} stop_code value(s) are shared by more than one stop: {worst}. "
        "resolve_stop returns the first match, so a rider entering one of these gets "
        "an arbitrary stop's timetable with no sign that anything went wrong.",
    )


def _stop_code_format(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    pattern = str(spec.get("pattern", "^[0-9]{4,5}$"))
    floor = float(spec.get("min_match_rate", 0.99))
    if "stop_code" not in _columns(ctx.conn, "stops"):
        return _skipped(floor, "stops.txt has no stop_code column; see stop_code_present.")
    # `search`, not `fullmatch`: the anchors live in the config pattern so an
    # operator can deliberately write an unanchored rule (e.g. a prefix check)
    # without editing this module.
    rx = re.compile(pattern)
    codes = [r[0] for r in ctx.conn.execute("SELECT stop_code FROM stops WHERE stop_code <> ''")]
    if not codes:
        return _skipped(floor, "No stop_code values to inspect; see stop_code_present.")
    bad = sorted({c for c in codes if not rx.search(c)})
    observed = round((len(codes) - len(bad)) / len(codes), 4)
    if observed >= floor:
        return _result(True, observed, floor,
                       f"{len(codes) - len(bad)} of {len(codes)} stop_codes match "
                       f"{pattern} ({observed}); threshold {floor}.")
    return _result(
        False, observed, floor,
        f"{len(bad)} of {len(codes)} stop_codes do not match {pattern} ({observed}, "
        f"threshold {floor}). Examples: {', '.join(repr(c) for c in bad[:5])}. The entry "
        "screen's keypad and validator are built around this format.",
    )


def _feed_not_expiring(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    floor = int(spec.get("min_days_remaining", 7))
    cov = coverage(ctx.conn, ctx.today)
    days = cov["days_remaining"]
    if days is None:
        return _skipped(floor, "The feed declares no service dates at all, so expiry is "
                               "not measurable. Check calendar.txt with `stl gtfs files`.")
    if days >= floor:
        return _result(True, days, floor,
                       f"Service data runs to {cov['service_end']}, {days} day(s) from "
                       f"{cov['today']}; threshold {floor}.")
    lapsed = (f"expired {abs(days)} day(s) ago" if days < 0
              else f"only {days} day(s) left")
    return _result(
        False, days, floor,
        f"Service data ends {cov['service_end']} -- {lapsed} as of {cov['today']} "
        f"(threshold {floor}). Past that date every query returns an empty list, which "
        "is indistinguishable from 'no buses tonight'. Fetch the new feed with "
        "`stl snapshot fetch metro_gtfs` and regenerate the oracle fixtures.",
    )


def _no_frequencies_file(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    limit = int(spec.get("max_rows", 0))
    if "frequencies" not in _tables(ctx.conn):
        return _result(True, 0, limit,
                       "frequencies.txt is absent from the feed, so every trip is "
                       "literally scheduled and stop_times can be read as written.")
    rows = ctx.conn.execute("SELECT COUNT(*) FROM frequencies").fetchone()[0]
    if rows <= limit:
        return _result(True, rows, limit,
                       f"frequencies.txt is present but empty ({rows} rows; threshold "
                       f"{limit}), so no trip is headway-based yet.")
    return _result(
        False, rows, limit,
        f"frequencies.txt now has {rows} row(s) (threshold {limit}). Those trips have "
        "their stop_times expanded at intervals rather than read literally, and the "
        "departures engine does neither -- it will silently under-report them in both "
        "Python and Kotlin.",
    )


def _max_time_bounded(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    ceiling_hour = int(spec.get("max_hour_exclusive", 28))
    ceiling = ceiling_hour * 3600
    expected = f"< {ceiling_hour:02d}:00:00"
    cols = [c for c in ("departure_time", "arrival_time") if c in _columns(ctx.conn, "stop_times")]
    if not cols:
        return _skipped(expected, "stop_times has neither departure_time nor "
                                  "arrival_time; the feed is unusable as a schedule.")
    sql = " UNION ".join(
        f'SELECT DISTINCT "{c}" FROM stop_times WHERE "{c}" <> \'\'' for c in cols
    )
    # Compare on PARSED hours, never on the text. Lexicographic comparison
    # happens to work on this feed only because every value is zero-padded to
    # eight characters: one producer emitting "9:05:00" would sort above
    # "28:00:00" and this assertion would report a clean pass while the feed
    # broke the very assumption it exists to catch. inspect.late_night() takes
    # the text shortcut on purpose -- it is a browse tool. An assertion may not.
    worst_seconds, worst_value = -1, None
    over: list[tuple[int, str]] = []
    malformed: list[str] = []
    for (value,) in ctx.conn.execute(sql):
        try:
            secs = parse_gtfs_time(value)
        except ValueError:
            malformed.append(value)
            continue
        if secs > worst_seconds:
            worst_seconds, worst_value = secs, value.strip()
        if secs >= ceiling:
            over.append((secs, value.strip()))
    if worst_value is None and not malformed:
        return _skipped(expected, "stop_times carries no times at all; nothing to bound.")

    if malformed:
        shown = ", ".join(repr(v) for v in sorted(set(malformed))[:3])
        return _result(
            False, worst_value, expected,
            f"{len(set(malformed))} stop_times value(s) do not parse as HH:MM:SS: "
            f"{shown}. Service-day arithmetic cannot bound what it cannot read, and "
            "the Kotlin parser will hit the same rows.",
        )
    if not over:
        return _result(
            True, worst_value, expected,
            f"The largest time in stop_times is {worst_value} "
            f"({worst_seconds // 3600}h), below {expected}. Compared numerically: a "
            "text comparison only holds while every value stays zero-padded.",
        )
    shown = ", ".join(v for _, v in sorted(over)[-5:])
    return _result(
        False, worst_value, expected,
        f"{len(over)} distinct time value(s) reach {expected.lstrip('< ')} or beyond "
        f"(largest {worst_value}): {shown}. Inspect them with `stl gtfs late-night "
        f"--threshold {ceiling_hour:02d}:00:00`, then widen the service-date lookback "
        "in candidate_service_dates on both sides of the port before raising this "
        "threshold.",
    )


def _timezone_unchanged(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    expected = str(spec.get("expected_timezone", "America/Chicago"))
    if "agency" not in _tables(ctx.conn) or "agency_timezone" not in _columns(ctx.conn, "agency"):
        return _result(False, None, expected,
                       "agency.txt declares no agency_timezone. Every time in the app is "
                       "rendered against AGENCY_TZ, which now has nothing to verify it.")
    zones = sorted(
        {str(r[0]).strip() for r in ctx.conn.execute(
            "SELECT DISTINCT agency_timezone FROM agency WHERE agency_timezone <> ''")}
    )
    if not zones:
        return _result(False, None, expected,
                       "Every agency row has an empty agency_timezone. See "
                       "io/clock.AGENCY_TZ, which this is meant to corroborate.")
    # A second zone in the feed IS the finding, so the observed value carries
    # all of them rather than picking one to report.
    observed: Any = zones[0] if len(zones) == 1 else zones
    if zones == [expected]:
        return _result(True, observed, expected,
                       f"The feed declares exactly one agency timezone, {expected}, "
                       "matching io/clock.AGENCY_TZ.")
    return _result(
        False, observed, expected,
        f"Agency timezone is now {observed!r}, not {expected!r}. Service days are "
        "measured from noon-minus-twelve-hours in the agency's zone, so every departure "
        "and both DST transition days are affected. A second zone usually means another "
        "operator was merged in -- decide whether the app covers it before changing "
        "io/clock.AGENCY_TZ.",
    )


def _no_fare_files(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    names = [str(t) for t in spec.get("fare_tables", [])]
    tables = _tables(ctx.conn)
    present = []
    for name in sorted(names):
        if name in tables:
            rows = ctx.conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            if rows:
                present.append((name, rows))
    observed = [name for name, _ in present]
    if not present:
        return _result(True, observed, [],
                       "No fare_*.txt in the feed, so fares still have to be scraped "
                       "and bundled (`stl web extract fares`).")
    listed = ", ".join(f"{name}.txt ({rows} rows)" for name, rows in present)
    # Returns FAIL; `run` demotes it to `opportunity` because the assumption is
    # declared with that severity. Nothing is broken -- something got better.
    return _result(
        False, observed, [],
        f"The feed now publishes {listed}. The scraped fare table and the "
        "fares_unchanged assumption that guards it could both be retired -- compare "
        "the feed's fares against the bundled table before switching.",
    )


# --------------------------------------------------------- stability checks --

def _stop_codes(conn: sqlite3.Connection) -> set[str]:
    """Every non-empty stop_code, boarding location or not.

    Deliberately a wider set than _stop_code_present's denominator: coverage is
    about signs, but survival is about what a rider may have saved, and a
    station's code resolves and is therefore savable.
    """
    if "stop_code" not in _columns(conn, "stops"):
        return set()
    return {r[0] for r in conn.execute("SELECT stop_code FROM stops WHERE stop_code <> ''")}


def _stop_ids_stable(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    floor = float(spec.get("min_survival", 0.98))
    base = _stop_codes(ctx.baseline) if ctx.baseline is not None else set()
    if not base:
        return _skipped(floor, "The baseline snapshot carries no stop_code values, so "
                               "survival is not measurable against it. Pin a baseline "
                               "from a feed that has them.")
    current = _stop_codes(ctx.conn)
    survived = base & current
    observed = round(len(survived) / len(base), 4)
    if observed >= floor:
        return _result(True, observed, floor,
                       f"{len(survived)} of {len(base)} baseline stop_codes still "
                       f"resolve ({observed}); threshold {floor}.")
    lost = sorted(base - current)
    return _result(
        False, observed, floor,
        f"{len(lost)} of {len(base)} baseline stop_codes no longer resolve "
        f"(survival {observed}, threshold {floor}). First few: "
        f"{', '.join(lost[:5])}. Each one is somebody's saved stop that will go blank "
        "with no explanation -- list them all with `stl diff stop-ids`.",
    )


def _rail_route_ids(conn: sqlite3.Connection, types: list[str]) -> list[str]:
    placeholders = ", ".join("?" * len(types))
    return sorted(
        {r[0] for r in conn.execute(
            f"SELECT route_id FROM routes WHERE TRIM(route_type) IN ({placeholders})",
            types)}
    )


def _rail_route_ids_stable(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    types = [str(t) for t in spec.get("rail_route_types", ["0", "1", "2"])]
    base = _rail_route_ids(ctx.baseline, types) if ctx.baseline is not None else []
    if not base:
        return _skipped(None, f"The baseline has no routes of type(s) "
                              f"{', '.join(types)}, so there is no rail set to compare.")
    current = _rail_route_ids(ctx.conn, types)
    if current == base:
        return _result(True, current, base,
                       f"All {len(base)} rail route_id(s) are unchanged since the "
                       "baseline: " + ", ".join(base) + ".")
    lost = [r for r in base if r not in current]
    added = [r for r in current if r not in base]
    parts = []
    if lost:
        parts.append("retired: " + ", ".join(lost))
    if added:
        parts.append("new: " + ", ".join(added))
    return _result(
        False, current, base,
        "Rail route_ids changed (" + "; ".join(parts) + "). Every rail branch keyed on "
        "route_id -- line colours, station lists, and the holiday rule where rail runs "
        "'Weekend' service while bus runs Sunday -- now silently applies to nothing. "
        "Confirm with `stl diff routes`.",
    )


# ---------------------------------------------------------- realtime checks --

def _rt_fresh(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    """Was Metro's realtime feed current AT THE MOMENT IT WAS FETCHED?

    That is the question the app depends on. Measuring the stored snapshot's
    header timestamp against *now* answers a different one -- "how long since I
    last fetched" -- and conflating the two makes this assumption fail every
    morning on any machine that did not fetch overnight. An assumption that is
    red every day is one people learn to ignore, which costs more than the
    check is worth.

    So: prefer `age_at_fetch_seconds`, and when the local copy is too old to
    testify about the live feed, SKIP with a remedy. Skip means the measurement
    was not taken, which is exactly what is true.
    """
    ceiling = float(spec.get("max_age_seconds", 300))
    required = [str(e) for e in spec.get("required_entities", [])] or sorted(ctx.rt_feeds or {})
    feeds = ctx.rt_feeds or {}
    missing = [e for e in required if e not in feeds]
    ages: dict[str, float] = {}
    unmeasured: list[str] = []
    measured_at_fetch = False
    for entity in required:
        entry = feeds.get(entity)
        if entry is None:
            continue
        age = entry.get("age_at_fetch_seconds")
        if age is not None:
            measured_at_fetch = True
        else:
            age = entry.get("age_seconds")
        if age is None:
            unmeasured.append(entity)
        else:
            ages[entity] = float(age)

    if measured_at_fetch:
        # How long ago the freshest sample was taken. Past the ceiling, nothing
        # here describes the live feed any more.
        since = [
            float(e["fetched_ago_seconds"]) for e in feeds.values()
            if e.get("fetched_ago_seconds") is not None
        ]
        if since and min(since) > ceiling:
            return _skipped(
                ceiling,
                f"The freshest realtime sample was fetched {round(min(since))}s ago, "
                f"which is past the {ceiling}s window this assumption describes. A "
                "stored snapshot cannot testify about the live feed. Re-fetch with "
                "`stl rt fetch --all` and re-run to measure it.",
            )

    if not ages and not missing:
        return _skipped(ceiling, "No entity in the realtime sample reports age_seconds, "
                                 "so freshness is not measurable. `stl rt health` "
                                 "computes it from the feed header timestamp.")
    worst_entity = max(ages, key=lambda e: (ages[e], e)) if ages else None
    observed = round(ages[worst_entity], 1) if worst_entity else None
    stale = sorted(e for e, a in ages.items() if a > ceiling)
    basis = "when fetched" if measured_at_fetch else "against now"
    if not missing and not stale:
        return _result(True, observed, ceiling,
                       f"The oldest realtime feed is {worst_entity} at {observed}s "
                       f"({basis}); threshold {ceiling}s.")
    parts = []
    if stale:
        parts.append("stale: " + ", ".join(f"{e} at {round(ages[e], 1)}s" for e in stale))
    if missing:
        # A feed that could not be fetched is not fresh. Reporting it as a skip
        # would let a 404 on the alerts endpoint pass for a healthy suite.
        parts.append("no sample at all: " + ", ".join(missing))
    if unmeasured:
        parts.append("no timestamp: " + ", ".join(sorted(unmeasured)))
    return _result(
        False, observed, ceiling,
        "Realtime is not fresh (" + "; ".join(parts) + f"; threshold {ceiling}s). A "
        "stale feed does not look broken to a rider, it looks like a bus that is not "
        "moving. If realtime is genuinely down the app must fall back to scheduled-only "
        "and say so.",
    )


def _rt_join_rate(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    floor = float(spec.get("min_join_rate", 0.95))
    entity = str(spec.get("entity", "trip_updates"))
    entry = (ctx.rt_feeds or {}).get(entity) or {}
    trip_ids = sorted({str(t) for t in (entry.get("trip_ids") or [])})
    if not trip_ids:
        return _skipped(floor, f"The realtime sample carries no trip_ids for {entity}, "
                               "so the join rate is not measurable. A rate over zero "
                               "trips is not evidence of anything.")
    known = {r[0] for r in ctx.conn.execute("SELECT trip_id FROM trips")}
    unresolved = [t for t in trip_ids if t not in known]
    observed = round((len(trip_ids) - len(unresolved)) / len(trip_ids), 4)
    if observed >= floor:
        return _result(True, observed, floor,
                       f"{len(trip_ids) - len(unresolved)} of {len(trip_ids)} realtime "
                       f"trip_ids resolve into the static feed ({observed}); threshold "
                       f"{floor}.")
    return _result(
        False, observed, floor,
        f"Only {len(trip_ids) - len(unresolved)} of {len(trip_ids)} realtime trip_ids "
        f"resolve into the static feed ({observed}, threshold {floor}). Unresolved: "
        f"{', '.join(unresolved[:3])}. This is the classic post-pick desync -- the "
        "realtime producer is still emitting the previous pick's ids. Re-fetch the "
        "static feed first; if the rate stays low the two Metro systems are genuinely "
        "out of step.",
    )


def _rt_wire_shape(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    ceiling = float(spec.get("max_unmodelled_rate", 0.05))
    rows: list[tuple[float, str, str]] = []
    censused = False
    for entity, entry in sorted((ctx.rt_feeds or {}).items()):
        census = (entry or {}).get("unmodelled")
        if census is None:
            continue
        censused = True
        for row in census:
            rows.append(
                (float(row.get("presence_rate") or 0.0), str(row.get("path", "?")), entity)
            )
    if not censused:
        return _skipped(ceiling, "No field census in the realtime sample. Pass "
                                 "`core.rt.decode.field_census(blobs)['unmodelled']` as "
                                 "each entity's `unmodelled` key, or run "
                                 "`stl rt schema --samples N`.")
    if not rows:
        return _result(True, 0.0, ceiling,
                       "Every protobuf path in the sample is named in "
                       "core/rt/schema.py; nothing unmodelled to rank.")
    rate, path, entity = max(rows, key=lambda r: (r[0], r[1]))
    observed = round(rate, 4)
    if observed <= ceiling:
        return _result(True, observed, ceiling,
                       f"The most frequent unmodelled path is {path} in {entity} at "
                       f"{observed} of samples; threshold {ceiling}.")
    return _result(
        False, observed, ceiling,
        f"Unmodelled protobuf path {path} appears in {observed} of {entity} samples "
        f"(threshold {ceiling}). A '?<n>' segment is a field number absent from "
        "core/rt/schema.py, so the hand-rolled Kotlin decoder skips it silently -- the "
        "data is missing rather than wrong, which is much harder to notice. Inspect it "
        "with `stl rt wire`.",
    )


# --------------------------------------------------------------- web checks --

def _page_hash_unchanged(ctx: _Ctx, spec: dict[str, Any]) -> dict[str, Any]:
    """Shared by fares_unchanged, holidays_unchanged and terms_unchanged: the
    measurement is identical, only the page and the consequence differ."""
    page = str(spec.get("page", spec["id"]))
    recorded = str(spec.get("expected_hash", "") or "")
    current = (ctx.web_hashes or {}).get(page)
    if current is None:
        return _skipped(recorded or None,
                        f"No capture for page {page!r} in this run. Capture it with "
                        f"`stl web capture {page}`.")
    if not recorded:
        # Unarmed on purpose: a hash invented before the first capture would
        # make the suite red on day one, and pasting it in is the gesture that
        # means "I have read this page and accept its current contents".
        return _skipped(
            None,
            f"No baseline hash recorded for {page!r}. Paste the current hash "
            f"({current}) into expected_hash under [assumptions.{spec['id']}] to arm "
            "this assumption.",
            observed=current,
        )
    if current == recorded:
        return _result(True, current, recorded,
                       f"The {page} page still hashes to {recorded[:12]}, so the "
                       "bundled copy derived from it is still accurate.")
    return _result(
        False, current, recorded,
        f"The {page} page content hash changed: {current[:12]} now, {recorded[:12]} "
        f"recorded. Review the change with `stl web diff {page} <a> <b>`, regenerate "
        "whatever is bundled from it, and only then update expected_hash -- updating "
        "the hash first hides the change from the next run.",
    )


# The one thing config alone cannot supply. Keyed by assumption id, validated
# at load time so a table without a measurement is a startup error rather than
# an assumption that silently never runs.
_CHECKS: dict[str, Callable[[_Ctx, dict[str, Any]], dict[str, Any]]] = {
    "stop_code_present": _stop_code_present,
    "stop_code_unique": _stop_code_unique,
    "stop_code_format": _stop_code_format,
    "stop_ids_stable": _stop_ids_stable,
    "feed_not_expiring": _feed_not_expiring,
    "no_frequencies_file": _no_frequencies_file,
    "max_time_bounded": _max_time_bounded,
    "timezone_unchanged": _timezone_unchanged,
    "rail_route_ids_stable": _rail_route_ids_stable,
    "rt_fresh": _rt_fresh,
    "rt_join_rate": _rt_join_rate,
    "rt_wire_shape": _rt_wire_shape,
    "fares_unchanged": _page_hash_unchanged,
    "holidays_unchanged": _page_hash_unchanged,
    "terms_unchanged": _page_hash_unchanged,
    "no_fare_files": _no_fare_files,
}
