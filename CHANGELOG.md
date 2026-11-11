# Changelog

## 0.6.0 — 2026-11-11

- New `turismo` feature (Turismo de Portugal open data, CC BY): `local_accommodation` searches the national Alojamento Local register by number, name, address, postal code, municipality or distance; `tourism_places` lists Blue Flag beaches, surf spots, golf courses, spas, historic/schist/wine/mountain villages, walking routes, the Ways of Saint James, marinas, UNESCO sites and hotels. Owner and operator names are left out on purpose.
- New `lisboa` feature (Câmara Municipal de Lisboa open data): `lisbon_events` (the city's cultural agenda, CC BY), `lisbon_street_works` (licensed occupations of public space, CC0, and road/pavement works in progress or planned) and `lisbon_recycling` (nearest recycling points by type).
- FlixBus coaches in `transit` (`operator="flixbus"` or `"intercity"`, ODbL, attributed in every result), within Portugal and across Europe; "Lisboa" also finds stops named "Lisbon".
- `transit_trips_between` tries the top three matching stations at each end, so "Porto" finds Campanhã when the airport stop has no direct service.
- `open_data_search(organization=...)` finds town halls by town name ("Lisboa", "Cascais", "Oeiras") and no longer returns an unrelated body when the portal's search is loose (e.g. "INE").
- New `electricity_price`: OMIE's day-ahead wholesale price for Portugal per 15 minutes, with computed average and cheapest periods. OMIE's terms allow free use if the content is unaltered and the source cited; its download files lag the market by about a day, which the tool says.
- Not included, with reasons in the README: REN Data Hub (terms forbid reuse without consent), Porto road closures (stale).

## 0.5.0 — 2026-11-11

- Timetables beyond Lisbon: Metro do Porto, TUB (Braga), SMTUC and SIT Metropolitano (Coimbra), Busway (Aveiro region), MobiCascais, TCB (Barreiro), Vamus and Próximo (Algarve), Horários do Funchal and the SIGA network (Madeira), all official GTFS. The transit tools take a city or region as `operator` ("porto", "coimbra", "madeira"...); with none they search CP and Lisbon as before.
- New `transit_vehicles`: live positions of STCP buses (Porto urban platform), SMTUC buses (AGIT) and Carris buses and trams (GTFS-Realtime, read by a small built-in decoder: no new dependency).
- When an operator's published timetable has run out, results carry `timetable_valid_until` and a warning instead of silently returning nothing.
- New `eletricidade` feature: `power_cuts`, the planned interruptions E-REDES announces, by postal code, municipality or parish (CC BY 4.0).
- Feed freshness checks fall back to a ranged GET when a host refuses HEAD; Madeira stop codes are stripped from stop names so both directions group together (timetable caches rebuild once).
- Correction: the README said Metro do Porto and STCP publish no feed. Metro do Porto does; STCP's last timetable ended in September 2026.

## 0.4.0 — 2026-11-10

- Hosted endpoint: `portugal-mcp --hosted` serves stateless streamable HTTP on `0.0.0.0:8000`, runs the blocking tools in worker threads (at most `PORTUGAL_MCP_MAX_PARALLEL`, default 2) so one slow download doesn't stall other users, and answers `GET /health`. It powers the free public endpoint at `https://mcp.thiagolannes.com/portugal`: a Cloudflare Worker (60 requests a minute per IP) in front of one container (`Dockerfile`, `deploy/cloudflare/`).

## 0.3.0 — 2026-11-10

- New `ocorrencias` feature: `active_incidents`, the live civil-protection occurrences (ANEPC, via Fogos.pt) of every kind near a postal code, municipality, place name or coordinates, closest first with distance and direction, or the national picture. Needs a personal Fogos.pt API key in `FOGOS_API_KEY`; police activity is not public and not included.

## 0.2.1 — 2026-11-09

- Public contracts: IMPIC's November 2026 files give buyers, suppliers and CPV as objects instead of `"NIF - Name"` strings, which made `public_contracts` and `public_contracts_summary` fail (issue #8). Both formats are now read; natural-person NIFs are still hidden.

## 0.2.0 — 2026-11-02

- New `transit` feature: scheduled departures, direct trips between stations and stop search, from the official GTFS timetables of CP (all trains), Metro de Lisboa, Carris (Lisbon buses, trams, lifts) and Fertagus. Each feed is stored in a local SQLite file and refreshed when the operator publishes a new one.
- New `incendios` feature: `wildfires`, the satellite-mapped burnt areas in Portugal from EFFIS (Copernicus), by period, year, region or parish.
- `download()` now asks for any content type (some operators answer 406 to `Accept: application/json`).

## 0.1.1 — 2026-11-02

- SNS: when tempos.min-saude.pt cannot be reached (it resets connections from some networks, such as cloud hosts outside Portugal), the institution list falls back to a bundled snapshot instead of failing.
- Live-check workflow: a failing check now fails the job and opens an issue (pipefail).

## 0.1.0 — 2026-11-02

First release.

- New features: `clima` (IPMA), `geo` (GeoAPI.pt plus national holidays), `ine` (INE indicators), `bpstat` (Banco de Portugal), `parlamento` (Assembleia da República open data), `contratos` (Portal BASE via IMPIC), `dados` (dados.gov.pt).
- Merged from earlier servers: `saude` (was `sns-portugal-mcp`), `medicamentos` (was `portugal-medicines-mcp`), `energia` (was `portugal-fuel-ev-mcp`), `transportes` (was `carris-metropolitana-mcp`).
- Renamed tools from the merged servers: `find_institutions` → `sns_find_institutions`, `emergency_stats` → `sns_emergency_stats`, `surgery_wait_stats` → `sns_surgery_wait_stats`, `consultation_wait_stats` → `sns_consultation_wait_stats`, `search_datasets` → `sns_search_datasets`, `query_dataset` → `sns_query_dataset`, `availability` → `medicine_availability`, `where_to_get_help` → `medicine_help`, and `find_stops` / `next_arrivals` / `line_info` / `service_alerts` / `vehicle_positions` → `bus_*`.
- `PORTUGAL_MCP_FEATURES` selects features; `--refresh` pre-downloads the large files; `--http` serves streamable HTTP.
