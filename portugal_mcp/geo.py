"""Places: postal codes, coordinates to parish, municipalities (GeoAPI.pt), and public holidays."""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from typing import Any

from .app import mcp, note
from .common import fail, get_json, norm

log = logging.getLogger("portugal_mcp.geo")

API = "https://json.geoapi.pt"
SOURCE = "GeoAPI.pt (official CAOP boundaries, CTT postal codes, INE census)"

note(
    "geo",
    "Place tools: postal_code for 'XXXX-XXX', where_is for coordinates, municipality for a concelho "
    "(parishes, census, contacts of the town hall). public_holidays covers national holidays only; "
    "each municipality also has its own holiday.",
)

_mem: dict[str, tuple[float, Any]] = {}


def _get(path: str, ttl: float = 86400) -> Any:
    hit = _mem.get(path)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    data = get_json(f"{API}/{path}")
    if isinstance(data, dict) and data.get("erro"):
        raise ValueError(str(data["erro"]))
    _mem[path] = (time.time(), data)
    return data


def _municipalities() -> list[dict[str, Any]]:
    """[{name, dico, district, parishes}] for all 308 municipalities."""
    key = "_municipalities"
    hit = _mem.get(key)
    if hit:
        return hit[1]
    district_of = {
        m["codigoine"]: d["distrito"]
        for d in _get("distritos/municipios", ttl=30 * 86400)
        for m in d["municipios"]
    }
    rows = [
        {"name": m["nome"], "dico": m["codigoine"], "district": district_of.get(m["codigoine"]), "parishes": m.get("freguesias") or []}
        for m in _get("municipios/freguesias", ttl=30 * 86400)
    ]
    _mem[key] = (time.time(), rows)
    return rows


def municipality_code(name: str) -> dict[str, Any]:
    """Resolve a municipality name to its 4-digit INE code (DICO)."""
    q = norm(name)
    rows = _municipalities()
    hit = next((m for m in rows if norm(m["name"]) == q), None)
    if not hit:
        cands = [m for m in rows if q and q in norm(m["name"])]
        if len(cands) == 1:
            hit = cands[0]
        elif cands:
            raise ValueError(f"{name!r} is ambiguous: " + ", ".join(m["name"] for m in cands[:10]))
    if not hit:
        raise ValueError(f"no municipality called {name!r}")
    return hit


@mcp.tool()
def postal_code(code: str) -> dict[str, Any]:
    """Look up a Portuguese postal code (código postal, 'XXXX-XXX' or 'XXXX').

    Returns district, municipality, locality and the streets the code covers, plus a central point.
    """
    try:
        m = re.fullmatch(r"\s*(\d{4})(?:\s*-?\s*(\d{3}))?\s*", code or "")
        if not m:
            raise ValueError("postal codes look like '1000-001' or '1000'")
        cp = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
        d = _get(f"cp/{cp}")
        if isinstance(d, list):
            d = d[0] if d else {}
        pts = [p.get("coordenadas") for p in d.get("pontos") or [] if p.get("coordenadas")]
        centre = [round(sum(p[0] for p in pts) / len(pts), 6), round(sum(p[1] for p in pts) / len(pts), 6)] if pts else (d.get("centro") or None)
        streets = sorted({(s.get("Artéria") or "").strip() for s in d.get("partes") or [] if s.get("Artéria")})
        return {
            "postal_code": d.get("CP") or cp,
            "postal_designation": d.get("Designação Postal"),
            "locality": d.get("Localidade"),
            "municipality": d.get("Concelho"),
            "district": d.get("Distrito"),
            "parishes": d.get("Freguesias") or d.get("freguesias"),
            "streets": streets[:40],
            "streets_total": len(streets),
            "centre_lat_lon": centre,
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "postal_code", exc)


@mcp.tool()
def where_is(lat: float, lon: float) -> dict[str, Any]:
    """Which parish, municipality and district a coordinate is in, plus altitude, land use and hazard maps."""
    try:
        if not (29 <= lat <= 43 and -32 <= lon <= -6):
            raise ValueError("coordinates are outside Portugal (mainland, Madeira, Azores)")
        d = _get(f"gps/{lat:.6f},{lon:.6f}", ttl=3600)
        return {
            "parish": d.get("freguesia"),
            "municipality": d.get("concelho"),
            "district": d.get("distrito"),
            "altitude_m": d.get("altitude_m"),
            "land_use": d.get("uso"),
            "flood_hazard": d.get("perigo_inundacao"),
            "fire_hazard": d.get("perigo_incendio"),
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "where_is", exc)


