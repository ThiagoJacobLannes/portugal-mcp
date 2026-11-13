"""Offline tests: parsing, privacy masking, safety checks, server wiring. No network."""

import base64
import io
import json
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

import anyio
import pytest

from portugal_mcp import bpstat, common, contratos, dados, geo, ine, parlamento, server

# --- common -------------------------------------------------------------------------


def test_iter_json_array_streams_items_across_chunks():
    items = [{"i": i, "text": "çãõ " * (i % 7), "nested": [1, {"a": None}]} for i in range(500)]
    fp = io.StringIO("  [\n" + ",\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n]  ")
    assert list(common.iter_json_array(fp, chunk_size=97)) == items


def test_iter_json_array_empty_and_truncated():
    assert list(common.iter_json_array(io.StringIO("[]"))) == []
    with pytest.raises(ValueError):
        list(common.iter_json_array(io.StringIO('[{"a": 1}, {"b": '), chunk_size=4))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1 234,5", 1234.5), ("1.234,5", 1234.5), ("12.5", 12.5), (7, 7.0), ("x", None), ("", None), (None, None), ("ND", None)],
)
def test_pt_number(raw, expected):
    assert common.pt_number(raw) == expected


def test_keywords_drop_stopwords_and_accents():
    assert common.keywords("Dólar dos EUA") == ["dolar", "eua"]


def test_cache_dir_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTUGAL_MCP_CACHE_DIR", str(tmp_path))
    assert common.cache_dir("x") == tmp_path / "x"


def test_filecache_serves_stale_copy_when_source_fails(monkeypatch):
    fc = common.FileCache("t", "doc", "https://example.invalid/doc", ttl=0)
    monkeypatch.setattr(fc, "_remote_modified", lambda url: None)
    assert fc.get(lambda url: [1, 2])["data"] == [1, 2]

    def boom(url):
        raise RuntimeError("down")

    fc._mem = None
    doc = fc.get(boom)
    assert doc["data"] == [1, 2]
    assert doc["stale"] is True


def test_user_agent_identifies_project():
    assert "portugal-mcp" in common.USER_AGENT
    assert common.PROJECT_URL in common.USER_AGENT


# --- holidays -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "easter"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)), (2038, date(2038, 4, 25))],
)
def test_easter(year, easter):
    assert geo._easter(year) == easter


def test_holidays_2026():
    rows = {r["name_en"]: r["date"] for r in geo.holidays(2026)}
    assert rows["Good Friday"] == date(2026, 4, 3)
    assert rows["Corpus Christi"] == date(2026, 6, 4)
    assert rows["Carnival"] == date(2026, 2, 17)
    assert len([r for r in geo.holidays(2026) if r["type"] == "national"]) == 13


# --- parliament ---------------------------------------------------------------------


def test_parse_vote_detail():
    d = "A Favor: <I>PSD</I>, <I> CH</I>, <I> IL</I><BR>Contra:<I>PCP</I>, <I> BE</I><BR>Abstenção: <I>PS</I>"
    assert parlamento.parse_vote_detail(d) == {"for": ["PSD", "CH", "IL"], "against": ["PCP", "BE"], "abstain": ["PS"]}
    assert parlamento.parse_vote_detail(None) == {}


def test_parliament_file_url_roundtrip():
    q = parse_qs(urlparse(parlamento._iniciativas_url("XVII")).query)
    path = base64.b64decode(bytes.fromhex(q["path"][0])).decode()
    assert path.endswith("/Iniciativas/XVII%20Legislatura/IniciativasXVII_json.txt")
    assert q["fich"] == ["IniciativasXVII_json.txt"]


@pytest.mark.parametrize("bad", ["17", "XVII; rm", "../x", "ABC"])
def test_legislature_validation(bad):
    with pytest.raises(ValueError):
        parlamento._leg(bad)


def test_compact_initiative():
    raw = {
        "IniId": "1", "IniDescTipo": "Projeto de Lei", "IniNr": "9", "IniLeg": "XVII", "IniTitulo": " Título ",
        "IniLinkTexto": "http://app.parlamento.pt/x.pdf",
        "IniAutorGruposParlamentares": [{"GP": "PS"}], "IniAutorDeputados": [{"nome": "A"}, {"nome": "B"}],
        "IniEventos": [
            {"Fase": "Entrada", "DataFase": "2025-07-01"},
            {"Fase": "Votação na generalidade", "DataFase": "2025-07-10",
             "Votacao": [{"data": "2025-07-10", "resultado": "Rejeitado", "detalhe": "Contra: <I>PSD</I>", "unanime": None}]},
        ],
    }
    c = parlamento._compact(raw)
    assert c["ref"] == "Projeto de Lei 9/XVII"
    assert c["title"] == "Título"
    assert c["author_deputies"] == 2
    assert c["text_url"].startswith("https://")
    assert c["votes"][0]["positions"] == {"against": ["PSD"]}
    assert c["last_phase"]["phase"] == "Votação na generalidade"


# --- public contracts ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "nif"),
    [
        ("500051070 - Município de Lisboa", "500051070"),
        ("509999999 - Exemplo Software, Lda", "509999999"),
        ("212345678 - Maria Silva", "individual (NIF hidden)"),
        ("123456789 - João", "individual (NIF hidden)"),
        ("451234567 - Non-resident", "individual (NIF hidden)"),
    ],
)
def test_person_nif_is_masked(raw, nif):
    assert contratos._party(raw)["nif"] == nif


def test_party_name_unescaped():
    assert contratos._party("501234567 - Sousa &amp; Filhos, Lda")["name"] == "Sousa & Filhos, Lda"


def test_compact_contract_dates_and_parties():
    c = contratos._compact({
        "idcontrato": "1", "dataPublicacao": "04/12/2025", "dataCelebracaoContrato": "23/02/2026",
        "objectoContrato": "x" * 500, "tipoContrato": ["Aquisição de serviços"], "adjudicante": ["600052737 - SAS"],
        "adjudicatarios": ["212345678 - Pessoa"], "precoContratual": 9000.0, "cpv": ["72210000-0 - Software"],
    })
    assert c["published"] == "2025-12-04"
    assert c["signed"] == "2026-02-23"
    assert len(c["object"]) == 240
    assert c["suppliers"][0]["nif"].startswith("individual")
    assert c["type"] == "Aquisição de serviços"


