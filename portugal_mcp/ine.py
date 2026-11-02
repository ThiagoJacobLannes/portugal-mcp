"""Official statistics: INE (Statistics Portugal) indicator API — read-only."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import defusedxml.ElementTree as ET

from .app import mcp, note
from .common import clamp, fail, get_json, http, keywords, norm, pt_number

log = logging.getLogger("portugal_mcp.ine")

BASE = "https://www.ine.pt/ine"
SOURCE = "INE - Instituto Nacional de Estatística (www.ine.pt)"

note(
    "ine",
    "INE indicators are identified by a 7-digit code (varcd). Use ine_search to find one, then "
    "ine_indicator. Values are official and dated by reference period; quote the period.",
)

_mem: dict[str, tuple[float, Any]] = {}


def _catalog(lang: str) -> list[dict[str, Any]]:
    key = f"cat:{lang}"
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    r = http().get(f"{BASE}/xml_indic.jsp", params={"opc": "3", "lang": lang}, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    rows = []
    for ind in root.iter("indicator"):
        def t(tag: str, ind: ET.Element = ind) -> str:
            el = ind.find(tag)
            return (el.text or "").strip() if el is not None else ""
        rows.append({
            "varcd": t("varcd"),
            "title": t("title"),
            "theme": t("theme"),
            "subtheme": t("subtheme"),
            "periodicity": t("periodicity"),
            "last_period": t("dates/last_period_available"),
            "last_update": t("dates/last_update"),
            "geo_level": t("geo_lastlevel"),
            "keywords": t("keywords"),
        })
    _mem[key] = (time.time(), rows)
    return rows


@mcp.tool()
def ine_search(query: str, lang: str = "PT", limit: int = 10) -> dict[str, Any]:
    """Find INE indicators by keyword (e.g. "inflação", "desemprego", "população", "rendas", "turismo").

    Searches INE's catalogue of key indicators. Returns varcd codes to pass to ine_indicator.
    Any other INE indicator can be read with ine_indicator if you know its code (from www.ine.pt).
    Args:
        lang: "PT" (more complete wording) or "EN".
    """
    try:
        lg = "EN" if str(lang).upper() == "EN" else "PT"
        words = keywords(query)
        if not words:
            raise ValueError("give at least one keyword")
        scored = []
        for row in _catalog(lg):
            hay = norm(" ".join([row["title"], row["theme"], row["subtheme"], row["keywords"]]))
            title = norm(row["title"])
            hits = sum(1 for w in words if w in hay)
            if hits == len(words):
                scored.append((hits * 2 + sum(1 for w in words if w in title), row))
        scored.sort(key=lambda x: -x[0])
        n = clamp(limit, 1, 50, 10)
        return {
            "query": query,
            "count": len(scored),
            "indicators": [{k: v for k, v in r.items() if k != "keywords"} for _, r in scored[:n]],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "ine_search", exc)


def _data(varcd: str, lang: str) -> list[dict[str, Any]]:
    key = f"data:{varcd}:{lang}"
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    d = get_json(f"{BASE}/json_indicador/pindica.jsp", {"op": "2", "varcd": varcd, "lang": lang}, timeout=90)
    _mem[key] = (time.time(), d)
    return d


@mcp.tool()
def ine_indicator(
    varcd: str,
    place: str | None = None,
    periods: int = 3,
    category: str | None = None,
    lang: str = "PT",
    limit: int = 40,
) -> dict[str, Any]:
    """Values of an INE indicator, latest periods first.

    Args:
        varcd: 7-digit indicator code from ine_search, e.g. "0008074".
        place: geography name to keep, e.g. "Portugal", "Lisboa", "Norte", "Porto". Default: "Portugal"
            when present, otherwise all geographies.
        periods: how many of the latest reference periods to return (1-24).
        category: keep only rows whose category labels contain this text (e.g. "Total", "Mulheres").
        lang: "PT" or "EN".
        limit: max rows.
    """
    try:
        code = re.sub(r"\D", "", str(varcd)).zfill(7)
        lg = "EN" if str(lang).upper() == "EN" else "PT"
        payload = _data(code, lg)
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"INE returned no data for indicator {code}")
        ind = payload[0]
        if ind.get("Sucesso") is False or "Dados" not in ind:
            msg = ind.get("Sucesso") or ind
            raise ValueError(f"INE: indicator {code} not available ({msg})")
        data: dict[str, list[dict[str, Any]]] = ind["Dados"]
        keys = list(data.keys())[: clamp(periods, 1, 24, 3)]
        geo_names = {norm(r.get("geodsg")) for k in keys for r in data[k]}
        want = norm(place) if place else ("portugal" if "portugal" in geo_names else None)
        if want and any(norm(r.get("geodsg")) == want for k in keys for r in data[k]):
            match = lambda g: norm(g) == want  # noqa: E731 — an exact name exists: don't add "Área Metropolitana de …"
        else:
            match = lambda g: not want or want in norm(g)  # noqa: E731
        rows = []
        for k in keys:
            for r in data[k]:
                if not match(r.get("geodsg")):
                    continue
                cats = {key: v for key, v in r.items() if key.startswith("dim_") and key.endswith("_t")}
                if category and not any(norm(category) in norm(v) for v in cats.values()):
                    continue
                rows.append({
                    "period": k,
                    "geo": r.get("geodsg"),
                    "geo_code": r.get("geocod"),
                    **{f"category_{i + 1}": v for i, v in enumerate(cats.values())},
                    "value": pt_number(r.get("valor")),
                    "value_label": r.get("ind_string"),
                })
        n = clamp(limit, 1, 500, 40)
        return {
            "indicator": code,
            "name": ind.get("IndicadorDsg"),
            "last_period": ind.get("UltimoPref"),
            "last_update": ind.get("DataUltimoAtualizacao"),
            "periods_returned": keys,
            "rows_total": len(rows),
            "rows": rows[:n],
            "truncated": len(rows) > n,
            "note": None if rows else f"no rows for place={place!r} category={category!r}; try without them",
            "metadata_url": ind.get("MetaInfUrl"),
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "ine_indicator", exc)
