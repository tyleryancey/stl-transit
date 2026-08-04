"""Schema-driven GTFS-RT decode, built on the hand-rolled wire reader."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import schema as S
from . import wire


def _coerce(f: wire.Field_, kind: str) -> Any:
    if kind == "str":
        return wire.as_str(f)
    if kind in S.SIGNED_KINDS:
        return wire.as_int(f)
    if kind in S.UNSIGNED_KINDS:
        return wire.as_uint(f)
    if kind == "bool":
        return bool(wire.as_uint(f))
    if kind == "float":
        return round(wire.as_float(f), 6)
    if kind.startswith("enum:"):
        value = wire.as_uint(f)
        return S.ENUMS.get(kind.split(":", 1)[1], {}).get(value, value)
    if kind.startswith("msg:"):
        return decode_message(f.raw, kind.split(":", 1)[1])
    return f.raw.hex()


def decode_message(data: bytes, message: str) -> dict[str, Any]:
    """Decode `data` per the named schema. Unknown fields are preserved under
    `_unknown` rather than dropped -- silently discarding fields is how you
    ship a decoder that is wrong in ways nobody notices."""
    spec = S.MESSAGES.get(message, {})
    out: dict[str, Any] = {}
    unknown: list[dict[str, Any]] = []
    for f in wire.parse(data).fields:
        entry = spec.get(f.number)
        if entry is None:
            unknown.append(
                {"field": f.number, "wire": f.wire_name,
                 "bytes": len(f.raw) if f.wire_type == wire.WIRE_LEN else None,
                 "varint": f.varint}
            )
            continue
        name, kind, repeated = entry
        try:
            value = _coerce(f, kind)
        except wire.WireError as exc:
            unknown.append({"field": f.number, "name": name, "error": str(exc)})
            continue
        if repeated:
            out.setdefault(name, []).append(value)
        else:
            out[name] = value
    if unknown:
        out["_unknown"] = unknown
    return out


def decode_feed(data: bytes) -> dict[str, Any]:
    feed = decode_message(data, "FeedMessage")
    header = feed.get("header", {})
    ts = header.get("timestamp")
    entities = feed.get("entity", [])
    kinds = Counter()
    for e in entities:
        for k in ("trip_update", "vehicle", "alert"):
            if k in e:
                kinds[k] += 1
    return {
        "header": header,
        # `is not None`, not truthiness: epoch 0 is a real (if implausible)
        # timestamp, and conflating it with absence hides a broken producer.
        "header_timestamp_iso": (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None
        ),
        "entity_count": len(entities),
        "entity_kinds": dict(kinds),
        "entities": entities,
        "unknown_top_level": feed.get("_unknown", []),
    }


def field_census(samples: list[bytes]) -> dict[str, Any]:
    """Which proto fields does this feed actually populate, and how often?

    Answers the question that decides what to model in Kotlin: everything with
    a low occurrence rate can be skipped in v1, and anything appearing here
    that is NOT in schema.MESSAGES is a field you have not modelled at all.
    """
    counts: Counter[str] = Counter()
    wires: dict[str, set[str]] = {}
    messages_seen = 0
    for blob in samples:
        messages_seen += 1
        seen_in_sample: set[str] = set()
        for path, _num, wt in wire.walk(blob, is_scalar=S.is_scalar_path):
            named = S.path_names(path)
            seen_in_sample.add(named)
            wires.setdefault(named, set()).add(wire.WIRE_NAMES.get(wt, str(wt)))
        for named in seen_in_sample:
            counts[named] += 1

    rows = []
    for named, n in counts.most_common():
        rows.append(
            {
                "path": named,
                "samples_present": n,
                "presence_rate": round(n / max(1, messages_seen), 4),
                "wire_types": sorted(wires.get(named, [])),
                "modelled": "?" not in named,
            }
        )
    unmodelled = [r for r in rows if not r["modelled"]]
    return {
        "samples": messages_seen,
        "distinct_paths": len(rows),
        "unmodelled_paths": len(unmodelled),
        "fields": rows,
        "unmodelled": unmodelled,
        "note": "Paths containing '?<n>' are field numbers absent from "
                "core/rt/schema.py. Investigate before porting the decoder.",
    }
