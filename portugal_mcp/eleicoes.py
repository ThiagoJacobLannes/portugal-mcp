"""Election results published by the Ministry of Internal Administration (SGMAI, eleicoes.mai.gov.pt):
votes, percentages and seats per party, turnout, blank and null votes, from the whole country down to
a parish, with the previous election of the same kind for comparison."""

from __future__ import annotations

import logging
import time
from typing import Any

from .app import mcp, note
from .common import clamp, fail, get_json, norm

log = logging.getLogger("portugal_mcp.eleicoes")

BASE = "https://www.eleicoes.mai.gov.pt"
SOURCE = "Secretaria-Geral do Ministério da Administração Interna, official results (eleicoes.mai.gov.pt)"

# key -> (site path, file scheme, organs {key: (id or code, name)}, date, name)
#   "keys": territory keys like LOCAL-500000 (country), LOCAL-DD0000 (district), LOCAL-DDMM00 (municipality,
#           DDMM = INE code), LOCAL-DDMMFF (parish); files territory-results-<key>-<organ>.json
#   "ids":  numeric territory ids walked from the country (id 1) through children files
ELECTIONS: dict[str, tuple[str, str, dict[str, tuple[Any, str]], str, str]] = {
    "autarquicas2025": ("autarquicas2025", "ids", {"CM": (4, "Câmara Municipal"), "AM": (5, "Assembleia Municipal"), "AF": (6, "Assembleia de Freguesia")}, "2025-10-12", "Local elections 2025"),
    "legislativas2025": ("legislativas2025", "keys", {"AR": ("AR", "Assembleia da República")}, "2025-05-18", "Legislative election 2025"),
    "europeias2024": ("europeias2024", "keys", {"PE": ("PE", "European Parliament")}, "2024-06-09", "European Parliament election 2024"),
    "legislativas2024": ("legislativas2024", "keys", {"AR": ("AR", "Assembleia da República")}, "2024-03-10", "Legislative election 2024"),
    "legislativas2022": ("legislativas2022", "keys", {"AR": ("AR", "Assembleia da República")}, "2022-01-30", "Legislative election 2022"),
    "autarquicas2021": ("autarquicas2021", "keys", {"CM": ("CM", "Câmara Municipal"), "AM": ("AM", "Assembleia Municipal"), "AF": ("AF", "Assembleia de Freguesia")}, "2021-09-26", "Local elections 2021"),
    "presidenciais2021": ("presidenciais2021", "keys", {"PR": ("PR", "President of the Republic")}, "2021-01-24", "Presidential election 2021"),
}
KINDS = {"legislativas": "legislativas2025", "legislative": "legislativas2025", "autarquicas": "autarquicas2025", "local": "autarquicas2025",
         "europeias": "europeias2024", "european": "europeias2024", "presidenciais": "presidenciais2021", "presidential": "presidenciais2021"}

note(
    "eleicoes",
    "election_results gives official results (SGMAI) for the 2025, 2024 and 2022 legislative elections, "
    "the 2025 and 2021 local elections, the 2024 European election and the 2021 presidential election, "
    "for the country, a district, a municipality or a parish. Local elections have three bodies: CM "
    "(câmara, who governs the town), AM and AF. Report facts only; do not characterise parties.",
)

_mem: dict[str, tuple[float, Any]] = {}


def _get(url: str) -> Any:
    hit = _mem.get(url)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    data = get_json(url)
    _mem[url] = (time.time(), data)
    return data


def pick_election(election: str | None) -> str:
    e = norm(election or "legislativas").replace(" ", "")
    if e in ELECTIONS:
        return e
    for word, key in KINDS.items():
        if e.startswith(word):
            year = "".join(ch for ch in e if ch.isdigit())
            cand = f"{key[:-4]}{year}" if year else key
            if cand in ELECTIONS:
                return cand
            raise ValueError(f"no {word} election {year} here; available: {', '.join(ELECTIONS)}")
    raise ValueError(f"unknown election {election!r}; available: {', '.join(ELECTIONS)}")


