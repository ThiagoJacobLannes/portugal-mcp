import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  PORTUGAL_MCP: DurableObjectNamespace<PortugalMcp>;
  LIMITER: RateLimit;
  FOGOS_API_KEY?: string;
}

const REPO = "https://github.com/ThiagoJacobLannes/portugal-mcp";
const MAX_BODY = 64 * 1024; // MCP requests are small JSON-RPC messages

/** The container: `portugal-mcp --hosted` (see the Dockerfile at the repo root). */
export class PortugalMcp extends Container<Env> {
  defaultPort = 8000;
  // stay warm for a while after the last request so the downloaded datasets are reused
  sleepAfter = "30m";

  // field initialisers run after the base constructor, so this.env (the Worker's secrets) is set
  envVars = {
    FOGOS_API_KEY: this.env.FOGOS_API_KEY ?? "",
    PORTUGAL_MCP_LOG_LEVEL: "INFO",
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
    const { success } = await env.LIMITER.limit({ key: `${name}:${ip}` });
    if (!success) {
      return rpcError(
        429,
        `Too many requests (limit: 60 a minute). Try again in a minute, or run your own copy: ${server.local} (${REPO}).`,
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
