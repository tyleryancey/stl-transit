"""Content-addressed snapshot store.

snapshot_id = <kind>-<UTC timestamp>-<sha256[:6]>. The content suffix means an
unchanged re-fetch is detectable without a diff.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import store_root
from ..errors import SnapshotNotFound


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_snapshot_id(kind: str, digest: str, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"{kind}-{when.strftime('%Y%m%dT%H%M%SZ')}-{digest[:6]}"


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    kind: str
    source: str
    path: Path
    manifest: dict[str, Any]

    @property
    def payload(self) -> Path:
        return self.path / self.manifest["filename"]

    @property
    def sha256(self) -> str:
        return self.manifest.get("sha256", "")

    @property
    def fetched_at(self) -> datetime | None:
        raw = self.manifest.get("fetched_at")
        return datetime.fromisoformat(raw) if raw else None


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or store_root()

    # ------------------------------------------------------------- layout --
    def _kind_dir(self, kind: str) -> Path:
        return self.root / "snapshots" / kind

    @property
    def pins_file(self) -> Path:
        return self.root / "pins.json"

    @property
    def http_cache(self) -> Path:
        d = self.root / "cache" / "http"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def jobs_dir(self) -> Path:
        d = self.root / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def recordings_dir(self, session: str) -> Path:
        d = self.root / "archives" / "rt-recordings" / session
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -------------------------------------------------------------- write --
    def put(
        self,
        kind: str,
        source: str,
        data: bytes,
        filename: str,
        source_url: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Snapshot:
        digest = sha256_bytes(data)
        fetched_at = datetime.now(timezone.utc)
        sid = make_snapshot_id(kind, digest, fetched_at)
        path = self._kind_dir(kind) / sid
        path.mkdir(parents=True, exist_ok=True)
        (path / filename).write_bytes(data)
        manifest: dict[str, Any] = {
            "snapshot_id": sid,
            "kind": kind,
            "source": source,
            "source_url": source_url,
            "filename": filename,
            "sha256": digest,
            "bytes": len(data),
            "fetched_at": fetched_at.isoformat(),
        }
        manifest.update(extra or {})
        (path / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        return Snapshot(sid, kind, source, path, manifest)

    # --------------------------------------------------------------- read --
    def list(self, kind: str | None = None, source: str | None = None) -> list[Snapshot]:
        out: list[Snapshot] = []
        kinds = [kind] if kind else self._known_kinds()
        for k in kinds:
            base = self._kind_dir(k)
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                mf = d / "manifest.json"
                if not mf.is_file():
                    continue
                m = json.loads(mf.read_text())
                if source and m.get("source") != source:
                    continue
                out.append(Snapshot(m["snapshot_id"], k, m.get("source", ""), d, m))
        out.sort(key=lambda s: s.manifest.get("fetched_at", ""), reverse=True)
        return out

    def _known_kinds(self) -> list[str]:
        base = self.root / "snapshots"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []

    def get(self, ref: str) -> Snapshot:
        """Resolve a snapshot by id, pin name, or 'latest:<source>'."""
        pins = self.pins()
        if ref in pins:
            ref = pins[ref]
        if ref.startswith("latest:"):
            src = ref.split(":", 1)[1]
            found = self.list(source=src)
            if not found:
                raise SnapshotNotFound(
                    f"No snapshot yet for source {src!r}.",
                    remedy=f"Fetch one with `stl snapshot fetch {src}`.",
                    source=src,
                )
            return found[0]
        for snap in self.list():
            if snap.snapshot_id == ref:
                return snap
        raise SnapshotNotFound(
            f"No snapshot matching {ref!r}.",
            remedy="Run `stl snapshot list` to see what is available.",
            requested=ref,
            available=len(self.list()),
        )

    def latest(self, source: str) -> Snapshot:
        return self.get(f"latest:{source}")

    def find_by_digest(self, kind: str, source: str, digest: str) -> Snapshot | None:
        for snap in self.list(kind=kind, source=source):
            if snap.sha256 == digest:
                return snap
        return None

    # --------------------------------------------------------------- pins --
    def pins(self) -> dict[str, str]:
        if self.pins_file.is_file():
            return json.loads(self.pins_file.read_text())
        return {}

    def pin(self, snapshot_id: str, name: str) -> dict[str, str]:
        self.get(snapshot_id)  # validate
        pins = self.pins()
        pins[name] = snapshot_id
        self.pins_file.parent.mkdir(parents=True, exist_ok=True)
        self.pins_file.write_text(json.dumps(pins, indent=2, sort_keys=True))
        return pins

    def unpin(self, name: str) -> dict[str, str]:
        pins = self.pins()
        pins.pop(name, None)
        self.pins_file.write_text(json.dumps(pins, indent=2, sort_keys=True))
        return pins

    # ----------------------------------------------------------- lifecycle --
    def verify(self, snapshot_id: str) -> dict[str, Any]:
        snap = self.get(snapshot_id)
        actual = sha256_bytes(snap.payload.read_bytes())
        return {
            "snapshot_id": snapshot_id,
            "expected_sha256": snap.sha256,
            "actual_sha256": actual,
            "intact": actual == snap.sha256,
        }

    def gc(self, keep: int = 5, dry_run: bool = True) -> dict[str, Any]:
        pinned = set(self.pins().values())
        removed, freed = [], 0
        by_source: dict[tuple[str, str], list[Snapshot]] = {}
        for snap in self.list():
            by_source.setdefault((snap.kind, snap.source), []).append(snap)
        for snaps in by_source.values():
            for snap in snaps[keep:]:
                if snap.snapshot_id in pinned:
                    continue
                size = sum(f.stat().st_size for f in snap.path.rglob("*") if f.is_file())
                removed.append(snap.snapshot_id)
                freed += size
                if not dry_run:
                    shutil.rmtree(snap.path)
        return {"removed": removed, "freed_bytes": freed, "dry_run": dry_run}
