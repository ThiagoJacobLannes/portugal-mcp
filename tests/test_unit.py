"""Offline tests: parsing, privacy masking, safety checks, server wiring. No network."""

import base64
import io
import json
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
