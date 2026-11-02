# Changelog

## 0.1.1 — 2026-11-02

- SNS: when tempos.min-saude.pt cannot be reached (it resets connections from some networks, such as cloud hosts outside Portugal), the institution list falls back to a bundled snapshot instead of failing.
- Live-check workflow: a failing check now fails the job and opens an issue (pipefail).

## 0.1.0 — 2026-11-02

First release.

- New features: `clima` (IPMA), `geo` (GeoAPI.pt plus national holidays), `ine` (INE indicators), `bpstat` (Banco de Portugal), `parlamento` (Assembleia da República open data), `contratos` (Portal BASE via IMPIC), `dados` (dados.gov.pt).
- Merged from earlier servers: `saude` (was `sns-portugal-mcp`), `medicamentos` (was `portugal-medicines-mcp`), `energia` (was `portugal-fuel-ev-mcp`), `transportes` (was `carris-metropolitana-mcp`).
- Renamed tools from the merged servers: `find_institutions` → `sns_find_institutions`, `emergency_stats` → `sns_emergency_stats`, `surgery_wait_stats` → `sns_surgery_wait_stats`, `consultation_wait_stats` → `sns_consultation_wait_stats`, `search_datasets` → `sns_search_datasets`, `query_dataset` → `sns_query_dataset`, `availability` → `medicine_availability`, `where_to_get_help` → `medicine_help`, and `find_stops` / `next_arrivals` / `line_info` / `service_alerts` / `vehicle_positions` → `bus_*`.
- `PORTUGAL_MCP_FEATURES` selects features; `--refresh` pre-downloads the large files; `--http` serves streamable HTTP.
