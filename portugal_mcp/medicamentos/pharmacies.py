"""Pharmacies and duty pharmacies from farmaciasdeservico.net.

farmaciasdeservico.net publishes, per municipality (concelho), every pharmacy with
address, phone, map coordinates, today's opening hours and which ones are on the
official duty roster ("De Serviço") for the current duty day (09:00 to 09:00).
Its robots.txt allows all crawling and its terms allow reproducing content with
the source cited. We read it on demand, one municipality at a time:

* GET /localidade/<district>/<municipality>             duty pharmacies + open now
* GET /localidade/<district>/<municipality>/expediente  every pharmacy in the municipality

Cities are resolved through the site's sitemap (all municipalities) and, failing
that, through the site's own locality search (parishes and towns). A coordinate
without a city is turned into a municipality with OpenStreetMap Nominatim
(reverse geocoding, within its usage policy).
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from .common import cache, http, norm

log = logging.getLogger("portugal_mcp.medicamentos")

BASE = "https://www.farmaciasdeservico.net"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"

SITEMAP_TTL = 24 * 3600
PAGE_TTL = 15 * 60
GEO_TTL = 24 * 3600


class LayoutChanged(RuntimeError):
    pass


@dataclass
class Locality:
    district: str
    municipality: str
    path: str  # localidade/<district>/<municipality>

    @property
    def url(self) -> str:
        return f"{BASE}/{self.path}"


@dataclass
class Pharmacy:
    id: str
    name: str
    address: str
    parish: str
    phone: str | None
    website: str | None
    lat: float | None
    lon: float | None
    hours_today: list[str]
    open_now: bool | None
    closed_today: bool
    on_duty: bool
    duty: str | None
    url: str
    distance_km: float | None = None


# ---------------------------------------------------------------------------
# Locality resolution
# ---------------------------------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"\s+", "_", norm(text).replace("-", " ").strip())


def _sitemap() -> dict[str, list[Locality]]:
    def load() -> dict[str, list[Locality]]:
        xml = http().get(BASE + "/sitemap.xml").text
        out: dict[str, list[Locality]] = {}
        for m in re.finditer(r"<loc>https?://[^/]+/(localidade/([^/<]+)/([^/<]+))</loc>", xml):
            path, district, muni = m.groups()
            out.setdefault(muni, []).append(Locality(district, muni, path))
        if not out:
            raise LayoutChanged("farmaciasdeservico.net sitemap layout changed: no municipalities found")
        return out

    return cache.get_or_set("fds_sitemap", SITEMAP_TTL, load)


def _site_search(q: str) -> list[tuple[Locality, str]]:
    """The site's own search: towns and parishes -> municipality pages."""

    def load() -> list[tuple[Locality, str]]:
        r = http().post(BASE + "/pesquisa", data={"q": q}, headers={"X-Requested-With": "XMLHttpRequest"})
        html = "".join((r.json().get("updates") or {}).values())
        out: list[tuple[Locality, str]] = []
        for m in re.finditer(r'<a href="(localidade/([^/"]+)/([^/"]+))">(.*?)</a>', html, re.S):
            label = " ".join(re.sub(r"<[^>]+>", " ", m.group(4)).split())
            out.append((Locality(m.group(2), m.group(3), m.group(1)), label))
        return out

    return cache.get_or_set("fds_search:" + norm(q), SITEMAP_TTL, load)


def resolve_city(city: str) -> tuple[Locality | None, list[str]]:
    """Return (locality, other candidates). 'Town, Municipality' narrows a town name."""
    name, _, hint = city.partition(",")
    hint_slug = _slug(hint) if hint.strip() else ""
    smap = _sitemap()
    slug = _slug(name)
    cands = [c for k in (slug, slug + "_(raa)", slug + "_(ram)") for c in smap.get(k, [])]
    if not cands:
        cands = [locs[0] for key, locs in smap.items() if key.replace("_", "") == slug.replace("_", "")]
    if cands:
        if hint_slug:
            cands = sorted(cands, key=lambda c: hint_slug not in (c.district, c.municipality))
        return cands[0], [c.path for c in cands[1:]]
    found = _site_search(name.strip())
    if not found:
        return None, []
    if hint_slug:
        found = sorted(found, key=lambda f: hint_slug not in (f[0].district, f[0].municipality))
    return found[0][0], [f"{label} -> {loc.path}" for loc, label in found[1:]]


def reverse_geocode(lat: float, lon: float) -> str | None:
    def load() -> str | None:
        r = http().get(
            NOMINATIM,
            params={
                "format": "jsonv2",
                "lat": f"{lat:.5f}",
                "lon": f"{lon:.5f}",
                "zoom": 10,
                "addressdetails": 1,
                "accept-language": "pt",
            },
        )
        addr = r.json().get("address") or {}
        if (addr.get("country_code") or "").lower() != "pt":
            return None
        for k in ("municipality", "city", "town", "county", "village"):
            if addr.get(k):
                return addr[k]
        return None

    return cache.get_or_set(f"geo:{lat:.3f},{lon:.3f}", GEO_TTL, load)


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------
_COORDS = re.compile(r"imgs/mapa/(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)/")
_VALID = re.compile(r"Informação válida[^<*]*")


def _open_now(horario: str | None) -> tuple[bool | None, bool]:
    """data-horario is 'start-end|start-end' in epoch ms, or 'fechada'."""
    if not horario:
        return None, False
    if horario.strip() == "fechada":
        return False, True
    now = time.time() * 1000
    try:
        for span in horario.split("|"):
            a, b = span.split("-")
            if int(a) <= now < int(b):
                return True, False
        return False, False
    except ValueError:
        return None, False


