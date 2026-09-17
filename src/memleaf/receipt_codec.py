"""Lossless, bounded encoding for sealed control receipts, never memory bodies.

The outer version deliberately differs from old wrappers: an old reader must
reject this representation rather than dropping replay-suppression evidence.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import zlib
from typing import Any

ENCODING = "zlib-base64-v1"
MAX_COMPACT_RECEIPTS = 4096
MAX_DECODED_LEDGER_BYTES = 64 * 1024 * 1024


def is_compact(wrapper: Any, version: int) -> bool:
    return isinstance(wrapper, dict) and type(wrapper.get("version")) is int and wrapper["version"] == version


def encode_receipt(wrapper: dict[str, Any], *, version: int) -> dict[str, Any]:
    """Keep the exact validated original payload and checksum; no field removal."""
    raw = wrapper["payload"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != wrapper["checksum"]:
        raise ValueError("invalid_receipt_checksum")
    return {"version": version, "encoding": ENCODING, "payload_version": wrapper["version"],
            "decoded_bytes": len(raw), "payload": base64.b64encode(zlib.compress(raw)).decode("ascii"),
            "checksum": wrapper["checksum"]}


def decode_receipt(wrapper: Any, *, version: int, maximum: int, payload_versions: set[int]) -> dict[str, Any]:
    """Bound decompression before allocation, including corrupt/trailing streams."""
    if (not is_compact(wrapper, version) or set(wrapper) != {
            "version", "encoding", "payload_version", "decoded_bytes", "payload", "checksum"}
            or wrapper["encoding"] != ENCODING
            or type(wrapper["payload_version"]) is not int or wrapper["payload_version"] not in payload_versions
            or type(wrapper["decoded_bytes"]) is not int or not 0 < wrapper["decoded_bytes"] <= maximum
            or not isinstance(wrapper["payload"], str) or len(wrapper["payload"]) > (maximum + 1024) * 2
            or not isinstance(wrapper["checksum"], str)):
        raise ValueError("invalid_compact_receipt")
    try:
        packed = base64.b64decode(wrapper["payload"], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, wrapper["decoded_bytes"] + 1)
        if (len(raw) != wrapper["decoded_bytes"] or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail
                or hashlib.sha256(raw).hexdigest() != wrapper["checksum"]):
            raise ValueError("invalid_compact_receipt")
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeError, zlib.error) as error:
        raise ValueError("invalid_compact_receipt") from error
    return {"version": wrapper["payload_version"], "payload": text, "checksum": wrapper["checksum"]}


def ledger_usage(wrappers: Any, *, compact_version: int) -> dict[str, int]:
    """Inspect sizes without decompressing; individual loads still verify bytes."""
    if not isinstance(wrappers, dict):
        raise ValueError("invalid_receipt_ledger")
    compact = decoded = 0
    for wrapper in wrappers.values():
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("payload"), str):
            raise ValueError("invalid_receipt_ledger")
        if is_compact(wrapper, compact_version):
            size = wrapper.get("decoded_bytes")
            if type(size) is not int or size < 0:
                raise ValueError("invalid_compact_receipt")
            compact += 1
        else:
            size = len(wrapper["payload"].encode("utf-8"))
        decoded += size
    if compact > MAX_COMPACT_RECEIPTS or decoded > MAX_DECODED_LEDGER_BYTES:
        raise ValueError("receipt_retention_full")
    return {"full": len(wrappers) - compact, "compact": compact, "decoded_bytes": decoded}
