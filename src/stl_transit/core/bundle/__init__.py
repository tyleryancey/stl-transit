"""The artifacts the shipped Light Phone 3 tool actually carries.

Everything else in this repo answers questions. This module produces files that
land inside the APK, so every decision here is a decision about what a rider on
a monochrome phone with no map, no location API and finite storage can see.

Four constraints shape all of it:

1. **Money is integer cents, never a float.** 2.50 has no exact binary
   floating-point representation. A fare table that renders "2.4999999" is a
   bug that reaches riders, and it reaches them at a farebox.
2. **Output is deterministic** (spec 2.8). Fixed sort order, fixed indent,
   `sort_keys`, no wall-clock timestamps and no elapsed-time fields anywhere in
   a result. Two runs against one snapshot produce byte-identical artifacts,
   which is what makes them diffable in review and reproducible in a build.
3. **Bus and rail are different systems.** MetroBus runs *Sunday* service on a
   holiday while MetroLink runs *Weekend* service. Those are different concepts
   with different timetables; merging them is a wrong answer on Labor Day.
4. **A prune must never cost the rider something they would notice without
   saying so.** Dropping `shapes.txt` is invisible to a departures-only app.
   Dropping a `calendar_dates` exception is a wrong departure time on a
   holiday. `compact` reports both kinds, labelled, in one list -- the
   decisions list is as much the deliverable as the file is, because it is what
   a reviewer reads to judge whether the app is lying about its coverage.

Pure logic (spec 2.1): never prints, never exits, never prompts. `compact`
writes the one file it is asked to write and returns the path.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import zlib
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from ... import __version__
from ...errors import UsageError
from ...io.db import INDICES
from ..gtfs.calendar import active_services, parse_ymd
from ..gtfs.inspect import _columns, _tables

CURRENCY = "USD"

# 1 KiB = 1024 B. Spelled out because "MB" is ambiguous by a factor of 1.05 and
# a storage budget quoted in the wrong one is a budget nobody can check.
_UNITS = (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024), ("B", 1))


# ------------------------------------------------------------------ helpers --

def human_bytes(n: int | None) -> str:
    """Byte count a human can hold in their head. Binary units, always."""
    if n is None:
        return "n/a"
    for unit, size in _UNITS:
        if abs(n) >= size or unit == "B":
            return f"{n} B" if unit == "B" else f"{n / size:.1f} {unit}"
    return f"{n} B"


def canonical_json(payload: Any) -> str:
    """The exact text an artifact file gets. Deterministic by construction.

    `sort_keys` plus a fixed indent is the whole determinism contract for the
    JSON artifacts: dict insertion order stops mattering, so a refactor that
    builds the same data in a different order still produces the same file and
    a diff in review means the *feed* moved. `ensure_ascii=False` keeps stop
    names readable and the file smaller; it is UTF-8, which is what a Kotlin
    JSON reader assumes.
    """
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compressed_bytes(path: Path) -> int:
    """What this file costs in an APK, or in a git object.

    Both store their payloads deflated, so the on-disk size overstates what
    shipping or committing the file actually costs. Spec 12 item 2 -- whether
    the compact database gets committed -- turns on this number, not on the
    size of the file in a directory listing.
    """
    # Level 6, not 9: it is zlib's default and therefore what git's object
    # store and ordinary zip tooling actually apply, so it is the honest
    # estimate. Level 9 would flatter the number by a percent or two and cost
    # several times the CPU on a 50 MB feed.
    comp = zlib.compressobj(6)
    total = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            total += len(comp.compress(chunk))
    return total + len(comp.flush())


def _slug(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return out or "unnamed"


def _unique(slug: str, taken: set[str]) -> str:
    """A stable id even when two rows slug identically.

    Suffixing by collision order is deterministic because the caller feeds rows
    in a fixed order; picking at random or by dict order would change ids
    between runs and break every diff.
    """
    if slug not in taken:
        taken.add(slug)
        return slug
    i = 2
    while f"{slug}_{i}" in taken:
        i += 1
    taken.add(f"{slug}_{i}")
    return f"{slug}_{i}"


def _first(row: dict[str, Any], keys: Iterable[str]) -> str:
    """First non-empty value among `keys`, matched case/space-insensitively.

    The web extractors derive their keys from Metro's HTML table headers, which
    Metro rewords without warning ("Fare" -> "Price", "Bus" -> "MetroBus").
    Matching a list of spellings here means a header reword shows up as an
    empty field with a warning rather than as a silently empty fare table.
    """
    normalized = {_slug(str(k)): v for k, v in row.items()}
    for key in keys:
        value = normalized.get(_slug(key))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _db_path(conn: sqlite3.Connection) -> Path | None:
    """The file behind `conn`, or None for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


# --------------------------------------------------------------------- money --

_MONEY_RE = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d+))?$")

# Words Metro uses on the fares page where a number would go.
_FREE_WORDS = {"free", "no charge", "no fare", "0", "n/a"}


def parse_cents(value: Any) -> int:
    """A published price -> integer cents. The only money parser in this module.

    Decimal, not float: `float("2.50") * 100` is 250.00000000000003, and
    `int()` of a value like that silently becomes 249 for some inputs. Cents
    are exact, they are what a farebox deals in, and they are what the Kotlin
    side will store.

    More than two decimal places is refused rather than rounded. A fares page
    never prints thousandths of a dollar, so a third digit means the extractor
    grabbed the wrong cell -- and rounding it would bake that mistake into the
    APK looking like a real fare.
    """
    text = str(value or "").strip().replace(" ", " ")
    if text.lower() in _FREE_WORDS:
        return 0
    match = _MONEY_RE.match(text)
    if not match:
        raise UsageError(
            f"Cannot read {text!r} as a price.",
            remedy="Fares must arrive as a plain amount such as '$2.50', '2.50', "
            "'$1' or 'Free'. Re-run `stl web extract fares` and check the column "
            "the extractor picked -- a range ('$1-$3') or a footnote marker means "
            "it grabbed the wrong cell.",
            value=text,
        )
    whole, frac = match.group(1).replace(",", ""), match.group(2) or "0"
    if len(frac) > 2:
        raise UsageError(
            f"Price {text!r} carries {len(frac)} decimal places; money has two.",
            remedy="A fares page prints dollars and cents. A third digit means the "
            "extractor matched something that is not a price. Fix the extraction "
            "rather than rounding here.",
            value=text,
        )
    try:
        amount = Decimal(whole) * 100 + Decimal(frac.ljust(2, "0"))
    except InvalidOperation:  # pragma: no cover - regex already constrains this
        raise UsageError(f"Cannot read {text!r} as a price.", remedy="See `web extract fares`.")
    return int(amount)