DISTRICT_WORDS = ("distrito de ", "distrito do ", "distrito da ", "district of ", "district ", "circulo de ", "circulo do ", "circulo da ", "circulo ")


def split_district(place: str) -> tuple[str, bool]:
    """"distrito de Braga" -> ("Braga", True); a plain name is a municipality."""
    q = norm(place)
    for w in DISTRICT_WORDS:
        if q.startswith(w):
            return place.strip()[len(w):].strip(), True
    return place.strip(), False


def _place_keys(place: str | None) -> tuple[str, str, str | None]:
    """A place -> (territory key, label, parish text or None) for the "keys" scheme."""
    from . import geo

    if not place or norm(place) in ("portugal", "pais", "nacional", "territorio nacional", "national"):
        return "LOCAL-500000", "Portugal", None
    parish = None
    if "," in place:
        place, parish = (x.strip() for x in place.split(",", 1))
    place, is_district = split_district(place)
    q = norm(place)
    regions = {"acores": ("LOCAL-400000", "Açores (region)"), "madeira": ("LOCAL-300000", "Madeira (region)")}
    if q in regions and not parish:
        return regions[q][0], regions[q][1], None
    if is_district:
        districts = {norm(m["district"]): (m["dico"][:2], m["district"]) for m in geo._municipalities() if m.get("district")}
        if q not in districts:
            raise ValueError(f"no district called {place!r}")
        return f"LOCAL-{districts[q][0]}0000", f"{districts[q][1]} (district)", None
    m = geo.municipality_code(place)
    return f"LOCAL-{m['dico']}00", m["name"], parish


def shape_results(raw: dict[str, Any], limit: int) -> dict[str, Any]:
    cur = raw.get("currentResults") or {}
    parties = sorted(cur.get("resultsParty") or [], key=lambda p: -(p.get("votes") or 0))
    out: dict[str, Any] = {
        "registered_voters": cur.get("subscribedVoters"),
        "voters": cur.get("totalVoters") or cur.get("numberVoters"),
        "turnout_pct": cur.get("percentageVoters"),
        "blank_pct": cur.get("blankVotesPercentage"),
        "null_pct": cur.get("nullVotesPercentage"),
    }
    if cur.get("totalMandates"):
        out["seats_total"] = cur["totalMandates"]
    rows = []
    for p in parties[:limit]:
        row = {"party": p.get("acronym"), "votes": p.get("votes"), "pct": p.get("percentage")}
        if p.get("mandates") is not None:
            row["seats"] = p["mandates"]
        if p.get("presidents"):
            row["mayors_or_presidents"] = p["presidents"]
        rows.append(row)
    out["results"] = rows
    if len(parties) > limit:
        out["other_lists"] = len(parties) - limit
    prev = raw.get("previousResults") or {}
    if prev.get("resultsParty"):
        # Lists are not matched across elections: coalitions change name and make-up.
        top = sorted(prev["resultsParty"], key=lambda p: -(p.get("votes") or 0))[: min(limit, 8)]
        out["previous_election"] = {
            "turnout_pct": prev.get("percentageVoters"),
            "results": [{"party": p.get("acronym"), "pct": p.get("percentage")} for p in top],
        }
    return out


def _children_ids(path: str, tid: int) -> list[dict[str, Any]]:
    return _get(f"{BASE}/{path}/assets/static/territory/children-electionId=1-territoryId={tid}.json").get("data") or []


