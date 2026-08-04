"""The `web` group: capture, compare, and drift-check Metro's published pages.

Four facts the app ships live on a marketing site rather than in the feed: the
fare table, the holiday service mapping, the pick id of the next service change,
and the developer terms that grant the right to redistribute any of it. None of
them is versioned, none is announced, and all of them can change on a Tuesday.
This module is how that gets noticed from a cron job instead of from a user.

Pure logic (spec 2.1): nothing here fetches, stores, prints, exits, or prompts.
`service.py` fetches through `io/http.py` and stores through `io/store.py`; this
module is handed the HTML and hands back a record. That split is what lets the
whole group be tested with no network at all.

`drift_detected` is returned as a boolean. Mapping it to exit code 4 is the
CLI's job (spec 5), not this module's.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
from typing import Any

from ...errors import ExtractionFailed, PageNotFound, UsageError
from .extract import EXTRACTORS, content_hash, main_text, normalize, robots_directives

# The extractor entry point is imported privately on purpose. Binding it to the
# name `extract` here would shadow the `extract` SUBMODULE on this package, and
# the house convention is submodule imports (`from .gtfs import inspect`), so a
# caller writing `from .web import extract` must get the module. Call it as
# `web.extract.extract(html, extractor)`.
from .extract import extract as _run_extractor

__all__ = [
    "EXTRACTORS",
    "capture",
    "check",
    "compare",
    "content_hash",
    "normalize",
    "should_fetch",
]

# Diff bounds. A page redesign changes every line, and an unbounded unified diff
# of a 40 KB page is a denial of service against the context window that asked
# what changed (spec 2.4). Truncation is always reported.
MAX_DIFF_HUNKS = 20
MAX_DIFF_LINES = 400
DIFF_CONTEXT_LINES = 2
MAX_FIELD_CHANGES = 50
HASH_HISTORY = 5

# Spec 9: "HTML pages capped at one fetch per day." Enforced as a floor rather
# than trusted to config, because the failure mode of a mistyped
# fetch_interval_hours is hammering a public agency's web server -- an outcome
# nobody notices locally and everybody notices in Metro's access logs.
MIN_FETCH_INTERVAL_HOURS = 24.0

# What a change to each page actually breaks. A drift report that says "the
# fares page changed" and stops has moved the work to the reader; this says what
# is now wrong in the shipped app, and names the assertion (spec 6.10) that
# encodes it.
IMPACT_BY_PAGE: dict[str, tuple[str, str, str]] = {
    "fares": (
        "alarming",
        "The app's bundled fare table is now lying to users. Re-run "
        "`stl bundle fares`, diff the prices, and ship an update before anyone "
        "boards on a number this tool printed.",
        "fares_unchanged",
    ),
    "holidays": (
        "alarming",
        "The bundled holiday mapping may be wrong. A rider shown weekday service "
        "on a holiday misses a bus by an hour. Re-run `stl bundle holidays` and "
        "confirm MetroBus and MetroLink separately -- they use different service "
        "vocabularies.",
        "holidays_unchanged",
    ),
    "developer_terms": (
        "alarming",
        "Your redistribution rights may have changed. The GTFS licence is "
        "non-exclusive, limited, and REVOCABLE; read the diff before the next "
        "release rather than after it.",
        "terms_unchanged",
    ),
    "schedule_changes": (
        "notable",
        "A new pick is being advertised. The bundled GTFS snapshot is about to be "
        "superseded: fetch the feed, run `stl diff stop-ids` against the pinned "
        "baseline, and regenerate the oracle fixtures.",
        "",
    ),
    "purchase": (
        "notable",
        "The fare-purchase instructions moved. Any in-app copy describing how to "
        "pay is now stale.",
        "",
    ),
    "rider_alerts": (
        "routine",
        "Reference only. This page carries noarchive/nosnippet and spec 9 forbids "
        "treating it as a live dependency -- the app reads GTFS-RT alerts, not "
        "this. A change here is context, not breakage.",
        "",
    ),
}

IMPACT_BY_EXTRACTOR: dict[str, tuple[str, str, str]] = {
    "fare_table": IMPACT_BY_PAGE["fares"],
    "holiday_table": IMPACT_BY_PAGE["holidays"],
    "pick_id": IMPACT_BY_PAGE["schedule_changes"],
    "alert_list": IMPACT_BY_PAGE["rider_alerts"],
}

DEFAULT_IMPACT = (
    "notable",
    "A page this tool reads changed. Read the diff and decide whether anything "
    "bundled from it is now stale.",
    "",
)

SEVERITY_RANK = {"alarming": 0, "notable": 1, "routine": 2}


def _as_utc(when: datetime) -> datetime:
    """Coerce to aware UTC.

    A naive datetime is read as UTC rather than as local time: the store writes
    ISO-8601 UTC (spec 2.8), and guessing the machine's zone would make the
    politeness gate behave differently on a laptop and in CI -- a five-hour
    difference in whether a fetch is allowed.
    """
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _impact(page_key: str, extractor: str) -> tuple[str, str, str]:
    return IMPACT_BY_PAGE.get(page_key) or IMPACT_BY_EXTRACTOR.get(extractor) or DEFAULT_IMPACT


# ------------------------------------------------------------------ capture --

def capture(page_key: str, html: str, url: str, fetched_at: datetime,
            extractor: str) -> dict[str, Any]:
    """Build a capture record from already-fetched HTML.

    Pure: the caller fetched the bytes and the caller stores the record. What
    happens here is normalization, hashing, extraction, and measurement.

    A failed extraction is RECORDED, not raised. When Metro redesigns a page the
    extraction is exactly what breaks, and that is the moment you most want the
    capture on disk: the stored HTML is the evidence you fix the extractor
    against. Raising would mean `web capture` throws away the one artifact that
    explains the failure. `ok` goes false and the error travels with the record,
    so a caller that wants to fail loudly still can.

    A bad extractor NAME still raises (UsageError): that is a typo in
    sources.toml, and storing a capture that silently extracts nothing would
    hide a config error behind a plausible-looking record.
    """
    if extractor not in EXTRACTORS:
        raise UsageError(
            f"Page {page_key!r} is configured with unknown extractor {extractor!r}.",
            remedy="`extractor` in sources.toml must be one of: " + ", ".join(EXTRACTORS)
                   + ". Fix the page entry before capturing, or the capture would be "
                     "stored with no extraction and look healthy.",
            page=page_key,
            extractor=extractor,
            known=list(EXTRACTORS),
        )

    raw_bytes = html.encode("utf-8")
    main = main_text(html)
    text = main["text"]
    text_bytes = len(text.encode("utf-8"))
    robots = robots_directives(html)

    extraction: dict[str, Any] | None = None
    extraction_error: dict[str, Any] | None = None
    try:
        extraction = _run_extractor(html, extractor)
    except ExtractionFailed as exc:
        extraction_error = exc.to_dict()["error"]

    warnings: list[str] = []
    notes: list[str] = [
        f"Content region found by the {main['strategy']!r} strategy; the hash covers "
        "that region only, so a site-wide footer edit is not reported as a change to "
        "this page (spec 6.7).",
    ]
    if extraction_error:
        warnings.append(
            f"Extraction failed for page {page_key!r}: {extraction_error['message']} "
            "The capture was kept anyway -- it is the evidence the extractor is fixed "
            "against."
        )
    if robots["noarchive"] or robots["nosnippet"]:
        notes.append(
            "This page carries " + robots["raw"] + ". Capture-for-reference only; "
            "never make it a live dependency (spec 9)."
        )
    if not text:
        warnings.append(
            "The page normalized to no text at all. That is what a JavaScript shell or "
            "a redirect looks like from here; check the stored HTML."
        )

    return {
        "ok": extraction_error is None and bool(text),
        "page": page_key,
        "url": url,
        "extractor": extractor,
        "fetched_at": _as_utc(fetched_at).isoformat(),
        "content_hash": content_hash(text),
        "raw_sha256": content_hash(html),
        "bytes_raw": len(raw_bytes),
        "bytes_normalized": text_bytes,
        # The compression ratio is the one-number answer to "was normalizing
        # worth it": Metro's pages come in at a few percent signal.
        "signal_ratio": round(text_bytes / len(raw_bytes), 4) if raw_bytes else None,
        "word_count": len(text.split()),
        "line_count": text.count("\n") + 1 if text else 0,
        "content_region": main["strategy"],
        "normalized_text": text,
        "robots": robots,
        "extraction": extraction,
        "extraction_ok": extraction_error is None,
        "extraction_error": extraction_error,
        "warnings": warnings,
        "notes": notes,
    }


# ------------------------------------------------------------------ compare --

def _require_capture(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or "normalized_text" not in value:
        raise UsageError(
            f"The {name!r} argument is not a capture record.",
            remedy="Pass records produced by `stl_transit.core.web.capture` (the JSON "
                   "stored beside each web snapshot), not raw HTML and not a snapshot "
                   "manifest.",
            argument=name,
            got=type(value).__name__,
        )
    return value


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Normalized-text diff between two captures.

    Both sides are already normalized, so every line in the diff is a line a
    reader would recognize on the page. Diffing raw HTML would produce a
    truthful, unreadable answer dominated by nonces.
    """
    before = _require_capture("before", before)
    after = _require_capture("after", after)
    if before.get("page") and after.get("page") and before["page"] != after["page"]:
        raise UsageError(
            f"Cannot compare a capture of {before['page']!r} with one of "
            f"{after['page']!r}.",
            remedy="Pass two captures of the same page: `stl web diff <page> <a> <b>`. "
                   "Comparing different pages produces a diff of two documents that "
                   "were never meant to match.",
            before=before.get("page"),
            after=after.get("page"),
        )

    a_lines = before["normalized_text"].split("\n") if before["normalized_text"] else []
    b_lines = after["normalized_text"].split("\n") if after["normalized_text"] else []
    diff = _bounded_diff(a_lines, b_lines,
                         before.get("fetched_at", "before"), after.get("fetched_at", "after"))
    fields = _field_changes(before.get("extraction"), after.get("extraction"))

    changed = before.get("content_hash") != after.get("content_hash")
    return {
        "ok": True,
        "page": after.get("page") or before.get("page"),
        "changed": changed,
        "hash_before": before.get("content_hash"),
        "hash_after": after.get("content_hash"),
        "fetched_at_before": before.get("fetched_at"),
        "fetched_at_after": after.get("fetched_at"),
        "bytes_normalized_before": before.get("bytes_normalized"),
        "bytes_normalized_after": after.get("bytes_normalized"),
        "lines_before": len(a_lines),
        "lines_after": len(b_lines),
        "diff": diff,
        "extraction": fields,
        "fields_changed": fields["fields"],
        "verdict": _compare_verdict(changed, diff, fields),
    }


