"""GTFS zip -> SQLite, plus the read-only connection that is the security
boundary for model-authored SQL (spec 9).

Writes are impossible at the driver level (mode=ro), not merely discouraged.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator

from ..errors import EmptyFeed, QueryFailed, QueryTimeout, UnsafeQuery

# Files we index. Anything else in the zip is still imported generically.
KNOWN_FILES = [
    "agency", "stops", "routes", "trips", "stop_times", "calendar",
    "calendar_dates", "shapes", "frequencies", "transfers", "feed_info",
    "fare_attributes", "fare_rules", "pathways", "levels", "translations",
    "attributions",
]

# Indices on the hot paths only. stop_times is the expensive table.
INDICES = [
    ("stop_times", "idx_st_stop", "stop_id, departure_time"),
    ("stop_times", "idx_st_trip", "trip_id, stop_sequence"),
    ("trips", "idx_trips_service", "service_id"),
    ("trips", "idx_trips_route", "route_id"),
    ("stops", "idx_stops_code", "stop_code"),
    ("stops", "idx_stops_parent", "parent_station"),
    ("calendar_dates", "idx_cd_date", "date, service_id"),
]


def zip_members(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(n for n in zf.namelist() if not n.endswith("/"))


def read_csv(zip_path: Path, member: str) -> Iterator[dict[str, str]]:
    """Stream one GTFS CSV. Handles the UTF-8 BOM Metro-style feeds often carry."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            yield from csv.DictReader(text)


def _table_name(member: str) -> str:
    return Path(member).stem.lower()


