"""
Tests für scripts/wb2rdf.py – hand-gebaute Entität, die alle Datentypen abdeckt.
Ausführen:  python3 -m pytest tests/  (oder: python3 tests/test_wb2rdf.py)
"""
import json
import os
import subprocess
import sys
import tempfile

import rdflib
from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import wb2rdf  # noqa: E402

BASE = "https://database.factgrid.de/"
WD = Namespace(BASE + "entity/")
WDT = Namespace(BASE + "prop/direct/")
P = Namespace(BASE + "prop/")
PS = Namespace(BASE + "prop/statement/")
PSV = Namespace(BASE + "prop/statement/value/")
PQ = Namespace(BASE + "prop/qualifier/")
PR = Namespace(BASE + "prop/reference/")
WDNO = Namespace(BASE + "prop/novalue/")
S = Namespace(BASE + "entity/statement/")
REF = Namespace(BASE + "reference/")
WIKIBASE = Namespace("http://wikiba.se/ontology#")
SCHEMA = Namespace("http://schema.org/")
PROV = Namespace("http://www.w3.org/ns/prov#")
XSD = Namespace("http://www.w3.org/2001/XMLSchema#")
GEO = Namespace("http://www.opengis.net/ont/geosparql#")


def snak(prop, dtype, vtype, value, snaktype="value"):
    s = {"snaktype": snaktype, "property": prop, "datatype": dtype}
    if snaktype == "value":
        s["datavalue"] = {"type": vtype, "value": value}
    return s


def statement(guid, mainsnak, rank="normal", qualifiers=None, references=None):
    st = {"id": guid, "type": "statement", "rank": rank, "mainsnak": mainsnak}
    if qualifiers:
        st["qualifiers"] = qualifiers
    if references:
        st["references"] = references
    return st


ITEM = {
    "type": "item",
    "id": "Q42",
    "labels": {"de": {"language": "de", "value": "Johann \"Test\" Bach"}, "en": {"language": "en", "value": "Johann Test Bach"}},
    "descriptions": {"de": {"language": "de", "value": "Testperson\nmit Zeilenumbruch"}},
    "aliases": {"de": [{"language": "de", "value": "J. T. Bach"}]},
    "lastrevid": 123456,
    "modified": "2026-08-30T21:00:00Z",
    "sitelinks": {"dewiki": {"site": "dewiki", "title": "Johann Test Bach", "badges": []}},
    "claims": {
        "P2": [
            statement("Q42$aaa-1", snak("P2", "wikibase-item", "wikibase-entityid", {"entity-type": "item", "id": "Q7"}),
                      references=[{"hash": "abc123", "snaks": {"P100": [snak("P100", "url", "string", "https://example.org/quelle?x=1&y=2")]}}]),
        ],
        "P77": [  # Geburtsdatum: julianisch, tagesgenau; plus deprecated + preferred
            statement("Q42$t-1", snak("P77", "time", "time", {"time": "+1685-03-21T00:00:00Z", "precision": 11, "timezone": 0, "calendarmodel": wb2rdf.JULIAN}), rank="preferred",
                      qualifiers={"P90": [snak("P90", "string", "string", "laut Taufregister")]}),
            statement("Q42$t-2", snak("P77", "time", "time", {"time": "+1685-00-00T00:00:00Z", "precision": 9, "timezone": 0, "calendarmodel": wb2rdf.GREGORIAN}), rank="normal"),
            statement("Q42$t-3", snak("P77", "time", "time", {"time": "+1600-00-00T00:00:00Z", "precision": 9, "timezone": 0, "calendarmodel": wb2rdf.GREGORIAN}), rank="deprecated"),
        ],
        "P48": [statement("Q42$g-1", snak("P48", "globe-coordinate", "globecoordinate", {"latitude": 50.98, "longitude": 11.03, "precision": 0.0001, "globe": wb2rdf.EARTH}))],
        "P55": [statement("Q42$q-1", snak("P55", "quantity", "quantity", {"amount": "+12.5", "unit": "1"}))],
        "P66": [statement("Q42$m-1", snak("P66", "monolingualtext", "monolingualtext", {"text": "Kantor", "language": "de"}))],
        "P67": [statement("Q42$s-1", snak("P67", "string", "string", "x"), ), statement("Q42$s-2", snak("P67", "string", "string", None, snaktype="somevalue"))],
        "P68": [statement("Q42$n-1", snak("P68", "wikibase-item", "wikibase-entityid", None, snaktype="novalue"))],
        "P76": [statement("Q42$e-1", snak("P76", "external-id", "string", "118505696"))],
        "P99": [statement("Q42$c-1", snak("P99", "commonsMedia", "string", "Bach Seal.jpg"))],
    },
}