def _bounded_diff(a_lines: list[str], b_lines: list[str],
                  from_label: str, to_label: str) -> dict[str, Any]:
    """Unified diff, capped at MAX_DIFF_HUNKS hunks and MAX_DIFF_LINES lines.

    The generator is consumed to the end even after the cap so the counts are
    the real ones. A truncated diff that also under-reports how much changed
    would be worse than no diff at all.
    """
    lines: list[str] = []
    hunks = added = removed = 0
    truncated = False
    for line in difflib.unified_diff(a_lines, b_lines, fromfile=from_label,
                                     tofile=to_label, lineterm="", n=DIFF_CONTEXT_LINES):
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        if truncated:
            continue
        if hunks > MAX_DIFF_HUNKS or len(lines) >= MAX_DIFF_LINES:
            truncated = True
            continue
        lines.append(line)

    shown = sum(1 for line in lines if line.startswith("@@"))
    note = (
        f"Diff bounded to {MAX_DIFF_HUNKS} hunks and {MAX_DIFF_LINES} lines; "
        f"{shown} of {hunks} hunk(s) shown. Fetch the two captures from the store to "
        "read the rest."
        if truncated else
        f"Complete: {hunks} hunk(s), nothing withheld."
    )
    return {
        "unified": lines,
        "hunks": hunks,
        "hunks_shown": shown,
        "lines_added": added,
        "lines_removed": removed,
        "context_lines": DIFF_CONTEXT_LINES,
        "truncated": truncated,
        "note": note,
    }


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Extraction result as dotted paths, so a change can be named precisely.

    "rows[3].price_cents: 250 -> 275" is a sentence someone can act on;
    "the extraction changed" is not.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in sorted(value):
            out.update(_flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list):
        out = {}
        for i, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}[{i}]"))
        return out
    return {prefix or "value": value}


