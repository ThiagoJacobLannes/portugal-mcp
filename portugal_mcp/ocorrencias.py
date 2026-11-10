"""Live civil-protection incidents (ANEPC): what emergency services are responding to right now, near a place."""

from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from .app import mcp, note
from .common import clamp, fail, haversine_km, http, norm

log = logging.getLogger("portugal_mcp.ocorrencias")

FEED = "https://api.fogos.pt/v2/incidents/active"
SOURCE = "ANEPC (Autoridade Nacional de Emergência e Proteção Civil), via Fogos.pt (https://fogos.pt)"
KEY_ENV = "FOGOS_API_KEY"
KEY_HELP = (
    "Fogos.pt now requires a personal API key: request one at https://source.fogos.pt/api/request-access "
    f"and set it in the {KEY_ENV} environment variable of this MCP server."
)
CACHE_S = 120  # be gentle with Fogos.pt: one national fetch serves every query for two minutes

note(
    "ocorrencias",
    "active_incidents lists what civil protection (ANEPC: firefighters, INEM-assisted rescues, "
    "civil protection) is responding to right now: fires, road collisions, fallen trees, floods, "
    "rescues, with status and resources on scene, updated every few minutes. It does NOT include "
    "police-only activity (PSP/GNR publish nothing live): if the user saw police cars, say so and "
    "show what civil protection has nearby, which may or may not be the same event. You don't know "
    "the user's location: if they ask about 'here' or 'near me' without a place, ask for their town, "
    "parish, postal code or coordinates. Never present it as an official alert; in an emergency, 112.",
)

_mem: tuple[float, list[dict[str, Any]]] | None = None

