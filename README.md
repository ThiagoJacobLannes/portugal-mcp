# portugal-mcp

<!-- mcp-name: io.github.ThiagoJacobLannes/portugal-mcp -->

[![CI](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/ci.yml)
[![Live sources](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/live.yml/badge.svg)](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/live.yml)
[![PyPI](https://img.shields.io/pypi/v/portugal-mcp)](https://pypi.org/project/portugal-mcp/)

**Portugal's public data for AI assistants, in one MCP server.** Ask Claude (or any MCP client) about the weather and warnings, a postal code, unemployment or inflation, the Euribor, what Parliament voted, who won a public contract, how busy the emergency rooms were, whether a medicine is in shortage, the cheapest fuel nearby, when the next train to Porto leaves or how much has burned this fire season.

**Read-only, no API keys, no accounts.** Every answer comes from an official or public open-data source and says which one and for which date. Not affiliated with any of the sources.

*Português:* servidor MCP com os dados públicos de Portugal (IPMA, INE, Banco de Portugal, Parlamento, Portal BASE, dados.gov.pt, SNS, Infarmed, DGEG, MOBI.E, Carris Metropolitana, horários da CP, de Lisboa, Porto, Coimbra, Braga, Aveiro, Cascais, Barreiro, Algarve e Madeira e da FlixBus, autocarros ao vivo no Porto, Coimbra e Lisboa, cortes de luz programados da E-REDES, registos do Turismo de Portugal (alojamento local, praias com bandeira azul, hotéis), agenda cultural, obras e reciclagem de Lisboa, incêndios do EFFIS, ocorrências da Proteção Civil). Só leitura, sem chaves. As ferramentas aceitam perguntas em português.

## What it covers

| Feature | Tools | Source |
|---|---|---|
| `clima` | `weather_forecast`, `weather_warnings`, `fire_risk`, `sea_forecast`, `earthquakes` | [IPMA](https://api.ipma.pt) |
| `geo` | `postal_code`, `where_is`, `municipality`, `public_holidays` | [GeoAPI.pt](https://geoapi.pt) (CAOP, CTT, INE census); holidays computed from the Labour Code |
| `ine` | `ine_search`, `ine_indicator` | [INE](https://www.ine.pt) indicator API |
| `bpstat` | `bpstat_search`, `bpstat_series` | [Banco de Portugal BPstat](https://bpstat.bportugal.pt) |
| `parlamento` | `parliament_search`, `parliament_initiative`, `parliament_votes`, `parliament_deputies` | [Assembleia da República open data](https://www.parlamento.pt/Cidadania/Paginas/DadosAbertos.aspx) |
| `contratos` | `public_contracts`, `public_contracts_summary` | [IMPIC / Portal BASE](https://dados.gov.pt/pt/datasets/contratos-publicos-portal-base-impic-contratos-de-2012-a-2026/) |
| `dados` | `open_data_search`, `open_data_dataset`, `open_data_preview` | [dados.gov.pt](https://dados.gov.pt) |
| `saude` | `sns_find_institutions`, `sns_emergency_stats`, `sns_surgery_wait_stats`, `sns_consultation_wait_stats`, `sns_search_datasets`, `sns_query_dataset` | [SNS Transparency portal](https://transparencia.sns.gov.pt) |
| `medicamentos` | `medicine_availability`, `pharmacies`, `find_medicine_near`, `medicine_help` | Infarmed public pages, pharmacies on duty |
| `energia` | `fuel_prices`, `fuel_station_detail`, `fuel_types`, `ev_chargers`, `ev_charger_detail` | [DGEG](https://precoscombustiveis.dgeg.gov.pt), [MOBI.E](https://www.mobie.pt) |
| `transportes` | `bus_find_stops`, `bus_next_arrivals`, `bus_line_info`, `bus_service_alerts`, `bus_vehicle_positions` | [Carris Metropolitana API](https://github.com/carrismetropolitana/api) |
| `transit` | `transit_operators`, `transit_find_stops`, `transit_departures`, `transit_trips_between`, `transit_vehicles` (live) | Official GTFS timetables of [CP](https://www.cp.pt), [Metro de Lisboa](https://www.metrolisboa.pt), [Carris](https://www.carris.pt), [Fertagus](https://www.fertagus.pt), [Metro do Porto](https://www.metrodoporto.pt), [TUB](https://www.tub.pt) (Braga), [SMTUC](https://www.smtuc.pt) and SIT (Coimbra), [Busway](https://www.busway.pt) (Aveiro region), [MobiCascais](https://www.mobicascais.pt), [TCB](https://www.tcbarreiro.pt) (Barreiro), [Vamus](https://vamus.pt) and Próximo (Algarve), [Horários do Funchal](https://www.horariosdofunchal.pt) and the SIGA network (Madeira), and [FlixBus](https://www.flixbus.pt) coaches (ODbL). Live positions: STCP ([Porto urban platform](https://dadosabertos.cm-porto.pt)), SMTUC ([AGIT](https://api.planner.agit.pt)), Carris (GTFS-Realtime) |
| `incendios` | `wildfires` | [EFFIS](https://forest-fire.emergency.copernicus.eu) (Copernicus, CC BY 4.0) |
| `ocorrencias` | `active_incidents` (live: what civil protection is responding to near a place, closest first; needs a free personal key in `FOGOS_API_KEY`, police activity is not public) | [ANEPC](https://prociv.gov.pt) via [Fogos.pt](https://fogos.pt) ([request a key](https://source.fogos.pt/api/request-access)) |
| `eletricidade` | `power_cuts` (planned interruptions by postal code, municipality or parish), `electricity_price` (day-ahead wholesale price for Portugal, per 15 minutes) | [E-REDES open data](https://e-redes.opendatasoft.com) (CC BY 4.0), [OMIE](https://www.omie.es) (free use, unaltered, source cited) |
| `turismo` | `local_accommodation` (the Alojamento Local register: is it registered, how many on a street), `tourism_places` (Blue Flag beaches, surf spots, golf, spas, villages, walking routes, Ways of Saint James, marinas, UNESCO sites, hotels) | [Turismo de Portugal open data](https://dadosabertos.turismodeportugal.pt) (CC BY 4.0) |
| `lisboa` | `lisbon_events` (the city's cultural agenda), `lisbon_street_works` (licensed occupations of public space, road and pavement works), `lisbon_recycling` (nearest ecoponto, vidrão, oleão...) | [Agenda Cultural de Lisboa](https://www.agendalx.pt) (CC BY), [Lisbon open data](https://geodados-cml.hub.arcgis.com) (CC0 for occupations) |

There are 56 tools in total. If your client struggles with that many, load only what you need with `PORTUGAL_MCP_FEATURES` (see below).

### Things to know

- **Statistics are dated.** INE, BPstat and SNS figures are official series with a reference period, often a few months back. Answers quote the period.
- **Health and medicines: information, not advice.** SNS figures are monthly statistics, not live emergency-room queues. Per-pharmacy stock is not public in Portugal, so the tools never claim a pharmacy has a medicine. In an emergency call **112**; for health advice call **SNS 24, 808 24 24 24**.
- **Parliament:** votes are recorded per parliamentary group, as the Parliament publishes them. The first call downloads about 100 MB (roughly a minute), then it is cached for a day.
- **Public contracts:** the yearly BASE files are 15-55 MB, so the first query for a year takes a minute or two. Amounts are contract prices without VAT as reported by the buyer. **Tax numbers (NIF) of individual people are hidden**; company and public-body NIFs are shown.
- **Fuel / EV:** the first EV query loads the national MOBI.E feed (about 80 s), then it is cached.
- **Timetables are scheduled, not live.** Each operator's official GTFS timetable is loaded into a local SQLite file on first use (up to about a minute) and refreshed when the operator publishes a new one. Outside Lisbon, name the city or region (`operator="porto"`, `"coimbra"`, `"madeira"`...); without it only CP and the Lisbon operators are searched. When an operator's published timetable has run out, answers carry a warning instead of silently showing nothing. `transit_trips_between` finds direct services only (no changes). STCP (Porto buses) last published a timetable valid until September 2026, so Porto buses have live positions only (`transit_vehicles`); no open feeds were found for Viseu, Évora, Leiria, the Azores or the Tagus ferries.
- **Live positions** (`transit_vehicles`) are what the vehicles report, usually under a minute old, not arrival predictions.
- **Power cuts** (`power_cuts`) are the interruptions E-REDES has planned for maintenance, usually for the next two weeks; live faults are not published (call E-REDES, 808 100 100).
- **Local accommodation:** the register shows what each owner declared; it does not vouch for an online listing. Owner and operator names of tourist establishments are left out on purpose (many are private individuals).
- **Town-hall data:** `open_data_search(organization="Lisboa")` (or Porto, Cascais, Oeiras, Águeda...) finds that town hall's datasets on dados.gov.pt. Most other towns publish little or nothing.
- **Electricity prices** (`electricity_price`) are OMIE's wholesale day-ahead prices for Portugal in €/MWh, as published (averages and cheapest periods are computed from them). OMIE's download files lag the market by about a day, so today's and tomorrow's prices may only be on omie.es yet. Not the price on a regular bill.
- **Not included:** REN Data Hub (its terms forbid reusing its data without consent), Porto's road-closure files (last updated in 2024).
- **Wildfires are satellite-mapped burnt areas** (EFFIS), a day or more after the fact, not live incidents. For today's danger use `fire_risk`; in an emergency call **112**.

## Install

### No install: the hosted endpoint

A free shared copy runs at **`https://mcp.thiagolannes.com/portugal`** (streamable HTTP, no key, no sign-in). In Claude, add it as a custom connector; in Claude Code:

```bash
claude mcp add --transport http portugal https://mcp.thiagolannes.com/portugal
```

It is rate-limited to 60 requests a minute per IP, and it stays awake and refreshes the large files (contracts, Parliament, EV chargers, timetables) in the background before they expire, so answers come in seconds. Only in the first few minutes after a new deploy can a question about one of those files take a minute or two. One known gap: Parliament's site refuses the deputies file to Cloudflare's network, so `parliament_deputies` only works on a local install (search and votes work on both). For heavy use, run your own copy with `uvx` as below, or `docker build -t portugal-mcp . && docker run -p 8000:8000 portugal-mcp`. The hosted copy is non-commercial and stays that way: its live incidents come from Fogos.pt, whose API terms rule out commercial use.

### Install locally

Needs Python 3.10+. With [uv](https://docs.astral.sh/uv/) nothing else is required:

```bash
uvx portugal-mcp --list-tools
```

Or with pip: `pip install portugal-mcp`.

### Claude Code

```bash
claude mcp add portugal -- uvx portugal-mcp
```

### Claude Desktop

Add this to `claude_desktop_config.json` (also in [claude_desktop_config.snippet.json](claude_desktop_config.snippet.json)):

```json
{
  "mcpServers": {
    "portugal": {
      "command": "uvx",
      "args": ["portugal-mcp"]
    }
  }
}
```

### Codex and other MCP clients

Any client that runs stdio servers works: command `uvx`, args `["portugal-mcp"]`. For HTTP, run `portugal-mcp --http`, which serves streamable HTTP on `127.0.0.1:8000` (change it with `MCP_HOST` / `MCP_PORT`).

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `PORTUGAL_MCP_FEATURES` | all | Comma list of features to load (`clima,geo,ine`) or to drop (`-contratos,-parlamento`). |
| `PORTUGAL_MCP_CACHE_DIR` | OS cache dir | Where downloaded files are cached. |
| `PORTUGAL_MCP_LOG_LEVEL` | `INFO` | Log level; logs go to stderr. |
| `PORTUGAL_MCP_MAX_PARALLEL` | `8` | `--hosted` only: how many tool calls run at once (the large files are still loaded one at a time). |
| `PORTUGAL_MCP_KEEP_WARM_HOURS` | `0` (off) | `--hosted` only: download and parse the large files in the background at start and every N hours, one at a time, rebuilding any that would expire before the next pass. |

`portugal-mcp --refresh` downloads the large files ahead of time (Parliament, this year's contracts, EV chargers, timetables) and then exits.

## Example questions

- "Vai chover no Porto esta semana? Há avisos?"
- "Qual é o risco de incêndio hoje em Monchique?"
- "What's the unemployment rate in Portugal, latest month?"
- "Como evoluiu a Euribor a 6 meses nos últimos 12 meses?"
- "O que o Parlamento votou sobre habitação este mês, e como votou cada partido?"
- "Quais os maiores ajustes diretos de 2026? Quanto a Câmara de Lisboa contratou este ano?"
- "Preciso de ibuprofeno em Coimbra — está em rutura? Que farmácias estão de serviço?"
- "Gasóleo mais barato perto de Setúbal" / "Carregadores rápidos perto do Marquês"
- "Quando passa o próximo autocarro na paragem do Cais do Sodré?"
- "Comboios diretos de Lisboa Oriente para o Porto amanhã de manhã?" / "Próximo elétrico 28 nos Prazeres?"
- "Próximo metro na Trindade?" / "Autocarros de Coimbra para a Figueira da Foz hoje" / "Onde estão agora os autocarros da linha 500 no Porto?"
- "Vai haver corte de luz no 2795-082 nas próximas semanas?"
- "Este alojamento local, 12345/AL, está registado?" / "Praias com bandeira azul em Cascais" / "Spots de surf perto de Peniche"
- "O que há para fazer em Lisboa este sábado, de graça?" / "Há obras na minha rua em Arroios?" / "Onde fica o oleão mais perto?"
- "FlixBus de Lisboa para Madrid amanhã"
- "Quanta área ardeu este ano no Algarve?"

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
pytest              # offline unit tests (fixtures, no network)
pytest -m live      # hits every real source; CI runs this daily
ruff check .
```

[CONTRIBUTING.md](CONTRIBUTING.md) explains how features are laid out and the rules for adding a source. [SECURITY.md](SECURITY.md) explains how to report a vulnerability.

### Maintenance

- **CI:** lint and unit tests on Linux and Windows for every push and pull request, plus `pip-audit`.
- **Live sources:** a daily run against the real APIs that opens an issue when a source changes its format.
- **Dependabot:** proposes dependency and GitHub Actions updates. Actions are pinned by commit SHA.
- **Hosted endpoint:** CI builds the container image and checks the Worker on every pull request; deploys are manual (`Deploy hosted endpoint` workflow).
- **Releases:** only from a version tag, and only after the tests pass. Publishing goes through PyPI trusted publishing and the official MCP Registry, with no stored tokens.

## Replaces

This package merges four earlier servers by the same author, which are now deprecated: `sns-portugal-mcp`, `portugal-medicines-mcp`, `portugal-fuel-ev-mcp` and `carris-metropolitana-mcp`. Their tools continue here. The SNS, medicine and bus tools now carry a prefix (`sns_`, `medicine_`, `bus_`) so they stay unambiguous among the 47 tools.

## License

MIT. The data belongs to its publishers and is used under their open-data terms; please credit them when you reuse it.
