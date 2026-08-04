"""Minimal protobuf wire-format reader.

Deliberately hand-rolled rather than using `gtfs-realtime-bindings`, for two
reasons:

  1. The LP3 tool cannot use a protobuf runtime -- `com.google.protobuf:*` is
     not on the Light SDK dependency allow-list. Whatever decodes GTFS-RT on
     the device is either kotlinx-serialization-protobuf or a hand-written
     reader. This module is the reference implementation for that port.
  2. Reading at the wire level discovers fields that are not in the schema,
     which is exactly what the field-usage census needs.

Wire types: 0 varint, 1 fixed64, 2 length-delimited, 5 fixed32.
Types 3/4 (start/end group) are proto2 legacy and unused by GTFS-RT.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

WIRE_NAMES = {0: "varint", 1: "fixed64", 2: "length-delimited", 5: "fixed32"}


class WireError(ValueError):
    pass


@dataclass
class Field_:
    number: int
    wire_type: int
    raw: bytes
    varint: int | None = None
    offset: int = 0

    @property
    def wire_name(self) -> str:
        return WIRE_NAMES.get(self.wire_type, f"unknown({self.wire_type})")


@dataclass
class Message:
    fields: list[Field_] = field(default_factory=list)

    def by_number(self, number: int) -> list[Field_]:
        return [f for f in self.fields if f.number == number]

    def first(self, number: int) -> Field_ | None:
        found = self.by_number(number)
        return found[0] if found else None


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise WireError(f"truncated varint starting at {start}")
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise WireError(f"varint too long at {start}")


def zigzag(n: int) -> int:
    """Decode a sint32/sint64 zigzag value. GTFS-RT does not use these, but a
    decoder that silently mis-handles them will produce plausible nonsense."""
    return (n >> 1) ^ -(n & 1)


def parse(data: bytes) -> Message:
    """Parse one protobuf message. No schema required."""
    msg, pos = Message(), 0
    while pos < len(data):
        start = pos
        key, pos = read_varint(data, pos)
        number, wire_type = key >> 3, key & 0x07
        if number == 0:
            raise WireError(f"field number 0 at offset {start}")
        if wire_type == WIRE_VARINT:
            value, pos = read_varint(data, pos)
            msg.fields.append(Field_(number, wire_type, b"", value, start))
        elif wire_type == WIRE_FIXED64:
            if pos + 8 > len(data):
                raise WireError(f"truncated fixed64 at {pos}")
            msg.fields.append(Field_(number, wire_type, data[pos : pos + 8], None, start))
            pos += 8
        elif wire_type == WIRE_FIXED32:
            if pos + 4 > len(data):
                raise WireError(f"truncated fixed32 at {pos}")
            msg.fields.append(Field_(number, wire_type, data[pos : pos + 4], None, start))
            pos += 4
        elif wire_type == WIRE_LEN:
            length, pos = read_varint(data, pos)
            if pos + length > len(data):
                raise WireError(f"length-delimited field overruns buffer at {pos}")
            msg.fields.append(Field_(number, wire_type, data[pos : pos + length], None, start))
            pos += length
        else:
            raise WireError(f"unsupported wire type {wire_type} at offset {start}")
    return msg


def as_float(f: Field_) -> float:
    if f.wire_type == WIRE_FIXED32:
        return struct.unpack("<f", f.raw)[0]
    if f.wire_type == WIRE_FIXED64:
        return struct.unpack("<d", f.raw)[0]
    if f.wire_type == WIRE_VARINT and f.varint is not None:
        return float(f.varint)
    raise WireError(f"field {f.number} (wire {f.wire_name}) is not numeric")


def as_int(f: Field_) -> int:
    """Decode an integral field, applying the two's-complement fixup.

    Protobuf encodes a negative int32/int64 as the FULL 64-bit two's-complement
    value, so -180 arrives on the wire as 18446744073709551436. Returning the
    raw varint makes every negative number enormous instead of negative --
    which for `StopTimeEvent.delay` means every early bus becomes a timestamp
    ~584 billion years in the future. Port this fixup; it is not optional.

    Note this is NOT zigzag: zigzag applies only to sint32/sint64, which
    GTFS-RT does not use. See `zigzag()` for that case.
    """
    if f.wire_type == WIRE_VARINT and f.varint is not None:
        v = f.varint
        return v - (1 << 64) if v >= (1 << 63) else v
    if f.wire_type == WIRE_FIXED64:
        return struct.unpack("<q", f.raw)[0]
    if f.wire_type == WIRE_FIXED32:
        return struct.unpack("<i", f.raw)[0]
    raise WireError(f"field {f.number} (wire {f.wire_name}) is not integral")


def as_uint(f: Field_) -> int:
    """Decode an unsigned integral field -- no two's-complement fixup.

    Used for fields the spec declares uint32/uint64 (timestamps, sequence
    numbers), where a value above 2^63 is legitimate rather than negative.
    """
    if f.wire_type == WIRE_VARINT and f.varint is not None:
        return f.varint
    if f.wire_type == WIRE_FIXED64:
        return struct.unpack("<Q", f.raw)[0]
    if f.wire_type == WIRE_FIXED32:
        return struct.unpack("<I", f.raw)[0]
    raise WireError(f"field {f.number} (wire {f.wire_name}) is not integral")


def as_str(f: Field_) -> str:
    if f.wire_type != WIRE_LEN:
        raise WireError(f"field {f.number} (wire {f.wire_name}) is not a string")
    return f.raw.decode("utf-8", errors="replace")


def as_message(f: Field_) -> Message:
    if f.wire_type != WIRE_LEN:
        raise WireError(f"field {f.number} (wire {f.wire_name}) is not a submessage")
    return parse(f.raw)


def dump(data: bytes, max_depth: int = 6, _depth: int = 0,
         path: str = "") -> list[dict[str, Any]]:
    """Recursive structural dump: field number, wire type, length, bytes.

    This is the ground-truth artifact. Point the Kotlin decoder at the same
    bytes and compare against this tree.
    """
    out: list[dict[str, Any]] = []
    try:
        msg = parse(data)
    except WireError as exc:
        return [{"error": str(exc), "path": path}]
    for f in msg.fields:
        node: dict[str, Any] = {
            "path": f"{path}.{f.number}" if path else str(f.number),
            "field": f.number,
            "wire_type": f.wire_type,
            "wire_name": f.wire_name,
            "offset": f.offset,
        }
        if f.wire_type == WIRE_VARINT:
            node["varint"] = f.varint
        elif f.wire_type in (WIRE_FIXED32, WIRE_FIXED64):
            node["hex"] = f.raw.hex()
            try:
                node["as_float"] = round(as_float(f), 6)
            except WireError:
                pass
        else:
            node["length"] = len(f.raw)
            printable = _maybe_text(f.raw)
            if printable is not None:
                node["as_string"] = printable
            if _depth < max_depth:
                nested = _try_nested(f.raw, max_depth, _depth + 1, node["path"])
                if nested:
                    node["submessage"] = nested
            if "as_string" not in node and "submessage" not in node:
                node["hex"] = f.raw[:64].hex()
        out.append(node)
    return out


def _maybe_text(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text and all(c == "\n" or c == "\t" or 0x20 <= ord(c) < 0x7F or ord(c) > 0xA0 for c in text):
        return text
    return None


def _try_nested(raw: bytes, max_depth: int, depth: int, path: str) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    try:
        msg = parse(raw)
    except WireError:
        return None
    if not msg.fields:
        return None
    return dump(raw, max_depth, depth, path)


def walk(
    data: bytes,
    max_depth: int = 8,
    is_scalar: Callable[[str], bool] | None = None,
) -> Iterator[tuple[str, int, int]]:
    """Yield (path, field_number, wire_type) for every field, recursively.

    Feeds the field-usage census: paths that appear in the bytes but not in the
    schema map are fields Metro populates that you have not modelled.

    `is_scalar(path)` should return True for any path the schema declares as a
    string or number. Without it the walker descends into every length-delimited
    field whose bytes happen to re-parse as protobuf -- and ASCII digits almost
    always do. A `stop_id` of "15111" then reports phantom subfields, and the
    census flags them as unmodelled, which is exactly the signal that is
    supposed to mean "investigate before porting".
    """

    def _walk(buf: bytes, prefix: str, depth: int) -> Iterator[tuple[str, int, int]]:
        if depth > max_depth:
            return
        try:
            msg = parse(buf)
        except WireError:
            return
        for f in msg.fields:
            p = f"{prefix}.{f.number}" if prefix else str(f.number)
            yield p, f.number, f.wire_type
            if f.wire_type != WIRE_LEN or not f.raw:
                continue
            if is_scalar is not None and is_scalar(p):
                continue
            try:
                sub = parse(f.raw)
            except WireError:
                continue
            if sub.fields:
                yield from _walk(f.raw, p, depth + 1)

    yield from _walk(data, "", 0)