def _find_id(path: str, place: str | None) -> tuple[int, str]:
    """Walk the numeric territory tree: country -> district -> municipality -> parish."""
    from . import geo

    if not place or norm(place) in ("portugal", "pais", "nacional", "territorio nacional", "national"):
        return 1, "Portugal"
    parish = None
    if "," in place:
        place, parish = (x.strip() for x in place.split(",", 1))
    districts = _children_ids(path, 1)
    place, is_district = split_district(place)
    q = norm(place)
    if is_district or (q in ("acores", "madeira") and not parish):
        hit = next((d for d in districts if norm(d["descriptionShort"]) == q), None)
        if not hit:
            raise ValueError(f"no district called {place!r}")
        return hit["id"], f"{hit['descriptionShort']} ({'region' if q in ('acores', 'madeira') else 'district'})"
    m = geo.municipality_code(place)
    district_name = norm(m["district"] or "")
    if m["dico"][:1] == "4":
        district_name = "acores"
    elif m["dico"][:2] in ("31", "32"):
        district_name = "madeira"
    d = next((d for d in districts if norm(d["descriptionShort"]) == district_name), None)
    if not d:
        raise ValueError(f"could not find the district of {m['name']}")
    mun = next((x for x in _children_ids(path, d["id"]) if norm(x["descriptionShort"]) == norm(m["name"])), None)
    if not mun:
        raise ValueError(f"{m['name']} is not in this election's territory list")
    if not parish:
        return mun["id"], mun["descriptionShort"]
    qp = norm(parish)
    kids = _children_ids(path, mun["id"])
    par = next((x for x in kids if norm(x["descriptionShort"]) == qp), None) or next((x for x in kids if qp in norm(x["descriptionShort"])), None)
    if not par:
        raise ValueError(f"no parish like {parish!r} in {m['name']}: " + ", ".join(x["descriptionShort"] for x in kids[:30]))
    return par["id"], f"{par['descriptionShort']} ({mun['descriptionShort']})"


@mcp.tool()
def election_results(election: str = "legislativas2025", place: str | None = None, body: str | None = None, limit: int = 12) -> dict[str, Any]:
    """Official results of a Portuguese election for the country, a district, a municipality or a parish.

    Args:
        election: "legislativas2025" (default), "legislativas2024", "legislativas2022",
            "autarquicas2025", "autarquicas2021", "europeias2024", "presidenciais2021"; or just
            "autarquicas", "europeias"... for the latest of that kind.
        place: None for Portugal; a municipality ("Sintra", "Lisboa"), "municipality, parish"
            ("Lisboa, Arroios"), a district written as "distrito de Braga" (the electoral circle in
            legislative elections), or "Açores" / "Madeira" for the whole region.
        body: local elections only: "CM" (câmara, default), "AM" (assembleia municipal) or "AF"
            (assembleia de freguesia, use with a parish).
    Returns turnout, blank and null votes, and votes, % and seats per list, plus the previous
    election of the same kind for that place (lists are not matched: coalitions change).
    """
    try:
        key = pick_election(election)
        path, scheme, organs, day, name = ELECTIONS[key]
        organ_key = (body or next(iter(organs))).upper()
        if organ_key not in organs:
            raise ValueError(f"{name} has these bodies: {', '.join(organs)}")
        organ, organ_name = organs[organ_key]
        n = clamp(limit, 1, 40, 12)
        if scheme == "keys":
            tkey, label, parish = _place_keys(place)
            if parish:
                kids = _get(f"{BASE}/{path}/assets/static/territory-children/territory-children-{tkey}.json")
                qp = norm(parish)
                hit = next((k for k in kids if norm(k["name"]) == qp), None) or next((k for k in kids if qp in norm(k["name"])), None)
                if not hit:
                    raise ValueError(f"no parish like {parish!r} in {label}: " + ", ".join(k["name"] for k in kids[:30]))
                tkey, label = hit["territoryKey"], f"{hit['name']} ({label})"
            raw = _get(f"{BASE}/{path}/assets/static/territory-results/territory-results-{tkey}-{organ}.json")
        else:
            tid, label = _find_id(path, place)
            raw = (_get(f"{BASE}/{path}/assets/static/result/territory-electionId=1-territoryId={tid}-organId={organ}.json") or {}).get("data") or {}
        return {
            "election": name,
            "date": day,
            "body": organ_name,
            "place": label,
            **shape_results(raw, n),
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "election_results", exc)