def _parse(html: str) -> tuple[list[Pharmacy], str | None]:
    tree = LexborHTMLParser(html)
    out: list[Pharmacy] = []
    for div in tree.css("div.farmacia"):
        link = div.css_first("h3 a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        m = re.match(r"f/(\d+)/", href)
        if not m:
            continue
        morada = div.css_first("p.morada")
        lines = [x.strip() for x in (morada.text(separator="\n") if morada else "").split("\n") if x.strip()]
        tel = div.css_first('a[href^="tel:"]')
        web = div.css_first("a.website")
        coords = None
        for node in div.css("[style]"):
            c = _COORDS.search(node.attributes.get("style") or "")
            if c:
                coords = (float(c.group(1)), float(c.group(2)))
                break
        hours = []
        for ln in div.css(".horario .linha"):
            parts = [s.text(strip=True) for s in ln.css("strong")]
            if len(parts) == 2:
                hours.append(f"{parts[0]}-{parts[1]}")
            elif ln.text(strip=True):
                hours.append(" ".join(ln.text().split()))
        classes = (div.attributes.get("class") or "").split()
        esc = div.css_first(".escalaOficial")
        duty = None
        if esc is not None:
            t = esc.css_first(".titulo")
            st = esc.css_first(".subtitulo")
            duty = " - ".join(
                x for x in (" ".join(t.text().split()) if t else "", " ".join(st.text().split()) if st else "") if x
            )
        open_now, closed = _open_now(div.attributes.get("data-horario"))
        out.append(
            Pharmacy(
                id=m.group(1),
                name=" ".join(link.text().split()),
                address=lines[0] if lines else "",
                parish=", ".join(lines[1:]),
                phone=(tel.text(strip=True) if tel else None) or None,
                website=web.attributes.get("href") if web else None,
                lat=coords[0] if coords else None,
                lon=coords[1] if coords else None,
                hours_today=hours,
                open_now=open_now,
                closed_today=closed,
                on_duty=("escalada" in classes) or esc is not None,
                duty=duty,
                url=f"{BASE}/{href}",
            )
        )
    valid = _VALID.search(html)
    return out, (" ".join(valid.group(0).split()) if valid else None)


def municipality_pharmacies(loc: Locality) -> tuple[list[Pharmacy], str | None]:
    def load() -> tuple[list[Pharmacy], str | None]:
        duty_list, valid = _parse(http().get(loc.url).text)
        all_list, valid2 = _parse(http().get(loc.url + "/expediente").text)
        merged: dict[str, Pharmacy] = {p.id: p for p in all_list}
        for p in duty_list:
            q = merged.get(p.id)
            if q is None:
                merged[p.id] = p
            elif p.on_duty and not q.on_duty:
                q.on_duty, q.duty = True, p.duty
        if not merged:
            raise LayoutChanged(
                f"farmaciasdeservico.net page layout changed: no pharmacies parsed from {loc.url}"
            )
        return list(merged.values()), valid or valid2

    return cache.get_or_set("fds:" + loc.path, PAGE_TTL, load)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_pharmacies(
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    on_duty_only: bool = False,
    open_now_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    if (lat is None) != (lon is None):
        return {"error": "Give both lat and lon, or neither."}
    if not city and lat is None:
        return {"error": "Give a city/town (e.g. 'Lisboa', 'Porto', 'Alvalade') or lat/lon."}

    place = city
    if not place:
        place = reverse_geocode(float(lat), float(lon))  # type: ignore[arg-type]
        if not place:
            return {"error": "That coordinate is not in Portugal (or could not be resolved to a municipality)."}

    loc, others = resolve_city(place)
    if loc is None:
        return {"error": f"Unknown Portuguese municipality or town: {place!r}."}

    items, valid = municipality_pharmacies(loc)
    in_municipality = len(items)
    duty_in_municipality = sum(1 for p in items if p.on_duty)
    if lat is not None and lon is not None:
        for p in items:
            if p.lat is not None and p.lon is not None:
                p.distance_km = round(_km(float(lat), float(lon), p.lat, p.lon), 2)
        if radius_km:
            items = [p for p in items if p.distance_km is not None and p.distance_km <= float(radius_km)]

    matched = len(items)
    if on_duty_only:
        items = [p for p in items if p.on_duty]
    if open_now_only:
        items = [p for p in items if p.open_now]

    def key(p: Pharmacy) -> tuple[Any, ...]:
        return (
            not p.on_duty,
            not bool(p.open_now),
            p.distance_km if p.distance_km is not None else 9999,
            p.name,
        )

    items.sort(key=key)
    lim = max(1, min(int(limit), 100))
    return {
        "municipality": loc.municipality.replace("_", " ").title(),
        "district": loc.district.replace("_", " ").title(),
        "other_candidates": others,
        "resolved_from": place,
        "pharmacies_in_municipality": in_municipality,
        "on_duty_in_municipality": duty_in_municipality,
        "within_radius": matched if radius_km and lat is not None else None,
        "duty_validity": valid,
        "count": len(items[:lim]),
        "pharmacies": [asdict(p) for p in items[:lim]],
        "notes": [
            "on_duty = on the official duty roster ('farmácia de serviço') for the current "
            "duty day, which runs from 09:00 to 09:00 the next day.",
            "Only pharmacies in this one municipality are covered; neighbouring municipalities "
            "may have closer ones.",
            "Opening hours and duty rosters can change at short notice: call before you go.",
        ],
        "source": f"farmaciasdeservico.net ({loc.url})"
        + ("; municipality from coordinates: © OpenStreetMap contributors (Nominatim)" if not city else ""),
    }