KINDS = {
    "fire": lambda x: bool(x.get("isFire")) or "incendio" in norm(x.get("natureza")),
    "road": lambda x: bool(re.search(r"rodovi|despiste|atropelamento", norm(x.get("natureza")))),
    "weather": lambda x: str(x.get("naturezaCode") or "").startswith(("1", "3")) and not x.get("isFire"),
    "rescue": lambda x: bool(re.search(r"resgate|salvamento|busca|socorro", norm(x.get("natureza")))),
}
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def incidents() -> list[dict[str, Any]]:
    """Every active ANEPC occurrence in Portugal (all kinds, not only fires), cached for a minute."""
    global _mem
    if _mem and time.time() - _mem[0] < CACHE_S:
        return _mem[1]
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(KEY_HELP)
    r = http().get(FEED, params={"all": 1}, headers={"X-API-Key": key}, timeout=30)
    if r.status_code == 429:  # no retry loop: tell the user when to come back instead of hanging
        wait = int(r.headers.get("Retry-After", "0") or 0)
        raise RuntimeError(f"Fogos.pt is rate-limiting this connection; try again in about {max(1, wait // 60)} min.")
    if r.status_code in (401, 403):
        raise RuntimeError(f"Fogos.pt refused the API key (HTTP {r.status_code}). {KEY_HELP}")
    r.raise_for_status()
    d = r.json()
    if not isinstance(d, dict) or not d.get("success", True):
        raise RuntimeError("the incident feed returned an unexpected answer")
    rows = d.get("data") or []
    _mem = (time.time(), rows)
    return rows


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _COMPASS[int((deg + 22.5) // 45) % 8]


def _resolve(near: str) -> tuple[float, float, str] | None:
    """A postal code or municipality name -> (lat, lon, label); None if it is neither."""
    from . import geo

    q = (near or "").strip()
    if re.fullmatch(r"\d{4}(?:\s*-?\s*\d{3})?", q):
        r = geo.postal_code(q)
        c = r.get("centre_lat_lon") if isinstance(r, dict) else None
        if c:
            return float(c[0]), float(c[1]), f"{r.get('postal_code')} {r.get('locality') or ''}".strip()
        return None
    try:
        m = geo.municipality_code(q)
        d = geo._get(f"municipio/{m['name']}")
    except Exception:  # noqa: BLE001 - not a municipality, or GeoAPI down: fall back to matching names
        return None
    centre = (((d or {}).get("geojson") or {}).get("properties") or {}).get("centros", {}).get("centro")
    if not centre:
        return None
    return float(centre[1]), float(centre[0]), f"{m['name']} (municipality centre)"


def _row(x: dict[str, Any], now: float) -> dict[str, Any]:
    started = (x.get("dateTime") or {}).get("sec")
    return {
        "what": x.get("natureza"),
        "status": x.get("status"),
        "since": f"{x.get('date')} {x.get('hour')}",
        "minutes_ago": int((now - started) / 60) if started else None,
        "where": x.get("location"),
        "locality": x.get("localidade") or None,
        "resources": {"personnel": x.get("man", 0), "ground_vehicles": x.get("terrain", 0), "aircraft": x.get("aerial", 0)},
        "lat_lon": [x.get("lat"), x.get("lng")] if x.get("coords") else None,
        "map": f"https://www.google.com/maps?q={x.get('lat')},{x.get('lng')}" if x.get("coords") else None,
        "details": f"https://fogos.pt/fogo/{x.get('id')}" if x.get("id") else None,
        "id": x.get("id"),
    }


@mcp.tool()
def active_incidents(
    near: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 10,
    kind: str = "all",
    limit: int = 15,
) -> dict[str, Any]:
    """What emergency services (civil protection, ANEPC) are responding to right now in Portugal, closest first.

    Live occurrences of every kind: fires, road collisions, fallen trees, floods, rescues, with their
    status (e.g. "Em Curso"), time started and resources on scene. Updated every few minutes.
    Police-only activity (PSP/GNR) is not published anywhere and is not included.
    Args:
        near: a postal code ("4450-208") or municipality ("Matosinhos") to search around; any other
            text (a parish or village) filters by the incident's location name instead.
        lat, lon: search around these coordinates (best when the user shares their location).
        radius_km: search radius around the point (default 10, max 100).
        kind: "all", "fire", "road", "weather" (trees, floods, wind, landslides) or "rescue".
        limit: max incidents listed (counts always cover every match).
    With no place at all, returns the national picture (counts by kind and district, biggest first).
    """
    try:
        k = norm(kind or "all")
        if k != "all" and k not in KINDS:
            raise ValueError("kind is one of: all, fire, road, weather, rescue")
        rows = [x for x in incidents() if k == "all" or KINDS[k](x)]
        now = time.time()
        radius = max(0.5, min(float(radius_km or 10), 100.0))
        n = clamp(limit, 1, 50, 15)
        centre: tuple[float, float, str] | None = None
        if lat is not None and lon is not None:
            if not (29 <= lat <= 43 and -32 <= lon <= -6):
                raise ValueError("coordinates are outside Portugal (mainland, Madeira, Azores)")
            centre = (float(lat), float(lon), f"{lat:.5f}, {lon:.5f}")
        elif near:
            centre = _resolve(near)

        base = {
            "as_of": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="minutes"),
            "kind": k,
            "source": SOURCE,
            "note": "Civil protection occurrences only; police (PSP/GNR) activity is not public. In an emergency call 112.",
        }
        if centre:
            clat, clon, label = centre
            hits = []
            for x in rows:
                if not x.get("coords") or x.get("lat") is None:
                    continue
                dist = haversine_km(clat, clon, float(x["lat"]), float(x["lng"]))
                if dist <= radius:
                    r = _row(x, now)
                    r["distance_km"] = round(dist, 1)
                    r["direction"] = _bearing(clat, clon, float(x["lat"]), float(x["lng"]))
                    hits.append(r)
            hits.sort(key=lambda r: r["distance_km"])
            return {**base, "around": label, "radius_km": radius, "count": len(hits), "incidents": hits[:n]}

        if near:  # not a postal code or municipality: match the location text
            q = norm(near)
            hits = [_row(x, now) for x in rows if q in norm(f"{x.get('location')} {x.get('localidade')}")]
            hits.sort(key=lambda r: r["minutes_ago"] if r["minutes_ago"] is not None else 10**9)
            return {**base, "matching": near, "count": len(hits), "incidents": hits[:n]}

        by_kind: dict[str, int] = {}
        by_district: dict[str, int] = {}
        for x in rows:
            by_kind[x.get("natureza") or "?"] = by_kind.get(x.get("natureza") or "?", 0) + 1
            by_district[x.get("district") or "?"] = by_district.get(x.get("district") or "?", 0) + 1
        biggest = sorted(rows, key=lambda x: -(int(x.get("man") or 0)))[:n]
        return {
            **base,
            "count": len(rows),
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
            "by_district": dict(sorted(by_district.items(), key=lambda kv: -kv[1])),
            "biggest": [_row(x, now) for x in biggest],
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "active_incidents", exc)