_CENSUS_KEYS = {
    "N_INDIVIDUOS": "residents",
    "N_INDIVIDUOS_H": "men",
    "N_INDIVIDUOS_M": "women",
    "N_INDIVIDUOS_0_14": "age_0_14",
    "N_INDIVIDUOS_15_24": "age_15_24",
    "N_INDIVIDUOS_25_64": "age_25_64",
    "N_INDIVIDUOS_65_OU_MAIS": "age_65_plus",
    "N_EDIFICIOS_CLASSICOS": "buildings",
    "N_ALOJAMENTOS_TOTAL": "dwellings",
    "N_AGREGADOS_DOMESTICOS_PRIVADOS": "households",
}


@mcp.tool()
def municipality(name: str) -> dict[str, Any]:
    """A municipality (concelho): district, INE code, area, parishes, 2021 census, town-hall contacts."""
    try:
        m = municipality_code(name)
        d = _get(f"municipio/{m['name']}")
        census = d.get("censos2021") or {}
        out = {
            "name": d.get("nome") or m["name"],
            "district": m["district"] or d.get("distrito"),
            "ine_code": m["dico"],
            "area_km2": float(d["areaha"]) if d.get("areaha") else None,  # GeoAPI's "areaha" field is in km²
            "parishes": m["parishes"],
            "census_2021": {v: census[k] for k, v in _CENSUS_KEYS.items() if k in census},
            "town_hall": {
                "website": d.get("sitio"),
                "email": d.get("email"),
                "phone": d.get("telefone"),
                "address": " ".join(x for x in [d.get("morada"), d.get("codigopostal")] if x) or None,
            },
            "source": SOURCE,
        }
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "municipality", exc)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def holidays(year: int) -> list[dict[str, Any]]:
    """National public holidays (Código do Trabalho art. 234), plus Carnival as an optional day."""
    e = _easter(year)
    fixed = [
        (1, 1, "Ano Novo", "New Year's Day"),
        (4, 25, "Dia da Liberdade", "Freedom Day"),
        (5, 1, "Dia do Trabalhador", "Labour Day"),
        (6, 10, "Dia de Portugal, de Camões e das Comunidades Portuguesas", "Portugal Day"),
        (8, 15, "Assunção de Nossa Senhora", "Assumption"),
        (10, 5, "Implantação da República", "Republic Day"),
        (11, 1, "Dia de Todos os Santos", "All Saints' Day"),
        (12, 1, "Restauração da Independência", "Restoration of Independence"),
        (12, 8, "Imaculada Conceição", "Immaculate Conception"),
        (12, 25, "Natal", "Christmas Day"),
    ]
    rows = [{"date": date(year, m, d), "name_pt": pt, "name_en": en, "type": "national"} for m, d, pt, en in fixed]
    rows += [
        {"date": e - timedelta(days=2), "name_pt": "Sexta-feira Santa", "name_en": "Good Friday", "type": "national"},
        {"date": e, "name_pt": "Páscoa", "name_en": "Easter Sunday", "type": "national"},
        {"date": e + timedelta(days=60), "name_pt": "Corpo de Deus", "name_en": "Corpus Christi", "type": "national"},
        {"date": e - timedelta(days=47), "name_pt": "Carnaval", "name_en": "Carnival", "type": "optional (tolerância, not a legal holiday)"},
    ]
    rows.sort(key=lambda r: r["date"])
    return rows


@mcp.tool()
def public_holidays(year: int | None = None, upcoming_only: bool = False) -> dict[str, Any]:
    """Portugal's national public holidays for a year (default: this year), with weekday.

    Carnival is listed as optional: it is usually a day off by government decision, not by law.
    Municipal holidays (one per concelho, e.g. Lisbon 13 June, Porto 24 June) are not included.
    """
    try:
        y = int(year or date.today().year)
        if not 1900 <= y <= 2200:
            raise ValueError("year out of range")
        today = date.today()
        rows = [
            {**r, "date": r["date"].isoformat(), "weekday": r["date"].strftime("%A")}
            for r in holidays(y)
            if not upcoming_only or r["date"] >= today
        ]
        return {"year": y, "holidays": rows, "source": "Código do Trabalho, art. 234 (computed; Easter-based dates via the Gregorian computus)"}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "public_holidays", exc)
