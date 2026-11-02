"""Medicine availability from Infarmed's public pages on www.infarmed.pt.

Sources (all on www.infarmed.pt, whose robots.txt allows automated access):

* The "Gestão da disponibilidade do medicamento" page. From it we read
  - the "Situações de escassez em monitorização" section (shortages Infarmed is
    actively monitoring, with circulars), and
  - the links to the lists currently in force under the Regulation on exports of
    medicines: the list of medicines whose export is temporarily suspended
    (updated monthly) and the prior-notification list (LNP, updated quarterly).
* The two lists themselves (.xlsx files).

The page is re-read every few hours, so a new monthly list is picked up
automatically. If the page or the spreadsheets change shape, we raise a clear
error instead of guessing.

Not used: Infomed and the SiATS shortage search live on extranet.infarmed.pt,
whose robots.txt disallows all automated access (`Disallow: /`).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote_plus, urljoin

import openpyxl
from selectolax.lexbor import LexborHTMLParser

from .common import cache, http, norm

log = logging.getLogger("portugal_mcp.medicamentos")

BASE = "https://www.infarmed.pt"
AVAILABILITY_PAGE = BASE + "/web/infarmed/gestao-da-disponibilidade-do-medicamento"
PAGE_TTL = 6 * 3600
LIST_TTL = 6 * 3600


class LayoutChanged(RuntimeError):
    pass


@dataclass
class ListLink:
    kind: str           # "export_suspended" | "prior_notification"
    url: str
    in_force: str       # e.g. "Lista em vigor a partir de 17 de outubro de 2026"
    deliberation: str   # e.g. "Deliberação n.º 101/CD/2026, de 16 de outubro"
    deliberation_url: str


@dataclass
class Monitored:
    substance: str
    title: str
    summary: str
    links: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PageInfo:
    lists: list[ListLink]
    monitored: list[Monitored]


def _abs(href: str) -> str:
    return urljoin(BASE, href.strip())


def _parse_page(html: str) -> PageInfo:
    tree = LexborHTMLParser(html)

    # --- Lists in force -----------------------------------------------------
    # Each list block reads: "Atualizada pela <a>Deliberação ...</a>" then
    # "<a>Lista em vigor a partir de DD de MES de AAAA</a>". The export-suspended
    # file name contains "exporta"; the prior-notification one contains "LNP".
    lists: list[ListLink] = []
    last_delib: tuple[str, str] = ("", "")
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        text = " ".join(a.text().split())
        if "/documents/" not in href:
            continue
        if text.lower().startswith("deliberação"):
            last_delib = (text, _abs(href))
            continue
        if not text.lower().startswith("lista em vigor"):
            continue
        h = norm(unquote_plus(href))
        if "exporta" in h:
            kind = "export_suspended"
        elif "lnp" in h or "notifica" in h:
            kind = "prior_notification"
        else:
            continue
        if any(x.kind == kind for x in lists):
            continue
        lists.append(ListLink(kind, _abs(href), text, last_delib[0], last_delib[1]))
    if not any(x.kind == "export_suspended" for x in lists):
        raise LayoutChanged(
            "Infarmed page layout changed: could not find the link to the export-suspended "
            f"list on {AVAILABILITY_PAGE}"
        )

    # --- Shortages under monitoring -----------------------------------------
    monitored: list[Monitored] = []
    box = None
    for div in tree.css("div.cms-editor"):
        if norm(div.text()).startswith("situacoes de escassez em monitorizacao"):
            box = div
            break
    if box is not None:
        # Split the block at each <strong> title (the first one is the section header).
        parts = re.split(r"<strong>", box.html or "")[2:]
        for part in parts:
            sub = LexborHTMLParser("<div><strong>" + part + "</div>")
            strong = sub.css_first("strong")
            substance = " ".join(strong.text().split()) if strong else ""
            u = sub.css_first("u")
            title = " ".join(u.text().split()) if u else ""
            body = " ".join(sub.body.text(separator=" ").split()) if sub.body else ""
            body = body.replace(substance, "", 1).replace(title, "", 1).strip()
            cut = body.find("Sobre este tema poderá consultar")
            if cut > 0:
                body = body[:cut].strip()
            links = [
                {"title": " ".join(x.text().split()), "url": _abs(x.attributes.get("href") or "")}
                for x in sub.css("a")
                if x.attributes.get("href")
            ]
            if substance:
                monitored.append(Monitored(substance, title, body[:900], links))
    else:
        log.warning("Infarmed page: 'Situações de escassez em monitorização' section not found")

    return PageInfo(lists, monitored)


def page_info() -> PageInfo:
    def load() -> PageInfo:
        return _parse_page(http().get(AVAILABILITY_PAGE).text)

    return cache.get_or_set("infarmed_page", PAGE_TTL, load)


# Column aliases (normalised header text -> field).
_COLS = {
    "registration_number": ("numero de registo", "no registo", "n registo", "nº registo"),
    "name": ("nome comercial", "nome do medicamento"),
    "active_substance": ("dci/substancia ativa", "dci"),
    "strength": ("dosagem",),
    "form": ("forma farmaceutica",),
    "pack": ("tamanho da embalagem", "apresentacao"),
    "holder": ("titular de aim",),
    "reason": ("motivo",),
    "therapeutic_group": ("cft",),
}


def _map_header(row: tuple[Any, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, cell in enumerate(row):
        h = norm(str(cell or "")).replace("º", "")
        for key, aliases in _COLS.items():
            if key not in out and h in aliases:
                out[key] = i
    return out


def _parse_xlsx(data: bytes) -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows_out: list[dict[str, Any]] = []
    for ws in wb.worksheets:
        header: dict[str, int] | None = None
        for row in ws.iter_rows(values_only=True):
            if header is None:
                cand = _map_header(row)
                if "name" in cand and "active_substance" in cand:
                    header = cand
                continue
            if not any(row):
                continue
            rec = {k: (row[i] if i < len(row) else None) for k, i in header.items()}
            if not rec.get("name"):
                continue
            rec = {k: (str(v).strip() if v is not None else None) for k, v in rec.items()}
            rec["sheet"] = ws.title
            rows_out.append(rec)
    if not rows_out:
        raise LayoutChanged("Infarmed list layout changed: no rows with the expected columns")
    return rows_out


def list_rows(link: ListLink) -> list[dict[str, Any]]:
    def load() -> list[dict[str, Any]]:
        return _parse_xlsx(http().get(link.url).content)

    return cache.get_or_set("xlsx:" + link.url, LIST_TTL, load)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
_STOP = {"de", "da", "do", "e", "com", "para", "mg", "ml", "g", "mcg", "ug", "ui"}


def _tokens(query: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+(?:[.,][0-9]+)?", norm(query))
    out = []
    for t in toks:
        if t in _STOP:
            continue
        if not t[0].isdigit() and len(t) > 4 and t.endswith("s"):
            t = t[:-1]  # comprimidos -> comprimido
        out.append(t.replace(",", "."))
    return out


def _matches(tokens: list[str], text: str) -> bool:
    if not tokens:
        return False
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.replace(",", "."))
    for t in tokens:
        if t[0].isdigit():
            if t not in words:
                return False
        elif not any(w.startswith(t) or (len(t) >= 6 and t.startswith(w) and len(w) >= 5) for w in words):
            return False
    return True


def _row_text(r: dict[str, Any]) -> str:
    return norm(" ".join(str(r.get(k) or "") for k in ("name", "active_substance", "strength", "form")))


def _group(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact view: one line per active substance + strength + form."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for r in rows:
        key = (r.get("active_substance") or "", r.get("strength") or "", r.get("form") or "")
        names = groups.setdefault(key, [])
        if r.get("name") and r["name"] not in names:
            names.append(r["name"])
    return [
        {"active_substance": k[0], "strength": k[1], "form": k[2], "brands": len(v), "examples": v[:5]}
        for k, v in groups.items()
    ]


def availability(query: str, limit: int = 25) -> dict[str, Any]:
    tokens = _tokens(query)
    if not tokens:
        return {"error": "Give a medicine name or active substance, e.g. 'quetiapina' or 'Brufen'."}
    info = page_info()

    statuses: list[str] = []
    sections: dict[str, Any] = {}

    for link in info.lists:
        rows = list_rows(link)
        hits = [r for r in rows if _matches(tokens, _row_text(r))]
        key = link.kind
        sections[key] = {
            "list": (
                "Medicines whose export / distribution to other EU countries is temporarily "
                "suspended to protect supply in Portugal (updated monthly)"
                if key == "export_suspended"
                else "Prior-notification list (LNP): medicines with identified availability "
                "problems or critical medicines; exporting them needs prior notice to Infarmed "
                "(updated quarterly)"
            ),
            "in_force": link.in_force,
            "deliberation": link.deliberation,
            "deliberation_url": link.deliberation_url,
            "source_url": link.url,
            "match_count": len(hits),
            "by_strength_and_form": _group(hits),
            "matches": [
                {k: v for k, v in r.items() if v} for r in hits[: max(1, min(int(limit), 200))]
            ],
        }
        if hits:
            statuses.append(key)

    # Monitored situations are described in prose, per substance: match on the words only
    # (a strength like "300" rarely appears in the text).
    word_tokens = [t for t in tokens if not t[0].isdigit()] or tokens
    mon_hits = [
        m
        for m in info.monitored
        if _matches(word_tokens, norm(" ".join([m.substance, m.title, m.summary])))
    ]
    sections["monitored_shortages"] = {
        "list": "Shortage situations Infarmed is actively monitoring (Infarmed availability page)",
        "source_url": AVAILABILITY_PAGE,
        "matches": [m.__dict__ for m in mon_hits],
    }
    if mon_hits:
        statuses.append("monitored_shortage")

    if "monitored_shortage" in statuses or "export_suspended" in statuses:
        summary = (
            "Supply problem reported: Infarmed lists this medicine (or presentations of it) as "
            "in shortage / supply risk. Some presentations or brands may still be available; "
            "ask a pharmacist about equivalent presentations."
        )
    elif "prior_notification" in statuses:
        summary = (
            "Watch list: Infarmed has flagged availability concerns or criticality for this "
            "medicine (exports need prior notice). It is not on the export-suspended list."
        )
    else:
        summary = (
            "No issue found in Infarmed's published lists (export-suspended list, prior-"
            "notification list, shortages under monitoring). This does not cover every "
            "shortage or discontinuation: check Infarmed's shortage search in a browser to be sure."
        )

    return {
        "query": query,
        "status": statuses or ["no_issue_in_published_lists"],
        "summary": summary,
        **sections,
        "not_checked": {
            "what": "Infarmed's per-medicine shortage (ruturas) and discontinuation search and "
            "the Infomed database are on extranet.infarmed.pt, whose robots.txt disallows "
            "automated access, so this server does not query them.",
            "check_yourself": AVAILABILITY_PAGE,
        },
    }
