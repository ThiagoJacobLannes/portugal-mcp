"""Medicines: shortages (Infarmed) and pharmacies open or on duty — read-only."""

from __future__ import annotations

import logging
from typing import Any

from ..app import mcp, note
from . import infarmed
from . import pharmacies as ph
from .common import DISCLAIMER, HELP_LINES, STOCK_NOTE
from .common import where_to_get_help as _help

log = logging.getLogger("portugal_mcp.medicamentos")

NOTES = (
    "Helps people in Portugal who need a medicine: is it in shortage, and which pharmacies "
    "are open or on duty nearby. Informational only, not medical advice: never tell anyone to "
    "switch, stop or substitute a medicine; a pharmacist or doctor must decide that. "
    "Per-pharmacy stock is NOT public in Portugal: never claim a pharmacy has a medicine; tell "
    "the user to call the pharmacy or Linha 1400. In an emergency, 112. "
    "For 'I need medicine X in city Y', call find_medicine_near first."
)


note("medicamentos", NOTES)

_HELP_SHORT = {
    k: {kk: vv for kk, vv in v.items() if kk in ("number", "url", "what", "hours")}
    for k, v in HELP_LINES.items()
}


def _compact_availability(res: dict[str, Any], limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {"status": res["status"], "summary": res["summary"]}
    for key in ("export_suspended", "prior_notification"):
        sec = res.get(key) or {}
        if sec.get("match_count"):
            out[key] = {
                "in_force": sec.get("in_force"),
                "match_count": sec["match_count"],
                "by_strength_and_form": sec.get("by_strength_and_form", [])[: max(limit, 12)],
                "source_url": sec.get("source_url"),
            }
    mon = (res.get("monitored_shortages") or {}).get("matches") or []
    if mon:
        out["monitored_shortages"] = mon
    out["not_checked"] = res.get("not_checked")
    return out


@mcp.tool(name="medicine_availability")
def availability(medicine: str, limit: int = 15) -> dict[str, Any]:
    """Is a medicine in shortage in Portugal? Checks Infarmed's published availability information.

    Use when someone can't find a medicine, or asks whether X is in shortage ("rutura",
    "esgotado", "falta"). Searches by brand or active substance (Portuguese names work best,
    e.g. "quetiapina", "Ozempic", "ácido acetilsalicílico"; dose and form narrow it,
    e.g. "quetiapina 300 mg libertação prolongada").

    Checks, all from www.infarmed.pt (latest versions are found automatically):
      - shortages Infarmed is actively monitoring (with its circulars and advice);
      - the monthly list of medicines whose export is temporarily suspended because of
        shortage / supply risk in Portugal;
      - the quarterly prior-notification list (LNP) of medicines with availability concerns.

    status is one or more of: monitored_shortage, export_suspended, prior_notification,
    no_issue_in_published_lists. "No issue" does not prove the medicine is available:
    Infarmed's full shortage search is not queried (see not_checked).

    Args:
        medicine: Brand name or active substance, optionally with strength/form.
        limit:    Max rows returned per list (default 15).
    """
    try:
        res = infarmed.availability(medicine, limit=limit)
        if "error" in res:
            return res
        res["where_to_get_help"] = _HELP_SHORT
        res["disclaimer"] = DISCLAIMER
        return res
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        log.exception("availability failed")
        return {"error": str(exc)}


@mcp.tool(name="pharmacies")
def pharmacies(
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    on_duty_only: bool = False,
    open_now_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Pharmacies in a Portuguese municipality, with which ones are on duty today and open now.

    Use for "farmácia de serviço em X", "pharmacy open now near me", or to list pharmacies
    to call. Each pharmacy has name, address, phone, coordinates, today's hours, open_now,
    on_duty (official duty roster for the duty day 09:00-09:00) and the duty type
    (e.g. "Permanente - Aberta 24h"). Results are sorted: on duty first, then open now,
    then distance (when lat/lon are given). Stock is not public: never say a pharmacy has a
    medicine.

    Args:
        city:          Municipality or town/parish, e.g. "Lisboa", "Porto", "Cascais",
                       "Alvalade, Lisboa" (add ", <municipality>" to disambiguate a town).
        lat, lon:      Optional user position: adds distance_km and allows radius_km. With
                       no city, the municipality is found from the coordinate.
        radius_km:     Keep only pharmacies within this distance of lat/lon.
        on_duty_only:  Only pharmacies on the duty roster today.
        open_now_only: Only pharmacies open right now.
        limit:         Max pharmacies returned (default 20, max 100).
    """
    try:
        return ph.find_pharmacies(
            city=city,
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            on_duty_only=on_duty_only,
            open_now_only=open_now_only,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pharmacies failed")
        return {"error": str(exc)}


@mcp.tool()
def find_medicine_near(
    medicine: str,
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """One-shot answer to "I need medicine X in city Y — what do I do?" in Portugal.

    Runs medicine_availability(medicine) and pharmacies(city or lat/lon), and returns: the shortage
    status (compact), the pharmacies on duty today and the ones open now (nearest first when
    lat/lon are given), the help lines (Linha 1400, SNS 24, 112) and next steps. It never
    says a pharmacy has the medicine: stock is not public.

    Args:
        medicine: Brand name or active substance, e.g. "quetiapina", "Brufen 400".
        city:     Municipality or town, e.g. "Lisboa". Or give lat/lon instead.
        lat, lon: Optional user position (sorts pharmacies by distance).
        limit:    Max pharmacies returned (default 8).
    """
    out: dict[str, Any] = {"medicine": medicine}
    try:
        res = infarmed.availability(medicine, limit=limit)
        out["availability"] = res if "error" in res else _compact_availability(res, limit)
    except Exception as exc:  # noqa: BLE001
        log.exception("availability failed")
        out["availability"] = {"error": str(exc)}

    try:
        p = ph.find_pharmacies(city=city, lat=lat, lon=lon, limit=100)
        if "error" in p:
            out["pharmacies"] = p
        else:
            useful = [x for x in p["pharmacies"] if x["on_duty"] or x["open_now"]]
            if lat is not None and lon is not None:
                useful.sort(key=lambda x: (not x["on_duty"], x["distance_km"] if x["distance_km"] is not None else 9999))
            out["pharmacies"] = {
                "municipality": p["municipality"],
                "district": p["district"],
                "on_duty_today": p["on_duty_in_municipality"],
                "duty_validity": p["duty_validity"],
                "on_duty_or_open_now": [
                    {
                        k: x[k]
                        for k in ("name", "address", "parish", "phone", "hours_today", "open_now",
                                  "on_duty", "duty", "distance_km", "lat", "lon", "url")
                    }
                    for x in useful[: max(1, min(int(limit), 30))]
                ],
                "source": p["source"],
            }
    except Exception as exc:  # noqa: BLE001
        log.exception("pharmacies failed")
        out["pharmacies"] = {"error": str(exc)}

    status = (out.get("availability") or {}).get("status") or []
    steps = [
        STOCK_NOTE,
        "Call Linha 1400 (24h) and ask them to find a pharmacy that has it; online form at "
        "https://www.1400safe.pt/.",
    ]
    if any(s in status for s in ("monitored_shortage", "export_suspended")):
        steps.append(
            "The medicine has a reported supply problem. Ask the pharmacist whether another "
            "brand or pack of the same medicine (same substance, strength and form) is "
            "available; any change of medicine must be decided by a pharmacist or doctor."
        )
    steps.append(
        "If you cannot get it and you need advice about your treatment, call SNS 24 "
        "(808 24 24 24) or your doctor. In an emergency call 112."
    )
    out["next_steps"] = steps
    out["where_to_get_help"] = _HELP_SHORT
    out["note"] = STOCK_NOTE
    out["disclaimer"] = DISCLAIMER
    return out


@mcp.tool(name="medicine_help")
def where_to_get_help() -> dict[str, Any]:
    """Verified help lines in Portugal for medicine problems: 112, SNS 24, Linha 1400, Infarmed.

    Use when someone can't find a medicine, needs urgent advice, or asks who to call. Each
    line has its number, what it is for, hours where published, and the page it was checked on.
    """
    return _help()

