"""Lisbon city data (Câmara Municipal de Lisboa open data): the cultural agenda (AgendaLX, CC BY),
licensed temporary occupations of public space and street works (CC0), and recycling points."""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .app import mcp, note
from .common import clamp, fail, get_json, haversine_km, http, like_pattern, norm

log = logging.getLogger("portugal_mcp.lisboa")

AGENDA = "https://www.agendalx.pt/wp-json/agendalx/v1/events"
ARCGIS = "https://services.arcgis.com/1dSrzEWVQn5kHHyK/arcgis/rest/services"
SRC_AGENDA = "Agenda Cultural de Lisboa (agendalx.pt, Câmara Municipal de Lisboa, CC BY)"
SRC_CML = "Câmara Municipal de Lisboa open data (geodados-cml.hub.arcgis.com)"

RECYCLING = {  # layer id in Amb_Reciclagem -> what it takes
    2: "ecoponto (paper, plastic/metal, glass)",
    6: "vidrão (glass)",
    1: "papel e cartão (paper and cardboard)",
    4: "óleos alimentares usados (used cooking oil)",
    5: "REEE (small electrical and electronic equipment)",
    12: "resíduos alimentares (food waste drop-off)",
    13: "ecoilha (underground or surface recycling island)",
    14: "ecocentro (large items, rubble, garden waste)",
}

note(
    "lisboa",
    "Lisbon city tools: lisbon_events (the city's cultural agenda: exhibitions, concerts, theatre, "
    "guided visits, mostly with dates, venue and price), lisbon_street_works (licensed occupations "
    "of public space such as filming, events and scaffolding, plus the city's road and pavement "
    "works in progress or planned), lisbon_recycling (the nearest recycling points by type) and "
    "lisbon_metro_status (is each Metro line running normally right now). "
    "These cover the Lisboa municipality only.",
)

_events: tuple[float, list[dict[str, Any]]] | None = None
_lock = threading.Lock()


def _price(e: dict[str, Any]) -> str | None:
    cat = e.get("price_cat")
    raw = str(e.get("price_val") or "")
    vals = re.findall(r's:\d+:"(?:value|description)";s:\d+:"(.*?)";', raw)
    text = "; ".join(v for v in vals if v)
    if cat == "free" or (not text and cat in ("gratuito", "free")):
        return "free"
    if text and re.fullmatch(r"[\d.,]+", vals[0] if vals else ""):
        text = f"€{text}" if ";" not in text else f"€{vals[0]}; " + "; ".join(vals[1:])
    return text or (cat if cat and cat != "value" else None)


def _all_events() -> list[dict[str, Any]]:
    global _events
    with _lock:
        if _events and time.time() - _events[0] < 6 * 3600:
            return _events[1]
    out: list[dict[str, Any]] = []
    for page in range(1, 60):
        r = http().get(AGENDA, params={"per_page": 50, "page": page}, timeout=60)
        if r.status_code == 400:  # past the last page
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 50:
            break
    with _lock:
        _events = (time.time(), out)
    return out


def _clean_event(e: dict[str, Any]) -> dict[str, Any]:
    title = html.unescape(re.sub(r"<[^>]+>", "", (e.get("title") or {}).get("rendered") or ""))
    desc = e.get("description") or ""
    desc = desc[0] if isinstance(desc, list) and desc else desc
    venues = [v.get("name") for v in (e.get("venue") or {}).values() if isinstance(v, dict)]
    cats = [v.get("name") for v in (e.get("categories_name_list") or {}).values() if isinstance(v, dict)]
    out = {
        "title": title.strip(),
        "subtitle": html.unescape(e.get("subtitle") or "") or None,
        "when": e.get("string_dates") or None,
        "times": e.get("string_times") or None,
        "venue": ", ".join(html.unescape(v) for v in venues if v) or None,
        "category": ", ".join(c for c in cats if c) or e.get("subject") or None,
        "price": _price(e),
        "description": html.unescape(re.sub(r"<[^>]+>", "", str(desc))).strip()[:300] or None,
        "link": e.get("link"),
    }
    return {k: v for k, v in out.items() if v}