def format_cents(cents: int) -> str:
    """250 -> '$2.50'. Integer arithmetic, mirroring the Kotlin helper exactly."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100}.{cents % 100:02d}"


# -------------------------------------------------------------------- kotlin --

_KOTLIN_ESCAPES = {"\\": "\\\\", '"': '\\"', "$": "\\$", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def kotlin_string(value: str) -> str:
    """A Kotlin string literal, quotes included.

    `$` is escaped because it opens a string template in Kotlin -- and a fare
    table is full of dollar signs, so an unescaped emitter would produce a file
    that does not compile at best and interpolates a variable at worst.
    """
    return '"' + "".join(_KOTLIN_ESCAPES.get(ch, ch) for ch in value) + '"'


def _kotlin_fares(fares: list[dict[str, Any]], as_of: str, source_url: str) -> str:
    lines = [
        f"// Generated by stl {__version__} -- `stl bundle fares --format kotlin`. DO NOT EDIT.",
        "//",
        "// Fares are not in the GTFS feed: Metro has no fare_attributes.txt, and",
        "// publishes prices only as an HTML table at",
        f"//   {source_url or '(source url not recorded)'}",
        f"// captured {as_of}.",
        "//",
        "// Regenerate when `stl assert run` reports `fares_unchanged` failing --",
        "// that assertion exists precisely because a bundled fare table that nobody",
        "// regenerated is a table that lies to riders. Hand-copying one into Kotlin",
        "// is how a stale fare ships.",
        "//",
        "// Add a `package` line to match the module this lands in. Nothing else here",
        "// needs editing.",
        "",
        "/** One row of Metro's published fare table. */",
        "data class Fare(",
        "    val id: String,",
        "    val mode: String,",
        "    val category: String,",
        "    val label: String,",
        "    /**",
        "     * Price in integer cents. Never Double: 2.50 has no exact binary",
        "     * floating-point representation, and a fare table that renders",
        '     * "2.4999999" is a bug that reaches riders at a farebox.',
        "     */",
        "    val priceCents: Int,",
        "    val notes: String,",
        ")",
        "",
        '/** 250 -> "$2.50", in integer arithmetic. Mirrors bundle.format_cents. */',
        "fun formatCents(cents: Int): String =",
        '    "\\$" + (cents / 100) + "." + (cents % 100).toString().padStart(2, \'0\')',
        "",
        f"/** Fares as published on {as_of}. Regenerate; do not hand-edit. */",
        "val FARES: List<Fare> = listOf(",
    ]
    for fare in fares:
        lines += [
            "    Fare(",
            f"        id = {kotlin_string(fare['id'])},",
            f"        mode = {kotlin_string(fare['mode'])},",
            f"        category = {kotlin_string(fare['category'])},",
            f"        label = {kotlin_string(fare['label'])},",
            f"        priceCents = {fare['price_cents']},",
            f"        notes = {kotlin_string(fare['notes'])},",
            "    ),",
        ]
    lines += [
        ")",
        "",
        f"const val FARES_AS_OF: String = {kotlin_string(as_of)}",
        f"const val FARES_SOURCE_URL: String = {kotlin_string(source_url)}",
        f"const val FARES_CURRENCY: String = {kotlin_string(CURRENCY)}",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------- fares --

FARE_LABEL_KEYS = ("label", "fare_type", "product", "pass", "name", "item", "description", "fare")
FARE_CATEGORY_KEYS = ("category", "rider", "rider_category", "rider_type", "type", "group")
FARE_MODE_KEYS = ("mode", "service", "system", "vehicle", "agency")
FARE_PRICE_KEYS = ("price", "price_usd", "amount", "cost", "fare_price", "value", "fare")
FARE_NOTE_KEYS = ("notes", "note", "detail", "details", "conditions", "footnote")


def fares(rows: list[dict[str, Any]], as_of: date, source_url: str,
          fmt: str = "json") -> dict[str, Any]:
    """The bundled fare table, with `as_of` and `source_url` baked in.

    `rows` arrives from the web group's `fare_table` extractor rather than from
    a database connection because Metro publishes no fare files in the GTFS
    feed (see the `no_fare_files` assumption, spec 6.10) -- prices live on a web
    page, so the fare table is a scrape with a date on it, and the date is part
    of the artifact.

    `fmt="kotlin"` emits a compilable Kotlin source string: a data class and an
    immutable `listOf`. Generating it is the point -- the alternative is a
    human retyping prices into Kotlin, which is exactly how a stale fare ships.

    Returns the parsed table plus `rendered`, the exact text to write to
    `--out`, and its sha256 for the manifest.
    """
    if fmt not in ("json", "kotlin"):
        raise UsageError(
            f"Unknown fare format {fmt!r}.",
            remedy="Use --format json (the bundled asset) or --format kotlin "
            "(a compilable source file for the tool repo).",
            format=fmt, available=["json", "kotlin"],
        )
    as_of_iso = as_of.isoformat() if isinstance(as_of, date) else str(as_of)

    parsed: list[dict[str, Any]] = []
    warnings: list[str] = []
    taken: set[str] = set()
    for index, row in enumerate(rows or []):
        label = _first(row, FARE_LABEL_KEYS)
        category = _first(row, FARE_CATEGORY_KEYS)
        mode = _first(row, FARE_MODE_KEYS)
        raw_price = _first(row, FARE_PRICE_KEYS)
        if not raw_price:
            warnings.append(
                f"Row {index} ({label or category or 'unlabelled'!r}) carries no price "
                "column this parser recognises; it is not in the bundled table."
            )
            continue
        cents = parse_cents(raw_price)
        parsed.append(
            {
                "id": _unique(_slug(" ".join(p for p in (mode, category, label) if p)), taken),
                "mode": mode,
                "category": category,
                "label": label,
                "price_cents": cents,
                # Rendered FROM the cents, never carried through from the page:
                # display and value cannot disagree if one is derived from the
                # other, and the round trip is what proves the parse.
                "price": format_cents(cents),
                "price_as_published": raw_price,
                "notes": _first(row, FARE_NOTE_KEYS),
            }
        )
    # Sorted, not source-ordered: Metro reorders its own table between
    # redesigns, and a bundled artifact that reshuffles for that reason
    # produces a diff nobody can read (spec 2.8).
    parsed.sort(key=lambda f: (f["mode"], f["category"], f["label"], f["price_cents"], f["id"]))

    payload = {
        "as_of": as_of_iso,
        "source_url": source_url,
        "currency": CURRENCY,
        "generated_by": f"stl {__version__}",
        "fares": parsed,
        "note": "price_cents is authoritative. `price` is rendered from it, and any "
                "consumer that re-parses the string instead of reading the integer "
                "has reintroduced the floating-point bug this format exists to avoid.",
    }
    rendered = _kotlin_fares(parsed, as_of_iso, source_url) if fmt == "kotlin" \
        else canonical_json(payload)
    name = "Fares.kt" if fmt == "kotlin" else "fares.json"
    if not parsed:
        warnings.append("No fares parsed. The bundled table would be empty -- check "
                        "`stl web extract fares` before shipping this.")
    return {
        "ok": True,
        "provenance": None,
        "warnings": warnings,
        "notes": [],
        "format": fmt,
        **payload,
        "count": len(parsed),
        "total": len(parsed),
        "rendered": rendered,
        "artifact": {
            "name": name,
            "kind": "fares",
            "rows": len(parsed),
            "bytes": len(rendered.encode("utf-8")),
            "sha256": sha256_text(rendered),
            "as_of": as_of_iso,
            "source_url": source_url,
        },
    }


# ------------------------------------------------------------------ holidays --

HOLIDAY_NAME_KEYS = ("holiday", "name", "holiday_name", "occasion", "event", "day")
HOLIDAY_DATE_KEYS = ("date", "observed", "observed_date", "when", "day_of")
BUS_KEYS = ("bus", "metrobus", "metro_bus", "bus_service", "bus_schedule")
RAIL_KEYS = ("rail", "metrolink", "metro_link", "train", "light_rail", "rail_service")
PARATRANSIT_KEYS = ("call_a_ride", "callaride", "call_a_ride_service", "paratransit", "van")

# Metro's own vocabulary, mapped to a token the app can branch on. The raw
# published text is always kept beside it: this map is a convenience, and if
# Metro invents a word it must show up as "unrecognised" rather than be
# silently coerced into the nearest known one.
SERVICE_TYPES = (
    ("no service", "no_service"),
    ("holiday", "holiday"),
    ("weekend", "weekend"),
    ("sunday", "sunday"),
    ("saturday", "saturday"),
    ("weekday", "weekday"),
    ("regular", "regular"),
    ("normal", "regular"),
    ("modified", "modified"),
    ("reduced", "reduced"),
)

# Written out rather than taken from strptime("%B"): %B is locale-dependent, so
# the same feed parsed on a machine with a non-English locale would silently
# produce different dates.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def normalize_service_type(text: str) -> str:
    lowered = (text or "").strip().lower()
    if not lowered:
        return "unknown"
    for needle, token in SERVICE_TYPES:
        if needle in lowered:
            return token
    return "unrecognised"


def parse_holiday_date(value: str, year: int) -> date | None:
    """A published holiday date -> a real date, or None.

    Metro writes these several ways across page revisions ("2026-09-07",
    "September 7", "Sept. 7, 2026", "9/7"). A year-less form is resolved
    against `year`, which is why `year` is a parameter and not derived.
    """
    text = (value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    if re.fullmatch(r"\d{8}", text):
        return parse_ymd(text)
    slash = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text)
    if slash:
        month, day, yr = int(slash.group(1)), int(slash.group(2)), slash.group(3)
        resolved = year if yr is None else (int(yr) + 2000 if len(yr) == 2 else int(yr))
        return _safe_date(resolved, month, day)
    named = re.match(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:\s*,\s*(\d{4}))?", text)
    if named:
        month = _MONTHS.get(named.group(1)[:4].lower().rstrip(".")) or \
            _MONTHS.get(named.group(1)[:3].lower())
        if month:
            return _safe_date(int(named.group(3) or year), month, int(named.group(2)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def holidays(rows: list[dict[str, Any]], year: int, source_url: str) -> dict[str, Any]:
    """holiday -> service-type mapping, with bus and rail kept apart.

    This is the artifact that decides which timetable the app shows on Labor
    Day, and the one thing it must not do is merge the two systems. MetroBus
    runs *Sunday* service on a holiday; MetroLink runs *Weekend* service. They
    are different words for different timetables, they are published in
    different columns, and an app that collapses them shows a rider the wrong
    train times on exactly the day they are least able to check.

    Per-mode entries are always emitted, even when the page gives them the same
    value, so a later page revision that changes one and not the other shows up
    as a one-line diff instead of a judgement call.
    """
    parsed: list[dict[str, Any]] = []
    warnings: list[str] = []
    notes: list[str] = []
    skipped: list[dict[str, Any]] = []
    modes_seen: set[str] = set()

    for index, row in enumerate(rows or []):
        name = _first(row, HOLIDAY_NAME_KEYS)
        raw_date = _first(row, HOLIDAY_DATE_KEYS)
        when = parse_holiday_date(raw_date, year)
        services: dict[str, dict[str, str]] = {}
        for mode, keys in (("bus", BUS_KEYS), ("rail", RAIL_KEYS),
                           ("paratransit", PARATRANSIT_KEYS)):
            published = _first(row, keys)
            if not published:
                continue
            modes_seen.add(mode)
            services[mode] = {
                "published": published,
                "service_type": normalize_service_type(published),
            }
        if when is None:
            skipped.append({"row": index, "name": name, "date_as_published": raw_date,
                            "reason": "date could not be resolved"})
            warnings.append(
                f"Row {index} ({name or 'unnamed'}): cannot resolve "
                f"{raw_date!r} to a date in {year}. It is not in the bundled mapping."
            )
            continue
        if when.year != year:
            skipped.append({"row": index, "name": name, "date": when.isoformat(),
                            "reason": f"belongs to {when.year}, not {year}"})
            continue
        if not services:
            warnings.append(
                f"Row {index} ({name or when.isoformat()}) names no service column this "
                "parser recognises (bus / rail / call-a-ride), so the app has nothing "
                "to branch on for that date."
            )
        parsed.append(
            {
                "date": when.isoformat(),
                "name": name,
                # The weekday is the sanity check a reader performs anyway:
                # Labor Day 2026-09-07 must read Monday, and if it reads Sunday
                # the year or the date is wrong.
                "weekday": when.strftime("%A"),
                "services": services,
                # Recorded per row, not per file: this is what tells a reviewer
                # whether the day is one where the two systems diverge.
                "bus_rail_differ": bool(
                    "bus" in services and "rail" in services
                    and services["bus"]["service_type"] != services["rail"]["service_type"]
                ),
            }
        )
    parsed.sort(key=lambda h: (h["date"], h["name"]))

    if "bus" in modes_seen and "rail" not in modes_seen:
        warnings.append(
            "The extractor supplied a bus column but no rail column. MetroBus runs "
            "Sunday service on a holiday while MetroLink runs Weekend service -- those "
            "are different timetables. Do not ship this: fix the holiday_table "
            "extractor so both columns are captured."
        )
    if "rail" in modes_seen and "bus" not in modes_seen:
        warnings.append(
            "The extractor supplied a rail column but no bus column. Bundling rail's "
            "answer for both systems would show bus riders the wrong timetable."
        )
    if not parsed:
        warnings.append(f"No holidays resolved for {year}. Check `stl web extract holidays`.")
    differ = sum(1 for h in parsed if h["bus_rail_differ"])
    notes.append(
        f"{differ} of {len(parsed)} holiday(s) give bus and rail different service "
        "types. That count is the reason the two are stored separately; the day it "
        "reads 0 is not the day to merge them, because the next pick can change it."
    )

    payload = {
        "year": year,
        "source_url": source_url,
        "generated_by": f"stl {__version__}",
        "modes": sorted(modes_seen),
        "service_types": sorted({t for _, t in SERVICE_TYPES}),
        "holidays": parsed,
        "note": "Each holiday carries one entry PER MODE. Bus and rail are never "
                "merged: 'Sunday service' (bus) and 'Weekend service' (rail) are "
                "different timetables published in different columns.",
    }
    rendered = canonical_json(payload)
    return {
        "ok": True,
        "provenance": None,
        "warnings": warnings,
        "notes": notes,
        **payload,
        "count": len(parsed),
        "total": len(parsed),
        "skipped": skipped,
        "bus_rail_differ_count": differ,
        "rendered": rendered,
        "artifact": {
            "name": f"holidays-{year}.json",
            "kind": "holidays",
            "rows": len(parsed),
            "bytes": len(rendered.encode("utf-8")),
            "sha256": sha256_text(rendered),
            "source_url": source_url,
        },
    }


# --------------------------------------------------------------- stops index --

def stops_index(conn: sqlite3.Connection, include_routes: bool = True) -> dict[str, Any]:
    """stop_code -> (stop_id, name, routes), for on-device resolution.

    The Light SDK exposes no usable location API, so "stops near me" is not
    buildable and typing the number printed on the pole is the app's entire
    input UX. This index sits on the hot path of the only interaction the tool
    has, which is why it is a flat map rather than a query against the bundled
    database: a keypad entry should resolve without touching SQLite at all.

    `include_routes` costs bytes and one scan of stop_times. It buys the route
    badges shown beside a stop before any departure is computed, which is how a
    rider confirms they typed the right pole.
    """
    tables = _tables(conn)
    warnings: list[str] = []
    notes: list[str] = []
    if "stops" not in tables:
        raise UsageError(
            "This snapshot has no stops table.",
            remedy="Rebuild the SQLite index with `stl gtfs import --force`; if it is "
            "still absent the downloaded feed is not a GTFS zip.",
            tables=sorted(tables),
        )
    cols = set(_columns(conn, "stops"))
    if "stop_code" not in cols:
        raise UsageError(
            "This feed's stops table has no stop_code column.",
            remedy="Run `stl gtfs stop-resolve` to find which field carries the number "
            "printed on the pole. The app's whole input UX depends on the answer, so "
            "this is a design decision, not a fallback this function should guess at.",
            columns=sorted(cols),
        )

    routes_by_stop: dict[str, list[str]] = {}
    if include_routes:
        missing = [t for t in ("stop_times", "trips", "routes") if t not in tables]
        if missing:
            warnings.append(
                f"Route badges omitted: {', '.join(missing)} absent from this snapshot. "
                "The index still resolves stop numbers; it just cannot say which routes "
                "serve them."
            )
        else:
            name_col = "route_short_name" if "route_short_name" in set(
                _columns(conn, "routes")) else "route_id"
            for stop_id, label in conn.execute(
                "SELECT DISTINCT st.stop_id, "
                # Fall back to route_id per row, not per feed: one unnamed route
                # should not blank the badges on every other route's stops.
                f'CASE WHEN r."{name_col}" <> \'\' THEN r."{name_col}" ELSE r.route_id END '
                "FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id "
                "JOIN routes r ON r.route_id = t.route_id"
            ):
                routes_by_stop.setdefault(stop_id, []).append(label)

    has_name = "stop_name" in cols
    has_parent = "parent_station" in cols
    has_type = "location_type" in cols
    index: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    without_code: list[str] = []
    for row in conn.execute('SELECT * FROM stops ORDER BY "stop_id"'):
        rec = dict(row)
        code = (rec.get("stop_code") or "").strip()
        stop_id = rec["stop_id"]
        if not code:
            without_code.append(stop_id)
            continue
        entry = {
            "stop_id": stop_id,
            "name": (rec.get("stop_name") or "").strip() if has_name else "",
            # Sorted and deduped: two runs must produce the same list, and the
            # app renders it as-is.
            "routes": sorted(set(routes_by_stop.get(stop_id, [])), key=_route_sort_key),
        }
        if has_parent and (rec.get("parent_station") or "").strip():
            entry["parent_station"] = rec["parent_station"].strip()
        if has_type and (rec.get("location_type") or "").strip():
            entry["location_type"] = rec["location_type"].strip()
        if code in index:
            # Never silently overwrite: a duplicate code means a rider typing
            # that number gets a coin flip between two poles. The first by
            # stop_id wins so the artifact is stable, and the collision is
            # reported so `assert run`'s stop_code_unique has something to point
            # at.
            duplicates.append({"stop_code": code, "kept": index[code]["stop_id"],
                               "also": stop_id})
            continue
        index[code] = entry

    if duplicates:
        warnings.append(
            f"{len(duplicates)} stop_code(s) are shared by more than one stop. The "
            "lowest stop_id wins in this index and the rest are unreachable by number "
            "-- see the `stop_code_unique` assumption (spec 6.10)."
        )
    if without_code:
        warnings.append(
            f"{len(without_code)} stop(s) carry no stop_code and cannot be reached by "
            "number entry at all."
        )
    payload = {
        "generated_by": f"stl {__version__}",
        "includes_routes": bool(include_routes and routes_by_stop),
        "stops": index,
        "note": "Keyed by stop_code -- the number printed on the pole, which is what "
                "the rider types. stop_id is the join key into the bundled database "
                "and is never shown.",
    }
    rendered = canonical_json(payload)
    total_stops = conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
    notes.append(
        f"{len(index)} of {total_stops} stops are reachable by number entry."
    )
    return {
        "ok": True,
        "provenance": None,
        "warnings": warnings,
        "notes": notes,
        **payload,
        "count": len(index),
        "total": len(index),
        "stops_in_feed": total_stops,
        "stops_without_code": without_code,
        "duplicate_codes": duplicates,
        "bytes": len(rendered.encode("utf-8")),
        "human_bytes": human_bytes(len(rendered.encode("utf-8"))),
        "rendered": rendered,
        "artifact": {
            "name": "stops_index.json",
            "kind": "stops_index",
            "rows": len(index),
            "bytes": len(rendered.encode("utf-8")),
            "sha256": sha256_text(rendered),
        },
    }


def _route_sort_key(label: str) -> tuple[int, int, str]:
    """Route badges sorted the way a rider reads them: 2, 11, 70, then RED.

    Plain lexicographic ordering puts route 11 before route 2, which looks like
    a bug on a screen that shows four badges.
    """
    return (0, int(label), "") if label.isdigit() else (1, 0, label)


# ------------------------------------------------------------------- compact --

STRATEGIES = ("minimal", "balanced", "full")

# Tables the departures engine cannot answer without. Dropping one of these is
# not a size decision, it is a wrong answer, so no strategy drops them.
ESSENTIAL_TABLES = {
    "agency": "agency_timezone. Every service-day calculation is measured in it.",
    "stops": "The rider types a number; without stops there is nothing to resolve.",
    "routes": "The route badge on every row of the board.",
    "trips": "Joins a stop_time to its route, headsign and service_id.",
    "stop_times": "The departures themselves.",
    "calendar": "Which service_ids run on which weekday.",
    "calendar_dates": "Holiday and one-off exceptions. Pruning one of these is a "
                      "wrong departure time on Labor Day, not a smaller file.",
    # Absent from Metro's feed today (the `no_frequencies_file` assumption), and
    # if it ever appears the app needs a whole second code path rather than a
    # smaller database. Kept at every strategy so its appearance is loud.
    "frequencies": "Headway-based trips. If this is present the app cannot compute "
                   "departures from stop_times alone.",
}

# Everything else, with the strategies at which it survives. `costs` is what the
# app gives up; `visible` is whether a rider would ever notice.
OPTIONAL_TABLES: dict[str, dict[str, Any]] = {
    "shapes": {
        "keep_at": {"full"},
        "why": "Route geometry exists to draw a line on a map. This tool has no map "
               "and the LP3 has no map surface, so the polyline is never rendered.",
        "costs": "A future map view would need the feed re-imported.",
        "visible": False,
    },
    "transfers": {
        "keep_at": {"full"},
        "why": "Transfer rules feed a trip planner. This tool answers 'when does the "
               "next bus leave this pole', which never crosses two routes.",
        "costs": "No trip planning, no 'you can make this connection' hint.",
        "visible": False,
    },
    "pathways": {
        "keep_at": {"full"},
        "why": "In-station walking graph. Needs a map and a rendering surface.",
        "costs": "No in-station wayfinding.",
        "visible": False,
    },
    "levels": {
        "keep_at": {"full"},
        "why": "Station levels; only meaningful alongside pathways.",
        "costs": "No in-station wayfinding.",
        "visible": False,
    },
    "translations": {
        "keep_at": {"full"},
        "why": "The tool ships English only.",
        "costs": "Localisation would need the feed re-imported.",
        "visible": False,
    },
    "feed_info": {
        "keep_at": {"balanced", "full"},
        "why": "Feed version and validity dates. Two rows at most, but nothing on the "
               "departures path reads it.",
        "costs": "The app cannot show which feed version it is running, which is the "
                 "first thing a support conversation asks for.",
        "visible": True,
    },
    "attributions": {
        "keep_at": {"balanced", "full"},
        "why": "Small, and it can carry attribution text a publisher requires.",
        "costs": "If Metro ever populates it, dropping it may drop a required credit. "
                 "Reproduce the attribution in the About screen before pruning this.",
        "visible": True,
    },
}
for _fare_table in ("fare_attributes", "fare_rules", "fare_products", "fare_media",
                    "fare_leg_rules", "fare_transfer_rules", "rider_categories",
                    "timeframes", "areas", "networks"):
    OPTIONAL_TABLES[_fare_table] = {
        "keep_at": {"full"},
        "why": "Absent from Metro's feed today; fares are bundled from the website "
               "instead (see `bundle fares`).",
        "costs": "If Metro ever publishes fares in the feed, the bundled fare table "
                 "should be replaced by these rather than pruned.",
        "visible": False,
    }

# Columns kept per strategy. `full` keeps every column of every table and so has
# no entry here. A column named but absent from the feed is simply skipped.
COLUMN_PLAN: dict[str, dict[str, list[str]]] = {
    "stop_times": {
        "minimal": ["trip_id", "stop_id", "stop_sequence", "departure_time", "pickup_type"],
        "balanced": ["trip_id", "stop_id", "stop_sequence", "departure_time",
                     "arrival_time", "pickup_type", "drop_off_type"],
    },
    "stops": {
        "minimal": ["stop_id", "stop_code", "stop_name", "parent_station", "location_type"],
        "balanced": ["stop_id", "stop_code", "stop_name", "parent_station", "location_type",
                     "wheelchair_boarding"],
    },
    "trips": {
        "minimal": ["trip_id", "route_id", "service_id", "trip_headsign", "direction_id"],
        "balanced": ["trip_id", "route_id", "service_id", "trip_headsign", "direction_id",
                     "wheelchair_accessible"],
    },
    "routes": {
        "minimal": ["route_id", "route_short_name", "route_long_name", "route_type"],
        "balanced": ["route_id", "route_short_name", "route_long_name", "route_type"],
    },
    "agency": {
        "minimal": ["agency_id", "agency_name", "agency_timezone", "agency_url"],
        "balanced": ["agency_id", "agency_name", "agency_timezone", "agency_url",
                     "agency_phone", "agency_lang"],
    },
}

# Why a given column can go, and what goes with it.
COLUMN_RATIONALE: dict[tuple[str, str], dict[str, Any]] = {
    ("stop_times", "arrival_time"): {
        "why": "A departure board answers 'when does it leave'. Arrival differs from "
               "departure only at timepoints, and this tool never shows it.",
        "costs": "No 'arrives 12:03, departs 12:05' at layover stops, and no basis for "
                 "a future 'when do I get there' feature.",
        "visible": True,
    },
    ("stop_times", "drop_off_type"): {
        "why": "Governs alighting, not boarding. The board only ever needs to know "
               "whether a rider can get ON.",
        "costs": "Cannot warn that a call is boarding-only.",
        "visible": False,
    },
    ("stop_times", "timepoint"): {
        "why": "Marks whether a time is exact or interpolated. Metro publishes exact "
               "times for the calls this tool renders.",
        "costs": "Cannot mark an approximate time as approximate.",
        "visible": False,
    },
    ("stop_times", "shape_dist_traveled"): {
        "why": "Distance along the shape. Meaningless without shapes.txt, which no "
               "strategy below `full` keeps.",
        "costs": "Nothing this tool can render.",
        "visible": False,
    },
    ("stop_times", "stop_headsign"): {
        "why": "Overrides trip_headsign for one call. Dropped only when it is empty on "
               "every row of the feed, which is checked rather than assumed.",
        "costs": "If ever populated, dropping it shows the WRONG destination for those "
                 "calls. Never drop it unpopulated-unchecked.",
        "visible": True,
    },
    ("stops", "stop_lat"): {
        "why": "The Light SDK exposes no usable location API, so nothing on the device "
               "can consume a coordinate: no map, no 'stops near me', no distance.",
        "costs": "Any future location feature needs the feed re-imported.",
        "visible": False,
    },
    ("stops", "stop_lon"): {
        "why": "See stop_lat -- there is no consumer for a coordinate on this device.",
        "costs": "Any future location feature needs the feed re-imported.",
        "visible": False,
    },
    ("stops", "stop_desc"): {
        "why": "Free-text description, usually empty and never rendered on a 1.8in "
               "monochrome screen that shows a stop name and four departures.",
        "costs": "No long-form stop description.",
        "visible": False,
    },
    ("stops", "wheelchair_boarding"): {
        "why": "Not needed to compute a departure.",
        "costs": "A wheelchair user cannot tell from the app whether a stop is "
                 "accessible. This is a real feature for a real rider, which is why "
                 "`balanced` keeps it and only `minimal` drops it.",
        "visible": True,
    },
    ("trips", "wheelchair_accessible"): {
        "why": "Not needed to compute a departure.",
        "costs": "Cannot tell a rider whether the specific vehicle is accessible.",
        "visible": True,
    },
    ("trips", "block_id"): {
        "why": "Vehicle blocking; used for interlining analysis, never rendered.",
        "costs": "No 'this bus becomes route 11' continuity.",
        "visible": False,
    },
    ("trips", "shape_id"): {
        "why": "A pointer into shapes.txt, which no strategy below `full` keeps.",
        "costs": "Nothing this tool can render.",
        "visible": False,
    },
    ("routes", "route_color"): {
        "why": "The LP3 screen is monochrome. A hex colour cannot be rendered on it.",
        "costs": "Nothing on this hardware. A colour device would want it back.",
        "visible": False,
    },
    ("routes", "route_text_color"): {
        "why": "See route_color -- monochrome screen.",
        "costs": "Nothing on this hardware.",
        "visible": False,
    },
}

# Indices are not free: on Metro's feed they are 19.7 MiB of a 52.9 MiB
# database, so which ones get rebuilt into the bundle is as much a size
# decision as which columns do. These are the ones a departures-only app
# actually queries; `minimal` builds only these.
DEPARTURES_INDICES = {
    "idx_st_stop": "The app's only hot query: departures at one stop, in time order.",
    "idx_stops_code": "Resolving the number a rider types off the pole.",
    "idx_stops_parent": "Station platforms resolve through their parent.",
    "idx_cd_date": "Calendar exceptions for a date -- the holiday path.",
    "idx_trips_service": "Trips for the service_ids active on a date.",
}

# Why an index can be skipped, and what gets slower without it.
INDEX_RATIONALE = {
    "idx_st_trip": {
        "why": "Indexes stop_times by trip, which answers 'every call on this trip'. "
               "A departures board never asks that -- it asks about one stop.",
        "costs": "A 'rest of this trip' view would scan all 489k stop_times rows. Add "
                 "the index back before building that feature, not after.",
    },
    "idx_trips_route": {
        "why": "Indexes trips by route, for browsing a route's timetable. The board is "
               "entered by stop number, never by route.",
        "costs": "A route-timetable view would scan the trips table -- roughly 9,600 "
                 "rows, which is survivable, unlike the stop_times scan above.",
    },
}

# Copy order. Explicit rather than alphabetical because stop_times must be
# copied before stops: when a filter is active, the surviving stop_ids are
# collected during that copy and decide which stops survive.
COPY_ORDER = ["agency", "calendar", "calendar_dates", "routes", "trips", "stop_times", "stops"]

# Deterministic row order per table. stop_times is ordered by the app's only hot
# query (stop_id, then time) so the rows a single lookup needs land on adjacent
# pages; the trailing terms exist purely to make ties deterministic, since a
# loop route can call the same stop twice at the same minute.
SORT_KEYS: dict[str, list[str]] = {
    "agency": ["agency_id"],
    "calendar": ["service_id"],
    "calendar_dates": ["date", "service_id", "exception_type"],
    "routes": ["route_id"],
    "trips": ["trip_id"],
    "stop_times": ["stop_id", "departure_time", "trip_id", "stop_sequence"],
    "stops": ["stop_id"],
}


def _column_stats(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, int]]:
    """Per-column non-empty count and total text length, in one scan.

    Both numbers earn their scan. The non-empty count is what lets a column be
    dropped as "empty on all 489,011 rows" -- a measured claim rather than an
    assumption -- and the text length is the honest estimate of what dropping it
    saves.
    """
    cols = _columns(conn, table)
    if not cols:
        return {}
    parts = []
    for col in cols:
        parts.append(f'SUM(CASE WHEN "{col}" IS NOT NULL AND "{col}" <> \'\' THEN 1 ELSE 0 END)')
        parts.append(f'SUM(LENGTH(COALESCE("{col}", \'\')))')
    row = conn.execute(f'SELECT {", ".join(parts)} FROM "{table}"').fetchone()
    return {
        col: {"non_empty": int(row[i * 2] or 0), "text_bytes": int(row[i * 2 + 1] or 0)}
        for i, col in enumerate(cols)
    }


def _index_bytes(db_path: Path) -> int | None:
    """Bytes the indices occupy, isolated from row data.

    Measured by copying the file, dropping every index and VACUUMing: the
    difference is the index. The `dbstat` virtual table would answer this
    directly but it is an optional compile-time extension that stock Python
    builds do not carry, so relying on it would make the size report work on
    one machine and silently return nothing on another.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "no-index.sqlite"
            shutil.copy2(db_path, copy)
            conn = sqlite3.connect(copy)
            try:
                # A throwaway file in a temp directory: durability is worth
                # nothing here and fsync on VACUUM costs whole seconds on some
                # filesystems.
                conn.execute("PRAGMA journal_mode=OFF")
                conn.execute("PRAGMA synchronous=OFF")
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")]
                for name in names:
                    conn.execute(f'DROP INDEX "{name}"')
                conn.commit()
                conn.execute("VACUUM")
            finally:
                conn.close()
            return db_path.stat().st_size - copy.stat().st_size
    except (OSError, sqlite3.Error):  # pragma: no cover - measurement is never fatal
        return None


