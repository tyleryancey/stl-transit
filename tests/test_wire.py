from stl_transit.core.rt import decode, schema, wire

from . import fixtures


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 1_754_000_000):
        assert wire.read_varint(fixtures.varint(n), 0)[0] == n


def test_parse_known_message():
    blob = fixtures.build_trip_updates_feed()
    msg = wire.parse(blob)
    assert [f.number for f in msg.fields] == [1, 2]
    assert all(f.wire_type == wire.WIRE_LEN for f in msg.fields)


def test_zigzag():
    assert wire.zigzag(0) == 0
    assert wire.zigzag(1) == -1
    assert wire.zigzag(2) == 1


def test_truncated_input_raises_not_silently_wrong():
    import pytest

    blob = fixtures.build_trip_updates_feed()
    with pytest.raises(wire.WireError):
        wire.parse(blob[:-3])


def test_decode_feed_matches_input():
    ts = 1_754_000_000
    blob = fixtures.build_trip_updates_feed(timestamp=ts, trip_id="T_WK_1200", delay=300)
    out = decode.decode_feed(blob)
    assert out["header"]["gtfs_realtime_version"] == "2.0"
    assert out["header"]["timestamp"] == ts
    assert out["header"]["incrementality"] == "FULL_DATASET"
    assert out["entity_count"] == 1
    tu = out["entities"][0]["trip_update"]
    assert tu["trip"]["trip_id"] == "T_WK_1200"
    assert tu["trip"]["route_id"] == "R11"
    stu = tu["stop_time_update"][0]
    assert stu["stop_id"] == "S1"
    assert stu["departure"]["delay"] == 300
    assert stu["schedule_relationship"] == "SCHEDULED"


def test_unknown_fields_are_preserved_not_dropped():
    blob = fixtures.build_trip_updates_feed()
    blob += fixtures.pb_string(99, "surprise")
    out = decode.decode_message(blob, "FeedMessage")
    assert any(u["field"] == 99 for u in out["_unknown"])


def test_path_names_translate_numeric_paths():
    assert schema.path_names("1.3") == "header.timestamp"
    assert schema.path_names("2.3.1.1") == "entity.trip_update.trip.trip_id"
    assert "?99" in schema.path_names("99")


def test_census_flags_unmodelled_paths():
    blob = fixtures.build_trip_updates_feed() + fixtures.pb_string(99, "surprise")
    census = decode.field_census([blob])
    assert census["unmodelled_paths"] >= 1
    assert any("?99" in row["path"] for row in census["unmodelled"])


def test_dump_produces_named_structure():
    blob = fixtures.build_trip_updates_feed()
    tree = wire.dump(blob)
    assert tree[0]["field"] == 1
    assert tree[0]["wire_name"] == "length-delimited"
    assert "submessage" in tree[0]


def test_kotlin_reference_covers_every_message():
    rows = schema.kotlin_reference()
    assert {r["message"] for r in rows} == set(schema.MESSAGES)
