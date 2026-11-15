"""School statistics from InfoEscolas (DGEEC, Ministry of Education): for every public and private
school in mainland Portugal, students enrolled, retention/dropout rates, the share finishing on time
next to comparable students nationally, and (3rd cycle) direct-success paths."""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from typing import Any

from .app import mcp, note
from .common import clamp, fail, http, norm

log = logging.getLogger("portugal_mcp.escolas")

SITE = "https://infoescolas.medu.pt"
SOURCE = "InfoEscolas, DGEEC - Direção-Geral de Estatísticas da Educação e Ciência (infoescolas.medu.pt)"
LEVELS = {
    "1ciclo": ("1Ciclo", "1st cycle (years 1-4)"),
    "2ciclo": ("2Ciclo", "2nd cycle (years 5-6)"),
    "3ciclo": ("3Ciclo", "3rd cycle (years 7-9)"),
    "secundario": ("Secundario_CH", "secondary, science-humanities courses (years 10-12)"),
    "profissional": ("Secundario_Profissional", "secondary, vocational courses"),
}
ALIASES = {"primario": "1ciclo", "primary": "1ciclo", "basico": "3ciclo", "secondary": "secundario", "vocational": "profissional",
           "cientifico-humanistico": "secundario", "ch": "secundario", "cp": "profissional"}
YEAR_RE = re.compile(r"(\d{4})/(\d{2,4})")

note(
    "escolas",
    "school_stats gives InfoEscolas (Ministry of Education) figures per school in mainland Portugal: "
    "students, retention or dropout rate, the share finishing the cycle on time next to the national "
    "figure for students with a similar profile, and for the 3rd cycle the share of 'direct success' "
    "paths. Say which school year the figures refer to; they are not a ranking.",
)

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def pick_level(level: str | None) -> str:
    q = norm(level or "secundario").replace(" ", "").replace(".", "").replace("º", "")
    q = ALIASES.get(q, q)
    if q in LEVELS:
        return q
    m = re.match(r"([123])(o|ciclo)?", q)
    if m:
        return f"{m.group(1)}ciclo"
    raise ValueError(f"level is one of {', '.join(LEVELS)}")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def year_spans(header: list[Any]) -> list[tuple[str, int, int]]:
    """[(school year, first col, last col exclusive)] from a header row with year labels."""
    marks = [(i, YEAR_RE.search(str(c))) for i, c in enumerate(header) if c is not None and YEAR_RE.search(str(c))]
    spans = []
    for k, (i, m) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else i + 1
        if k + 1 == len(marks):
            # The last year runs until the first "Observações" column (or the end of the labels below).
            obs = next((j for j in range(i + 1, len(header)) if header[j] is not None and "observ" in norm(header[j])), None)
            end = obs if obs is not None else len(header)
        y2 = m.group(2)
        spans.append((f"{m.group(1)}/{y2[-2:]}", i, end))
    return spans


def _rows(ws: Any) -> tuple[list[Any], list[Any], list[Any], list[list[Any]]]:
    """(header, sub-header, sub-sub-header, data rows) of one InfoEscolas sheet."""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    h = next(i for i, r in enumerate(rows[:12]) if r and r[0] and norm(r[0]).startswith("codigo"))
    pad = lambda r: r + [None] * (len(rows[h]) - len(r))  # noqa: E731
    data = [r for r in rows[h + 1:] if r and (isinstance(r[0], (int, float)) or r[0] == "TOTALPT")]
    return pad(rows[h]), pad(rows[h + 1]) if h + 1 < len(rows) else [], pad(rows[h + 2]) if h + 2 < len(rows) else [], data


def _key(r: list[Any]) -> str:
    return "TOTALPT" if r[0] == "TOTALPT" else str(int(r[0]))


