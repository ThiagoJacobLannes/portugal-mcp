"""portugal-mcp entry point: load the enabled features and serve over stdio (or HTTP)."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import time

from . import __version__
from .app import instructions, mcp

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("PORTUGAL_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s portugal-mcp %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("portugal_mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)

# feature name -> module (relative to this package)
FEATURES = {
    "clima": ".clima",
    "geo": ".geo",
    "ine": ".ine",
    "bpstat": ".bpstat",
    "parlamento": ".parlamento",
    "contratos": ".contratos",
    "dados": ".dados",
    "saude": ".saude.tools",
    "medicamentos": ".medicamentos.tools",
    "energia": ".energia.tools",
    "transportes": ".transportes.tools",
    "transit": ".transit",
    "incendios": ".incendios",
}


def enabled_features() -> list[str]:
    """PORTUGAL_MCP_FEATURES="clima,geo" keeps only those; "-contratos" drops one; unset = all."""
    raw = [x.strip().lower() for x in os.environ.get("PORTUGAL_MCP_FEATURES", "").split(",") if x.strip()]
    keep = [x for x in raw if not x.startswith("-")]
    drop = {x[1:] for x in raw if x.startswith("-")}
    unknown = (set(keep) | drop) - set(FEATURES)
    if unknown:
        log.warning("unknown features ignored: %s (known: %s)", ", ".join(sorted(unknown)), ", ".join(FEATURES))
    names = [f for f in FEATURES if (not keep or f in keep) and f not in drop]
    return names or list(FEATURES)


def load(names: list[str]) -> None:
    for name in names:
        importlib.import_module(FEATURES[name], __package__)
    server = getattr(mcp, "_mcp_server", None)
    if server is not None:
        if hasattr(server, "instructions"):
            server.instructions = instructions()
        if hasattr(server, "version"):
            server.version = __version__


def refresh() -> None:
    """Download the big open-data files now so the first questions are fast."""
    t0 = time.time()
    from . import contratos, parlamento, transit
    from .energia import tools as energia

    steps = [
        ("parlamento: initiatives", lambda: parlamento._initiatives(parlamento.CURRENT)),
        ("parlamento: deputies", lambda: parlamento._deputies(parlamento.CURRENT)),
        ("contratos: this year", lambda: contratos._contracts(contratos._year(None))),
        ("energia: EV chargers", energia.refresh),
        *[(f"transit: {op}", lambda op=op: transit._db(op)) for op in transit.OPERATORS],
    ]
    for label, fn in steps:
        t = time.time()
        try:
            fn()
            log.info("%s ready (%.0fs)", label, time.time() - t)
        except Exception:  # noqa: BLE001
            log.exception("%s failed", label)
    log.info("refresh done in %.0fs", time.time() - t0)


def main() -> None:
    ap = argparse.ArgumentParser(prog="portugal-mcp", description=__doc__)
    ap.add_argument("--version", action="version", version=f"portugal-mcp {__version__}")
    ap.add_argument("--refresh", action="store_true", help="download the large datasets into the cache, then exit")
    ap.add_argument("--list-tools", action="store_true", help="print the enabled tools and exit")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP on 127.0.0.1:8000 instead of stdio")
    args = ap.parse_args()
    names = enabled_features()
    load(names)
    if args.refresh:
        refresh()
        return
    if args.list_tools:
        import anyio

        tools = anyio.run(mcp.list_tools)
        for t in tools:
            print(f"{t.name}\t{(t.description or '').strip().splitlines()[0]}")
        print(f"{len(tools)} tools from {len(names)} features: {', '.join(names)}", file=sys.stderr)
        return
    log.info("portugal-mcp %s: %s", __version__, ", ".join(names))
    if args.http:
        mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