@mcp.tool()
def lisbon_events(
    day: str | None = None,
    until: str | None = None,
    query: str | None = None,
    category: str | None = None,
    free_only: bool = False,
    limit: int = 15,
) -> dict[str, Any]:
    """What's on in Lisbon: the city's official cultural agenda (exhibitions, concerts, theatre,
    dance, cinema, guided visits, kids, festivals).

    Args:
        day: ISO date, e.g. "2026-11-14". Default today.
        until: ISO date to look at a range from `day` (max 31 days).
        query: words in the title, venue or description, e.g. "fado", "Gulbenkian", "jazz".
        category: e.g. "música", "teatro", "exposições", "visitas guiadas", "crianças".
        free_only: only free events.
    """
    try:
        start = date.fromisoformat(day) if day else datetime.now(timezone.utc).date()
        end = date.fromisoformat(until) if until else start
        if end < start or (end - start).days > 31:
            raise ValueError("`until` must be on or after `day` and at most 31 days later")
        n = clamp(limit, 1, 50, 15)
        days = {(start + timedelta(d)).isoformat() for d in range((end - start).days + 1)}
        rows = []
        for e in _all_events():
            occ = set(e.get("occurences") or [])
            if not occ:
                s, l_ = e.get("StartDate") or "", e.get("LastDate") or ""
                if not (s and l_ and s <= end.isoformat() and l_ >= start.isoformat()):
                    continue
            elif not occ & days:
                continue
            ev = _clean_event(e)
            text = norm(" ".join(str(v) for v in ev.values()))
            if query and not all(w in text for w in norm(query).split()):
                continue
            if category and norm(category).rstrip("s") not in norm(ev.get("category", "")):
                continue
            if free_only and ev.get("price") != "free" and "gratuit" not in norm(ev.get("price", "")):
                continue
            rows.append(ev)
        return {"from": start.isoformat(), "to": end.isoformat(), "found": len(rows), "events": rows[:n],
                "note": "Check times on the event page before going.", "source": SRC_AGENDA}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "lisbon_events", exc)


def _arc(path: str, **params: Any) -> list[dict[str, Any]]:
    base = {"f": "json", "outFields": "*", "returnGeometry": "true", "outSR": "4326"}
    d = get_json(f"{ARCGIS}/{path}/query", {**base, **{k: str(v) for k, v in params.items()}}, timeout=60)
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(f"Lisbon map server: {d['error'].get('message')}")
    return d.get("features", [])


def _xy(geom: dict[str, Any] | None) -> tuple[float, float] | None:
    if not geom:
        return None
    if "x" in geom:
        return geom["y"], geom["x"]
    pts = [p for part in (geom.get("rings") or geom.get("paths") or []) for p in part]
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)) if pts else None


def _ms(v: Any) -> str | None:
    if isinstance(v, (int, float)):
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=v)).date().isoformat()
    return str(v) if v else None


def _spatial(lat: float | None, lon: float | None, radius_m: int) -> dict[str, Any]:
    if lat is None or lon is None:
        return {}
    return {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
            "distance": max(20, min(int(radius_m), 3000)), "units": "esriSRUnit_Meter"}


