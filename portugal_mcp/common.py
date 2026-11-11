"""Shared helpers: HTTP client, on-disk cache, text normalisation."""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import re
import socket
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

import httpx
from platformdirs import user_cache_dir

from . import __version__

PROJECT_URL = "https://github.com/ThiagoJacobLannes/portugal-mcp"
USER_AGENT = f"portugal-mcp/{__version__} (+{PROJECT_URL}; open-source, read-only)"

log = logging.getLogger("portugal_mcp")


def cache_dir(feature: str) -> Path:
    base = os.environ.get("PORTUGAL_MCP_CACHE_DIR") or user_cache_dir("portugal-mcp", appauthor=False)
    d = Path(base) / feature
    d.mkdir(parents=True, exist_ok=True)
    return d


# The big files (contracts, Parliament, EV chargers, timetables) peak at ~500 MB each while they are
# downloaded and parsed. Only one such build runs at a time in the whole process, whether a user or
# the hosted copy's background refresh started it, so a 1 GiB container can't run out of memory.
HEAVY = threading.Lock()

_ahead = threading.local()


@contextmanager
def refreshing_ahead(seconds: float) -> Iterator[None]:
    """Inside this block (the background refresh thread only), caches count as stale `seconds`
    before they expire, so they are rebuilt in the background before any user finds them stale."""
    _ahead.seconds = seconds
    try:
        yield
    finally:
        _ahead.seconds = 0.0


def fresh_for(ttl: float) -> float:
    """The TTL a cache should apply right now: shorter in the background refresh thread."""
    return ttl - getattr(_ahead, "seconds", 0.0)


_http: httpx.Client | None = None


def http() -> httpx.Client:
    """One pooled client for every feature; redirects followed, polite user agent."""
    global _http
    if _http is None:
        _http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    return _http


def get_json(url: str, params: dict[str, Any] | None = None, *, retries: int = 2, timeout: float | None = None) -> Any:
    """GET a JSON document; retries transient failures (5xx, timeouts) with a short backoff."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = http().get(url, params=params, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
            if r.status_code == 429 and attempt < retries:  # rate limited: honour Retry-After, briefly
                wait = min(30.0, float(r.headers.get("Retry-After", "5") or 5))
                log.warning("%s rate-limited us; waiting %.0fs", r.url.host, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code} from {r.url.host}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", 500)
            if status < 500 or attempt == retries:
                break
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"source unavailable: {last}") from last


class FileCache:
    """A downloaded-and-parsed document kept as gzip JSON on disk.

    Trusted for `ttl` seconds; after that a HEAD request compares Last-Modified so an
    unchanged upstream file is not downloaded again.
    """

    def __init__(self, feature: str, name: str, url: str | Callable[[], str], ttl: float = 86400, heavy: bool = False):
        self.path = cache_dir(feature) / f"{name}.json.gz"
        self.url = url
        self.ttl = ttl
        self.heavy = heavy
        self._mem: Any = None

    def _url(self) -> str:
        return self.url() if callable(self.url) else self.url

    def _read(self) -> dict[str, Any] | None:
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write(self, doc: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.path)

    def _remote_modified(self, url: str) -> str | None:
        try:
            r = http().head(url, timeout=15)
            h = r.headers
            if r.status_code >= 400:
                return None
            return h.get("Last-Modified") or h.get("ETag") or (f"len:{h['Content-Length']}" if h.get("Content-Length") else None)
        except httpx.HTTPError:
            return None

    def get(self, build: Callable[[str], Any], force: bool = False) -> dict[str, Any]:
        """Return {"data": ..., "fetched_at": iso, "source_modified": str|None}.

        `build(url)` downloads and parses the source into JSON-able data.
        """
        ttl = fresh_for(self.ttl)
        if self._mem is not None and not force and time.time() - self._mem["_t"] < ttl:
            return self._mem
        if self.heavy:
            with HEAVY:  # one big build at a time; whoever waited finds the file another thread built
                return self._get(build, force, ttl)
        return self._get(build, force, ttl)

    def _get(self, build: Callable[[str], Any], force: bool, ttl: float) -> dict[str, Any]:
        doc = None if force else self._read()
        url = self._url()
        if doc and doc.get("url") == url:
            fresh = time.time() - doc["_t"] < ttl
            if not fresh:
                mod = self._remote_modified(url)
                if mod and mod == doc.get("source_modified"):
                    doc["_t"] = time.time()
                    self._write(doc)
                    fresh = True
            if fresh:
                self._mem = doc
                return doc
        try:
            mod = self._remote_modified(url)
            data = build(url)
        except Exception:
            if doc:  # serve stale rather than fail
                log.warning("refresh of %s failed; serving cached copy", self.path.name, exc_info=True)
                doc["stale"] = True
                self._mem = doc
                return doc
            raise
        doc = {
            "data": data,
            "url": url,
            "_t": time.time(),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_modified": mod,
        }
        self._write(doc)
        self._mem = doc
        return doc


def http_date(value: str | None) -> str | None:
    try:
        return parsedate_to_datetime(value).date().isoformat() if value else None
    except (TypeError, ValueError):
        return None


def norm(text: Any) -> str:
    """Lowercase, accents stripped, whitespace collapsed — for matching names."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(s.split())