def _plan_columns(conn: sqlite3.Connection, table: str, strategy: str,
                  stats: dict[str, dict[str, int]], rows: int) -> tuple[list[str], list[dict]]:
    """Columns to keep for `table`, and one decision per column dropped."""
    present = _columns(conn, table)
    decisions: list[dict[str, Any]] = []

    # Rule 1, applied before any strategy: a column empty on every row carries
    # no information, so dropping it cannot cost anything. Measured, not
    # assumed -- and `full` is exempt, because `full` is the faithful copy the
    # others are measured against.
    keep = list(present)
    if strategy != "full" and rows:
        empty = [c for c in present if stats.get(c, {}).get("non_empty", 0) == 0]
        for col in empty:
            rationale = COLUMN_RATIONALE.get((table, col), {})
            decisions.append(_decision(
                "drop_column", f"{table}.{col}",
                f"Dropped: empty on all {rows:,} rows of {table}.",
                rationale.get("why", "") or
                "The column exists in the header but no row populates it in this "
                "snapshot, so nothing can read it. Verified by counting, not assumed.",
                visible=False,
                costs="Nothing in this snapshot. If a later feed populates it, this "
                      "check will keep the column automatically.",
                bytes_saved=stats.get(col, {}).get("text_bytes", 0) + rows,
            ))
        keep = [c for c in keep if c not in set(empty)]

    # Rule 2: the strategy's keep-list, intersected with what the feed has.
    plan = COLUMN_PLAN.get(table, {}).get(strategy)
    if plan is not None:
        wanted = [c for c in plan if c in set(keep)]
        for col in keep:
            if col in set(wanted):
                continue
            rationale = COLUMN_RATIONALE.get((table, col), {})
            decisions.append(_decision(
                "drop_column", f"{table}.{col}",
                f"Dropped at strategy '{strategy}' ({rows:,} rows, "
                f"{human_bytes(stats.get(col, {}).get('text_bytes', 0))} of text).",
                rationale.get("why") or
                "Not on the departures path. Not in this strategy's keep-list, and no "
                "rationale is recorded for it -- review before relying on this.",
                visible=bool(rationale.get("visible", False)),
                costs=rationale.get("costs", "Unrecorded. Treat as unknown."),
                bytes_saved=stats.get(col, {}).get("text_bytes", 0) + rows,
                review=not rationale,
            ))
        keep = wanted
    return keep, decisions


