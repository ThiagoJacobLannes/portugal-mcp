"""Buses: Carris Metropolitana (Greater Lisbon) real-time open API — read-only."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from ..app import mcp
from .api import (
    BASE_URL,
    CarrisClient,
    haversine_m,
    line_of,
    lisbon_time,
    normalize,
    now_lisbon,
    strip_prefix,
)

log = logging.getLogger("portugal_mcp.transportes")


_client: CarrisClient | None = None


def client() -> CarrisClient:
    global _client
    if _client is None:
        _client = CarrisClient(os.environ.get("CARRIS_METROPOLITANA_BASE_URL", BASE_URL))
    return _client


def _stop_id(value: str | int) -> str:
    """Stop ids are 6-digit strings ("060361"); accept "60361" or 60361 too."""
    s = str(value).strip()
    return s.zfill(6) if s.isdigit() else s


def _line_summary(ln: dict[str, Any]) -> dict[str, Any]:
    c = client()
    return {
        "line_id": ln["id"],
        "name": ln.get("long_name"),
        "municipalities": [m for m in (c.municipality_name(i) for i in ln.get("municipality_ids") or []) if m],
        "color": ln.get("color"),
    }


def _translation(block: dict[str, Any] | None, lang: str) -> str | None:
    items = (block or {}).get("translation") or []
    for t in items:
        if t.get("language") == lang:
            return t.get("text")
    return items[0].get("text") if items else None


@mcp.tool(name="bus_find_stops")
def find_stops(
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Find Carris Metropolitana bus stops by name and/or near a coordinate.

    Use this first whenever the user names a place, street, station or gives a location,
    to get the stop_id needed by bus_next_arrivals. Results include the lines serving each stop.

    Args:
        query:    Text to match in the stop name (accent/case-insensitive, all words must match),
                  e.g. "Gare do Oriente", "Cacilhas terminal". A 6-digit stop id also works.
        lat, lon: A coordinate (WGS84). Results are sorted by straight-line distance from it.
                  Note: buses inside Lisbon city are mostly run by Carris (a different
                  company, not covered here); Carris Metropolitana serves the wider region.
        radius_m: Only stops within this many metres of lat/lon. Default 500 when no query is
                  given; unlimited when a query is given.
        limit:    Max stops to return (default 10, max 50).
    """
    try:
        c = client()
        stops = c.stops()
        limit = max(1, min(int(limit), 50))
        have_coord = lat is not None and lon is not None
        if not query and not have_coord:
            return {"error": "give a query and/or lat+lon"}

        candidates = [s for s in stops.values() if s.get("line_ids")]  # skip stops no line serves
        if query:
            q = query.strip()
            if _stop_id(q) in stops:
                candidates = [stops[_stop_id(q)]]
            else:
                words = normalize(q).split()
                candidates = [s for s in candidates if all(w in s["_norm"] for w in words)]

        rows = []
        for s in candidates:
            dist = haversine_m(lat, lon, s["lat"], s["lon"]) if have_coord else None
            rows.append((dist, s))
        if have_coord:
            radius = radius_m if radius_m is not None else (None if query else 500)
            if radius is not None:
                rows = [r for r in rows if r[0] <= radius]
            rows.sort(key=lambda r: r[0])
        else:
            rows.sort(key=lambda r: r[1].get("long_name") or "")

        lines = c.lines()
        out = []
        for dist, s in rows[:limit]:
            out.append(
                {
                    "stop_id": s["id"],
                    "name": s.get("long_name"),
                    "locality": c.locality_name(s.get("locality_id")),
                    "municipality": c.municipality_name(s.get("municipality_id")),
                    "distance_m": round(dist) if dist is not None else None,
                    "lat": s.get("lat"),
                    "lon": s.get("lon"),
                    "lines": [
                        {"line_id": lid, "name": (lines.get(lid) or {}).get("long_name")}
                        for lid in s.get("line_ids") or []
                    ],
                    "wheelchair_boarding": s.get("wheelchair_boarding"),
                }
            )
        return {"total_matches": len(rows), "count": len(out), "stops": out}
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        log.exception("find_stops failed")
        return {"error": str(exc)}


