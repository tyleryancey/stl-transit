"""Golden-fixture generation for the Kotlin pure-JVM test gate.

The fixtures are the product. Everything else in this repo is convenience.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import UsageError
from ..io.clock import AGENCY_TZ
from ..io.store import Store
from . import service

# Each case exists because it can break independently of every other case.
CASES: list[dict[str, Any]] = [
    {"id": "weekday_midday", "why": "The boring baseline. If this fails nothing else matters.",
     "window_minutes": 90},
    {"id": "saturday", "why": "Separate calendar.txt weekday pattern.", "window_minutes": 90},
    {"id": "sunday", "why": "Separate again, and the pattern holidays map onto.",
     "window_minutes": 90},
    {"id": "late_night_rollover",
     "why": "Departures encoded 24:xx/25:xx belong to the PREVIOUS service date.",
     "window_minutes": 120},
    {"id": "just_after_midnight",
     "why": "Queried at 00:15, the answers come from yesterday's service date.",
     "window_minutes": 90},
    {"id": "first_departure_of_day", "why": "Window opening before service starts.",
     "window_minutes": 180},
    {"id": "last_departure_of_day", "why": "Correct answer is often an empty list.",
     "window_minutes": 60},
    {"id": "holiday_bus_sunday",
     "why": "Labor Day: MetroBus runs SUNDAY service on a Monday.", "window_minutes": 120},
    {"id": "holiday_rail_weekend",
     "why": "Same date, MetroLink runs WEEKEND service -- a different concept from Sunday.",
     "window_minutes": 120},
    {"id": "holiday_normal_weekday",
     "why": "Veterans Day is NOT a service change. Guards against over-eager holiday logic.",
     "window_minutes": 90},
    {"id": "dst_spring_forward", "why": "The 02:00-03:00 gap. noon-minus-12h handles it; "
                                        "local-midnight arithmetic does not.",
     "window_minutes": 240},
    {"id": "dst_fall_back", "why": "The repeated hour.", "window_minutes": 240},
    {"id": "multimodal_stop", "why": "One stop, bus and rail together.", "window_minutes": 60},
    {"id": "no_service_at_stop", "why": "Valid stop, zero departures. Must be empty, not an error.",
     "window_minutes": 30},
    {"id": "unknown_stop_code", "why": "A number that resolves to nothing.", "window_minutes": 60},
    {"id": "feed_expired", "why": "as_of past feed_end_date must be a distinct visible state.",
     "window_minutes": 90},
    {"id": "rt_delayed", "why": "Replayed realtime, bus running late.", "window_minutes": 60},
    {"id": "rt_cancelled", "why": "schedule_relationship = CANCELED.", "window_minutes": 60},
    {"id": "rt_absent", "why": "Realtime fetch fails: degrade to scheduled and SAY SO.",
     "window_minutes": 60},
]


def list_cases() -> dict[str, Any]:
    """The oracle case list, each with the failure mode it pins down."""
    return {"ok": True, "provenance": None, "warnings": [], "notes": [],
            "items": CASES, "count": len(CASES), "total": len(CASES)}


def generate(spec_path: str | None = None, out_dir: str = "fixtures",
             case: str | None = None, snapshot: str | None = None,
             source: str = service.DEFAULT_GTFS_SOURCE,
             store: Store | None = None) -> dict[str, Any]:
    """Compute expected outputs for each case and write committed fixture JSON.

    `spec_path` is a JSON file binding each case id to concrete inputs
    (stop, as_of). Without it, only cases with inputs already bound are
    generated -- there is no guessing about which stop is 'multimodal'.
    """
    store = store or Store()
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    bindings: dict[str, dict[str, Any]] = {}
    if spec_path:
        bindings = json.loads(Path(spec_path).expanduser().read_text())

    written, skipped = [], []
    for c in CASES:
        cid = c["id"]
        if case and cid != case:
            continue
        binding = bindings.get(cid)
        if not binding:
            skipped.append({"case_id": cid, "reason": "no input binding in the spec file"})
            continue
        at = binding["at"]
        stop = binding["stop"]
        window = binding.get("window_minutes", c["window_minutes"])
        try:
            result = service.gtfs_departures(
                stop=stop, at=at, window_minutes=window, limit=200,
                snapshot=snapshot, source=source, store=store)
            expected = result["items"]
            error = None
        except Exception as exc:  # a case may legitimately expect an error
            expected, error = [], {"type": type(exc).__name__, "message": str(exc)}
            result = {"provenance": None}

        fixture = {
            "case_id": cid,
            "why": c["why"],
            "generated_by": f"stl {__version__}",
            "generated_at": datetime.now(AGENCY_TZ).isoformat(),
            "provenance": result.get("provenance"),
            "input": {"stop": stop, "at": at, "window_minutes": window},
            "expected_error": error,
            "expected": [
                {k: v for k, v in item.items()
                 if k in ("route_short_name", "headsign", "departure_local", "gtfs_time",
                          "service_date", "trip_id", "direction_id", "after_midnight")}
                for item in expected
            ],
        }
        path = out / f"{cid}.json"
        # sort_keys + fixed indent: two runs against one snapshot must be
        # byte-identical, or `oracle verify` is not a meaningful drift check.
        path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
        written.append({"case_id": cid, "path": str(path), "departures": len(fixture["expected"])})

    return {"ok": True, "provenance": None, "notes": [],
            "warnings": ([f"{len(skipped)} case(s) skipped for lack of input bindings."]
                         if skipped else []),
            "written": written, "skipped": skipped, "out_dir": str(out)}


# The fields a fixture pins. Fixed, not derived from the golden's first item:
# deriving it meant an empty golden had no key set, so an empty expectation
# could never be compared against a non-empty result on equal terms.
FIXTURE_FIELDS = (
    "route_short_name", "headsign", "departure_local", "gtfs_time",
    "service_date", "trip_id", "direction_id", "after_midnight",
)


def _project(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k in FIXTURE_FIELDS}


def verify(fixtures_dir: str = "fixtures", snapshot: str | None = None,
           source: str = service.DEFAULT_GTFS_SOURCE,
           store: Store | None = None) -> dict[str, Any]:
    """Recompute every fixture against the current snapshot and report drift.

    A case that legitimately raises (unknown stop code, expired feed) is a
    first-class expectation, not a failure: `generate` records it under
    `expected_error` and this compares against it. Treating a raise as
    automatic drift made six of the nineteen cases permanently red, and a
    drift check that is always red is a drift check nobody reads.
    """
    store = store or Store()
    d = Path(fixtures_dir).expanduser()
    if not d.is_dir():
        raise UsageError(
            f"No fixtures directory at {d}.",
            remedy="Generate fixtures first with `stl oracle generate --out <dir> "
            "--spec <bindings.json>`, or point --fixtures at the right directory.",
            fixtures_dir=str(d),
        )
    results, drifted = [], 0
    for path in sorted(d.glob("*.json")):
        golden = json.loads(path.read_text())
        inp = golden["input"]
        expected_error = golden.get("expected_error")
        actual_error = None
        try:
            fresh = service.gtfs_departures(
                stop=inp["stop"], at=inp["at"], window_minutes=inp["window_minutes"],
                limit=200, snapshot=snapshot, source=source, store=store)
            actual = [_project(item) for item in fresh["items"]]
        except Exception as exc:  # noqa: BLE001 - the raise itself is the datum
            actual = []
            actual_error = {"type": type(exc).__name__, "message": str(exc)}

        # Compare on error TYPE, not message. Messages carry snapshot ids and
        # counts that change legitimately; the type is the behavioural contract.
        error_matches = (
            (expected_error or {}).get("type") == (actual_error or {}).get("type")
        )
        same = error_matches and actual == golden["expected"]
        if not same:
            drifted += 1
        results.append(
            {
                "case_id": golden["case_id"],
                "matches": same,
                "expected_count": len(golden["expected"]),
                "actual_count": len(actual),
                "expected_error": (expected_error or {}).get("type"),
                "actual_error": (actual_error or {}).get("type"),
                "reason": _drift_reason(same, expected_error, actual_error,
                                        golden["expected"], actual),
                "golden_snapshot": (golden.get("provenance") or {}).get("snapshot_id"),
            }
        )
    return {"ok": drifted == 0, "provenance": None, "notes": [],
            "warnings": ([f"{drifted} fixture(s) no longer match the current feed."]
                         if drifted else []),
            "items": results, "drifted": drifted, "checked": len(results)}


def _drift_reason(same: bool, expected_error, actual_error, expected, actual) -> str | None:
    """Name the branch that failed, so a red run is actionable without a diff."""
    if same:
        return None
    if expected_error and not actual_error:
        return (f"Expected {expected_error['type']} but the call succeeded with "
                f"{len(actual)} departure(s). The condition the case pins down no "
                "longer holds.")
    if actual_error and not expected_error:
        return (f"Expected {len(expected)} departure(s) but the call raised "
                f"{actual_error['type']}: {actual_error['message']}")
    if actual_error and expected_error:
        return (f"Error type changed: {expected_error['type']} -> {actual_error['type']}")
    if len(expected) != len(actual):
        return f"Departure count changed: {len(expected)} -> {len(actual)}"
    for i, (e, a) in enumerate(zip(expected, actual)):
        if e != a:
            fields = sorted(k for k in set(e) | set(a) if e.get(k) != a.get(k))
            return f"Departure {i} differs on: {', '.join(fields)}"
    return "Contents differ."
