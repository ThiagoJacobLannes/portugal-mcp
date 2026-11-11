"""Parliament (Assembleia da República) open data: initiatives, votes, deputies — read-only."""

from __future__ import annotations

import base64
import html
import logging
import re
from datetime import date
from typing import Any

from .app import mcp, note
from .common import BigFileCache, FileCache, cache_dir, clamp, download, fail, get_json, iter_json_array, keywords, norm

log = logging.getLogger("portugal_mcp.parlamento")

CURRENT = "XVII"
SOURCE = "Assembleia da República, dados abertos (parlamento.pt)"
DOCS = "https://app.parlamento.pt/webutils/docs/doc.txt"

note(
    "parlamento",
    f"Parliament tools read the official open-data files of the current legislature ({CURRENT}, since "
    "June 2025) unless another is asked for. The first call downloads ~100 MB and takes a minute or two; "
    "after that it is cached for a day. Votes are by parliamentary group, as the Parliament records them. "
    "Report facts only; do not characterise parties or deputies.",
)

_ROMAN = re.compile(r"^(X{0,3})(IX|IV|V?I{0,3})$")


def _leg(legislature: str | None) -> str:
    leg = (legislature or CURRENT).strip().upper()
    if not leg or not _ROMAN.match(leg):
        raise ValueError("legislature is a Roman numeral, e.g. 'XVII' (current) or 'XVI'")
    return leg


def _url(folder: str, name: str) -> str:
    path = f"http://arnet/opendata/DadosAbertos/{folder}/{name}"
    token = base64.b64encode(path.encode("utf-8")).hex()
    return f"{DOCS}?path={token}&fich={name.rsplit('/', 1)[-1]}&Inline=true"


def _iniciativas_url(leg: str) -> str:
    return _url(f"Iniciativas/{leg}%20Legislatura", f"Iniciativas{leg}_json.txt")


def _base_url(leg: str) -> str:
    return _url(f"Informação%20base/{leg}%20Legislatura", f"InformacaoBase{leg}_json.txt")


_VOTE_LABELS = {"a favor": "for", "contra": "against", "abstencao": "abstain", "ausencia": "absent"}


def parse_vote_detail(detail: str | None) -> dict[str, list[str]]:
    """'A Favor: <I>PSD</I>, <I> CH</I><BR>Contra:<I>PCP</I>' -> {'for': ['PSD','CH'], 'against': ['PCP']}."""
    out: dict[str, list[str]] = {}
    if not detail:
        return out
    for part in re.split(r"<br\s*/?>", detail, flags=re.I):
        text = html.unescape(re.sub(r"<[^>]+>", "", part))
        if ":" not in text:
            continue
        label, names = text.split(":", 1)
        key = _VOTE_LABELS.get(norm(label).rstrip("s"), _VOTE_LABELS.get(norm(label), norm(label)))
        out.setdefault(key, []).extend(n.strip() for n in names.split(",") if n.strip())
    return out


def _compact(ini: dict[str, Any]) -> dict[str, Any]:
    events = ini.get("IniEventos") or []
    phases = [{"date": e.get("DataFase"), "phase": e.get("Fase")} for e in events if e.get("Fase")]
    votes = []
    for e in events:
        for v in e.get("Votacao") or []:
            votes.append({
                "date": v.get("data") or e.get("DataFase"),
                "phase": e.get("Fase"),
                "result": v.get("resultado"),
                "unanimous": (v.get("unanime") or "").lower() == "unanime" or None,
                "positions": parse_vote_detail(v.get("detalhe")),
                "description": (v.get("descricao") or "")[:300] or None,
            })
    other = ini.get("IniAutorOutros") or {}
    groups = sorted({g.get("GP") for g in ini.get("IniAutorGruposParlamentares") or [] if g.get("GP")})
    deputies = ini.get("IniAutorDeputados") or []
    author = ", ".join(groups) if groups else (other.get("nome") or None)
    return {
        "id": ini.get("IniId"),
        "type": ini.get("IniDescTipo"),
        "number": ini.get("IniNr"),
        "ref": f"{ini.get('IniDescTipo')} {ini.get('IniNr')}/{ini.get('IniLeg')}",
        "title": (ini.get("IniTitulo") or "").strip(),
        "author": author,
        "author_groups": groups,
        "author_deputies": len(deputies),
        "submitted": phases[0]["date"] if phases else None,
        "last_phase": phases[-1] if phases else None,
        "phases": phases,
        "votes": votes,
        "text_url": (ini.get("IniLinkTexto") or "").replace("http://", "https://", 1) or None,
    }


