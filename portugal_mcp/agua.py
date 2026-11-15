"""Water: how full mainland Portugal's reservoirs are, per river basin, from APA's national water
resources information system (SNIRH), compared with the historical average for the month."""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import date
from typing import Any

from .app import mcp, note
from .common import fail, http, norm

log = logging.getLogger("portugal_mcp.agua")

TABLE = "https://snirh.apambiente.pt/snirh/_dadossintese/albufeiras/tabelas/tabelageral.php"
SOURCE = "APA - Agência Portuguesa do Ambiente, SNIRH monthly reservoir storage bulletin (snirh.apambiente.pt)"
MONTHS = ["OUT", "NOV", "DEZ", "JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET"]
MONTH_NO = {m: (i + 9) % 12 + 1 for i, m in enumerate(MONTHS)}  # OUT -> 10 ... SET -> 9

note(
    "agua",
    "reservoir_levels gives how full mainland Portugal's reservoirs are per river basin (% of capacity), "
    "for the latest month APA has published, next to the historical average for that month and the "
    "same month a year before. Months run in hydrological years (October to September).",
)

_mem: dict[str, tuple[float, dict[str, Any]]] = {}


def _num(text: str) -> float | None:
    t = text.replace(" ", "").replace("\xa0", "")
    try:
        return float(t)
    except ValueError:
        return None


def parse_table(page: str) -> dict[str, Any]:
    """SNIRH's general table -> {basins, capacity_hm3, average: {month: [..]}, years: {"2025/26": {month: [..]}}}."""
    table = re.search(r"<table.*?</table>", page, re.S | re.I)
    if not table:
        raise ValueError("the SNIRH page has no table")
    rows = []
    for tr in re.findall(r"<tr.*?</tr>", table.group(0), re.S | re.I):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        if cells:
            rows.append(cells)
    basins = [c for c in rows[0] if c]
    out: dict[str, Any] = {"basins": basins, "capacity_hm3": [], "average": {}, "years": {}}
    block = None
    for cells in rows[1:]:
        if norm(cells[0]).startswith("capacidade"):
            out["capacity_hm3"] = [_num(c) for c in cells[1:]]
            continue
        if len(cells) == len(basins) + 2:  # block label, month, values
            label, cells = cells[0], cells[1:]
            m = re.match(r"(\d{4}/\d{2})", label)
            block = m.group(1) if m else "average"
        month, values = cells[0], [_num(c) for c in cells[1:]]
        if block == "average":
            out["average"][month] = values
        elif block:
            out["years"].setdefault(block, {})[month] = values
    return out


def latest(parsed: dict[str, Any]) -> tuple[str, str, list[float | None]] | None:
    """(hydrological year, month, values) of the newest month with data."""
    for year in sorted(parsed["years"], reverse=True):
        for month in reversed(MONTHS):
            vals = parsed["years"][year].get(month) or []
            if any(v is not None for v in vals):
                return year, month, vals
    return None


def _fetch(year: int, month: int) -> dict[str, Any]:
    key = f"{year}-{month}"
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    r = http().get(TABLE, params={"percOUvolum": 0, "anohi": year, "mes": month, "bacia": "", "albuf": "", "albuftblgeralopcao": "", "zonas": ""}, headers={"Accept": "text/html"}, timeout=30)
    r.raise_for_status()
    parsed = parse_table(r.content.decode("iso-8859-1"))
    _mem[key] = (time.time(), parsed)
    return parsed


def summarise(parsed: dict[str, Any], basin: str | None) -> dict[str, Any]:
    found = latest(parsed)
    if not found:
        raise ValueError("SNIRH has no reservoir data for this period yet")
    year, month, values = found
    avg = parsed["average"].get(month) or [None] * len(values)
    years = sorted(parsed["years"])
    prev_year = years[years.index(year) - 1] if years.index(year) > 0 else None
    prev = (parsed["years"].get(prev_year) or {}).get(month) if prev_year else None
    caps = parsed["capacity_hm3"]
    rows = []
    for i, name in enumerate(parsed["basins"]):
        rows.append({
            "basin": name.title(),
            "pct_full": values[i] if i < len(values) else None,
            "historical_average_pct": avg[i] if i < len(avg) else None,
            "pct_a_year_before": prev[i] if prev and i < len(prev) else None,
            "capacity_hm3": caps[i] if i < len(caps) else None,
        })
    total_cap = sum(r["capacity_hm3"] or 0 for r in rows if r["pct_full"] is not None)
    stored = sum((r["capacity_hm3"] or 0) * (r["pct_full"] or 0) / 100 for r in rows if r["pct_full"] is not None)
    if basin:
        q = norm(basin)
        rows = [r for r in rows if q in norm(r["basin"])]
        if not rows:
            raise ValueError(f"no basin like {basin!r}; basins: " + ", ".join(b.title() for b in parsed["basins"]))
    start = int(year[:4])
    return {
        "month": f"{start + (0 if MONTH_NO[month] >= 10 else 1)}-{MONTH_NO[month]:02d}",
        "hydrological_year": year,
        "mainland_pct_full": round(100 * stored / total_cap, 1) if total_cap else None,
        "basins": rows,
    }


@mcp.tool()
def reservoir_levels(basin: str | None = None) -> dict[str, Any]:
    """How full mainland Portugal's reservoirs are, per river basin, in the latest month published.

    Args:
        basin: optional, e.g. "Tejo", "Douro", "Guadiana", "Sado", "Ribeiras do Algarve", "Arade".
    Each basin has its % of capacity, the historical average for that month, the same month a
    year before and the capacity in hm³; plus the capacity-weighted mainland total.
    """
    try:
        today = date.today()
        parsed = _fetch(today.year, today.month)
        found = latest(parsed)
        # The monthly average column is filled only for the month asked for; ask again for the
        # newest month that actually has data.
        if found and not any(v is not None for v in parsed["average"].get(found[1]) or []):
            m = MONTH_NO[found[1]]
            y = int(found[0][:4]) + (0 if m >= 10 else 1)
            parsed = _fetch(y, m)
        return {**summarise(parsed, basin), "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "reservoir_levels", exc)
