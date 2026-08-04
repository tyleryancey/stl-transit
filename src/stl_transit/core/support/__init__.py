"""The `support` group (spec 6.9): reproduce a reported problem without a device.

"Stop 15111 showed nothing at 11:47 last Tuesday" is the report this group
exists to answer, and it has to be answerable on Thursday, on a laptop, without
the phone and without waiting for next Tuesday. The clock is injected (spec
2.7), the feed comes from a stored snapshot, and the realtime frame -- if there
is one -- comes from a recording. Nothing here fetches.

Three functions, in the order a support conversation actually goes:

1. `repro` -- what the app SHOULD have shown. The oracle for the complaint.
2. `diff_device` -- what it actually showed, against that oracle.
3. `bundle_report` -- what to attach to the issue when the answer is "that is a
   bug in the app".

The forgiveness rule for `diff_device` is not politeness, it is the whole job: a
support tool that rejects a user's paste because the device spelled a key
`routeShortName` instead of `route_short_name` has failed at the one thing it
was for. It normalizes, it guesses, and it says what it guessed.

Pure logic (spec 2.1): never prints, never exits, never prompts, never fetches.
Deterministic (spec 2.8): fixed sort orders, injected `now`.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ... import __version__
from ...errors import StopNotFound
from ...io.clock import AGENCY_TZ
from ..bundle import canonical_json, sha256_text
from ..gtfs import departures as dep
from ..rt import merge as rtmerge

__all__ = ["bundle_report", "diff_device", "repro"]

# The whole window is returned, not the four rows the phone shows. A support
# repro is read by someone deciding whether the app was wrong, and "the fifth
# departure was there all along" is exactly the answer they need.
REPRO_LIMIT = 50


# --------------------------------------------------------------------- repro --

def _stop_code(merged: dict[str, Any]) -> str:
    """The rider-facing number this board belongs to, when it is unambiguous.

    Carried onto every row so a device that logs the number the rider TYPED
    (`stop_code`) can still be matched against an oracle keyed on the internal
    `stop_id` -- the stop_code-vs-stop_id trap, arriving here as a diff nobody
    would otherwise be able to explain. Left empty when the needle matched
    several stops, because guessing which code a row belongs to would be worse
    than not answering.
    """
    codes = sorted({str(s.get("stop_code") or "").strip()
                    for s in ((merged.get("stop") or {}).get("stops") or [])
                    if str(s.get("stop_code") or "").strip()})
    return codes[0] if len(codes) == 1 else ""


def _render_rows(merged: dict[str, Any]) -> list[dict[str, Any]]:
    """The departure board, one dict per line, in canonical field names.

    Deliberately flat and deliberately named the way `diff_device` names things:
    this list IS the `expected` side of a device comparison, and a shape that
    needed translating on the way there would be a shape that drifted.
    """
    stop_code = _stop_code(merged)
    rows: list[dict[str, Any]] = []
    for item in merged.get("items", []):
        predicted = item.get("predicted_local")
        rows.append(
            {
                "route": item.get("route_short_name") or item.get("route_id") or "",
                "headsign": item.get("headsign") or "",
                # What the rider is shown: the prediction when there is one, the
                # schedule otherwise. Both are carried so a disagreement can be
                # attributed to the RT merge rather than to the schedule.
                "departure": predicted or item.get("departure_local"),
                "scheduled_departure": item.get("departure_local"),
                "predicted_departure": predicted,
                "minutes_away": (item.get("minutes_away_predicted") if predicted
                                 else item.get("minutes_away")),
                "realtime": bool(item.get("realtime")),
                "status": item.get("status") or "SCHEDULED_ONLY",
                "trip_id": item.get("trip_id"),
                "stop_id": item.get("stop_id"),
                "stop_code": stop_code,
                "route_id": item.get("route_id"),
                "service_date": item.get("service_date"),
                "gtfs_time": item.get("gtfs_time"),
            }
        )
    # Ordered by when the bus actually arrives, not by when it was scheduled: a
    # bus running twelve minutes late belongs below one that is on time, and
    # that reordering is itself a thing the app can get wrong. Route and trip
    # break ties so two runs agree byte for byte (spec 2.8).
    rows.sort(key=lambda r: (str(r["departure"] or ""), str(r["route"] or ""),
                             str(r["trip_id"] or "")))
    return rows


def repro(conn, stop: str, at: datetime, window_minutes: int, tz: ZoneInfo,
          rt_decoded: dict[str, Any] | None = None,
          feed_end: date | None = None) -> dict[str, Any]:
    """Exactly what the app should have shown at one stop, at one instant.

    Composes `gtfs.departures`, `rt.merge` and `gtfs.explain_empty`: the
    expected render, and -- when it is empty -- the reason it is empty, named
    down to the branch of the decision tree.

    `rt_decoded` is an optional decoded GTFS-RT frame (from a recording, via
    `rt.decode.decode_feed`). Without one the result degrades to scheduled-only
    and says so, because that is exactly what the app must do when realtime is
    unavailable and a repro that quietly hid the difference would be answering a
    different question from the one asked.

    An unresolvable stop number does NOT raise. "The number I typed showed
    nothing" is the complaint, and a stop that no longer exists is one of its
    answers (`explain_empty`'s STOP_NOT_FOUND branch) -- refusing the input here
    would mean the support tool declined the exact report it was built for.
    """
    warnings: list[str] = []
    stop_error: dict[str, Any] | None = None

    try:
        scheduled = dep.departures(conn, stop, at, window_minutes, tz, REPRO_LIMIT)
    except StopNotFound as exc:
        stop_error = exc.to_dict()["error"]
        scheduled = {
            "stop": {"needle": stop, "matched_by": "", "ambiguous": False,
                     "stops": [], "stop_ids": []},
            "query": {"at": at.isoformat(), "window_minutes": window_minutes},
            "calendars": {}, "total": 0, "count": 0, "items": [], "has_more": False,
        }
        warnings.append(f"{exc.message} {exc.remedy}")

    merged = rtmerge.merge(scheduled, rt_decoded)
    rows = _render_rows(merged)
    empty = not rows

    reason = None
    if empty:
        # Only computed when there is nothing to show. explain_empty walks the
        # whole decision tree and re-queries a full day, which is not work worth
        # doing to tell someone their bus is at 12:05.
        reason = dep.explain_empty(conn, stop, at, window_minutes, tz, feed_end)

    if feed_end is not None and at.date() > feed_end:
        warnings.append(
            f"The instant queried ({at.date()}) is past the feed's last service date "
            f"({feed_end}). Any answer below describes a feed that had already lapsed when "
            "the rider looked, which is itself the likely bug."
        )
    for warning in merged.get("warnings", []) or []:
        if warning not in warnings:
            warnings.append(warning)

    realtime = merged.get("realtime") or {}
    if empty:
        verdict = (f"Nothing to show: {reason['verdict']}. {reason['remedy']}"
                   if reason else "Nothing to show.")
    else:
        verdict = (
            f"{len(rows)} departure(s) expected at {_stop_label(merged)} in the "
            f"{window_minutes}-minute window from {at.isoformat()}. Realtime "
            + ("applied: " + str(realtime.get("matched_departures", 0)) + " of "
               + str(realtime.get("departures_inspected", len(rows)))
               + " departures carried a prediction."
               if realtime.get("available") else
               "was not supplied, so these are scheduled times -- the app must say so too.")
        )

    return {
        "ok": True,
        "query": {
            "stop": stop,
            "at": at.isoformat(),
            "window_minutes": window_minutes,
            "timezone": str(tz),
            "feed_end": feed_end.isoformat() if feed_end else None,
            "realtime_supplied": rt_decoded is not None,
        },
        "stop": merged.get("stop"),
        "stop_error": stop_error,
        # The full structure, calendars and all: when Python and the app
        # disagree, which service_ids were active is the first thing anybody asks.
        "expected": merged,
        "render": rows,
        "count": len(rows),
        "total": merged.get("total", 0),
        "empty": empty,
        "reason": reason,
        "remedy": (reason or {}).get("remedy") if empty else None,
        "realtime": realtime,
        "verdict": verdict,
        "warnings": warnings,
        "notes": [
            "`render` is the oracle: pass it to `support.diff_device` as `expected` "
            "alongside whatever the device logged.",
            "`expected.calendars` shows which service_ids were active on each candidate "
            "service date, which is where a 24:xx or holiday disagreement shows up first.",
        ],
    }


def _stop_label(merged: dict[str, Any]) -> str:
    resolved = merged.get("stop") or {}
    stops = resolved.get("stops") or []
    name = (stops[0].get("stop_name") if stops else "") or ""
    needle = resolved.get("needle", "")
    return f"{needle} ({name})" if name else str(needle)


# --------------------------------------------------------------- diff_device --

# Canonical field names, and every spelling a device log has been seen to use
# for them. Matching is done on a slug (lowercased, non-alphanumerics removed),
# so `route_short_name`, `routeShortName` and `Route Short Name` all collapse to
# one key and only genuinely different WORDS need listing here.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "trip_id": ("trip_id", "tripId", "trip"),
    "route": ("route", "route_short_name", "routeShortName", "short_name", "line",
              "route_name", "routeNumber", "route_id", "routeId"),
    "headsign": ("headsign", "trip_headsign", "tripHeadsign", "stop_headsign",
                 "destination", "dest", "sign", "toward"),
    "departure": ("departure", "departure_local", "departureLocal", "predicted_local",
                  "departure_time", "departureTime", "time", "when", "scheduled",
                  "expected_time", "arrival_time"),
    "minutes_away": ("minutes_away", "minutesAway", "minutes", "mins", "eta",
                     "minutes_until", "countdown"),
    "status": ("status", "state", "schedule_relationship", "scheduleRelationship"),
    "realtime": ("realtime", "is_realtime", "isRealtime", "live", "rt"),
    "stop_id": ("stop_id", "stopId", "stop", "stop_code", "stopCode"),
    "service_date": ("service_date", "serviceDate", "service_day"),
}

# Fields where two spellings are two DIFFERENT identifiers for the same thing,
# so a mismatch on the primary is not yet a difference. `route` is short_name vs
# route_id ("11" vs "R11"); `stop_id` is the stop_id-vs-stop_code trap this whole
# project exists to keep straight. Reported when it happens rather than hidden.
_ALTERNATE_SPELLING_FIELDS = ("route", "stop_id")

# Compared when both sides carry them. `trip_id` is excluded on purpose: it is
# the matching key, so a difference in it means the records did not pair, not
# that a field differs.
_COMPARED_FIELDS = ("route", "headsign", "departure", "minutes_away", "status",
                    "realtime", "stop_id", "service_date")

# Keys the device may sensibly carry that are not departures. Excluded from
# `unmapped_fields` so the noise floor stays low.
_IGNORED_KEYS = {"index", "position", "id", "key", "raw", "source", "generated_at"}

# Keys under which a list of departures has been seen to hide. Checked in this
# order; the first that holds a list (or a dict containing one) wins.
_LIST_KEYS = ("render", "items", "departures", "results", "rows", "entries", "data",
              "expected", "actual", "board", "arrivals", "predictions", "list")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(key: Any) -> str:
    return _SLUG_RE.sub("", str(key).lower())


def _looks_like_record(value: Any) -> bool:
    """Does this dict look like one departure rather than a container?"""
    if not isinstance(value, dict):
        return False
    slugs = {_slug(k) for k in value}
    known = {_slug(alias) for aliases in _FIELD_ALIASES.values() for alias in aliases}
    return bool(slugs & known)


def _records(value: Any, side: str, assumptions: list[str]) -> list[dict[str, Any]]:
    """Coerce whatever arrived into a list of departure records.

    Every branch that guesses records what it guessed. A support tool is allowed
    to be wrong about the shape of a paste; it is not allowed to be wrong
    silently, because then the diff is wrong and looks authoritative.
    """
    if value is None:
        assumptions.append(f"{side}: nothing supplied; treated as an empty board.")
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            assumptions.append(f"{side}: empty string; treated as an empty board.")
            return []
        try:
            parsed = json.loads(text)
        except ValueError:
            assumptions.append(
                f"{side}: a string that is not JSON. Nothing could be read from it -- paste "
                "the log as JSON, or as a bare list of departures."
            )
            return []
        assumptions.append(f"{side}: parsed as a JSON string.")
        return _records(parsed, side, assumptions)
    if isinstance(value, list):
        records = [item for item in value if isinstance(item, dict)]
        if len(records) != len(value):
            assumptions.append(
                f"{side}: {len(value) - len(records)} non-object element(s) in the list were "
                "skipped; only objects can be compared field by field."
            )
        return records
    if isinstance(value, dict):
        for key in _LIST_KEYS:
            if key not in value:
                continue
            inner = value[key]
            if isinstance(inner, (list, str)):
                assumptions.append(f"{side}: took the departures from the {key!r} key.")
                return _records(inner, side, assumptions)
            if isinstance(inner, dict):
                # One level of nesting: a full `repro` result carries the board
                # at expected.items, and asking a user to unwrap it first is
                # exactly the refusal this function must not make.
                nested = _records(inner, side, assumptions)
                if nested:
                    assumptions.append(f"{side}: unwrapped {key!r} to reach the departures.")
                    return nested
        if _looks_like_record(value):
            assumptions.append(f"{side}: a single departure object, not a list; wrapped it.")
            return [value]
        assumptions.append(
            f"{side}: an object with no recognisable list of departures (keys: "
            + ", ".join(sorted(str(k) for k in value)[:8]) + "). Treated as empty."
        )
        return []
    assumptions.append(f"{side}: a {type(value).__name__}, which cannot hold departures. "
                       "Treated as empty.")
    return []


def _values(record: dict[str, Any], field: str) -> list[Any]:
    """Every value in `record` that could be `field`, in alias order."""
    by_slug: dict[str, Any] = {}
    for key, value in record.items():
        by_slug.setdefault(_slug(key), value)
    out = []
    for alias in _FIELD_ALIASES[field]:
        value = by_slug.get(_slug(alias))
        if value is not None and value != "":
            out.append(value)
    return out


def _mapped_key(record: dict[str, Any], field: str) -> str | None:
    """Which key on this record supplied `field`. Reported, never assumed."""
    by_slug = {_slug(k): k for k in record}
    for alias in _FIELD_ALIASES[field]:
        if _slug(alias) in by_slug:
            original = by_slug[_slug(alias)]
            if record.get(original) not in (None, ""):
                return str(original)
    return None


def _time_key(value: Any) -> str | None:
    """A departure time reduced to local HH:MM, or None if unreadable.

    Devices log times as ISO instants, as bare clock times, and as GTFS times
    past 24:00. All three describe the same minute on a screen, so all three
    reduce to the same key -- 24:12 and 00:12 are one departure, and treating
    them as two would report a phantom missing bus on every late-night repro.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            return _clock_key(parsed)
        match = re.search(r"(\d{1,2}):(\d{2})", text)
        if match:
            return f"{int(match.group(1)) % 24:02d}:{int(match.group(2)):02d}"
        return None
    if isinstance(value, datetime):
        return _clock_key(value)
    return None


def _clock_key(parsed: datetime) -> str:
    """Local agency clock reading for an instant.

    A device that logs in UTC writes 19:00Z for the same departure the oracle
    calls 14:00-05:00. Reading `.hour` off whichever form arrived made every
    such row a mismatch, and this function's own docstring promises the
    opposite. Offset-aware values are normalised to the agency zone; naive
    values are taken at face value, since a bare '14:00' is already local.
    """
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(AGENCY_TZ)
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def _compare_key(value: Any) -> Any:
    """Normalize a single field value for equality.

    Case and surrounding whitespace are formatting, not data: a device that
    renders "Chippewa Eastbound" in title case has not shown the rider anything
    different. Booleans and numbers are compared as themselves so `true` and
    `"true"` agree, which they do on a screen.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        return float(text)
    except ValueError:
        return text.casefold()


def _field_value(record: dict[str, Any], field: str) -> Any:
    values = _values(record, field)
    return values[0] if values else None


def _pair_key(record: dict[str, Any], mode: str) -> tuple[Any, ...]:
    time_key = _time_key(_field_value(record, "departure"))
    if mode == "trip_id":
        return (str(_field_value(record, "trip_id") or ""),)
    if mode == "departure_route":
        return (time_key or "", _compare_key(_field_value(record, "route") or ""))
    return (time_key or "",)


def _choose_mode(expected: list[dict[str, Any]],
                 actual: list[dict[str, Any]]) -> tuple[str, str]:
    """How to decide two records describe the same departure, and why.

    trip_id when both sides carry one and it is unique on each side -- it is the
    only identifier in GTFS that means one specific bus on one specific day.
    Otherwise time plus route, then time alone, then position. Position is last
    because it only means anything if the device rendered the same window in the
    same order, which is one of the things a bug report might be about.
    """
    def complete(records: list[dict[str, Any]], field: str) -> bool:
        return bool(records) and all(_field_value(r, field) is not None for r in records)

    def unique(records: list[dict[str, Any]]) -> bool:
        ids = [str(_field_value(r, "trip_id")) for r in records]
        return len(set(ids)) == len(ids)

    if complete(expected, "trip_id") and complete(actual, "trip_id") \
            and unique(expected) and unique(actual):
        return "trip_id", ("Paired on trip_id: both sides carry one on every row, and it is "
                           "the only GTFS identifier for one specific bus on one specific day.")
    times_ok = (all(_time_key(_field_value(r, "departure")) for r in expected)
                and all(_time_key(_field_value(r, "departure")) for r in actual))
    if times_ok and complete(expected, "route") and complete(actual, "route"):
        return "departure_route", ("Paired on departure time (local HH:MM) and route -- "
                                   "trip_id was absent or repeated on at least one side.")
    if times_ok:
        return "departure", ("Paired on departure time (local HH:MM) alone -- neither trip_id "
                             "nor a route was available on both sides.")
    return "position", ("Paired by position in the list: no departure time could be read on "
                        "both sides. Treat the pairing itself as a guess.")


def _summarise(record: dict[str, Any]) -> dict[str, Any]:
    """One record reduced to canonical fields, for the diff's own output."""
    return {field: _field_value(record, field) for field in
            ("route", "headsign", "departure", "minutes_away", "status", "trip_id")}


def diff_device(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """Diff an on-device capture against the oracle.

    `expected` is normally `repro(...)["render"]`, but the full `repro` result, a
    `departures` result, or a bare list all work. `actual` is whatever the app
    logged -- a bare list, a dict with an `items` key, a JSON string, camelCase
    keys, different words for the same field. Every one of those is accepted and
    every guess taken is reported in `assumptions`, because a support tool that
    rejects the user's paste over a key name has failed at its one job, and one
    that guesses silently is worse than one that refuses.

    Never raises. A paste that yields no records at all comes back as a result
    saying so, with a remedy -- the shape of the paste is itself a finding.
    """
    assumptions: list[str] = []
    exp_records = _records(expected, "expected", assumptions)
    act_records = _records(actual, "actual", assumptions)

    mode, why = _choose_mode(exp_records, act_records)
    assumptions.append(why)

    field_map = {
        "expected": _field_map(exp_records),
        "actual": _field_map(act_records),
    }
    renamed = sorted(
        f"{field}: expected `{field_map['expected'][field]}` vs device "
        f"`{field_map['actual'][field]}`"
        for field in _COMPARED_FIELDS
        if field_map["expected"].get(field) and field_map["actual"].get(field)
        and field_map["expected"][field] != field_map["actual"][field]
    )
    if renamed:
        assumptions.append("Different key names taken to mean the same field -- "
                           + "; ".join(renamed) + ".")

    unmapped = _unmapped(act_records)
    if unmapped:
        assumptions.append(
            "Device key(s) with no canonical meaning here, left uncompared: "
            + ", ".join(unmapped) + "."
        )

    # Greedy pairing over a key. A key can legitimately repeat (two buses on the
    # same route leave the same pole at the same minute more often than you
    # would think), so pairing consumes from a queue rather than a dict, and
    # both sides keep their original order.
    pools: dict[tuple[Any, ...], list[int]] = {}
    for index, record in enumerate(act_records):
        pools.setdefault(_pair_key(record, mode) if mode != "position" else (index,),
                         []).append(index)

    matched: list[dict[str, Any]] = []
    differing: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    consumed: set[int] = set()

    for index, exp in enumerate(exp_records):
        key = _pair_key(exp, mode) if mode != "position" else (index,)
        queue = pools.get(key) or []
        partner = None
        while queue:
            candidate = queue.pop(0)
            if candidate not in consumed:
                partner = candidate
                break
        if partner is None:
            missing.append({"expected": _summarise(exp), "key": list(key),
                            "detail": "The oracle expected this departure and the device did "
                                      "not show it."})
            continue
        consumed.add(partner)
        act = act_records[partner]
        fields, alternates = _compare_fields(exp, act)
        pair = {
            "key": list(key),
            "expected": _summarise(exp),
            "actual": _summarise(act),
            "fields": fields,
            "differing_fields": [f["field"] for f in fields if not f["equal"]],
            "matched_via_alternate_spelling": alternates,
        }
        matched.append(pair)
        if pair["differing_fields"]:
            differing.append(pair)
        for note in alternates:
            if note not in assumptions:
                assumptions.append(note)

    extra = [
        {"actual": _summarise(act_records[i]),
         "detail": "The device showed this and the oracle did not expect it."}
        for i in range(len(act_records)) if i not in consumed
    ]

    counts = {
        "expected": len(exp_records),
        "actual": len(act_records),
        "matched": len(matched),
        "missing": len(missing),
        "extra": len(extra),
        "differing": len(differing),
    }
    agrees = not missing and not extra and not differing
    return {
        "ok": agrees,
        "match": agrees,
        "verdict": _device_verdict(counts, agrees, differing),
        "remedy": None if agrees else _device_remedy(counts, differing),
        "key": mode,
        "matched": matched,
        "missing": missing,
        "extra": extra,
        "differing": differing,
        "counts": counts,
        "assumptions": assumptions,
        "field_map": field_map,
        "unmapped_fields": unmapped,
        "compared_fields": list(_COMPARED_FIELDS),
        "warnings": [] if agrees else [_device_verdict(counts, agrees, differing)],
        "notes": [
            "Every guess about the shape of the paste is in `assumptions`. If one of them is "
            "wrong the diff below is wrong, so read them before acting on it.",
            "Times are compared as local HH:MM, so a GTFS 24:12 and a rendered 00:12 are one "
            "departure rather than two.",
        ],
    }


def _field_map(records: list[dict[str, Any]]) -> dict[str, str]:
    """Which original key supplied each canonical field, across the records."""
    out: dict[str, str] = {}
    for field in _FIELD_ALIASES:
        for record in records:
            key = _mapped_key(record, field)
            if key:
                out[field] = key
                break
    return out


def _unmapped(records: list[dict[str, Any]]) -> list[str]:
    known = {_slug(alias) for aliases in _FIELD_ALIASES.values() for alias in aliases}
    known |= {_slug(k) for k in _IGNORED_KEYS}
    seen: set[str] = set()
    for record in records:
        for key in record:
            if _slug(key) not in known:
                seen.add(str(key))
    return sorted(seen)


def _compare_fields(exp: dict[str, Any],
                    act: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    fields: list[dict[str, Any]] = []
    alternates: list[str] = []
    for field in _COMPARED_FIELDS:
        exp_values, act_values = _values(exp, field), _values(act, field)
        if not exp_values or not act_values:
            continue  # a field only one side carries is not a disagreement
        exp_value, act_value = exp_values[0], act_values[0]
        equal = _compare_key(exp_value) == _compare_key(act_value)
        via_alternate = False
        if not equal and field in _ALTERNATE_SPELLING_FIELDS:
            # "11" against "R11" is route_short_name against route_id, not a
            # wrong route. Same for stop_code against stop_id.
            if {_compare_key(v) for v in exp_values} & {_compare_key(v) for v in act_values}:
                equal, via_alternate = True, True
                alternates.append(
                    f"{field}: matched via an alternate spelling ({exp_value!r} vs "
                    f"{act_value!r}) -- the same stop or route under its other identifier."
                )
        if field == "departure" and not equal:
            # Two clock renderings of the same minute are the same departure.
            exp_time, act_time = _time_key(exp_value), _time_key(act_value)
            if exp_time and act_time and exp_time == act_time:
                equal = True
        fields.append({
            "field": field,
            "expected": exp_value,
            "actual": act_value,
            "equal": equal,
            "matched_via_alternate_spelling": via_alternate,
        })
    return fields, alternates


def _device_verdict(counts: dict[str, int], agrees: bool,
                    differing: list[dict[str, Any]]) -> str:
    if agrees and not counts["expected"] and not counts["actual"]:
        return ("Both sides are empty. The oracle expected nothing and the device showed "
                "nothing, which is a correct blank screen -- run `repro` on its own to see "
                "WHY the window is empty.")
    if agrees:
        return (f"Match: the device rendered exactly the {counts['expected']} departure(s) "
                "the oracle expected, field for field.")
    parts = []
    if counts["missing"]:
        parts.append(f"{counts['missing']} departure(s) the oracle expected are MISSING from "
                     "the device")
    if counts["extra"]:
        parts.append(f"{counts['extra']} departure(s) on the device are not in the oracle")
    if differing:
        names = sorted({f for pair in differing for f in pair["differing_fields"]})
        parts.append(f"{len(differing)} matched departure(s) differ on {', '.join(names)}")
    return "Mismatch: " + "; ".join(parts) + "."


def _device_remedy(counts: dict[str, int], differing: list[dict[str, Any]]) -> str:
    if counts["missing"] and not counts["actual"]:
        return ("The device showed nothing at all. Confirm the oracle first with `stl support "
                "explain-empty --stop <stop> --at <instant>`; if the oracle is non-empty the "
                "bug is on the device, and the next question is which snapshot it shipped.")
    if counts["missing"]:
        return ("Departures are dropping out on the device. Check the service-date "
                "attribution first (`stl gtfs service-day <instant>`): a 24:xx trip filed "
                "against the wrong service date is the usual cause.")
    if counts["extra"]:
        return ("The device is showing departures the oracle does not expect. Confirm both "
                "sides are on the same snapshot -- `stl snapshot list` -- before assuming a "
                "logic bug.")
    names = sorted({f for pair in differing for f in pair["differing_fields"]})
    if names == ["departure"]:
        return ("Only the times differ, which is a timezone or DST bug rather than a "
                "selection bug. Compare `stl gtfs service-day` on both sides.")
    return (f"Matched departures disagree on {', '.join(names)}. Compare one trip end to end "
            "with `stl gtfs departures --stop <stop> --at <instant>` and the device log side "
            "by side.")


# ------------------------------------------------------------ bundle_report --

# Key names that would mean a bundle is not safe to paste in public. This
# project has no auth anywhere (spec 9), so this scan is expected to find
# nothing -- which is exactly why it is worth running: an assertion that
# something is safe is worth less than a check that says so.
_SECRET_KEY_PATTERN = re.compile(
    r"(secret|passwo?rd|token|api[_-]?key|authorization|auth[_-]?header|credential|"
    r"private[_-]?key|bearer|session[_-]?id|cookie)", re.IGNORECASE
)

# A store path under a home directory carries a username. Not a secret, but it
# is identifying, and a reader pasting this into a public issue deserves to know
# it is there rather than discover it afterwards.
_HOME_PATH_PATTERN = re.compile(r"(/Users/[^/\s\"]+|/home/[^/\s\"]+|C:\\\\Users\\\\[^\\\\\s\"]+)")


def _walk(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key in sorted(value, key=str):
            out.extend(_walk(value[key], f"{path}.{key}" if path else str(key)))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            out.extend(_walk(item, f"{path}[{index}]"))
        return out
    return [(path, value)]


def _scan(payload: Any) -> dict[str, Any]:
    """Look for anything that should not be pasted into a public issue."""
    suspicious: list[str] = []
    home_paths: list[str] = []
    for path, value in _walk(payload):
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        if _SECRET_KEY_PATTERN.search(leaf):
            suspicious.append(path)
        match = _HOME_PATH_PATTERN.search(str(value))
        if match:
            home_paths.append(f"{path} = {match.group(0)}")
    return {"suspicious_keys": sorted(set(suspicious)),
            "local_paths": sorted(set(home_paths))}


def _file(name: str, payload: Any) -> dict[str, Any]:
    content = canonical_json(payload) if not isinstance(payload, str) else payload
    return {
        "name": name,
        "content": content,
        "bytes": len(content.encode("utf-8")),
        "sha256": sha256_text(content),
    }


def bundle_report(store_summary: dict[str, Any], config_summary: dict[str, Any],
                  versions: dict[str, Any], recent_assertions: dict[str, Any] | None,
                  now: datetime) -> dict[str, Any]:
    """The contents of a support bundle, for attaching to a GitHub issue.

    Pure: this builds the files, `service.py` zips them. Returns `files` as a
    list of `{name, content, bytes, sha256}` ready to write verbatim.

    Nothing here is redacted because nothing in this project is sensitive --
    there is no auth anywhere in it (spec 9), so there is no token to leak. That
    claim is CHECKED rather than asserted: the payload is scanned for
    secret-shaped keys and for home-directory paths, and the result of that scan
    travels with the bundle. Saying "safe to paste in public" without having
    looked would be the same sentence with none of the value.
    """
    store = store_summary or {}
    config = config_summary or {}
    assertions_payload: dict[str, Any]
    if recent_assertions is None:
        assertions_payload = {
            "run": False,
            "detail": "The assumption suite was not run for this bundle, so nothing here "
                      "says whether the feed still satisfies what the app assumes.",
            "remedy": "Run `stl assert run` and regenerate the bundle before attaching it.",
        }
    else:
        assertions_payload = {
            "run": True,
            "ok": bool(recent_assertions.get("ok")),
            "total": recent_assertions.get("total"),
            "passed": recent_assertions.get("passed"),
            "failed": recent_assertions.get("failed"),
            "skipped": recent_assertions.get("skipped"),
            "opportunities": recent_assertions.get("opportunities"),
            "violations": recent_assertions.get("violations") or [],
            "items": recent_assertions.get("items") or [],
        }

    snapshot_ids = sorted(
        {str(s.get("snapshot_id")) for s in (store.get("items") or []) if s.get("snapshot_id")}
    )
    manifest = {
        # `now` is injected (spec 2.7), so this stays deterministic under test
        # while still telling a maintainer reading the issue in six months when
        # the bundle was taken -- which for a support artifact is the point.
        "generated_at": now.isoformat(),
        "generated_by": f"stl {__version__}",
        "versions": dict(sorted((str(k), v) for k, v in (versions or {}).items())),
        "snapshot_ids": snapshot_ids,
        "snapshot_count": store.get("snapshots", len(snapshot_ids)),
        "pins": store.get("pins") or {},
        "config_path": config.get("config_path"),
        "sources": sorted(str(s.get("source")) for s in (config.get("items") or [])),
        "pages": sorted(str(p) for p in (config.get("pages") or [])),
        "assertions_included": recent_assertions is not None,
    }

    scan = _scan({"manifest": manifest, "store": store, "config": config,
                  "assertions": assertions_payload})
    safe = not scan["suspicious_keys"]

    warnings: list[str] = []
    if scan["suspicious_keys"]:
        warnings.append(
            "Key(s) with secret-shaped names are present: "
            + ", ".join(scan["suspicious_keys"])
            + ". This project has no auth anywhere, so this is unexpected -- inspect those "
              "values before attaching the bundle to a public issue."
        )
    if scan["local_paths"]:
        warnings.append(
            f"{len(scan['local_paths'])} value(s) contain a home-directory path, which "
            "carries a username. Not a secret, but it is identifying: edit them out of the "
            "issue body if you would rather not publish it."
        )
    if recent_assertions is None:
        warnings.append(
            "No recent assertion results were supplied. A bundle without them cannot say "
            "whether the feed still satisfies the app's assumptions -- run `stl assert run` "
            "first."
        )

    readme = _bundle_readme(manifest, assertions_payload, scan, safe, now)
    files = [
        _file("README.md", readme),
        _file("manifest.json", manifest),
        _file("snapshots.json", store),
        _file("config.json", config),
        _file("assertions.json", assertions_payload),
    ]
    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "generated_by": f"stl {__version__}",
        "manifest": manifest,
        "files": files,
        "file_names": [f["name"] for f in files],
        "total_bytes": sum(f["bytes"] for f in files),
        "safe_to_publish": safe,
        "redaction": {
            "redacted": [],
            "why": "Nothing was redacted because nothing in this project is sensitive: there "
                   "is no authentication anywhere in it (spec 9), every source is a public "
                   "URL, and the snapshot store holds published open data. This bundle is "
                   "safe to paste into a public issue.",
            "checked_for": ["secret-shaped key names", "home-directory paths"],
            "findings": scan,
        },
        "attach_to": "a GitHub issue on the tool repo",
        "warnings": warnings,
        "notes": [
            "`files` is the bundle verbatim: write each `content` under its `name` and zip "
            "the directory. The hashes are over exactly those bytes.",
            "Every file is canonical JSON (sorted keys, fixed indent), so two bundles taken "
            "from the same state diff cleanly (spec 2.8).",
        ],
    }


def _bundle_readme(manifest: dict[str, Any], assertions_payload: dict[str, Any],
                   scan: dict[str, Any], safe: bool, now: datetime) -> str:
    lines = [
        "# stl-transit support bundle",
        "",
        f"Generated by `stl support bundle` ({__version__}) at {now.isoformat()}.",
        "",
        "## Is this safe to paste in public?",
        "",
        "Yes. There is no authentication anywhere in this project (spec 9): every source is "
        "a public URL and the snapshot store holds published open data, so there is no "
        "credential in here to leak. Nothing has been redacted.",
        "",
        "That is checked rather than claimed. This bundle was scanned for secret-shaped key "
        f"names ({len(scan['suspicious_keys'])} found) and for home-directory paths, which "
        f"carry a username ({len(scan['local_paths'])} found).",
        "",
    ]
    if not safe:
        lines += ["> **Read the warnings before attaching this.** The scan found key(s) with "
                  "secret-shaped names: " + ", ".join(scan["suspicious_keys"]) + ".", ""]
    if scan["local_paths"]:
        lines += ["> A few values contain a local path with a username in it. Not a secret, "
                  "but edit them out of the issue body if you would rather not publish it.",
                  ""]
    lines += [
        "## What is in here",
        "",
        "| File | Contents |",
        "|---|---|",
        "| `manifest.json` | Tool versions, snapshot ids, pins, configured sources and pages |",
        "| `snapshots.json` | The local snapshot store: what was fetched, when, and its hash |",
        "| `config.json` | Resolved configuration, including the path it was loaded from |",
        "| `assertions.json` | The most recent `stl assert run`, or a note that it was not run |",
        "",
        "## State at the time of the report",
        "",
        f"- Snapshots: {manifest['snapshot_count']}",
        f"- Pins: {', '.join(sorted(manifest['pins'])) or 'none'}",
        f"- Sources: {', '.join(manifest['sources']) or 'none'}",
        f"- Versions: "
        + (", ".join(f"{k} {v}" for k, v in manifest["versions"].items()) or "not recorded"),
    ]
    if assertions_payload.get("run"):
        lines.append(
            f"- Assertions: {assertions_payload.get('passed')} passed, "
            f"{assertions_payload.get('failed')} failed, "
            f"{assertions_payload.get('skipped')} skipped"
        )
    else:
        lines.append("- Assertions: NOT RUN. " + str(assertions_payload.get("remedy", "")))
    lines += [
        "",
        "## Reproducing whatever this issue is about",
        "",
        "```",
        "stl snapshot list",
        "stl support repro --stop <stop> --at <iso8601>",
        "stl support explain-empty --stop <stop> --at <iso8601>",
        "```",
        "",
    ]
    return "\n".join(lines)