def _field_changes(before: dict[str, Any] | None,
                   after: dict[str, Any] | None) -> dict[str, Any]:
    if before is None or after is None:
        state = (
            "extraction_failed_before" if before is None and after is not None else
            "extraction_failed_after" if after is None and before is not None else
            "extraction_failed_both"
        )
        return {
            "comparable": False,
            "state": state,
            "fields": [],
            "changes": [],
            "truncated": False,
            "note": "One side has no extraction result, so field-level comparison is "
                    "meaningless. Read the capture's extraction_error: the page shape "
                    "moved, which is a bigger finding than any single field.",
        }

    flat_a, flat_b = _flatten(before), _flatten(after)
    changes = []
    for path in sorted(set(flat_a) | set(flat_b)):
        old, new = flat_a.get(path, None), flat_b.get(path, None)
        if old == new:
            continue
        changes.append(
            {
                "field": path,
                "before": old,
                "after": new,
                "status": ("added" if path not in flat_a
                           else "removed" if path not in flat_b else "changed"),
            }
        )
    shown = changes[:MAX_FIELD_CHANGES]
    return {
        "comparable": True,
        "state": "compared",
        "fields": [c["field"] for c in shown],
        "changes": shown,
        "change_count": len(changes),
        "truncated": len(changes) > MAX_FIELD_CHANGES,
        "note": f"Showing {len(shown)} of {len(changes)} changed field(s)."
                if len(changes) > MAX_FIELD_CHANGES else
                f"{len(changes)} field(s) changed.",
    }


