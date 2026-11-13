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
    "it is not the retail tariff on a bill, though indexed ('dynamic') tariffs follow it. Cite OMIE. "
    "electricity_grid gives a day's national consumption, peak and wind/solar/hydro generation on the "
    "distribution grid (one day behind); electricity_local gives a municipality's or postal area's "
    "monthly consumption and how often the power went out there. Mainland only (E-REDES).",
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


ODS = "https://e-redes.opendatasoft.com/api/explore/v2.1/catalog/datasets"
SRC_GRID = "E-REDES open data: national consumption and generation on the distribution grid, per 15 minutes (e-redes.opendatasoft.com, CC BY 4.0)"
SRC_LOCAL = "E-REDES open data: billed consumption per municipality and postal code, and service-continuity indicators (e-redes.opendatasoft.com, CC BY 4.0)"
DS_CONS = "consumo-total-nacional"
DS_PROD = "energia-produzida-total-nacional"
DS_INJ = "energia-injetada-na-rede-de-distribuicao"
DS_MUNI = "3-consumos-faturados-por-municipio-ultimos-10-anos"
DS_CP = "02-consumos-faturados-por-codigo-postal-ultimos-5-anos"
DS_QOS = "12-continuidade-de-servico-indicadores-gerais-de-continuidade-de-servico"


def _ods(dataset: str, **params: Any) -> list[dict[str, Any]]:
    return get_json(f"{ODS}/{dataset}/records", params).get("results") or []


def _gwh(kwh: Any) -> float | None:
    return round(float(kwh) / 1e6, 2) if kwh is not None else None


def latest_complete_day(last: dict[str, Any]) -> date:
    """The newest row is often the first quarter-hour of a day; the day before is then the last full one."""
    d = date.fromisoformat(last["date"])
    return d if last.get("time") == "23:45" else date.fromordinal(d.toordinal() - 1)


def shape_grid(day: date, cons: dict[str, Any], prod: dict[str, Any], inj: dict[str, Any]) -> dict[str, Any]:
    renewable = sum(float(inj.get(k) or 0) for k in ("wind", "solar", "hydro"))
    local = float(inj.get("total") or 0)
    out: dict[str, Any] = {
        "day": day.isoformat(),
        "consumption_gwh": _gwh(cons.get("total")),
        "consumption_by_voltage_gwh": {
            "low (homes, small shops)": _gwh(cons.get("bt")),
            "medium": _gwh(cons.get("mt")),
            "high": _gwh(cons.get("at")),
            "very high": _gwh(cons.get("mat")),
        },
        "peak_mw": round(float(cons["peak"]) * 4 / 1000) if cons.get("peak") is not None else None,
        "supply_gwh": {
            "from the transmission grid (REN)": _gwh(prod.get("dgm")),
            "generated on the distribution grid": _gwh(prod.get("pre")),
        },
        "distribution_grid_generation_gwh": {
            "wind": _gwh(inj.get("wind")),
            "solar": _gwh(inj.get("solar")),
            "hydro": _gwh(inj.get("hydro")),
            "cogeneration": _gwh(inj.get("cogen")),
            "other": _gwh(inj.get("other")),
        },
        "renewable_share_of_distribution_generation": round(renewable / local, 3) if local else None,
    }
    if int(cons.get("n") or 0) < 92:
        out["warning"] = f"Only {cons.get('n')} of 96 quarter-hours are published for this day; totals are partial."
    return out


@mcp.tool()
def electricity_grid(day: str | None = None) -> dict[str, Any]:
    """How much electricity mainland Portugal used on a day, the peak, and how much wind, solar and
    hydro the distribution grid generated (E-REDES, 15-minute data, usually one day behind).

    Args:
        day: "YYYY-MM-DD" (default: the latest complete day, usually yesterday).
    Big power stations feed the transmission grid (REN) and show here only as one total.
    """
    try:
        if day:
            d = date.fromisoformat(day)
        else:
            last = _ods(DS_CONS, order_by="datahora desc", limit=1)
            if not last:
                raise ValueError("E-REDES returned no consumption data")
            d = latest_complete_day(last[0])
        where = f"date=date'{d.isoformat()}'"
        cons = _ods(DS_CONS, where=where, limit=1, select="sum(total) as total,sum(bt) as bt,sum(mt) as mt,sum(at) as at,sum(mat) as mat,max(total) as peak,count(*) as n")
        if not cons or not cons[0].get("n"):
            raise ValueError(f"no data for {d.isoformat()} (the series starts in 2023 and runs about a day behind)")
        prod = _ods(DS_PROD, where=where, limit=1, select="sum(dgm) as dgm,sum(pre) as pre")
        inj = _ods(DS_INJ, where=where, limit=1, select="sum(eolica) as wind,sum(fotovoltaica) as solar,sum(hidrica) as hydro,sum(cogeracao) as cogen,sum(outras_tecnologias) as other,sum(rede_dist) as total")
        return {**shape_grid(d, cons[0], (prod or [{}])[0], (inj or [{}])[0]), "source": SRC_GRID}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "electricity_grid", exc)


