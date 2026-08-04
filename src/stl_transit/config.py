"""Configuration: source registry + on-disk store location."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import SourceNotFound

_PKG_SOURCES = Path(__file__).parent / "data" / "sources.toml"


def store_root() -> Path:
    """Root of the snapshot store. Override with $STL_HOME."""
    env = os.environ.get("STL_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base).expanduser() / "stl-transit"


@dataclass(frozen=True)
class Source:
    name: str
    kind: str  # "gtfs" | "gtfs-rt"
    url: str
    agency: str = ""
    timezone: str = "America/Chicago"
    terms_url: str = ""
    entity: str = ""
    static_source: str = ""
    region: str = ""
    unresolved: bool = False
    seasonal: bool = False
    discovery_notes: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.url) and not self.unresolved


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str = "stl-transit-dev/0.1"
    min_interval_seconds: float = 2.0
    timeout_seconds: float = 60.0
    max_retries: int = 3


@dataclass(frozen=True)
class Config:
    http: HttpConfig
    feeds: dict[str, Source]
    pages: dict[str, dict[str, Any]]
    mirrors: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path = _PKG_SOURCES

    def source(self, name: str) -> Source:
        try:
            return self.feeds[name]
        except KeyError:
            raise SourceNotFound(
                f"No source named {name!r}.",
                remedy="Run `stl snapshot sources` to list configured sources.",
                available=sorted(self.feeds),
            ) from None


@lru_cache(maxsize=4)
def load_config(path: Path | None = None) -> Config:
    """Load sources.toml. $STL_SOURCES overrides the packaged copy."""
    if path is None:
        env = os.environ.get("STL_SOURCES")
        path = Path(env) if env else _PKG_SOURCES
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    http = HttpConfig(**raw.get("http", {}))
    feeds = {
        name: Source(name=name, **body) for name, body in raw.get("feeds", {}).items()
    }
    return Config(
        http=http,
        feeds=feeds,
        pages=raw.get("pages", {}),
        mirrors=raw.get("mirrors", {}),
        path=path,
    )
