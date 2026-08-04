"""Result envelope shared by the CLI and the MCP server.

Rule (spec 2.3): a result without provenance is a rumour. Every response says
which snapshot it came from, when that snapshot was fetched, and whether the
feed it describes has expired.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ResponseFormat = Literal["json", "markdown"]

# Hard ceilings. An MCP client is a context window; these are not negotiable
# by the caller (spec 2.4).
MAX_LIST_ITEMS = 500
MAX_QUERY_ROWS = 1_000
MAX_QUERY_BYTES = 256 * 1024


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(description="Content-addressed snapshot identifier.")
    source: str = Field(description="Source name from sources.toml, e.g. 'metro_gtfs'.")
    source_url: str = ""
    fetched_at: datetime | None = None
    sha256: str = ""
    feed_start_date: date | None = None
    feed_end_date: date | None = None
    stale_days: int | None = Field(
        default=None,
        description="Days past feed_end_date. Negative means still valid. "
        "Positive means the feed has expired and departures may be empty.",
    )

    @property
    def expired(self) -> bool:
        return self.stale_days is not None and self.stale_days > 0


class Result(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool = True
    provenance: Provenance | list[Provenance] | None = None
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ListResult(Result):
    total: int = 0
    count: int = 0
    offset: int = 0
    has_more: bool = False
    next_offset: int | None = None
    truncated: bool = False
    items: list[Any] = Field(default_factory=list)


def paginate(
    rows: list[Any],
    offset: int = 0,
    limit: int = 50,
    hard_cap: int = MAX_LIST_ITEMS,
) -> tuple[list[Any], dict[str, Any]]:
    """Slice `rows` and build the pagination metadata block.

    Applies the hard cap regardless of what the caller asked for, and reports
    the clipping in the metadata rather than silently truncating.
    """
    total = len(rows)
    offset = max(0, offset)
    effective = min(max(1, limit), hard_cap)
    window = rows[offset : offset + effective]
    has_more = offset + len(window) < total
    # `truncated` reports what actually happened to the DATA, not what the
    # caller asked for. Reporting on the request meant `limit=1000` over 62
    # routes returned all 62 rows flagged truncated, training a reader to
    # ignore the flag exactly where it matters.
    return window, {
        "total": total,
        "count": len(window),
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(window) if has_more else None,
        "truncated": has_more,
        "limit_clamped_to": effective if limit > hard_cap else None,
    }
