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
    "ocorrencias": ".ocorrencias",
    "eletricidade": ".eletricidade",
    "turismo": ".turismo",
    "lisboa": ".lisboa",
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
    from . import contratos, parlamento, transit, turismo
    from .energia import tools as energia

    # One step at a time, so at most one big file is being parsed (see common.HEAVY). Quick and
    # most-asked first: after a deploy, timetables are ready in about a minute, the rest in ~4.
    steps = [
        # the AL register is queried on Turismo de Portugal's server; warm its layer and DNS lookup
        ("turismo: local accommodation", lambda: turismo._layer_meta(turismo.AL_LAYER)),
        *[(f"transit: {op}", lambda op=op: transit._db(op)) for op in transit.OPERATORS],
        ("contratos: this year", lambda: contratos._contracts(contratos._year(None))),
        ("parlamento: initiatives", lambda: parlamento._initiatives(parlamento.CURRENT)),
        ("parlamento: deputies", lambda: parlamento._deputies(parlamento.CURRENT)),
        ("energia: EV chargers", energia.refresh),
    ]
    for label, fn in steps:
        t = time.time()
        try:
            fn()
            log.info("%s ready (%.0fs)", label, time.time() - t)
        except Exception:  # noqa: BLE001
            log.exception("%s failed", label)
    log.info("refresh done in %.0fs", time.time() - t0)
    _warm["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _warm["last_refresh_seconds"] = round(time.time() - t0)


_warm: dict[str, object] = {}


def _keep_warm(hours: float) -> None:
    """Hosted copy: download and parse the big files in a background thread, now and every few
    hours. Files due to expire before the next pass are rebuilt early, so no user waits for one."""
    import threading

    from .common import refreshing_ahead

    def loop() -> None:
        while True:
            with refreshing_ahead(hours * 3600 + 1800):
                refresh()
            time.sleep(hours * 3600)

    def ev_status_loop() -> None:
        # EV live status: calls answer from a copy up to an hour old and refresh it behind the
        # scenes; this keeps it under the hour even when nobody asked for a while
        from .energia import ev

        while True:
            time.sleep(50 * 60)
            ev.data()._refresh_status()

    _warm["every_hours"] = hours
    threading.Thread(target=loop, name="keep-warm", daemon=True).start()
    if "energia" in enabled_features():
        threading.Thread(target=ev_status_loop, name="keep-warm-ev", daemon=True).start()
    log.info("keep-warm: refreshing the large datasets every %g h", hours)


def _run_sync_tools_in_threads(max_parallel: int) -> None:
    """FastMCP calls plain functions on the event loop, so one slow download would freeze every
    other user of a shared server. Hand them to worker threads, a few at a time (memory is kept
    in check by common.HEAVY: the big files are downloaded, parsed and loaded one at a time)."""
    import functools

    import anyio

    limiter = anyio.CapacityLimiter(max_parallel)
    for tool in mcp._tool_manager.list_tools():
        if tool.is_async:
            continue
        fn = tool.fn

        @functools.wraps(fn)
        async def threaded(*a, _fn=fn, **kw):
            return await anyio.to_thread.run_sync(functools.partial(_fn, *a, **kw), limiter=limiter)

        tool.fn, tool.is_async = threaded, True


def serve_hosted() -> None:
    """Shared public endpoint (the Cloudflare container): stateless streamable HTTP on 0.0.0.0.

    Only the Worker in front can reach the container, and it does the per-user rate limiting,
    so DNS-rebinding protection (meant for servers on a user's own machine) is switched off.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    s = mcp.settings
    s.host = os.environ.get("MCP_HOST", "0.0.0.0")  # noqa: S104 - reachable only through the Worker
    s.port = int(os.environ.get("MCP_PORT", "8000"))
    s.stateless_http = True
    s.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # Memory is bounded by common.HEAVY (one big file at a time), not by this: a call waiting for a
    # big file must not hold back the quick ones, so allow several at once.
    _run_sync_tools_in_threads(int(os.environ.get("PORTUGAL_MCP_MAX_PARALLEL", "8")))
    warm = float(os.environ.get("PORTUGAL_MCP_KEEP_WARM_HOURS", "0"))
    if warm > 0:
        _keep_warm(warm)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "keep_warm": _warm or None})

    mcp.run(transport="streamable-http")


def main() -> None:
    ap = argparse.ArgumentParser(prog="portugal-mcp", description=__doc__)
    ap.add_argument("--version", action="version", version=f"portugal-mcp {__version__}")
    ap.add_argument("--refresh", action="store_true", help="download the large datasets into the cache, then exit")
    ap.add_argument("--list-tools", action="store_true", help="print the enabled tools and exit")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP on 127.0.0.1:8000 instead of stdio")
    ap.add_argument("--hosted", action="store_true", help="shared public endpoint: stateless HTTP on 0.0.0.0:8000 (used by the container)")
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
    if args.hosted:
        serve_hosted()
    elif args.http:
        mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
