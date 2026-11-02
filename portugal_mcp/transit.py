"""Timetables from the operators' official GTFS feeds: CP trains, Metro de Lisboa, Carris, Fertagus.

Each feed is downloaded once a day at most (re-downloaded only when it changed) and loaded into a
local SQLite file, so even Carris' 145 MB of stop times are queried without holding them in memory.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import sqlite3
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .app import mcp, note
from .common import cache_dir, clamp, download, fail, haversine_km, http, norm

log = logging.getLogger("portugal_mcp.transit")

TZ = ZoneInfo("Europe/Lisbon")

# Official feeds only (each operator publishes it; listed in the Mobility Database as official).
OPERATORS: dict[str, dict[str, str]] = {
    "cp": {
        "name": "CP - Comboios de Portugal",
        "mode": "train",
        "url": "https://publico.cp.pt/gtfs/gtfs.zip",
        "covers": "Every CP train: long distance (Alfa Pendular, Intercidades), regional, and the Lisbon, Porto and Coimbra suburban lines",
    },
    "metro_lisboa": {
        "name": "Metropolitano de Lisboa",
        "mode": "metro",
        "url": "https://www.metrolisboa.pt/google_transit/googleTransit.zip",
        "covers": "The four Lisbon metro lines (Azul, Amarela, Verde, Vermelha)",
    },
    "carris": {
        "name": "Carris (Lisbon city buses, trams and lifts)",
        "mode": "bus",
        "url": "https://gateway.carris.pt/gateway/gtfs/api/v2.11/GTFS",
        "covers": "Lisbon city buses, trams (e.g. 28E) and lifts. Greater-Lisbon buses are Carris Metropolitana: use the bus_* tools",
    },
    "fertagus": {
        "name": "Fertagus",
        "mode": "train",
        "url": "https://www.fertagus.pt/GTFSTMLzip/Fertagus_GTFS.zip",
        "covers": "The train over the 25 de Abril bridge: Roma-Areeiro - Setúbal",
    },
}

SOURCE = "Operators' official GTFS timetables"
CHECK_EVERY = 86400  # seconds between upstream freshness checks

note(
    "transit",
    "Timetables (scheduled times, not live) for CP trains, Metro de Lisboa, Carris (Lisbon city "
    "buses and trams) and Fertagus. Use transit_find_stops, then transit_departures or "
    "transit_trips_between. The first use of an operator downloads its timetable (Carris: about a "
    "minute). Say the times are scheduled; for disruptions point to the operator's site.",
)

_locks: dict[str, threading.Lock] = {op: threading.Lock() for op in OPERATORS}
_conns: dict[str, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE stops (stop_id TEXT PRIMARY KEY, name TEXT, norm TEXT, lat REAL, lon REAL, station TEXT);
CREATE TABLE routes (route_id TEXT PRIMARY KEY, short TEXT, long TEXT, type INTEGER);
CREATE TABLE trips (trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT, headsign TEXT, short TEXT);
CREATE TABLE stop_times (trip_id TEXT, seq INTEGER, stop_id TEXT, arr INTEGER, dep INTEGER);
CREATE TABLE calendar (service_id TEXT PRIMARY KEY, days TEXT, start TEXT, end TEXT);
CREATE TABLE calendar_dates (service_id TEXT, date TEXT, type INTEGER);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
INDEXES = """
CREATE INDEX st_stop ON stop_times(stop_id, dep);
CREATE INDEX st_trip ON stop_times(trip_id, seq);
CREATE INDEX cd_date ON calendar_dates(date);
CREATE INDEX stops_station ON stops(station);
"""


def _key(text: Any) -> str:
    """Search key: accents, case and punctuation folded ("Baixa-Chiado" == "baixa chiado")."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", norm(text)).split())


