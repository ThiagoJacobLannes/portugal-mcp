"""Porto city data: road and pavement restrictions (Câmara Municipal do Porto open data, planned
and unplanned, refreshed daily) and the city's live environmental sensors (air quality, noise,
weather) from the Porto Digital urban platform (FIWARE, CC0)."""

from __future__ import annotations

import csv
import logging
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .app import mcp, note
from .common import cache_dir, clamp, download, fail, get_json, haversine_km, http, norm

log = logging.getLogger("portugal_mcp.porto")

TZ = ZoneInfo("Europe/Lisbon")
CKAN = "https://dadosabertos.cm-porto.pt/api/3/action/package_show"
DATASETS = {"planned": "condicionamentos-de-transito", "unplanned": "condicionamentos-nao-programados"}
BROKER = "https://broker.fiware.urbanplatform.portodigital.pt/v2/entities"
SRC_CLOSURES = "Câmara Municipal do Porto open data (dadosabertos.cm-porto.pt), traffic restrictions, updated daily"
SRC_SENSORS = "Porto Digital urban platform sensors (dadosabertos.cm-porto.pt, CC0)"
TTL = 6 * 3600
FRESH_S = 3 * 3600
# Porto's centre, used when no place is given and to reject points outside the city.
PORTO = (41.1579, -8.6291)

note(
    "porto",
    "Porto city tools: porto_road_closures lists the streets where the Câmara do Porto has "
    "restricted traffic or parking (works, events, loading, scaffolding: planned) or registered an "
    "unplanned restriction (accidents, burst pipes, fallen trees), filtered to what is active on a "
    "date. porto_sensors gives the latest readings of the city's air-quality, noise and weather "
    "sensors. Both cover the Porto municipality only; give the date or reading time.",
)

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

_closures: tuple[float, list[dict[str, Any]]] | None = None
_lock = threading.Lock()


