# portugal-mcp

<!-- mcp-name: io.github.ThiagoJacobLannes/portugal-mcp -->

[![CI](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/ci.yml)
[![Live sources](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/live.yml/badge.svg)](https://github.com/ThiagoJacobLannes/portugal-mcp/actions/workflows/live.yml)
[![PyPI](https://img.shields.io/pypi/v/portugal-mcp)](https://pypi.org/project/portugal-mcp/)

**Portugal's public data for AI assistants, in one MCP server.** Ask Claude (or any MCP client) about the weather and warnings, a postal code, unemployment or inflation, the Euribor, what Parliament voted, who won a public contract, how busy the emergency rooms were, whether a medicine is in shortage, the cheapest fuel nearby, when the next train to Porto leaves or how much has burned this fire season.

**Read-only, no API keys, no accounts.** Every answer comes from an official or public open-data source and says which one and for which date. Not affiliated with any of the sources.

*Português:* servidor MCP com os dados públicos de Portugal (IPMA, INE, Banco de Portugal, Parlamento, Portal BASE, dados.gov.pt, SNS, Infarmed, DGEG, MOBI.E, Carris Metropolitana, horários da CP, Metro de Lisboa, Carris e Fertagus, incêndios do EFFIS). Só leitura, sem chaves. As ferramentas aceitam perguntas em português.

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
| `transit` | `transit_operators`, `transit_find_stops`, `transit_departures`, `transit_trips_between` | Official GTFS timetables of [CP](https://www.cp.pt), [Metro de Lisboa](https://www.metrolisboa.pt), [Carris](https://www.carris.pt) and [Fertagus](https://www.fertagus.pt) |
| `incendios` | `wildfires` | [EFFIS](https://forest-fire.emergency.copernicus.eu) (Copernicus, CC BY 4.0) |
| `ocorrencias` | `active_incidents` (live: what civil protection is responding to near a place, closest first; needs a free personal key in `FOGOS_API_KEY`, police activity is not public) | [ANEPC](https://prociv.gov.pt) via [Fogos.pt](https://fogos.pt) ([request a key](https://source.fogos.pt/api/request-access)) |

There are 48 tools in total. If your client struggles with that many, load only what you need with `PORTUGAL_MCP_FEATURES` (see below).

### Things to know

- **Statistics are dated.** INE, BPstat and SNS figures are official series with a reference period, often a few months back. Answers quote the period.
- **Health and medicines: information, not advice.** SNS figures are monthly statistics, not live emergency-room queues. Per-pharmacy stock is not public in Portugal, so the tools never claim a pharmacy has a medicine. In an emergency call **112**; for health advice call **SNS 24, 808 24 24 24**.
- **Parliament:** votes are recorded per parliamentary group, as the Parliament publishes them. The first call downloads about 100 MB (roughly a minute), then it is cached for a day.
- **Public contracts:** the yearly BASE files are 15-55 MB, so the first query for a year takes a minute or two. Amounts are contract prices without VAT as reported by the buyer. **Tax numbers (NIF) of individual people are hidden**; company and public-body NIFs are shown.
- **Fuel / EV:** the first EV query loads the national MOBI.E feed (about 80 s), then it is cached.
- **Timetables are scheduled, not live.** CP, Metro de Lisboa, Carris and Fertagus publish official GTFS timetables; each is loaded into a local SQLite file on first use (Carris takes about 20 s) and refreshed when the operator publishes a new one. `transit_trips_between` finds direct services only (no changes). Metro do Porto and STCP do not publish a current official feed.
- **Wildfires are satellite-mapped burnt areas** (EFFIS), a day or more after the fact, not live incidents. For today's danger use `fire_risk`; in an emergency call **112**.

## Install

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
- **Releases:** only from a version tag, and only after the tests pass. Publishing goes through PyPI trusted publishing and the official MCP Registry, with no stored tokens.

## Replaces

This package merges four earlier servers by the same author, which are now deprecated: `sns-portugal-mcp`, `portugal-medicines-mcp`, `portugal-fuel-ev-mcp` and `carris-metropolitana-mcp`. Their tools continue here. The SNS, medicine and bus tools now carry a prefix (`sns_`, `medicine_`, `bus_`) so they stay unambiguous among the 47 tools.

## License

MIT. The data belongs to its publishers and is used under their open-data terms; please credit them when you reuse it.
