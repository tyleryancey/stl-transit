"""The `report` group (spec 6.11): three documents, one composition layer.

Nothing here opens a connection, fetches a feed, or reads a file. `service.py`
does the I/O and hands the results in as plain dicts, which is what lets every
function in this module be tested with no snapshot store and no network at all.

The three reports answer three different questions and are shaped accordingly:

- `brief` -- "is the feed all right *right now*?" One screen, read daily.
- `handoff` -- "what do I already know?" A markdown block for pasting into
  CLAUDE.md, read once by a future reader who was not here for the
  investigation and has no reason to trust an undated claim.
- `changelog` -- "what moved since the baseline?" Readable prose over the diff
  group's structured output.

Two rules shape all three:

1. **Every optional input is genuinely optional.** A developer may not have
   fetched realtime or captured pages yet, and a brief that refuses to render
   without them is a brief nobody runs. A missing input is reported as missing
   -- never as passing, and never as a reason to render nothing.
2. **Every report names the next command.** A report that states facts without
   naming what to run makes the reader redo the triage the report just did.

Pure logic (spec 2.1): never prints, never exits, never prompts. Deterministic
(spec 2.8): fixed sort orders throughout, and `now` is injected by the caller
(spec 2.7) so two runs over the same inputs produce byte-identical output.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ... import __version__

__all__ = ["SHARP_EDGES", "brief", "changelog", "handoff"]

# Three states, deliberately three and not five. This is the number a person
# reads at a glance before deciding whether to open a terminal, and every extra
# rung just moves the judgement call from the report back to the reader.
OK = "ok"
ATTENTION = "attention"
BROKEN = "broken"

_STATUS_RANK = {OK: 0, ATTENTION: 1, BROKEN: 2}
_RANK_STATUS = {rank: name for name, rank in _STATUS_RANK.items()}

# Finding severities reuse the status words, plus `info` for something true and
# worth printing that changes nothing about what to do next.
INFO = "info"
_SEVERITY_RANK = {BROKEN: 0, ATTENTION: 1, INFO: 2}

# When to run the thing. Ordering next_actions by anything else -- by the order
# the checks happened to run in, say -- puts "regenerate the fixtures" above
# "the feed expired four days ago", which is the exact triage the report is
# supposed to have done for the reader.
URGENCIES = ("now", "today", "soon", "routine")
_URGENCY_RANK = {name: i for i, name in enumerate(URGENCIES)}

# The `assert run` severities (spec 6.10), ranked so the worst violation leads.
_ASSERTION_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "opportunity": 3}

# Matches the `feed_not_expiring` assumption and `inspect.coverage`'s own
# warning threshold. The same number lives in three places and they have to
# agree, or the brief contradicts the assertion suite in the same terminal.
EXPIRY_WARN_DAYS = 7

# How many examples any one section of the brief embeds. A brief is one screen;
# the full lists are one command away and named in `next_actions`.
MAX_LISTED = 5


# ------------------------------------------------------------------ helpers --

def _unchecked(area: str, detail: str, command: str) -> dict[str, Any]:
    """A section that was not measured.

    `checked: False` rather than an absent key or an empty result: a reader
    skimming for zeros must never mistake "nobody looked" for "nothing wrong",
    and neither must a `render.py` template.
    """
    return {
        "checked": False,
        "area": area,
        "detail": detail,
        "remedy": f"Run `{command}` and pass the result in.",
        "command": command,
    }


def _listed(values: list[str]) -> str:
    """A bounded, comma-joined sample for a one-line summary."""
    shown = values[:MAX_LISTED]
    tail = f", +{len(values) - len(shown)} more" if len(values) > len(shown) else ""
    return ", ".join(shown) + tail


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


# -------------------------------------------------------------------- brief --

def _feed_block(coverage: dict[str, Any]) -> dict[str, Any]:
    cov = coverage or {}
    info = cov.get("feed_info") or {}
    return {
        "checked": bool(cov),
        "today": cov.get("today"),
        "service_start": cov.get("service_start"),
        "service_end": cov.get("service_end"),
        "days_remaining": cov.get("days_remaining"),
        "expired": bool(cov.get("expired")),
        # feed_info is optional in GTFS; an empty string reads better than a
        # null in a one-screen report and formats identically in both consumers.
        "feed_version": str(info.get("feed_version") or ""),
        "publisher": str(info.get("feed_publisher_name") or ""),
        "warning": cov.get("warning"),
    }


def _assertions_block(assertions: dict[str, Any]) -> dict[str, Any]:
    a = assertions or {}
    violations = sorted(
        (dict(v) for v in (a.get("violations") or [])),
        key=lambda v: (_ASSERTION_SEVERITY_RANK.get(str(v.get("severity")), 9),
                       str(v.get("id", ""))),
    )
    return {
        "checked": True,
        "total": int(a.get("total") or 0),
        "passed": int(a.get("passed") or 0),
        "failed": int(a.get("failed") or 0),
        "skipped": int(a.get("skipped") or 0),
        "opportunities": int(a.get("opportunities") or 0),
        "violated_ids": [str(v.get("id", "")) for v in violations],
        "violations": [
            {"id": str(v.get("id", "")), "severity": str(v.get("severity", "")),
             "observed": v.get("observed"), "expected": v.get("expected"),
             "detail": str(v.get("detail", "")), "breaks": str(v.get("breaks", ""))}
            for v in violations[:MAX_LISTED]
        ],
        "violations_truncated": len(violations) > MAX_LISTED,
    }


def _realtime_block(rt_health: dict[str, Any]) -> dict[str, Any]:
    h = rt_health or {}
    # Sorted by entity, not left in the order the caller polled: two runs must
    # agree byte for byte (spec 2.8).
    items = sorted((dict(i) for i in (h.get("items") or [])),
                   key=lambda i: str(i.get("entity", "")))
    unavailable = [str(i.get("entity", "")) for i in items if not i.get("available")]
    stale = [str(i.get("entity", "")) for i in items if i.get("stale")]
    ages = {str(i.get("entity", "")): float(i["age_seconds"])
            for i in items if i.get("age_seconds") is not None}
    oldest = max(ages, key=lambda e: (ages[e], e)) if ages else None
    return {
        "checked": True,
        "checked_at": h.get("checked_at"),
        "entities": [
            {"entity": str(i.get("entity", "")), "available": bool(i.get("available")),
             "age_seconds": i.get("age_seconds"), "stale": bool(i.get("stale")),
             "entity_count": i.get("entity_count"), "snapshot_id": i.get("snapshot_id")}
            for i in items
        ],
        "unavailable": unavailable,
        "stale": stale,
        "oldest_entity": oldest,
        "oldest_age_seconds": round(ages[oldest], 1) if oldest else None,
        "healthy": bool(items) and not unavailable and not stale,
    }


def _drift_block(web_check: dict[str, Any]) -> dict[str, Any]:
    w = web_check or {}
    items = [dict(i) for i in (w.get("items") or [])]
    changed = sorted(str(p) for p in (w.get("changed_pages")
                                      or [i.get("page") for i in items if i.get("changed")]))
    alarming = sorted(str(i.get("page")) for i in items
                      if i.get("changed") and i.get("severity") == "alarming")
    failed = sorted(str(i.get("page")) for i in items
                    if i.get("status") == "extraction_failed")
    never = sorted(str(i.get("page")) for i in items if i.get("status") == "never_captured")
    return {
        "checked": True,
        "drift_detected": bool(w.get("drift_detected")),
        "headline": str(w.get("headline") or ""),
        "changed": changed,
        "alarming": alarming,
        "extraction_failed": failed,
        "never_captured": never,
        "pages": int((w.get("counts") or {}).get("pages") or len(items)),
    }


def _snapshots_block(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [dict(s) for s in (snapshots or [])]
    # Defensive re-sort. The store returns newest-first, and a caller that got
    # it backwards would otherwise be told the oldest snapshot is the current
    # one -- the single fact everything else in this report hangs off.
    rows.sort(key=lambda s: (str(s.get("fetched_at") or ""), str(s.get("snapshot_id") or "")),
              reverse=True)
    by_kind: dict[str, int] = {}
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = str(row.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        latest.setdefault(kind, {"snapshot_id": row.get("snapshot_id"),
                                 "source": row.get("source"),
                                 "fetched_at": row.get("fetched_at"),
                                 "pin": row.get("pin")})
    pinned = sorted(
        ({"pin": str(s.get("pin")), "snapshot_id": s.get("snapshot_id"),
          "kind": str(s.get("kind") or "unknown")} for s in rows if s.get("pin")),
        key=lambda p: (p["pin"], str(p["snapshot_id"])),
    )
    return {
        "count": len(rows),
        "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
        "latest": {k: latest[k] for k in sorted(latest)},
        "pinned": pinned,
    }


def brief(coverage: dict[str, Any], assertions: dict[str, Any] | None,
          rt_health: dict[str, Any] | None, web_check: dict[str, Any] | None,
          snapshots: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """One screen: the state of the feed right now (spec 6.11).

    Feed version and expiry, assertion pass/fail, realtime health, page drift,
    and what to do next. Everything is passed in -- `service.py` does the I/O,
    this does the composition.

    Each of `assertions`, `rt_health` and `web_check` may be None because a
    developer may not have run `assert run`, fetched realtime, or captured the
    pages yet. A brief that refuses to render without them is a brief nobody
    runs, so an absent input becomes an entry in `not_checked` with the command
    that would supply it, and never a silent pass.

    `status` is one of "ok", "attention", "broken":

    - **broken** -- an assumption the app is built on is violated, or the feed
      has expired. Something shipped is now wrong.
    - **attention** -- drift: a page changed, realtime is stale, the feed is
      inside its expiry window. Nothing is wrong yet and something will be.
    - **ok** -- nothing that was measured is wrong. It says nothing about what
      was not measured, which is why `not_checked` is beside it.

    `next_actions` is ordered by urgency and is never empty: on a wholly green
    feed the next action is the one that keeps it green.
    """
    findings: list[dict[str, str]] = []
    actions: list[dict[str, Any]] = []
    not_checked: list[dict[str, str]] = []

    def note(severity: str, area: str, detail: str) -> None:
        findings.append({"severity": severity, "area": area, "detail": detail})

    def act(urgency: str, command: str, why: str) -> None:
        # Deduped on the command. Several findings routinely point at the same
        # fix, and a list that says `stl snapshot fetch metro_gtfs` three times
        # reads as noise rather than as urgency. The most urgent reason wins.
        for existing in actions:
            if existing["command"] == command:
                if _URGENCY_RANK[urgency] < _URGENCY_RANK[existing["urgency"]]:
                    existing["urgency"], existing["why"] = urgency, why
                return
        actions.append({"urgency": urgency, "command": command, "why": why,
                        "_seq": len(actions)})

    # ---- feed validity: the fact this report leads with
    feed = _feed_block(coverage)
    days = feed["days_remaining"]
    if not feed["checked"]:
        note(ATTENTION, "feed", "No coverage was supplied, so feed validity is unknown -- "
                                "the one fact this report exists to lead with.")
        act("today", "stl gtfs coverage", "Establish whether the bundled feed is still "
                                          "inside its service window.")
        not_checked.append({"area": "feed", "detail": "No coverage supplied.",
                            "command": "stl gtfs coverage"})
    elif feed["expired"]:
        note(BROKEN, "feed", f"Service data ended {feed['service_end']}, "
                             f"{_plural(abs(days or 0), 'day')} before {feed['today']}. Every "
                             "departure query now returns an empty list, which on a phone is "
                             "indistinguishable from 'no buses tonight'.")
        act("now", "stl snapshot fetch metro_gtfs",
            "The feed has expired. Fetch the new pick, then regenerate the oracle fixtures.")
        act("now", "stl oracle generate", "The committed fixtures were generated from an "
                                          "expired feed and no longer describe live service.")
    elif days is None:
        note(ATTENTION, "feed", "The feed declares no service dates at all, so expiry cannot "
                                "be measured. That is a malformed calendar, not a healthy feed.")
        act("today", "stl gtfs files", "Check whether calendar.txt survived the import.")
    elif days < EXPIRY_WARN_DAYS:
        note(ATTENTION, "feed", f"Only {_plural(days, 'day')} of service data remain "
                                f"(ends {feed['service_end']}). Past that date the app goes "
                                "silently blank.")
        act("today", "stl snapshot fetch metro_gtfs",
            f"The feed expires in {_plural(days, 'day')}; Metro publishes the next pick "
            "before the current one lapses.")
    else:
        note(INFO, "feed", f"Service data runs {feed['service_start']} to "
                           f"{feed['service_end']}, {_plural(days, 'day')} from "
                           f"{feed['today']}.")

    # ---- assumptions
    if assertions is None:
        assertions_block = _unchecked(
            "assertions",
            "The assumption suite was not run for this brief, so nothing has checked the "
            "facts about the feed that Metro never promised.",
            "stl assert run",
        )
        not_checked.append({"area": "assertions",
                            "detail": "The assumption suite was not run.",
                            "command": "stl assert run"})
        act("soon", "stl assert run", "Nothing has verified the assumptions the app is "
                                      "built on since this brief was generated.")
    else:
        assertions_block = _assertions_block(assertions)
        if assertions_block["failed"]:
            worst = assertions_block["violations"][0] if assertions_block["violations"] else {}
            note(BROKEN, "assertions",
                 f"{_plural(assertions_block['failed'], 'assumption')} violated: "
                 f"{_listed(assertions_block['violated_ids'])}. "
                 + (f"Worst: {worst.get('breaks', '')}" if worst.get("breaks") else ""))
            act("now", f"stl assert explain {assertions_block['violated_ids'][0]}",
                "An assumption the shipped app depends on no longer holds.")
        else:
            note(INFO, "assertions",
                 f"{_plural(assertions_block['passed'], 'assumption')} pass"
                 + (f", {assertions_block['skipped']} skipped for want of an input."
                    if assertions_block["skipped"] else "."))
        if assertions_block["skipped"]:
            note(INFO, "assertions",
                 f"{_plural(assertions_block['skipped'], 'assumption')} could not be measured: "
                 "each needs a pinned baseline, a web capture, or a realtime sample. Skipped "
                 "is not passed.")
            act("soon", "stl assert run --baseline <pin>",
                "Unlock the stability assumptions by comparing against a pinned baseline.")
        if assertions_block["opportunities"]:
            note(INFO, "assertions",
                 f"{_plural(assertions_block['opportunities'], 'assumption')} reports the feed "
                 "getting BETTER (an `opportunity`) -- something the app works around may no "
                 "longer need working around.")

    # ---- realtime
    if rt_health is None:
        realtime = _unchecked(
            "realtime",
            "No realtime health was supplied, so live arrivals are unverified. The app "
            "degrades to scheduled-only when realtime is down; this brief cannot tell you "
            "whether it is currently doing that.",
            "stl rt health",
        )
        not_checked.append({"area": "realtime", "detail": "No realtime health supplied.",
                            "command": "stl rt health"})
        act("soon", "stl rt fetch --all", "Take a realtime sample so freshness and the "
                                          "RT/static join rate can be measured.")
    else:
        realtime = _realtime_block(rt_health)
        if realtime["unavailable"]:
            note(ATTENTION, "realtime",
                 f"No sample at all for: {_listed(realtime['unavailable'])}. A feed that "
                 "could not be fetched is not fresh.")
            act("today", "stl rt fetch --all", "One or more realtime feeds have no sample.")
        if realtime["stale"]:
            note(ATTENTION, "realtime",
                 f"Stale: {_listed(realtime['stale'])} (oldest {realtime['oldest_entity']} at "
                 f"{realtime['oldest_age_seconds']}s). A stale feed does not look broken to a "
                 "rider -- it looks like a bus that is not moving.")
            act("today", "stl rt health", "Confirm whether staleness is the producer or the "
                                          "local sample.")
        if realtime["healthy"]:
            note(INFO, "realtime",
                 f"All {len(realtime['entities'])} realtime feed(s) present; oldest "
                 f"{realtime['oldest_entity']} at {realtime['oldest_age_seconds']}s.")

    # ---- page drift
    if web_check is None:
        drift = _unchecked(
            "drift",
            "No page check was supplied. Four facts the app ships -- fares, the holiday "
            "mapping, the next pick id, and the redistribution terms -- live on a marketing "
            "site that changes without announcement.",
            "stl web check",
        )
        not_checked.append({"area": "drift", "detail": "No page drift check supplied.",
                            "command": "stl web check"})
        act("soon", "stl web capture --all", "Establish a baseline for the pages the app's "
                                             "bundled artifacts are derived from.")
    else:
        drift = _drift_block(web_check)
        if drift["changed"]:
            note(ATTENTION, "drift",
                 f"{_plural(len(drift['changed']), 'page')} changed: "
                 f"{_listed(drift['changed'])}."
                 + (f" Alarming: {_listed(drift['alarming'])} -- the app bundles something "
                    "derived from these." if drift["alarming"] else ""))
            act("now" if drift["alarming"] else "today",
                f"stl web diff {drift['changed'][0]} <a> <b>",
                "Read the text delta before deciding whether anything bundled is now stale.")
            if drift["alarming"]:
                act("now", "stl bundle fares",
                    "A page the bundled artifacts are derived from changed; regenerate "
                    "before the next release rather than after it.")
        if drift["extraction_failed"]:
            note(ATTENTION, "drift",
                 f"{_plural(len(drift['extraction_failed']), 'page')} no longer extract: "
                 f"{_listed(drift['extraction_failed'])}. That is a redesign.")
            act("today", "stl web capture --all",
                "The stored HTML is the evidence the extractor gets fixed against.")
        if drift["never_captured"]:
            note(INFO, "drift",
                 f"{_plural(len(drift['never_captured']), 'page')} never captured: "
                 f"{_listed(drift['never_captured'])}. There is no baseline to drift from.")
            act("soon", "stl web capture --all", "Some configured pages have no baseline.")
        if not drift["changed"] and not drift["extraction_failed"]:
            note(INFO, "drift", f"No drift across {_plural(drift['pages'], 'page')}.")

    # ---- snapshots
    snaps = _snapshots_block(snapshots)
    if not snaps["count"]:
        # Attention rather than broken: nothing the app ships is wrong, but every
        # number above is now unattributable, and a result without provenance is
        # a rumour (spec 2.3). `broken` stays reserved for a violated assumption
        # or a lapsed feed -- things that are wrong on a rider's phone.
        note(ATTENTION, "snapshots", "No snapshots were supplied, so nothing in this brief "
                                     "can be traced to a source (spec 2.3).")
        act("today", "stl snapshot list", "Confirm the store actually holds the snapshot "
                                          "these numbers came from.")
    elif not snaps["pinned"]:
        note(INFO, "snapshots",
             f"{_plural(snaps['count'], 'snapshot')} stored, none pinned. Without a pinned "
             "baseline the stability assumptions cannot run and `diff` has nothing to "
             "compare against.")
        act("routine", "stl snapshot pin <id> --as baseline",
            "A pinned baseline is what makes stop_id survival measurable at the next pick.")

    status = _RANK_STATUS[max(
        (_STATUS_RANK[f["severity"]] for f in findings if f["severity"] in _STATUS_RANK),
        default=_STATUS_RANK[OK],
    )]

    if not actions:
        # Never empty. On a wholly green feed the useful next action is the one
        # that keeps it green, and a brief whose action list disappears when
        # everything passes teaches the reader to stop looking at it.
        act("routine", "stl oracle verify",
            "Nothing is wrong. Confirm the committed fixtures still match this snapshot, "
            "so a future disagreement isolates to the Kotlin side.")

    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], f["area"], f["detail"]))
    actions.sort(key=lambda a: (_URGENCY_RANK[a["urgency"]], a["_seq"]))
    next_actions = [
        {"order": i, "urgency": a["urgency"], "command": a["command"], "why": a["why"]}
        for i, a in enumerate(actions, start=1)
    ]

    return {
        "ok": status == OK,
        "status": status,
        "headline": _brief_headline(status, feed, assertions_block, realtime, drift,
                                    not_checked),
        "generated_at": now.isoformat(),
        "generated_by": f"stl {__version__}",
        "feed": feed,
        "assertions": assertions_block,
        "realtime": realtime,
        "drift": drift,
        "snapshots": snaps,
        "findings": findings,
        "blocking": [f for f in findings if f["severity"] == BROKEN],
        "next_actions": next_actions,
        "not_checked": sorted(not_checked, key=lambda n: n["area"]),
        "warnings": [f["detail"] for f in findings if f["severity"] == BROKEN],
        "notes": [
            "`status` describes what was measured. `not_checked` says what it does not "
            "cover -- an unmeasured system is never reported as passing.",
            "The CLI maps status `broken` to exit 3 and `attention` to exit 4 (spec 5), so "
            "this drops into cron without parsing output.",
        ],
    }


def _brief_headline(status: str, feed: dict[str, Any], assertions: dict[str, Any],
                    realtime: dict[str, Any], drift: dict[str, Any],
                    not_checked: list[dict[str, str]]) -> str:
    """One line a human reads in one breath, then decides whether to read on."""
    parts: list[str] = []
    if not feed["checked"]:
        parts.append("feed validity unknown")
    elif feed["expired"]:
        parts.append(f"feed EXPIRED {_plural(abs(feed['days_remaining'] or 0), 'day')} ago "
                     f"({feed['service_end']})")
    elif feed["days_remaining"] is None:
        parts.append("feed declares no service dates")
    else:
        parts.append(f"feed valid to {feed['service_end']} "
                     f"({_plural(feed['days_remaining'], 'day')})")

    if assertions.get("checked"):
        if assertions["failed"]:
            parts.append(f"{_plural(assertions['failed'], 'assumption')} VIOLATED "
                         f"({_listed(assertions['violated_ids'])})")
        else:
            parts.append(f"{_plural(assertions['passed'], 'assumption')} pass")
    if realtime.get("checked"):
        if realtime["unavailable"] or realtime["stale"]:
            parts.append("realtime degraded "
                         f"({_listed(sorted(set(realtime['unavailable']) | set(realtime['stale'])))})")
        else:
            parts.append("realtime fresh")
    if drift.get("checked"):
        parts.append(f"{_plural(len(drift['changed']), 'page')} changed"
                     if drift["changed"] else "no page drift")

    prefix = {OK: "OK", ATTENTION: "Attention", BROKEN: "BROKEN"}[status]
    tail = ""
    if not_checked:
        tail = " Not checked: " + ", ".join(sorted(n["area"] for n in not_checked)) + "."
    return f"{prefix}: " + "; ".join(parts) + "." + tail


# ------------------------------------------------------------------ handoff --

# The things that will bite a port, written once and used by both the markdown
# and the structured result. Each carries the command that re-verifies it,
# because a warning a reader cannot check is a warning a reader eventually
# stops believing.
SHARP_EDGES: tuple[dict[str, str], ...] = (
    {
        "id": "service_day_2400",
        "title": "24:xx service-day encoding",
        "bites": "GTFS measures times from noon-minus-twelve-hours on the SERVICE date, not "
                 "from local midnight, so a bus leaving at 00:12 is written `24:12:00` "
                 "against the previous service date. An implementation that reads the wall "
                 "clock and takes today's calendar date drops every late-night departure and "
                 "shows a blank screen at exactly the hour a rider most needs it.",
        "do": "Evaluate candidate service dates for today AND yesterday, and attribute each "
              "departure to the service date whose window contains it. Keep `service_date` "
              "and `gtfs_time` beside the resolved instant in every fixture: an "
              "implementation that gets the instant right by luck and the service date wrong "
              "fails on the next test instead of in production.",
        "verify_with": "stl gtfs late-night",
    },
    {
        "id": "dst_arithmetic",
        "title": "DST arithmetic",
        "bites": "Adding a duration to a zone-aware LOCAL datetime is wall-clock arithmetic: "
                 "a 90-minute window spanning 02:00 on a transition night covers 30 or 150 "
                 "real minutes. And because the service day starts at noon-minus-twelve, "
                 "service days on transition dates are 23 or 25 hours long -- the 24:xx "
                 "offset and the local clock disagree on precisely those two days a year.",
        "do": "Do all window and delay arithmetic in UTC (Kotlin: `Instant`), then convert "
              "back through `ZoneId.of(\"America/Chicago\")`. Never "
              "`LocalDateTime.plusMinutes`. Both transitions are oracle cases: "
              "`dst_spring_forward` (2027-03-14) and `dst_fall_back` (2026-11-01).",
        "verify_with": "stl gtfs service-day 2026-11-01T01:30:00",
    },
    {
        "id": "stop_code_vs_stop_id",
        "title": "stop_code vs stop_id",
        "bites": "The number printed on the pole is `stop_code`; `stop_id` is the internal "
                 "key that joins into `stop_times`. They are different values for the same "
                 "stop and both are numeric, so choosing wrong does not error -- it resolves "
                 "some other stop and returns its timetable with a straight face. The Light "
                 "SDK exposes no usable location API, so typing this number is the app's "
                 "entire input UX.",
        "do": "Resolve by stop_code first, fall back to stop_id, and carry BOTH on every "
              "record. Never key a saved stop on stop_id alone -- ids are regenerated at "
              "picks, which is what `stl diff stop-ids` measures.",
        "verify_with": "stl gtfs stop-resolve",
    },
    {
        "id": "no_protobuf_runtime",
        "title": "No protobuf runtime on the Light SDK",
        "bites": "GTFS-Realtime is protobuf, and the Light SDK's dependency allow-list "
                 "carries no protobuf runtime -- there is no `com.google.protobuf` to add. "
                 "Discovering this after the realtime feature is designed is a rewrite, not "
                 "a dependency bump.",
        "do": "Decode with kotlinx-serialization-protobuf or a hand-written varint reader "
              "built from `stl rt reference`, and grade it against the byte-level dump from "
              "`stl rt wire` on the same snapshot. Model only the fields the census shows "
              "Metro actually populates.",
        "verify_with": "stl rt schema --samples 5",
    },
    {
        "id": "fares_not_in_feed",
        "title": "Fares are not in the feed",
        "bites": "Metro publishes no `fare_attributes.txt` (the `no_fare_files` assumption "
                 "watches for that changing). Prices exist only as an HTML table on the "
                 "marketing site, so any bundled fare table is a dated scrape that can go "
                 "stale silently -- and a stale fare reaches a rider at a farebox.",
        "do": "Ship `stl bundle fares` output with its `as_of` date visible in the app, store "
              "money as integer cents, and keep the `fares_unchanged` assertion armed so a "
              "price change is a CI failure rather than a support ticket.",
        "verify_with": "stl web extract fares",
    },
)


def _fact(fact_id: str, claim: str, evidence: str, reverify_with: str,
          snapshot_id: str, on: str) -> dict[str, str]:
    """One verified claim.

    Every field is mandatory. A claim without its snapshot id and date is a
    rumour (spec 2.3), and a claim without the command that reproduces it makes
    the next reader take this document on faith -- which is the failure mode the
    house format exists to prevent.
    """
    return {
        "id": fact_id,
        "claim": claim,
        "evidence": evidence,
        "verified_against": snapshot_id,
        "verified_on": on,
        "reverify_with": reverify_with,
    }


def handoff(coverage: dict[str, Any], stop_resolve: dict[str, Any],
            rt_census: dict[str, Any] | None, stats: dict[str, Any],
            snapshot_id: str, feed_sha256: str, now: datetime) -> dict[str, Any]:
    """A markdown block for pasting into CLAUDE.md: verified facts with citations.

    The house convention this honours: every claim carries the snapshot id and
    the date it was verified against, plus the command that reproduces it, so a
    future reader can re-verify rather than trust. Metro republishes this feed
    at every quarterly pick; a fact without a date on it is a fact that quietly
    stopped being true.

    `rt_census` is optional because the realtime questions can be answered on a
    different day from the static ones. Absent, the realtime section says it was
    not verified in this pass rather than going quiet.

    Returns `{"markdown": ..., "facts": [...], "sharp_edges": [...], ...}`.
    """
    on = now.date().isoformat()
    short_sha = (feed_sha256 or "")[:12]
    cov = coverage or {}
    st = stats or {}
    resolve = stop_resolve or {}

    facts: list[dict[str, str]] = []

    def fact(fact_id: str, claim: str, evidence: str, reverify: str) -> None:
        facts.append(_fact(fact_id, claim, evidence, reverify, snapshot_id, on))

    # ---- feed validity
    days = cov.get("days_remaining")
    fact(
        "feed_window",
        f"The feed's service data runs {cov.get('service_start')} to "
        f"{cov.get('service_end')}.",
        (f"{_plural(days, 'day')} remained as of {cov.get('today')}."
         if days is not None else "No service dates are declared in this snapshot.")
        + " Metro's feed ends at the next quarterly pick, so this window moves and a "
          "cached copy goes silently blank past its end date.",
        "stl gtfs coverage",
    )
    info = cov.get("feed_info") or {}
    if info.get("feed_version"):
        fact("feed_version",
             f"feed_info.feed_version is {info['feed_version']!r}.",
             f"Published by {info.get('feed_publisher_name') or 'the agency'}. This is the "
             "string to show in an About screen and to quote in a support conversation.",
             "stl gtfs coverage")

    # ---- the stop_id / stop_code question
    verdict = (resolve.get("verdict") or {})
    field = verdict.get("rider_facing_field", "unknown")
    code = resolve.get("stop_code") or {}
    fact(
        "rider_facing_field",
        f"The number printed on a stop sign resolves via `{field}`.",
        f"{verdict.get('rationale', '')} "
        + (f"Coverage {code.get('coverage')}, unique={code.get('unique')}, observed lengths "
           f"{code.get('observed_lengths')}." if code.get("present") else
           "stops.txt carries no stop_code column in this snapshot.")
        + f" Total stops: {resolve.get('total_stops')}.",
        "stl gtfs stop-resolve",
    )
    example = (resolve.get("published_example") or {})
    if example.get("value"):
        hits = example.get("found_in") or []
        fact("published_example",
             f"Metro's own published example {example['value']} resolves in "
             + (", ".join(f"`{h['field']}` ({h['matches']} match(es))" for h in hits)
                if hits else "NEITHER field"),
             "Metro's developer page shows this number on a stop-sign photo, which makes it "
             "the one externally-checkable answer to the stop_code-vs-stop_id question.",
             "stl gtfs stop-resolve")

    # ---- shape of the feed
    by_type = st.get("routes_by_type") or {}
    fact(
        "feed_scale",
        f"{st.get('routes')} route(s), {st.get('stops')} stop(s), {st.get('trips')} trip(s), "
        f"{st.get('stop_times')} stop_time(s), {st.get('service_ids')} service_id(s).",
        "Route types present: "
        + (", ".join(f"{name} x{by_type[name]}" for name in sorted(by_type)) or "none")
        + ". These are the numbers the on-device import budget and the compact-bundle "
          "strategy are argued from.",
        "stl gtfs stats",
    )
    fact(
        "calendar_shape",
        f"calendar.txt has {st.get('calendar_rows')} row(s); calendar_dates.txt has "
        f"{st.get('calendar_dates_rows')}.",
        "Holidays live in calendar_dates.txt as type-1 (added) and type-2 (removed) "
        "exceptions layered over the weekday pattern. Both must be applied, separately, or "
        "Labor Day shows weekday service.",
        "stl gtfs calendar --date 2026-09-07",
    )

    # ---- realtime
    if rt_census is None:
        rt_fact = {
            "id": "rt_field_census",
            "claim": "NOT VERIFIED IN THIS PASS: which GTFS-Realtime fields Metro populates.",
            "evidence": "No field census was supplied when this handoff was generated. The "
                        "realtime shape is the input to the Kotlin decoder, so leaving it "
                        "unstated is better than leaving it stale.",
            "verified_against": snapshot_id,
            "verified_on": on,
            "reverify_with": "stl rt schema --samples 5",
        }
        facts.append(rt_fact)
    else:
        top = [row for row in (rt_census.get("fields") or [])][:MAX_LISTED]
        fact(
            "rt_field_census",
            f"{rt_census.get('distinct_paths')} distinct protobuf path(s) observed across "
            f"{rt_census.get('samples')} sample(s); {rt_census.get('unmodelled_paths')} "
            "unmodelled.",
            "Most frequent: "
            + ("; ".join(f"{r.get('path')} at {r.get('presence_rate')}" for r in top)
               or "none observed")
            + ". A path containing '?<n>' is a field number absent from core/rt/schema.py -- "
              "a hand-rolled decoder skips it silently, so the data goes missing rather than "
              "wrong, which is much harder to notice.",
            "stl rt schema --samples 5",
        )

    markdown = _handoff_markdown(facts, snapshot_id, feed_sha256, short_sha, on, now)
    return {
        "ok": True,
        "markdown": markdown,
        "facts": facts,
        "sharp_edges": [dict(edge) for edge in SHARP_EDGES],
        "snapshot_id": snapshot_id,
        "feed_sha256": feed_sha256,
        "verified_on": on,
        "generated_at": now.isoformat(),
        "generated_by": f"stl {__version__}",
        "counts": {"facts": len(facts), "sharp_edges": len(SHARP_EDGES)},
        "warnings": [] if rt_census is not None else [
            "No realtime field census was supplied, so the handoff says the realtime shape "
            "is unverified rather than asserting a stale answer. Run "
            "`stl rt schema --samples 5` and regenerate."
        ],
        "notes": [
            "Paste the `markdown` block into CLAUDE.md verbatim. Every line carries the "
            "snapshot id and date it was verified against so the next reader can re-verify "
            "instead of trusting.",
            "Regenerate rather than hand-edit: an edited claim loses the citation that makes "
            "it worth anything.",
        ],
    }


def _handoff_markdown(facts: list[dict[str, str]], snapshot_id: str, feed_sha256: str,
                      short_sha: str, on: str, now: datetime) -> str:
    lines = [
        "## St. Louis Metro feed -- verified facts",
        "",
        f"<!-- Generated by `stl report handoff` ({__version__}) at {now.isoformat()}. "
        "Regenerate; do not hand-edit. -->",
        "",
        f"Every claim below was verified on **{on}** against snapshot "
        f"`{snapshot_id}` (feed sha256 `{short_sha}`). Each line names the command that "
        "reproduces it. Metro republishes this feed at every quarterly pick, so re-verify "
        "rather than trust -- an undated fact about this feed is a fact that quietly stopped "
        "being true.",
        "",
        "### Verified facts",
        "",
    ]
    for item in facts:
        lines.append(f"- **{item['claim']}** {item['evidence']}")
        lines.append(f"  <br>_[verified {item['verified_on']} against `"
                     f"{item['verified_against']}` -- re-verify: `{item['reverify_with']}`]_")
    lines += [
        "",
        "### Sharp edges -- the things that will bite a port",
        "",
    ]
    for i, edge in enumerate(SHARP_EDGES, start=1):
        lines.append(f"{i}. **{edge['title']}** -- {edge['bites']}")
        lines.append(f"   - Do this: {edge['do']}")
        lines.append(f"   - Check it: `{edge['verify_with']}`")
    lines += [
        "",
        "### Re-verifying all of the above",
        "",
        "```",
        "stl snapshot fetch metro_gtfs",
        "stl assert run",
        "stl report handoff",
        "```",
        "",
        f"Full feed sha256: `{feed_sha256}`",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- changelog --

def _entry(severity: str, area: str, text: str) -> dict[str, str]:
    return {"severity": severity, "area": area, "text": text}


def changelog(diff_summary: dict[str, Any], since_pin: str,
              now: datetime) -> dict[str, Any]:
    """Everything that changed since a pinned baseline, as readable prose.

    A wrapper over `diffing.summary` output, and deliberately only that: the
    diff group already decided what is alarming and what is routine, and a
    second opinion here would be a second place to keep those thresholds in
    sync. What this adds is sentences -- a reader who wants counts reads the
    diff, a reader who wants to know whether to care reads this.
    """
    summary = diff_summary or {}
    changed = bool(summary.get("drift_detected"))
    sections: list[dict[str, Any]] = []
    entries: list[dict[str, str]] = []

    if not changed:
        headline = (f"Nothing changed since `{since_pin}`: the two snapshots agree on files, "
                    "routes, stops, stop_ids and calendar.")
        markdown = _changelog_markdown(headline, [], since_pin, now, changed=False)
        return {
            "ok": True,
            "changed": False,
            "drift_detected": False,
            "since": since_pin,
            "headline": headline,
            "diff_headline": str(summary.get("headline") or headline),
            "sections": [],
            "entries": [],
            "alarming": [],
            "counts": {"sections": 0, "entries": 0, "alarming": 0},
            "markdown": markdown,
            "generated_at": now.isoformat(),
            "generated_by": f"stl {__version__}",
            "warnings": [],
            "notes": [
                "A byte-identical answer is a real result, not an empty one: it means the "
                "pick did not move anything this tool measures.",
            ],
        }

    # ---- stop numbers, first, because it is the finding with a user attached
    ids = summary.get("stop_ids") or {}
    if ids:
        code = ids.get("stop_code") or {}
        sid = ids.get("stop_id") or {}
        lines = [ids.get("verdict", "")] if ids.get("verdict") else []
        if code or sid:
            lines.append(
                f"{code.get('lost', 0)} stop_code(s) and {sid.get('lost', 0)} stop_id(s) "
                f"stopped resolving; {code.get('gained', 0)} stop_code(s) and "
                f"{sid.get('gained', 0)} stop_id(s) appeared. Every retired code is "
                "somebody's saved stop going blank with no explanation."
            )
        severity = ATTENTION if not ids.get("meets_assumption", True) else INFO
        sections.append({"title": "Stop numbers", "area": "stop_ids",
                         "severity": severity, "lines": [line for line in lines if line]})
        for line in lines:
            if line:
                entries.append(_entry(severity, "stop_ids", line))

    # ---- routes
    routes = summary.get("routes") or {}
    counts = routes.get("counts") or {}
    if any(counts.get(k) for k in ("added", "removed", "changed")):
        lines = []
        if counts.get("removed"):
            lines.append(f"{_plural(counts['removed'], 'route')} retired: "
                         + _listed([r.get("route_id", "") for r in routes.get("removed") or []]))
        if counts.get("added"):
            lines.append(f"{_plural(counts['added'], 'route')} added: "
                         + _listed([r.get("route_id", "") for r in routes.get("added") or []]))
        if counts.get("changed"):
            lines.append(f"{_plural(counts['changed'], 'route')} renamed or retyped. A rename "
                         "is what a pick does; a route_type change means the app's bus/rail "
                         "special-casing now applies to a different set.")
        sections.append({"title": "Routes", "area": "routes", "severity": INFO,
                         "lines": lines})
        entries.extend(_entry(INFO, "routes", line) for line in lines)

    # ---- stops
    stops = summary.get("stops") or {}
    scounts = stops.get("counts") or {}
    if any(scounts.get(k) for k in ("added", "removed", "renamed", "moved")):
        lines = [
            f"{scounts.get('a', 0)} -> {scounts.get('b', 0)} stops: "
            f"+{scounts.get('added', 0)} / -{scounts.get('removed', 0)}, "
            f"{scounts.get('renamed', 0)} renamed, {scounts.get('moved', 0)} moved more than "
            f"{stops.get('moved_threshold_m')} m."
        ]
        moved = stops.get("moved") or []
        if moved:
            lines.append(f"Furthest move: {moved[0].get('stop_name')} "
                         f"({moved[0].get('stop_code')}) at {moved[0].get('moved_m')} m. Far "
                         "enough matters: a rider walks to a pole, not to a coordinate.")
        sections.append({"title": "Stops", "area": "stops", "severity": INFO, "lines": lines})
        entries.extend(_entry(INFO, "stops", line) for line in lines)

    # ---- calendar
    cal = summary.get("calendar") or {}
    date_range = cal.get("date_range") or {}
    if cal:
        lines = []
        end_shift = date_range.get("end_shift_days")
        if end_shift:
            direction = "later" if end_shift > 0 else "EARLIER"
            lines.append(
                f"Service window now ends {(date_range.get('b') or {}).get('end')}, "
                f"{abs(end_shift)} day(s) {direction} than {(date_range.get('a') or {}).get('end')}."
                + ("" if end_shift > 0 else " The newer snapshot covers less than the one it "
                                            "replaces, which is not what a pick looks like.")
            )
        service_ids = cal.get("service_ids") or {}
        added = (service_ids.get("added") or {}).get("count", 0)
        removed = (service_ids.get("removed") or {}).get("count", 0)
        if added or removed:
            lines.append(f"{removed} service_id(s) removed, {added} added. service_ids are "
                         "regenerated every pick; only their date coverage matters.")
        exceptions = cal.get("exceptions") or {}
        if exceptions.get("added") or exceptions.get("removed"):
            lines.append(f"{exceptions.get('added', 0)} calendar_dates exception(s) added, "
                         f"{exceptions.get('removed', 0)} removed. These are the holidays: an "
                         "exception that moved is a wrong departure time on one specific day.")
        if lines:
            severity = ATTENTION if (end_shift or 0) < 0 else INFO
            sections.append({"title": "Calendar", "area": "calendar", "severity": severity,
                             "lines": lines})
            entries.extend(_entry(severity, "calendar", line) for line in lines)

    # ---- files
    files = summary.get("files") or {}
    if files.get("tables_added") or files.get("tables_removed"):
        lines = []
        for table in files.get("tables_added") or []:
            lines.append(f"{table.get('file')} appeared ({table.get('rows')} rows). "
                         + str(table.get("why_it_matters") or ""))
        for table in files.get("tables_removed") or []:
            lines.append(f"{table.get('file')} disappeared (was {table.get('rows')} rows). "
                         + str(table.get("why_it_matters") or ""))
        sections.append({"title": "Files", "area": "files", "severity": ATTENTION,
                         "lines": lines})
        entries.extend(_entry(ATTENTION, "files", line) for line in lines)

    # The diff group already graded its own findings. Anything it called
    # alarming is promoted to the top of the document rather than re-derived
    # here -- one set of thresholds, in the module that owns them.
    alarming = [
        _entry(BROKEN, str(f.get("dimension", "")), str(f.get("detail", "")))
        for f in (summary.get("alarming") or [])
    ]
    if alarming:
        # Promoted, not duplicated. A finding that appears both under "Read this
        # first" and again three paragraphs down teaches the reader to skim, and
        # skimming is exactly what the promotion was meant to prevent.
        promoted = {a["text"] for a in alarming}
        for section in sections:
            section["lines"] = [line for line in section["lines"] if line not in promoted]
        sections = [s for s in sections if s["lines"]]
        entries = [e for e in entries if e["text"] not in promoted]
        sections.insert(0, {"title": "Read this first", "area": "alarming",
                            "severity": BROKEN, "lines": [a["text"] for a in alarming]})
        entries = alarming + entries

    sections.sort(key=lambda s: (_SEVERITY_RANK[s["severity"]], s["title"]))
    headline = (f"Since `{since_pin}`: " + str(summary.get("headline") or "the feed moved.")
                + (f" {_plural(len(alarming), 'alarming finding')} -- read those first."
                   if alarming else ""))
    return {
        "ok": not alarming,
        "changed": True,
        "drift_detected": True,
        "since": since_pin,
        "headline": headline,
        "diff_headline": str(summary.get("headline") or ""),
        "sections": sections,
        "entries": entries,
        "alarming": alarming,
        "counts": {"sections": len(sections), "entries": len(entries),
                   "alarming": len(alarming)},
        "markdown": _changelog_markdown(headline, sections, since_pin, now, changed=True),
        "generated_at": now.isoformat(),
        "generated_by": f"stl {__version__}",
        "warnings": [a["text"] for a in alarming],
        "notes": [
            "Severity is the diff group's, not this module's: one set of thresholds, in the "
            "module that owns them.",
        ],
    }


def _changelog_markdown(headline: str, sections: list[dict[str, Any]], since_pin: str,
                        now: datetime, changed: bool) -> str:
    lines = [
        f"## Feed changelog since `{since_pin}`",
        "",
        f"<!-- Generated by `stl report changelog --since {since_pin}` at "
        f"{now.isoformat()}. -->",
        "",
        headline,
        "",
    ]
    if not changed:
        lines += ["Nothing to report. Re-run after the next `stl snapshot fetch metro_gtfs`.",
                  ""]
        return "\n".join(lines)
    for section in sections:
        lines.append(f"### {section['title']}")
        lines.append("")
        for line in section["lines"]:
            lines.append(f"- {line}")
        lines.append("")
    lines += ["Full structured delta: `stl diff summary <baseline> <current>`.", ""]
    return "\n".join(lines)
