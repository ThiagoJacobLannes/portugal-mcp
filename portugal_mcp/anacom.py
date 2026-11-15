"""Communications (ANACOM, the national regulator, geo.anacom.pt): which fixed broadband technologies
reach a place (fibre, cable, copper), and the CTT post offices, postal agents and post boxes nearby
with their opening hours, services and last collection times."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from .app import mcp, note
from .common import clamp, fail, get_json, haversine_km, norm

log = logging.getLogger("portugal_mcp.anacom")

ARCGIS = "https://geo.anacom.pt/server/rest/services/publico"
COVERAGE = f"{ARCGIS}/CoberturaQoS_Pub/MapServer"
POSTAL = f"{ARCGIS}/ServicosPostais_Pub/MapServer"
SOURCE = "ANACOM - Autoridade Nacional de Comunicações (geo.anacom.pt). Fonte: ANACOM"
POSTAL_LAYERS = {"stations": (6, "post office (CTT station)"), "agents": (9, "postal agent (posto de correios)"), "boxes": (4, "post box (marco or caixa)")}

note(
    "anacom",
    "broadband_coverage says which fixed technologies (fibre, cable DOCSIS 3.1/3.0, copper with ADSL) "
    "reach a place according to ANACOM, with the date of the data; it does not say which operator. "
    "Mobile coverage per operator is published by ANACOM only as a map image (geo.anacom.pt). "
    "post_offices lists CTT post offices, postal agents and post boxes near a place with hours, "
    "services and the last collection time.",
)

_layers: tuple[float, list[tuple[int, str]]] | None = None
_lock = threading.Lock()


def _point(near: str | None, lat: float | None, lon: float | None) -> tuple[float, float, str]:
    if lat is not None and lon is not None:
        return float(lat), float(lon), f"{float(lat):.5f}, {float(lon):.5f}"
    if near:
        from .ocorrencias import _resolve

        pt = _resolve(near)
        if pt:
            return pt
        raise ValueError(f"could not place {near!r}: give a postal code ('1100-148'), a municipality, or lat/lon")
    raise ValueError("give a place (postal code or municipality) or lat/lon")


def fixed_layers(service: dict[str, Any]) -> list[tuple[int, str]]:
    """The fixed-network technology layers (children of 'Rede Fixa') in ANACOM's coverage service."""
    layers = service.get("layers") or []
    parent = next((lyr["id"] for lyr in layers if norm(lyr["name"]) == "rede fixa"), None)
    return [(lyr["id"], lyr["name"]) for lyr in layers if parent is not None and lyr.get("parentLayerId") == parent]


def _fixed() -> list[tuple[int, str]]:
    global _layers
    with _lock:
        if _layers and time.time() - _layers[0] < 86400:
            return _layers[1]
    found = fixed_layers(get_json(COVERAGE, {"f": "json"}))
    if not found:
        raise ValueError("ANACOM's coverage service has no fixed-network layers")
    with _lock:
        _layers = (time.time(), found)
    return found


def _query(url: str, params: dict[str, Any]) -> dict[str, Any]:
    data = get_json(url, {"f": "json", "returnGeometry": "false", "inSR": 4326, "geometryType": "esriGeometryPoint", "spatialRel": "esriSpatialRelIntersects", **params})
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(f"ANACOM: {data['error'].get('message')}")
    return data


