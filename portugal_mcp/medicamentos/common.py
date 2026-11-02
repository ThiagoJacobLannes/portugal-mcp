"""Shared HTTP session, small TTL cache, text normalisation and help-line facts."""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from ..common import PROJECT_URL, USER_AGENT  # noqa: F401  (re-exported)

log = logging.getLogger("portugal_mcp.medicamentos")


HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}

# Minimum seconds between two requests to the same host (politeness).
MIN_INTERVAL = 1.0


class Http:
    """One httpx session for the whole server, with a per-host request spacing."""

    def __init__(self) -> None:
        self._client = httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            delta = time.monotonic() - self._last.get(host, 0.0)
            if delta < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - delta)
            self._last[host] = time.monotonic()

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        for attempt in (1, 2):
            self._wait(url)
            log.info("%s %s", method, url)
            try:
                r = self._client.request(method, url, **kw)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                log.warning("connection problem, retrying once: %s", url)
                time.sleep(2)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("unreachable")

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)


_http: Http | None = None


def http() -> Http:
    global _http
    if _http is None:
        _http = Http()
    return _http


class TTLCache:
    """Tiny in-memory cache: key -> (expires_at, value)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (now + ttl, value)
        return value


cache = TTLCache()


def norm(text: str | None) -> str:
    """Lower-case, accent-free, single-spaced."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


# ---------------------------------------------------------------------------
# Help lines. Every number below was checked on the source given next to it
# (2026-10-31). Do not add numbers that cannot be verified the same way.
# ---------------------------------------------------------------------------
HELP_LINES: dict[str, Any] = {
    "emergency": {
        "number": "112",
        "what": "Emergencies (INEM): risk to life or a serious situation.",
        "source": "https://www.gov.pt/guias/a-minha-saude",
    },
    "sns24": {
        "number": "808 24 24 24",
        "what": "SNS 24, the national health service line: a health professional assesses the "
        "situation and tells you what to do (non-emergency).",
        "source": "https://www.gov.pt/guias/a-minha-saude",
    },
    "linha_1400": {
        "number": "1400",
        "what": "Linha 1400, run by the pharmacies (Associação Nacional das Farmácias): finds a "
        "pharmacy that can supply a medicine with reduced availability, and the pharmacies on "
        "duty. Also an online search form at https://www.1400safe.pt/.",
        "hours": "24 hours a day, 7 days a week. Free from 19:00 to 09:00 and on Saturdays, "
        "Sundays and national holidays; EUR 0.10/min from 09:00 to 19:00 (any network).",
        "source": "https://www.1400safe.pt/",
        "note": "Hours and cost as published by the line's own website; not found on an "
        "SNS/Infarmed/gov.pt page.",
    },
    "infarmed_linha_do_medicamento": {
        "number": "800 222 444",
        "what": "Infarmed's 'Linha do Medicamento' (CIMI, the medicines information centre).",
        "source": "https://www.infarmed.pt/documents/15786/12277909/Sistema+de+Pre%C3%A7os+de+"
        "Refer%C3%AAncia+-+3%C2%BA+Trimestre+de+2026+%28agosto%29/57ba41ea-aaea-1767-ad71-5afab2a50d27",
        "note": "Listed as a contact on Infarmed circulars; hours not stated there.",
    },
    "infarmed_shortage_search": {
        "url": "https://www.infarmed.pt/web/infarmed/gestao-da-disponibilidade-do-medicamento",
        "what": "Infarmed's official shortage (ruturas) and discontinuation (cessações) search. "
        "Check it in a browser; this server does not query it (see README).",
    },
    "infomed": {
        "url": "https://extranet.infarmed.pt/INFOMED-fo/",
        "what": "Infomed, Infarmed's public medicine database (presentations, generics, prices, "
        "leaflets). Check it in a browser; this server does not query it (see README).",
    },
}

DISCLAIMER = (
    "Informational only, not medical advice. Never switch, stop or substitute a medicine "
    "without a pharmacist or doctor. In an emergency call 112."
)

STOCK_NOTE = (
    "Pharmacy stock is not public in Portugal: call these pharmacies to confirm they have "
    "the medicine before you go, or call Linha 1400."
)


def where_to_get_help() -> dict[str, Any]:
    return {"lines": HELP_LINES, "disclaimer": DISCLAIMER}
