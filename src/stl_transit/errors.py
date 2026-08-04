"""Structured errors.

Every error carries a `remedy`. In MCP that is the difference between the model
recovering on its own and the model giving up.
"""

from __future__ import annotations

from typing import Any


class StlError(Exception):
    code = "STL_ERROR"
    exit_code = 1

    def __init__(self, message: str, remedy: str = "", **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "remedy": self.remedy,
                "context": self.context,
            },
        }


class UsageError(StlError):
    code = "USAGE"
    exit_code = 2


class SnapshotNotFound(StlError):
    code = "SNAPSHOT_NOT_FOUND"


class SourceNotFound(StlError):
    code = "SOURCE_NOT_FOUND"


class NetworkUnavailable(StlError):
    code = "NETWORK_UNAVAILABLE"
    exit_code = 5


class FeedStale(StlError):
    code = "FEED_STALE"
    exit_code = 6


class AssertionViolated(StlError):
    code = "ASSERTION_VIOLATED"
    exit_code = 3


class DriftDetected(StlError):
    code = "DRIFT_DETECTED"
    exit_code = 4


class UnsafeQuery(StlError):
    """The statement was refused on safety grounds -- a write, ATTACH, PRAGMA,
    extension load, or multiple statements in one call."""

    code = "UNSAFE_QUERY"
    exit_code = 2


class QueryTimeout(StlError):
    """A permitted query ran past its time budget. Distinct from UNSAFE_QUERY
    because the remedy is 'narrow the query', not 'stop trying to write'."""

    code = "QUERY_TIMEOUT"
    exit_code = 1


class QueryFailed(StlError):
    """Ordinary SQL error -- bad column, syntax, type. Distinct from
    UNSAFE_QUERY so a typo does not read as an attempted attack."""

    code = "QUERY_FAILED"
    exit_code = 2


class StopNotFound(StlError):
    code = "STOP_NOT_FOUND"


class PageNotFound(StlError):
    code = "PAGE_NOT_FOUND"


class ExtractionFailed(StlError):
    """A page was fetched but its expected structure was not found -- usually
    means Metro redesigned the page and the extractor needs updating."""

    code = "EXTRACTION_FAILED"
    exit_code = 4