def _decision(action: str, target: str, detail: str, rationale: str, visible: bool,
              costs: str, bytes_saved: int | None = None,
              review: bool = False) -> dict[str, Any]:
    """One pruning decision. Every field is mandatory on purpose.

    A decision without a rationale is a decision nobody can review, and this
    list is what a reviewer reads instead of diffing two SQLite files.
    """
    out = {
        "action": action,
        "target": target,
        "detail": detail,
        "rationale": rationale,
        # The distinction the whole list exists for: pruning shapes.txt is
        # invisible, pruning a calendar exception is a wrong answer on Labor Day.
        "visible_to_user": visible,
        "costs": costs,
        "bytes_saved_estimate": bytes_saved,
        "bytes_saved_human": human_bytes(bytes_saved) if bytes_saved is not None else "n/a",
    }
    if review:
        out["review"] = True
    return out


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str,
                columns: list[str], keep_when: tuple[str, set[str]] | None = None,
                collect: str | None = None) -> tuple[int, set[str]]:
    """Stream one table into the compact database, filtered and ordered.

    Rows move through Python rather than through `INSERT ... SELECT` over an
    ATTACHed database because the source is opened read-only (spec 9) and
    ATTACH is denied at the driver. At 489k rows the cost is a couple of
    seconds and the filter stays legible.
    """
    coldefs = ", ".join(f'"{c}" TEXT' for c in columns)
    dst.execute(f'CREATE TABLE "{table}" ({coldefs})')

    source_cols = set(_columns(src, table))
    order = [c for c in SORT_KEYS.get(table, columns) if c in source_cols]
    order_sql = (" ORDER BY " + ", ".join(f'"{c}"' for c in order)) if order else ""

    # The filter and collect columns are selected even when they are being
    # dropped from the output -- a row cannot be filtered on a column that was
    # never read.
    wanted = list(dict.fromkeys(
        [*columns,
         *([keep_when[0]] if keep_when else []),
         *([collect] if collect else [])]
    ))
    select = ", ".join(f'"{c}"' for c in wanted)
    cur = src.execute(f'SELECT {select} FROM "{table}"{order_sql}')
    names = [d[0] for d in cur.description]
    take = [names.index(c) for c in columns]
    filter_idx = names.index(keep_when[0]) if keep_when else None
    collect_idx = names.index(collect) if collect else None
    allowed = keep_when[1] if keep_when else None

    stmt = f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(columns))})'
    collected: set[str] = set()
    written = 0
    batch: list[tuple] = []
    for row in cur:
        if allowed is not None and row[filter_idx] not in allowed:
            continue
        if collect_idx is not None:
            collected.add(row[collect_idx])
        batch.append(tuple(row[i] for i in take))
        if len(batch) >= 20_000:
            dst.executemany(stmt, batch)
            written += len(batch)
            batch.clear()
    if batch:
        dst.executemany(stmt, batch)
        written += len(batch)
    return written, collected


