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


def test_regional_transit():
    from portugal_mcp import transit

    for op in ("metro_porto", "tub", "smtuc", "sit_coimbra", "cira", "mobicascais", "tcb", "vamus", "proximo", "funchal", "siga_madeira"):
        r = ok(transit.transit_find_stops(lat=transit_center[op][0], lon=transit_center[op][1], operator=op, limit=1))
        assert r["stops"], op


transit_center = {
    "metro_porto": (41.1517, -8.6093), "tub": (41.5503, -8.4200), "smtuc": (40.2033, -8.4103),
    "sit_coimbra": (40.2033, -8.4103), "cira": (40.6405, -8.6538), "mobicascais": (38.6970, -9.4210),
    "tcb": (38.6631, -9.0724), "vamus": (37.0194, -7.9304), "proximo": (37.0194, -7.9304),
    "funchal": (32.6496, -16.9086), "siga_madeira": (32.6496, -16.9086),
}


def test_live_vehicles():
    from portugal_mcp import transit_live

    for op in ("stcp", "smtuc", "carris"):
        assert "vehicles_reporting" in ok(transit_live.transit_vehicles(op, limit=1)), op


def test_power_cuts():
    from portugal_mcp import eletricidade

    assert "by_municipality" in ok(eletricidade.power_cuts())
    assert len(ok(eletricidade.electricity_price())["periods"]) >= 24


def test_tourism():
    from portugal_mcp import turismo

    assert ok(turismo.local_accommodation(municipality="Lisboa", limit=1))["found"] > 1000
    assert ok(turismo.tourism_places("blue_flag_beaches", place="Cascais"))["found"] > 0
    assert ok(turismo.tourism_places("hotels", place="Porto", limit=1))["places"]


def test_lisbon():
    from portugal_mcp import lisboa

    assert "events" in ok(lisboa.lisbon_events())
    ok(lisboa.lisbon_street_works(lat=38.7369, lon=-9.1427, radius_m=800))
    assert ok(lisboa.lisbon_recycling(38.7369, -9.1427, kind="vidrao", radius_m=800))["found"] > 0


def test_open_data_by_town_hall():
    from portugal_mcp import dados

    assert ok(dados.open_data_search("estacionamento", organization="Lisboa"))["organization"] == "Município de Lisboa"


def test_flixbus():
    from portugal_mcp import transit

    assert ok(transit.transit_find_stops("Lisboa", operator="flixbus", limit=1))["stops"]


def test_wildfires():
    from portugal_mcp import incendios

    assert ok(incendios.wildfires(year=2025))["fires"] > 0


@pytest.mark.skipif(not __import__("os").environ.get("FOGOS_API_KEY"), reason="needs a personal Fogos.pt API key")
def test_active_incidents():
    from portugal_mcp import ocorrencias

    r = ok(ocorrencias.active_incidents())
    assert "count" in r and "by_district" in r
