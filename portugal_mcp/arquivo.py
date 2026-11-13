"""Arquivo.pt, the Portuguese web archive (FCT): full-text search of archived pages, the capture
history of an address, and the text of a page as it was on a given date."""

from __future__ import annotations

import calendar
import html
import json
import logging
import re
from datetime import date
from typing import Any

from .app import mcp, note
from .common import clamp, fail, get_json, http

log = logging.getLogger("portugal_mcp.arquivo")

BASE = "https://arquivo.pt"
SOURCE = "Arquivo.pt, the Portuguese web archive (arquivo.pt, FCT)"

note(
    "arquivo",
    "Arquivo.pt keeps copies of Portuguese websites (mostly .pt) since 1996. web_archive_search finds "
    "archived pages by words, web_archive_history shows when an address was captured and how often it "
    "changed, web_archive_page gives the text of a page as it was on a date. The newest copy of a "
    "page can be weeks or months old, and sites that block crawlers are missing. Quote archived text "
    "briefly and give the archive link.",
)


def _ts(d: Any, end: bool = False) -> str | None:
    """'2015', '2015-06' or '2015-06-01' (or an int year) -> a 14-digit Wayback timestamp."""
    if d in (None, ""):
        return None
    digits = re.sub(r"\D", "", str(d))
    if len(digits) not in (4, 6, 8):
        raise ValueError(f"dates look like 2015, 2015-06 or 2015-06-01, not {d!r}")
    if not end:
        return digits + {4: "0101000000", 6: "01000000", 8: "000000"}[len(digits)]
    if len(digits) == 6:
        digits += f"{calendar.monthrange(int(digits[:4]), int(digits[4:6]))[1]:02d}"
    return digits + {4: "1231235959", 8: "235959"}[len(digits)]


def _iso(ts: str) -> str:
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" + (f"T{ts[8:10]}:{ts[10:12]}" if len(ts) >= 12 else "")


def _replay(ts: str, url: str) -> str:
    return f"{BASE}/wayback/{ts}/{url}"


def _strip(snippet: str | None) -> str | None:
    if not snippet:
        return None
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", snippet))).strip()