PROP = {
    "type": "property", "id": "P77", "datatype": "time",
    "labels": {"de": {"language": "de", "value": "Geburtsdatum"}}, "claims": {}, "lastrevid": 1,
}

# Übrige Properties des Fixtures (im echten Dump sind alle Properties enthalten)
OTHER_PROPS = [
    {"type": "property", "id": pid, "datatype": dt, "claims": {},
     "labels": {"de": {"language": "de", "value": label}}}
    for pid, dt, label in [
        ("P2", "wikibase-item", "Ist ein(e)"), ("P90", "string", "Anmerkung"),
        ("P48", "globe-coordinate", "Koordinaten"), ("P55", "quantity", "Anzahl"),
        ("P66", "monolingualtext", "Bezeichnung"), ("P67", "string", "Notiz"),
        ("P68", "wikibase-item", "Vater"), ("P76", "external-id", "GND"),
        ("P99", "commonsMedia", "Bild"), ("P100", "url", "Online-Quelle"),
    ]
]


def convert(entities, extra_args=()):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write("[\n" + ",\n".join(json.dumps(e, ensure_ascii=False) for e in entities) + "\n]\n")
        path = fh.name
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "wb2rdf.py"), path, "--workers", "1", *extra_args],
                         capture_output=True, text=True, check=True).stdout
    os.unlink(path)
    g = rdflib.Graph()
    g.parse(data=out, format="turtle")
    return g, out


