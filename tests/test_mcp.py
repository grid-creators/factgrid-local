"""
End-to-End-Test der MCP-Tools gegen einen rdflib-Mock-Endpunkt, der den vom Konverter
erzeugten Graphen hält. Prüft Prefix-Injektion, Label-Service-Umschreibung, Suche, get_entity.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "mcp"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import mock_sparql  # noqa: E402
import test_wb2rdf as fixture  # noqa: E402

# Server-Module erst nach dem Setzen der Umgebung importieren
_graph, _ttl = fixture.convert([fixture.ITEM, fixture.PROP, *fixture.OTHER_PROPS])
_srv, _url = mock_sparql.serve(_graph)
os.environ["QLEVER_ENDPOINT"] = _url
os.environ["FACTGRID_CACHE_DIR"] = os.path.join(HERE, ".cache-test")

from factgrid_mcp import qlever, server  # noqa: E402
from factgrid_mcp.labelservice import rewrite_label_service  # noqa: E402
from factgrid_mcp.prefixes import inject_prefixes  # noqa: E402

qlever.ENDPOINT = _url  # Modul wurde evtl. schon früher importiert


def tool(t):
    """fastmcp 2.x kapselt Tools in FunctionTool(.fn), 3.x gibt die Funktion zurück."""
    return getattr(t, "fn", t)



def test_rewrite_implicit():
    q = """SELECT ?item ?itemLabel ?itemDescription WHERE {
  SERVICE wikibase:label { bd:serviceParam wikibase:language "[AUTO_LANGUAGE],en". }
  ?item wdt:P2 wd:Q7 .
} LIMIT 10"""
    out = rewrite_label_service(q, "de")
    assert "SERVICE" not in out
    assert 'OPTIONAL { ?item rdfs:label ?itemLabel__de . FILTER(LANG(?itemLabel__de) = "de") }' in out
    assert "BIND(COALESCE(?itemLabel__de, ?itemLabel__en, IF(isIRI(?item)" in out
    assert "BIND(COALESCE(?itemDescription__de, ?itemDescription__en) AS ?itemDescription)" in out
    # Muster stehen NACH dem Tripelmuster (Left-Join-Reihenfolge)
    assert out.index("?item wdt:P2 wd:Q7") < out.index("OPTIONAL { ?item rdfs:label")


def test_rewrite_explicit_and_subquery():
    q = """SELECT ?n ?name WHERE {
  { SELECT ?x (COUNT(*) AS ?n) WHERE { ?x wdt:P2 wd:Q7 } GROUP BY ?x }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". ?x rdfs:label ?name . }
}"""
    out = rewrite_label_service(q)
    assert "SERVICE" not in out
    assert 'OPTIONAL { ?x rdfs:label ?name__en . FILTER(LANG(?name__en) = "en") }' in out
    assert 'BIND(COALESCE(?name__en, IF(isIRI(?x), STRAFTER(STR(?x), "/entity/"), STR(?x))) AS ?name)' in out


def test_rewrite_scoping_union_minus_optional():
    q = """SELECT ?p ?pLabel ?o ?oLabel ?d ?dLabel WHERE {
  { ?p wdt:P2 wd:Q7 } UNION { ?p wdt:P2 wd:Q8 }
  OPTIONAL { ?p wdt:P68 ?o }
  MINUS { ?p wdt:P38 ?d }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "de". }
}"""
    out = rewrite_label_service(q)
    # ?pLabel in beiden UNION-Zweigen, ?oLabel im OPTIONAL, ?dLabel außerhalb von MINUS
    assert out.count("OPTIONAL { ?p rdfs:label ?pLabel__de") == 2
    assert out.index("?p wdt:P68 ?o") < out.index("OPTIONAL { ?o rdfs:label") < out.index("MINUS")
    assert "?d rdfs:label" not in out  # ?d ist nur in MINUS gebunden → kein Label-Muster (kein Kreuzprodukt)
    from rdflib.plugins.sparql import prepareQuery
    prepareQuery(inject_prefixes(out))  # muss parsen


def test_rewrite_subquery_projection():
    q = """SELECT ?x ?xLabel ?n WHERE {
  { SELECT ?x (COUNT(*) AS ?n) WHERE { ?x wdt:P2 wd:Q7 . ?x ?p ?o } GROUP BY ?x }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "de". }
} ORDER BY DESC(?n)"""
    out = rewrite_label_service(q)
    # Bindung kommt aus der Subquery-Projektion → Muster am Ende der äußeren Gruppe
    assert out.rstrip().endswith("} ORDER BY DESC(?n)") and out.count("?x rdfs:label ?xLabel__de") == 1
    assert out.index("GROUP BY ?x }") < out.index("OPTIONAL { ?x rdfs:label")


def test_prepare_comments_limit_and_guard():
    from factgrid_mcp.qlever import prepare, QLeverError
    q = prepare('# Kopf\nSELECT ?x WHERE { ?x wdt:P2 wd:Q7 } # Ende\nLIMIT 10 OFFSET 5', 50)
    assert q.count("LIMIT") == 1 and "OFFSET 5" in q and "#" not in q
    q = prepare('SELECT ?x WHERE { ?x wdt:P2 wd:Q7 } VALUES ?x { wd:Q42 }', 5)
    assert q.index("LIMIT 6") < q.index("VALUES")
    q = prepare('SELECT ?x WHERE { ?x rdfs:label "a # b" }', 5)
    assert '"a # b"' in q
    for bad in ["# x\nINSERT DATA { wd:Q1 wdt:P2 wd:Q7 }", "BASE <http://x/>\nDELETE WHERE { ?s ?p ?o }", "CLEAR ALL"]:
        try:
            prepare(bad, 5)
            assert False, bad
        except QLeverError:
            pass
    prepare('SELECT ?add WHERE { ?add rdfs:label "insert" }', 5)  # kein Fehlalarm


def test_inject_prefixes():
    q = "SELECT * WHERE { ?x wdt:P2 wd:Q7 ; rdfs:label ?l } LIMIT 1"
    out = inject_prefixes(q)
    assert out.startswith("PREFIX rdfs:") or out.startswith("PREFIX wd")
    assert "PREFIX wdt: <https://database.factgrid.de/prop/direct/>" in out
    assert "PREFIX p:" not in out  # nicht benutzt → nicht injiziert
    assert inject_prefixes(out) == out  # idempotent


def test_sparql_tool_with_label_service():
    q = """SELECT ?item ?itemLabel WHERE {
  SERVICE wikibase:label { bd:serviceParam wikibase:language "[AUTO_LANGUAGE],en". }
  ?item wdt:P2 wd:Q7 .
}"""
    out = tool(server.sparql)(q, limit=10)
    assert "wd:Q42" in out and 'Johann "Test" Bach@de' in out, out


def test_sparql_tool_error_is_returned():
    out = tool(server.sparql)("SELECT ?x WHERE { ?x wdt:P2 wd:Q7 ", limit=5)
    assert out.startswith("FEHLER"), out
    out = tool(server.sparql)("DELETE WHERE { ?s ?p ?o }", limit=5)
    assert "Nur lesende" in out


def test_search_entities():
    out = tool(server.search_entities)("Johann", lang="de")
    assert "wd:Q42" in out and "Johann" in out, out
    out = tool(server.search_entities)("bach", lang="de")  # Teilstring, klein geschrieben
    assert "wd:Q42" in out, out
    out = tool(server.search_entities)("Geburtsdatum", entity_type="property")
    assert "wd:P77" in out, out
    assert tool(server.search_entities)("Gibtsnicht") == "Keine Treffer."


def test_get_entity_item_and_property():
    out = tool(server.get_entity)("Q42")
    assert out.startswith('Q42: Johann "Test" Bach'), out
    assert "wd:P77 Geburtsdatum: 1685-03-31T00:00:00" in out and "[PreferredRank]" in out, out
    assert "laut Taufregister" in out
    assert "wd:P2" in out and "wd:Q7" in out
    out = tool(server.get_entity)("P77")
    assert "Datentyp: Time" in out and "Verwendungen (wdt:P77): 1" in out, out


def test_get_entity_skips_external_ids():
    out = tool(server.get_entity)("Q42")
    assert "wd:P76" not in out, out                                   # GND (external-id) per Default weg
    assert "wd:P76" in tool(server.get_entity)("Q42", include_external_ids=True)


def test_get_statement_values():
    out = tool(server.get_statement_values)("Q42", "P77")
    assert out.startswith("Q42 Johann") and "wd:P77 Geburtsdatum: 3 Statement(s)" in out, out
    assert "[Preferred]" in out and "[Deprecated]" in out, out
    assert "Qualifikator wd:P90 Anmerkung: laut Taufregister" in out, out
    # Wertknoten des julianischen Datums: Präzision und Kalendermodell
    assert "Wertknoten: wikibase:timePrecision = 11" in out, out
    assert "wikibase:timeCalendarModel" in out, out
    out = tool(server.get_statement_values)("Q42", "P2")
    assert "Referenz wd:P100 Online-Quelle: <https://example.org/quelle?x=1&y=2>" in out, out
    assert "keine Statements" in tool(server.get_statement_values)("Q42", "P90")
    assert tool(server.get_statement_values)("Q42", "Q7").startswith("Erwartet")


def test_get_property_hierarchy():
    out = tool(server.get_property_hierarchy)("Q7", "P2")            # was ist ein Mensch → Q42
    assert "wd:Q7" in out and "└ Johann \"Test\" Bach (wd:Q42)" in out, out
    out = tool(server.get_property_hierarchy)("Q42", "P2", direction="up")
    assert "└ Q7 (wd:Q7)" in out, out                                # Q7 hat kein Label im Fixture
    assert "keine verbundenen" in tool(server.get_property_hierarchy)("Q42", "P2"), out


def test_get_wikibase_info():
    out = tool(server.get_wikibase_info)(refresh=True)
    assert "Instanz-Property: wdt:P2" in out and "Datenstand" in out, out
    assert "Items:            1" in out and "Properties:       11" in out, out


def test_schema_overview():
    out = tool(server.schema_overview)(refresh=True)
    assert "wd:Q7" in out and "wd:P77" in out and "Time" in out, out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    _srv.shutdown()