def parse_workbook(blob: bytes) -> dict[str, Any]:
    """InfoEscolas per-school workbook -> {year, schools: {code: {...}}}."""
    import warnings

    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the files carry headers/footers openpyxl cannot parse
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    sheets = {norm(ws.title): ws for ws in wb.worksheets}
    schools: dict[str, dict[str, Any]] = {}
    year = None

    def school(r: list[Any]) -> dict[str, Any]:
        return schools.setdefault(_key(r), {"code": _key(r), "name": r[1], "municipality": r[2]})

    if "populacao" in sheets:
        head, sub, _, data = _rows(sheets["populacao"])
        spans = year_spans(head)
        if spans:
            year, a, b = spans[-1]
            for r in data:
                vals = [_num(v) for v in r[a:b]]
                if any(v is not None for v in vals):
                    school(r)["students"] = int(sum(v for v in vals if v is not None))
    if "retencao" in sheets:
        head, sub, _, data = _rows(sheets["retencao"])
        spans = year_spans(head)
        if spans:
            y, a, b = spans[-1]
            grades = [str(g).strip() for g in sub[a:b]]
            for r in data:
                rates = {g: round(v * 100, 1) for g, v in zip(grades, [_num(x) for x in r[a:b]], strict=False) if v is not None}
                if rates:
                    school(r)["retention_or_dropout_pct"] = {"school_year": y, **rates}
    cte = sheets.get("conclusaotempoesperado")
    if cte is not None:
        head, sub, subsub, data = _rows(cte)
        spans = year_spans(head)
        if spans:
            y, a, b = spans[-1]
            labels = [norm(x) for x in (subsub[a:b] if any(subsub[a:b]) and any("conclu" in norm(x) for x in subsub[a:b] if x) else sub[a:b])]
            pct_cols = [a + i for i, lab in enumerate(labels) if "conclu" in lab and "nao conclu" not in lab]
            for r in data:
                if len(pct_cols) >= 2:
                    mine, peers = _num(r[pct_cols[0]]), _num(r[pct_cols[1]])
                    if mine is not None:
                        school(r)["finished_on_time_pct"] = {"school_year": y, "school": round(mine * 100, 1),
                                                             "similar_students_nationally": round(peers * 100, 1) if peers is not None else None}
    pds = sheets.get("percursosdiretossucesso")
    if pds is not None:
        head, sub, _, data = _rows(pds)
        spans = year_spans(head)
        if spans:
            y, a, b = spans[-1]
            for r in data:
                mine, peers = _num(r[a + 1]) if a + 1 < len(r) else None, _num(r[a + 2]) if a + 2 < len(r) else None
                if mine is not None:
                    school(r)["direct_success_pct"] = {"school_year": y, "school": round(mine * 100, 1),
                                                       "similar_students_nationally": round(peers * 100, 1) if peers is not None else None,
                                                       "meaning": "passed the 9th-grade national tests after the 3rd cycle without repeating a year"}
    return {"year": year, "schools": schools}


def _latest_url(prefix: str) -> str:
    page = http().get(f"{SITE}/bds.asp", headers={"Accept": "text/html"}, timeout=30).text
    found = re.findall(rf"docs/(\d{{4}})/InfoEscolas\1_{prefix}_DadosPorEscola\.xlsx", page)
    if not found:
        raise ValueError("could not find the InfoEscolas file list")
    y = max(found)
    return f"{SITE}/docs/{y}/InfoEscolas{y}_{prefix}_DadosPorEscola.xlsx"


def _load(level: str) -> dict[str, Any]:
    with _lock:
        hit = _cache.get(level)
        if hit and time.time() - hit[0] < 7 * 86400:
            return hit[1]
        url = _latest_url(LEVELS[level][0])
        r = http().get(url, headers={"Accept": "*/*"}, timeout=120)
        r.raise_for_status()
        doc = parse_workbook(r.content)
        doc["file"] = url
        _cache[level] = (time.time(), doc)
        return doc


@mcp.tool()
def school_stats(school: str | None = None, municipality: str | None = None, level: str = "secundario", limit: int = 10) -> dict[str, Any]:
    """Statistics for schools in mainland Portugal (InfoEscolas, Ministry of Education).

    Args:
        school: part of a school's name ("Pedro Nunes", "Marquês de Pombal") or its DGEEC code.
        municipality: list that municipality's schools (e.g. "Braga").
        level: "1ciclo", "2ciclo", "3ciclo", "secundario" (science-humanities, default) or
            "profissional" (vocational).
    Each school has its students, retention or dropout rate per grade, the share finishing the
    cycle on time next to the national figure for students with a similar profile, and (3rd
    cycle) direct-success paths. The national totals come with every answer.
    """
    try:
        lv = pick_level(level)
        doc = _load(lv)
        schools = doc["schools"]
        n = clamp(limit, 1, 50, 10)
        rows = [s for k, s in schools.items() if k != "TOTALPT"]
        if school:
            q = norm(school)
            rows = [s for s in rows if s["code"] == q or q in norm(s["name"])]
        if municipality:
            from . import geo

            try:
                mname = norm(geo.municipality_code(municipality)["name"])
            except ValueError:
                mname = norm(municipality)
            rows = [s for s in rows if norm(s["municipality"]) == mname]
        if not school and not municipality:
            rows = []
        rows.sort(key=lambda s: (norm(s["municipality"]), norm(s["name"])))
        out: dict[str, Any] = {
            "level": LEVELS[lv][1],
            "school_year": doc.get("year"),
            "national": {k: v for k, v in (schools.get("TOTALPT") or {}).items() if k not in ("code", "name", "municipality")},
            "count": len(rows),
            "schools": rows[:n],
        }
        if not school and not municipality:
            out["note"] = "Pass a school name or a municipality to see schools."
        elif not rows:
            out["note"] = f"No {LEVELS[lv][1]} school matches; try another level or part of the name."
        out["source"] = SOURCE
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "school_stats", exc)
