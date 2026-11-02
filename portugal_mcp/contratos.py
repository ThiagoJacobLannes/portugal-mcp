"""Public contracts: the BASE portal records published by IMPIC on dados.gov.pt — read-only."""

from __future__ import annotations

import html
import io
import logging
import re
import zipfile
from datetime import date
from typing import Any

from .app import mcp, note
from .common import FileCache, cache_dir, clamp, download, fail, get_json, iter_json_array, keywords, norm

log = logging.getLogger("portugal_mcp.contratos")

DATASET = "https://dados.gov.pt/api/1/datasets/66d72d488ca4b7cb2de28712/"
SOURCE = "IMPIC / Portal BASE (base.gov.pt), open data via dados.gov.pt"
BASE_CONTRACT = "https://www.base.gov.pt/Base4/pt/detalhe/?type=contratos&id="

note(
    "contratos",
    "Public-contract tools read IMPIC's yearly files of the BASE portal (contracts since 2012). The first "
    "call for a year downloads 15-55 MB and takes a minute or two. Amounts are contract prices in euros "
    "without VAT as reported by the buyer. Tax numbers of individual people are hidden; companies and public "
    "bodies keep theirs.",
)


def _first_year() -> int:
    return 2012


def _resource_url(year: int) -> str:
    ds = get_json(DATASET)
    for r in ds.get("resources") or []:
        if (r.get("title") or "").lower() == f"contratos{year}.zip":
            return r["url"]
    raise ValueError(f"IMPIC has no contracts file for {year} on dados.gov.pt")


_PERSON_NIF = re.compile(r"^(?:[123]\d{8}|45\d{7})$")


def _party(raw: str) -> dict[str, Any]:
    """'500051070 - Município de Lisboa' -> {nif, name}; NIFs of natural persons are masked."""
    m = re.match(r"\s*(\d{9})\s*-\s*(.*)$", raw or "")
    nif, name = (m.group(1), m.group(2).strip()) if m else (None, (raw or "").strip())
    name = html.unescape(name)
    if nif and _PERSON_NIF.match(nif):
        nif = "individual (NIF hidden)"
    return {"nif": nif, "name": name}


def _iso(d: str | None) -> str | None:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _compact(c: dict[str, Any]) -> dict[str, Any]:
    first = lambda v: (v[0] if isinstance(v, list) and v else v) or None  # noqa: E731
    return {
        "id": c.get("idcontrato"),
        "published": _iso(c.get("dataPublicacao")),
        "signed": _iso(c.get("dataCelebracaoContrato")),
        "object": (c.get("objectoContrato") or "").strip()[:240],
        "type": first(c.get("tipoContrato")),
        "procedure": c.get("tipoprocedimento"),
        "buyer": [_party(x) for x in c.get("adjudicante") or []],
        "suppliers": [_party(x) for x in c.get("adjudicatarios") or []],
        "price_eur": c.get("precoContratual"),
        "base_price_eur": c.get("precoBaseProcedimento"),
        "cpv": first(c.get("cpv")),
        "place": first(c.get("localExecucao")),
        "duration_days": c.get("prazoExecucao"),
        "framework": c.get("DescrAcordoQuadro") or None,
    }


def _contracts(year: int) -> list[dict[str, Any]]:
    def build(url: str) -> list[dict[str, Any]]:
        dest = cache_dir("contratos") / f"contratos{year}.zip.download"
        download(url, dest)
        try:
            with zipfile.ZipFile(dest) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".json"))
                with z.open(name) as raw:
                    text = io.TextIOWrapper(raw, encoding="utf-8-sig")
                    return [_compact(x) for x in iter_json_array(text)]
        finally:
            dest.unlink(missing_ok=True)

    ttl = 86400 if year >= date.today().year - 1 else 30 * 86400
    return FileCache("contratos", f"contratos-{year}", lambda: _resource_url(year), ttl=ttl).get(build)["data"]


def _year(year: int | None) -> int:
    y = int(year or date.today().year)
    if not _first_year() <= y <= date.today().year:
        raise ValueError(f"year must be between {_first_year()} and {date.today().year}")
    return y


def _match_party(parties: list[dict[str, Any]], q: str) -> bool:
    digits = re.sub(r"\D", "", q)
    if len(digits) == 9:
        return any(p["nif"] == digits for p in parties)
    nq = norm(q)
    return any(nq in norm(p["name"]) for p in parties)


