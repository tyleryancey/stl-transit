"""Polite HTTP client (spec 9).

You are an unpaid guest on a public agency's infrastructure and this
relationship has to survive past submission. Identifying UA, conditional
requests, minimum interval, exponential backoff.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import HttpConfig
from ..errors import NetworkUnavailable

_last_request: dict[str, float] = {}


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    headers: dict[str, str]
    not_modified: bool = False
    elapsed_seconds: float = 0.0

    @property
    def etag(self) -> str:
        return self.headers.get("etag", "")

    @property
    def last_modified(self) -> str:
        return self.headers.get("last-modified", "")


def _throttle(url: str, min_interval: float) -> None:
    host = urlparse(url).netloc
    last = _last_request.get(host)
    if last is not None:
        wait = min_interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request[host] = time.monotonic()


def _cache_path(cache_dir: Path, url: str) -> Path:
    import hashlib

    return cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".json")


def fetch(
    url: str,
    cfg: HttpConfig,
    cache_dir: Path | None = None,
    conditional: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """GET `url` with conditional-request support and backoff.

    Returns `not_modified=True` and empty content on a 304 so callers can skip
    re-storing an unchanged feed. An unchanged feed should cost a 304.
    """
    headers = {"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip, deflate"}
    headers.update(extra_headers or {})

    cache_file = _cache_path(cache_dir, url) if cache_dir else None
    if conditional and cache_file and cache_file.is_file():
        prior = json.loads(cache_file.read_text())
        if prior.get("etag"):
            headers["If-None-Match"] = prior["etag"]
        if prior.get("last_modified"):
            headers["If-Modified-Since"] = prior["last_modified"]

    _throttle(url, cfg.min_interval_seconds)
    delay, last_exc = 1.0, None
    for attempt in range(cfg.max_retries):
        started = time.monotonic()
        try:
            with httpx.Client(timeout=cfg.timeout_seconds, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
            elapsed = time.monotonic() - started
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code == 304:
                return FetchResult(url, 304, b"", hdrs, not_modified=True, elapsed_seconds=elapsed)
            if resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            if cache_file:
                cache_file.write_text(
                    json.dumps(
                        {"url": url, "etag": hdrs.get("etag", ""),
                         "last_modified": hdrs.get("last-modified", "")}
                    )
                )
            return FetchResult(url, resp.status_code, resp.content, hdrs, elapsed_seconds=elapsed)
        except httpx.HTTPStatusError:
            raise
        except httpx.HTTPError as exc:
            last_exc = exc
            time.sleep(delay)
            delay *= 2

    raise NetworkUnavailable(
        f"Could not fetch {url} after {cfg.max_retries} attempts: {last_exc}",
        remedy="Check connectivity, then retry. If Metro is down, work from a pinned "
        "snapshot with --snapshot, or import a Mobility Database archive with "
        "`stl snapshot import`.",
        url=url,
    )


def head(url: str, cfg: HttpConfig) -> dict[str, Any]:
    _throttle(url, cfg.min_interval_seconds)
    with httpx.Client(timeout=cfg.timeout_seconds, follow_redirects=True) as client:
        resp = client.head(url, headers={"User-Agent": cfg.user_agent})
    return {"status": resp.status_code, "headers": {k.lower(): v for k, v in resp.headers.items()}}
