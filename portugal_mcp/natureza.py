"""Nature conservation (ICNF, Instituto da Conservação da Natureza e das Florestas): whether a place is
inside a protected area, a Natura 2000 site (SIC/ZEC habitats, ZPE birds), a Ramsar wetland or a
biosphere reserve, and the classified trees of public interest nearby."""

from __future__ import annotations

import logging
from typing import Any

from .app import mcp, note
from .common import clamp, fail, get_json, haversine_km

log = logging.getLogger("portugal_mcp.natureza")

WFS = "https://si.icnf.pt/geoserver/wfs"
SOURCE = "ICNF - Instituto da Conservação da Natureza e das Florestas, geographic services (si.icnf.pt)"
AREAS = [
    ("snimar:rnap_snimar", "protected_area", "National Network of Protected Areas (RNAP)"),
    ("BDG:sic", "natura2000_habitats", "Natura 2000, Site of Community Importance (SIC/ZEC, habitats)"),
    ("BDG:zpe", "natura2000_birds", "Natura 2000, Special Protection Area (ZPE, birds)"),
    ("BDG:ramsar_continente", "ramsar", "Ramsar wetland of international importance"),
    ("BDG:biosfera", "biosphere_reserve", "UNESCO biosphere reserve"),
]
SKIP = {"geom", "the_geom", "shape", "objectid", "fid", "gid", "id", "shape_leng", "shape_area", "perimetro", "perimeter"}

note(
    "natureza",
    "protected_areas says whether a place (mainland Portugal) is inside a protected area (natural "
    "park, reserve...), a Natura 2000 site, a Ramsar wetland or a biosphere reserve (ICNF), with the "
    "classifying act. notable_trees lists classified trees of public interest near a place or in a "
    "municipality, with species, size and age. Rules for building or activities there are set by "
    "each area's management plan: point to ICNF for those.",
)


def _point(near: str | None, lat: float | None, lon: float | None) -> tuple[float, float, str]:
    if lat is not None and lon is not None:
        return float(lat), float(lon), f"{float(lat):.5f}, {float(lon):.5f}"
    if near:
        from .ocorrencias import _resolve

        pt = _resolve(near)
        if pt:
            return pt
        raise ValueError(f"could not place {near!r}: give a postal code, a municipality, or lat/lon")
    raise ValueError("give a place (postal code or municipality) or lat/lon")


def _features(layer: str, cql: str, count: int = 10) -> list[dict[str, Any]]:
    data = get_json(WFS, {"service": "wfs", "version": "1.1.0", "request": "GetFeature", "typeName": layer,
                          "maxFeatures": count, "outputFormat": "application/json", "CQL_FILTER": cql})
    return [f.get("properties") or {} for f in (data or {}).get("features") or []]


def clean(props: dict[str, Any]) -> dict[str, Any]:
    """Drop geometry and housekeeping fields; round areas."""
    out = {}
    for k, v in props.items():
        if k.lower() in SKIP or v in (None, ""):
            continue
        if isinstance(v, float) and "area" in k.lower():
            v = round(v, 1)
        out[k] = v.strip() if isinstance(v, str) else v
    return out


@mcp.tool()
def protected_areas(near: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    """Is this place in a protected area or Natura 2000 site? (mainland Portugal, ICNF)

    Args:
        near: a postal code or municipality (its centre is used); for a precise answer pass lat/lon.
    Checks the National Network of Protected Areas (natural parks, reserves, protected landscapes),
    Natura 2000 (SIC/ZEC for habitats, ZPE for birds), Ramsar wetlands and biosphere reserves,
    and returns each designation that covers the point, with its classifying act and area.
    """
    try:
        la, lo, label = _point(near, lat, lon)
        cql = f"INTERSECTS(geom,SRID=4326;POINT({lo} {la}))"
        found: dict[str, Any] = {}
        failed = []
        for layer, key, what in AREAS:
            try:
                hits = [clean(p) for p in _features(layer, cql)]
            except Exception as exc:  # noqa: BLE001 - one layer down should not hide the others
                log.warning("ICNF layer %s failed: %s", layer, exc)
                failed.append(what)
                continue
            if hits:
                found[key] = {"what": what, "areas": hits}
        out: dict[str, Any] = {"place": label, "inside_any": bool(found), "designations": found}
        if near and lat is None:
            out["note"] = "Checked at the centre of that postal code or municipality; pass lat/lon for an exact point."
        if failed:
            out["not_checked"] = failed
        out["source"] = SOURCE
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "protected_areas", exc)


def shape_tree(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "species": p.get("nome_cient"), "common_name": p.get("nomevulgar"), "kind": p.get("tipo"),
        "place": ", ".join(str(x) for x in (p.get("lugar"), p.get("freguesia"), p.get("concelho")) if x),
        "height_m": p.get("altura_tot"), "trunk_circumference_m": p.get("circun1_30"), "crown_diameter_m": p.get("diammedcop"),
        "age_years": p.get("idade"), "process": p.get("processo"), "lat": p.get("latitude"), "lon": p.get("longitude"),
    }


@mcp.tool()
def notable_trees(near: str | None = None, lat: float | None = None, lon: float | None = None, municipality: str | None = None, radius_km: float = 5, limit: int = 15) -> dict[str, Any]:
    """Classified trees of public interest (arvoredo de interesse público, ICNF): species, size, age.

    Args:
        municipality: list a municipality's trees (e.g. "Sintra"), or
        near / lat / lon: the trees around a place, closest first, within radius_km (max 50).
    """
    try:
        n = clamp(limit, 1, 50, 15)
        if municipality:
            from . import geo

            try:
                name = geo.municipality_code(municipality)["name"]
            except ValueError:
                name = municipality
            safe = name.replace("'", "''")
            rows = [shape_tree(p) for p in _features("BDG:arv_class", f"concelho ILIKE '{safe}'", 500)]
            rows.sort(key=lambda r: -(r["age_years"] or 0))
            return {"municipality": name, "count": len(rows), "trees": rows[:n], "source": SOURCE}
        la, lo, label = _point(near, lat, lon)
        radius = max(0.2, min(float(radius_km or 5), 50.0))
        rows = []
        for p in _features("BDG:arv_class", f"DWITHIN(geom,SRID=4326;POINT({lo} {la}),{radius * 1000},meters)", 500):
            t = shape_tree(p)
            if t["lat"] is not None and t["lon"] is not None:
                t["distance_km"] = round(haversine_km(la, lo, float(t["lat"]), float(t["lon"])), 2)
                if t["distance_km"] <= radius:
                    rows.append(t)
        rows.sort(key=lambda r: r["distance_km"])
        return {"around": label, "radius_km": radius, "count": len(rows), "trees": rows[:n], "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "notable_trees", exc)