def _cdx(params: dict[str, Any]) -> list[dict[str, Any]]:
    r = http().get(f"{BASE}/wayback/cdx", params={"output": "json", **params}, timeout=60)
    r.raise_for_status()
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def page_text(raw_html: str) -> tuple[str | None, str]:
    """(title, readable text) from an archived HTML page."""
    t = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.S | re.I)
    title = _strip(t.group(1)) if t else None
    body = re.sub(r"<(script|style|noscript|svg|head)\b.*?</\1\s*>", " ", raw_html, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"<(br|p|div|li|h[1-6]|tr|section|article)\b[^>]*>", "\n", body, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    return title, "\n".join(ln for ln in lines if ln)


@mcp.tool()
def web_archive_search(
    query: str,
    site: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search old versions of Portuguese websites in Arquivo.pt (full text, since 1996).

    `site` limits to one domain (e.g. "publico.pt", "cm-porto.pt"). `from_date`/`to_date` take a
    year, "YYYY-MM" or "YYYY-MM-DD". Each result has the capture date, title, a short snippet and a
    link to the archived copy. Good for "what did the news say about X in 2010?".
    """
    try:
        if not (query or "").strip():
            raise ValueError("give some words to search for")
        params: dict[str, Any] = {"q": query.strip(), "maxItems": clamp(limit, 1, 50, 10), "snippetMaxLength": 250}
        if site:
            params["siteSearch"] = re.sub(r"^https?://", "", site.strip()).strip("/")
        if from_date:
            params["from"] = _ts(from_date)
        if to_date:
            params["to"] = _ts(to_date, end=True)
        data = get_json(f"{BASE}/textsearch", params)
        items = [
            {
                "date": _iso(it.get("tstamp") or ""),
                "title": _strip(it.get("title")),
                "url": it.get("originalURL"),
                "snippet": _strip(it.get("snippet")),
                "archive_link": it.get("linkToArchive"),
            }
            for it in data.get("response_items") or []
        ][: params["maxItems"]]
        return {"query": query, "site": params.get("siteSearch"), "estimated_matches": data.get("estimated_nr_results"), "count": len(items), "results": items, "source": SOURCE}
    except Exception as exc:  # noqa: BLE001
        return fail(log, "web_archive_search", exc)


@mcp.tool()
def web_archive_history(url: str, from_date: str | None = None, to_date: str | None = None, max_versions: int = 12) -> dict[str, Any]:
    """When was a web address captured by Arquivo.pt, and how often did its content change?

    Returns the first and last capture, captures per year and up to `max_versions` versions where
    the content changed (spread over the period), each with a link to the archived copy.
    """
    try:
        if not (url or "").strip():
            raise ValueError("give a web address, e.g. 'cm-lisboa.pt' or 'https://www.sns.gov.pt/'")
        params: dict[str, Any] = {"url": url.strip(), "fl": "timestamp,url,digest", "filter": "status:200"}
        if from_date:
            params["from"] = _ts(from_date)
        if to_date:
            params["to"] = _ts(to_date, end=True)
        caps = sorted(_cdx(params), key=lambda c: c["timestamp"])
        if not caps:
            return {"url": url, "captures": 0, "note": "Arquivo.pt has no copy of this address in that period (it may block crawlers, or be too recent).", "source": SOURCE}
        per_year: dict[str, int] = {}
        changes: list[dict[str, Any]] = []
        last_digest = None
        for c in caps:
            per_year[c["timestamp"][:4]] = per_year.get(c["timestamp"][:4], 0) + 1
            if c.get("digest") != last_digest:
                changes.append(c)
                last_digest = c.get("digest")
        n = clamp(max_versions, 1, 50, 12)
        pick = changes if len(changes) <= n else [changes[round(i * (len(changes) - 1) / (n - 1))] for i in range(n)] if n > 1 else [changes[-1]]
        return {
            "url": url,
            "captures": len(caps),
            "distinct_contents": len(changes),
            "first": {"date": _iso(caps[0]["timestamp"]), "archive_link": _replay(caps[0]["timestamp"], caps[0]["url"])},
            "last": {"date": _iso(caps[-1]["timestamp"]), "archive_link": _replay(caps[-1]["timestamp"], caps[-1]["url"])},
            "captures_per_year": per_year,
            "sample_versions": [{"date": _iso(c["timestamp"]), "archive_link": _replay(c["timestamp"], c["url"])} for c in pick],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "web_archive_history", exc)


@mcp.tool()
def web_archive_page(url: str, when: str | None = None, max_chars: int = 3000) -> dict[str, Any]:
    """The text of a web page as Arquivo.pt saved it closest to a date.

    `when` takes a year, "YYYY-MM" or "YYYY-MM-DD" (default: the latest copy). Returns the capture
    date, the page title, its readable text (cut at `max_chars`, up to 8000) and the archive link.
    Quote it briefly; the archive link shows the full page.
    """
    try:
        if not (url or "").strip():
            raise ValueError("give a web address")
        params: dict[str, Any] = {"url": url.strip(), "fl": "timestamp,url,status,mime", "filter": "status:200", "limit": 1}
        if when:
            params.update(closest=_ts(when)[:8], sort="closest")
        else:
            params["sort"] = "reverse"
        caps = _cdx(params)
        if not caps:
            return {"url": url, "found": False, "note": "Arquivo.pt has no copy of this address.", "source": SOURCE}
        c = caps[0]
        out: dict[str, Any] = {"url": c["url"], "found": True, "captured": _iso(c["timestamp"]), "archive_link": _replay(c["timestamp"], c["url"])}
        if when:
            asked = _ts(when)[:8]
            gap = abs((date(int(c["timestamp"][:4]), int(c["timestamp"][4:6]), int(c["timestamp"][6:8])) - date(int(asked[:4]), int(asked[4:6]), int(asked[6:8]))).days)
            if gap > 31:
                out["note"] = f"The closest copy is {gap} days away from the date asked."
        if "html" not in (c.get("mime") or "html"):
            out["note"] = f"This capture is {c.get('mime')}, not a web page: open the archive link."
            out["source"] = SOURCE
            return out
        r = http().get(f"{BASE}/noFrame/replay/{c['timestamp']}id_/{c['url']}", headers={"Accept": "text/html"}, timeout=60)
        r.raise_for_status()
        title, text = page_text(r.text[:3_000_000])
        cap = clamp(max_chars, 200, 8000, 3000)
        out.update(title=title, text=text[:cap], truncated=len(text) > cap, source=SOURCE)
        return out
    except Exception as exc:  # noqa: BLE001
        return fail(log, "web_archive_page", exc)
