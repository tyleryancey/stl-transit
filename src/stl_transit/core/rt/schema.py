"""Declarative GTFS-Realtime field map.

Doubles as the porting reference: each entry is (field number, name, kind,
repeated). Transcribe this table into Kotlin and the decoder writes itself.

Field numbers are from gtfs-realtime.proto v2.0. `kind` is one of
str | int32 | int64 | uint32 | uint64 | float | bool | enum:<name> | msg:<Name>.

SIGNEDNESS IS LOAD-BEARING, not documentation. Protobuf encodes a negative
int32/int64 as the full 64-bit two's-complement varint, so a decoder that
treats `StopTimeEvent.delay` as unsigned turns "3 minutes early" (-180) into
18446744073709551436. Roughly a third of the delay values in Metro's feed are
negative. `int*` kinds get the two's-complement fixup; `uint*` kinds must not.
"""

from __future__ import annotations

from typing import Any

Spec = dict[int, tuple[str, str, bool]]  # number -> (name, kind, repeated)

SIGNED_KINDS = {"int32", "int64", "int"}
UNSIGNED_KINDS = {"uint32", "uint64"}
SCALAR_KINDS = SIGNED_KINDS | UNSIGNED_KINDS | {"str", "float", "bool"}

FEED_MESSAGE: Spec = {
    1: ("header", "msg:FeedHeader", False),
    2: ("entity", "msg:FeedEntity", True),
}

FEED_HEADER: Spec = {
    1: ("gtfs_realtime_version", "str", False),
    2: ("incrementality", "enum:Incrementality", False),
    3: ("timestamp", "uint64", False),
}

FEED_ENTITY: Spec = {
    1: ("id", "str", False),
    2: ("is_deleted", "bool", False),
    3: ("trip_update", "msg:TripUpdate", False),
    4: ("vehicle", "msg:VehiclePosition", False),
    5: ("alert", "msg:Alert", False),
}

TRIP_UPDATE: Spec = {
    1: ("trip", "msg:TripDescriptor", False),
    2: ("stop_time_update", "msg:StopTimeUpdate", True),
    3: ("vehicle", "msg:VehicleDescriptor", False),
    4: ("timestamp", "uint64", False),
    5: ("delay", "int32", False),  # signed: negative means running early
}

TRIP_DESCRIPTOR: Spec = {
    1: ("trip_id", "str", False),
    5: ("route_id", "str", False),
    6: ("direction_id", "uint32", False),
    2: ("start_time", "str", False),
    3: ("start_date", "str", False),
    4: ("schedule_relationship", "enum:TripScheduleRelationship", False),
}

STOP_TIME_UPDATE: Spec = {
    1: ("stop_sequence", "uint32", False),
    4: ("stop_id", "str", False),
    2: ("arrival", "msg:StopTimeEvent", False),
    3: ("departure", "msg:StopTimeEvent", False),
    5: ("schedule_relationship", "enum:StopScheduleRelationship", False),
}

STOP_TIME_EVENT: Spec = {
    1: ("delay", "int32", False),  # signed: negative means running early
    2: ("time", "int64", False),
    3: ("uncertainty", "int32", False),
}

VEHICLE_POSITION: Spec = {
    1: ("trip", "msg:TripDescriptor", False),
    8: ("vehicle", "msg:VehicleDescriptor", False),
    2: ("position", "msg:Position", False),
    3: ("current_stop_sequence", "uint32", False),
    7: ("stop_id", "str", False),
    4: ("current_status", "enum:VehicleStopStatus", False),
    5: ("timestamp", "uint64", False),
    6: ("congestion_level", "enum:CongestionLevel", False),
    9: ("occupancy_status", "enum:OccupancyStatus", False),
}

POSITION: Spec = {
    1: ("latitude", "float", False),
    2: ("longitude", "float", False),
    3: ("bearing", "float", False),
    4: ("odometer", "float", False),
    5: ("speed", "float", False),
}

VEHICLE_DESCRIPTOR: Spec = {
    1: ("id", "str", False),
    2: ("label", "str", False),
    3: ("license_plate", "str", False),
}

ALERT: Spec = {
    1: ("active_period", "msg:TimeRange", True),
    5: ("informed_entity", "msg:EntitySelector", True),
    6: ("cause", "enum:Cause", False),
    7: ("effect", "enum:Effect", False),
    8: ("url", "msg:TranslatedString", False),
    10: ("header_text", "msg:TranslatedString", False),
    11: ("description_text", "msg:TranslatedString", False),
}

TIME_RANGE: Spec = {1: ("start", "uint64", False), 2: ("end", "uint64", False)}

ENTITY_SELECTOR: Spec = {
    1: ("agency_id", "str", False),
    2: ("route_id", "str", False),
    3: ("route_type", "int32", False),
    4: ("trip", "msg:TripDescriptor", False),
    5: ("stop_id", "str", False),
    6: ("direction_id", "uint32", False),
}

TRANSLATED_STRING: Spec = {1: ("translation", "msg:Translation", True)}
TRANSLATION: Spec = {1: ("text", "str", False), 2: ("language", "str", False)}

