# Privacy

portugal-mcp is a read-only server of public data. This page says what happens to your requests,
in both ways of using it.

## Installed locally (`uvx portugal-mcp`, pip, Docker)

Nothing is sent to the author, ever. There is no telemetry and no "phone home". The server talks
only to the public sources a tool needs (IPMA, INE, GeoAPI.pt, the SNS portal and so on), from your
own machine, as your browser would if you opened those sites. Logs stay on your machine.

## The hosted endpoint (`https://mcp.thiagolannes.com/portugal`)

The free shared copy runs on Cloudflare (a Worker in front of one container).

**What is counted.** To know whether anyone uses it, each request adds 1 to a daily counter made of:
the date, the MCP method (`initialize`, `tools/list`, `tools/call`), the tool name, the kind of
client (a coarse family such as claude, chatgpt or cursor, worked out from the User-Agent or the
client's self-reported name), the country Cloudflare already resolves for the request, the outcome
(worked, tool error, protocol error, HTTP error) and the time taken. Nothing else goes into the
counter.

**What is never stored.** IP addresses, tool arguments (they can hold a place, a postal code or
coordinates), the answers, session ids, cookies, or anything that tells one person from another.
Tool names outside the server's own identifiers are stored as "other", so no free text gets in.

**Where and for how long.** The counters live in one Cloudflare Durable Object and are kept for
400 days, then deleted. The totals are public at [mcp.thiagolannes.com/stats](https://mcp.thiagolannes.com/stats),
and the counting code is [`deploy/cloudflare/src/usage-core.ts`](deploy/cloudflare/src/usage-core.ts),
with tests.

**Your IP address.** Cloudflare necessarily sees it to deliver the request. The Worker uses it only
for the rate limit (60 requests a minute per address, 600 for the addresses Claude's connectors use; Cloudflare keeps that count briefly in memory) and does not
write it anywhere.

**Logs.** Cloudflare keeps short-lived operational logs of the Worker and the container (time, path,
status, errors), visible only to the maintainer, used only to fix problems and deleted automatically
after a few days. In the hosted container, error messages that could echo what you typed (a place
name, a postal code) are logged as their type only, never their text.

**The public sources.** The container, not your device, fetches the data, so the sources (IPMA,
INE, GeoAPI.pt...) see Cloudflare's address, not yours. They do receive the place or code a tool was
asked about, because that is the question being answered.

**Your assistant.** What you type goes first to your AI assistant (Claude, ChatGPT...), which decides
which tool to call and with what arguments. That part is covered by the assistant's own privacy
policy.

## Questions

Open an issue at [github.com/ThiagoJacobLannes/portugal-mcp/issues](https://github.com/ThiagoJacobLannes/portugal-mcp/issues).
This page changes only together with the code it describes; its history is in git.

Last updated: 2026-11-23.