@mcp.tool()
def broadband_coverage(near: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    """Which fixed broadband technologies reach a place in Portugal, according to ANACOM: fibre,
    coaxial cable (DOCSIS 3.1 / 3.0) and copper with ADSL, with the date of the data.

    Args:
        near: a postal code ("1100-148"; the full 7 digits is the most precise) or a municipality;
            or pass lat/lon for an exact point.
    It does not say which operator offers it, and mobile coverage is not included (ANACOM
    publishes that per operator only as a map at geo.anacom.pt).
    """
    try:
        la, lo, label = _point(near, lat, lon)
        techs = []
        as_of = None
        for lid, name in _fixed():
            data = _query(f"{COVERAGE}/{lid}/query", {"geometry": f"{lo},{la}", "outFields": "tecnologia,data"})
            feats = data.get("features") or []
            techs.append({"technology": name, "available": bool(feats)})
            for f in feats:
                as_of = as_of or (f.get("attributes") or {}).get("data")
        return {
            "place": label,
            "fixed_broadband": techs,
            "fibre": next((t["available"] for t in techs if "fibra" in norm(t["technology"])), None),
            "data_as_of": as_of,
            "note": "Coverage of the area, not a promise for a specific building; check with operators.",
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "broadband_coverage", exc)


def _hhmm(v: Any) -> str | None:
    m = re.match(r"(\d{1,2}):(\d{2})", str(v or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def shape_station(a: dict[str, Any]) -> dict[str, Any]:
    def span(prefix: str) -> str | None:
        ab, fx = _hhmm(a.get(f"{prefix}_ab")), _hhmm(a.get(f"{prefix}_fx"))
        if not ab or not fx:
            return None
        li, lf = _hhmm(a.get(f"{prefix}_alm_ini")), _hhmm(a.get(f"{prefix}_alm_fim"))
        return f"{ab}-{li}, {lf}-{fx}" if li and lf else f"{ab}-{fx}"

    services = [k.replace("_", " ") for k, v in a.items() if v == "Sim"]
    return {
        "name": a.get("designacao"), "address": a.get("morada"), "postal_code": a.get("cod_postal"),
        "hours": {"weekdays": span("dias_uteis") or "closed", "saturday": span("sabado") or "closed", "sunday_holidays": span("domingo_feriado") or "closed"},
        "notes": a.get("observacoes_horario"), "services": services,
    }


# Postal agents: up to three opening periods a day, numbered field suffixes per day (ANACOM aliases).
AGENT_DAYS = {"monday": ("", "_1", "_2"), "tuesday": ("_3", "_4", "_5"), "wednesday": ("_6", "_7", "_8"), "thursday": ("_9", "_10", "_11"),
              "friday": ("_12", "_13", "_14"), "saturday": ("_15", "_16", "_17"), "sunday": ("_18", "_19", "_20"), "holidays": ("_21", "_22", "_23")}


def shape_agent(a: dict[str, Any]) -> dict[str, Any]:
    hours = {}
    for day, suffixes in AGENT_DAYS.items():
        spans = [f"{_hhmm(a.get('abertura' + s))}-{_hhmm(a.get('fecho' + s))}" for s in suffixes if _hhmm(a.get("abertura" + s)) and _hhmm(a.get("fecho" + s))]
        hours[day] = ", ".join(spans) if spans else "closed"
    if all(v == "closed" for v in hours.values()):
        hours = None
    return {"name": a.get("designacao"), "address": a.get("morada"), "postal_code": a.get("cod_postal"), "hours": hours}


def shape_box(a: dict[str, Any]) -> dict[str, Any]:
    street = " ".join(str(x) for x in (a.get("arteria"), a.get("n_policia_porta")) if x)
    return {
        "type": a.get("tipo_marco_caixa"), "address": street or a.get("info_auxiliar_loc"), "locality": a.get("localidade"),
        "last_collection": {"normal_mail": _hhmm(a.get("ultima_recolha_cn")), "priority_mail": _hhmm(a.get("ultima_recolha_cp"))},
        "restrictions": a.get("restricoes"),
    }


@mcp.tool()
def post_offices(near: str | None = None, lat: float | None = None, lon: float | None = None, kind: str = "all", radius_km: float = 1.0, limit: int = 8) -> dict[str, Any]:
    """CTT post offices, postal agents and post boxes near a place, closest first (ANACOM register).

    Args:
        near: a postal code or municipality; or pass lat/lon.
        kind: "stations" (post offices, with hours and services), "agents" (postos de correios in
            shops and parish councils), "boxes" (post boxes, with last collection times) or "all".
        radius_km: search radius (default 1, max 10).
    """
    try:
        la, lo, label = _point(near, lat, lon)
        k = (kind or "all").lower()
        kinds = list(POSTAL_LAYERS) if k == "all" else [k]
        if any(x not in POSTAL_LAYERS for x in kinds):
            raise ValueError("kind is 'stations', 'agents', 'boxes' or 'all'")
        radius = max(0.1, min(float(radius_km or 1), 10.0))
        n = clamp(limit, 1, 30, 8)
        out: dict[str, Any] = {"around": label, "radius_km": radius}
        for kk in kinds:
            lid, what = POSTAL_LAYERS[kk]
            data = _query(f"{POSTAL}/{lid}/query", {"geometry": f"{lo},{la}", "distance": int(radius * 1000), "units": "esriSRUnit_Meter",
                                                     "outFields": "*", "returnGeometry": "true", "outSR": 4326, "resultRecordCount": 200})
            rows = []
            for f in data.get("features") or []:
                a, g = f.get("attributes") or {}, f.get("geometry") or {}
                row = (shape_station if kk == "stations" else shape_agent if kk == "agents" else shape_box)(a)
                if g.get("x") is not None:
                    row["distance_m"] = round(haversine_km(la, lo, g["y"], g["x"]) * 1000)
                rows.append(row)
            rows.sort(key=lambda r: r.get("distance_m", 1e9))
            out[kk] = {"what": what, "count": len(rows), "nearest": rows[:n]}
        out["source"] = SOURCE
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "post_offices", exc)