def _initiatives(leg: str) -> list[dict[str, Any]]:
    def build(url: str) -> list[dict[str, Any]]:
        dest = cache_dir("parlamento") / f"Iniciativas{leg}.json.download"
        download(url, dest)
        try:
            with open(dest, encoding="utf-8-sig") as f:
                return [_compact(x) for x in iter_json_array(f)]
        finally:
            dest.unlink(missing_ok=True)

    return BigFileCache("parlamento", f"iniciativas-{leg}", _iniciativas_url(leg)).get(build)["data"]


def _deputies(leg: str) -> dict[str, Any]:
    def build(url: str) -> dict[str, Any]:
        d = get_json(url, timeout=120)
        today = date.today().isoformat()
        rows = []
        for dep in d.get("Deputados") or []:
            sit = sorted(dep.get("DepSituacao") or [], key=lambda s: s.get("sioDtInicio") or "")
            cur = sit[-1] if sit else {}
            gps = sorted(dep.get("DepGP") or [], key=lambda g: g.get("gpDtInicio") or "")
            gp = gps[-1] if gps else {}
            rows.append({
                "name": dep.get("DepNomeParlamentar"),
                "full_name": dep.get("DepNomeCompleto"),
                "party": gp.get("gpSigla"),
                "constituency": dep.get("DepCPDes"),
                "status": cur.get("sioDes"),
                "status_since": cur.get("sioDtInicio"),
                "active": bool(cur) and not cur.get("sioDtFim") and norm(cur.get("sioDes")).startswith("efetiv"),
                "position": dep.get("DepCargo"),
            })
        return {
            "groups": [{"acronym": g.get("sigla"), "name": g.get("nome")} for g in d.get("GruposParlamentares") or []],
            "deputies": rows,
            "legislature": d.get("DetalheLegislatura"),
            "as_of": today,
        }

    return FileCache("parlamento", f"base-{leg}", _base_url(leg)).get(build)["data"]


def _vote_summary(v: dict[str, Any]) -> dict[str, Any]:
    return {k: v[k] for k in ("date", "phase", "result", "unanimous", "positions")}


