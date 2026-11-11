import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  PORTUGAL_MCP: DurableObjectNamespace<PortugalMcp>;
  LIMITER: RateLimit;
  LIMITER_CONNECTORS: RateLimit;
  FOGOS_API_KEY?: string;
}

const REPO = "https://github.com/ThiagoJacobLannes/portugal-mcp";
const MAX_BODY = 64 * 1024; // MCP requests are small JSON-RPC messages

// Claude's custom connectors (claude.ai, desktop, mobile) call MCP servers from Anthropic's servers,
// so every claude.ai user arrives from these few addresses. They get their own, larger budget:
// at 60 a minute per IP, a handful of people trying it at once would lock each other out.
// Source: https://platform.claude.com/docs/en/api/ip-addresses (outbound, "MCP tool calls").
const CONNECTOR_RANGES: [number, number][] = [cidr("160.79.104.0/21"), cidr("153.61.208.0/20")];
const LIMIT = 60; // a minute, per client IP (wrangler.jsonc: LIMITER)
const CONNECTOR_LIMIT = 600; // a minute, per Anthropic egress IP (wrangler.jsonc: LIMITER_CONNECTORS)

function ipv4(ip: string): number | null {
  const p = ip.split(".");
  if (p.length !== 4) return null;
  let n = 0;
  for (const part of p) {
    if (!/^\d{1,3}$/.test(part) || Number(part) > 255) return null;
    n = n * 256 + Number(part);
  }
  return n;
}

function cidr(range: string): [number, number] {
  const [base, bits] = range.split("/");
  const size = 2 ** (32 - Number(bits));
  const start = Math.floor((ipv4(base) ?? 0) / size) * size;
  return [start, start + size];
}

function isClaudeConnector(ip: string): boolean {
  const n = ipv4(ip);
  return n !== null && CONNECTOR_RANGES.some(([lo, hi]) => n >= lo && n < hi);
}

/** The container: `portugal-mcp --hosted` (see the Dockerfile at the repo root). */
export class PortugalMcp extends Container<Env> {
  defaultPort = 8000;
  // stay warm for a while after the last request so the downloaded datasets are reused
  sleepAfter = "30m";

  // field initialisers run after the base constructor, so this.env (the Worker's secrets) is set
  envVars = {
    FOGOS_API_KEY: this.env.FOGOS_API_KEY ?? "",
    PORTUGAL_MCP_LOG_LEVEL: "INFO",
    // the container re-downloads the big files in the background, so users never wait for them
    PORTUGAL_MCP_KEEP_WARM_HOURS: "6",
  };
}

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Accept, Mcp-Session-Id, Mcp-Protocol-Version, Last-Event-ID",
  "Access-Control-Expose-Headers": "Mcp-Session-Id",
};

function rpcError(status: number, message: string, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message }, id: null }), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extra },
  });
}

/**
 * One domain, one path per server: https://mcp.thiagolannes.com/<server>.
 * A future server gets its own container class + binding and one line here.
 */
const SERVERS: Record<string, { binding: (env: Env) => DurableObjectNamespace<PortugalMcp>; what: string; local: string }> = {
  portugal: {
    binding: (env) => env.PORTUGAL_MCP,
    what: "Portugal's public data: weather, statistics, Parliament, contracts, health, medicines, fuel, transport, live incidents.",
    local: "uvx portugal-mcp",
  },
};

export default {
  // Cron (wrangler.jsonc): a ping every 10 minutes keeps the container awake (it sleeps after 30
  // idle minutes, and sleeping wipes its disk cache) and starts it again after a deploy or a host move.
  async scheduled(ctrl: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    for (const [name, server] of Object.entries(SERVERS)) {
      ctx.waitUntil(
        getContainer(server.binding(env))
          .fetch(new Request("http://container/health"))
          .then(async (r) => console.log(`keep-warm ${name} ${ctrl.cron}: ${r.status} ${await r.text()}`))
          .catch((e) => console.error(`keep-warm ${name}: ${e}`)),
      );
    }
  },

  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const name = url.pathname.replace(/^\/+|\/+$/g, "");

    if (name === "") {
      return Response.json(
        {
          what: "Free, read-only MCP servers for AI assistants, by Thiago Lannes. Open source.",
          servers: Object.fromEntries(
            Object.entries(SERVERS).map(([k, v]) => [k, { endpoint: `${url.origin}/${k}`, what: v.what, install_locally: v.local }]),
          ),
          source: REPO,
        },
        { headers: CORS },
      );
    }
    const server = SERVERS[name];
    if (!server) return new Response("Not found", { status: 404, headers: CORS });
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    const ip = req.headers.get("CF-Connecting-IP") ?? "unknown";
    const connector = isClaudeConnector(ip);
    const limiter = connector ? env.LIMITER_CONNECTORS : env.LIMITER;
    const { success } = await limiter.limit({ key: `${name}:${ip}` });
    if (!success) {
      return rpcError(
        429,
        `The free shared endpoint is busy right now (over ${connector ? CONNECTOR_LIMIT : LIMIT} requests a minute ` +
          `from this address). Please try again in a minute. For heavy use, run your own copy in seconds: ` +
          `${server.local} (${REPO}).`,
        { "Retry-After": "60" },
      );
    }
    const len = Number(req.headers.get("Content-Length") ?? "0");
    if (len > MAX_BODY) return rpcError(413, "Request too large.");

    // the container serves MCP at /mcp; the public path is the server's name
    const inner = new URL(req.url);
    inner.pathname = "/mcp";
    const res = await getContainer(server.binding(env)).fetch(new Request(inner, req));
    const out = new Response(res.body, res);
    for (const [k, v] of Object.entries(CORS)) out.headers.set(k, v);
    return out;
  },
} satisfies ExportedHandler<Env>;
