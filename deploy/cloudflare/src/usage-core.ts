// Anonymous usage counts for the hosted endpoint.
//
// What is counted, per day: the MCP method (initialize, tools/list, tools/call), the tool name, the
// kind of client (from the User-Agent or the client's self-reported name, e.g. "claude", "chatgpt",
// "cursor"), the country Cloudflare already resolves, the outcome (ok, tool_error, rpc_error, http_4xx,
// http_5xx) and the total time. What is never stored: IP addresses, tool arguments (they can hold a
// postal code or coordinates), session ids, cookies, or anything that tells one person from another.
// The aggregated numbers are public at GET /stats.
//
// This file holds the pure functions (tested with `npm test`); usage.ts holds the Durable Object.

export interface Hit {
  method: string;
  tool: string;
  client: string;
  country: string;
}

export interface Event extends Hit {
  outcome: string;
  ms: number;
}

const COUNTED = new Set(["initialize", "tools/list", "tools/call"]);
export const KEEP_DAYS = 400;

/** The JSON-RPC requests worth counting in a POST body (a single message or a batch). */
export function parseHits(body: string, clientHeader: string, country: string): Hit[] {
  let msg: unknown;
  try {
    msg = JSON.parse(body);
  } catch {
    return [];
  }
  const list = Array.isArray(msg) ? msg : [msg];
  const hits: Hit[] = [];
  for (const m of list) {
    if (!m || typeof m !== "object") continue;
    const { method, params } = m as { method?: unknown; params?: Record<string, unknown> };
    if (typeof method !== "string" || !COUNTED.has(method)) continue;
    let tool = "";
    let client = clientHeader;
    if (method === "tools/call" && params && typeof params.name === "string") tool = safeName(params.name);
    if (method === "initialize" && params && typeof params.clientInfo === "object" && params.clientInfo) {
      const self = (params.clientInfo as { name?: unknown }).name;
      if (typeof self === "string" && self) client = clientFamily(self, clientHeader);
    }
    hits.push({ method, tool, client, country });
  }
  return hits;
}

/** Tool names are our own identifiers; anything else is folded into "other" (never free text). */
export function safeName(name: string): string {
  return /^[a-z][a-z0-9_]{0,48}$/.test(name) ? name : "other";
}

/** A coarse client family: never a version, an account or a free-form string. */
export function clientFamily(userAgent: string, fallback = "other"): string {
  const ua = userAgent.toLowerCase();
  const families: [RegExp, string][] = [
    [/claude[- ]?code/, "claude-code"],
    [/claude|anthropic/, "claude"],
    [/chatgpt|openai|oai-/, "chatgpt"],
    [/cursor/, "cursor"],
    [/windsurf|codeium/, "windsurf"],
    [/vscode|visual studio code|copilot/, "vscode"],
    [/gemini|google/, "gemini"],
    [/mcp-inspector|inspector/, "mcp-inspector"],
    [/python|httpx|aiohttp|requests/, "python-script"],
    [/node|undici|axios|fetch/, "node-script"],
    [/curl|wget/, "curl"],
  ];
  for (const [re, fam] of families) if (re.test(ua)) return fam;
  return fallback;
}

/** ok / tool_error / rpc_error from a response body; HTTP failures are classed before this. */
export function outcomeOf(status: number, body: string): string {
  if (status >= 500) return "http_5xx";
  if (status >= 400) return "http_4xx";
  // A JSON-RPC error object at message level (unknown tool, bad params...).
  if (/"error"\s*:\s*\{\s*"code"/.test(body)) return "rpc_error";
  // Our tools report failures as a JSON result whose first key is "error" (common.fail).
  if (/"text"\s*:\s*"\{\\n\s*\\"error\\"\s*:/.test(body) || /"isError"\s*:\s*true/.test(body)) return "tool_error";
  return "ok";
}

export function today(now = new Date()): string {
  return now.toISOString().slice(0, 10);
}

