"""Weather: IPMA open data (forecasts, warnings, UV, fire risk, sea, earthquakes) — read-only."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from .app import mcp, note
from .common import clamp, fail, get_json, haversine_km, norm

log = logging.getLogger("portugal_mcp.clima")

API = "https://api.ipma.pt/open-data"
SOURCE = "IPMA - Instituto Português do Mar e da Atmosfera (api.ipma.pt)"

note(
    "clima",
    "Weather tools use IPMA, the official met office. Forecasts cover the 5 next days for district "
    "capitals and island towns; for other places pass lat/lon and the nearest IPMA location is used "
    "(say which one). Warnings: yellow/orange/red only; green means none.",
)

_mem: dict[str, tuple[float, Any]] = {}


def _get(path: str, ttl: float = 900) -> Any:
    hit = _mem.get(path)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    data = get_json(f"{API}/{path}")
    _mem[path] = (time.time(), data)
    return data


def _locations() -> list[dict[str, Any]]:
    return _get("distrits-islands.json", ttl=7 * 86400)["data"]


def _classes(path: str, key: str) -> dict[int, dict[str, str]]:
    rows = _get(path, ttl=7 * 86400)["data"]
    return {int(r[key]): {"pt": r.get("descWeatherTypePT") or r.get("descClassWindSpeedDailyPT") or "",
                          "en": r.get("descWeatherTypeEN") or r.get("descClassWindSpeedDailyEN") or ""}
            for r in rows if str(r.get(key, "")).lstrip("-").isdigit()}


# idAreaAviso codes that are not a district-capital name in distrits-islands.json
WARNING_AREAS = {
    "ACE": "Açores - Grupo Central", "AOC": "Açores - Grupo Ocidental", "AOR": "Açores - Grupo Oriental",
    "MCN": "Madeira - Costa Norte", "MCS": "Madeira - Costa Sul", "MRM": "Madeira - Regiões Montanhosas",
    "MPS": "Porto Santo",
}

LEVELS = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
FIRE_RISK = {1: "reduzido (low)", 2: "moderado (moderate)", 3: "elevado (high)", 4: "muito elevado (very high)", 5: "máximo (maximum)"}


def _area_names() -> dict[str, str]:
    names = dict(WARNING_AREAS)
    for loc in _locations():
        names.setdefault(loc["idAreaAviso"], loc["local"])
    return names


def _resolve(place: str | None, lat: float | None, lon: float | None) -> dict[str, Any]:
    locs = _locations()
    if lat is not None and lon is not None:
        best = min(locs, key=lambda loc_: haversine_km(lat, lon, float(loc_["latitude"]), float(loc_["longitude"])))
        return {**best, "distance_km": round(haversine_km(lat, lon, float(best["latitude"]), float(best["longitude"])), 1)}
    if not place:
        raise ValueError("give a place name (district capital or island town) or lat/lon")
    q = norm(place)
    exact = [loc_ for loc_ in locs if norm(loc_["local"]) == q]
    part = exact or [loc_ for loc_ in locs if q in norm(loc_["local"]) or norm(loc_["local"]) in q]
    if not part:
        raise ValueError(
            f"no IPMA forecast location matches {place!r}. Forecasts exist for district capitals and island "
            "towns; pass lat/lon to use the nearest one. Known: " + ", ".join(sorted(loc_["local"] for loc_ in locs))
        )
    return part[0]


@mcp.tool()
def weather_forecast(place: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    """5-day weather forecast for a place in Portugal (IPMA, official).

    Args:
        place: district capital or island town, e.g. "Lisboa", "Porto", "Faro", "Funchal", "Ponta Delgada".
        lat, lon: use instead of place for anywhere else; the nearest IPMA forecast location is used.
    Returns daily min/max temperature, rain probability, weather type, wind, and today's UV index.
    """
    try:
        loc = _resolve(place, lat, lon)
        gid = loc["globalIdLocal"]
        fc = _get(f"forecast/meteorology/cities/daily/{gid}.json")
        wt = _classes("weather-type-classe.json", "idWeatherType")
        wind = _classes("wind-speed-daily-classe.json", "classWindSpeed")
        days = [
            {
                "date": d["forecastDate"],
                "t_min_c": float(d["tMin"]),
                "t_max_c": float(d["tMax"]),
                "rain_probability_pct": float(d["precipitaProb"]),
                "weather": wt.get(int(d["idWeatherType"]), {}).get("en"),
                "weather_pt": wt.get(int(d["idWeatherType"]), {}).get("pt"),
                "wind": f"{wind.get(int(d['classWindSpeed']), {}).get('en', '')} from {d['predWindDir']}".strip(),
            }
            for d in fc["data"]
        ]
        uv = None
        try:
            uv_rows = [u for u in _get("forecast/meteorology/uv/uv.json") if u["globalIdLocal"] == gid]
            if uv_rows:
                u = min(uv_rows, key=lambda r: r["data"])
                uv = {"date": u["data"], "uv_index": float(u["iUv"])}
        except Exception:  # noqa: BLE001 — UV is a bonus
            log.debug("uv lookup failed", exc_info=True)
        out: dict[str, Any] = {
            "location": loc["local"],
            "forecast_updated": fc.get("dataUpdate"),
            "days": days,
            "uv_today": uv,
            "source": SOURCE,
        }
        if "distance_km" in loc:
            out["note"] = f"Nearest IPMA forecast location is {loc['local']}, {loc['distance_km']} km away."
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "weather_forecast", exc)


@mcp.tool()
def weather_warnings(area: str | None = None, include_green: bool = False) -> dict[str, Any]:
    """Active IPMA weather warnings (rain, wind, heat, cold, snow, fog, sea, thunderstorms).

    Args:
        area: optional district or island group, e.g. "Lisboa", "Madeira", "Açores". Omit for the whole country.
        include_green: also list green (no-warning) entries. Default false.
    """
    try:
        names = _area_names()
        now = datetime.now()
        rows = []
        for w in _get("forecast/warnings/warnings_www.json", ttl=600):
            lvl = w.get("awarenessLevelID")
            if lvl == "green" and not include_green:
                continue
            try:
                if datetime.fromisoformat(w["endTime"]) < now:
                    continue
            except (KeyError, ValueError):
                pass
            region = names.get(w["idAreaAviso"], w["idAreaAviso"])
            if area and norm(area) not in norm(region):
                continue
            rows.append({
                "area": region,
                "type": w.get("awarenessTypeName"),
                "level": lvl,
                "from": w.get("startTime"),
                "until": w.get("endTime"),
                "text": (w.get("text") or "").strip() or None,
            })
        rows.sort(key=lambda r: (-LEVELS.get(r["level"], 0), r["area"], r["from"] or ""))
        return {
            "count": len(rows),
            "warnings": rows,
            "note": "No active yellow/orange/red warnings." if not rows else None,
            "levels": "yellow < orange < red",
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "weather_warnings", exc)


@mcp.tool()
def fire_risk(municipality: str, day: int = 0) -> dict[str, Any]:
    """Wildfire danger (RCM, risco de incêndio rural) for a municipality, today or tomorrow.

    Args:
        municipality: concelho name, e.g. "Sintra", "Monchique".
        day: 0 = today, 1 = tomorrow.
    Scale: 1 reduzido, 2 moderado, 3 elevado, 4 muito elevado, 5 máximo.
    """
    try:
        from .geo import municipality_code

        code = municipality_code(municipality)
        d = clamp(day, 0, 1, 0)
        doc = _get(f"forecast/meteorology/rcm/rcm-d{d}.json")
        hit = doc["local"].get(code["dico"])
        if not hit:
            return {"error": f"IPMA has no fire-risk value for {code['name']} (code {code['dico']})"}
        level = int(hit["data"]["rcm"])
        return {
            "municipality": code["name"],
            "date": doc.get("dataPrev"),
            "risk_level": level,
            "risk": FIRE_RISK.get(level),
            "advice_url": "https://www.ipma.pt/pt/riscoincendio/rcm.pt/",
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "fire_risk", exc)


@mcp.tool()
def sea_forecast(place: str | None = None, lat: float | None = None, lon: float | None = None, day: int = 0) -> dict[str, Any]:
    """Coastal sea forecast: wave height and period, wave direction, sea temperature (IPMA).

    Args:
        place: coastal spot, e.g. "Nazaré", "Peniche", "Faro", "Funchal". Or pass lat/lon for the nearest one.
        day: 0 = today, 1 = tomorrow, 2 = the day after.
    """
    try:
        locs = _get("sea-locations.json", ttl=7 * 86400)
        if lat is not None and lon is not None:
            loc = min(locs, key=lambda loc_: haversine_km(lat, lon, float(loc_["latitude"]), float(loc_["longitude"])))
        else:
            q = norm(place)
            hits = [loc_ for loc_ in locs if q and q in norm(loc_["local"])]
            if not hits:
                raise ValueError(f"no IPMA sea location matches {place!r}; pass lat/lon to use the nearest one. Known: " + ", ".join(sorted(loc_["local"] for loc_ in locs)))
            loc = hits[0]
        d = clamp(day, 0, 2, 0)
        doc = _get(f"forecast/oceanography/daily/hp-daily-sea-forecast-day{d}.json")
        row = next((r for r in doc["data"] if r["globalIdLocal"] == loc["globalIdLocal"]), None)
        if not row:
            return {"error": f"no sea forecast for {loc['local']} on day {d}"}
        return {
            "location": loc["local"],
            "date": doc.get("forecastDate"),
            "wave_height_m": [float(row["waveHighMin"]), float(row["waveHighMax"])],
            "wave_period_s": [float(row["wavePeriodMin"]), float(row["wavePeriodMax"])],
            "wave_direction": row.get("predWaveDir"),
            "total_sea_m": [row.get("totalSeaMin"), row.get("totalSeaMax")],
            "sea_temperature_c": [float(row["sstMin"]), float(row["sstMax"])],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "sea_forecast", exc)


@mcp.tool()
def earthquakes(region: str = "mainland", days: int = 7, min_magnitude: float = 0.0, limit: int = 20) -> dict[str, Any]:
    """Recent earthquakes recorded by IPMA.

    Args:
        region: "mainland" (mainland Portugal and Madeira) or "azores".
        days: look back this many days (max 30).
        min_magnitude: only quakes at or above this magnitude.
        limit: max rows (newest first).
    """
    try:
        area = 3 if norm(region).startswith(("azor", "acor")) else 7
        doc = _get(f"observation/seismic/{area}.json", ttl=600)
        since = datetime.now() - timedelta(days=clamp(days, 1, 30, 7))
        rows = []
        for q in doc["data"]:
            try:
                mag = float(q["magnitud"])
                t = datetime.fromisoformat(q["time"])
            except (TypeError, ValueError, KeyError):
                continue
            if mag < 0 or t < since or mag < min_magnitude:  # -99 = not computed yet
                continue
            rows.append({
                "time": q["time"],
                "magnitude": mag,
                "magnitude_type": q.get("magType"),
                "region": (q.get("obsRegion") or "").strip(),
                "depth_km": q.get("depth"),
                "lat": float(q["lat"]),
                "lon": float(q["lon"]),
                "felt": bool(q.get("sensed")),
                "intensity": q.get("degree"),
            })
        rows.sort(key=lambda r: r["time"], reverse=True)
        n = clamp(limit, 1, 100, 20)
        return {
            "region": "Azores" if area == 3 else "mainland Portugal and Madeira",
            "count": len(rows),
            "largest": max(rows, key=lambda r: r["magnitude"]) if rows else None,
            "earthquakes": rows[:n],
            "updated": doc.get("updateDate"),
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "earthquakes", exc)
