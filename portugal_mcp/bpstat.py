"""Banco de Portugal statistics (BPstat API): interest rates, Euribor, exchange rates, credit, debt."""

from __future__ import annotations

import logging
import time
from typing import Any

from .app import mcp, note
from .common import FileCache, clamp, fail, get_json, keywords, norm

log = logging.getLogger("portugal_mcp.bpstat")

API = "https://bpstat.bportugal.pt/data/v1"
SOURCE = "Banco de Portugal, BPstat (bpstat.bportugal.pt)"

note(
    "bpstat",
    "Banco de Portugal series: search with bpstat_search (e.g. 'euribor 12 meses', 'taxa de juro "
    "crédito habitação', 'dólar', 'dívida pública'), then read values with bpstat_series. Always give "
    "the observation date and unit.",
)

_mem: dict[str, tuple[float, Any]] = {}


def _domains(lang: str) -> list[dict[str, Any]]:
    key = f"domains:{lang}"
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    rows = [d for d in get_json(f"{API}/domains/", {"lang": lang}) if d.get("has_series")]
    _mem[key] = (time.time(), rows)
    return rows


MAX_PAGES = 60


def _pages(url: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Follow BPstat's `next_page` links (page_size is capped at 100).

    The next_page url already carries its query string; params must be None from then on
    (httpx replaces the url's query with `params`, even an empty dict).
    """
    out: list[dict[str, Any]] = []
    nxt: str | None = url
    seen: set[str] = set()
    while nxt and nxt not in seen and len(out) < MAX_PAGES:
        seen.add(nxt)
        page = get_json(nxt, params, timeout=90)
        out.append(page)
        nxt, params = page.get("extension", {}).get("next_page"), None
    if nxt and len(out) >= MAX_PAGES:
        log.warning("stopped after %d pages of %s", MAX_PAGES, url)
    return out


def _domain_catalog(domain_id: int, lang: str) -> list[dict[str, Any]]:
    """Every series of one domain: id, label, dataset. Sequential requests; cached on disk for a week."""

    def build(_url: str) -> list[dict[str, Any]]:
        rows = []
        for p in _pages(f"{API}/domains/{domain_id}/datasets/", {"lang": lang, "page_size": 100}):
            for item in p["link"]["item"]:
                ds = item["extension"]["id"]
                for sp in _pages(f"{API}/domains/{domain_id}/datasets/{ds}/", {"lang": lang, "obs_last_n": 1, "page_size": 100}):
                    rows += [{"id": s["id"], "label": s["label"], "dataset": ds} for s in sp["extension"]["series"]]
        return rows

    url = f"{API}/domains/{domain_id}/datasets/?lang={lang}"
    return FileCache("bpstat", f"catalog-{domain_id}-{lang}", url, ttl=7 * 86400).get(build)["data"]


# everyday words -> BPstat domain ids (domain labels alone rarely contain them)
TOPICS = {
    "euribor": [22], "eonia": [22], "estr": [22], "mercado monetario": [22],
    "juro": [21], "juros": [21], "taxa de juro": [21], "interest": [21], "spread": [21],
    "habitacao": [186, 21, 39], "housing": [186, 21, 39], "casa": [39, 186], "credito": [186, 209, 188],
    "consumo": [209], "emprestimo": [188, 21], "emprestimos": [188, 21], "loan": [188, 21],
    "cambio": [29], "dolar": [29], "usd": [29], "libra": [29], "gbp": [29], "real": [29], "brl": [29],
    "exchange": [29], "inflacao": [41, 12], "ipc": [12], "ihpc": [41], "precos": [41, 12, 39], "prices": [41, 12],
    "divida": [28, 18], "debt": [28, 18], "defice": [10, 172], "deficit": [10, 172], "pib": [31, 54, 55], "gdp": [31, 54, 55],
    "depositos": [206], "deposits": [206], "desemprego": [13, 139], "unemployment": [13, 139], "emprego": [13, 134, 139],
    "pagamentos": [8], "mbway": [8], "cartoes": [8], "multibanco": [8], "poupanca": [58, 45], "savings": [58, 45],
}


def _pick_domains(words: list[str], lang: str) -> list[dict[str, Any]]:
    doms = {d["id"]: d for d in _domains(lang)}
    text = " ".join(words)
    score: dict[int, float] = {}
    for key, ids in TOPICS.items():
        if key in text:
            for rank, i in enumerate(ids):
                score[i] = score.get(i, 0) + 3 - rank * 0.5
    for d in doms.values():
        n = sum(1 for w in words if w in norm(d["label"]))
        if n:
            score[d["id"]] = score.get(d["id"], 0) + n
    return [doms[i] for i, _ in sorted(score.items(), key=lambda kv: -kv[1]) if i in doms][:2]


@mcp.tool()
def bpstat_search(query: str, domain_id: int | None = None, lang: str = "PT", limit: int = 15) -> dict[str, Any]:
    """Find Banco de Portugal statistical series by keyword.

    Examples: "euribor 12 meses", "taxa de juro novos empréstimos habitação", "dólar", "dívida pública",
    "depósitos particulares", "preços habitação". The search picks the most likely statistical domains
    (or the one you pass as domain_id) and searches the series inside them; it also lists every domain
    so you can retry with a specific domain_id. The first search of a domain takes a few seconds to a
    minute; then it is cached for a week.
    Args:
        lang: "PT" or "EN" (labels are richer in PT).
    """
    try:
        lg = "EN" if str(lang).upper() == "EN" else "PT"
        words = keywords(query)
        if not words:
            raise ValueError("give at least one keyword")
        doms = _domains(lg)
        if domain_id is not None:
            picked = [d for d in doms if d["id"] == int(domain_id)]
            if not picked:
                raise ValueError(f"unknown domain_id {domain_id}")
        else:
            picked = _pick_domains(words, lg)
        topic_words = {w for k in TOPICS for w in k.split()}
        hits = []
        for d in picked:
            for s in _domain_catalog(d["id"], lg):
                lab = norm(s["label"])
                n = sum(1 for w in words if w in lab)
                need = len([w for w in words if w not in topic_words]) or 1
                if n >= min(len(words), need):
                    hits.append((n, len(lab), {**s, "domain_id": d["id"], "domain": d["label"]}))
        hits.sort(key=lambda x: (-x[0], x[1]))
        n = clamp(limit, 1, 50, 15)
        return {
            "query": query,
            "domains_searched": [{"id": d["id"], "label": d["label"]} for d in picked],
            "count": len(hits),
            "series": [{k: h[2][k] for k in ("id", "label", "domain_id")} for h in hits[:n]],
            "other_domains": None if hits else [{"id": d["id"], "label": d["label"]} for d in doms],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "bpstat_search", exc)


def _observations(doc: dict[str, Any], series: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode one series out of a JSON-stat 2.0 dataset response."""
    ids, size = doc["id"], doc["size"]
    cat_of = {str(dc["dimension_id"]): str(dc["category_id"]) for dc in series["dimension_category"]}
    dates = doc["dimension"]["reference_date"]["category"]
    date_index = dates.get("index")
    if isinstance(date_index, list):
        date_index = {k: i for i, k in enumerate(date_index)}
    values, status = doc.get("value") or {}, doc.get("status") or {}
    out = []
    for dt, pos in sorted(date_index.items(), key=lambda kv: kv[1]):
        flat = 0
        for dim, n in zip(ids, size, strict=True):
            if dim == "reference_date":
                i = pos
            else:
                idx = doc["dimension"][dim]["category"]["index"]
                if isinstance(idx, list):
                    idx = {k: j for j, k in enumerate(idx)}
                i = idx.get(cat_of.get(dim), 0)
            flat = flat * n + i
        v = values.get(str(flat)) if isinstance(values, dict) else (values[flat] if flat < len(values) else None)
        if v is not None:
            out.append({"date": dt, "value": v, "status": (status.get(str(flat)) if isinstance(status, dict) else None)})
    return out


@mcp.tool()
def bpstat_series(series_ids: list[int], last_n: int = 12, since: str | None = None, lang: str = "PT") -> dict[str, Any]:
    """Observations of Banco de Portugal series (ids from bpstat_search).

    Args:
        series_ids: one or more series ids (max 10), e.g. [12559924].
        last_n: latest observations per series (1-500). Ignored when `since` is given.
        since: "YYYY-MM-DD" — return every observation from this date.
        lang: "PT" or "EN".
    """
    try:
        lg = "EN" if str(lang).upper() == "EN" else "PT"
        ids = [int(s) for s in series_ids][:10]
        if not ids:
            raise ValueError("give at least one series id")
        meta = get_json(f"{API}/series/", {"series_ids": ",".join(map(str, ids)), "lang": lg})
        out = []
        by_dataset: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for m in meta:
            by_dataset.setdefault((m["domain_ids"][0], m["dataset_id"]), []).append(m)
        for (dom, ds), ms in by_dataset.items():
            params: dict[str, Any] = {"lang": lg, "series_ids": ",".join(str(m["id"]) for m in ms)}
            if since:
                params["obs_since"] = since
            else:
                params["obs_last_n"] = clamp(last_n, 1, 500, 12)
            doc = get_json(f"{API}/domains/{dom}/datasets/{ds}/", params, timeout=90)
            series_meta = {s["id"]: s for s in doc["extension"]["series"]}
            for m in ms:
                obs = _observations(doc, series_meta.get(m["id"], m))
                out.append({
                    "id": m["id"],
                    "label": m["label"],
                    "description": m.get("description"),
                    "updated": m.get("obs_updated_at"),
                    "observations": obs,
                    "latest": obs[-1] if obs else None,
                })
        missing = sorted(set(ids) - {s["id"] for s in out})
        return {"series": out, "unknown_ids": missing or None, "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "bpstat_series", exc)