def _secs(hms: str) -> int | None:
    try:
        h, m, s = hms.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def _rows(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def _build(op: str, zip_path: str, db_path: str, fingerprint: str) -> None:
    tmp = db_path + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        with zipfile.ZipFile(zip_path) as z:
            stops = []
            for r in _rows(z, "stops.txt"):
                try:
                    lat, lon = float(r.get("stop_lat") or "nan"), float(r.get("stop_lon") or "nan")
                except ValueError:
                    lat = lon = float("nan")
                station = (r.get("parent_station") or "").strip() or r["stop_id"]
                stops.append((r["stop_id"], r.get("stop_name", "").strip(), _key(r.get("stop_name")), lat, lon, station))
            con.executemany("INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?,?)", stops)
            con.executemany(
                "INSERT OR REPLACE INTO routes VALUES (?,?,?,?)",
                ((r["route_id"], r.get("route_short_name", ""), r.get("route_long_name", ""), int(r.get("route_type") or 3)) for r in _rows(z, "routes.txt")),
            )
            con.executemany(
                "INSERT OR REPLACE INTO trips VALUES (?,?,?,?,?)",
                ((r["trip_id"], r["route_id"], r["service_id"], r.get("trip_headsign", ""), r.get("trip_short_name", "")) for r in _rows(z, "trips.txt")),
            )
            batch: list[tuple[Any, ...]] = []
            for r in _rows(z, "stop_times.txt"):
                dep = _secs(r.get("departure_time") or r.get("arrival_time") or "")
                arr = _secs(r.get("arrival_time") or r.get("departure_time") or "")
                if dep is None:
                    continue
                batch.append((r["trip_id"], int(r.get("stop_sequence") or 0), r["stop_id"], arr, dep))
                if len(batch) >= 50000:
                    con.executemany("INSERT INTO stop_times VALUES (?,?,?,?,?)", batch)
                    batch.clear()
            con.executemany("INSERT INTO stop_times VALUES (?,?,?,?,?)", batch)
            days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            con.executemany(
                "INSERT OR REPLACE INTO calendar VALUES (?,?,?,?)",
                ((r["service_id"], "".join((r.get(d) or "0").strip() for d in days), r["start_date"], r["end_date"]) for r in _rows(z, "calendar.txt")),
            )
            con.executemany(
                "INSERT INTO calendar_dates VALUES (?,?,?)",
                ((r["service_id"], r["date"], int(r.get("exception_type") or 0)) for r in _rows(z, "calendar_dates.txt")),
            )
        con.executescript(INDEXES)
        con.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            [("fingerprint", fingerprint), ("checked", str(time.time())), ("built", datetime.now(TZ).isoformat(timespec="minutes"))],
        )
        con.commit()
    finally:
        con.close()
    os.replace(tmp, db_path)


def _fingerprint(url: str) -> str | None:
    try:
        r = http().head(url, timeout=20)
        if r.status_code >= 400:
            return None
        h = r.headers
        return h.get("Last-Modified") or h.get("ETag") or (f"len:{h['Content-Length']}" if h.get("Content-Length") else None)
    except Exception:  # noqa: BLE001 — freshness check is best effort
        return None


