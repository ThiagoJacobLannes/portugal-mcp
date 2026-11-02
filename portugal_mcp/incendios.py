"""Wildfires: burnt areas mapped by EFFIS (Copernicus, European Commission) for Portugal — read-only."""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import defusedxml.ElementTree as ET

from .app import mcp, note
from .common import clamp, fail, http, norm

log = logging.getLogger("portugal_mcp.incendios")

WFS = "https://maps.effis.emergency.copernicus.eu/effis"
SOURCE = "EFFIS - European Forest Fire Information System, Copernicus Emergency Management Service (CC BY 4.0)"
FIRST_YEAR = 2016

note(
    "incendios",
    "Wildfire tools use EFFIS satellite mapping of burnt areas (mapped a day or more after the "
    "fire; small fires can be missing). They are not a live list of fires in progress. For "
    "today's danger use fire_risk (IPMA); in an emergency call 112; for live incidents point to "
    "prociv.gov.pt.",
)

PERIODS = {"today": "today", "week": "week", "month": "month", "season": "season"}
FIELDS = ("FIREDATE", "LASTUPDATE", "PROVINCE", "COMMUNE", "AREA_HA", "BROADLEA", "CONIFER", "MIXED", "SCLEROPH", "AGRIAREAS", "ARTIFSURF")
NS = {"gml": "http://www.opengis.net/gml", "ms": "http://mapserver.gis.umn.edu/mapserver", "wfs": "http://www.opengis.net/wfs"}

_mem: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _layer(period: str | None, year: int | None) -> str:
    if year:
        y = int(year)
        if y == date.today().year:
            return "modis.ba.poly.season"
        if not FIRST_YEAR <= y < date.today().year:
            raise ValueError(f"year must be between {FIRST_YEAR} and {date.today().year}")
        return f"modis.ba.poly.{y}"
    p = PERIODS.get(norm(period or "season"))
    if not p:
        raise ValueError("period is one of: today, week, month, season (or pass a year)")
    return f"modis.ba.poly.{p}"


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def burnt_areas(layer: str) -> list[dict[str, Any]]:
    """Every EFFIS burnt-area polygon in Portugal for a layer, attributes only (no geometry)."""
    hit = _mem.get(layer)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    flt = "<Filter><PropertyIsEqualTo><PropertyName>COUNTRY</PropertyName><Literal>PT</Literal></PropertyIsEqualTo></Filter>"
    r = http().get(
        WFS,
        params={
            "service": "WFS", "version": "1.1.0", "request": "GetFeature", "typename": f"ms:{layer}",
            "propertyName": ",".join(FIELDS), "filter": flt,
        },
        headers={"Accept": "application/xml"},
        timeout=120,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    if root.tag.endswith("ExceptionReport"):
        raise RuntimeError("EFFIS rejected the request: " + " ".join(root.itertext()).strip()[:200])
    rows = []
    for member in root.iter("{http://www.opengis.net/gml}featureMember"):
        feat = next(iter(member), None)
        if feat is None:
            continue
        v = {f: (feat.findtext(f"ms:{f}", namespaces=NS) or "").strip() for f in FIELDS}
        land = {k: _num(v[k]) for k in ("BROADLEA", "CONIFER", "MIXED", "SCLEROPH", "AGRIAREAS", "ARTIFSURF")}
        rows.append({
            "date": v["FIREDATE"][:10] or None,
            "updated": v["LASTUPDATE"][:10] or None,
            "region": v["PROVINCE"] or None,
            "parish": v["COMMUNE"] or None,
            "area_ha": _num(v["AREA_HA"]) or 0.0,
            "land_cover_pct": {
                "forest": round(sum(x or 0 for x in (land["BROADLEA"], land["CONIFER"], land["MIXED"])), 1),
                "shrubland": round(land["SCLEROPH"] or 0, 1),
                "agriculture": round(land["AGRIAREAS"] or 0, 1),
                "urban": round(land["ARTIFSURF"] or 0, 1),
            },
        })
    _mem[layer] = (time.time(), rows)
    return rows


@mcp.tool()
def wildfires(
    period: str = "season",
    year: int | None = None,
    place: str | None = None,
    min_hectares: float = 0,
    sort: str = "area",
    limit: int = 15,
) -> dict[str, Any]:
    """Burnt areas from wildfires in Portugal, mapped by satellite (EFFIS / Copernicus).

    Not a live list of fires in progress: EFFIS maps burnt areas from satellite a day or more later,
    and small fires can be missing.
    Args:
        period: "today", "week", "month" or "season" (this year so far). Ignored when `year` is given.
        year: a past year from 2016, for the whole year.
        place: keep fires whose region or parish contains this, e.g. "Algarve", "Monchique", "Viseu".
        min_hectares: only fires at least this large.
        sort: "area" (largest first) or "date" (newest first).
        limit: max fires listed (totals always cover every match).
    """
    try:
        layer = _layer(period, year)
        rows = burnt_areas(layer)
        if place:
            q = norm(place)
            rows = [r for r in rows if q in norm(r["region"]) or q in norm(r["parish"])]
        rows = [r for r in rows if r["area_ha"] >= (min_hectares or 0)]
        by_region: dict[str, list[float]] = {}
        for r in rows:
            agg = by_region.setdefault(r["region"] or "?", [0, 0.0])
            agg[0] += 1
            agg[1] += r["area_ha"]
        if sort == "date":
            rows = sorted(rows, key=lambda r: r["date"] or "", reverse=True)
        else:
            rows = sorted(rows, key=lambda r: r["area_ha"], reverse=True)
        n = clamp(limit, 1, 100, 15)
        return {
            "layer": layer.removeprefix("modis.ba.poly."),
            "fires": len(rows),
            "burnt_hectares": round(sum(r["area_ha"] for r in rows)),
            "by_region": [
                {"region": k, "fires": c, "hectares": round(h)}
                for k, (c, h) in sorted(by_region.items(), key=lambda kv: -kv[1][1])[:12]
            ],
            "largest_or_latest": rows[:n],
            "note": "Satellite-mapped burnt areas (small fires can be missing), not live incidents. Today's danger: fire_risk. Emergency: 112.",
            "map": "https://forest-fire.emergency.copernicus.eu/apps/effis_current_situation/",
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "wildfires", exc)