def test_compact_contract_object_parties_2026_format():
    # IMPIC switched to party objects and an upper-case CPV key in Nov 2026.
    c = contratos._compact({
        "idcontrato": "2", "adjudicante": [{"nifEntContrato": "600052737", "descEntContrato": "SAS &amp; Co"}],
        "adjudicatarios": [{"nifEntContrato": "212345678", "descEntContrato": "Pessoa"}],
        "CPV": [{"cpvID": "72210000-0", "cpvdescricao": "Software", "CPVObjeto": "Principal"}],
    })
    assert c["buyer"][0] == {"nif": "600052737", "name": "SAS & Co"}
    assert c["suppliers"][0]["nif"] == "individual (NIF hidden)"
    assert c["cpv"] == "72210000-0 - Software"


def test_match_party_by_nif_and_name():
    parties = [{"nif": "500051070", "name": "Município de Lisboa"}]
    assert contratos._match_party(parties, "500 051 070")
    assert contratos._match_party(parties, "municipio de lisboa")
    assert not contratos._match_party(parties, "Porto")


# --- open data preview safety -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://dados.gov.pt/x.csv",
        "https://evil.example/x.csv",
        "https://dados.gov.pt.evil.example/x.csv",
        "file:///etc/passwd",
        "https://localhost/x",
    ],
)
def test_preview_refuses_other_hosts(url):
    assert "error" in dados.open_data_preview(url)


# --- BPstat -------------------------------------------------------------------------


def test_bpstat_observations_decode():
    doc = {
        "id": ["1", "2", "reference_date"],
        "size": [2, 1, 2],
        "dimension": {
            "1": {"category": {"index": {"10": 0, "11": 1}}},
            "2": {"category": {"index": {"20": 0}}},
            "reference_date": {"category": {"index": {"2026-01-31": 0, "2026-02-28": 1}}},
        },
        "value": {"0": 1.0, "1": 1.5, "2": 2.0, "3": 2.5},
    }
    s = {"dimension_category": [{"dimension_id": 1, "category_id": 11}, {"dimension_id": 2, "category_id": 20}]}
    assert [o["value"] for o in bpstat._observations(doc, s)] == [2.0, 2.5]


def test_bpstat_pages_never_loops_on_a_repeated_link(monkeypatch):
    calls = []

    def fake(url, params=None, **kw):
        calls.append((url, params))
        return {"extension": {"next_page": "https://x/p2"}}

    monkeypatch.setattr(bpstat, "get_json", fake)
    assert len(bpstat._pages("https://x/p1", {"a": 1})) == 2
    # params only on the first call: next_page urls carry their own query string
    assert calls == [("https://x/p1", {"a": 1}), ("https://x/p2", None)]


def test_bpstat_topic_routing(monkeypatch):
    monkeypatch.setattr(bpstat, "_domains", lambda lang: [{"id": i, "label": f"d{i}"} for i in (21, 22, 29)])
    assert bpstat._pick_domains(["euribor", "12", "meses"], "PT")[0]["id"] == 22
    assert bpstat._pick_domains(["dolar"], "PT")[0]["id"] == 29


# --- INE ----------------------------------------------------------------------------


def test_ine_indicator_prefers_exact_place(monkeypatch):
    payload = [{
        "IndicadorDsg": "Teste", "UltimoPref": "2024",
        "Dados": {"2024": [
            {"geodsg": "Área Metropolitana de Lisboa", "geocod": "17", "dim_3_t": "Total", "valor": "1,5"},
            {"geodsg": "Lisboa", "geocod": "1106", "dim_3_t": "Total", "valor": "2,5"},
        ]},
    }]
    monkeypatch.setattr(ine, "_data", lambda code, lang: payload)
    r = ine.ine_indicator("8074", place="Lisboa")
    assert r["indicator"] == "0008074"
    assert [x["value"] for x in r["rows"]] == [2.5]


# --- server wiring ------------------------------------------------------------------


def test_feature_selection(monkeypatch):
    monkeypatch.setenv("PORTUGAL_MCP_FEATURES", "clima,geo")
    assert server.enabled_features() == ["clima", "geo"]
    monkeypatch.setenv("PORTUGAL_MCP_FEATURES", "-contratos,-parlamento")
    names = server.enabled_features()
    assert "contratos" not in names
    assert "clima" in names
    monkeypatch.setenv("PORTUGAL_MCP_FEATURES", "nonsense")
    assert server.enabled_features() == list(server.FEATURES)


def test_all_tools_register_with_unique_names_and_docs():
    server.load(list(server.FEATURES))
    tools = anyio.run(server.mcp.list_tools)
    names = [t.name for t in tools]
    assert len(names) == len(set(names)) >= 40
    for t in tools:
        assert t.description and len(t.description) > 20, t.name
    assert "[parlamento]" in server.instructions()


def test_sns_institutions_fall_back_to_bundled_snapshot(monkeypatch):
    from portugal_mcp.saude import client as sns

    c = sns.SNSClient(sns.ODS_BASE, "https://example.invalid/institutions.json")

    def unreachable(url, params=None):
        raise sns.SNSError("could not reach it: Connection reset by peer")

    monkeypatch.setattr(c, "_get_json", unreachable)
    rows = c.institutions()
    assert len(rows) > 100
    assert {"name", "latitude", "longitude"} <= set(rows[0])


# --- transit (GTFS) -----------------------------------------------------------------


def _mini_gtfs(path):
    import zipfile as zf

    files = {
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "A,Lisboa Oriente,38.7677,-9.0991,1,\nA1,Lisboa Oriente,38.7677,-9.0991,0,A\n"
        "B,Porto Campanhã,41.1486,-8.5855,0,\nC,Baixa-Chiado,38.7106,-9.1395,0,\n",
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\nR,AP,Alfa Pendular,2\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,trip_short_name\n"
        "R,WK,T1,Porto Campanhã,131\nR,WK,T2,Porto Campanhã,133\nR,SUN,T3,Porto Campanhã,135\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,07:09:00,07:09:00,A1,1\nT1,09:48:00,09:48:00,B,2\n"
        "T2,23:50:00,23:50:00,A1,1\nT2,26:10:00,26:10:00,B,2\n"
        "T3,10:00:00,10:00:00,A1,1\nT3,12:40:00,12:40:00,B,2\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WK,1,1,1,1,1,0,0,20260101,20261231\nSUN,0,0,0,0,0,0,1,20260101,20261231\n",
        "calendar_dates.txt": "service_id,date,exception_type\nWK,20261208,2\nSUN,20261208,1\n",
    }
    with zf.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)