def _db(op: str) -> sqlite3.Connection:
    """Open the operator's timetable, refreshing it from the official feed when it changed."""
    if op not in OPERATORS:
        raise ValueError(f"unknown operator {op!r}; one of: {', '.join(OPERATORS)}")
    with _locks[op]:
        d = cache_dir("transit")
        db_path = str(d / f"{op}.sqlite")
        con = _conns.get(op)
        meta: dict[str, str] = {}
        if os.path.exists(db_path):
            try:
                c = con or sqlite3.connect(db_path, check_same_thread=False)
                meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
                con = c
            except sqlite3.Error:
                con = None
        stale = not meta or time.time() - float(meta.get("checked", 0)) > CHECK_EVERY
        if stale:
            url = OPERATORS[op]["url"]
            fp = _fingerprint(url)
            if meta and fp and fp == meta.get("fingerprint"):
                con.execute("UPDATE meta SET value=? WHERE key='checked'", (str(time.time()),))
                con.commit()
            else:
                try:
                    zpath = d / f"{op}.zip.download"
                    log.info("downloading %s timetable", op)
                    download(url, zpath)
                    if con is not None:
                        con.close()
                        con = None
                        _conns.pop(op, None)
                    _build(op, str(zpath), db_path, fp or f"t:{int(time.time())}")
                    zpath.unlink(missing_ok=True)
                except Exception:
                    if not meta:
                        raise
                    log.warning("could not refresh %s timetable; using the cached one", op, exc_info=True)
        if con is None:
            con = sqlite3.connect(db_path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _conns[op] = con
        return con


def _ops(operator: str | None) -> list[str]:
    if not operator:
        return list(OPERATORS)
    key = norm(operator).replace(" ", "_").replace("-", "_")
    aliases = {"metro": "metro_lisboa", "metropolitano": "metro_lisboa", "ml": "metro_lisboa", "comboios": "cp", "trains": "cp", "train": "cp", "comboio": "cp"}
    key = aliases.get(key, key)
    if key not in OPERATORS:
        raise ValueError(f"unknown operator {operator!r}; one of: {', '.join(OPERATORS)}")
    return [key]


def _active_services(con: sqlite3.Connection, day: date) -> set[str]:
    ymd = day.strftime("%Y%m%d")
    wd = day.weekday()
    active = {
        r[0]
        for r in con.execute("SELECT service_id, days FROM calendar WHERE start <= ? AND end >= ?", (ymd, ymd))
        if len(r[1]) == 7 and r[1][wd] == "1"
    }
    for sid, typ in con.execute("SELECT service_id, type FROM calendar_dates WHERE date = ?", (ymd,)):
        if typ == 1:
            active.add(sid)
        elif typ == 2:
            active.discard(sid)
    return active


def _stations(con: sqlite3.Connection, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Stations matching a name. Stops sharing a parent station, or with no parent but the same
    name (e.g. one bus stop per direction), are grouped, with all their stop ids."""
    words = [w for w in _key(query).split() if w not in {"de", "da", "do", "das", "dos", "e"}]
    if not words:
        raise ValueError("give a stop or station name")
    where = " AND ".join("norm LIKE ?" for _ in words)
    rows = con.execute(f"SELECT stop_id, name, norm, lat, lon, station FROM stops WHERE {where}", [f"%{w}%" for w in words]).fetchall()  # noqa: S608 — only placeholders are interpolated
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        has_parent = r["station"] != r["stop_id"]
        gkey = f"s:{r['station']}" if has_parent or con.execute("SELECT 1 FROM stops WHERE station = ? AND stop_id != ? LIMIT 1", (r["stop_id"], r["stop_id"])).fetchone() else f"n:{r['norm']}"
        g = groups.setdefault(gkey, {"station": r["station"], "name": r["name"], "lat": r["lat"], "lon": r["lon"], "stop_ids": set(), "_n": r["norm"]})
        g["stop_ids"].add(r["stop_id"])
        if r["stop_id"] == r["station"] and gkey.startswith("s:"):
            g["name"], g["_n"], g["lat"], g["lon"] = r["name"], r["norm"], r["lat"], r["lon"]
    for gkey, g in groups.items():
        if gkey.startswith("s:"):
            g["stop_ids"].update(x[0] for x in con.execute("SELECT stop_id FROM stops WHERE station = ?", (g["station"],)))
    q = " ".join(words)

    def rank(g: dict[str, Any]) -> tuple[Any, ...]:
        n = g["_n"]
        return (n != q, not n.startswith(q), not re.search(rf"\b{re.escape(q)}\b", n), len(n))

    return sorted(groups.values(), key=rank)[:limit]


def _hhmm(secs: int) -> str:
    h, m = divmod(secs // 60, 60)
    return f"{h % 24:02d}:{m:02d}" + (" (+1 day)" if h >= 24 else "")


def _when(when: str | None) -> datetime:
    if not when:
        return datetime.now(TZ)
    try:
        dt = datetime.fromisoformat(when)
    except ValueError as exc:
        raise ValueError("`when` is an ISO date-time like 2026-11-03T08:00 (Lisbon time)") from exc
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def _feed_info(con: sqlite3.Connection, op: str) -> dict[str, Any]:
    built = con.execute("SELECT value FROM meta WHERE key='built'").fetchone()
    return {"operator": OPERATORS[op]["name"], "timetable_loaded": built[0] if built else None}


@mcp.tool()
def transit_operators() -> dict[str, Any]:
    """Which transport operators have timetables here, and what each one covers."""
    return {
        "operators": [{"id": k, "name": v["name"], "mode": v["mode"], "covers": v["covers"]} for k, v in OPERATORS.items()],
        "not_covered": "Metro do Porto and STCP do not publish a current official timetable feed; "
        "Greater-Lisbon buses (Carris Metropolitana) have live tools: bus_find_stops, bus_next_arrivals.",
        "source": SOURCE,
    }


@mcp.tool()
def transit_find_stops(
    query: str | None = None,
    operator: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Find train stations, metro stations or bus/tram stops by name, or the nearest to a point.

    Args:
        query: e.g. "Oriente", "Porto Campanhã", "Marquês de Pombal", "Prazeres".
        operator: "cp", "metro_lisboa", "carris" or "fertagus". Default: all of them.
        lat, lon: find the nearest instead of (or as well as) by name.
    """
    try:
        n = clamp(limit, 1, 30, 8)
        out = []
        for op in _ops(operator):
            con = _db(op)
            if query:
                found = _stations(con, query, n)
            elif lat is not None and lon is not None:
                rows = con.execute("SELECT stop_id, name, lat, lon, station FROM stops WHERE stop_id = station").fetchall()
                rows = sorted(rows, key=lambda r: haversine_km(lat, lon, r["lat"], r["lon"]))[:n]
                found = [{"station": r["station"], "name": r["name"], "lat": r["lat"], "lon": r["lon"], "stop_ids": {r["stop_id"]}} for r in rows]
            else:
                raise ValueError("give a name (query) or lat/lon")
            for g in found:
                row = {"operator": op, "station_id": g["station"], "name": g["name"], "lat": g["lat"], "lon": g["lon"]}
                if lat is not None and lon is not None and g["lat"] == g["lat"]:
                    row["distance_km"] = round(haversine_km(lat, lon, g["lat"], g["lon"]), 2)
                out.append(row)
        if lat is not None and lon is not None:
            out.sort(key=lambda r: r.get("distance_km", 1e9))
        return {"stops": out[: n * 2], "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "transit_find_stops", exc)


def _departures(con: sqlite3.Connection, stop_ids: set[str], start: datetime, horizon_min: int, route_filter: str | None) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(stop_ids))
    t0 = start.hour * 3600 + start.minute * 60
    out = []
    # trips of today, plus trips of yesterday still running after midnight (times past 24:00)
    for day, offset in ((start.date(), 0), (start.date() - timedelta(days=1), 86400)):
        services = _active_services(con, day)
        if not services:
            continue
        lo, hi = t0 + offset, t0 + offset + horizon_min * 60
        q = f"""
            SELECT st.trip_id, st.dep, st.seq, t.service_id, t.headsign, t.short, r.short AS rshort, r.long AS rlong, r.type
            FROM stop_times st JOIN trips t ON t.trip_id = st.trip_id JOIN routes r ON r.route_id = t.route_id
            WHERE st.stop_id IN ({marks}) AND st.dep BETWEEN ? AND ?
            ORDER BY st.dep
        """  # noqa: S608 — only placeholders are interpolated
        for r in con.execute(q, [*stop_ids, lo, hi]):
            if r["service_id"] not in services:
                continue
            last = con.execute("SELECT MAX(seq) FROM stop_times WHERE trip_id = ?", (r["trip_id"],)).fetchone()[0]
            if r["seq"] >= last:  # arriving here, not departing
                continue
            headsign = r["headsign"]
            if not headsign:
                end = con.execute(
                    "SELECT s.name FROM stop_times st JOIN stops s ON s.stop_id = st.stop_id WHERE st.trip_id = ? ORDER BY st.seq DESC LIMIT 1",
                    (r["trip_id"],),
                ).fetchone()
                headsign = end[0] if end else ""
            line = r["rshort"] or r["rlong"] or ""
            if route_filter and norm(route_filter) not in norm(f"{line} {r['rlong']} {headsign}"):
                continue
            out.append({
                "time": _hhmm(r["dep"] - offset),
                "minutes": round((r["dep"] - offset - t0) / 60),
                "line": line,
                "to": headsign,
                "train_number": r["short"] or None,
                "_k": r["dep"] - offset,
            })
    out.sort(key=lambda x: x["_k"])
    seen, unique = set(), []
    for x in out:
        key = (x["time"], x["to"], x["line"])
        if key not in seen:
            seen.add(key)
            unique.append({k: v for k, v in x.items() if k != "_k"})
    return unique


@mcp.tool()
def transit_departures(
    stop: str,
    operator: str | None = None,
    when: str | None = None,
    window_minutes: int = 90,
    line: str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """Scheduled departures from a station or stop (official timetable, not live).

    Args:
        stop: station or stop name, e.g. "Lisboa Oriente", "Cais do Sodré", "Baixa-Chiado", "Prazeres".
        operator: "cp", "metro_lisboa", "carris" or "fertagus"; default: every operator that has the stop.
        when: ISO date-time in Lisbon time, e.g. "2026-11-03T08:00". Default: now.
        window_minutes: how far ahead to look (max 720).
        line: keep only a line or destination, e.g. "28E", "Sintra", "Azul".
    """
    try:
        start = _when(when)
        horizon = clamp(window_minutes, 5, 720, 90)
        n = clamp(limit, 1, 50, 12)
        results = []
        for op in _ops(operator):
            con = _db(op)
            st = _stations(con, stop, 4)
            if not st:
                continue
            best, deps = st[0], []
            for cand in st:  # the best-named stop may not serve the line asked for; try the next ones
                deps = _departures(con, cand["stop_ids"], start, horizon, line)
                if deps:
                    best = cand
                    break
            results.append({**_feed_info(con, op), "station": best["name"], "departures": deps[:n],
                            "other_matches": [s["name"] for s in st if s is not best] or None})
        if not results:
            return {"error": f"no stop called {stop!r}; try transit_find_stops"}
        return {"from": start.strftime("%Y-%m-%d %H:%M"), "results": results, "note": "Scheduled times from the official timetable, not live positions.", "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "transit_departures", exc)


@mcp.tool()
def transit_trips_between(
    origin: str,
    destination: str,
    operator: str | None = None,
    when: str | None = None,
    window_minutes: int = 240,
    limit: int = 6,
) -> dict[str, Any]:
    """Direct services between two stations (no changes), with departure, arrival and duration.

    Examples: CP "Lisboa Oriente" → "Porto Campanhã"; Fertagus "Roma-Areeiro" → "Setúbal";
    Metro "Alameda" → "Aeroporto" (same line only). Scheduled times, not live.
    Args:
        operator: "cp", "metro_lisboa", "carris" or "fertagus"; default: try all.
        when: ISO date-time in Lisbon time; default now.
        window_minutes: departures within this window (max 1440).
    """
    try:
        start = _when(when)
        horizon = clamp(window_minutes, 10, 1440, 240)
        n = clamp(limit, 1, 20, 6)
        t0 = start.hour * 3600 + start.minute * 60
        results = []
        for op in _ops(operator):
            con = _db(op)
            a, b = _stations(con, origin, 1), _stations(con, destination, 1)
            if not a or not b:
                continue
            a_ids, b_ids = a[0]["stop_ids"], b[0]["stop_ids"]
            am, bm = ",".join("?" * len(a_ids)), ",".join("?" * len(b_ids))
            trips = []
            for day, offset in ((start.date(), 0), (start.date() - timedelta(days=1), 86400)):
                services = _active_services(con, day)
                q = f"""
                    SELECT o.trip_id, o.dep AS dep, d.arr AS arr, t.service_id, t.short, t.headsign, r.short AS rshort, r.long AS rlong
                    FROM stop_times o JOIN stop_times d ON d.trip_id = o.trip_id AND d.seq > o.seq
                    JOIN trips t ON t.trip_id = o.trip_id JOIN routes r ON r.route_id = t.route_id
                    WHERE o.stop_id IN ({am}) AND d.stop_id IN ({bm}) AND o.dep BETWEEN ? AND ?
                    ORDER BY o.dep
                """  # noqa: S608 — only placeholders are interpolated
                for r in con.execute(q, [*a_ids, *b_ids, t0 + offset, t0 + offset + horizon * 60]):
                    if r["service_id"] not in services:
                        continue
                    trips.append({
                        "depart": _hhmm(r["dep"] - offset),
                        "arrive": _hhmm(r["arr"] - offset),
                        "duration_min": round((r["arr"] - r["dep"]) / 60),
                        "service": r["rshort"] or r["rlong"] or None,
                        "train_number": r["short"] or None,
                        "towards": r["headsign"] or None,
                        "_k": r["dep"] - offset,
                    })
            trips.sort(key=lambda x: x["_k"])
            if trips or len(_ops(operator)) == 1:
                results.append({
                    **_feed_info(con, op),
                    "from": a[0]["name"],
                    "to": b[0]["name"],
                    "trips": [{k: v for k, v in x.items() if k != "_k"} for x in trips[:n]],
                })
        if not any(r["trips"] for r in results):
            return {
                "results": results,
                "note": "No direct service in that window. Try a wider window, another day, or a change of line/operator "
                "(e.g. Metro to Oriente, then CP).",
                "source": SOURCE,
            }
        return {"from": start.strftime("%Y-%m-%d %H:%M"), "results": [r for r in results if r["trips"]],
                "note": "Scheduled times from the official timetable, not live.", "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "transit_trips_between", exc)
