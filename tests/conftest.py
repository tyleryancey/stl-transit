from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stl_transit.io.db import build_sqlite, connect_ro
from stl_transit.io.store import Store

from . import fixtures


@pytest.fixture
def mini_zip(tmp_path: Path) -> Path:
    return fixtures.build_gtfs_zip(tmp_path / "mini.zip")


@pytest.fixture
def conn(mini_zip: Path, tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "mini.sqlite"
    build_sqlite(mini_zip, db)
    c = connect_ro(db)
    yield c
    c.close()


@pytest.fixture
def store(tmp_path: Path, mini_zip: Path) -> Store:
    s = Store(root=tmp_path / "store")
    s.put("gtfs", "metro_gtfs", mini_zip.read_bytes(), "source.zip", "https://example.org/f.zip")
    return s
