// Run with `npm test` (Node's own test runner; Node strips the TypeScript types).
import assert from "node:assert/strict";
import { test } from "node:test";

import { clientFamily, outcomeOf, parseHits, safeName } from "../src/usage-core.ts";

test("counts only initialize, tools/list and tools/call, never arguments", () => {
  const body = JSON.stringify([
    { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "postal_code", arguments: { code: "1100-148" } } },
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method: "tools/list" },
    { jsonrpc: "2.0", id: 3, method: "ping" },
  ]);
  const hits = parseHits(body, "claude", "PT");
  assert.deepEqual(hits, [
    { method: "tools/call", tool: "postal_code", client: "claude", country: "PT" },
    { method: "tools/list", tool: "", client: "claude", country: "PT" },
  ]);
  assert.ok(!JSON.stringify(hits).includes("1100-148"));
});

test("initialize takes the client family from clientInfo, never the raw name", () => {
  const body = JSON.stringify({ jsonrpc: "2.0", id: 0, method: "initialize", params: { clientInfo: { name: "Cursor (user 4815)", version: "1.2" } } });
  assert.deepEqual(parseHits(body, "other", "BR"), [{ method: "initialize", tool: "", client: "cursor", country: "BR" }]);
  const unknown = JSON.stringify({ jsonrpc: "2.0", id: 0, method: "initialize", params: { clientInfo: { name: "Thiago's laptop" } } });
  assert.equal(parseHits(unknown, "python-script", "PT")[0].client, "python-script");
});

test("bad bodies and odd tool names are not stored as free text", () => {
  assert.deepEqual(parseHits("not json", "x", "PT"), []);
  assert.equal(safeName("weather_forecast"), "weather_forecast");
  assert.equal(safeName("DROP TABLE hits; --"), "other");
  assert.equal(safeName("a".repeat(80)), "other");
});

test("client families", () => {
  assert.equal(clientFamily("claude-code/2.1.0 (cli)"), "claude-code");
  assert.equal(clientFamily("Claude-User"), "claude");
  assert.equal(clientFamily("openai-mcp/1.0"), "chatgpt");
  assert.equal(clientFamily("python-httpx/0.28.1"), "python-script");
  assert.equal(clientFamily("Mozilla/5.0 something"), "other");
});

test("outcomes", () => {
  const ok = String.raw`data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\n  \"day\": \"2026-11-11\"}"}],"isError":false}}`;
  const toolError = String.raw`data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\n  \"error\": \"no municipality called 'X'\"}"}],"isError":false}}`;
  const rpcError = `{"jsonrpc":"2.0","id":2,"error":{"code":-32602,"message":"Unknown tool"}}`;
  assert.equal(outcomeOf(200, ok), "ok");
  assert.equal(outcomeOf(200, toolError), "tool_error");
  assert.equal(outcomeOf(200, rpcError), "rpc_error");
  assert.equal(outcomeOf(200, `{"result":{"isError":true}}`), "tool_error");
  assert.equal(outcomeOf(429, ""), "http_4xx");
  assert.equal(outcomeOf(503, ""), "http_5xx");
});
