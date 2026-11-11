"""Live vehicle positions: STCP (Porto buses), SMTUC (Coimbra buses) and Carris (Lisbon buses and trams).

STCP comes from the Porto urban platform (FIWARE NGSI v2, open data); SMTUC and Carris publish
GTFS-Realtime. Positions are what each operator's vehicles report, usually under a minute old.
Line names, next stops and destinations come from the operator's timetable when it is loaded.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import gtfsrt, transit
from .app import mcp
from .common import clamp, fail, haversine_km, http, norm

log = logging.getLogger("portugal_mcp.transit_live")

LIVE: dict[str, dict[str, str]] = {
    "stcp": {
        "name": "STCP (Porto buses)",
        "url": "https://broker.fiware.urbanplatform.portodigital.pt/v2/entities",
        "source": "Porto Digital urban platform (STCP vehicles, open data CC0)",
    },
    "smtuc": {
        "name": "SMTUC (Coimbra buses)",
        "url": "https://api.planner.agit.pt/v1/datasets/smtuc/realtime/vehicles/feed",
        "source": "AGIT - Região de Coimbra planner API (GTFS-Realtime, CC BY)",
    },
    "carris": {
        "name": "Carris (Lisbon buses and trams)",
        "url": "https://gateway.carris.pt/gateway/gtfs/api/v2.11/GTFS/realtime/vehiclepositions",
        "source": "Carris GTFS-Realtime (gateway.carris.pt)",
    },
}
LIVE_ALIASES = {"porto": "stcp", "coimbra": "smtuc", "lisboa": "carris", "lisbon": "carris"}
MAX_AGE = 15 * 60  # drop positions older than this (parked or switched-off vehicles)

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lock = threading.Lock()


def _stcp() -> list[dict[str, Any]]:
    rows = http().get(
        LIVE["stcp"]["url"],
        params={"type": "Vehicle", "q": "vehicleType==bus", "limit": "1000", "options": "keyValues"},
        timeout=30,
    ).json()
    out = []
    for r in rows:
        ann = {}
        for a in r.get("annotations") or []:
            k, _, v = str(a).partition(":")[2].partition(":")
            ann[k] = v
        coords = (r.get("location") or {}).get("coordinates") or [None, None]
        ts = r.get("observationDateTime") or r.get("dateModified")
        try:
            epoch = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError:
            epoch = None
        if coords[0] is None:
            continue
        out.append({
            "vehicle_id": r.get("fleetVehicleId"),
            "line": ann.get("route"),
            "direction_id": int(ann["sentido"]) if ann.get("sentido", "").isdigit() else None,
            "lat": coords[1],
            "lon": coords[0],
            "bearing": r.get("bearing"),
            "speed_kmh": r.get("speed"),
            "timestamp": epoch,
        })
    return out


def _gtfsrt(op: str) -> list[dict[str, Any]]:
    _, rows = gtfsrt.vehicles(http().get(LIVE[op]["url"], timeout=30, headers={"Accept": "*/*"}).content)
    return rows


def _fetch(op: str) -> list[dict[str, Any]]:
    with _lock:
        hit = _cache.get(op)
        if hit and time.time() - hit[0] < 15:
            return hit[1]
    rows = _stcp() if op == "stcp" else _gtfsrt(op)
    with _lock:
        _cache[op] = (time.time(), rows)
    return rows


def _enrich(op: str, rows: list[dict[str, Any]]) -> None:
    """Line names, destinations and next-stop names from the operator's timetable (best effort)."""
    if op not in transit.OPERATORS:
        return
    try:
        con = transit._db(op)
    except Exception:  # noqa: BLE001 — live positions still work without names
        log.warning("%s timetable unavailable; live positions without names", op, exc_info=True)
        return
    routes = {r["route_id"]: (r["short"] or r["long"]) for r in con.execute("SELECT route_id, short, long FROM routes")}
    for v in rows:
        rid = v.pop("route_id", None)
        v["line"] = routes.get(rid) or rid
        trip = v.pop("trip_id", None)
        if trip:
            try:
                t = con.execute("SELECT headsign FROM trips WHERE trip_id = ?", (trip,)).fetchone()
                if t and t[0]:
                    v["towards"] = t[0]
            except sqlite3.Error:
                pass
        sid = v.pop("stop_id", None)
        if sid:
            s = con.execute("SELECT name FROM stops WHERE stop_id = ?", (sid,)).fetchone()
            if s:
                v["next_stop"] = f"{v.pop('status', None) or 'towards'} {s[0]}".strip()


@mcp.tool()
def transit_vehicles(
    operator: str,
    line: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 1.0,
    limit: int = 20,
) -> dict[str, Any]:
    """Where the buses (and Lisbon trams) are right now: live positions reported by the vehicles.

    Args:
        operator: "stcp" (Porto buses), "smtuc" (Coimbra buses) or "carris" (Lisbon buses and
            trams). City names work too: "porto", "coimbra", "lisboa".
        line: keep one line, e.g. "500", "205", "28E", "103".
        lat, lon: keep the vehicles near this point (within radius_km), closest first.
    For Greater-Lisbon buses use bus_vehicle_positions / bus_next_arrivals (Carris Metropolitana).
    """
    try:
        key = norm(operator).replace(" ", "_")
        op = LIVE_ALIASES.get(key, key)
        if op not in LIVE:
            raise ValueError(f"live positions exist for: {', '.join(LIVE)} (Porto, Coimbra, Lisbon)")
        n = clamp(limit, 1, 100, 20)
        now = time.time()
        rows = [dict(v) for v in _fetch(op) if v.get("timestamp") and now - v["timestamp"] < MAX_AGE]
        total = len(rows)
        if op != "stcp":
            _enrich(op, rows)
        if line:
            want = norm(line).replace(" ", "")
            rows = [v for v in rows if norm(str(v.get("line") or "")).replace(" ", "") == want]
        if lat is not None and lon is not None:
            r_km = max(0.1, min(float(radius_km), 20.0))
            for v in rows:
                v["distance_km"] = round(haversine_km(lat, lon, v["lat"], v["lon"]), 2)
            rows = sorted((v for v in rows if v["distance_km"] <= r_km), key=lambda v: v["distance_km"])
        for v in rows:
            ts = v.pop("timestamp")
            v["seconds_ago"] = max(0, round(now - ts))
            v["reported_at"] = datetime.fromtimestamp(ts, timezone.utc).astimezone(transit.TZ).strftime("%H:%M:%S")
            if v.get("label") == v.get("vehicle_id"):
                v.pop("label", None)
            for k in [k for k, x in v.items() if x is None]:
                del v[k]
        return {
            "operator": LIVE[op]["name"],
            "vehicles_reporting": total,
            "matching": len(rows),
            "vehicles": rows[:n],
            "note": "Live positions as the vehicles report them; not a prediction of arrival times.",
            "source": LIVE[op]["source"],
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "transit_vehicles", exc)