@mcp.tool()
def public_contracts(
    query: str | None = None,
    buyer: str | None = None,
    supplier: str | None = None,
    procedure: str | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    year: int | None = None,
    sort: str = "value",
    limit: int = 15,
) -> dict[str, Any]:
    """Search Portuguese public contracts (Portal BASE) for one year.

    Args:
        query: words in the contract object, e.g. "software", "limpeza", "ambulâncias".
        buyer: public body name or 9-digit NIF, e.g. "Município de Lisboa", "SPMS".
        supplier: company name or NIF.
        procedure: e.g. "Ajuste Direto", "Concurso público", "Consulta Prévia".
        min_value, max_value: contract price range in euros.
        year: contract-file year (default: this year; 2012 onwards).
        sort: "value" (largest first) or "date" (newest first).
        limit: max rows.
    """
    try:
        y = _year(year)
        words = keywords(query) if query else []
        rows = []
        for c in _contracts(y):
            if words and not all(w in norm(c["object"]) for w in words):
                continue
            if buyer and not _match_party(c["buyer"], buyer):
                continue
            if supplier and not _match_party(c["suppliers"], supplier):
                continue
            if procedure and norm(procedure) not in norm(c["procedure"]):
                continue
            price = c["price_eur"] or 0
            if min_value is not None and price < min_value:
                continue
            if max_value is not None and price > max_value:
                continue
            rows.append(c)
        if sort == "date":
            rows.sort(key=lambda c: c["signed"] or c["published"] or "", reverse=True)
        else:
            rows.sort(key=lambda c: c["price_eur"] or 0, reverse=True)
        n = clamp(limit, 1, 100, 15)
        return {
            "year": y,
            "count": len(rows),
            "total_value_eur": round(sum(c["price_eur"] or 0 for c in rows), 2),
            "contracts": [{**c, "url": BASE_CONTRACT + str(c["id"])} for c in rows[:n]],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "public_contracts", exc)


@mcp.tool()
def public_contracts_summary(entity: str, role: str = "any", year: int | None = None, top: int = 10) -> dict[str, Any]:
    """Totals for one buyer or supplier in a year: number of contracts, total value, by procedure, top partners.

    Args:
        entity: name or 9-digit NIF of a public body or a company.
        role: "buyer", "supplier" or "any".
        year: default this year.
        top: how many top counterparties to list.
    """
    try:
        y = _year(year)
        r = norm(role)
        as_buyer, as_supplier = [], []
        for c in _contracts(y):
            if r in ("buyer", "any") and _match_party(c["buyer"], entity):
                as_buyer.append(c)
            if r in ("supplier", "any") and _match_party(c["suppliers"], entity):
                as_supplier.append(c)

        def summarise(rows: list[dict[str, Any]], other: str) -> dict[str, Any] | None:
            if not rows:
                return None
            names, by_proc, partners = set(), {}, {}
            for c in rows:
                v = c["price_eur"] or 0
                by_proc.setdefault(c["procedure"] or "?", [0, 0.0])
                by_proc[c["procedure"] or "?"][0] += 1
                by_proc[c["procedure"] or "?"][1] += v
                for p in c[other]:
                    k = p["name"]
                    partners.setdefault(k, [0, 0.0])
                    partners[k][0] += 1
                    partners[k][1] += v / max(1, len(c[other]))
                for p in c["buyer" if other == "suppliers" else "suppliers"]:
                    if _match_party([p], entity):
                        names.add(p["name"])
            tops = sorted(partners.items(), key=lambda kv: -kv[1][1])[: clamp(top, 1, 50, 10)]
            return {
                "matched_names": sorted(names)[:10],
                "contracts": len(rows),
                "total_value_eur": round(sum(c["price_eur"] or 0 for c in rows), 2),
                "by_procedure": {k: {"contracts": n, "value_eur": round(v, 2)} for k, (n, v) in sorted(by_proc.items(), key=lambda kv: -kv[1][1])},
                "top_counterparties": [{"name": k, "contracts": n, "value_eur": round(v, 2)} for k, (n, v) in tops],
            }

        return {
            "entity": entity,
            "year": y,
            "as_buyer": summarise(as_buyer, "suppliers"),
            "as_supplier": summarise(as_supplier, "buyer"),
            "note": "When a contract has several suppliers its value is split evenly among them in the counterparty totals.",
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "public_contracts_summary", exc)
