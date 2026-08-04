"""Merge realtime predictions onto scheduled departures.

Produces precisely what the app should render, which makes it the RT-aware
sibling of gtfs.departures and the fixture source for the rt_* oracle cases.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ...io.clock import AGENCY_TZ

# A transit delay outside +/-12 hours is a decoding fault, not a schedule fact.
# The specific failure this guards is an unsigned read of a signed int32, which
# turns -180 into 1.8e19 and blows up datetime.fromtimestamp.
MAX_PLAUSIBLE_DELAY_SECONDS = 12 * 3600


def _plausible_delay(delay: int) -> bool:
    return -MAX_PLAUSIBLE_DELAY_SECONDS <= delay <= MAX_PLAUSIBLE_DELAY_SECONDS


def index_trip_updates(decoded: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """trip_id -> TripUpdate, for joining against scheduled trips."""
    out: dict[str, dict[str, Any]] = {}
    for entity in decoded.get("entities", []):
        tu = entity.get("trip_update")
        if not tu:
            continue
        trip_id = (tu.get("trip") or {}).get("trip_id")
        if trip_id:
            out[trip_id] = tu
    return out


def _stop_delay(
    tu: dict[str, Any], stop_id: str, stop_sequence: str | None
) -> tuple[int | None, int | None, str]:
    """Find the prediction applying to one stop: (delay_seconds, epoch, relationship).

    GTFS-RT allows a producer to give a prediction at only some stops; the spec
    says a StopTimeUpdate applies until the next one. So: prefer an exact stop
    match, then fall back to the trip-level delay.

    A StopTimeEvent may carry EITHER a `delay` relative to the schedule OR an
    absolute `time` as a POSIX timestamp, and the spec treats them as equally
    valid. An earlier version recognised `time` and returned nothing for it,
    with a comment claiming the caller handled it -- the caller had no such
    branch, so those stops were rendered as `realtime: true` carrying the
    scheduled time. A rider was shown a scheduled time labelled live.
    """
    updates = tu.get("stop_time_update", []) or []
    for u in updates:
        if u.get("stop_id") == stop_id or (
            stop_sequence and str(u.get("stop_sequence")) == str(stop_sequence)
        ):
            rel = u.get("schedule_relationship", "SCHEDULED")
            if rel == "SKIPPED":
                return None, None, "SKIPPED"
            event = u.get("departure") or u.get("arrival") or {}
            if event.get("delay") is not None:
                return int(event["delay"]), None, rel
            if event.get("time") is not None:
                return None, int(event["time"]), rel
            return None, None, rel
    if tu.get("delay") is not None:
        return int(tu["delay"]), None, "SCHEDULED"
    return None, None, "NO_DATA"


def merge(scheduled: dict[str, Any], decoded: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay RT onto a `departures()` result."""
    if decoded is None:
        out = dict(scheduled)
        out["realtime"] = {
            "available": False,
            "reason": "No realtime snapshot supplied.",
        }
        for item in out.get("items", []):
            item["realtime"] = False
            item["status"] = "SCHEDULED_ONLY"
        out.setdefault("warnings", []).append(
            "Realtime unavailable - times shown are scheduled. The app must say so too."
        )
        return out

    by_trip = index_trip_updates(decoded)
    header_ts = decoded.get("header", {}).get("timestamp")
    matched = 0
    items = []
    warnings: list[str] = []
    for item in scheduled.get("items", []):
        rec = dict(item)
        tu = by_trip.get(rec["trip_id"])
        if tu is None:
            rec["realtime"] = False
            rec["status"] = "SCHEDULED_ONLY"
        else:
            matched += 1
            delay, epoch, rel = _stop_delay(tu, rec["stop_id"], rec.get("stop_sequence"))
            rec["realtime"] = True
            rec["status"] = rel
            base = datetime.fromisoformat(rec["departure_local"])
            zone = base.tzinfo if isinstance(base.tzinfo, ZoneInfo) else AGENCY_TZ
            if rel == "SKIPPED":
                rec["predicted_local"] = None
            elif epoch is not None:
                # Absolute prediction. Derive the delay from it rather than the
                # other way round, so downstream code has one shape to handle.
                predicted = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(zone)
                derived = int(
                    (predicted.astimezone(timezone.utc) - base.astimezone(timezone.utc))
                    .total_seconds()
                )
                if not _plausible_delay(derived):
                    rec["status"] = "IMPLAUSIBLE_DELAY"
                    rec["delay_seconds"] = derived
                    rec["predicted_local"] = None
                    warnings.append(
                        f"Trip {rec['trip_id']} gave an absolute time implying "
                        f"{derived}s of delay, outside +/-{MAX_PLAUSIBLE_DELAY_SECONDS}s."
                    )
                else:
                    rec["delay_seconds"] = derived
                    rec["delay_basis"] = "absolute_time"
                    rec["predicted_local"] = predicted.isoformat()
                    rec["minutes_away_predicted"] = rec["minutes_away"] + round(derived / 60)
            elif delay is None:
                # Matched a TripUpdate, but it carries no prediction for this
                # stop. Say so, rather than let a scheduled time pass for live.
                rec["status"] = rel if rel != "SCHEDULED" else "NO_PREDICTION"
                rec["predicted_local"] = None
            elif not _plausible_delay(delay):
                # A delay outside this range is a decoder fault, not a late bus.
                # Report it as a data problem rather than rendering a departure
                # in the year 584 billion -- or crashing on OverflowError.
                rec["status"] = "IMPLAUSIBLE_DELAY"
                rec["delay_seconds"] = delay
                rec["predicted_local"] = None
                warnings.append(
                    f"Trip {rec['trip_id']} reported delay {delay}s, outside "
                    f"+/-{MAX_PLAUSIBLE_DELAY_SECONDS}s. Treated as no prediction. "
                    "A value near 2^64 means an unsigned decode of a signed field."
                )
            else:
                rec["delay_seconds"] = delay
                rec["delay_basis"] = "delay"
                # Add in UTC, then convert back through the agency zone. Adding
                # to a fixed-offset datetime keeps a stale offset across a DST
                # boundary, so a bus predicted past 02:00 on a transition night
                # would be reported an hour off.
                predicted = (
                    base.astimezone(timezone.utc) + timedelta(seconds=delay)
                ).astimezone(zone)
                rec["predicted_local"] = predicted.isoformat()
                rec["minutes_away_predicted"] = rec["minutes_away"] + round(delay / 60)
        items.append(rec)

    out = dict(scheduled)
    out["items"] = items
    # Denominator is the page actually inspected, not scheduled["total"] --
    # naming it explicitly stops a reader assuming it covers the whole result.
    inspected = len(items)
    out["realtime"] = {
        "available": True,
        "header_timestamp": header_ts,
        "header_timestamp_iso": (
            datetime.fromtimestamp(header_ts, tz=timezone.utc).isoformat()
            if header_ts is not None else None
        ),
        "trip_updates_in_feed": len(by_trip),
        "departures_inspected": inspected,
        "matched_departures": matched,
        "match_rate": round(matched / inspected, 4) if inspected else None,
        "match_rate_basis": "matched_departures / departures_inspected (this page only)",
    }
    if warnings:
        out.setdefault("warnings", []).extend(warnings)
    return out
