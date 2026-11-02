"""MOBI.E charging network from Portugal's National Access Point (NAP), DATEX II 3.3 XML.

Two open feeds (listed on nap-portugal.imt-ip.pt, datasets 148 and 149, "Livre acesso"):
- static inventory  .../integration/nap/evChargingInfra  (~180 MB): sites, stations,
  refill points, connectors, power, operator, coordinates, address, opening hours
- dynamic status    .../integration/nap/evActualStatus   (~40 MB): per point `status`
  (available, charging, outOfOrder, ...) and `rates` (pricing policy + fee)

Both are stream-parsed straight from the HTTP response (no DOM, no temp XML file) into
a compact gzip JSON cache in the user's cache dir. The static cache is refreshed at most
every 24 h and the status cache every 10 min, and only when a tool needs them.

Join key between the feeds: (site id, point id). Some operators publish bare point ids
("19", "160") that repeat across sites, so the point id alone is not unique.
(See CarregaErvilhas/nap-dashboard references/gotchas.md.)
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET  # types only; parsing goes through defusedxml
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..common import cache_dir as shared_cache_dir
from .common import USER_AGENT, haversine_km, norm

log = logging.getLogger("portugal_mcp.energia")

# pgm.mobie.pt is the host used by most consumers; ev-nap.mobie.pt is what the NAP lists.
STATIC_URLS = [
    "https://pgm.mobie.pt/integration/nap/evChargingInfra",
    "https://ev-nap.mobie.pt/integration/nap/evChargingInfra",
]
STATUS_URLS = [
    "https://pgm.mobie.pt/integration/nap/evActualStatus",
    "https://ev-nap.mobie.pt/integration/nap/evActualStatus",
]
STATIC_TTL = 24 * 3600
STATUS_TTL = 10 * 60
CACHE_VERSION = 1

SOURCE = (
    "MOBI.E charging network via Portugal's National Access Point (NAP, IMT): "
    "DATEX II feeds evChargingInfra (static) and evActualStatus (status + prices), open access."
)

PRICE_NOTE = (
    "Prices come from the `rates` the MOBI.E status feed publishes per charging point: a "
    "pricing policy and a fee (per kWh, per minute of charging, per session, or 'other', e.g. "
    "parking time). The feed does not say which tariff it is (regular vs. ad hoc / pay at the "
    "charger) nor whether VAT is included. A spot check against MOBI.E's own tariff list found "
    "that where a point has an ad hoc tariff the feed usually carries that one; elsewhere it "
    "carries the operator's (OPC) regular tariff. On the MOBI.E network the OPC tariff is only "
    "the charging point's fee: when you pay with a MOBI.E card or app, your provider (CEME) "
    "adds its own energy price, plus network access charges, the MOBI.E management fee and "
    "taxes. Treat these prices as indicative and compare like with like; check the operator's "
    "app or your CEME for the final bill."
)

CONNECTOR_LABELS = {
    "iec62196T2": "Type 2",
    "iec62196T2COMBO": "CCS2",
    "chademo": "CHAdeMO",
    "iec60309x2single16": "CEE 16A (industrial)",
}
MODE_LABELS = {"mode4DC": "DC", "mode3AC3p": "AC 3-phase", "mode3AC1p": "AC 1-phase", "mode2AC1p": "AC 1-phase"}
CONNECTOR_ALIASES = {
    "ccs": "iec62196T2COMBO",
    "ccs2": "iec62196T2COMBO",
    "combo": "iec62196T2COMBO",
    "type 2": "iec62196T2",
    "type2": "iec62196T2",
    "t2": "iec62196T2",
    "mennekes": "iec62196T2",
    "chademo": "chademo",
    "cee": "iec60309x2single16",
    "industrial": "iec60309x2single16",
}
STATUS_LABELS = {
    "available": "available",
    "charging": "in use (charging)",
    "outOfOrder": "out of order",
    "inoperative": "inoperative",
    "blocked": "blocked",
    "planned": "planned (not yet open)",
    "unknown": "unknown",
}
POLICY_KEYS = {
    "pricePerDeliveryUnit": "per_kwh",
    "pricePerChargingTime": "per_minute",
    "flatRate": "per_session",
}
DAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_SHORT = dict(zip(DAY_ORDER, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], strict=True))
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

Progress = Callable[[str, float | None], None]


# ---------------------------------------------------------------------------------
# XML helpers (namespace-agnostic)
# ---------------------------------------------------------------------------------


def _ln(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for c in el:
        if _ln(c.tag) == name:
            return c
    return None


def _desc(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for d in el.iter():
        if d is not el and _ln(d.tag) == name:
            return d
    return None


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _value(el: ET.Element | None) -> str | None:
    """Multilingual text (values/value) or plain text."""
    if el is None:
        return None
    v = _desc(el, "value")
    return _text(v) if v is not None else _text(el)


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def _hours(oh: ET.Element | None) -> str | None:
    """'24/7', 'Mon-Fri 07:30-21:00; Sat 08:00-13:00', or None when not specified."""
    if oh is None:
        return None
    if "OpenAllHours" in (oh.get(XSI_TYPE) or ""):
        return "24/7"
    per_day: dict[str, list[str]] = {}
    for vp in (d for d in oh.iter() if _ln(d.tag) == "validPeriod"):
        start = (_text(_desc(vp, "startTimeOfPeriod")) or "")[:5]
        end = (_text(_desc(vp, "endTimeOfPeriod")) or "")[:5]
        days = [_text(d) for d in vp.iter() if _ln(d.tag) == "applicableDay"] or DAY_ORDER
        for day in days:
            if day and start:
                per_day.setdefault(day, []).append(f"{start}-{end}")
    if not per_day:
        return None
    groups: list[tuple[list[str], str]] = []
    for day in DAY_ORDER:
        span = ", ".join(per_day.get(day, [])) or "closed"
        if groups and groups[-1][1] == span:
            groups[-1][0].append(day)
        else:
            groups.append(([day], span))
    parts = []
    for days, span in groups:
        label = DAY_SHORT[days[0]] if len(days) == 1 else f"{DAY_SHORT[days[0]]}-{DAY_SHORT[days[-1]]}"
        parts.append(f"{label} {span}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------------
# Streaming download + parse
# ---------------------------------------------------------------------------------


def _stream_parse(
    urls: list[str],
    end_tag: str,
    handle: Callable[[ET.Element], None],
    on_end: Callable[[ET.Element], None] | None,
    label: str,
    progress: Progress | None,
    etag: str | None = None,
    reset: Callable[[], None] | None = None,
) -> tuple[str | None, str | None, bool]:
    """Download one of `urls` and feed it to an incremental XML parser.

    `handle` is called for every element named `end_tag` (then the element is cleared);
    `on_end` for every other completed element (used for small context such as site refs).
    `reset` is called before each attempt, so a mirror retry starts from empty results.
    Returns (publicationTime, etag, not_modified).
    """
    last_exc: Exception | None = None
    for url in urls:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/xml"}
        if etag:
            headers["If-None-Match"] = etag
        if reset is not None:
            reset()
        try:
            pub: list[str | None] = [None]
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0), follow_redirects=True) as http:
                with http.stream("GET", url, headers=headers) as r:
                    if r.status_code == 304:
                        return None, etag, True
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    log.info("downloading %s (%s)", url, f"{total / 1e6:.0f} MB" if total else "size unknown")
                    parser = ET.XMLPullParser(events=("end",))
                    done, next_mark, t0 = 0, 0.1, time.time()

                    def drain() -> None:
                        for _, el in parser.read_events():
                            name = _ln(el.tag)
                            if name == end_tag:
                                handle(el)
                                el.clear()
                            elif name == "publicationTime" and pub[0] is None:
                                pub[0] = _text(el)
                            elif on_end is not None:
                                on_end(el)

                    for chunk in r.iter_bytes(1 << 16):
                        parser.feed(chunk)
                        drain()
                        done += len(chunk)
                        if total and done / total >= next_mark:
                            msg = f"{label}: {done / 1e6:.0f}/{total / 1e6:.0f} MB ({time.time() - t0:.0f}s)"
                            log.info(msg)
                            if progress:
                                progress(msg, done / total)
                            next_mark += 0.1
                    parser.close()
                    drain()
                    log.info("%s: parsed in %.0fs", label, time.time() - t0)
                    return pub[0], r.headers.get("etag"), False
        except Exception as exc:  # noqa: BLE001 — try the mirror, then give up
            log.warning("%s: %s failed: %s", label, url, exc)
            last_exc = exc
    raise RuntimeError(f"Could not download the MOBI.E {label} feed: {last_exc}")


def _parse_site(el: ET.Element) -> dict[str, Any]:
    op = _child(el, "operator")
    addr = _desc(el, "address")
    lat = _num(_text(_desc(el, "latitude")))
    lon = _num(_text(_desc(el, "longitude")))
    auth: set[str] = set()
    points = []
    for st in (d for d in el.iter() if _ln(d.tag) == "energyInfrastructureStation"):
        for a in st:
            if _ln(a.tag) == "authenticationAndIdentificationMethods" and _text(a):
                auth.add(_text(a))  # type: ignore[arg-type]
        for rp in (d for d in st.iter() if _ln(d.tag) == "refillPoint"):
            conns = []
            for c in rp:
                if _ln(c.tag) != "connector":
                    continue
                w = _num(_text(_child(c, "maxPowerAtSocket")))
                conns.append(
                    [
                        _text(_child(c, "connectorType")),
                        _text(_child(c, "chargingMode")),
                        _text(_child(c, "connectorFormat")),
                        round(w / 1000, 1) if w is not None else None,
                    ]
                )
            points.append({"id": rp.get("id"), "evse_id": _text(_child(rp, "externalIdentifier")), "connectors": conns})
    return {
        "id": el.get("id"),
        "ext_id": _text(_child(el, "externalIdentifier")),
        "name": _value(_child(el, "name")),
        "operator": (_value(_child(op, "name")) or "").strip() or None,
        "operator_id": op.get("id") if op is not None else None,
        "operator_website": _text(_desc(op, "linkToGeneralInformation")),
        "operator_phone": _text(_desc(op, "telephoneNumber")),
        "lat": lat,
        "lon": lon,
        "address": _value(_desc(_desc(addr, "addressLine"), "text")),
        "postcode": _text(_desc(addr, "postcode")),
        "city": _value(_desc(addr, "city")),
        "area": _value(_desc(el, "areaName")),
        "hours": _hours(_child(el, "operatingHours")),
        "directions": _value(_desc(el, "locationDescription")),
        "auth": sorted(auth),
        "updated": _text(_child(el, "lastUpdated")),
        "points": points,
    }


def _parse_rates(rp: ET.Element) -> list[list[Any]]:
    """[[policy, fee, currency, start, end, info], ...] for one refillPointStatus."""
    out = []
    for mix in rp:
        if _ln(mix.tag) != "electricEnergyMixOverride":
            continue
        for rates in (d for d in mix if _ln(d.tag) == "rates"):
            out.append(
                [
                    _text(_desc(rates, "pricingPolicy")),
                    _num(_text(_desc(rates, "minimumDeliveryFee"))),
                    _text(_desc(rates, "applicableCurrency")),
                    _text(_desc(rates, "overallStartTime")),
                    _text(_desc(rates, "overallEndTime")),
                    _value(_desc(rates, "additionalInformation")),
                ]
            )
    return out


# ---------------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------------


def cache_dir() -> Path:
    d = shared_cache_dir("energia")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data if data.get("version") == CACHE_VERSION else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 — a corrupt cache is just a cache miss
        log.warning("ignoring unreadable cache %s: %s", path.name, exc)
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_min(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((datetime.now(timezone.utc) - t).total_seconds() / 60, 1)


class EVData:
    """Loads, caches and refreshes both feeds; answers spatial and id queries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.static: dict[str, Any] | None = None
        self.status: dict[str, Any] | None = None
        self._points_by_status_key: dict[str, list[Any]] = {}
        self.warnings: list[str] = []

    # -- refresh ------------------------------------------------------------------

    def _fresh(self, data: dict[str, Any] | None, ttl: float) -> bool:
        return bool(data) and (time.time() - data.get("fetched_epoch", 0)) < ttl

    def _load_static(self, progress: Progress | None) -> None:
        path = cache_dir() / "mobie_static.json.gz"
        if self.static is None:
            self.static = _read_json(path)
        if self._fresh(self.static, STATIC_TTL):
            return
        log.info("refreshing MOBI.E static inventory (first run can take 1-3 minutes)")
        if progress:
            progress("Downloading the MOBI.E charger inventory (~180 MB, first run only per day)", 0.0)
        sites: list[dict[str, Any]] = []
        try:
            pub, etag, not_mod = _stream_parse(
                STATIC_URLS,
                "energyInfrastructureSite",
                lambda el: sites.append(_parse_site(el)),
                None,
                "static inventory",
                progress,
                etag=(self.static or {}).get("etag"),
                reset=sites.clear,
            )
        except Exception as exc:  # noqa: BLE001
            if self.static:
                self.warnings.append(f"Could not refresh the charger inventory ({exc}); using the cached copy.")
                return
            raise
        if not_mod and self.static:
            self.static["fetched_epoch"] = time.time()
            self.static["fetched_at"] = _now_iso()
        else:
            if not sites:
                raise RuntimeError("The MOBI.E static feed contained no charging sites.")
            self.static = {
                "version": CACHE_VERSION,
                "publication_time": pub,
                "fetched_at": _now_iso(),
                "fetched_epoch": time.time(),
                "etag": etag,
                "sites": sites,
            }
            log.info("static inventory: %d sites, %d points", len(sites), sum(len(s["points"]) for s in sites))
        _write_json(path, self.static)

    def _load_status(self, progress: Progress | None) -> None:
        path = cache_dir() / "mobie_status.json.gz"
        if self.status is None:
            self.status = _read_json(path)
        if self._fresh(self.status, STATUS_TTL):
            return
        if progress:
            progress("Downloading live MOBI.E charger status (~40 MB)", 0.0)
        points: dict[str, list[Any]] = {}
        site_ref: list[str | None] = [None]

        def on_end(el: ET.Element) -> None:
            name = _ln(el.tag)
            if name == "reference" and site_ref[0] is None:
                site_ref[0] = el.get("id")  # first <reference> in a site status is the site
            elif name == "energyInfrastructureSiteStatus":
                site_ref[0] = None
                el.clear()

        def handle(rp: ET.Element) -> None:
            ref = _child(rp, "reference")
            pid = ref.get("id") if ref is not None else rp.get("id")
            if pid:
                points[f"{site_ref[0]}|{pid}"] = [_text(_child(rp, "status")), _parse_rates(rp)]

        try:
            pub, etag, not_mod = _stream_parse(
                STATUS_URLS, "refillPointStatus", handle, on_end, "live status", progress,
                etag=(self.status or {}).get("etag"),
                reset=lambda: (points.clear(), site_ref.__setitem__(0, None)),
            )
        except Exception as exc:  # noqa: BLE001
            if self.status:
                self.warnings.append(f"Could not refresh live status ({exc}); using the cached copy.")
                return
            raise
        if not_mod and self.status:
            self.status["fetched_epoch"] = time.time()
            self.status["fetched_at"] = _now_iso()
        else:
            if not points:
                raise RuntimeError("The MOBI.E status feed contained no charging points.")
            self.status = {
                "version": CACHE_VERSION,
                "publication_time": pub,
                "fetched_at": _now_iso(),
                "fetched_epoch": time.time(),
                "etag": etag,
                "points": points,
            }
            log.info("live status: %d points", len(points))
        _write_json(path, self.status)

    def ensure(self, progress: Progress | None = None, need_status: bool = True) -> None:
        with self._lock:
            self.warnings = []
            self._load_static(progress)
            if need_status:
                try:
                    self._load_status(progress)
                except Exception as exc:  # noqa: BLE001 — inventory still useful without live status
                    log.warning("status unavailable: %s", exc)
                    self.warnings.append(f"Live status and prices are unavailable right now ({exc}).")

    def freshness(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, data in (("inventory", self.static), ("live_status", self.status)):
            if data:
                out[key] = {
                    "published": data.get("publication_time"),
                    "published_minutes_ago": _age_min(data.get("publication_time")),
                    "fetched_at": data.get("fetched_at"),
                }
        out["refresh_policy"] = "inventory at most every 24 h, live status every 10 min (on demand)"
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out

    # -- shaping ------------------------------------------------------------------

    def _point_status(self, site_id: str, point_id: str) -> tuple[str | None, list[Any]]:
        if not self.status:
            return None, []
        hit = self.status["points"].get(f"{site_id}|{point_id}")
        return (hit[0], hit[1]) if hit else ("not in status feed", [])

    @staticmethod
    def price(rates: list[Any]) -> dict[str, Any] | None:
        now = _now_iso()
        current: dict[str, tuple[str, Any]] = {}
        other = []
        currency = None
        for pol, fee, cur, start, end, info in rates:
            if start and start[:19] > now[:19]:
                continue
            if end and end[:19] <= now[:19]:
                continue
            currency = currency or cur
            if pol in POLICY_KEYS:
                key = POLICY_KEYS[pol]
                if key not in current or (start or "") >= current[key][0]:
                    current[key] = (start or "", fee)
            else:
                other.append({"policy": pol, "description": info, "value": fee})
        if not current and not other:
            return None
        out: dict[str, Any] = {"currency": currency or "EUR"}
        out.update({k: v for k, (_, v) in current.items()})
        if other:
            out["other"] = other
        return out

    @staticmethod
    def _connector_text(c: list[Any]) -> str:
        ctype, mode, fmt, kw = c
        parts = [CONNECTOR_LABELS.get(ctype or "", ctype or "?"), MODE_LABELS.get(mode or "", mode or "")]
        if kw is not None:
            parts.append(f"{kw:g} kW")
        if fmt == "cableMode3":
            parts.append("tethered cable")
        elif fmt == "socket":
            parts.append("socket (bring your cable)")
        return " ".join(p for p in parts if p)

    def _point_out(self, site: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
        status, rates = self._point_status(site["id"], p["id"])
        kws = [c[3] for c in p["connectors"] if c[3] is not None]
        return {
            "point_id": p["id"],
            "evse_id": p.get("evse_id"),
            "status": STATUS_LABELS.get(status or "", status),
            "max_power_kw": max(kws) if kws else None,
            "connectors": [self._connector_text(c) for c in p["connectors"]],
            "price": self.price(rates),
        }

    def _site_out(self, site: dict[str, Any], points: list[dict[str, Any]], dist: float | None) -> dict[str, Any]:
        out = {
            "site_id": site["id"],
            "name": site.get("name"),
            "operator": site.get("operator"),
            "address": site.get("address"),
            "postcode": site.get("postcode"),
            "city": site.get("city"),
            "lat": site.get("lat"),
            "lon": site.get("lon"),
            "opening_hours": site.get("hours") or "not specified",
        }
        if dist is not None:
            out["distance_km"] = round(dist, 2)
        out["points"] = points
        return out

    # -- queries ------------------------------------------------------------------

    @staticmethod
    def _connector_match(p: dict[str, Any], connector: str | None) -> bool:
        if not connector:
            return True
        q = norm(connector)
        if q in ("dc", "ac"):
            want_dc = q == "dc"
            return any((c[1] == "mode4DC") == want_dc for c in p["connectors"])
        target = CONNECTOR_ALIASES.get(q) or CONNECTOR_ALIASES.get(q.replace(" ", ""))
        if target is None:
            raise ValueError(f"Unknown connector '{connector}'. Use one of: CCS, Type 2, CHAdeMO, CEE, DC, AC.")
        return any(c[0] == target for c in p["connectors"])

    def nearby(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        min_power_kw: float | None,
        connector: str | None,
        available_only: bool,
        limit: int,
    ) -> dict[str, Any]:
        assert self.static is not None
        self._connector_match({"connectors": []}, connector)  # validate early
        found = []
        for s in self.static["sites"]:
            if s.get("lat") is None or s.get("lon") is None:
                continue
            d = haversine_km(lat, lon, s["lat"], s["lon"])
            if d > radius_km:
                continue
            pts = []
            for p in s["points"]:
                kws = [c[3] for c in p["connectors"] if c[3] is not None]
                if min_power_kw and (not kws or max(kws) < min_power_kw):
                    continue
                if not self._connector_match(p, connector):
                    continue
                po = self._point_out(s, p)
                if available_only and po["status"] != "available":
                    continue
                pts.append(po)
            if pts:
                found.append((d, s, pts))
        found.sort(key=lambda x: x[0])
        sites_out = []
        for d, s, pts in found[:limit]:
            so = self._site_out(s, pts[:12], d)
            so["matching_points"] = len(pts)
            so["available_points"] = sum(1 for p in pts if p["status"] == "available")
            if len(pts) > 12:
                so["note"] = f"{len(pts) - 12} more matching points; see ev_charger_detail."
            sites_out.append(so)
        return {
            "matching_sites": len(found),
            "sites": sites_out,
        }

    def detail(self, ident: str) -> dict[str, Any]:
        assert self.static is not None
        q = ident.strip().lower()
        site_hits, point_hits = [], []
        for s in self.static["sites"]:
            if q in ((s["id"] or "").lower(), (s.get("ext_id") or "").lower()):
                site_hits.append(s)
                continue
            for p in s["points"]:
                if q in ((p["id"] or "").lower(), (p.get("evse_id") or "").lower()):
                    point_hits.append((s, p))
        if site_hits:
            s = site_hits[0]
            return self._detail_out(s, [self._point_out(s, p) for p in s["points"]])
        if len(point_hits) == 1:
            s, p = point_hits[0]
            out = self._detail_out(s, [self._point_out(s, x) for x in s["points"]])
            out["requested_point"] = p["id"]
            return out
        if point_hits:
            return {
                "error": f"Point id '{ident}' exists at several sites; pass a site id instead.",
                "candidate_sites": [s["id"] for s, _ in point_hits][:20],
            }
        raise ValueError(f"No MOBI.E site or charging point with id '{ident}'. Use a site_id, point_id or EVSE id from ev_chargers.")

    def _detail_out(self, s: dict[str, Any], points: list[dict[str, Any]]) -> dict[str, Any]:
        out = self._site_out(s, points, None)
        out.update(
            {
                "site_external_id": s.get("ext_id"),
                "area": s.get("area"),
                "directions": s.get("directions"),
                "operator_website": s.get("operator_website"),
                "operator_phone": s.get("operator_phone"),
                "authentication": s.get("auth"),
                "inventory_last_updated": s.get("updated"),
                "status_counts": {
                    st: sum(1 for p in points if p["status"] == st) for st in sorted({p["status"] or "?" for p in points})
                },
            }
        )
        # keep points last for readability
        out["points"] = out.pop("points")
        return out


_data: EVData | None = None


def data() -> EVData:
    global _data
    if _data is None:
        _data = EVData()
    return _data