STOPWORDS = {"de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "em", "no", "na", "nos", "nas", "por", "para", "com", "the", "of", "to", "in", "and"}


def keywords(text: Any) -> list[str]:
    """Normalised search words without Portuguese/English stopwords."""
    return [w for w in norm(text).split() if w not in STOPWORDS]


def clamp(n: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(n)))
    except (TypeError, ValueError):
        return default


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def pt_number(value: Any) -> float | None:
    """'1 234,5' / '1.234,5' / '12.5' / 12.5 -> float; blanks and markers -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(" ", "").replace(" ", "")
    if not s or s in {"-", "x", "..", "...", "ND", "n.d.", "§"}:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fail(log_: logging.Logger, tool: str, exc: Exception) -> dict[str, Any]:
    """Uniform error payload: bad input is a warning, anything else gets a traceback in the log."""
    if isinstance(exc, ValueError):
        log_.warning("%s: %s", tool, exc)
    else:
        log_.exception("%s failed", tool)
    return {"error": str(exc)}


def iter_json_array(fp: Any, chunk_size: int = 1 << 20) -> Any:
    """Yield the items of a top-level JSON array from a text stream without loading it whole."""
    dec = json.JSONDecoder()
    buf = ""
    started = False
    while True:
        chunk = fp.read(chunk_size)
        buf += chunk
        pos = 0
        if not started:
            pos = buf.find("[")
            if pos < 0:
                if not chunk:
                    return
                continue
            started, pos = True, pos + 1
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf) and buf[pos] == "]":
                return
            try:
                item, end = dec.raw_decode(buf, pos)
            except ValueError:
                if not chunk:
                    raise
                break
            yield item
            pos = end
        buf = buf[pos:]


def download(url: str, dest: Path, *, timeout: float = 600) -> Path:
    """Stream a large file to disk (atomic rename)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    with http().stream("GET", url, headers={"Accept": "*/*"}, timeout=httpx.Timeout(timeout, connect=15)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for block in r.iter_bytes(1 << 20):
                f.write(block)
    os.replace(tmp, dest)
    return dest


def like_pattern(text: str) -> str:
    """An ArcGIS/SQL LIKE pattern for UPPER(field) that ignores accents and case: vowels and 'c'
    become single-character wildcards ("São Vicente" also matches "SAO VICENTE")."""
    t = re.sub(r"[^a-z0-9 ]", " ", norm(text)).upper()
    return "%" + "%".join(re.sub(r"[AEIOUC]", "_", w) for w in t.split()) + "%"


# Some public-data hosts have inconsistent DNS (their name servers sometimes answer with no address,
# so a resolver caches "no such host" for minutes). For these hosts only, when the system resolver
# fails, ask a public DNS-over-HTTPS resolver instead. This works around broken DNS, not access rules.
DNS_FALLBACK_HOSTS = {"geo.turismodeportugal.pt"}
_orig_getaddrinfo = socket.getaddrinfo
_doh_cache: dict[str, tuple[float, list[str]]] = {}


def _doh_lookup(host: str) -> list[str]:
    hit = _doh_cache.get(host)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    url = "https://dns.google/resolve?" + urlencode({"name": host, "type": "A"})
    with urlopen(url, timeout=10) as r:  # noqa: S310 — fixed https URL
        data = json.load(r)
    ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    _doh_cache[host] = (time.time(), ips)
    return ips


def _getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
    try:
        return _orig_getaddrinfo(host, *args, **kwargs)
    except socket.gaierror:
        if host not in DNS_FALLBACK_HOSTS:
            raise
        ips = _doh_lookup(host)
        if not ips:
            raise
        log.warning("system DNS failed for %s; using public DNS-over-HTTPS (%s)", host, ips[0])
        return _orig_getaddrinfo(ips[0], *args, **kwargs)


socket.getaddrinfo = _getaddrinfo
