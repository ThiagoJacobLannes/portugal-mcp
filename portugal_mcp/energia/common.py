"""Small helpers shared by the fuel and EV modules."""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections.abc import Iterable
from typing import TypeVar

from ..common import USER_AGENT  # noqa: F401  (re-exported)

T = TypeVar("T")


def norm(s: str | None) -> str:
    """Lowercase, accent-free, single-spaced: 'Évora ' -> 'evora'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def resolve_name(query: str, options: dict[str, T], what: str) -> T:
    """Match `query` against {display name: value}, case- and accent-insensitive.

    Exact match first, then a unique prefix / substring match. Raises ValueError with
    suggestions otherwise.
    """
    q = norm(query)
    by_norm = {norm(k): v for k, v in options.items()}
    if q in by_norm:
        return by_norm[q]
    hits = [k for k in by_norm if k.startswith(q)] or [k for k in by_norm if q and q in k]
    if len(hits) == 1:
        return by_norm[hits[0]]
    names = {norm(k): k for k in options}
    if hits:
        raise ValueError(f"Ambiguous {what} '{query}': could be {', '.join(sorted(names[h] for h in hits))}.")
    close = difflib.get_close_matches(q, list(by_norm), n=5, cutoff=0.6)
    hint = f" Did you mean: {', '.join(names[c] for c in close)}?" if close else ""
    raise ValueError(f"Unknown {what} '{query}'.{hint}")


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n)))


def median(xs: Iterable[float]) -> float | None:
    v = sorted(xs)
    if not v:
        return None
    m = len(v) // 2
    return round(v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2, 3)
