"""Client for the official Carris Metropolitana open API (v2).

Carris Metropolitana runs the buses of the Lisbon Metropolitan Area (outside the
Lisbon and Barreiro municipal networks). Its JSON API is public, keyless and the
same one its own website and apps use: https://api.carrismetropolitana.pt/v2

Big, slow-changing lists (stops ~7 MB, lines, localities, municipalities) are
cached in memory with a long TTL; real-time feeds (alerts, vehicles) with a short one.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..common import USER_AGENT

log = logging.getLogger("portugal_mcp.transportes")

BASE_URL = "https://api.carrismetropolitana.pt/v2"
TZ = ZoneInfo("Europe/Lisbon")

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

STATIC_TTL = 12 * 3600  # stops, lines, localities, municipalities
REALTIME_TTL = 30       # alerts, vehicles

# Operator/agency prefixes like "[BNA17]2794" or "[PL3CL][YA15B]3508_0_2".
_PREFIX = re.compile(r"^(?:\[[^\]]*\])+")


def strip_prefix(value: str | None) -> str:
    """'[BNA17]2794' -> '2794'; '[A2L1N]4407_0' -> '4407_0'."""
    return _PREFIX.sub("", value or "")


def line_of(route_or_pattern_id: str | None) -> str:
    """'[A2L1N]4407_0' -> '4407'; '2792_0_1' -> '2792'."""
    return strip_prefix(route_or_pattern_id).split("_", 1)[0]


def normalize(text: str | None) -> str:
    """Lowercase, accent-free, single-spaced text for forgiving name matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def lisbon_time(unix_s: float | None, fmt: str = "%H:%M") -> str | None:
    if unix_s is None:
        return None
    return datetime.fromtimestamp(unix_s, TZ).strftime(fmt)


def now_lisbon() -> datetime:
    return datetime.now(TZ)


class _Cached:
    def __init__(self, loader: Callable[[], Any], ttl: float) -> None:
        self._loader = loader
        self._ttl = ttl
        self._value: Any = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> Any:
        with self._lock:
            if self._value is None or time.monotonic() - self._at > self._ttl:
                self._value = self._loader()
                self._at = time.monotonic()
            return self._value


class _Lookup(_Cached):
    """Cached id -> record map for optional enrichment (names). A failed download yields {}
    and is retried only after a short back-off, so one bad fetch can't stall every call."""

    RETRY_AFTER = 300

    def get(self) -> Any:
        try:
            return super().get()
        except Exception:  # noqa: BLE001 — names are a nice-to-have
            log.warning("lookup download failed; continuing without names", exc_info=True)
            with self._lock:
                self._value = {}
                self._at = time.monotonic() - self._ttl + self.RETRY_AFTER
            return {}


class CarrisClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)
        self._stops = _Cached(self._load_stops, STATIC_TTL)
        self._lines = _Cached(self._load_lines, STATIC_TTL)
        self._localities = _Lookup(lambda: self._index("/locations/localities"), STATIC_TTL)
        self._municipalities = _Lookup(lambda: self._index("/locations/municipalities"), STATIC_TTL)
        self._alerts = _Cached(lambda: self.get_json("/alerts"), REALTIME_TTL)
        self._vehicles = _Cached(lambda: self.get_json("/vehicles"), REALTIME_TTL)
        self._patterns: dict[str, tuple[float, Any]] = {}

    # --- HTTP -------------------------------------------------------------

    def get_json(self, path: str) -> Any:
        url = self.base_url + path
        resp = self._http.get(url)
        if resp.status_code == 404:
            raise LookupError(f"not found: {path}")
        resp.raise_for_status()
        data = resp.json()
        # Some endpoints use the JSend envelope {status, timestamp, data}; others return the bare payload.
        if isinstance(data, dict) and "data" in data and "status" in data:
            if data.get("status") != "success":
                raise RuntimeError(data.get("message") or f"API error on {path}")
            return data["data"]
        return data

    def _index(self, path: str) -> dict[str, dict[str, Any]]:
        # Skip malformed rows (the localities list has one entry without an id).
        return {str(x["id"]): x for x in self.get_json(path) if isinstance(x, dict) and x.get("id")}

    # --- static data (cached) ----------------------------------------------

    def _load_stops(self) -> dict[str, dict[str, Any]]:
        log.info("downloading stops list")
        stops = {}
        for s in self.get_json("/stops"):
            s["_norm"] = normalize(s.get("long_name"))
            stops[str(s["id"])] = s
        log.info("cached %d stops", len(stops))
        return stops

    def _load_lines(self) -> dict[str, dict[str, Any]]:
        lines = {}
        for ln in self.get_json("/lines"):
            ln["_norm"] = normalize(f"{ln.get('short_name', '')} {ln.get('long_name', '')}")
            lines[str(ln["id"])] = ln
        log.info("cached %d lines", len(lines))
        return lines

    def stops(self) -> dict[str, dict[str, Any]]:
        return self._stops.get()

    def lines(self) -> dict[str, dict[str, Any]]:
        return self._lines.get()

    def locality_name(self, locality_id: str | None) -> str | None:
        if not locality_id:
            return None
        loc = self._localities.get().get(str(locality_id))
        return (loc or {}).get("display") or (loc or {}).get("name")

    def municipality_name(self, municipality_id: str | None) -> str | None:
        if not municipality_id:
            return None
        mun = self._municipalities.get().get(str(municipality_id))
        return (mun or {}).get("name")

    def stop_name(self, stop_id: str | None) -> str | None:
        if not stop_id:
            return None
        return (self.stops().get(str(stop_id)) or {}).get("long_name")

    def pattern(self, pattern_id: str) -> dict[str, Any] | None:
        """One pattern (a line's path in one direction); the API returns a list of versions."""
        hit = self._patterns.get(pattern_id)
        if hit and time.monotonic() - hit[0] < STATIC_TTL:
            return hit[1]
        data = self.get_json(f"/patterns/{pattern_id}")
        versions = data if isinstance(data, list) else [data]
        today = now_lisbon().strftime("%Y%m%d")
        # Prefer the version valid today; else the next upcoming one; else the most recent past one.
        chosen = next((v for v in versions if today in (v.get("valid_on") or [])), None)
        if chosen is None and versions:
            def first_day(v: dict[str, Any]) -> str:
                return min(v.get("valid_on") or ["99999999"])

            upcoming = sorted((v for v in versions if first_day(v) > today), key=first_day)
            chosen = upcoming[0] if upcoming else max(versions, key=lambda v: max(v.get("valid_on") or ["0"]))
        self._patterns[pattern_id] = (time.monotonic(), chosen)
        return chosen

    # --- real-time ----------------------------------------------------------

    def arrivals_by_stop(self, stop_id: str) -> list[dict[str, Any]]:
        return self.get_json(f"/arrivals/by_stop/{stop_id}")

    def alerts(self) -> list[dict[str, Any]]:
        return self._alerts.get()

    def vehicles(self) -> list[dict[str, Any]]:
        return self._vehicles.get()