@pytest.fixture
def mini_transit(tmp_path, monkeypatch):
    import sqlite3

    from portugal_mcp import transit

    zp, db = tmp_path / "f.zip", tmp_path / "f.sqlite"
    _mini_gtfs(zp)
    transit._build("cp", str(zp), str(db), "fp")
    con = sqlite3.connect(db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    monkeypatch.setattr(transit, "_db", lambda op: con)
    monkeypatch.setattr(transit, "_ops", lambda operator: ["cp"])
    return transit


def test_transit_key_folds_punctuation():
    from portugal_mcp import transit

    assert transit._key("Baixa-Chiado") == transit._key("baixa / chiado") == "baixa chiado"


def test_transit_trips_between_weekday(mini_transit):
    r = mini_transit.transit_trips_between("Oriente", "Campanha", when="2026-11-03T07:00", window_minutes=120)
    trips = r["results"][0]["trips"]
    assert [(t["depart"], t["arrive"], t["duration_min"], t["train_number"]) for t in trips] == [("07:09", "09:48", 159, "131")]


def test_transit_after_midnight_and_exceptions(mini_transit):
    late = mini_transit.transit_trips_between("Oriente", "Campanha", when="2026-11-03T23:30", window_minutes=60)
    assert late["results"][0]["trips"][0]["arrive"] == "02:10 (+1 day)"
    # 8 December is a holiday: weekday service removed, Sunday service added
    hol = mini_transit.transit_trips_between("Oriente", "Campanha", when="2026-12-08T06:00", window_minutes=600)
    assert [t["train_number"] for t in hol["results"][0]["trips"]] == ["135"]


def test_transit_departures_skip_arrivals(mini_transit):
    dep = mini_transit.transit_departures("Lisboa Oriente", when="2026-11-03T07:00", window_minutes=60)
    assert [d["time"] for d in dep["results"][0]["departures"]] == ["07:09"]
    arr = mini_transit.transit_departures("Porto Campanhã", when="2026-11-03T09:30", window_minutes=60)
    assert arr["results"][0]["departures"] == []


def test_transit_regions_and_aliases():
    from portugal_mcp import transit

    assert transit._ops(None) == ["cp", "metro_lisboa", "carris", "fertagus"]
    assert transit._ops("Porto") == ["metro_porto", "cp"]
    assert transit._ops("Metro do Porto") == ["metro_porto"]
    assert transit._ops("Funchal") == ["funchal", "siga_madeira"]
    assert all(op in transit.OPERATORS for ops in transit.REGIONS.values() for op in ops)
    with pytest.raises(ValueError):
        transit._ops("Atlantis")


def test_transit_expired_timetable_warns(mini_transit):
    r = mini_transit.transit_departures("Lisboa Oriente", when="2027-02-01T07:00", window_minutes=60)
    res = r["results"][0]
    assert res["timetable_valid_until"] == "2026-12-31"
    assert "ends on 2026-12-31" in res["warning"]
    ok = mini_transit.transit_departures("Lisboa Oriente", when="2026-11-03T07:00", window_minutes=60)
    assert "warning" not in ok["results"][0]


def test_transit_strips_stop_codes(tmp_path):
    import sqlite3
    import zipfile as zf

    from portugal_mcp import transit

    zp, db = tmp_path / "m.zip", tmp_path / "m.sqlite"
    with zf.ZipFile(zp, "w") as z:
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n1,Marina (00001),32.6,-16.9\n2,Marina (00003),32.6,-16.9\n")
    transit._build("funchal", str(zp), str(db), "fp")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    groups = transit._stations(con, "marina")
    assert len(groups) == 1 and groups[0]["name"] == "Marina" and groups[0]["stop_ids"] == {"1", "2"}


# --- live vehicles (GTFS-Realtime, STCP) ------------------------------------------------


def _pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb(num, value):
    import struct

    if isinstance(value, int):
        return _pb_varint(num << 3) + _pb_varint(value)
    if isinstance(value, float):
        return _pb_varint(num << 3 | 5) + struct.pack("<f", value)
    data = value.encode() if isinstance(value, str) else value
    return _pb_varint(num << 3 | 2) + _pb_varint(len(data)) + data


def test_gtfsrt_vehicle_positions():
    from portugal_mcp import gtfsrt

    trip = _pb(1, "T9") + _pb(5, "28E") + _pb(6, 1)
    pos = _pb(1, 38.7106) + _pb(2, -9.1395) + _pb(3, 90.0) + _pb(5, 5.0)
    vp = _pb(1, trip) + _pb(2, pos) + _pb(4, 2) + _pb(5, 1794389018) + _pb(7, "S1") + _pb(8, _pb(1, "544"))
    feed = _pb(1, _pb(1, "2.0") + _pb(3, 1794389020)) + _pb(2, _pb(1, "e1") + _pb(4, vp)) + _pb(2, _pb(1, "e2"))
    ts, rows = gtfsrt.vehicles(feed)
    assert ts == 1794389020 and len(rows) == 1
    v = rows[0]
    assert (v["vehicle_id"], v["route_id"], v["trip_id"], v["stop_id"], v["status"]) == ("544", "28E", "T9", "S1", "on the way to")
    assert abs(v["lat"] - 38.7106) < 1e-4 and v["speed_kmh"] == 18 and v["bearing"] == 90


def test_transit_vehicles_stcp(monkeypatch):
    import time as _t
    from datetime import datetime, timezone

    from portugal_mcp import transit_live

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.00Z")

    class Resp:
        def json(self):
            return [
                {"fleetVehicleId": "3543", "annotations": ["stcp:route:902", "stcp:sentido:0"], "bearing": 258, "speed": 0,
                 "location": {"coordinates": [-8.6597, 41.1318]}, "observationDateTime": now},
                {"fleetVehicleId": "3262", "annotations": ["stcp:route:500", "stcp:sentido:1"], "speed": 12,
                 "location": {"coordinates": [-8.6458, 41.1484]}, "observationDateTime": now},
                {"fleetVehicleId": "1", "annotations": ["stcp:route:500"], "location": {"coordinates": [-8.6, 41.1]},
                 "observationDateTime": "2020-01-01T00:00:00.00Z"},
            ]

    monkeypatch.setattr(transit_live, "http", lambda: type("C", (), {"get": lambda self, *a, **k: Resp()})())
    transit_live._cache.clear()
    r = transit_live.transit_vehicles("porto", line="500")
    assert r["vehicles_reporting"] == 2 and r["matching"] == 1
    v = r["vehicles"][0]
    assert v["vehicle_id"] == "3262" and v["direction_id"] == 1 and v["seconds_ago"] < 60
    near = transit_live.transit_vehicles("stcp", lat=41.1318, lon=-8.6597, radius_km=0.5)
    assert [x["vehicle_id"] for x in near["vehicles"]] == ["3543"]
    assert _t.time()  # keeps the import used


# --- electricity (E-REDES planned cuts) --------------------------------------------------


def test_power_cuts_groups_and_matches(monkeypatch):
    from datetime import datetime, timedelta

    from portugal_mcp import eletricidade

    day = (datetime.now(eletricidade.TZ) + timedelta(days=3)).date().isoformat()
    rows = [
        {"zipcode": z, "municipality": "Oeiras", "parish": "Algés", "startdatetime": f"{day}T00:30:00",
         "enddatetime": f"{day}T04:30:00", "durationallocation": "240", "updatedatetime": "2026-11-11"}
        for z in ("2795-082", "2795-083")
    ] + [{"zipcode": "2735-133", "municipality": "Sintra", "parish": "Cacém", "startdatetime": "2020-01-01T04:00:00",
          "enddatetime": "2020-01-01T06:00:00", "durationallocation": "120"}]
    monkeypatch.setattr(eletricidade, "_rows", lambda: rows)
    r = eletricidade.power_cuts(postal_code="2795-082")
    assert r["your_postal_code_affected"] is True and r["found"] == 1
    c = r["planned_cuts"][0]
    assert (c["date"], c["from"], c["to"], c["postal_codes"]) == (day, "00:30", "04:30", ["2795-082", "2795-083"])
    assert eletricidade.power_cuts(postal_code="2795-999")["your_postal_code_affected"] is False
    assert eletricidade.power_cuts(municipality="sintra")["found"] == 0  # past cut dropped
    assert eletricidade.power_cuts()["by_municipality"][0]["municipality"] == "Oeiras"
    assert "error" in eletricidade.power_cuts(postal_code="27950")


def test_like_pattern_ignores_accents():
    from portugal_mcp.common import like_pattern

    assert like_pattern("São Vicente") == like_pattern("sao vicente") == "%S__%V___NT_%"


def test_transit_lisbon_synonym_and_station_fallback(tmp_path):
    import sqlite3
    import zipfile as zf

    from portugal_mcp import transit

    zp, db = tmp_path / "f.zip", tmp_path / "f.sqlite"
    with zf.ZipFile(zp, "w") as z:
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\nL,Lisbon (Oriente),38.77,-9.10\nPA,Porto (Airport),41.24,-8.67\nPC,Porto (Campanha),41.15,-8.59\n")
        z.writestr("routes.txt", "route_id,route_short_name,route_long_name,route_type\nR,N1,FlixBus N1,3\n")
        z.writestr("trips.txt", "route_id,service_id,trip_id\nR,S,T\n")
        z.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT,01:30:00,01:30:00,L,1\nT,05:00:00,05:00:00,PC,2\n")
        z.writestr("calendar.txt", "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nS,1,1,1,1,1,1,1,20260101,20271231\n")
    transit._build("flixbus", str(zp), str(db), "fp")
    con = sqlite3.connect(db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    assert transit._stations(con, "Lisboa")[0]["name"] == "Lisbon (Oriente)"
    import pytest as _pt

    mp = _pt.MonkeyPatch()
    mp.setattr(transit, "_db", lambda op: con)
    try:
        r = transit.transit_trips_between("Lisboa", "Porto", operator="flixbus", when="2026-11-12T00:00", window_minutes=600)
    finally:
        mp.undo()
    res = r["results"][0]
    assert res["to"] == "Porto (Campanha)" and res["trips"][0]["depart"] == "01:30"
    assert "ODbL" in res["attribution"]


def test_electricity_price_parses_and_falls_back(monkeypatch):
    from datetime import date

    from portugal_mcp import eletricidade

    text = "MARGINALPDBCPT;\n" + "".join(f"2026;11;09;{p};{100 + p};{100 + p};\n" for p in range(1, 97)) + "*\n"

    class Resp:
        status_code = 200

        def __init__(self, t):
            self.text = t

        def raise_for_status(self):
            pass

    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params.get("filename"))
        if params.get("filename", "").endswith("20261109.1"):
            return Resp(text)
        r = Resp("<html>not found</html>")
        r.status_code = 404
        return r

    monkeypatch.setattr(eletricidade, "http", lambda: type("C", (), {"get": staticmethod(get)})())
    monkeypatch.setattr(eletricidade, "_latest_published", lambda: date(2026, 11, 9))
    eletricidade._prices.clear()
    r = eletricidade.electricity_price("2026-11-12", cheapest=2)
    assert r["day"] == "2026-11-09" and "not published" in r["note"]
    assert len(r["periods"]) == 96 and r["periods"][1] == {"from": "00:15", "eur_mwh": 102.0}
    c = r["computed_from_omie_values"]
    assert c["min"] == 101.0 and c["max"] == 196.0 and [x["from"] for x in c["cheapest"]] == ["00:00", "00:15"]
    assert "OMIE" in r["source"]


# --- tourism (Turismo de Portugal) -------------------------------------------------------


def test_turismo_clean_drops_owners_and_maps_codes(monkeypatch):
    from portugal_mcp import turismo

    monkeypatch.setattr(turismo, "_muni_names", lambda: {"1105": "Cascais"})
    feat = {
        "attributes": {"OBJECTID": 1, "Denominação": "Carcavelos", "DICO": 1105, "EntProprietaria": "Maria Silva",
                       "EntExploradora": "Joao Lda", "DataRegisto": -86400000,
                       "Website": '<a href="https://example.pt/x" target="_top">site</a>', "Email": " "},
        "geometry": {"x": -9.335, "y": 38.6797},
    }
    r = turismo._clean(feat, {"DataRegisto"})
    assert r["Concelho"] == "Cascais" and "DICO" not in r
    assert "EntProprietaria" not in r and "EntExploradora" not in r and "Email" not in r
    assert r["DataRegisto"] == "1969-12-31"  # pre-1970 dates work on Windows too
    assert r["Website"] == "https://example.pt/x" and (r["lat"], r["lon"]) == (38.6797, -9.335)


def test_tourism_places_filters_small_layers(monkeypatch):
    from portugal_mcp import turismo

    rows = [
        {"Denominação": "Carcavelos", "Concelho": "Cascais", "lat": 38.6797, "lon": -9.335, "_pts": [(38.6797, -9.335)]},
        {"Denominação": "Nazaré", "Concelho": "Nazaré", "lat": 39.6, "lon": -9.07, "_pts": [(39.6, -9.07)]},
    ]
    monkeypatch.setattr(turismo, "_small", lambda kind: rows)
    assert [p["Denominação"] for p in turismo.tourism_places("beaches", place="cascais")["places"]] == ["Carcavelos"]
    near = turismo.tourism_places("praias", lat=39.6, lon=-9.07, radius_km=5)
    assert near["found"] == 1 and near["places"][0]["distance_km"] == 0
    assert "error" in turismo.tourism_places("castles")


def test_local_accommodation_needs_a_filter():
    from portugal_mcp import turismo

    assert "error" in turismo.local_accommodation()
    assert "error" in turismo.local_accommodation(registration="AL")


# --- Lisbon --------------------------------------------------------------------------------


def test_lisbon_events_filters(monkeypatch):
    from portugal_mcp import lisboa

    events = [
        {"title": {"rendered": "Fado no Castelo"}, "occurences": ["2026-11-14"], "categories_name_list": {"m": {"name": "música"}},
         "venue": {"v": {"name": "Castelo de S. Jorge"}}, "price_cat": "free", "price_val": "", "link": "https://x/1"},
        {"title": {"rendered": "Expo &amp; Arte"}, "occurences": [], "StartDate": "2026-11-01", "LastDate": "2026-12-31",
         "categories_name_list": {"a": {"name": "exposições"}}, "price_cat": "value",
         "price_val": 'a:1:{i:0;a:1:{s:5:"value";s:1:"5";}}', "link": "https://x/2"},
        {"title": {"rendered": "Old"}, "occurences": ["2026-10-01"], "link": "https://x/3"},
    ]
    monkeypatch.setattr(lisboa, "_all_events", lambda: events)
    r = lisboa.lisbon_events(day="2026-11-14")
    assert [e["title"] for e in r["events"]] == ["Fado no Castelo", "Expo & Arte"]
    assert r["events"][1]["price"] == "€5" and r["events"][0]["price"] == "free"
    assert [e["title"] for e in lisboa.lisbon_events(day="2026-11-14", free_only=True)["events"]] == ["Fado no Castelo"]
    assert [e["title"] for e in lisboa.lisbon_events(day="2026-11-14", query="fado")["events"]] == ["Fado no Castelo"]
    assert lisboa.lisbon_events(day="2026-11-14", category="exposicoes")["found"] == 1
    assert "error" in lisboa.lisbon_events(day="2026-11-14", until="2027-02-01")


# --- wildfires (EFFIS) ----------------------------------------------------------------

EFFIS_SAMPLE = b"""<?xml version='1.0' encoding="UTF-8" ?>
<wfs:FeatureCollection xmlns:ms="http://mapserver.gis.umn.edu/mapserver" xmlns:gml="http://www.opengis.net/gml" xmlns:wfs="http://www.opengis.net/wfs">
 <gml:featureMember><ms:modis.ba.poly.season gml:id="a">
  <ms:FIREDATE>2026-07-02 02:00:00</ms:FIREDATE><ms:LASTUPDATE>2026-07-03 10:00:00</ms:LASTUPDATE>
  <ms:PROVINCE>Viseu D\xc3\xa3o Laf\xc3\xb5es</ms:PROVINCE><ms:COMMUNE>Cambra</ms:COMMUNE><ms:AREA_HA>13197</ms:AREA_HA>
  <ms:BROADLEA>10</ms:BROADLEA><ms:CONIFER>30</ms:CONIFER><ms:MIXED>8</ms:MIXED><ms:SCLEROPH>40</ms:SCLEROPH>
 </ms:modis.ba.poly.season></gml:featureMember>
 <gml:featureMember><ms:modis.ba.poly.season gml:id="b">
  <ms:FIREDATE>2026-08-10 02:00:00</ms:FIREDATE><ms:PROVINCE>Algarve</ms:PROVINCE><ms:COMMUNE>Monchique</ms:COMMUNE><ms:AREA_HA>50</ms:AREA_HA>
 </ms:modis.ba.poly.season></gml:featureMember>
</wfs:FeatureCollection>"""


def test_wildfires_parse_and_totals(monkeypatch):
    from portugal_mcp import incendios

    class Resp:
        content = EFFIS_SAMPLE

        def raise_for_status(self):
            pass

    class Client:
        def get(self, *a, **k):
            return Resp()

    monkeypatch.setattr(incendios, "http", lambda: Client())
    incendios._mem.clear()
    r = incendios.wildfires()
    assert r["fires"] == 2 and r["burnt_hectares"] == 13247
    top = r["largest_or_latest"][0]
    assert top["region"] == "Viseu Dão Lafões" and top["land_cover_pct"]["forest"] == 48.0
    assert incendios.wildfires(place="monchique")["fires"] == 1
    assert "error" in incendios.wildfires(period="decade")
    assert "error" in incendios.wildfires(year=2001)


# --- live civil-protection incidents (ANEPC via Fogos.pt) ------------------------------------


def _fogos(monkeypatch, status=200, headers=None):
    import json
    from pathlib import Path

    from portugal_mcp import ocorrencias

    payload = json.loads((Path(__file__).parent / "fixtures" / "fogos_active.json").read_text(encoding="utf-8"))
    seen = {}

    class Resp:
        status_code = status

        def __init__(self):
            self.headers = headers or {}

        def json(self):
            return payload

        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

    class Client:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(url=url, params=params, headers=headers)
            return Resp()

    monkeypatch.setattr(ocorrencias, "http", lambda: Client())
    monkeypatch.setenv("FOGOS_API_KEY", "test-key")
    ocorrencias._mem = None
    return ocorrencias, seen


def test_incidents_near_point_sorted_with_distance(monkeypatch):
    o, seen = _fogos(monkeypatch)
    r = o.active_incidents(lat=41.2357, lon=-8.6199, radius_km=15)  # Maia
    assert seen["headers"]["X-API-Key"] == "test-key" and seen["params"] == {"all": 1}
    assert r["count"] >= 1
    d = [i["distance_km"] for i in r["incidents"]]
    assert d == sorted(d)
    first = r["incidents"][0]
    assert first["direction"] in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
    assert first["map"].startswith("https://www.google.com/maps?q=")
    assert "police" in r["note"].lower() and "112" in r["note"]


def test_incidents_kind_and_text_filters(monkeypatch):
    o, _ = _fogos(monkeypatch)
    road = o.active_incidents(kind="road")
    assert road["count"] >= 1 and all("rodovi" in k.lower() or "despiste" in k.lower() for k in road["by_kind"])
    monkeypatch.setattr(o, "_resolve", lambda near: None)  # a parish/village name: matched by location text
    txt = o.active_incidents(near="rio maior")
    assert txt["count"] == 1 and txt["incidents"][0]["what"] == "Mato"
    assert "error" in o.active_incidents(kind="nope")


def test_incidents_national_summary(monkeypatch):
    o, _ = _fogos(monkeypatch)
    r = o.active_incidents()
    assert r["count"] == 8 and r["by_district"] and len(r["biggest"]) <= 15


def test_incidents_need_api_key_and_handle_rate_limit(monkeypatch):
    o, _ = _fogos(monkeypatch)
    monkeypatch.delenv("FOGOS_API_KEY")
    assert "FOGOS_API_KEY" in o.active_incidents()["error"]
    o, _ = _fogos(monkeypatch, status=429, headers={"Retry-After": "600"})
    assert "10 min" in o.active_incidents()["error"]


def test_hosted_mode_moves_sync_tools_to_threads():
    """--hosted must not run blocking tools on the event loop (one slow download would stall every user)."""
    server.load(["geo"])
    server._run_sync_tools_in_threads(2)
    tool = server.mcp._tool_manager.get_tool("public_holidays")
    assert tool.is_async

    async def go():
        return await tool.run({"year": 2026})

    out = anyio.run(go)
    assert "2026-12-25" in json.dumps(out, default=str)


def test_dns_fallback_only_for_listed_hosts(monkeypatch):
    import socket as _socket

    from portugal_mcp import common

    real = common._orig_getaddrinfo

    def broken(host, *a, **k):
        if host in ("geo.turismodeportugal.pt", "other.example.pt"):
            raise _socket.gaierror(-5, "No address associated with hostname")
        return real(host, *a, **k)

    monkeypatch.setattr(common, "_orig_getaddrinfo", broken)
    monkeypatch.setattr(common, "_doh_lookup", lambda host: ["127.0.0.1"])
    assert _socket.getaddrinfo("geo.turismodeportugal.pt", 443)[0][4][0] == "127.0.0.1"
    with pytest.raises(_socket.gaierror):
        _socket.getaddrinfo("other.example.pt", 443)


def test_keep_warm_refreshes_in_the_background(monkeypatch):
    import threading

    from portugal_mcp import server

    ran = threading.Event()
    monkeypatch.setattr(server, "refresh", ran.set)
    server._keep_warm(6)
    assert ran.wait(5)


@pytest.fixture
def big_cache(monkeypatch, tmp_path):
    from portugal_mcp import common

    monkeypatch.setenv("PORTUGAL_MCP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(common.BigFileCache, "_mem", {})
    monkeypatch.setattr(common.BigFileCache, "_remote_modified", lambda self, url: "v1")
    return common


def test_big_files_build_one_at_a_time_and_once(big_cache):
    import threading

    running, peak, builds = [0], [0], []
    gate = threading.Lock()

    def build(url):
        with gate:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        builds.append(url)
        with gate:
            running[0] -= 1
        return [{"n": 1}]

    def call(name):
        big_cache.BigFileCache("t", name, f"https://x/{name}").get(build)

    threads = [threading.Thread(target=call, args=(n,)) for n in ("a", "a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1  # one big build at a time
    assert sorted(builds) == ["https://x/a", "https://x/b"]  # the second "a" reused the first


def test_big_files_keep_one_shared_copy_and_reload_from_disk(big_cache):
    c = big_cache.BigFileCache
    a = c("t", "big", "https://x/big").get(lambda u: [{"n": 1}, {"n": "é"}])
    b = c("t", "big", "https://x/big").get(lambda u: [{"n": 9}])
    assert a is b  # same parsed copy: no re-parse, no second copy in memory
    c._mem.clear()  # a restart: rows come back from disk without building
    again = c("t", "big", "https://x/big").get(lambda u: [{"n": 9}])
    assert again["data"] == [{"n": 1}, {"n": "é"}]


def test_background_refresh_rebuilds_big_files_before_expiry(big_cache, monkeypatch):
    c = big_cache.BigFileCache
    builds = []
    c("t", "c", "https://x/c", ttl=3600).get(lambda u: builds.append(u) or [1])
    with big_cache.refreshing_ahead(3000):
        c("t", "c", "https://x/c", ttl=3600).get(lambda u: builds.append(u) or [2])
    assert len(builds) == 1  # an hour left, 50 min ahead: still fresh
    with big_cache.refreshing_ahead(3700):
        doc = c("t", "c", "https://x/c", ttl=3600).get(lambda u: builds.append(u) or [2])
    assert len(builds) == 1 and doc["data"] == [1]  # upstream unchanged: kept, no download
    monkeypatch.setattr(big_cache.BigFileCache, "_remote_modified", lambda self, url: "v2")
    with big_cache.refreshing_ahead(3700):
        doc = c("t", "c", "https://x/c", ttl=3600).get(lambda u: builds.append(u) or [2])
    assert len(builds) == 2 and doc["data"] == [2]  # changed upstream: rebuilt now, not at expiry


def test_big_file_build_failure_serves_the_copy_on_disk(big_cache, monkeypatch):
    c = big_cache.BigFileCache
    c("t", "s", "https://x/s", ttl=0).get(lambda u: [1])
    monkeypatch.setattr(big_cache.BigFileCache, "_remote_modified", lambda self, url: "v2")

    def broken(url):
        raise RuntimeError("down")

    doc = c("t", "s", "https://x/s", ttl=0).get(broken)
    assert doc["data"] == [1] and doc["stale"] is True


def test_ev_status_served_from_cache_while_refreshing(monkeypatch):
    import threading

    from portugal_mcp.energia import ev

    d = ev.EVData()
    d.static = {"fetched_epoch": time.time(), "sites": []}
    d.status = {"fetched_epoch": time.time() - 20 * 60, "points": {}}  # 20 min old: past the 10-min TTL
    started, release = threading.Event(), threading.Event()

    def slow_load(progress):
        started.set()
        release.wait(5)
        d.status = {"fetched_epoch": time.time(), "points": {"x": 1}}

    monkeypatch.setattr(d, "_load_status", slow_load)
    t = time.time()
    d.ensure()
    assert time.time() - t < 1 and started.wait(2)  # answered at once, refresh running behind
    d.ensure()  # a second caller while the refresh runs: not blocked, no second download
    release.set()
    for _ in range(50):
        if not d._status_refreshing:
            break
        time.sleep(0.05)
    assert d.status["points"] == {"x": 1}


# --- municipal holidays (SAPO) and Arquivo.pt ------------------------------------------

SAPO_SAMPLE = """<GetAllHolidaysResponse xmlns="http://services.sapo.pt/Metadata/Holiday"><GetAllHolidaysResult>
<Holiday><Name>Ano Novo</Name><Date>2026-01-01T00:00:00</Date><Description/><Type>National</Type></Holiday>
<Holiday><Name>Feriado Municipal</Name><Date>2026-06-24T00:00:00</Date><Description/><Type>Municipal</Type>
 <Municipality><Id>0303</Id><Name>Braga</Name></Municipality></Holiday>
<Holiday><Name>Feriado Municipal</Name><Date>2026-08-21T00:00:00</Date><Description/><Type>Municipal</Type>
 <Municipality><Id>3103</Id><Name>Funchal</Name></Municipality></Holiday>
<Holiday><Name>Feriado Municipal</Name><Date>2026-04-22T00:00:00</Date><Description/><Type>Municipal</Type>
 <Municipality><Id>4203</Id><Name>Ponta Delgada</Name></Municipality></Holiday>
<Holiday><Name>Segunda-Feira do Espírito Santo</Name><Date>2026-05-25T00:00:00</Date><Description/><Type>Regional</Type></Holiday>
<Holiday><Name>Feriado Regional da Região Autónoma da Madeira</Name><Date>2026-12-26T00:00:00</Date><Description/><Type>Regional</Type></Holiday>
</GetAllHolidaysResult></GetAllHolidaysResponse>"""


def test_sapo_holidays_parse_and_regions():
    rows = geo.parse_sapo_holidays(SAPO_SAMPLE)
    assert len(rows) == 5 and all(r["type"] in ("municipal", "regional") for r in rows)
    assert [r["date"] for r in geo.local_holidays_for(rows, "0303")] == ["2026-06-24"]
    assert {r["date"] for r in geo.local_holidays_for(rows, "3103")} == {"2026-08-21", "2026-12-26"}
    assert {r["date"] for r in geo.local_holidays_for(rows, "4203")} == {"2026-04-22", "2026-05-25"}


def test_public_holidays_with_municipality(monkeypatch):
    munis = [{"name": "Braga", "dico": "0303", "district": "Braga", "parishes": []},
             {"name": "Funchal", "dico": "3103", "district": "Ilha da Madeira", "parishes": []}]
    monkeypatch.setattr(geo, "_municipalities", lambda: munis)
    monkeypatch.setattr(geo, "_local_holidays", lambda y: geo.parse_sapo_holidays(SAPO_SAMPLE))
    r = geo.public_holidays(2026, municipality="braga")
    local = [h for h in r["holidays"] if h["type"] in ("municipal", "regional")]
    assert r["municipality"]["ine_code"] == "0303" and [h["date"] for h in local] == ["2026-06-24"]
    assert local[0]["weekday"] == "Wednesday" and "SAPO" in r["source"]
    funchal = {h["date"] for h in geo.public_holidays(2026, municipality="Funchal")["holidays"] if h["type"] == "regional"}
    assert funchal == {"2026-07-01", "2026-12-26"}  # Madeira Day added, SAPO only lists 26 December

    def down(y):
        raise OSError("down")

    monkeypatch.setattr(geo, "_local_holidays", down)
    r = geo.public_holidays(2026, municipality="Braga")
    assert "warning" in r and all(h["type"] != "municipal" for h in r["holidays"])
    assert "error" in geo.public_holidays(2026, municipality="Atlantis")


def test_arquivo_dates_and_page_text():
    from portugal_mcp import arquivo

    assert arquivo._ts("2015") == "20150101000000" and arquivo._ts("2015-06", end=True) == "20150630235959"
    assert arquivo._ts(2010, end=True) == "20101231235959"
    with pytest.raises(ValueError):
        arquivo._ts("2015-6")
    title, text = arquivo.page_text("<html><head><title>Câmara &amp; Cidade</title><style>x{}</style></head>"
                                    "<body><script>var a=1</script><p>Olá</p><div>Mundo  &nbsp; aqui</div></body></html>")
    assert title == "Câmara & Cidade" and text == "Olá\nMundo aqui"


def test_arquivo_history_summarises_versions(monkeypatch):
    from portugal_mcp import arquivo

    caps = [{"timestamp": ts, "url": "http://x.pt/", "digest": d}
            for ts, d in [("20040101000000", "a"), ("20040201000000", "a"), ("20100101000000", "b"), ("20200101000000", "c")]]
    monkeypatch.setattr(arquivo, "_cdx", lambda params: caps)
    r = arquivo.web_archive_history("x.pt", max_versions=2)
    assert r["captures"] == 4 and r["distinct_contents"] == 3
    assert r["captures_per_year"] == {"2004": 2, "2010": 1, "2020": 1}
    assert [v["date"][:4] for v in r["sample_versions"]] == ["2004", "2020"]
    assert r["first"]["archive_link"] == "https://arquivo.pt/wayback/20040101000000/http://x.pt/"
    monkeypatch.setattr(arquivo, "_cdx", lambda params: [])
    assert arquivo.web_archive_history("x.pt")["captures"] == 0


# --- Porto city data, Lisbon Metro status, E-REDES grid and local ---------------------

PLANNED_CSV = """'tipo_de_condicionamento','troco','motivo','data_de_inicio','data_de_fim','observacoes','data_de_registo','latitude','longitude','objectid','topo','esriGeometryPolygon'
'Corte total','entre o n.º 1 e o n.º 9','Obras públicas','2026-11-10','2026-11-20','','2026-11-01','41.15','-8.61',1,'Rua de Santa Catarina','[]'
'Estacionamento','','Eventos e lazer','2026-01-01','2026-01-02','','2025-12-01','41.16','-8.62',2,'Rua Antiga','[]'
'Estreitamento','','Cargas e descargas','','','','','41.15','-8.61',3,'Rua Sem Datas','[]'
'Estreitamento','','Outro motivo','2026-11-10','2026-11-30','TESTE-PLAYWRIGHT-obs-1','','41.15','-8.61',4,'Rua Teste','[]'
"""
UNPLANNED_CSV = """'motivo','arruamento','troco','entidade','tipo_de_condicionamento','data_de_início','hora_de_inicio','data_de_fim','hora_de_fim','observacoes','registo_data','coordenada_x','coordenada_y','registo_hora','ultima_modificacao_data','ultima_modificacao_hora','objectid','esriGeometryPoint'
'Rotura de conduta','Rua do Bonjardim','','','Corte total',1794873600000,32400000,1795046400000,64800000,'',1794873600000,-8.6105,41.1502,0,0,0,1,'0,0'
"""


def test_porto_closures_parse_and_filter():
    import io

    from portugal_mcp import porto

    keep = date(2026, 11, 12)
    planned = porto.parse_planned(io.StringIO(PLANNED_CSV), keep)
    assert [r["street"] for r in planned] == ["Rua de Santa Catarina"]  # old, undated and test rows dropped
    unplanned = porto.parse_unplanned(io.StringIO(UNPLANNED_CSV), keep)
    assert unplanned[0]["start"] == "2026-11-17" and unplanned[0]["start_time"] == "09:00" and unplanned[0]["end_time"] == "18:00"
    rows = planned + unplanned
    assert len(porto.active_on(rows, date(2026, 11, 18))) == 2
    assert len(porto.active_on(rows, date(2026, 11, 14))) == 1


def test_porto_road_closures_tool(monkeypatch):
    import io

    from portugal_mcp import porto

    rows = porto.parse_planned(io.StringIO(PLANNED_CSV), date(2026, 1, 1)) + porto.parse_unplanned(io.StringIO(UNPLANNED_CSV), date(2026, 1, 1))
    monkeypatch.setattr(porto, "_all_closures", lambda: rows)
    r = porto.porto_road_closures(near="santa catarina", day="2026-11-18")
    assert r["count"] == 1 and r["restrictions"][0]["reason"] == "Obras públicas"
    r = porto.porto_road_closures(lat=41.1502, lon=-8.6105, radius_km=0.2, day="2026-11-18")
    assert r["restrictions"][0]["street"] == "Rua do Bonjardim" and r["restrictions"][0]["distance_m"] == 0
    r = porto.porto_road_closures(day="2026-11-18", kind="unplanned")
    assert r["count"] == 1 and r["by_reason"] == {"Rotura de conduta": 1}
    assert "error" in porto.porto_road_closures(kind="nope")


def test_porto_sensors_keep_fresh_readings_only():
    from datetime import datetime, timezone

    from portugal_mcp import porto

    now = datetime(2026, 11, 13, 22, 30, tzinfo=timezone.utc)
    ents = [
        {"id": "a", "name": "Aliados", "dateObserved": "2026-11-13T22:15:00.00Z", "location": {"coordinates": [-8.61, 41.15]}, "pm25": 7, "no2": 30.5, "junk": "x"},
        {"id": "b", "name": "Old", "dateObserved": "2022-11-18T14:40:00.00Z", "location": {"coordinates": [-8.6, 41.1]}, "pm25": 9},
    ]
    rows, stale = porto.shape_sensors("air", ents, now)
    assert stale == 1 and rows[0]["sensor"] == "Aliados" and rows[0]["readings"] == {"pm25": 7, "no2": 30.5}
    assert rows[0]["observed"] == "2026-11-13 22:15"


def test_lisbon_metro_status_shape():
    from portugal_mcp import lisboa

    rows = lisboa.shape_metro({"resposta": {"amarela": " Ok", "azul": "Circulação interrompida entre Baixa-Chiado e Santa Apolónia", "verde": " Ok", "vermelha": " Ok"}, "codigo": "200"})
    assert [r["normal_service"] for r in rows] == [True, False, True, True]
    assert rows[1]["status"].startswith("Circulação interrompida")


def test_electricity_grid_shape():
    from portugal_mcp import eletricidade as e

    assert e.latest_complete_day({"date": "2026-11-12", "time": "00:00"}) == date(2026, 11, 11)
    assert e.latest_complete_day({"date": "2026-11-12", "time": "23:45"}) == date(2026, 11, 12)
    out = e.shape_grid(
        date(2026, 11, 11),
        {"total": 154448053.9, "bt": 79149439.8, "mt": 49548152.5, "at": 19689836.3, "mat": 6060625.2, "peak": 1924988.98, "n": 96},
        {"dgm": 123896577.0, "pre": 30551477.0},
        {"wind": 24048152.0, "solar": 50031.0, "hydro": 643958.0, "cogen": 2518241.0, "other": 3291095.0, "total": 30551477.0},
    )
    assert out["consumption_gwh"] == 154.45 and out["peak_mw"] == 7700
    assert out["renewable_share_of_distribution_generation"] == 0.81 and "warning" not in out
    assert "warning" in e.shape_grid(date(2026, 11, 11), {"total": 1, "n": 40}, {}, {})


def test_electricity_local(monkeypatch):
    from portugal_mcp import eletricidade as e

    monkeypatch.setattr(geo, "_municipalities", lambda: [
        {"name": "Sintra", "dico": "1111", "district": "Lisboa", "parishes": []},
        {"name": "Funchal", "dico": "3103", "district": "Ilha da Madeira", "parishes": []},
    ])
    calls = []

    def ods(dataset, **params):
        calls.append(dataset)
        if dataset == e.DS_MUNI:
            return [{"data": "2026-07-01T00:00:00+00:00", "kwh": 83164144.9}]
        if dataset == e.DS_QOS and "codigo_concelho" in params.get("where", ""):
            return [{"ano": "2025", "saidi_bt_min": 160.96, "saifi_bt_num": 1.3236, "maifi_mt_num": 2.8}, {"ano": "2024", "saidi_bt_min": 145.5, "saifi_bt_num": 1.47}]
        if dataset == e.DS_QOS:
            return [{"saidi": 92.24, "saifi": 1.9158}]
        return []

    monkeypatch.setattr(e, "_ods", ods)
    r = e.electricity_local(municipality="Sintra")
    assert r["consumption_mwh_by_month"] == [{"month": "2026-07", "mwh": 83164.1}]
    po = r["power_outages"]
    assert po["minutes_without_power_per_customer"] == 161.0 and po["mainland_municipality_average"]["minutes_without_power_per_customer"] == 92.2
    assert po["previous_years"][0]["year"] == "2024"
    calls.clear()
    assert "warning" in e.electricity_local(municipality="Funchal") and calls == []
    assert "error" in e.electricity_local()