MESSAGES: dict[str, Spec] = {
    "FeedMessage": FEED_MESSAGE,
    "FeedHeader": FEED_HEADER,
    "FeedEntity": FEED_ENTITY,
    "TripUpdate": TRIP_UPDATE,
    "TripDescriptor": TRIP_DESCRIPTOR,
    "StopTimeUpdate": STOP_TIME_UPDATE,
    "StopTimeEvent": STOP_TIME_EVENT,
    "VehiclePosition": VEHICLE_POSITION,
    "Position": POSITION,
    "VehicleDescriptor": VEHICLE_DESCRIPTOR,
    "Alert": ALERT,
    "TimeRange": TIME_RANGE,
    "EntitySelector": ENTITY_SELECTOR,
    "TranslatedString": TRANSLATED_STRING,
    "Translation": TRANSLATION,
}

ENUMS: dict[str, dict[int, str]] = {
    "Incrementality": {0: "FULL_DATASET", 1: "DIFFERENTIAL"},
    "TripScheduleRelationship": {
        0: "SCHEDULED", 1: "ADDED", 2: "UNSCHEDULED", 3: "CANCELED", 5: "REPLACEMENT",
    },
    "StopScheduleRelationship": {0: "SCHEDULED", 1: "SKIPPED", 2: "NO_DATA", 3: "UNSCHEDULED"},
    "VehicleStopStatus": {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"},
    "CongestionLevel": {
        0: "UNKNOWN_CONGESTION_LEVEL", 1: "RUNNING_SMOOTHLY",
        2: "STOP_AND_GO", 3: "CONGESTION", 4: "SEVERE_CONGESTION",
    },
    "OccupancyStatus": {
        0: "EMPTY", 1: "MANY_SEATS_AVAILABLE", 2: "FEW_SEATS_AVAILABLE",
        3: "STANDING_ROOM_ONLY", 4: "CRUSHED_STANDING_ROOM_ONLY",
        5: "FULL", 6: "NOT_ACCEPTING_PASSENGERS",
    },
    "Cause": {
        1: "UNKNOWN_CAUSE", 2: "OTHER_CAUSE", 3: "TECHNICAL_PROBLEM", 4: "STRIKE",
        5: "DEMONSTRATION", 6: "ACCIDENT", 7: "HOLIDAY", 8: "WEATHER",
        9: "MAINTENANCE", 10: "CONSTRUCTION", 11: "POLICE_ACTIVITY", 12: "MEDICAL_EMERGENCY",
    },
    "Effect": {
        1: "NO_SERVICE", 2: "REDUCED_SERVICE", 3: "SIGNIFICANT_DELAYS", 4: "DETOUR",
        5: "ADDITIONAL_SERVICE", 6: "MODIFIED_SERVICE", 7: "OTHER_EFFECT",
        8: "UNKNOWN_EFFECT", 9: "STOP_MOVED", 10: "NO_EFFECT", 11: "ACCESSIBILITY_ISSUE",
    },
}


def resolve_path(path: str, root: str = "FeedMessage") -> tuple[str, str | None]:
    """Translate a numeric wire path like '2.3.1.1' into (named_path, kind).

    `kind` is the declared kind of the LAST field in the path, or None when the
    path leaves the schema. Callers use it to decide whether descending further
    is meaningful: a length-delimited field declared `str` holds text, and
    parsing its bytes as a submessage invents fields that do not exist.
    """
    parts, current, named = path.split("."), root, []
    kind: str | None = f"msg:{root}"
    for raw in parts:
        try:
            num = int(raw)
        except ValueError:
            named.append(raw)
            kind = None
            continue
        spec = MESSAGES.get(current or "", {})
        entry = spec.get(num)
        if entry is None:
            named.append(f"?{num}")
            current = ""
            kind = None
            continue
        name, kind, _ = entry
        named.append(name)
        current = kind.split(":", 1)[1] if kind.startswith("msg:") else ""
    return ".".join(named), kind


def path_names(path: str, root: str = "FeedMessage") -> str:
    """Translate a numeric wire path like '2.3.1.1' into a named path."""
    return resolve_path(path, root)[0]


def is_scalar_path(path: str, root: str = "FeedMessage") -> bool:
    """True when `path` names a field the schema declares as a scalar.

    A scalar's bytes are never a submessage, so the wire walker must not
    descend into them. ASCII digits in a `stop_id` re-parse as plausible
    protobuf and would otherwise be reported as unmodelled fields, sending a
    port on a hunt for something that was never there.
    """
    _, kind = resolve_path(path, root)
    return kind in SCALAR_KINDS or (kind or "").startswith("enum:")


def kotlin_reference() -> list[dict[str, Any]]:
    """Flat table for porting: message, field number, name, kind, repeated."""
    rows = []
    for msg, spec in MESSAGES.items():
        for num, (name, kind, repeated) in sorted(spec.items()):
            rows.append(
                {"message": msg, "field": num, "name": name, "kind": kind, "repeated": repeated}
            )
    return rows