@mcp.tool()
def parliament_search(
    query: str | None = None,
    type: str | None = None,
    author: str | None = None,
    result: str | None = None,
    since: str | None = None,
    legislature: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search initiatives in the Portuguese Parliament: bills, resolutions, inquiries.

    Args:
        query: words in the title, e.g. "habitação", "IRS", "imigração".
        type: e.g. "Projeto de Lei", "Proposta de Lei" (Government bills), "Projeto de Resolução".
        author: party acronym ("PS", "PSD", "CH", "IL", "L", "PCP", "BE", "CDS-PP", "PAN", "JPP") or "Governo".
        result: "aprovado" or "rejeitado" — keeps initiatives whose latest vote had this result.
        since: "YYYY-MM-DD" — submitted on or after this date.
        legislature: Roman numeral, default the current one (XVII).
        limit: max results, newest first.
    """
    try:
        leg = _leg(legislature)
        rows = _initiatives(leg)
        words = keywords(query) if query else []
        out = []
        for r in rows:
            if words and not all(w in norm(r["title"]) for w in words):
                continue
            if type and norm(type) not in norm(r["type"]):
                continue
            if author:
                a = norm(author)
                if a not in [norm(g) for g in r["author_groups"]] and a not in norm(r["author"] or ""):
                    continue
            if result:
                last = r["votes"][-1]["result"] if r["votes"] else None
                if not last or not norm(last).startswith(norm(result)[:6]):
                    continue
            if since and (r["submitted"] or "") < since:
                continue
            out.append(r)
        out.sort(key=lambda r: r["submitted"] or "", reverse=True)
        n = clamp(limit, 1, 50, 10)
        return {
            "legislature": leg,
            "count": len(out),
            "initiatives": [
                {
                    **{k: r[k] for k in ("id", "ref", "title", "author", "submitted", "last_phase", "text_url")},
                    "last_vote": _vote_summary(r["votes"][-1]) if r["votes"] else None,
                }
                for r in out[:n]
            ],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "parliament_search", exc)


@mcp.tool()
def parliament_initiative(initiative_id: str, legislature: str | None = None) -> dict[str, Any]:
    """Full record of one initiative: every phase with dates, every vote with each group's position, text link.

    Args:
        initiative_id: the `id` from parliament_search (e.g. "356403"), or a reference like "Proposta de Lei 3".
    """
    try:
        leg = _leg(legislature)
        rows = _initiatives(leg)
        key = str(initiative_id).strip()
        hit = next((r for r in rows if r["id"] == key), None)
        if not hit:
            m = re.match(r"(.+?)\s*n?\.?º?\s*(\d+)", key)
            if m:
                t, num = norm(m.group(1)), m.group(2)
                hit = next((r for r in rows if r["number"] == num and t in norm(r["type"])), None)
        if not hit:
            return {"error": f"no initiative {initiative_id!r} in legislature {leg}; use parliament_search"}
        return {**hit, "legislature": leg, "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "parliament_initiative", exc)


@mcp.tool()
def parliament_votes(
    party: str | None = None,
    query: str | None = None,
    since: str | None = None,
    result: str | None = None,
    legislature: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Recent plenary votes, with how each parliamentary group voted.

    Args:
        party: show this group's position on each vote (e.g. "PS", "CH"); only votes where it is recorded.
        query: words in the initiative title.
        since: "YYYY-MM-DD".
        result: "aprovado" or "rejeitado".
        limit: max votes, newest first.
    """
    try:
        leg = _leg(legislature)
        words = keywords(query) if query else []
        p = norm(party) if party else None
        out = []
        for r in _initiatives(leg):
            if words and not all(w in norm(r["title"]) for w in words):
                continue
            for v in r["votes"]:
                if since and (v["date"] or "") < since:
                    continue
                if result and not norm(v["result"]).startswith(norm(result)[:6]):
                    continue
                row = {"initiative_id": r["id"], "ref": r["ref"], "title": r["title"], **_vote_summary(v)}
                if p:
                    pos = next((k for k, names in v["positions"].items() if p in [norm(n) for n in names]), None)
                    if pos is None and v.get("unanimous"):
                        pos = "for (unanimous)"
                    if pos is None:
                        continue
                    row["party_position"] = pos
                out.append(row)
        out.sort(key=lambda v: v["date"] or "", reverse=True)
        n = clamp(limit, 1, 100, 20)
        summary = None
        if p:
            counts: dict[str, int] = {}
            for v in out:
                counts[v["party_position"]] = counts.get(v["party_position"], 0) + 1
            summary = {"party": party, "positions": counts}
        return {"legislature": leg, "count": len(out), "summary": summary, "votes": out[:n], "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "parliament_votes", exc)


@mcp.tool()
def parliament_deputies(
    name: str | None = None,
    party: str | None = None,
    constituency: str | None = None,
    include_inactive: bool = False,
    legislature: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Members of Parliament (deputados): party, constituency (círculo), status. Seats per party included.

    Args:
        name: part of the parliamentary or full name.
        party: group acronym, e.g. "PSD", "PS", "CH".
        constituency: e.g. "Lisboa", "Porto", "Açores", "Europa".
        include_inactive: also list deputies who left or were replaced. Default false.
    """
    try:
        leg = _leg(legislature)
        d = _deputies(leg)
        deps = d["deputies"]
        seats: dict[str, int] = {}
        for x in deps:
            if x["active"]:
                seats[x["party"] or "?"] = seats.get(x["party"] or "?", 0) + 1
        rows = [
            x for x in deps
            if (include_inactive or x["active"])
            and (not name or norm(name) in norm(x["name"]) or norm(name) in norm(x["full_name"]))
            and (not party or norm(party) == norm(x["party"]))
            and (not constituency or norm(constituency) in norm(x["constituency"]))
        ]
        rows.sort(key=lambda x: (x["party"] or "", x["name"] or ""))
        n = clamp(limit, 1, 300, 50)
        return {
            "legislature": leg,
            "seats_by_party": dict(sorted(seats.items(), key=lambda kv: -kv[1])),
            "seats_total": sum(seats.values()),
            "groups": d["groups"],
            "count": len(rows),
            "deputies": rows[:n],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "parliament_deputies", exc)