def _csv_url(dataset: str) -> str:
    res = get_json(CKAN, {"id": dataset})["result"]["resources"]
    return next(r["url"] for r in res if (r.get("format") or "").upper() == "CSV")


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ms_date(v: Any) -> date | None:
    """The unplanned file stores dates as epoch milliseconds at midnight UTC."""
    try:
        return datetime.fromtimestamp(int(float(v)) / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _ms_hhmm(v: Any) -> str | None:
    """...and times of day as milliseconds since midnight."""
    try:
        m = int(float(v)) // 60000
    except (TypeError, ValueError):
        return None
    return f"{m // 60:02d}:{m % 60:02d}" if 0 <= m < 24 * 60 else None


def _test_row(r: dict[str, Any]) -> bool:
    """The live files carry the odd test record from the city's own tooling."""
    return "teste" in norm(r.get("observacoes")) and "playwright" in norm(r.get("observacoes"))


def parse_planned(lines: Any, keep_from: date) -> list[dict[str, Any]]:
    out = []
    for r in csv.DictReader(lines, quotechar="'", skipinitialspace=True):
        start, end = r.get("data_de_inicio") or "", r.get("data_de_fim") or ""
        if not (start or end) or (end or start) < keep_from.isoformat() or _test_row(r):
            continue
        out.append({
            "kind": "planned",
            "street": (r.get("topo") or "").strip() or None,
            "stretch": (r.get("troco") or "").strip() or None,
            "restriction": r.get("tipo_de_condicionamento") or None,
            "reason": r.get("motivo") or None,
            "start": start or None,
            "end": end or None,
            "notes": (r.get("observacoes") or "").strip() or None,
            "lat": _f(r.get("latitude")),
            "lon": _f(r.get("longitude")),
        })
    return out


def parse_unplanned(lines: Any, keep_from: date) -> list[dict[str, Any]]:
    out = []
    for r in csv.DictReader(lines, quotechar="'", skipinitialspace=True):
        start = _ms_date(r.get("data_de_início") or r.get("data_de_inicio"))
        end = _ms_date(r.get("data_de_fim"))
        if not (start or end) or (end or start) < keep_from or _test_row(r):
            continue
        out.append({
            "kind": "unplanned",
            "street": (r.get("arruamento") or "").strip() or None,
            "stretch": (r.get("troco") or "").strip() or None,
            "restriction": r.get("tipo_de_condicionamento") or None,
            "reason": r.get("motivo") or None,
            "start": start.isoformat() if start else None,
            "start_time": _ms_hhmm(r.get("hora_de_inicio")),
            "end": end.isoformat() if end else None,
            "end_time": _ms_hhmm(r.get("hora_de_fim")),
            "notes": (r.get("observacoes") or "").strip() or None,
            "lat": _f(r.get("coordenada_y")),
            "lon": _f(r.get("coordenada_x")),
        })
    return out


def _all_closures() -> list[dict[str, Any]]:
    """Both files, downloaded at most every 6 hours, keeping rows that end today or later."""
    global _closures
    with _lock:
        if _closures and time.time() - _closures[0] < TTL:
            return _closures[1]
        keep_from = datetime.now(TZ).date() - timedelta(days=1)
        rows: list[dict[str, Any]] = []
        for kind, dataset in DATASETS.items():
            dest = cache_dir("porto") / f"{dataset}.csv"
            download(_csv_url(dataset), dest, timeout=300)
            with open(dest, encoding="utf-8-sig", newline="") as f:
                rows += (parse_planned if kind == "planned" else parse_unplanned)(f, keep_from)
        _closures = (time.time(), rows)
        return rows


def _point(near: str | None, lat: float | None, lon: float | None) -> tuple[float, float, str] | None:
    if lat is not None and lon is not None:
        return float(lat), float(lon), f"{float(lat):.5f}, {float(lon):.5f}"
    if near and re.fullmatch(r"\s*\d{4}(?:\s*-?\s*\d{3})?\s*", near):
        from .ocorrencias import _resolve

        return _resolve(near)
    return None


def active_on(rows: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    d = day.isoformat()
    return [r for r in rows if (r["start"] or d) <= d <= (r["end"] or r["start"] or d)]


@mcp.tool()
def porto_road_closures(
    near: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 0.5,
    day: str | None = None,
    kind: str = "all",
    limit: int = 25,
) -> dict[str, Any]:
    """Streets in Porto with traffic or parking restrictions active on a day (default today).

    Args:
        near: a street name ("Rua de Santa Catarina", matches part of it) or a Porto postal code
            ("4000-457"); or pass lat/lon.
        radius_km: around a postal code or point (default 0.5, max 5).
        day: "YYYY-MM-DD" (default today); planned restrictions are known weeks ahead.
        kind: "all", "planned" (works, events, loading, scaffolding) or "unplanned" (accidents,
            burst pipes, fallen trees...).
    With no place, returns a city summary by reason plus the restrictions on the main streets.
    """
    try:
        when = date.fromisoformat(day) if day else datetime.now(TZ).date()
        k = (kind or "all").lower()
        if k not in ("all", "planned", "unplanned"):
            raise ValueError("kind is 'all', 'planned' or 'unplanned'")
        rows = [r for r in active_on(_all_closures(), when) if k == "all" or r["kind"] == k]
        n = clamp(limit, 1, 100, 25)
        out: dict[str, Any] = {"day": when.isoformat(), "kind": k}
        pt = _point(near, lat, lon)
        if pt:
            radius = max(0.05, min(float(radius_km or 0.5), 5.0))
            if haversine_km(pt[0], pt[1], *PORTO) > 15:
                out["warning"] = "That place is outside Porto; this data covers the Porto municipality only."
            hits = []
            for r in rows:
                if r["lat"] is None or r["lon"] is None:
                    continue
                dist = haversine_km(pt[0], pt[1], r["lat"], r["lon"])
                if dist <= radius:
                    hits.append({**r, "distance_m": round(dist * 1000)})
            hits.sort(key=lambda r: r["distance_m"])
            out.update(around=pt[2], radius_km=radius, count=len(hits), restrictions=hits[:n])
        elif near:
            q = norm(near)
            hits = [r for r in rows if q in norm(r["street"])]
            hits.sort(key=lambda r: (r["street"] or "", r["start"] or ""))
            out.update(street=near, count=len(hits), restrictions=hits[:n])
        else:
            by_reason: dict[str, int] = {}
            for r in rows:
                by_reason[r["reason"] or "?"] = by_reason.get(r["reason"] or "?", 0) + 1
            unplanned = [r for r in rows if r["kind"] == "unplanned"]
            out.update(
                count=len(rows),
                by_reason=dict(sorted(by_reason.items(), key=lambda x: -x[1])),
                unplanned=unplanned[:n],
                note="Pass a street name or postal code to see the restrictions there.",
            )
        out["source"] = SRC_CLOSURES
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "porto_road_closures", exc)


SENSORS = {
    "air": ("AirQualityObserved", ["pm25", "pm10", "no2", "o3", "co", "so2"]),
    "noise": ("NoiseLevelObserved", ["LAeq", "LAmax", "LA90"]),
    "weather": ("WeatherObserved", ["temperature", "relativeHumidity", "precipitation", "windSpeed", "windDirection", "atmosphericPressure", "solarRadiation"]),
}
UNITS = {"pm25": "µg/m³", "pm10": "µg/m³", "no2": "µg/m³", "o3": "µg/m³", "co": "µg/m³", "so2": "µg/m³",
         "LAeq": "dB(A)", "LAmax": "dB(A)", "LA90": "dB(A)", "temperature": "°C", "relativeHumidity": "0-1",
         "precipitation": "mm", "windSpeed": "m/s", "windDirection": "°", "atmosphericPressure": "hPa", "solarRadiation": "W/m²"}


def _when(s: str | None) -> datetime | None:
    """FIWARE timestamps ('2026-11-13T22:15:00.00Z'); Python 3.10 rejects 2-digit fractions, so drop them."""
    text = re.sub(r"\.\d+", "", str(s or "")).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def shape_sensors(kind: str, entities: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], int]:
    """Fresh readings (last 3 hours) of one sensor type, and how many sensors were stale."""
    fields = SENSORS[kind][1]
    out, stale = [], 0
    for e in entities:
        seen = _when(e.get("dateObserved"))
        if not seen or (now - seen).total_seconds() > FRESH_S:
            stale += 1
            continue
        coords = (e.get("location") or {}).get("coordinates") or [None, None]
        out.append({
            "sensor": e.get("name") or e.get("id", "").rsplit(":", 1)[-1],
            "observed": seen.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
            "lat": coords[1],
            "lon": coords[0],
            "readings": {f: e[f] for f in fields if isinstance(e.get(f), (int, float))},
        })
    return out, stale