def _compare_verdict(changed: bool, diff: dict[str, Any], fields: dict[str, Any]) -> str:
    if not changed:
        return "No change: the two captures normalize to the same text and the same hash."
    parts = [f"{diff['lines_added']} line(s) added, {diff['lines_removed']} removed"]
    if fields["comparable"]:
        parts.append(f"{fields['change_count']} extracted field(s) changed")
    else:
        parts.append("extraction is not comparable across these two captures")
    return "Changed: " + ", ".join(parts) + "."


# -------------------------------------------------------------------- check --

def check(captures: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Drift check across pages.

    `captures` maps page_key -> capture records, newest first. Nothing is
    fetched here: the caller reads what the store already holds, which is what
    makes `web check` safe to run on a schedule without touching Metro at all.
    """
    if not captures:
        raise PageNotFound(
            "No page captures on record.",
            remedy="Capture the configured pages first: `stl web capture --all`. "
                   "Then `stl web check` has two points to compare.",
            pages=0,
        )

    items = []
    for page in sorted(captures):
        # Defensive re-sort. The contract is newest-first, and a caller that got
        # it backwards would otherwise be told the current hash is the old one.
        # Python's sort is stable, so records without a timestamp keep the order
        # they were given.
        history = sorted(captures[page] or [],
                         key=lambda rec: rec.get("fetched_at") or "", reverse=True)
        items.append(_check_item(page, history))

    items.sort(key=lambda i: (SEVERITY_RANK.get(i["severity"], 9), i["page"]))
    drift = any(i["changed"] for i in items)
    failed = [i["page"] for i in items if i["status"] == "extraction_failed"]
    missing = [i["page"] for i in items if i["status"] == "never_captured"]
    changed = [i["page"] for i in items if i["changed"]]

    return {
        "ok": not (drift or failed or missing),
        "drift_detected": drift,
        "items": items,
        "counts": {
            "pages": len(items),
            "changed": len(changed),
            "extraction_failed": len(failed),
            "never_captured": len(missing),
            "alarming": sum(1 for i in items if i["changed"] and i["severity"] == "alarming"),
        },
        "changed_pages": changed,
        "headline": _check_headline(items, changed, failed, missing),
        "note": "drift_detected is any change to the normalized content of any page. "
                "The CLI maps it to exit code 4 (spec 5) so this drops into cron "
                "without parsing output; `severity` says whether to act tonight.",
    }


def _check_item(page: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    if not history:
        severity, breaks, assertion = _impact(page, "")
        return {
            "page": page,
            "extractor": None,
            "url": None,
            "captures": 0,
            "current_hash": None,
            "previous_hash": None,
            "changed": False,
            "status": "never_captured",
            "last_fetched": None,
            "extraction_ok": None,
            "severity": severity,
            "breaks": breaks,
            "assertion": assertion,
            "hash_history": [],
            "detail": f"No capture of {page!r} has ever been stored, so there is "
                      "nothing to compare and no baseline to detect drift against. "
                      "Run `stl web capture " + page + "`.",
        }

    current = history[0]
    previous = history[1] if len(history) > 1 else None
    severity, breaks, assertion = _impact(page, current.get("extractor", ""))
    changed = bool(previous) and current.get("content_hash") != previous.get("content_hash")
    extraction_ok = bool(current.get("extraction_ok"))

    if not extraction_ok:
        status = "extraction_failed"
    elif changed:
        status = "changed"
    elif previous is None:
        status = "single_capture"
    else:
        status = "unchanged"

    detail = {
        "extraction_failed": (
            f"The {page!r} page still resolves but no longer extracts: "
            f"{(current.get('extraction_error') or {}).get('message', 'unknown reason')} "
            "That is a redesign, and it is the signal this job exists to produce."
        ),
        "changed": (
            f"{page!r} changed between {previous.get('fetched_at') if previous else None} "
            f"and {current.get('fetched_at')}. Run `stl web diff {page} <a> <b>` for the "
            "text delta."
        ),
        "single_capture": (
            f"Only one capture of {page!r} exists, so 'changed' cannot be answered yet. "
            "This is the baseline; the next capture is the first real check."
        ),
        "unchanged": f"{page!r} is byte-identical to the previous capture after "
                     "normalization.",
    }[status]

    return {
        "page": page,
        "extractor": current.get("extractor"),
        "url": current.get("url"),
        "captures": len(history),
        "current_hash": current.get("content_hash"),
        "previous_hash": previous.get("content_hash") if previous else None,
        "changed": changed,
        "status": status,
        "last_fetched": current.get("fetched_at"),
        "extraction_ok": extraction_ok,
        "severity": severity,
        "breaks": breaks,
        "assertion": assertion,
        "hash_history": [
            {"fetched_at": rec.get("fetched_at"), "content_hash": rec.get("content_hash")}
            for rec in history[:HASH_HISTORY]
        ],
        "detail": detail,
    }


def _check_headline(items: list[dict[str, Any]], changed: list[str],
                    failed: list[str], missing: list[str]) -> str:
    if not items:
        return "No pages to check."
    if not changed and not failed and not missing:
        return f"No drift: all {len(items)} page(s) match their previous capture."
    parts = []
    if changed:
        alarming = [i["page"] for i in items if i["changed"] and i["severity"] == "alarming"]
        parts.append(
            f"{len(changed)} page(s) changed ({', '.join(changed)})"
            + (f" -- {', '.join(alarming)} feed(s) something the app bundles" if alarming else "")
        )
    if failed:
        parts.append(f"{len(failed)} page(s) no longer extract ({', '.join(failed)})")
    if missing:
        parts.append(f"{len(missing)} page(s) never captured ({', '.join(missing)})")
    return "; ".join(parts) + "."


# ------------------------------------------------------------- should_fetch --

def should_fetch(page_key: str, last_fetched: datetime | None,
                 interval_hours: float, now: datetime) -> tuple[bool, str]:
    """Politeness gate. Returns (allowed, reason).

    Metro is a public agency whose infrastructure this tool is an unpaid guest
    on, and the relationship has to survive past submission (spec 9). HTML pages
    are capped at one fetch per day, so a configured interval below that floor
    is raised to it and the reason says so -- the failure mode of a mistyped
    `fetch_interval_hours` is a loop against someone else's web server, which
    nobody notices locally and everybody notices in their access logs.

    There is deliberately no `force` argument. A caller that decides to override
    the gate does so at the call site, where it is visible in review, rather
    than by passing a flag that defaults to skipping the check.
    """
    effective = max(float(interval_hours or 0.0), MIN_FETCH_INTERVAL_HOURS)
    floor_note = (
        f" (configured interval {float(interval_hours or 0.0):g} h raised to the "
        f"{MIN_FETCH_INTERVAL_HOURS:g} h floor: spec 9 caps HTML pages at one fetch "
        "per day.)"
        if float(interval_hours or 0.0) < MIN_FETCH_INTERVAL_HOURS else ""
    )

    if last_fetched is None:
        return True, (
            f"No capture of {page_key!r} on record; this is the first fetch and there is "
            f"no baseline to compare against yet.{floor_note}"
        )

    elapsed = (_as_utc(now) - _as_utc(last_fetched)).total_seconds() / 3600.0
    if elapsed < 0:
        return False, (
            f"The last capture of {page_key!r} is dated {abs(elapsed):.1f} h in the "
            "future. Refusing to fetch on a clock this confused: fix the system clock, "
            "or pass --as-of, before deciding the page is stale."
        )
    if elapsed >= effective:
        return True, (
            f"{elapsed:.1f} h since the last capture of {page_key!r}, at or past the "
            f"{effective:g} h interval.{floor_note}"
        )
    return False, (
        f"Only {elapsed:.1f} h since the last capture of {page_key!r}; the interval is "
        f"{effective:g} h, so the next fetch is in {effective - elapsed:.1f} h. Read the "
        f"stored capture instead -- it has not gone stale in that time.{floor_note}"
    )
