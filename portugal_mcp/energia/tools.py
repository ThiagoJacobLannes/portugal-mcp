"""Energy: cheapest fuel (DGEG) and nearby EV chargers (MOBI.E) — read-only."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server.fastmcp import Context

from ..app import mcp
from . import ev, fuel
from .common import clamp

log = logging.getLogger("portugal_mcp.energia")


_fuel: fuel.DGEGClient | None = None


def fuel_client() -> fuel.DGEGClient:
    global _fuel
    if _fuel is None:
        _fuel = fuel.DGEGClient()
    return _fuel


async def _run(fn: Callable[..., Any], ctx: Context | None = None) -> Any:
    """Run blocking work in a thread; forward progress to the client when it asked for it."""

    def progress(message: str, fraction: float | None) -> None:
        if ctx is None:
            return
        try:
            anyio.from_thread.run(ctx.report_progress, round((fraction or 0) * 100, 1), 100.0, message)
        except TypeError:  # older mcp without the `message` argument
            try:
                anyio.from_thread.run(ctx.report_progress, round((fraction or 0) * 100, 1), 100.0)
            except Exception:  # noqa: BLE001, S110 — progress is best effort
                pass
        except Exception:  # noqa: BLE001, S110
            pass

    return await anyio.to_thread.run_sync(lambda: fn(progress))


def _fail(tool: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValueError):  # bad input: no traceback needed
        log.warning("%s: %s", tool, exc)
    else:
        log.exception("%s failed", tool)
    return {"error": str(exc)}


# ---------------------------------------------------------------------------------
# Fuel (DGEG)
# ---------------------------------------------------------------------------------


@mcp.tool()
async def fuel_prices(
    fuel_type: str,
    district: str | None = None,
    municipality: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 10.0,
    brand: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Cheapest fuel stations in Portugal for one fuel type, cheapest first (official DGEG prices).

    Use for "where is diesel/petrol/LPG cheapest in Lisboa / near me / in Braga?". Give an
    area as a district, a municipality, or a point (lat + lon, with radius_km); with no area
    it searches all of Portugal. Names are case- and accent-insensitive ("evora" works).

    Args:
        fuel_type:    A DGEG fuel name or id, e.g. "Gasóleo simples" (regular diesel, 2101),
                      "Gasolina simples 95" (regular 95 petrol, 3201), "Gasolina 98", "GPL Auto"
                      (LPG), or plain words: "diesel", "petrol", "95", "98", "lpg", "cng".
                      See fuel_types() for the full list.
        district:     Portuguese district, e.g. "Lisboa", "Porto", "Faro".
        municipality: Municipality (concelho), e.g. "Sintra", "Vila Nova de Gaia". Overrides district.
        lat, lon:     A point (WGS84). Results get distance_km and are limited to radius_km.
        radius_km:    Search radius around lat/lon (default 10).
        brand:        Only this brand (substring match), e.g. "Galp", "Prio", "Intermarché".
        limit:        How many stations to return (default 10, max 50).

    Each station: price_eur (per litre, or per kg/m3 for gas), brand, name, address,
    municipality, distance_km (with lat/lon), coordinates and last_update (the station's
    last price update). Also returns how many stations matched and the min/median/max price.
    Always mention the source (DGEG) and the last_update when quoting prices.
    """
    try:
        return await _run(
            lambda _p: fuel.fuel_prices(
                fuel_client(),
                fuel_type,
                district=district,
                municipality=municipality,
                lat=lat,
                lon=lon,
                radius_km=max(0.1, float(radius_km)),
                brand=brand,
                limit=clamp(limit, 1, 50),
            )
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        return _fail("fuel_prices", exc)


@mcp.tool()
async def fuel_station_detail(station_id: int) -> dict[str, Any]:
    """Full details of one fuel station: every fuel it sells with price and last update,
    opening hours, services (car wash, shop, tyre air...) and payment methods.

    Use after fuel_prices, with a station `id` from its results.
    """
    try:
        return await _run(lambda _p: fuel.station_detail(fuel_client(), int(station_id)))
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        return _fail("fuel_station_detail", exc)


@mcp.tool()
async def fuel_types() -> dict[str, Any]:
    """List DGEG fuel types (id, name, unit) and the districts accepted by fuel_prices.

    Only needed when unsure which fuel name to pass; fuel_prices also understands
    "diesel", "petrol", "95", "98", "lpg".
    """
    try:

        def work(_p: Any) -> dict[str, Any]:
            c = fuel_client()
            return {"fuel_types": c.fuel_types(), "districts": sorted(c.districts()), "source": fuel.SOURCE}

        return await _run(work)
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        return _fail("fuel_types", exc)


# ---------------------------------------------------------------------------------
# EV charging (MOBI.E via NAP)
# ---------------------------------------------------------------------------------


@mcp.tool()
async def ev_chargers(
    lat: float,
    lon: float,
    radius_km: float = 5.0,
    min_power_kw: float | None = None,
    connector: str | None = None,
    available_only: bool = False,
    limit: int = 10,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Public EV charging sites near a point in Portugal (MOBI.E network), nearest first,
    with live status and the price the network publishes for each charging point.

    Use for "where can I charge near X?", "fast chargers near me", "is there a free CCS
    charger nearby?". Needs coordinates; if the user gives a place name, use known
    coordinates for it. Covers mainland Portugal, Azores and Madeira.

    Args:
        lat, lon:       Point to search around (WGS84).
        radius_km:      Search radius (default 5, max 100).
        min_power_kw:   Only points that can deliver at least this power, e.g. 50 for fast DC.
        connector:      "CCS" (CCS2), "Type 2", "CHAdeMO", "CEE", or "DC" / "AC".
        available_only: Only points whose live status is "available" right now.
        limit:          Max sites to return (default 10, max 50).

    Each site: operator, address, distance_km, opening hours and its matching points, each
    with status (available, in use, out of order...), max power, connectors and price
    (per_kwh / per_minute / per_session in EUR, from the feed). IMPORTANT: these are the
    charging operator's published fees and may not be the whole bill: see `price_note` and
    tell the user. `data_freshness` says how old the inventory and live status are.
    The first call of the day downloads ~220 MB from MOBI.E and can take 1-3 minutes.
    """
    try:

        def work(progress: ev.Progress) -> dict[str, Any]:
            d = ev.data()
            d.ensure(progress)
            res = d.nearby(
                float(lat),
                float(lon),
                min(max(0.1, float(radius_km)), 100.0),
                min_power_kw,
                connector,
                bool(available_only),
                clamp(limit, 1, 50),
            )
            res["data_freshness"] = d.freshness()
            res["price_note"] = ev.PRICE_NOTE
            res["source"] = ev.SOURCE
            if not res["sites"]:
                res["note"] = "Nothing matched. Try a larger radius_km or fewer filters."
            return res

        return await _run(work, ctx)
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        return _fail("ev_chargers", exc)


@mcp.tool()
async def ev_charger_detail(
    site_or_point_id: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Everything about one MOBI.E charging site: all its points with live status, power,
    connectors and price, plus operator contact, opening hours and accepted authentication
    (app, RFID card, credit card...).

    Use after ev_chargers. Accepts a site_id (e.g. "EZC-AMD-00051"), a point_id
    (e.g. "AMD-00051-1") or an EVSE id (e.g. "PT*EZC*E*AMD*00051*1").
    """
    try:

        def work(progress: ev.Progress) -> dict[str, Any]:
            d = ev.data()
            d.ensure(progress)
            res = d.detail(site_or_point_id)
            res["data_freshness"] = d.freshness()
            res["price_note"] = ev.PRICE_NOTE
            res["source"] = ev.SOURCE
            return res

        return await _run(work, ctx)
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        return _fail("ev_charger_detail", exc)


def refresh() -> None:
    """Warm the MOBI.E cache (the first EV load takes ~80 s)."""
    ev.data().ensure(lambda msg, _f: None)