def build_sqlite(zip_path: Path, db_path: Path, force: bool = False) -> dict[str, Any]:
    """Import every CSV in the zip into SQLite. All columns TEXT.

    TEXT everywhere is deliberate: GTFS ids are opaque strings, leading zeros
    are meaningful, and coercing them loses information the Kotlin side will
    also have to preserve.
    """
    if db_path.exists() and not force:
        return {"db_path": str(db_path), "rebuilt": False, "tables": _table_counts(db_path)}
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Build into a temporary file and rename only on success. `sqlite3.connect`
    # CREATES the file the instant it is called -- before a single row is read
    # -- so building in place left an empty database behind whenever the import
    # failed: a corrupt zip, an interrupted run, a snapshot of the wrong kind.
    # Every later call then took the `db_path.exists()` fast path above and
    # served that empty file as a complete feed. Nothing errored. `diff` duly
    # reported that all 5,118 stops had disappeared.
    #
    # rename() is atomic within a filesystem, so a reader either sees the old
    # database or the finished new one, never a half-built one.
    tmp_path = db_path.with_name(db_path.name + f".building-{os.getpid()}")
    tmp_path.unlink(missing_ok=True)

    counts: dict[str, int] = {}
    conn = sqlite3.connect(tmp_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    try:
        for member in zip_members(zip_path):
            if not member.lower().endswith(".txt"):
                continue
            table = _table_name(member)
            rows = read_csv(zip_path, member)
            try:
                first = next(rows)
            except StopIteration:
                conn.execute(f'CREATE TABLE "{table}" (empty TEXT)')
                counts[table] = 0
                continue
            cols = list(first.keys())
            coldefs = ", ".join(f'"{c}" TEXT' for c in cols)
            conn.execute(f'CREATE TABLE "{table}" ({coldefs})')
            placeholders = ", ".join("?" * len(cols))
            stmt = f'INSERT INTO "{table}" VALUES ({placeholders})'

            def batched(first_row: dict[str, str]) -> Iterator[tuple]:
                yield tuple((first_row.get(c) or "") for c in cols)
                for r in rows:
                    yield tuple((r.get(c) or "") for c in cols)

            n = 0
            batch: list[tuple] = []
            for rec in batched(first):
                batch.append(rec)
                if len(batch) >= 20_000:
                    conn.executemany(stmt, batch)
                    n += len(batch)
                    batch.clear()
            if batch:
                conn.executemany(stmt, batch)
                n += len(batch)
            counts[table] = n

        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, name, cols in INDICES:
            if table in existing:
                try:
                    conn.execute(f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" ({cols})')
                except sqlite3.OperationalError:
                    pass  # column absent in this feed; not fatal
        conn.commit()
    except BaseException:
        # Includes KeyboardInterrupt: a Ctrl-C mid-import must not leave a
        # plausible-looking empty database where a real one is expected.
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    if not counts:
        # An archive with no .txt members is not a GTFS feed. Publishing an
        # empty database here is what made a broken import indistinguishable
        # from an agency that deleted every stop.
        tmp_path.unlink(missing_ok=True)
        raise EmptyFeed(
            f"No GTFS .txt files found in {zip_path.name}; refusing to build an "
            "empty database.",
            remedy="Check that this snapshot is a GTFS zip and not a realtime "
            "protobuf or an HTML error page saved with a .zip name. "
            "`stl snapshot show <id>` reports the stored content type.",
            zip_path=str(zip_path),
        )
    tmp_path.replace(db_path)
    return {"db_path": str(db_path), "rebuilt": True, "tables": counts}


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = connect_ro(db_path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


# --------------------------------------------------------------- read-only --

_DENIED_ACTIONS = {
    sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_DROP_INDEX,
}


def _authorizer(action: int, arg1, arg2, dbname, source) -> int:
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect_ro(db_path: Path, timeout_seconds: float = 15.0, guard: bool = False) -> sqlite3.Connection:
    """Open a read-only connection.

    `guard=True` additionally installs an authorizer and a genuine wall-clock
    timeout. Use it for any SQL that did not originate in this codebase -- i.e.
    every call that arrives through `stl_gtfs_query`.

    The timeout consults the clock. An earlier version counted progress-handler
    invocations and called the count seconds, which aborted a 0.34 s GROUP BY
    over stop_times -- and stop_times is 489k rows, so most legitimate
    aggregates over the largest table in the feed failed.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if guard:
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + timeout_seconds

        def progress() -> int:
            return 1 if time.monotonic() > deadline else 0

        # Every 10k VM instructions: frequent enough to honour the deadline
        # closely, rare enough not to dominate the query's own cost.
        conn.set_progress_handler(progress, 10_000)
    return conn


def _is_multi_statement(sql: str) -> bool:
    """True if `sql` contains a statement separator outside a string or comment.

    A naive `';' in sql` refuses `SELECT ';' AS semi` and any query carrying a
    trailing comment -- both valid, both read-only. Since the authorizer and
    mode=ro already make writes impossible, over-rejecting here buys no safety
    and costs the model working queries.
    """
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"', "`"):
            quote, i = ch, i + 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # doubled = escaped
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "[":  # sqlite bracket-quoted identifier
            while i < n and sql[i] != "]":
                i += 1
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 1
        elif ch == ";":
            return True
        i += 1
    return False


def run_query(
    db_path: Path,
    sql: str,
    max_rows: int,
    max_bytes: int,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Execute one read-only statement with row, size and time caps.

    Failure modes get distinct error codes. Reporting a timeout or a typo as
    UNSAFE_QUERY tells the model it wrote something dangerous when it wrote
    something slow or misspelled, and it then rewrites the wrong thing.
    """
    stripped = sql.strip().rstrip().rstrip(";")
    if _is_multi_statement(stripped):
        raise UnsafeQuery(
            "Only a single statement is permitted.",
            remedy="Send one SELECT at a time; remove the ';' separators.",
            sql=stripped[:400],
        )
    conn = connect_ro(db_path, timeout_seconds=timeout_seconds, guard=True)
    started = time.monotonic()
    try:
        try:
            cur = conn.execute(stripped)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows, size, truncated = [], 0, False
            for rec in cur:
                row = {c: rec[c] for c in cols}
                size += sum(len(str(v)) for v in row.values()) + 16 * len(cols)
                if len(rows) >= max_rows or size > max_bytes:
                    truncated = True
                    break
                rows.append(row)
        except sqlite3.DatabaseError as exc:
            raise _classify(exc, stripped, time.monotonic() - started, timeout_seconds) from None
        return {
            "columns": cols,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_seconds": round(time.monotonic() - started, 4),
        }
    finally:
        conn.close()


def _classify(exc: sqlite3.DatabaseError, sql: str, elapsed: float, budget: float):
    """Map a sqlite error onto the narrowest error we can justify."""
    text = str(exc)
    lowered = text.lower()
    if "interrupted" in lowered:
        return QueryTimeout(
            f"Query exceeded the {budget:g}s budget (ran {elapsed:.2f}s) and was cancelled.",
            remedy="Narrow it: add a WHERE clause, aggregate with GROUP BY instead of "
            "returning rows, or filter on an indexed column (stop_times is indexed on "
            "stop_id+departure_time and trip_id+stop_sequence).",
            sql=sql[:400],
            elapsed_seconds=round(elapsed, 3),
        )
    if "not authorized" in lowered:
        return UnsafeQuery(
            f"Statement denied by the read-only authorizer: {text}",
            remedy="This database is opened read-only. Writes, ATTACH, PRAGMA and "
            "extension loading are refused at the driver. Rewrite as a SELECT or WITH.",
            sql=sql[:400],
        )
    # VACUUM, REINDEX and ANALYZE are refused by mode=ro rather than by the
    # authorizer, so they arrive as "readonly database" or "attempt to write".
    # Without this they fell through to QUERY_FAILED and were answered with
    # advice about checking column names -- true of a typo, useless here.
    if "readonly database" in lowered or "attempt to write" in lowered:
        return UnsafeQuery(
            f"Statement requires write access: {text}",
            remedy="The feed database is opened read-only and is a regenerable "
            "cache -- rebuild it with `stl gtfs import --force` rather than "
            "trying to modify it in place.",
            sql=sql[:400],
        )
    if "out of memory" in lowered or "too big" in lowered or "string or blob" in lowered:
        return QueryFailed(
            f"Query exceeded a size limit: {text}",
            remedy="Something in this query allocates more than SQLite will hold "
            "(a very large randomblob/zeroblob, or an unbounded recursive CTE). "
            "Bound it, or aggregate instead of materialising rows.",
            sql=sql[:400],
        )
    return QueryFailed(
        f"Query failed: {text}",
        remedy="Check table and column names with `stl gtfs schema <file>` or "
        "`stl gtfs files`. Table names are GTFS filenames without .txt, and every "
        "column is TEXT.",
        sql=sql[:400],
    )