def _service_window(conn: sqlite3.Connection) -> tuple[date | None, date | None]:
    tables = _tables(conn)
    starts: list[date] = []
    ends: list[date] = []
    if "calendar" in tables:
        row = conn.execute("SELECT MIN(start_date), MAX(end_date) FROM calendar").fetchone()
        if row and row[0]:
            starts.append(parse_ymd(row[0]))
            ends.append(parse_ymd(row[1]))
    if "calendar_dates" in tables:
        row = conn.execute("SELECT MIN(date), MAX(date) FROM calendar_dates").fetchone()
        if row and row[0]:
            starts.append(parse_ymd(row[0]))
            ends.append(parse_ymd(row[1]))
    return (min(starts) if starts else None), (max(ends) if ends else None)


def compact(conn: sqlite3.Connection, out_path: Path, strategy: str = "balanced",
            routes: list[str] | None = None, days: int | None = None) -> dict[str, Any]:
    """Build the pruned on-device SQLite database and report every decision.

    Strategies, and what each one costs:

    - **minimal** -- departures only. Drops shapes, transfers, feed_info,
      attributions and every fare table; drops arrival_time, drop_off_type,
      timepoint and shape_dist_traveled from stop_times; drops coordinates,
      descriptions and `wheelchair_boarding` from stops; drops block_id,
      shape_id and `wheelchair_accessible` from trips; drops route colours.
      The visible cost is accessibility information: a wheelchair user cannot
      tell from the app whether a stop or a vehicle is accessible. Do not ship
      this without deciding that deliberately.
    - **balanced** (default) -- everything minimal keeps, plus the accessibility
      flags, feed_info and attributions. Costs arrival times at layover stops
      and any future map or trip-planning feature.
    - **full** -- drops nothing. It exists as the control the other two are
      measured against and as the escape hatch when a pruned field turns out to
      matter. Still smaller than the imported feed, because it is written in
      the app's access order and VACUUMed.

    Independent of strategy: `calendar_dates` is never pruned by table, and
    `frequencies` is never dropped if it appears. `--routes` and `--days` are
    the two prunes that remove coverage a rider could ask for, and both are
    reported as visible.
    """
    if strategy not in STRATEGIES:
        raise UsageError(
            f"Unknown compaction strategy {strategy!r}.",
            remedy="Use one of: minimal (departures only), balanced (default), "
            "full (nothing dropped). Run `stl bundle size-report` first if you are "
            "choosing between them.",
            strategy=strategy, available=list(STRATEGIES),
        )
    tables = _tables(conn)
    source_path = _db_path(conn)
    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    decisions: list[dict[str, Any]] = []
    warnings: list[str] = []
    notes: list[str] = []

    # ---- which tables survive
    keep_tables: list[str] = []
    for table in sorted(tables):
        if table in ESSENTIAL_TABLES:
            keep_tables.append(table)
            continue
        plan = OPTIONAL_TABLES.get(table)
        if plan is None:
            # An unrecognised table is kept everywhere except `minimal`, and
            # flagged. Silently dropping something nobody has reasoned about is
            # exactly the failure this list exists to prevent.
            plan = {"keep_at": {"balanced", "full"},
                    "why": "Not in the pruning plan. No reasoning is recorded for this "
                           "table, so it is kept except at the most aggressive strategy.",
                    "costs": "Unknown. Add it to OPTIONAL_TABLES before relying on this.",
                    "visible": True, "review": True}
        if strategy in plan["keep_at"]:
            keep_tables.append(table)
            continue
        rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        stats = _column_stats(conn, table)
        decisions.append(_decision(
            "drop_table", table,
            f"Dropped whole table: {rows:,} rows, "
            f"{human_bytes(sum(s['text_bytes'] for s in stats.values()))} of text.",
            plan["why"], visible=bool(plan["visible"]), costs=plan["costs"],
            bytes_saved=sum(s["text_bytes"] for s in stats.values()) + rows,
            review=bool(plan.get("review")),
        ))
    for table in sorted(set(ESSENTIAL_TABLES) & tables):
        decisions.append(_decision(
            "keep_table", table, "Kept at every strategy, including `minimal`.",
            ESSENTIAL_TABLES[table], visible=False,
            costs="Nothing was dropped. Dropping it would be a wrong answer, not a "
                  "smaller file.",
        ))
    if "frequencies" in tables:
        warnings.append(
            "frequencies.txt is present in this feed. Departures cannot be computed "
            "from stop_times alone when it is, so the app needs a second code path -- "
            "see the `no_frequencies_file` assumption (spec 6.10)."
        )
    for missing, why in sorted(ESSENTIAL_TABLES.items()):
        if missing not in tables:
            warnings.append(f"{missing}.txt is absent from this snapshot. {why}")

    # ---- which rows survive
    kept_services: set[str] | None = None
    kept_trips: set[str] | None = None
    window: dict[str, Any] | None = None
    if days is not None:
        if days < 1:
            raise UsageError(
                f"--days must be at least 1, got {days}.",
                remedy="Omit --days to keep the feed's whole service window.", days=days)
        start, end = _service_window(conn)
        if start is None:
            warnings.append("--days ignored: this feed declares no service dates to "
                            "measure a window from.")
        else:
            last = min(end, start + timedelta(days=days - 1)) if end else \
                start + timedelta(days=days - 1)
            kept_services = set()
            cursor = start
            while cursor <= last:
                kept_services |= set(active_services(conn, cursor)["active"])
                cursor += timedelta(days=1)
            window = {"from": start.isoformat(), "to": last.isoformat(), "days": days,
                      "feed_ends": end.isoformat() if end else None}
            total_services = conn.execute(
                "SELECT COUNT(DISTINCT service_id) FROM trips").fetchone()[0] \
                if "trips" in tables else 0
            decisions.append(_decision(
                "prune_rows", "calendar/calendar_dates/trips/stop_times",
                f"Kept only service active in {start.isoformat()}..{last.isoformat()} "
                f"({len(kept_services)} of {total_services} service_ids).",
                "The window is anchored on the feed's first service date, not on today, "
                "so the result does not depend on when it was run (spec 2.8).",
                visible=True,
                costs=f"After {last.isoformat()} the app has NO data. It must detect the "
                      "boundary and say the schedule has run out -- rendering an empty "
                      "board there is indistinguishable from 'no more buses tonight'.",
            ))
            warnings.append(
                f"--days {days} truncates coverage at {last.isoformat()}. The app must "
                "refuse to answer past that date rather than show an empty board."
            )
    if routes:
        available = {r[0] for r in conn.execute("SELECT route_id FROM routes")} \
            if "routes" in tables else set()
        unknown = sorted(set(routes) - available)
        if unknown:
            raise UsageError(
                f"No such route_id: {', '.join(unknown)}.",
                remedy="List them with `stl gtfs routes`. route_id is Metro's internal "
                "id (e.g. '19731B'), not the number on the front of the bus.",
                unknown=unknown, available=sorted(available)[:20],
            )
    if routes or kept_services is not None:
        clauses, params = [], []
        if routes:
            clauses.append(f"route_id IN ({', '.join('?' * len(routes))})")
            params += list(routes)
        if kept_services is not None:
            clauses.append(f"service_id IN ({', '.join('?' * len(kept_services))})")
            params += sorted(kept_services)
        kept_trips = {r[0] for r in conn.execute(
            f"SELECT trip_id FROM trips WHERE {' AND '.join(clauses)}", params)}
        if routes:
            decisions.append(_decision(
                "prune_rows", "routes/trips/stop_times/stops",
                f"Kept only route_id in {sorted(routes)}: {len(kept_trips):,} trips.",
                "An explicit route filter, requested by the caller.",
                visible=True,
                costs="Every stop served only by an excluded route disappears from the "
                      "index. A rider typing its number is told the stop does not exist, "
                      "for a stop that does.",
            ))

    # ---- copy
    dst = sqlite3.connect(out_path)
    table_rows: list[dict[str, Any]] = []
    kept_stop_ids: set[str] = set()
    try:
        dst.execute("PRAGMA journal_mode=OFF")
        dst.execute("PRAGMA synchronous=OFF")
        ordered = [t for t in COPY_ORDER if t in keep_tables]
        ordered += [t for t in sorted(keep_tables) if t not in set(ordered)]
        for table in ordered:
            rows_before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            stats = _column_stats(conn, table)
            columns, col_decisions = _plan_columns(conn, table, strategy, stats, rows_before)
            decisions += col_decisions
            if not columns:  # a table whose every column was empty
                columns = _columns(conn, table)
            keep_when = None
            collect = None
            # Each filter is conditional on the column existing: a feed missing
            # the key it would filter on is a feed that gets copied whole, not
            # one that raises here.
            source_cols = set(_columns(conn, table))
            if table in ("trips", "stop_times") and kept_trips is not None \
                    and "trip_id" in source_cols:
                keep_when = ("trip_id", kept_trips)
            elif table in ("calendar", "calendar_dates") and kept_services is not None \
                    and "service_id" in source_cols:
                keep_when = ("service_id", kept_services)
            elif table == "routes" and routes and "route_id" in source_cols:
                keep_when = ("route_id", set(routes))
            elif table == "stops" and kept_trips is not None and kept_stop_ids \
                    and "stop_id" in source_cols:
                keep_when = ("stop_id", kept_stop_ids)
            if table == "stop_times" and kept_trips is not None and "stop_id" in source_cols:
                collect = "stop_id"
            written, collected = _copy_table(conn, dst, table, columns, keep_when, collect)
            if collected:
                kept_stop_ids |= collected
                # Station parents are never referenced by a stop_time, so
                # collecting only the called stops would orphan every platform's
                # parent and break station resolution.
                #
                # Done by scanning the 5k-row stops table rather than with an
                # `IN (...)` over the collected ids: that list can run to
                # thousands of host parameters, and SQLite's limit on those is a
                # compile-time setting that differs between builds.
                if "parent_station" in set(_columns(conn, "stops")):
                    kept_stop_ids |= {
                        parent for stop_id, parent in conn.execute(
                            "SELECT stop_id, parent_station FROM stops "
                            "WHERE parent_station <> ''")
                        if stop_id in collected
                    }
            table_rows.append({
                "table": table,
                "rows_before": rows_before,
                "rows_after": written,
                "rows_removed": rows_before - written,
                "columns_before": len(_columns(conn, table)),
                "columns_after": len(columns),
                "columns_kept": columns,
                "columns_dropped": [c for c in _columns(conn, table) if c not in set(columns)],
            })

        # Indices: the same hot paths the dev database indexes, minus any whose
        # table or column did not survive. An index the app never uses is pure
        # bytes on a device with none to spare.
        built: list[dict[str, str]] = []
        existing = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, name, cols in INDICES:
            if table not in existing:
                continue
            have = {r[1] for r in dst.execute(f'PRAGMA table_info("{table}")')}
            wanted = [c.strip() for c in cols.split(",")]
            if not set(wanted) <= have:
                continue
            if strategy == "minimal" and name not in DEPARTURES_INDICES:
                rationale = INDEX_RATIONALE.get(name, {})
                decisions.append(_decision(
                    "drop_index", name,
                    f"Index on {table}({cols}) not rebuilt at strategy 'minimal'.",
                    rationale.get("why", "Not on the departures path."),
                    visible=False,
                    costs=rationale.get("costs", "Queries in that shape become scans."),
                ))
                continue
            dst.execute(f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" ({cols})')
            built.append({"name": name, "table": table, "columns": cols,
                          "why": DEPARTURES_INDICES.get(
                              name, "Kept at this strategy; not on the departures path.")})
        dst.commit()
        before_vacuum = out_path.stat().st_size
        # VACUUM rewrites the file with no free pages and the rows in insertion
        # order, which is why the copy is ordered by the app's access pattern.
        dst.execute("VACUUM")
        dst.commit()
    finally:
        dst.close()

    after = out_path.stat().st_size
    decisions.append(_decision(
        "vacuum", out_path.name,
        f"VACUUM reclaimed {human_bytes(before_vacuum - after)} "
        f"({human_bytes(before_vacuum)} -> {human_bytes(after)}).",
        "Deleting rows and columns leaves free pages behind; without VACUUM the file "
        "keeps the space it no longer uses, so the pruning shows up in the row counts "
        "and not on the device.",
        visible=False, costs="None.", bytes_saved=before_vacuum - after,
    ))
    decisions.append(_decision(
        "order", "stop_times",
        "Rows written in (stop_id, departure_time) order.",
        "The app has exactly one hot query -- departures at one stop -- so clustering "
        "those rows together means one lookup touches a handful of adjacent pages "
        "instead of scattering across the file. The trailing sort terms make ties "
        "deterministic so two builds produce the same bytes (spec 2.8).",
        visible=False, costs="None.",
    ))

    source_bytes = source_path.stat().st_size if source_path and source_path.exists() else None
    if source_bytes is None:
        warnings.append("Source database size unknown (in-memory connection); the "
                        "before/after ratio is omitted.")
    packed = compressed_bytes(out_path)
    index_bytes = _index_bytes(out_path)
    decisions.sort(key=lambda d: (d["action"], d["target"]))

    notes.append(
        f"Compressed, this file is {human_bytes(packed)} -- that is what it costs in an "
        "APK asset or a git object, and it is the number spec 12 item 2 turns on."
    )
    return {
        "ok": True,
        "provenance": None,
        "warnings": warnings,
        "notes": notes,
        "strategy": strategy,
        "strategy_drops": _strategy_summary(strategy),
        "out_path": str(out_path),
        "filters": {"routes": sorted(routes) if routes else None, "days": days,
                    "window": window},
        "source": {
            "path": str(source_path) if source_path else None,
            "bytes": source_bytes,
            "human": human_bytes(source_bytes),
        },
        "compact": {
            "bytes": after,
            "human": human_bytes(after),
            "before_vacuum_bytes": before_vacuum,
            "reclaimed_by_vacuum_bytes": before_vacuum - after,
            "reclaimed_by_vacuum_human": human_bytes(before_vacuum - after),
            "index_bytes": index_bytes,
            "index_human": human_bytes(index_bytes),
            "row_data_bytes": (after - index_bytes) if index_bytes is not None else None,
            "compressed_bytes": packed,
            "compressed_human": human_bytes(packed),
            "compression_ratio": round(packed / after, 4) if after else None,
        },
        "ratio": round(after / source_bytes, 4) if source_bytes else None,
        "reduction_pct": round(100 * (1 - after / source_bytes), 1) if source_bytes else None,
        "saved_bytes": (source_bytes - after) if source_bytes else None,
        "saved_human": human_bytes(source_bytes - after) if source_bytes else "n/a",
        "tables": table_rows,
        "indices": built,
        "decisions": decisions,
        "decision_count": len(decisions),
        "visible_decisions": [d for d in decisions if d["visible_to_user"]],
        "artifact": {
            "name": out_path.name,
            "kind": "compact_db",
            "path": str(out_path),
            "bytes": after,
            "sha256": sha256_file(out_path),
            "rows": sum(t["rows_after"] for t in table_rows),
        },
    }


def _strategy_summary(strategy: str) -> dict[str, Any]:
    """What this strategy drops, in one paragraph, for the CLI and the README."""
    return {
        "minimal": {
            "drops": "shapes, transfers, feed_info, attributions, every fare table; "
                     "arrival_time / drop_off_type / timepoint / shape_dist_traveled "
                     "from stop_times; coordinates, description and wheelchair_boarding "
                     "from stops; block_id, shape_id and wheelchair_accessible from "
                     "trips; route colours; and the two indices no departures query "
                     "uses (idx_st_trip, idx_trips_route).",
            "costs": "Accessibility information disappears: the app cannot tell a "
                     "wheelchair user whether a stop or a vehicle is accessible. Ship "
                     "this only as a deliberate choice. A trip-detail view would also "
                     "scan stop_times rather than seek into it.",
        },
        "balanced": {
            "drops": "shapes, transfers and the fare tables; timepoint and "
                     "shape_dist_traveled from stop_times; coordinates and "
                     "descriptions from stops; block_id and shape_id from trips; "
                     "route colours. Keeps arrival_time, the accessibility flags and "
                     "every index.",
            "costs": "No map or trip planning later without re-importing. Accessibility "
                     "flags, arrival times, feed_info and every index survive -- on "
                     "Metro's feed the indices alone are 19.7 MiB of the result, which "
                     "is the price of leaving room for features that do not exist yet.",
        },
        "full": {
            "drops": "Nothing. Every table and column survives.",
            "costs": "None. It is the control the other two are measured against, and "
                     "still smaller than the imported feed because it is VACUUMed and "
                     "written in the app's access order.",
        },
    }[strategy]


# --------------------------------------------------------------- size report --

def size_report(conn: sqlite3.Connection, compact_path: Path | None = None) -> dict[str, Any]:
    """Size budget by table, with index bytes isolated from row data.

    Three numbers get conflated whenever anyone quotes a database size: the
    text in the rows, the indices over it, and what the file compresses to.
    They answer different questions -- what to prune, what to index, and what
    it costs to ship or commit -- so this reports them apart.
    """
    tables = _tables(conn)
    source_path = _db_path(conn)
    rows_out: list[dict[str, Any]] = []
    for table in sorted(tables):
        rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        stats = _column_stats(conn, table)
        text_bytes = sum(s["text_bytes"] for s in stats.values())
        rows_out.append({
            "table": table,
            "rows": rows,
            "columns": len(stats),
            "text_bytes": text_bytes,
            "text_human": human_bytes(text_bytes),
            "bytes_per_row": round(text_bytes / rows, 1) if rows else 0.0,
            "column_detail": sorted(
                ({"column": col, "non_empty": s["non_empty"], "text_bytes": s["text_bytes"],
                  "text_human": human_bytes(s["text_bytes"]),
                  "empty_on_every_row": s["non_empty"] == 0 and rows > 0}
                 for col, s in stats.items()),
                key=lambda c: (-c["text_bytes"], c["column"]),
            ),
        })
    # Biggest first: the reader is looking for what to prune, and on this feed
    # the answer is always stop_times.
    rows_out.sort(key=lambda t: (-t["text_bytes"], t["table"]))
    total_text = sum(t["text_bytes"] for t in rows_out)

    def file_block(path: Path | None) -> dict[str, Any]:
        if path is None or not Path(path).exists():
            return {"path": str(path) if path else None, "bytes": None, "human": "n/a"}
        path = Path(path)
        size = path.stat().st_size
        idx = _index_bytes(path)
        packed = compressed_bytes(path)
        return {
            "path": str(path),
            "bytes": size,
            "human": human_bytes(size),
            "index_bytes": idx,
            "index_human": human_bytes(idx),
            "index_share": round(idx / size, 4) if idx is not None and size else None,
            "row_data_bytes": (size - idx) if idx is not None else None,
            "row_data_human": human_bytes(size - idx) if idx is not None else "n/a",
            "compressed_bytes": packed,
            "compressed_human": human_bytes(packed),
            # Isolated deliberately: "how big is it" and "how big is it in the
            # APK" differ by this factor, and quoting the wrong one either
            # overstates the shipping cost or understates the on-device cost.
            "compression_ratio": round(packed / size, 4) if size else None,
            "compression_saves_bytes": size - packed,
            "compression_saves_human": human_bytes(size - packed),
        }

    source = file_block(source_path)
    out: dict[str, Any] = {
        "ok": True,
        "provenance": None,
        "warnings": [],
        "notes": [
            "text_bytes is the payload in the rows: the sum of every value's length. "
            "The file is always larger -- SQLite adds a per-row header, per-page "
            "overhead and the indices, which are reported separately.",
            "Index bytes are measured by copying the file, dropping the indices and "
            "VACUUMing. dbstat would answer directly but is not compiled into stock "
            "Python.",
        ],
        "source": source,
        "tables": rows_out,
        "total_text_bytes": total_text,
        "total_text_human": human_bytes(total_text),
        "total_rows": sum(t["rows"] for t in rows_out),
        "biggest_table": rows_out[0]["table"] if rows_out else None,
    }
    if source.get("bytes"):
        out["storage_overhead_bytes"] = source["bytes"] - total_text
        out["storage_overhead_note"] = (
            f"{human_bytes(source['bytes'] - total_text)} of the file is SQLite "
            "structure and indices rather than feed text."
        )
    if compact_path is not None:
        compact_block = file_block(Path(compact_path))
        out["compact"] = compact_block
        if source.get("bytes") and compact_block.get("bytes"):
            out["comparison"] = {
                "ratio": round(compact_block["bytes"] / source["bytes"], 4),
                "reduction_pct": round(100 * (1 - compact_block["bytes"] / source["bytes"]), 1),
                "saved_bytes": source["bytes"] - compact_block["bytes"],
                "saved_human": human_bytes(source["bytes"] - compact_block["bytes"]),
                "compressed_ratio": round(
                    compact_block["compressed_bytes"] / source["bytes"], 4),
            }
    return out


# ------------------------------------------------------------------ manifest --

def manifest(artifacts: list[dict[str, Any]], snapshot_id: str,
             feed_sha256: str) -> dict[str, Any]:
    """What was generated, from which snapshot, with hashes.

    This goes in the tool's README and in its vetting defense. Light's
    reviewers get a claim -- "the bundled schedule data comes from Metro's
    published feed" -- and this is the evidence for it: a dated, hashed source
    and a hash for every file derived from it, so anyone can re-run the
    generator and compare.

    Accepts either an `artifact` block from the functions above or a bare dict
    with name/path/sha256/bytes. Where a `path` exists on disk, the file itself
    is hashed: the manifest must describe what shipped, not what was intended.

    Deliberately carries no generation timestamp. `snapshot_id` already encodes
    the fetch time to the second, and a wall-clock field would make two runs of
    the same build differ (spec 2.8).
    """
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    for index, entry in enumerate(artifacts or []):
        rec = dict((entry or {}).get("artifact") or entry or {})
        path = rec.get("path") or rec.get("out_path")
        name = rec.get("name") or (Path(path).name if path else "") or f"artifact_{index}"
        sha = rec.get("sha256") or rec.get("content_sha256") or ""
        size = rec.get("bytes")
        if path and Path(path).is_file():
            sha = sha256_file(Path(path))
            size = Path(path).stat().st_size
        if not sha:
            warnings.append(
                f"Artifact {name!r} carries no hash, so it cannot be traced back to "
                f"snapshot {snapshot_id}. Generate it through this module, or hash the "
                "file before adding it."
            )
        record = {
            "name": name,
            "kind": rec.get("kind", "unknown"),
            "sha256": sha,
            "bytes": size,
            "human_bytes": human_bytes(size),
        }
        for optional in ("rows", "as_of", "source_url", "path"):
            if rec.get(optional) is not None:
                record[optional] = rec[optional]
        records.append(record)
    records.sort(key=lambda a: (a["name"], a["sha256"]))

    document = {
        "generated_by": f"stl {__version__}",
        "snapshot_id": snapshot_id,
        "feed_sha256": feed_sha256,
        "artifacts": records,
        "artifact_count": len(records),
        "note": "Every artifact here was generated from the snapshot named above. "
                "Re-running the generator against the same snapshot reproduces these "
                "hashes exactly; a mismatch means either the feed or the generator "
                "moved, and both are worth knowing about.",
    }
    rendered = canonical_json(document)
    total = sum(a["bytes"] or 0 for a in records)
    return {
        "ok": True,
        "provenance": None,
        "warnings": warnings,
        "notes": [],
        **document,
        "count": len(records),
        "total": len(records),
        "total_bytes": total,
        "total_human": human_bytes(total),
        # The hash OF the manifest, not IN it -- a document cannot contain its
        # own digest.
        "manifest_sha256": sha256_text(rendered),
        "rendered": rendered,
        "artifact": {
            "name": "manifest.json",
            "kind": "manifest",
            "rows": len(records),
            "bytes": len(rendered.encode("utf-8")),
            "sha256": sha256_text(rendered),
        },
    }