@mcp.tool(name="bus_next_arrivals")
def next_arrivals(stop_id: str, line_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Upcoming buses at a Carris Metropolitana stop, real-time where available.

    Use for "when is the next bus at X?" (get stop_id from bus_find_stops first).
    Each arrival says whether it is "realtime" (live GPS estimate) or "scheduled" (timetable
    only: the bus may not have started its trip yet). minutes_from_now uses the real-time
    estimate when present. Times are Europe/Lisbon. Only today's operational day is covered,
    so late at night the list may be short or empty.

    Args:
        stop_id: 6-digit stop id, e.g. "060361".
        line_id: Optional 4-digit line number to keep only that line, e.g. "2794".
        limit:   Max arrivals to return (default 10, max 50).
    """
    try:
        c = client()
        sid = _stop_id(stop_id)
        limit = max(1, min(int(limit), 50))
        try:
            raw = c.arrivals_by_stop(sid)
        except LookupError:
            return {"error": f"unknown stop_id {sid!r}; use bus_find_stops to look it up"}

        now = time.time()
        want_line = line_of(line_id) if line_id else None
        lines = c.lines()
        rows = []
        for a in raw:
            if a.get("observed_arrival_unix"):  # already passed this stop
                continue
            lid = line_of(a.get("line_id"))
            if want_line and lid != want_line:
                continue
            est = a.get("estimated_arrival_unix")
            sched = a.get("scheduled_arrival_unix")
            when = est or sched
            if when is None or when < now - 60:
                continue
            rows.append((when, lid, a, est, sched))
        rows.sort(key=lambda r: r[0])

        out = []
        for when, lid, a, est, sched in rows[:limit]:
            out.append(
                {
                    "line_id": lid,
                    "line_name": (lines.get(lid) or {}).get("long_name"),
                    "headsign": a.get("headsign"),
                    "minutes_from_now": max(0, round((when - now) / 60)),
                    "time": lisbon_time(when),
                    "source": "realtime" if est else "scheduled",
                    "scheduled_time": lisbon_time(sched),
                    "delay_min": round((est - sched) / 60) if est and sched else None,
                    "vehicle_id": a.get("vehicle_id"),
                    "pattern_id": strip_prefix(a.get("pattern_id")),
                }
            )
        stop = c.stops().get(sid) or {}
        return {
            "stop_id": sid,
            "stop_name": stop.get("long_name"),
            "now": now_lisbon().strftime("%Y-%m-%d %H:%M"),
            "count": len(out),
            "arrivals": out,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("next_arrivals failed")
        return {"error": str(exc)}


@mcp.tool(name="bus_line_info")
def line_info(line: str, include_stops: bool = False) -> dict[str, Any]:
    """Details of a Carris Metropolitana line: name, route ends, directions, municipalities.

    Use for "where does line 4715 go?", "which bus goes from Cacilhas to Almada?", or to check
    whether a line passes a stop (include_stops=True). If `line` is not an exact line number it
    is treated as text (e.g. "Oriente Setubal") and the matching lines are listed instead.
    trips_today = 0 means the line does not run today (some lines are seasonal or weekday-only).
    Departure times past midnight are written "24:30", "25:10" (same service day).

    Args:
        line:          4-digit line number ("4715") or free text matched against line names.
        include_stops: Also list every stop (id + name) in order for each direction.
    """
    try:
        c = client()
        lines = c.lines()
        key = line.strip()
        ln = lines.get(key) or lines.get(line_of(key))
        if ln is None:
            words = normalize(key).split()
            matches = [x for x in lines.values() if words and all(w in x["_norm"] for w in words)]
            matches.sort(key=lambda x: x["id"])
            if not matches:
                return {"error": f"no line matches {line!r}"}
            if len(matches) > 1:
                return {"matches": [_line_summary(x) for x in matches[:30]], "total_matches": len(matches)}
            ln = matches[0]

        directions = []
        for pid in ln.get("pattern_ids") or []:
            try:
                p = c.pattern(pid)
            except LookupError:
                continue
            if not p:
                continue
            path = sorted(p.get("path") or [], key=lambda x: x.get("stop_sequence", 0))
            first = path[0]["stop_id"] if path else None
            last = path[-1]["stop_id"] if path else None
            today = now_lisbon().strftime("%Y%m%d")
            departures = sorted(
                (t.get("schedule") or [{}])[0].get("arrival_time") or ""
                for t in p.get("trips") or []
                if today in (t.get("valid_on") or [])
            )
            departures = [d for d in departures if d]
            d: dict[str, Any] = {
                "pattern_id": pid,
                "direction_id": p.get("direction_id"),
                "headsign": p.get("headsign"),
                "from": c.stop_name(first),
                "to": c.stop_name(last),
                "stop_count": len(path),
                "trips_today": len(departures),
                "first_departure_today": departures[0][:5] if departures else None,
                "last_departure_today": departures[-1][:5] if departures else None,
            }
            if include_stops:
                d["stops"] = [{"stop_id": x["stop_id"], "name": c.stop_name(x["stop_id"])} for x in path]
            directions.append(d)

        info = _line_summary(ln)
        info["localities"] = [n for n in (c.locality_name(i) for i in ln.get("locality_ids") or []) if n]
        info["directions"] = directions
        return info
    except Exception as exc:  # noqa: BLE001
        log.exception("line_info failed")
        return {"error": str(exc)}


@mcp.tool(name="bus_service_alerts")
def service_alerts(
    line_id: str | None = None,
    stop_id: str | None = None,
    include_upcoming: bool = True,
    language: str = "pt",
    limit: int = 20,
) -> dict[str, Any]:
    """Service alerts (detours, works, strikes, stops not served) for Carris Metropolitana.

    Use before recommending a trip, or when the user asks "is line X disrupted?". Filter by
    line and/or stop; with no filter, returns all current alerts (there are often ~100).
    Alerts are mostly published in Portuguese only.

    Args:
        line_id:          4-digit line number, e.g. "4407".
        stop_id:          6-digit stop id, e.g. "160584".
        include_upcoming: Also return alerts that start in the future (default True).
        language:         Preferred language ("pt" or "en"); falls back to what exists.
        limit:            Max alerts to return (default 20, max 100).
    """
    try:
        c = client()
        now = time.time()
        want_line = line_of(line_id) if line_id else None
        want_stop = _stop_id(stop_id) if stop_id else None
        limit = max(1, min(int(limit), 100))

        rows = []
        for al in c.alerts():
            ents = al.get("informed_entity") or []
            al_lines = sorted({line_of(e["route_id"]) for e in ents if e.get("route_id")})
            al_stops = sorted({str(e["stop_id"]) for e in ents if e.get("stop_id")})
            if want_line and want_line not in al_lines:
                continue
            if want_stop and want_stop not in al_stops:
                continue
            periods = al.get("active_period") or [{}]
            active_now = any((p.get("start") or 0) <= now <= (p.get("end") or float("inf")) for p in periods)
            future = any((p.get("start") or 0) > now for p in periods)
            if not active_now and not (include_upcoming and future):
                continue
            start = min((p.get("start") or 0) for p in periods) or None
            ends = [p.get("end") for p in periods]
            end = None if any(e is None for e in ends) else max(ends)
            img = ((al.get("image") or {}).get("localized_image") or [{}])[0].get("url")
            rows.append(
                (
                    not active_now,
                    start or 0,
                    {
                        "title": _translation(al.get("header_text"), language),
                        "description": _translation(al.get("description_text"), language),
                        "cause": al.get("cause"),
                        "effect": al.get("effect"),
                        "active_now": active_now,
                        "start": lisbon_time(start, "%Y-%m-%d %H:%M"),
                        "end": lisbon_time(end, "%Y-%m-%d %H:%M"),
                        "lines": al_lines,
                        "stops": al_stops,
                        "image_url": img,
                    },
                )
            )
        rows.sort(key=lambda r: (r[0], -r[1] if not r[0] else r[1]))
        return {"total": len(rows), "count": min(len(rows), limit), "alerts": [r[2] for r in rows[:limit]]}
    except Exception as exc:  # noqa: BLE001
        log.exception("service_alerts failed")
        return {"error": str(exc)}


@mcp.tool(name="bus_vehicle_positions")
def vehicle_positions(line_id: str) -> dict[str, Any]:
    """Live GPS positions of the buses currently running on a Carris Metropolitana line.

    Use for "where is the 3508 right now?" or to see how many buses are out on a line.
    Each vehicle includes its position, heading, speed, the stop it is at / heading to,
    and how many seconds old the fix is.

    Args:
        line_id: 4-digit line number, e.g. "3508".
    """
    try:
        c = client()
        want = line_of(line_id)
        now = time.time()
        out = []
        for v in c.vehicles():
            if line_of(v.get("line_id")) != want:
                continue
            ts = v.get("timestamp")
            ts_s = ts / 1000 if ts and ts > 1e11 else ts  # API sends milliseconds
            pattern = None
            pid = strip_prefix(v.get("pattern_id"))
            if pid:
                try:
                    pattern = c.pattern(pid)
                except Exception:  # noqa: BLE001 — headsign is a nice-to-have
                    pattern = None
            out.append(
                {
                    "vehicle_id": strip_prefix(v.get("id")),
                    "lat": v.get("lat"),
                    "lon": v.get("lon"),
                    "bearing": v.get("bearing"),
                    "speed_kmh": v.get("speed"),
                    "headsign": (pattern or {}).get("headsign"),
                    "status": v.get("current_status"),
                    "stop_id": v.get("stop_id"),
                    "stop_name": c.stop_name(v.get("stop_id")),
                    "occupancy": v.get("occupancy_status"),
                    "updated": lisbon_time(ts_s, "%H:%M:%S"),
                    "age_s": round(now - ts_s) if ts_s else None,
                }
            )
        ln = c.lines().get(want) or {}
        return {"line_id": want, "line_name": ln.get("long_name"), "count": len(out), "vehicles": out}
    except Exception as exc:  # noqa: BLE001
        log.exception("vehicle_positions failed")
        return {"error": str(exc)}

