"""Live checks against the real public sources (the daily CI job runs: pytest -m live).

They check shape, not values: a failure means a source moved or changed its format.
"""

import anyio
import pytest

pytestmark = pytest.mark.live


def ok(r):
    assert isinstance(r, dict), r
    assert "error" not in r, r
    return r


def test_ipma():
    from portugal_mcp import clima

    assert ok(clima.weather_forecast("Lisboa"))["days"]
    ok(clima.weather_warnings())
    assert ok(clima.fire_risk("Sintra"))["risk_level"] in range(1, 6)
    assert ok(clima.sea_forecast("Lisboa"))["wave_height_m"]
    ok(clima.earthquakes(days=30))


def test_geoapi():
    from portugal_mcp import geo

    assert ok(geo.postal_code("1000-001"))["municipality"] == "Lisboa"
    assert ok(geo.where_is(41.1496, -8.611))["municipality"] == "Porto"
    assert ok(geo.municipality("Sintra"))["census_2021"]["residents"] > 300000


def test_ine():
    from portugal_mcp import ine

    assert ok(ine.ine_search("desemprego"))["indicators"]
    assert ok(ine.ine_indicator("0007976", periods=1))["rows"]


def test_bpstat():
    from portugal_mcp import bpstat

    r = ok(bpstat.bpstat_search("dólar"))
    assert r["series"]
    assert ok(bpstat.bpstat_series([r["series"][0]["id"]], last_n=2))["series"][0]["observations"]


def test_dados():
    from portugal_mcp import dados

    r = ok(dados.open_data_search("escolas", limit=2))
    assert r["datasets"]
    ok(dados.open_data_dataset(r["datasets"][0]["id"]))


def test_parliament():
    from portugal_mcp import parlamento

    assert 200 <= ok(parlamento.parliament_deputies())["seats_total"] <= 230
    assert ok(parlamento.parliament_search(limit=1))["initiatives"]


def test_contracts():
    from portugal_mcp import contratos

    assert ok(contratos.public_contracts(limit=1))["count"] > 0


def test_sns():
    from portugal_mcp.saude import tools

    ok(tools.find_institutions("Santa Maria"))


def test_medicines():
    from portugal_mcp.medicamentos import tools

    ok(tools.pharmacies("Lisboa"))


def test_fuel():
    from portugal_mcp.energia import tools

    ok(anyio.run(tools.fuel_types))


def test_carris():
    from portugal_mcp.transportes import tools

    ok(tools.find_stops("Cais do Sodré"))


def test_transit():
    from portugal_mcp import transit

    r = ok(transit.transit_trips_between("Lisboa Oriente", "Porto Campanhã", operator="cp", window_minutes=1440))
    assert r.get("results")
    assert ok(transit.transit_departures("Baixa-Chiado", operator="metro_lisboa", window_minutes=1440))["results"]
    ok(transit.transit_find_stops("Roma-Areeiro", operator="fertagus"))
    ok(transit.transit_find_stops("Prazeres", operator="carris"))


def test_wildfires():
    from portugal_mcp import incendios

    assert ok(incendios.wildfires(year=2025))["fires"] > 0
