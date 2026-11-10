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

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/" || url.pathname === "") {
      return Response.json(
        {
          name: "portugal-mcp",
          what: "Portugal's public data for AI assistants (MCP server). Free, read-only, open source.",
          mcp_endpoint: `${url.origin}/mcp`,
          install_locally: "uvx portugal-mcp",
          source: REPO,
        },
        { headers: CORS },
      );
    }
    if (url.pathname !== "/mcp") return new Response("Not found", { status: 404, headers: CORS });
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    const ip = req.headers.get("CF-Connecting-IP") ?? "unknown";
    const { success } = await env.LIMITER.limit({ key: ip });
    if (!success) {
      return rpcError(
        429,
        `Too many requests (limit: 60 a minute). Try again in a minute, or run your own copy: uvx portugal-mcp (${REPO}).`,
        { "Retry-After": "60" },
      );
    }
    const len = Number(req.headers.get("Content-Length") ?? "0");
    if (len > MAX_BODY) return rpcError(413, "Request too large.");

    const res = await getContainer(env.PORTUGAL_MCP).fetch(req);
    const out = new Response(res.body, res);
    for (const [k, v] of Object.entries(CORS)) out.headers.set(k, v);
    return out;
  },
} satisfies ExportedHandler<Env>;