def _r(v: Any, nd: int) -> float:
    return round(float(v or 0), nd)


def _continuity(dico: str) -> dict[str, Any] | None:
    rows = _ods(DS_QOS, where=f"codigo_concelho='{dico}' and zona_rqs='Concelho'", order_by="ano desc", limit=5)
    if not rows:
        return None
    year = rows[0]["ano"]
    nat = _ods(DS_QOS, where=f"zona_rqs='Concelho' and ano='{year}'", limit=1, select="avg(saidi_bt_min) as saidi,avg(saifi_bt_num) as saifi")
    nat = nat[0] if nat else {}
    return {
        "year": year,
        "minutes_without_power_per_customer": _r(rows[0].get("saidi_bt_min"), 1),
        "interruptions_per_customer": _r(rows[0].get("saifi_bt_num"), 2),
        "short_interruptions_medium_voltage": _r(rows[0].get("maifi_mt_num"), 2),
        "mainland_municipality_average": {
            "minutes_without_power_per_customer": _r(nat.get("saidi"), 1),
            "interruptions_per_customer": _r(nat.get("saifi"), 2),
        },
        "previous_years": [
            {"year": r["ano"], "minutes_without_power_per_customer": _r(r.get("saidi_bt_min"), 1), "interruptions_per_customer": _r(r.get("saifi_bt_num"), 2)}
            for r in rows[1:]
        ],
        "meaning": "Per low-voltage customer (homes, small shops) over the year: long interruptions (over 3 minutes), planned and unplanned.",
    }


@mcp.tool()
def electricity_local(municipality: str | None = None, postal_code: str | None = None, months: int = 12) -> dict[str, Any]:
    """Electricity in one place (mainland Portugal): billed consumption per month, and how often and
    for how long the power went out there in recent years compared with the average municipality.

    Args:
        municipality: a concelho, e.g. "Sintra".
        postal_code: "2710" or "2710-123" (consumption is published per 4-digit area); its
            municipality is used for the outage figures.
        months: how many recent months of consumption (max 60).
    """
    try:
        from . import geo

        if not municipality and not postal_code:
            raise ValueError("give a municipality or a postal code")
        n = clamp(months, 1, 60, 12)
        out: dict[str, Any] = {}
        if postal_code:
            cp4 = re.sub(r"\D", "", postal_code)[:4]
            if len(cp4) != 4:
                raise ValueError("postal codes look like '2710' or '2710-123'")
            rows = _ods(DS_CP, where=f"codigopostal='{cp4}'", order_by="date desc", limit=n)
            out["postal_area"] = cp4
            out["consumption_mwh_by_month"] = [{"month": r["date"], "mwh": _r((r.get("energiaativa") or 0) / 1000, 1)} for r in rows]
            if not municipality:
                pc = geo.postal_code(cp4)
                municipality = pc.get("municipality") if isinstance(pc, dict) else None
        if municipality:
            m = geo.municipality_code(municipality)
            out["municipality"] = {"name": m["name"], "ine_code": m["dico"]}
            if m["dico"][:2] in ("31", "32") or m["dico"][:1] == "4":
                out["warning"] = "The Azores and Madeira have their own utilities (EDA, EEM); E-REDES covers mainland Portugal only."
            else:
                if not postal_code:
                    rows = _ods(DS_MUNI, where=f"coddistritoconcelho='{m['dico']}'", group_by="data", select="data,sum(energia_ativa_kwh) as kwh", order_by="data desc", limit=n)
                    out["consumption_mwh_by_month"] = [{"month": str(r["data"])[:7], "mwh": _r((r.get("kwh") or 0) / 1000, 1)} for r in rows]
                out["power_outages"] = _continuity(m["dico"])
        out["source"] = SRC_LOCAL
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "electricity_local", exc)