@mcp.tool()
def lisbon_street_works(
    parish: str | None = None,
    street: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: int = 400,
    day: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Street works and public-space occupations in Lisbon: what is (or will be) blocking a street.

    Two official sources: licensed temporary occupations of public space (filming, events,
    scaffolding, containers...) and the city's road and pavement works in progress or planned.
    Args:
        parish: freguesia, e.g. "Arroios", "Santa Maria Maior", "Belém".
        street: part of a street name (works only), e.g. "Avenida da República".
        lat, lon: within radius_m metres of a point (max 3000).
        day: ISO date the occupation must cover; default today.
    """
    try:
        n = clamp(limit, 1, 100, 20)
        d = (date.fromisoformat(day) if day else datetime.now(timezone.utc).date()).isoformat()
        if not (parish or street or (lat is not None and lon is not None)):
            raise ValueError("give a parish, a street or lat/lon")
        sp = _spatial(lat, lon, radius_m)
        where = [f"DATA_INICIO <= '{d}'", f"DATA_FIM >= '{d}'"]
        if parish:
            where.append(f"UPPER(FREGUESIA) LIKE '{like_pattern(parish)}'")
        occ = []
        if not street:
            for f in _arc("UCT_OcupacoesTemporariasEspacoPublico/FeatureServer/0", where=" AND ".join(where), resultRecordCount=200, **sp):
                a = f["attributes"]
                xy = _xy(f.get("geometry"))
                occ.append({"what": a.get("TIPO_EVENTO"), "name": a.get("NOME_EVENTO"), "parish": a.get("FREGUESIA"),
                            "from": a.get("DATA_INICIO"), "to": a.get("DATA_FIM"), "licence": a.get("ID_EVENTO"),
                            **({"lat": round(xy[0], 5), "lon": round(xy[1], 5)} if xy else {})})
        wwhere = ["ESTADO IN ('Em curso', 'Previsto')"]
        if parish:
            wwhere.append(f"UPPER(FREGUESIA) LIKE '{like_pattern(parish)}'")
        if street:
            wwhere.append(f"UPPER(NOME_ARRUAMENTO) LIKE '{like_pattern(street)}'")
        works = []
        for f in _arc("UCT_Interven%C3%A7%C3%B5es/FeatureServer/0", where=" AND ".join(wwhere), resultRecordCount=200, **sp):
            a = f["attributes"]
            xy = _xy(f.get("geometry"))
            works.append({"street": a.get("NOME_ARRUAMENTO"), "work": a.get("TIPO_INTERVENCAO"), "status": a.get("ESTADO"),
                          "parish": a.get("FREGUESIA"), "start": _ms(a.get("INICIO_OBRA")), "end": _ms(a.get("CONCLUSAO_OBRA")),
                          **({"lat": round(xy[0], 5), "lon": round(xy[1], 5)} if xy else {})})
        for rows in (occ, works):
            if lat is not None and lon is not None:
                for r in rows:
                    if "lat" in r:
                        r["distance_m"] = round(haversine_km(lat, lon, r["lat"], r["lon"]) * 1000)
                rows.sort(key=lambda r: r.get("distance_m", 1e9))
        return {
            "day": d,
            "public_space_occupations": occ[:n],
            "occupations_found": len(occ),
            "street_works": works[:n],
            "works_found": len(works),
            "note": "Occupations are what the city licensed; works are the city's own road and pavement interventions.",
            "source": SRC_CML + " (CC0 for occupations)",
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "lisbon_street_works", exc)


@mcp.tool()
def lisbon_recycling(
    lat: float,
    lon: float,
    kind: str = "ecoponto",
    radius_m: int = 500,
    limit: int = 5,
) -> dict[str, Any]:
    """The nearest recycling points in Lisbon of a given kind.

    Args:
        lat, lon: where you are.
        kind: "ecoponto" (paper, plastic, glass), "vidrao" (glass), "papel", "oleo" (used cooking oil),
            "reee" (small electronics), "alimentar" (food waste), "ecoilha", "ecocentro" (large items).
        radius_m: search radius in metres (max 3000).
    """
    try:
        keys = {"ecoponto": 2, "vidrao": 6, "vidro": 6, "glass": 6, "papel": 1, "paper": 1, "oleo": 4, "oil": 4,
                "reee": 5, "electronics": 5, "eletronicos": 5, "alimentar": 12, "food": 12, "ecoilha": 13, "ecocentro": 14}
        k = norm(kind).replace(" ", "")
        layer = next((v for key, v in keys.items() if key in k), None)
        if layer is None:
            raise ValueError("kind is one of: ecoponto, vidrao, papel, oleo, reee, alimentar, ecoilha, ecocentro")
        n = clamp(limit, 1, 30, 5)
        rows = []
        for f in _arc(f"Amb_Reciclagem/FeatureServer/{layer}", where="1=1", resultRecordCount=200, **_spatial(lat, lon, radius_m)):
            xy = _xy(f.get("geometry"))
            if not xy:
                continue
            a = f.get("attributes") or {}
            row = {"distance_m": round(haversine_km(lat, lon, xy[0], xy[1]) * 1000), "type": a.get("TPRS_DESC"),
                   "street": a.get("TOP_MOD_1"), "where": a.get("PRSL_LOCAL"), "parish": a.get("FRE_AB"),
                   "lat": round(xy[0], 6), "lon": round(xy[1], 6)}
            rows.append({k2: v for k2, v in row.items() if v not in (None, "", " ", "n.a.")})
        rows.sort(key=lambda r: r["distance_m"])
        return {"kind": RECYCLING[layer], "found": len(rows), "points": rows[:n], "source": SRC_CML}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "lisbon_recycling", exc)


METRO_STATUS = "https://app.metrolisboa.pt/status/getLinhas.php"
SRC_METRO = "Metropolitano de Lisboa, line status published on metrolisboa.pt (app.metrolisboa.pt)"
METRO_LINES = {"amarela": "Amarela (Yellow)", "azul": "Azul (Blue)", "verde": "Verde (Green)", "vermelha": "Vermelha (Red)"}


def shape_metro(data: dict[str, Any]) -> list[dict[str, Any]]:
    resp = data.get("resposta") or {}
    rows = []
    for key, name in METRO_LINES.items():
        text = re.sub(r"\s+", " ", html.unescape(str(resp.get(key) or ""))).strip()
        normal = norm(text) in ("ok", "")
        rows.append({"line": name, "normal_service": normal, "status": "Normal service" if normal else text})
    return rows


@mcp.tool()
def lisbon_metro_status() -> dict[str, Any]:
    """Is the Lisbon Metro running normally right now? Status of the four lines (Amarela, Azul,
    Verde, Vermelha) as Metropolitano de Lisboa publishes it, with the disruption text if any.

    For timetables use transit_departures with operator "metro".
    """
    try:
        data = http().get(METRO_STATUS, timeout=20).json()
        if str(data.get("codigo")) != "200":
            raise ValueError(f"the Metro status service answered code {data.get('codigo')}")
        lines = shape_metro(data)
        return {
            "as_of": datetime.now(ZoneInfo("Europe/Lisbon")).strftime("%Y-%m-%d %H:%M"),
            "all_normal": all(x["normal_service"] for x in lines),
            "lines": lines,
            "source": SRC_METRO,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "lisbon_metro_status", exc)
