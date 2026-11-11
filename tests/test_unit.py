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
