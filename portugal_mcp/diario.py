"""Diário da República: what was published in the official gazette in its latest issue (official RSS
of INCM), Series I (laws, decree-laws, government resolutions, ordinances) and Series II (dispatches,
notices, appointments, tenders)."""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

from .app import mcp, note
from .common import clamp, fail, http, norm

log = logging.getLogger("portugal_mcp.diario")

FEED = "https://files.diariodarepublica.pt/rss/serie{n}-html.xml"
SOURCE = "Diário da República, official RSS of the latest issue (diariodarepublica.pt, INCM)"

note(
    "diario",
    "diario_republica lists what the latest Diário da República issue published (Series I: laws, "
    "decree-laws, ordinances, resolutions; Series II: dispatches, notices, appointments), with a link "
    "to each act. It covers the latest issue only; it cannot search older legislation.",
)

_mem: dict[int, tuple[float, dict[str, Any]]] = {}


def _cdata(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_feed(xml: str) -> dict[str, Any]:
    desc = re.search(r"<channel>.*?<description>(.*?)</description>", xml, re.S)
    day = re.search(r"(\d{4}-\d{2}-\d{2})", desc.group(1)) if desc else None
    acts = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = _cdata((re.search(r"<title>(.*?)</title>", item, re.S) or [None, ""])[1])
        body = _cdata((re.search(r"<description[^>]*>(.*?)</description>", item, re.S) or [None, ""])[1])
        link = _cdata((re.search(r"<link>(.*?)</link>", item, re.S) or [None, ""])[1])
        act, _, issue = title.partition(" - ")
        acts.append({"act": act.strip(), "issue": issue.strip() or None, "summary": body, "link": link or None})
    return {"day": day.group(1) if day else None, "acts": acts}


def _feed(series: int) -> dict[str, Any]:
    hit = _mem.get(series)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    r = http().get(FEED.format(n=series), headers={"Accept": "application/rss+xml, text/xml"}, timeout=30)
    r.raise_for_status()
    doc = parse_feed(r.text)
    _mem[series] = (time.time(), doc)
    return doc


@mcp.tool()
def diario_republica(series: int = 1, query: str | None = None, limit: int = 30) -> dict[str, Any]:
    """What the latest Diário da República issue published, with a link to each act.

    Args:
        series: 1 (laws, decree-laws, ordinances, government resolutions; default) or 2
            (dispatches, notices, appointments, tenders: hundreds per day).
        query: optional words to filter titles and summaries, e.g. "IRS", "habitação", "Saúde".
    """
    try:
        if series not in (1, 2):
            raise ValueError("series is 1 or 2")
        doc = _feed(series)
        acts = doc["acts"]
        if query:
            words = norm(query).split()
            acts = [a for a in acts if all(w in norm(f"{a['act']} {a['summary']}") for w in words)]
        n = clamp(limit, 1, 100, 30)
        return {
            "series": series,
            "issue_date": doc["day"],
            "count": len(acts),
            "acts": acts[:n],
            "note": "Latest issue only (including its supplements). For older acts, search diariodarepublica.pt.",
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "diario_republica", exc)
