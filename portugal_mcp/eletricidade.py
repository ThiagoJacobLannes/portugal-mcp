"""Electricity: planned power cuts from E-REDES (open data) and the Portuguese day-ahead market
price from OMIE, the Iberian market operator (free use with attribution, unaltered)."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .app import mcp, note
from .common import clamp, fail, get_json, http, norm

log = logging.getLogger("portugal_mcp.eletricidade")

TZ = ZoneInfo("Europe/Lisbon")
EXPORT = "https://e-redes.opendatasoft.com/api/explore/v2.1/catalog/datasets/network-scheduling-work/exports/json"
SOURCE = "E-REDES open data: scheduled maintenance on the distribution network (e-redes.opendatasoft.com, CC BY 4.0)"
TTL = 3600

note(
    "eletricidade",
    "power_cuts lists the PLANNED electricity interruptions E-REDES has announced (maintenance and "
    "works), usually for the next two weeks, by postal code, municipality or parish. It is not a "
    "list of live faults: for an outage happening now, point to E-REDES (808 100 100 or "
    "balcaodigital.e-redes.pt). Give the date, start and end time and say it is planned. "
    "electricity_price gives OMIE's day-ahead wholesale price for Portugal in €/MWh, per 15 minutes: "
    "it is not the retail tariff on a bill, though indexed ('dynamic') tariffs follow it. Cite OMIE.",
)

OMIE_FILE = "https://www.omie.es/es/file-download"
OMIE_LIST = "https://www.omie.es/es/file-access-list"
SRC_OMIE = "OMIE - Iberian electricity market operator (omie.es), Portugal day-ahead marginal price; source: OMIE"

_cache: tuple[float, list[dict[str, Any]]] | None = None
_lock = threading.Lock()


def _rows() -> list[dict[str, Any]]:
    global _cache
    with _lock:
        if _cache and time.time() - _cache[0] < TTL:
            return _cache[1]
    rows = get_json(EXPORT, timeout=60)
    with _lock:
        _cache = (time.time(), rows)
    return rows


def _dt(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)).replace(tzinfo=TZ)
    except ValueError:
        return None


@mcp.tool()
def power_cuts(
    postal_code: str | None = None,
    municipality: str | None = None,
    parish: str | None = None,
    days: int = 14,
    limit: int = 30,
) -> dict[str, Any]:
    """Planned power cuts (E-REDES maintenance works) for a postal code, municipality or parish.

    Args:
        postal_code: "2795-082" for that street, or "2795" for the whole postal area.
        municipality: e.g. "Sintra", "Vila Nova de Gaia".
        parish: e.g. "Algés" (matches part of the parish name).
        days: how far ahead to look (max 60).
    With nothing given, returns a national summary by municipality.
    """
    try:
        now = datetime.now(TZ)
        horizon = clamp(days, 1, 60, 14)
        n = clamp(limit, 1, 100, 30)
        rows = []
        for r in _rows():
            start, end = _dt(r.get("startdatetime")), _dt(r.get("enddatetime"))
            if not start or not end or end < now or (start - now).days >= horizon:
                continue
            rows.append((start, end, r))
        updated = max((str(r.get("updatedatetime") or "") for _, _, r in rows), default=None)

        if not (postal_code or municipality or parish):
            by: dict[str, int] = {}
            for _, _, r in rows:
                by[r.get("municipality") or "?"] = by.get(r.get("municipality") or "?", 0) + 1
            top = sorted(by.items(), key=lambda kv: -kv[1])
            return {
                "planned_cuts_postal_codes": len(rows),
                "municipalities": len(by),
                "by_municipality": [{"municipality": k, "postal_codes_affected": v} for k, v in top[:n]],
                "data_updated": updated,
                "note": "Planned works only. Ask again with a postal code or municipality for dates and times.",
                "source": SOURCE,
            }

        exact = None
        if postal_code:
            m = re.fullmatch(r"\s*(\d{4})(?:\s*-\s*(\d{3}))?\s*", postal_code)
            if not m:
                raise ValueError("postal_code is 'XXXX-XXX' or the first four digits 'XXXX'")
            cp4, exact = m.group(1), f"{m.group(1)}-{m.group(2)}" if m.group(2) else None
            rows = [x for x in rows if str(x[2].get("zipcode", "")).startswith(cp4)]
        if municipality:
            q = norm(municipality)
            rows = [x for x in rows if q in norm(x[2].get("municipality"))]
        if parish:
            q = norm(parish)
            rows = [x for x in rows if q in norm(x[2].get("parish"))]

        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for start, end, r in sorted(rows, key=lambda x: x[0]):
            k = (start, end, r.get("municipality"), r.get("parish"))
            g = groups.setdefault(k, {
                "date": start.date().isoformat(),
                "from": start.strftime("%H:%M"),
                "to": end.strftime("%H:%M") if end.date() == start.date() else end.strftime("%Y-%m-%d %H:%M"),
                "duration_min": int(r["durationallocation"]) if str(r.get("durationallocation", "")).isdigit() else None,
                "municipality": r.get("municipality"),
                "parish": r.get("parish"),
                "postal_codes": [],
            })
            g["postal_codes"].append(r.get("zipcode"))
        cuts = list(groups.values())
        out: dict[str, Any] = {"planned_cuts": cuts[:n], "found": len(cuts), "days_ahead": horizon, "data_updated": updated}
        if exact:
            hit = [c for c in cuts if exact in c["postal_codes"]]
            out["your_postal_code_affected"] = bool(hit)
            if hit:
                out["planned_cuts"] = hit + [c for c in cuts if c not in hit][: max(0, n - len(hit))]
            out["note"] = (
                f"{exact} is on the list: see the first entries." if hit else
                f"No planned cut announced for {exact}; the list shows others in the same postal area ({exact[:4]})."
            )
        if not cuts:
            out["note"] = "No planned power cuts announced there in this period."
        out["source"] = SOURCE
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "power_cuts", exc)


_prices: dict[str, list[tuple[int, float]]] = {}


def _omie_day(day: date) -> list[tuple[int, float]] | None:
    """[(period, €/MWh)] for one delivery day, exactly as OMIE publishes it, or None if not published."""
    key = day.isoformat()
    if key in _prices:
        return _prices[key]
    r = http().get(OMIE_FILE, params={"parents": "marginalpdbcpt", "filename": f"marginalpdbcpt_{day:%Y%m%d}.1"}, timeout=30)
    if r.status_code == 404 or "MARGINALPDBCPT" not in r.text[:40]:
        return None
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines():
        parts = line.strip().rstrip(";").split(";")
        if len(parts) >= 5 and parts[0].isdigit():
            rows.append((int(parts[3]), float(parts[4])))
    if day < date.today():  # past days never change
        _prices[key] = rows
    return rows


def _latest_published() -> date | None:
    r = http().get(OMIE_LIST, params={"parents[0]": "/", "parents[1]": "Mercado Diario", "parents[2]": "1. Precios",
                                       "dir": "Precios horarios del mercado diario en Portugal", "realdir": "marginalpdbcpt"}, timeout=30)
    days = sorted(set(re.findall(r"marginalpdbcpt_(\d{8})\.1", r.text)))
    return datetime.strptime(days[-1], "%Y%m%d").date() if days else None


def _hhmm(period: int, n: int) -> str:
    minutes = (period - 1) * (1440 // n)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@mcp.tool()
def electricity_price(day: str | None = None, cheapest: int = 4) -> dict[str, Any]:
    """Portugal's day-ahead wholesale electricity price (OMIE), per 15-minute period, in €/MWh.

    Useful for indexed ("dynamic") tariffs: when is electricity cheapest to run the washing
    machine or charge the car. Not the retail price on a normal bill.
    Args:
        day: ISO date, e.g. "2026-11-10". Default: the most recent day OMIE has published.
        cheapest: also list this many cheapest periods (max 12).
    """
    try:
        want = date.fromisoformat(day) if day else None
        got = _omie_day(want) if want else None
        note_ = None
        if got is None:
            latest = _latest_published()
            if latest is None:
                raise RuntimeError("OMIE's price files could not be listed right now")
            if want:
                note_ = (f"OMIE has not published the {want.isoformat()} file yet (its download files lag the market "
                         f"results by about a day); showing {latest.isoformat()}, the latest published. "
                         "Today's and tomorrow's prices are on omie.es.")
            want, got = latest, _omie_day(latest)
        if not got:
            raise RuntimeError(f"no OMIE prices for {want}")
        n = len(got)
        values = [v for _, v in got]
        periods = [{"from": _hhmm(p, n), "eur_mwh": v} for p, v in got]
        k = clamp(cheapest, 0, 12, 4)
        out: dict[str, Any] = {
            "day": want.isoformat(),
            "unit": "€/MWh (wholesale, before taxes, network access and supplier margin)",
            "periods": periods,
            "computed_from_omie_values": {
                "average": round(sum(values) / n, 2), "min": min(values), "max": max(values),
                "cheapest": sorted(periods, key=lambda x: x["eur_mwh"])[:k] if k else [],
                "eur_kwh_average": round(sum(values) / n / 1000, 4),
            },
            "source": SRC_OMIE,
        }
        if note_:
            out["note"] = note_
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "electricity_price", exc)
