import base64
import binascii
import json
import zlib
from pathlib import Path

import pytest

from isis_monitor.instlist import from_snapshot, parse_instlist, to_snapshot

# CS:INSTLIST's b64byt as published on 13 Oct 2026 (public facility metadata).
SAMPLE = (Path(__file__).parent / "data" / "instlist_2026-10-13.b64").read_text().strip()


def encode(entries) -> str:
    """As ISIS publishes it: zlib-compressed JSON, as hex, NUL-padded, in base64."""
    return base64.b64encode(binascii.hexlify(zlib.compress(json.dumps(entries).encode())) + b"\0").decode()


def test_parses_the_published_list():
    listed = parse_instlist(SAMPLE)
    assert len(listed) == 57
    assert listed["PEARL"] == ("IN:PEARL:", "TS1")
    assert listed["WISH"] == ("IN:WISH:", "TS2")
    assert listed["EMU"] == ("IN:EMU:", "Muons")
    assert listed["PEARL_SETUP"] == ("IN:PEARL_5B:", "TS1")  # not always IN:<NAME>:
    assert listed["HIFI-CRYOMAG"] == ("IN:HIFI-C11:", "Muons")
    assert all(target in ("TS1", "TS2", "Muons", None) for _, target in listed.values())


@pytest.mark.parametrize("payload", [
    "!!!not base64",
    base64.b64encode(b"zz").decode(),  # not hex
    base64.b64encode(b"00ff").decode(),  # not zlib
    encode({"name": "PEARL"}),  # not a list
    pytest.param(encode(["x" * (1 << 20)]), id="too big uncompressed"),
    pytest.param(base64.b64encode(binascii.hexlify(zlib.compress(b"[]")[:-4])).decode(), id="cut short"),
    12,
])
def test_unreadable_lists_are_empty_with_a_warning(payload, caplog):
    assert parse_instlist(payload) == {}
    assert "Unreadable CS:INSTLIST" in caplog.text


def test_unusable_entries_are_skipped():
    listed = parse_instlist(encode([
        {"name": "pearl", "pvPrefix": "IN:PEARL:", "targetStation": "MISC"},
        {"name": "BAD", "pvPrefix": "IN:BAD\nX:"},
        {"name": "NOPREFIX"},
        "junk",
    ]))
    assert listed == {"PEARL": ("IN:PEARL:", None)}


def test_snapshot_round_trip_skips_malformed_entries():
    listed = parse_instlist(SAMPLE)
    assert from_snapshot(json.loads(json.dumps(to_snapshot(listed)))) == listed
    assert from_snapshot({"A": ["IN:A:", "TS3"], "B": ["no colon", None], "C": "x", "D": ["IN:D:", None]}) == {
        "D": ("IN:D:", None)
    }
    assert from_snapshot(None) == {}
