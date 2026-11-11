"""A tiny GTFS-Realtime reader for vehicle positions (no protobuf dependency).

Only the fields the live-vehicle tool needs are read; everything else in the message is skipped.
Spec: https://gtfs.org/realtime/reference/ (FeedMessage > FeedEntity > VehiclePosition).
"""

from __future__ import annotations

import struct
from typing import Any

STATUS = {0: "arriving at", 1: "stopped at", 2: "on the way to"}


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf: bytes):
    """Yield (field_number, wire_type, value) for one message; value is int, bytes or a float."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = struct.unpack_from("<d", buf, i)[0], i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i : i + ln], i + ln
        elif wt == 5:
            v, i = struct.unpack_from("<f", buf, i)[0], i + 4
        else:  # groups (3/4) are not used by GTFS-RT
            raise ValueError(f"unsupported protobuf wire type {wt}")
        yield num, wt, v


def _msg(buf: bytes) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for num, _, v in _fields(buf):
        out.setdefault(num, v)
    return out


def _s(v: Any) -> str | None:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else None


def vehicles(feed: bytes) -> tuple[int | None, list[dict[str, Any]]]:
    """Decode a GTFS-RT feed: (header timestamp, [vehicle dicts])."""
    header_ts: int | None = None
    out: list[dict[str, Any]] = []
    for num, _, v in _fields(feed):
        if num == 1 and isinstance(v, bytes):  # FeedHeader
            header_ts = _msg(v).get(3)
        elif num == 2 and isinstance(v, bytes):  # FeedEntity
            ent = _msg(v)
            raw = ent.get(4)  # VehiclePosition
            if not isinstance(raw, bytes):
                continue
            vp = _msg(raw)
            trip = _msg(vp[1]) if isinstance(vp.get(1), bytes) else {}
            pos = _msg(vp[2]) if isinstance(vp.get(2), bytes) else {}
            veh = _msg(vp[8]) if isinstance(vp.get(8), bytes) else {}
            if 1 not in pos or 2 not in pos:
                continue
            out.append(
                {
                    "vehicle_id": _s(veh.get(1)) or _s(ent.get(1)),
                    "label": _s(veh.get(2)),
                    "trip_id": _s(trip.get(1)),
                    "route_id": _s(trip.get(5)),
                    "direction_id": trip.get(6),
                    "lat": round(pos[1], 6),
                    "lon": round(pos[2], 6),
                    "bearing": round(pos[3]) if 3 in pos else None,
                    "speed_kmh": round(pos[5] * 3.6) if 5 in pos else None,
                    "stop_id": _s(vp.get(7)),
                    "status": STATUS.get(vp.get(4)) if 4 in vp else None,
                    "timestamp": vp.get(5),
                }
            )
    return header_ts, out