def test_full_graph():
    g, ttl = convert([ITEM, PROP])
    q = WD.Q42
    assert (q, RDF.type, WIKIBASE.Item) in g
    assert (q, RDFS.label, Literal('Johann "Test" Bach', lang="de")) in g
    assert (q, SCHEMA.description, Literal("Testperson\nmit Zeilenumbruch", lang="de")) in g
    assert (q, SKOS.altLabel, Literal("J. T. Bach", lang="de")) in g
    # truthy
    assert (q, WDT.P2, WD.Q7) in g
    # Ranking: nur preferred wird truthy, deprecated nie
    truthy_dates = set(g.objects(q, WDT.P77))
    assert truthy_dates == {Literal("1685-03-31T00:00:00Z", datatype=XSD.dateTime)}, truthy_dates  # julianisch→gregorianisch (+10 Tage)
    # volles Statement mit BestRank / Rank / Qualifier / Referenz
    st = S["Q42-t-1"]
    assert (q, P.P77, st) in g
    assert (st, RDF.type, WIKIBASE.Statement) in g and (st, RDF.type, WIKIBASE.BestRank) in g
    assert (st, WIKIBASE.rank, WIKIBASE.PreferredRank) in g
    assert (st, PQ.P90, Literal("laut Taufregister")) in g
    dep = S["Q42-t-3"]
    assert (dep, WIKIBASE.rank, WIKIBASE.DeprecatedRank) in g and (dep, RDF.type, WIKIBASE.BestRank) not in g
    # Wertknoten Zeit
    vn = next(g.objects(st, PSV.P77))
    assert (vn, RDF.type, WIKIBASE.TimeValue) in g
    assert (vn, WIKIBASE.timePrecision, Literal("11", datatype=XSD.integer)) in g
    assert (vn, WIKIBASE.timeCalendarModel, URIRef(wb2rdf.JULIAN)) in g
    # Jahrespräzision → 01-01
    assert (S["Q42-t-2"], PS.P77, Literal("1685-01-01T00:00:00Z", datatype=XSD.dateTime)) in g
    # Referenz
    assert (S["Q42-aaa-1"], PROV.wasDerivedFrom, REF.abc123) in g
    assert (REF.abc123, PR.P100, URIRef("https://example.org/quelle?x=1&y=2")) in g
    # Koordinaten / Quantity / Monolingual / Commons / External-ID
    assert (q, WDT.P48, Literal("Point(11.03 50.98)", datatype=GEO.wktLiteral)) in g
    assert (q, WDT.P55, Literal("12.5", datatype=XSD.decimal)) in g
    qn = next(g.objects(S["Q42-q-1"], PSV.P55))
    assert (qn, WIKIBASE.quantityUnit, URIRef(wb2rdf.UNITLESS)) in g
    assert (q, WDT.P66, Literal("Kantor", lang="de")) in g
    assert (q, WDT.P99, URIRef("http://commons.wikimedia.org/wiki/Special:FilePath/Bach%20Seal.jpg")) in g
    assert (q, WDT.P76, Literal("118505696")) in g
    # somevalue → Blank Node, novalue → rdf:type wdno:
    assert any(isinstance(o, rdflib.BNode) for o in g.objects(q, WDT.P67))
    assert (q, RDF.type, WDNO.P68) in g
    assert (S["Q42-n-1"], RDF.type, WDNO.P68) in g
    # Metadaten
    assert (q, WIKIBASE.statements, Literal("12", datatype=XSD.integer)) in g
    assert (q, WIKIBASE.identifiers, Literal("1", datatype=XSD.integer)) in g
    assert (q, WIKIBASE.sitelinks, Literal("1", datatype=XSD.integer)) in g
    page = URIRef("https://de.wikipedia.org/wiki/Johann_Test_Bach")
    assert (page, SCHEMA.about, q) in g
    # Property-Definition
    assert (WD.P77, RDF.type, WIKIBASE.Property) in g
    assert (WD.P77, WIKIBASE.propertyType, WIKIBASE.Time) in g
    assert (WD.P77, WIKIBASE.directClaim, WDT.P77) in g
    assert (WD.P77, WIKIBASE.claim, P.P77) in g


def test_truthy_flavor():
    g, ttl = convert([ITEM], ["--flavor", "truthy", "--no-sitelinks"])
    assert (WD.Q42, WDT.P2, WD.Q7) in g
    assert not list(g.triples((None, P.P2, None)))
    assert not list(g.triples((None, RDF.type, WIKIBASE.Statement)))


def test_time_normalization():
    G = wb2rdf.GREGORIAN
    tl = lambda t, p, cal=G: wb2rdf.time_literal({"time": t, "precision": p, "calendarmodel": cal})
    assert tl("+1750-06-15T00:00:00Z", 9) == '"1750-01-01T00:00:00Z"^^xsd:dateTime'   # Jahr → 01-01
    assert tl("+1750-06-15T00:00:00Z", 10) == '"1750-06-01T00:00:00Z"^^xsd:dateTime'  # Monat → Tag 01
    assert tl("+1700-02-30T00:00:00Z", 11) == '"1700-02-28T00:00:00Z"^^xsd:dateTime'  # Tag geklemmt
    assert tl("-0100-01-01T00:00:00Z", 9) == '"-0099-01-01T00:00:00Z"^^xsd:dateTime'  # XSD-Verschiebung
    assert tl("-0001-01-01T00:00:00Z", 9) == '"0000-01-01T00:00:00Z"^^xsd:dateTime'
    assert tl("+1582-10-04T00:00:00Z", 11, wb2rdf.JULIAN) == '"1582-10-14T00:00:00Z"^^xsd:dateTime'


def test_julian_conversion():
    assert wb2rdf._julian_to_gregorian(1582, 10, 5) == (1582, 10, 15)
    assert wb2rdf._julian_to_gregorian(1700, 2, 19) == (1700, 3, 1)
    assert wb2rdf._julian_to_gregorian(1600, 1, 1) == (1600, 1, 11)


if __name__ == "__main__":
    test_full_graph()
    test_truthy_flavor()
    test_julian_conversion()
    print("OK")
