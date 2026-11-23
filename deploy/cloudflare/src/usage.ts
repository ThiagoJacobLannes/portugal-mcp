// The Durable Object behind GET /stats: anonymous daily counters (see usage-core.ts for what is
// counted and what never is).

import { DurableObject } from "cloudflare:workers";
import { type Event, KEEP_DAYS, today } from "./usage-core";

export * from "./usage-core";

/** One Durable Object holds the daily counters (SQLite). */
export class UsageStats extends DurableObject {
  private ready = false;

  private setup(): void {
    if (this.ready) return;
    this.ctx.storage.sql.exec(
      `CREATE TABLE IF NOT EXISTS hits (
         day TEXT NOT NULL, method TEXT NOT NULL, tool TEXT NOT NULL, client TEXT NOT NULL,
         country TEXT NOT NULL, outcome TEXT NOT NULL, n INTEGER NOT NULL, ms INTEGER NOT NULL,
         PRIMARY KEY (day, method, tool, client, country, outcome))`,
    );
    this.ready = true;
  }

  async record(events: Event[]): Promise<void> {
    this.setup();
    const day = today();
    for (const e of events) {
      this.ctx.storage.sql.exec(
        `INSERT INTO hits (day, method, tool, client, country, outcome, n, ms) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
         ON CONFLICT (day, method, tool, client, country, outcome) DO UPDATE SET n = n + 1, ms = ms + excluded.ms`,
        day, e.method, e.tool, e.client, e.country, e.outcome, Math.max(0, Math.round(e.ms)),
      );
    }
    // Housekeeping now and then: nothing older than KEEP_DAYS.
    if (Math.random() < 0.01) {
      const cutoff = today(new Date(Date.now() - KEEP_DAYS * 86400_000));
      this.ctx.storage.sql.exec(`DELETE FROM hits WHERE day < ?`, cutoff);
    }
  }

  async summary(days: number): Promise<Record<string, unknown>> {
    this.setup();
    const since = today(new Date(Date.now() - (days - 1) * 86400_000));
    const q = (sql: string) => this.ctx.storage.sql.exec(sql, since).toArray();
    const calls = `method = 'tools/call'`;
    return {
      since,
      days,
      totals: q(`SELECT method, SUM(n) AS n FROM hits WHERE day >= ? GROUP BY method ORDER BY n DESC`),
      calls_per_day: q(`SELECT day, SUM(n) AS calls FROM hits WHERE day >= ? AND ${calls} GROUP BY day ORDER BY day`),
      tools: q(
        `SELECT tool, SUM(n) AS calls, SUM(CASE WHEN outcome = 'ok' THEN n ELSE 0 END) AS ok,
                ROUND(1.0 * SUM(ms) / SUM(n)) AS avg_ms
         FROM hits WHERE day >= ? AND ${calls} GROUP BY tool ORDER BY calls DESC`,
      ),
      outcomes: q(`SELECT outcome, SUM(n) AS n FROM hits WHERE day >= ? AND ${calls} GROUP BY outcome ORDER BY n DESC`),
      clients: q(`SELECT client, SUM(n) AS requests FROM hits WHERE day >= ? GROUP BY client ORDER BY requests DESC`),
      new_sessions_by_client: q(
        `SELECT client, SUM(n) AS sessions FROM hits WHERE day >= ? AND method = 'initialize' GROUP BY client ORDER BY sessions DESC`,
      ),
      countries: q(`SELECT country, SUM(n) AS requests FROM hits WHERE day >= ? GROUP BY country ORDER BY requests DESC LIMIT 30`),
    };
  }
}
