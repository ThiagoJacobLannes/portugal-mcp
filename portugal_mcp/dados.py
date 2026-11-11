"""The national open-data portal dados.gov.pt (udata API): search datasets, inspect, preview files."""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any
from urllib.parse import urlparse

from .app import mcp, note
from .common import clamp, fail, get_json, http, norm

log = logging.getLogger("portugal_mcp.dados")

API = "https://dados.gov.pt/api/1"
SOURCE = "dados.gov.pt (Portal de Dados Abertos da Administração Pública)"
PREVIEW_HOSTS = ("dados.gov.pt",)
PREVIEW_BYTES = 512 * 1024

note(
    "dados",
    "dados.gov.pt holds ~6,000 open datasets from public bodies. Prefer the specialised tools when "
    "one fits; use open_data_search for anything else, open_data_dataset for its files, and "
    "open_data_preview to peek at a CSV or JSON file hosted on dados.gov.pt.",
)


def _dataset_row(d: dict[str, Any]) -> dict[str, Any]:
    org = d.get("organization") or {}
    return {
        "id": d.get("id"),
        "slug": d.get("slug"),
        "title": d.get("title"),
        "organization": org.get("name"),
        "last_update": (d.get("last_update") or "")[:10] or None,
        "frequency": d.get("frequency"),
        "formats": sorted({(r.get("format") or "").lower() for r in d.get("resources") or [] if r.get("format")}),
        "page": d.get("page"),
    }


def _org(name: str) -> dict[str, Any] | None:
    """Find a publisher. Town halls publish under slugs like municipio-de-lisboa-1 or
    camara-municipal-de-cascais, which the portal's search does not always surface, so try those."""
    q = norm(name)
    known = {"ine": "instituto-nacional-de-estatistica", "turismo de portugal": "turismo-de-portugal-i-p"}
    if q in known:
        r = http().get(f"{API}/organizations/{known[q]}/", timeout=20)
        if r.status_code == 200:
            return r.json()
    city = re.sub(r"^(camara municipal|municipio|cm)\s+(de|do|da|dos|das)\s+", "", q).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", city).strip("-")
    found: list[dict[str, Any]] = []
    if slug:
        for prefix in ("municipio-de", "municipio-do", "municipio-da", "camara-municipal-de", "camara-municipal-do", "camara-municipal-da"):
            for suffix in ("", "-1"):
                try:
                    r = http().get(f"{API}/organizations/{prefix}-{slug}{suffix}/", timeout=20)
                except Exception:  # noqa: BLE001 — best effort
                    log.debug("organization slug probe failed", exc_info=True)
                    continue
                if r.status_code == 200:
                    found.append(r.json())
    if found:
        return max(found, key=lambda o: (o.get("metrics") or {}).get("datasets") or 0)
    orgs = get_json(f"{API}/organizations/", {"q": name, "page_size": 20}).get("data") or []
    # the portal's organization search is loose ("INE" returns unrelated bodies): only accept a name match
    exact = [o for o in orgs if norm(o.get("name")) == q]
    named = [o for o in orgs if q in norm(o.get("name"))]
    return (exact or named or [None])[0]


@mcp.tool()
def open_data_search(query: str, organization: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search the datasets of Portugal's national open-data portal (dados.gov.pt).

    Args:
        query: e.g. "acidentes rodoviários", "escolas", "qualidade do ar", "alojamento local".
        organization: optional publisher to filter by, e.g. "INE", "Lisboa" or "Câmara Municipal de
            Cascais" (a town name finds its town hall's datasets: Lisboa, Porto, Cascais, Oeiras...).
        limit: max results (1-50).
    """
    try:
        params: dict[str, Any] = {"q": query, "page_size": clamp(limit, 1, 50, 10)}
        if organization:
            org = _org(organization)
            if not org:
                return {"error": f"no organization matches {organization!r}"}
            params["organization"] = org["id"]
        d = get_json(f"{API}/datasets/", params)
        return {
            "query": query,
            **({"organization": org.get("name")} if organization else {}),
            "total": d.get("total"),
            "datasets": [_dataset_row(x) for x in d.get("data") or []],
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "open_data_search", exc)


@mcp.tool()
def open_data_dataset(dataset: str) -> dict[str, Any]:
    """Details and downloadable files of one dados.gov.pt dataset (id or slug from open_data_search)."""
    try:
        if not re.fullmatch(r"[\w\-]+", dataset or ""):
            raise ValueError("give the dataset id or slug")
        d = get_json(f"{API}/datasets/{dataset}/")
        desc = re.sub(r"\s+", " ", d.get("description") or "").strip()
        return {
            **_dataset_row(d),
            "description": desc[:1500] + ("…" if len(desc) > 1500 else ""),
            "license": d.get("license"),
            "resources": [
                {
                    "title": r.get("title"),
                    "format": r.get("format"),
                    "size_bytes": r.get("filesize"),
                    "updated": (r.get("last_modified") or "")[:10] or None,
                    "url": r.get("url"),
                }
                for r in (d.get("resources") or [])[:40]
            ],
            "resources_total": len(d.get("resources") or []),
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "open_data_dataset", exc)


@mcp.tool()
def open_data_preview(url: str, rows: int = 20) -> dict[str, Any]:
    """First rows of a CSV or JSON file hosted on dados.gov.pt (reads at most 512 KB).

    Args:
        url: a resource url from open_data_dataset (must be on dados.gov.pt).
        rows: how many rows to return (1-100).
    """
    try:
        u = urlparse(url)
        if u.scheme != "https" or not any(u.hostname == h or (u.hostname or "").endswith("." + h) for h in PREVIEW_HOSTS):
            raise ValueError("only https files hosted on dados.gov.pt can be previewed; open other links in a browser")
        n = clamp(rows, 1, 100, 20)
        buf = b""
        with http().stream("GET", url, headers={"Accept": "*/*"}, follow_redirects=False) as r:
            if r.is_redirect:
                return {"error": "this file is hosted outside dados.gov.pt; open it in a browser", "location": r.headers.get("Location")}
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            for chunk in r.iter_bytes(64 * 1024):
                buf += chunk
                if len(buf) >= PREVIEW_BYTES:
                    break
        truncated = len(buf) >= PREVIEW_BYTES
        try:
            text = buf.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = buf.decode("cp1252", errors="replace")
        if "json" in ctype or url.lower().endswith(".json") or text.lstrip()[:1] in "[{":
            import json

            try:
                data = json.loads(text)
            except ValueError:
                return {"error": "JSON file is larger than the preview limit; download it instead", "url": url}
            items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [data]) if isinstance(data, dict) else [data]
            return {"format": "json", "rows": items[:n], "rows_in_preview": len(items), "truncated": truncated, "source": SOURCE}
        if truncated:
            text = text[: text.rfind("\n")]
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        all_rows = list(reader)
        if not all_rows:
            return {"error": "empty file"}
        header, body = all_rows[0], all_rows[1:]
        return {
            "format": "csv",
            "columns": header,
            "rows": [dict(zip(header, r, strict=False)) for r in body[:n]],
            "rows_in_preview": len(body),
            "truncated": truncated,
            "source": SOURCE,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(log, "open_data_preview", exc)
