"""ISIS's instrument list, CS:INSTLIST: each instrument's PV prefix and target station."""
import base64
import binascii
import json
import logging
import re
import zlib
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

INSTLIST_PV = "CS:INSTLIST"
MAX_JSON = 1 << 20  # bytes, decompressed; the real list is about 10 KB
# Its targetStation values; others (e.g. MISC) aren't on one beam target.
_STATIONS = {"TS1": "TS1", "TS2": "TS2", "MUON": "Muons"}
_PREFIX_RE = re.compile(r"[A-Za-z0-9_\-:]+:")

Instruments = Dict[str, Tuple[str, Optional[str]]]  # name -> (PV prefix, beam target or None)


def parse_instlist(b64: object) -> Instruments:
    """The list from the PV's b64byt field: base64 of NUL-padded hex of
    zlib-compressed JSON. Entries without a usable name and prefix are
    skipped; {} (with a warning) if the whole thing can't be read."""
    try:
        raw = base64.b64decode(str(b64)).rstrip(b"\0")
        inflate = zlib.decompressobj()
        text = inflate.decompress(binascii.unhexlify(raw), MAX_JSON)
        if not inflate.eof:  # cut short, or too big
            raise ValueError(f"over {MAX_JSON} bytes uncompressed")
        entries = json.loads(text)
        if not isinstance(entries, list):
            raise ValueError(f"expected a list, got {type(entries).__name__}")
    except (ValueError, zlib.error) as exc:  # binascii.Error and JSONDecodeError are ValueErrors
        logger.warning(f"Unreadable {INSTLIST_PV}: {exc}")
        return {}
    result: Instruments = {}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        prefix = entry.get("pvPrefix") if isinstance(entry, dict) else None
        if isinstance(name, str) and isinstance(prefix, str) and _PREFIX_RE.fullmatch(prefix):
            result[name.upper()] = (prefix, _STATIONS.get(str(entry.get("targetStation"))))
    return result


def from_snapshot(saved: object) -> Instruments:
    """A list cached by to_snapshot(), skipping anything malformed."""
    if not isinstance(saved, dict):
        return {}
    return {
        name: (entry[0], entry[1]) for name, entry in saved.items()
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)
        and _PREFIX_RE.fullmatch(entry[0]) and entry[1] in (None, *_STATIONS.values())
    }


def to_snapshot(instruments: Instruments) -> dict:
    return {name: list(entry) for name, entry in instruments.items()}
