"""Client for DGEG's "Preços dos Combustíveis Online" JSON API (no key needed).

Base: https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb
- GetTiposCombustiveis, GetDistritos, GetMunicipios, GetMarcas: lookup tables
- PesquisarPostos?idsTiposComb=&idDistrito=&idsMunicipios=&qtdPorPagina=&pagina=
  stations selling one fuel, with price ("1,899 €"), coordinates and last update
- GetDadosPosto?id=: one station with address, opening hours, services and all prices

Endpoints as used by the Home Assistant integration
netsoft-ruidias/ha-custom-component-precoscombustiveis and GeiserX/Pumperly.

DGEG's terms: the data is free to use; reuse must name DGEG as the source, show the
date-time of the last price update, and reproduce the information faithfully.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import httpx

from .common import USER_AGENT, haversine_km, median, norm, resolve_name

log = logging.getLogger("portugal_mcp.energia")

API = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb"

SOURCE = (
    "DGEG - Preços dos Combustíveis Online (precoscombustiveis.dgeg.gov.pt). "
    "Prices are posted by each station's operator; last_update is when that station last "
    "updated the price (Portugal local time)."
)

LOOKUP_TTL = 24 * 3600
SEARCH_TTL = 10 * 60

# Everyday names -> DGEG fuel type id.
FUEL_ALIASES = {
    "diesel": 2101,
    "gasoleo": 2101,
    "gasoleo simples": 2101,
    "diesel premium": 2105,
    "diesel especial": 2105,
    "gasoleo aditivado": 2105,
    "petrol": 3201,
    "gasoline": 3201,
    "gasolina": 3201,
    "unleaded": 3201,
    "unleaded 95": 3201,
    "gasolina 95": 3201,
    "95": 3201,
    "e5": 3201,
    "sp95": 3201,
    "98": 3400,
    "unleaded 98": 3400,
    "sp98": 3400,
    "gasolina aditivada": 3205,
    "lpg": 1120,
    "gpl": 1120,
    "autogas": 1120,
    "cng": 1143,
    "gnc": 1143,
    "lng": 1142,
    "gnl": 1142,
    "b15": 2115,
    "biodiesel": 2115,
}


# English / old names -> DGEG district or municipality name.
PLACE_ALIASES = {"lisbon": "Lisboa", "oporto": "Porto"}
UNITS = {"litro": "litre", "kg": "kg", "m3": "m3"}


def parse_price(raw: Any) -> float | None:
    """'1,899 €' / '2,029 €/litro' -> 1.899"""
    if raw is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(raw))
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", "."))
    except ValueError:
        return None
    return v if v > 0 else None


class DGEGClient:
    def __init__(self, base_url: str = API) -> None:
        self.base = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=httpx.Timeout(45.0, connect=15.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    # -- plumbing -----------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None, ttl: float = LOOKUP_TTL) -> Any:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        r = self._http.get(f"{self.base}/{path}", params=params)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict) or not body.get("status"):
            raise RuntimeError(f"DGEG API {path} failed: {body.get('mensagem') if isinstance(body, dict) else body!r}")
        res = body.get("resultado")
        with self._lock:
            self._cache[key] = (time.time(), res)
        return res

    # -- lookups ------------------------------------------------------------------

    def fuel_types(self) -> list[dict[str, Any]]:
        rows = self._get("GetTiposCombustiveis") or []
        return [
            {"id": r["Id"], "name": r["Descritivo"], "unit": r.get("UnidadeMedida"), "road_fuel": r.get("fl_rodoviario")}
            for r in rows
            if r.get("fl_ativo", True)
        ]

    def districts(self) -> dict[str, int]:
        return {r["Descritivo"]: r["Id"] for r in self._get("GetDistritos") or []}

    def municipalities(self) -> dict[str, tuple[int, int, str]]:
        """name -> (municipality id, district id, district name)"""
        out = {}
        for r in self._get("GetMunicipios") or []:
            out[r["Descritivo"]] = (r["Id"], r.get("IdDistrito"), (r.get("Distrito") or {}).get("Descritivo"))
        return out

    def resolve_fuel(self, fuel_type: str | int) -> dict[str, Any]:
        types = self.fuel_types()
        by_id = {t["id"]: t for t in types}
        s = str(fuel_type).strip()
        if s.isdigit() and int(s) in by_id:
            return by_id[int(s)]
        q = norm(s)
        for t in types:
            if norm(t["name"]) == q:
                return t
        if q in FUEL_ALIASES and FUEL_ALIASES[q] in by_id:
            return by_id[FUEL_ALIASES[q]]
        words = set(q.split())
        hits = [t for t in types if words and words <= set(norm(t["name"]).split())]
        if len(hits) > 1:
            simple = [t for t in hits if "simples" in norm(t["name"])]
            hits = simple if len(simple) == 1 else hits
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(t["name"] for t in (hits or types))
        raise ValueError(f"Unknown or ambiguous fuel type '{fuel_type}'. Options: {names}.")

    # -- queries ------------------------------------------------------------------

    def search(self, fuel_id: int, district_id: int | None = None, municipality_id: int | None = None) -> list[dict]:
        params = {
            "idsTiposComb": fuel_id,
            "idMarca": "",
            "idTipoPosto": "",
            "idDistrito": district_id or "",
            "idsMunicipios": municipality_id or "",
            "qtdPorPagina": 9999,
            "pagina": 1,
        }
        return self._get("PesquisarPostos", params, ttl=SEARCH_TTL) or []

    def station(self, station_id: int) -> dict:
        res = self._get("GetDadosPosto", {"id": int(station_id)}, ttl=SEARCH_TTL)
        if not res:
            raise ValueError(f"No fuel station with id {station_id}.")
        return res


def station_row(s: dict[str, Any], dist: float | None) -> dict[str, Any]:
    row = {
        "id": s.get("Id"),
        "name": (s.get("Nome") or "").strip(),
        "brand": (s.get("Marca") or "").strip() or None,
        "price_eur": parse_price(s.get("Preco")),
        "address": (s.get("Morada") or "").strip() or None,
        "locality": (s.get("Localidade") or "").strip() or None,
        "postcode": s.get("CodPostal"),
        "municipality": s.get("Municipio"),
        "district": s.get("Distrito"),
        "station_type": s.get("TipoPosto"),
        "lat": s.get("Latitude"),
        "lon": s.get("Longitude"),
        "last_update": s.get("DataAtualizacao"),
    }
    if dist is not None:
        row["distance_km"] = round(dist, 2)
    return row


def fuel_prices(
    client: DGEGClient,
    fuel_type: str,
    district: str | None = None,
    municipality: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 10.0,
    brand: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    if (lat is None) != (lon is None):
        raise ValueError("Give both lat and lon, or neither.")
    fuel = client.resolve_fuel(fuel_type)

    district_id = muni_id = None
    area = []
    if municipality:
        munis = client.municipalities()
        municipality = PLACE_ALIASES.get(norm(municipality), municipality)
        muni_id, district_id, dname = resolve_name(municipality, munis, "municipality")
        area.append(f"municipality {next(k for k, v in munis.items() if v[0] == muni_id)} ({dname})")
    elif district:
        dists = client.districts()
        district = PLACE_ALIASES.get(norm(district), district)
        district_id = resolve_name(district, dists, "district")
        area.append(f"district {next(k for k, v in dists.items() if v == district_id)}")
    if lat is not None:
        area.append(f"within {radius_km:g} km of {lat:.5f},{lon:.5f}")

    raw = client.search(fuel["id"], district_id, muni_id)

    rows = []
    for s in raw:
        price = parse_price(s.get("Preco"))
        if price is None:
            continue
        if brand and norm(brand) not in norm(s.get("Marca")):
            continue
        dist = None
        if lat is not None:
            slat, slon = s.get("Latitude"), s.get("Longitude")
            if slat is None or slon is None:
                continue
            dist = haversine_km(lat, lon, float(slat), float(slon))
            if dist > radius_km:
                continue
        rows.append((price, dist if dist is not None else 0.0, s, dist))
    rows.sort(key=lambda r: (r[0], r[1]))

    prices = [r[0] for r in rows]

    out: dict[str, Any] = {
        "fuel_type": fuel["name"],
        "fuel_type_id": fuel["id"],
        "unit": f"EUR per {UNITS.get(fuel.get('unit') or '', fuel.get('unit'))}" if fuel.get("unit") else "EUR",
        "area": ", ".join(area) or "all of Portugal",
        "brand_filter": brand,
        "matching_stations": len(rows),
        "price_stats": (
            {"min": prices[0], "median": median(prices), "max": prices[-1]} if prices else None
        ),
        "stations": [station_row(s, d) for _, _, s, d in rows[:limit]],
        "source": SOURCE,
    }
    if not rows:
        out["note"] = "No station matched. Try a larger radius, another area, or drop the brand filter."
    return out


def station_detail(client: DGEGClient, station_id: int) -> dict[str, Any]:
    d = client.station(station_id)
    m = d.get("Morada") or {}
    h = d.get("HorarioPosto") or {}
    return {
        "id": int(station_id),
        "name": d.get("Nome"),
        "brand": d.get("Marca"),
        "station_type": d.get("TipoPosto"),
        "usage": d.get("Utilizacao"),
        "address": m.get("Morada"),
        "locality": m.get("Localidade"),
        "postcode": m.get("CodPostal"),
        "municipality": m.get("Municipio"),
        "district": m.get("Distrito"),
        "lat": m.get("Latitude"),
        "lon": m.get("Longitude"),
        "opening_hours": {
            "weekdays": h.get("DiasUteis"),
            "saturday": h.get("Sabado"),
            "sunday": h.get("Domingo"),
            "holidays": h.get("Feriado"),
        },
        "fuels": [
            {
                "fuel_type": f.get("TipoCombustivel"),
                "price_eur": parse_price(f.get("Preco")),
                "price_text": f.get("Preco"),
                "last_update": f.get("DataAtualizacao"),
            }
            for f in d.get("Combustiveis") or []
        ],
        "services": [s.get("Descritivo") for s in d.get("Servicos") or [] if s.get("Descritivo")],
        "payment_methods": [s.get("Descritivo") for s in d.get("MeiosPagamento") or [] if s.get("Descritivo")],
        "notes": (d.get("Observacoes") or "").strip(" -") or None,
        "source": SOURCE,
    }
