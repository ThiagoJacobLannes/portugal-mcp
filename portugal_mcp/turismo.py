"""Tourism: Turismo de Portugal open data (CC BY): the local-accommodation registry (RNAL),
tourist establishments (RNET), Blue Flag beaches, surf spots, golf, spas, villages, routes."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .app import mcp, note
from .common import clamp, fail, get_json, haversine_km, like_pattern, norm

log = logging.getLogger("portugal_mcp.turismo")

BASE = "https://geo.turismodeportugal.pt/server/rest/services/TDP"
SOURCE = "Turismo de Portugal open data (dadosabertos.turismodeportugal.pt, CC BY 4.0)"

# kind -> (layer path, description). Small layers are fetched whole and filtered here.
KINDS: dict[str, tuple[str, str]] = {
    "blue_flag_beaches": ("OpenData_PraiasBA/MapServer/30", "Beaches with the Blue Flag (Bandeira Azul) this season"),
    "surf_spots": ("OpenData_SpotsSurf/MapServer/3", "Main surf spots"),
    "golf": ("OpenData_Golfe/MapServer/13", "Golf courses"),
    "spas": ("OpenData_Termas/MapServer/27", "Thermal spas (termas)"),
    "unesco": ("OpenData_PatrimonioMundialUNESCO/MapServer/38", "UNESCO World Heritage sites"),
    "historic_villages": ("OpenData_AldeiasHistoricas/MapServer/1", "Historic villages (Aldeias Históricas)"),
    "schist_villages": ("OpenData_AldeiasXisto/MapServer/4", "Schist villages (Aldeias do Xisto)"),
    "wine_villages": ("OpenData_AldeiasVinhateiras/MapServer/10", "Wine villages of the Douro (Aldeias Vinhateiras)"),
    "mountain_villages": ("OpenData_AldeiasMontanha/MapServer/7", "Mountain villages (Aldeias de Montanha)"),
    "marinas": ("OpenData_MarinasPortosDocas/MapServer/1", "Marinas, recreational ports and docks"),
    "walking_routes": ("OpenData_GrandesRotasWalking/MapServer/1", "Long-distance walking routes (Grandes Rotas)"),
    "santiago_ways": ("OpenData_CaminhosSantiago/MapServer/44", "The Ways of Saint James (Caminhos de Santiago) in Portugal"),
    "hotels": ("OpenData_ETExistentes/MapServer/0", "Hotels, resorts, rural tourism and campsites (tourist establishments, RNET)"),
}
AL_LAYER = "OpenData_AL/MapServer/6"
BIG = {"hotels"}  # queried on the server, not fetched whole

DROP = {"OBJECTID", "Shape", "SHAPE", "Shape_Length", "Shape_Area", "Shape.STLength()", "Shape__Length", "Shape__Area",
        "LatLong", "Lat", "Lon", "Latitude", "Longitude", "FiabilidadeGeo", "DTMNFR", "NUTSII", "NUTSIII", "ERT",
        "GlobalID",
        # owner/operator names can be private individuals (rural tourism, small AL): left out on purpose
        "EntProprietaria", "EntExploradora", "EntExplora"}

note(
    "turismo",
    "Tourism tools use Turismo de Portugal's official registries (CC BY). local_accommodation checks "
    "the national register of local accommodation (Alojamento Local, RNAL): give the registration "
    "number ('12345/AL'), a name, an address or postal code, or a point. Being registered does not "
    "mean the listing online is honest; say the register shows what the owner declared. "
    "tourism_places lists Blue Flag beaches, surf spots, golf, spas, villages, routes, marinas, "
    "UNESCO sites and hotels near a place or in a municipality.",
)

_meta: dict[str, dict[str, Any]] = {}
_all: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_lock = threading.Lock()


def _layer_meta(path: str) -> dict[str, Any]:
    with _lock:
        if path in _meta:
            return _meta[path]
    m = get_json(f"{BASE}/{path}", {"f": "json"}, timeout=30)
    with _lock:
        _meta[path] = m
    return m


def _query(path: str, **params: Any) -> list[dict[str, Any]]:
    base = {"f": "json", "outFields": "*", "returnGeometry": "true", "outSR": "4326", "maxAllowableOffset": "0.0005"}
    d = get_json(f"{BASE}/{path}/query", {**base, **{k: str(v) for k, v in params.items()}}, timeout=60)
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(f"Turismo de Portugal map server: {d['error'].get('message')}")
    return d.get("features", [])


def _points(geom: dict[str, Any] | None) -> list[tuple[float, float]]:
    """All (lat, lon) vertices of a point, line or polygon (lines and polygons generalised)."""
    if not geom:
        return []
    if "x" in geom:
        return [(geom["y"], geom["x"])]
    parts = geom.get("rings") or geom.get("paths") or []
    return [(p[1], p[0]) for part in parts for p in part]


def _clean(feat: dict[str, Any], date_fields: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (feat.get("attributes") or {}).items():
        if k in DROP or v in (None, "", " "):
            continue
        if k in date_fields and isinstance(v, (int, float)):
            v = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=v)).date().isoformat()
        if isinstance(v, str):
            href = re.search(r'href="([^"]+)"', v)
            v = href.group(1) if href else v.strip()
        if k == "DICO":
            muni = _muni_names().get(str(v).zfill(4))
            if muni:
                out["Concelho"] = muni
            continue
        out[k] = v
    pts = _points(feat.get("geometry"))
    if pts:
        out["_pts"] = pts
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        out["lat"], out["lon"] = round(lat, 5), round(lon, 5)
    return out


_munis: dict[str, str] = {}


def _muni_names() -> dict[str, str]:
    """INE municipality code (DICO) -> name, for layers that only carry the code (Blue Flag beaches)."""
    if not _munis:
        try:
            for m in get_json("https://json.geoapi.pt/municipios/freguesias", timeout=30):
                _munis[str(m.get("codigoine") or "").zfill(4)] = m.get("nome")
        except Exception:  # noqa: BLE001 — names are a bonus
            log.warning("municipality names unavailable", exc_info=True)
    return _munis


def _dates(path: str) -> set[str]:
    return {f["name"] for f in _layer_meta(path).get("fields", []) if f.get("type") == "esriFieldTypeDate"}


_wild = like_pattern


def _near(rows: list[dict[str, Any]], lat: float, lon: float, radius_km: float) -> list[dict[str, Any]]:
    for r in rows:
        pts = r.get("_pts") or [(r.get("lat"), r.get("lon"))]
        r["distance_km"] = round(min(haversine_km(lat, lon, a, b) for a, b in pts if a is not None), 2)
    return sorted((r for r in rows if r["distance_km"] <= radius_km), key=lambda r: r["distance_km"])


def _finish(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    for r in rows:
        r.pop("_pts", None)
    return rows[:n]


def _small(kind: str) -> list[dict[str, Any]]:
    path = KINDS[kind][0]
    with _lock:
        hit = _all.get(kind)
        if hit and time.time() - hit[0] < 86400:
            return hit[1]
    dates = _dates(path)
    rows = [_clean(f, dates) for f in _query(path, where="1=1", resultRecordCount=5000)]
    with _lock:
        _all[kind] = (time.time(), rows)
    return rows


@mcp.tool()
def local_accommodation(
    registration: str | None = None,
    name: str | None = None,
    address: str | None = None,
    postal_code: str | None = None,
    municipality: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: int = 200,
    limit: int = 20,
) -> dict[str, Any]:
    """Look up the national register of local accommodation (Alojamento Local, RNAL).

    Answers "is this flat/guesthouse registered?" and "how many AL are on my street?".
    Args:
        registration: the RNAL number, e.g. "12345/AL" or "12345".
        name: part of the registered name, e.g. "InSuites".
        address: part of the street address, e.g. "Rua de Santa Catarina".
        postal_code: "1200-290", or "1200" for the whole area.
        municipality: e.g. "Lisboa", "Porto", "Albufeira".
        lat, lon: registered accommodation within radius_m metres (max 2000) of a point.
    """
    try:
        n = clamp(limit, 1, 100, 20)
        where: list[str] = []
        if registration:
            digits = re.sub(r"\D", "", registration.split("/")[0])
            if not digits:
                raise ValueError("registration is the RNAL number, e.g. '12345/AL'")
            where.append(f"NrRNAL = {int(digits)}")
        if name:
            where.append(f"UPPER(Denominacao) LIKE '{_wild(name)}'")
        if address:
            where.append(f"UPPER(Endereco) LIKE '{_wild(address)}'")
        if postal_code:
            m = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{3}))?\s*", postal_code)
            if not m:
                raise ValueError("postal_code is 'XXXX-XXX' or 'XXXX'")
            where.append(f"CodigoPostal = '{m.group(1)}-{m.group(2)}'" if m.group(2) else f"CodigoPostal LIKE '{m.group(1)}-%'")
        if municipality:
            where.append(f"UPPER(Concelho) LIKE '{_wild(municipality)}'")
        params: dict[str, Any] = {"where": " AND ".join(where) or "1=1", "resultRecordCount": n, "orderByFields": "NrRNAL"}
        if lat is not None and lon is not None:
            params.update(geometry=f"{lon},{lat}", geometryType="esriGeometryPoint", inSR=4326,
                          distance=max(10, min(int(radius_m), 2000)), units="esriSRUnit_Meter")
        elif not where:
            raise ValueError("give a registration number, name, address, postal code, municipality or lat/lon")
        count = get_json(f"{BASE}/{AL_LAYER}/query", {**{k: str(v) for k, v in params.items()}, "returnCountOnly": "true", "f": "json"}, timeout=60)
        dates = _dates(AL_LAYER)
        rows = [_clean(f, dates) for f in _query(AL_LAYER, **params)]
        if lat is not None and lon is not None:
            rows = _near(rows, lat, lon, 10)
        for r in rows:
            if "NrRNAL" in r:
                r["registration"] = f"{r.pop('NrRNAL')}/AL"
        out: dict[str, Any] = {"found": count.get("count", len(rows)) if isinstance(count, dict) else len(rows), "accommodation": _finish(rows, n)}
        if registration and not rows:
            out["note"] = (f"No accommodation with number {registration} in the register. Check the number; "
                           "an unregistered short-term rental can be reported to ASAE or the town hall.")
        out["note_register"] = "The register shows what the owner declared to the town hall; it does not vouch for any online listing."
        out["source"] = SOURCE
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "local_accommodation", exc)


@mcp.tool()
def tourism_places(
    kind: str,
    place: str | None = None,
    name: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 25,
    limit: int = 20,
) -> dict[str, Any]:
    """Official tourism places in Portugal, near a point or in a municipality/region.

    Args:
        kind: one of blue_flag_beaches, surf_spots, golf, spas, unesco, historic_villages,
            schist_villages, wine_villages, mountain_villages, marinas, walking_routes,
            santiago_ways, hotels.
        place: municipality, parish, district or region name, e.g. "Cascais", "Algarve", "Peniche".
        name: part of the name, e.g. "Nazaré", "Pestana".
        lat, lon: closest first, within radius_km (max 200).
    """
    try:
        key = norm(kind).replace(" ", "_").replace("-", "_")
        aliases = {"beaches": "blue_flag_beaches", "praias": "blue_flag_beaches", "surf": "surf_spots", "termas": "spas",
                   "hoteis": "hotels", "hotel": "hotels", "golfe": "golf", "aldeias_historicas": "historic_villages",
                   "caminhos_de_santiago": "santiago_ways", "trails": "walking_routes", "unesco_sites": "unesco"}
        key = aliases.get(key, key)
        if key not in KINDS:
            raise ValueError(f"kind is one of: {', '.join(KINDS)}")
        n = clamp(limit, 1, 100, 20)
        path, what = KINDS[key]
        if key in BIG:
            where = []
            if name:
                where.append(f"UPPER(Denominacao) LIKE '{_wild(name)}'")
            if place:
                w = _wild(place)
                where.append(f"(UPPER(Concelho) LIKE '{w}' OR UPPER(Freguesia) LIKE '{w}' OR UPPER(Distrito) LIKE '{w}' OR UPPER(NUTSII) LIKE '{w}')")
            params: dict[str, Any] = {"where": " AND ".join(where) or "1=1", "resultRecordCount": 200 if lat is not None else n}
            if lat is not None and lon is not None:
                params.update(geometry=f"{lon},{lat}", geometryType="esriGeometryPoint", inSR=4326,
                              distance=int(min(float(radius_km), 200) * 1000), units="esriSRUnit_Meter")
            elif not where:
                raise ValueError("for hotels give a place, a name or lat/lon")
            rows = [_clean(f, _dates(path)) for f in _query(path, **params)]
        else:
            rows = [dict(r) for r in _small(key)]
            if name:
                q = norm(name)
                rows = [r for r in rows if q in norm(r.get("Denominacao") or r.get("Denominação") or r.get("Denominaca") or "")]
            if place:
                q = norm(place)
                rows = [r for r in rows if any(q in norm(r.get(f) or "") for f in ("Concelho", "Freguesia", "Distrito", "Região", "Localidade", "LocalidadeCP", "Localidade_CP", "Denominacao", "Denominação", "Denominaca", "NUTSII", "NUTSIII"))]
        if lat is not None and lon is not None:
            rows = _near(rows, lat, lon, min(float(radius_km), 200))
        return {"kind": what, "found": len(rows), "places": _finish(rows, n), "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "tourism_places", exc)