@mcp.tool()
def porto_sensors(kind: str = "all", near: str | None = None, lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    """Latest readings of Porto's street sensors: air quality, noise and weather.

    Args:
        kind: "air" (PM2.5, PM10, NO2, O3, CO in µg/m³), "noise" (LAeq in dB(A)), "weather"
            (temperature, humidity, rain, wind) or "all".
        near: a Porto postal code, or pass lat/lon, to sort sensors by distance.
    Only readings from the last 3 hours are returned; sensors that stopped reporting are counted.
    """
    try:
        kinds = list(SENSORS) if (kind or "all") == "all" else [kind]
        if any(k not in SENSORS for k in kinds):
            raise ValueError("kind is 'air', 'noise', 'weather' or 'all'")
        now = datetime.now(timezone.utc)
        pt = _point(near, lat, lon)
        out: dict[str, Any] = {"as_of": now.astimezone(TZ).strftime("%Y-%m-%d %H:%M")}
        if pt:
            out["around"] = pt[2]
        for k in kinds:
            r = http().get(BROKER, params={"type": SENSORS[k][0], "limit": 1000, "options": "keyValues"}, timeout=30)
            r.raise_for_status()
            rows, stale = shape_sensors(k, r.json(), now)
            if pt:
                for s in rows:
                    if s["lat"] is not None:
                        s["distance_km"] = round(haversine_km(pt[0], pt[1], s["lat"], s["lon"]), 2)
                rows.sort(key=lambda s: s.get("distance_km", 1e9))
            out[k] = {"sensors": rows, "not_reporting": stale}
        out["units"] = {f: UNITS[f] for k in kinds for f in SENSORS[k][1]}
        out["source"] = SRC_SENSORS
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "porto_sensors", exc)
