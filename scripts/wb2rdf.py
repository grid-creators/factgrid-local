#!/usr/bin/env python3
"""
wb2rdf.py – Streaming-Konverter: Wikibase-JSON-Dump  →  Turtle im Wikibase-RDF-Dump-Format

Ziel: Ein FactGrid-JSON-Dump (https://database.factgrid.de/dumps/YYYY-MM-DD.json.gz)
wird in Turtle umgewandelt, das denselben Graphen beschreibt, den Wikibase selbst
(dumpRdf.php / Special:EntityData/Qn.ttl) erzeugen würde. Dadurch laufen die
SPARQL-Abfragen aus dem FactGrid-Query-Service unverändert (bis auf den
Label-Service, siehe mcp/factgrid_mcp/labelservice.py) gegen den lokalen QLever.

Abgebildet werden:
  * Items/Properties mit rdfs:label, schema:description, skos:altLabel
  * Truthy-Statements  (wdt:)  – nur Best-Rank, keine "deprecated"
  * Volle Statements   (p:/ps:/psv:, wikibase:rank, wikibase:BestRank)
  * Qualifier (pq:/pqv:) und Referenzen (prov:wasDerivedFrom, pr:/prv:)
  * Wertknoten für time / quantity / globe-coordinate (wikibase:TimeValue …)
  * somevalue (Blank Node) und novalue (rdf:type wdno:P…)
  * Property-Definitionen (wikibase:propertyType, directClaim, claim, …)
  * Sitelinks (schema:Article) – optional
  * Metadaten: wikibase:statements, wikibase:sitelinks, wikibase:identifiers,
    schema:dateModified, schema:version (lastrevid)

Bewusst weggelassen (selten abgefragt, spart Tripel):
  * Normalisierte Werte (wdtn:/psn:/pqn:/prn:)
  * OWL-Definitionen der wdno:-Klassen
  * skos:prefLabel / schema:name als Dubletten von rdfs:label (per --full-labels zuschaltbar)

Aufruf:
  python3 wb2rdf.py DUMP.json.gz | pigz > factgrid.ttl.gz
  python3 wb2rdf.py DUMP.json.gz --flavor truthy --workers 8 -o factgrid-truthy.ttl
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import multiprocessing as mp
import os
import re
import sys
from typing import Iterator
from urllib.parse import quote

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

DEFAULT_BASE = "https://database.factgrid.de/"

# Kalendermodelle / Globen, wie Wikibase sie in JSON referenziert (immer Wikidata-IRIs)
GREGORIAN = "http://www.wikidata.org/entity/Q1985727"
JULIAN = "http://www.wikidata.org/entity/Q1985786"
EARTH = "http://www.wikidata.org/entity/Q2"
UNITLESS = "http://www.wikidata.org/entity/Q199"  # Wikibase-Konvention für Einheit "1"

COMMONS_FILEPATH = "http://commons.wikimedia.org/wiki/Special:FilePath/"
COMMONS_DATA = "http://commons.wikimedia.org/data/main/"

# Datentyp → wikibase:propertyType
PROPERTY_TYPES = {
    "wikibase-item": "WikibaseItem",
    "wikibase-property": "WikibaseProperty",
    "string": "String",
    "external-id": "ExternalId",
    "url": "Url",
    "time": "Time",
    "quantity": "Quantity",
    "globe-coordinate": "GlobeCoordinate",
    "monolingualtext": "Monolingualtext",
    "commonsMedia": "CommonsMedia",
    "geo-shape": "GeoShape",
    "tabular-data": "TabularData",
    "math": "Math",
    "musical-notation": "MusicalNotation",
    "wikibase-lexeme": "WikibaseLexeme",
    "wikibase-form": "WikibaseForm",
    "wikibase-sense": "WikibaseSense",
    "edtf": "Edtf",
    "entity-schema": "EntitySchema",
}

RANK_IRI = {
    "normal": "wikibase:NormalRank",
    "preferred": "wikibase:PreferredRank",
    "deprecated": "wikibase:DeprecatedRank",
}


def prefixes(base: str) -> dict[str, str]:
    """Alle Namensräume, exakt wie in FactGrids Special:EntityData/*.ttl."""
    return {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "owl": "http://www.w3.org/2002/07/owl#",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "schema": "http://schema.org/",
        "prov": "http://www.w3.org/ns/prov#",
        "geo": "http://www.opengis.net/ont/geosparql#",
        "wikibase": "http://wikiba.se/ontology#",
        "wd": f"{base}entity/",
        "data": f"{base}wiki/Special:EntityData/",
        "s": f"{base}entity/statement/",
        "ref": f"{base}reference/",
        "v": f"{base}value/",
        "wdt": f"{base}prop/direct/",
        "wdtn": f"{base}prop/direct-normalized/",
        "p": f"{base}prop/",
        "ps": f"{base}prop/statement/",
        "psv": f"{base}prop/statement/value/",
        "psn": f"{base}prop/statement/value-normalized/",
        "pq": f"{base}prop/qualifier/",
        "pqv": f"{base}prop/qualifier/value/",
        "pqn": f"{base}prop/qualifier/value-normalized/",
        "pr": f"{base}prop/reference/",
        "prv": f"{base}prop/reference/value/",
        "prn": f"{base}prop/reference/value-normalized/",
        "wdno": f"{base}prop/novalue/",
    }


# --------------------------------------------------------------------------- #
# Turtle-Hilfsfunktionen
# --------------------------------------------------------------------------- #

_ESC = {
    "\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t",
    "\b": "\\b", "\f": "\\f",
}
_ESC_RE = re.compile(r'[\\"\n\r\t\b\f]')
_IRI_BAD = re.compile(r'[\x00-\x20<>"{}|^`\\]')
_LANG_RE = re.compile(r"^[a-zA-Z]+(-[a-zA-Z0-9]+)*$")
_SAFE_LOCAL = re.compile(r"^[A-Za-z0-9_\-]+$")


def lit(s: str, lang: str | None = None, dtype: str | None = None) -> str:
    out = '"' + _ESC_RE.sub(lambda m: _ESC[m.group(0)], s) + '"'
    if lang and _LANG_RE.match(lang):
        return out + "@" + lang
    if dtype:
        return out + "^^" + dtype
    return out


def iri(s: str) -> str:
    """Volles IRI in spitzen Klammern; unerlaubte Zeichen werden prozentkodiert."""
    return "<" + _IRI_BAD.sub(lambda m: quote(m.group(0), safe=""), s) + ">"


def wd(eid: str, base: str = DEFAULT_BASE) -> str:
    return "wd:" + eid if _SAFE_LOCAL.match(eid) else iri(base + "entity/" + eid)


def sha1(obj) -> str:
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Zeit: Wikibase-Zeitstring → xsd:dateTime (inkl. Julianisch→Gregorianisch)
# --------------------------------------------------------------------------- #

_TIME_RE = re.compile(r"^([+-])(\d+)-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)Z$")


def _julian_to_gregorian(y: int, m: int, d: int) -> tuple[int, int, int]:
    a = (14 - m) // 12
    yy = y + 4800 - a
    mm = m + 12 * a - 3
    jdn = d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - 32083
    # JDN → gregorianisch
    f = jdn + 1401 + (((4 * jdn + 274277) // 146097) * 3) // 4 - 38
    e = 4 * f + 3
    g = (e % 1461) // 4
    h = 5 * g + 2
    day = (h % 153) // 5 + 1
    month = (h // 153 + 2) % 12 + 1
    year = e // 1461 - 4716 + (12 + 2 - month) // 12
    return year, month, day


def _days_in_month(y: int, m: int) -> int:
    if m == 2:
        return 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28
    return 30 if m in (4, 6, 9, 11) else 31


def time_literal(value: dict) -> str | None:
    """
    xsd:dateTime-Literal wie Wikibase (DateTimeValueCleaner / JulianDateTimeValueCleaner):
      * Präzision ≤ Jahr → -01-01, Präzision Monat → Tag 01; Tag/Monat 00 → 01; Tag auf Monatslänge geklemmt
      * julianische Tagesdaten (Präzision ≥ 11) → proleptisch gregorianisch
      * v. Chr.: XSD-1.1-Verschiebung (Wikibase "-0100" = 100 v. Chr. → "-0099"; "-0001" → "0000")
    """
    m = _TIME_RE.match(value.get("time", ""))
    if not m:
        return None
    sign, ys, mo, da, hh, mi, ss = m.groups()
    year, month, day = int(ys), int(mo), int(da)
    precision = int(value.get("precision", 11))
    if precision <= 9:
        month, day = 1, 1
    elif precision == 10:
        day = 1
    month = min(max(month, 1), 12)
    day = max(day, 1)
    if sign == "+" and year > 0:
        if value.get("calendarmodel") == JULIAN and precision >= 11:
            day = min(day, 31)
            try:
                year, month, day = _julian_to_gregorian(year, month, day)
            except Exception:  # pragma: no cover
                pass
        day = min(day, _days_in_month(year, month))
        return lit(f"{year:04d}-{month:02d}-{day:02d}T{hh}:{mi}:{ss}Z", dtype="xsd:dateTime")
    # v. Chr. bzw. Jahr 0: keine Kalenderumrechnung (in FactGrid praktisch nicht vorhanden), XSD-Verschiebung
    day = min(day, 31)
    if sign == "-" and year > 0:
        year -= 1
    neg = "-" if year > 0 and sign == "-" else ""
    return lit(f"{neg}{year:04d}-{month:02d}-{day:02d}T{hh}:{mi}:{ss}Z", dtype="xsd:dateTime")


# --------------------------------------------------------------------------- #
# Snak → Turtle-Objekt (+ ggf. Wertknoten)
# --------------------------------------------------------------------------- #

class Emitter:
    """Sammelt Tripel für eine Entität und schreibt sie als Turtle-Zeilen."""

    def __init__(self, base: str, opts: argparse.Namespace):
        self.base = base
        self.opts = opts
        self.lines: list[str] = []
        self.value_nodes: dict[str, list[str]] = {}  # hash → Tripel des Wertknotens

    def t(self, s: str, p: str, o: str) -> None:
        self.lines.append(f"{s} {p} {o} .")

    # -- Werte ------------------------------------------------------------------

    def simple_value(self, snak: dict) -> str | None:
        """Objekt für wdt:/ps:/pq:/pr: (ohne Wertknoten). None → nicht abbildbar."""
        dv = snak.get("datavalue") or {}
        dtype = snak.get("datatype") or ""
        vtype = dv.get("type")
        val = dv.get("value")
        if val is None:
            return None
        if vtype == "wikibase-entityid":
            eid = val.get("id") or ""
            return wd(eid, self.base) if eid else None
        if vtype == "string":
            if dtype == "url":
                return iri(val)
            if dtype == "commonsMedia":
                # Wikibase kodiert Leerzeichen als %20 (wie Wikidata: Special:FilePath/Douglas%20adams...)
                return iri(COMMONS_FILEPATH + quote(val, safe=""))
            if dtype in ("geo-shape", "tabular-data"):
                return iri(COMMONS_DATA + quote(val.replace(" ", "_"), safe="/:"))
            return lit(val)
        if vtype == "monolingualtext":
            return lit(val.get("text", ""), lang=val.get("language"))
        if vtype == "time":
            return time_literal(val)
        if vtype == "quantity":
            amount = str(val.get("amount", "")).lstrip("+")
            return lit(amount, dtype="xsd:decimal") if amount else None
        if vtype == "globecoordinate":
            lat, lon = val.get("latitude"), val.get("longitude")
            if lat is None or lon is None:
                return None
            globe = val.get("globe") or EARTH
            wkt = f"Point({lon} {lat})"
            if globe != EARTH:
                wkt = f"<{globe}> " + wkt
            return lit(wkt, dtype="geo:wktLiteral")
        # Unbekannter Typ: als String
        return lit(json.dumps(val, ensure_ascii=False))

    def value_node(self, snak: dict) -> str | None:
        """Erzeugt (einmalig) den Wertknoten v:<hash> für time/quantity/coordinate."""
        dv = snak.get("datavalue") or {}
        vtype, val = dv.get("type"), dv.get("value")
        if vtype not in ("time", "quantity", "globecoordinate") or val is None:
            return None
        h = sha1(val)
        node = "v:" + h
        if h in self.value_nodes:
            return node
        tr: list[str] = []
        if vtype == "time":
            tl = time_literal(val)
            if tl is None:
                return None
            tr.append(f"{node} a wikibase:TimeValue .")
            tr.append(f"{node} wikibase:timeValue {tl} .")
            tr.append(f'{node} wikibase:timePrecision "{int(val.get("precision", 11))}"^^xsd:integer .')
            tr.append(f'{node} wikibase:timeTimezone "{int(val.get("timezone", 0))}"^^xsd:integer .')
            tr.append(f"{node} wikibase:timeCalendarModel {iri(val.get('calendarmodel') or GREGORIAN)} .")
        elif vtype == "quantity":
            tr.append(f"{node} a wikibase:QuantityValue .")
            tr.append(f'{node} wikibase:quantityAmount {lit(str(val.get("amount", "0")).lstrip("+"), dtype="xsd:decimal")} .')
            for key, pred in (("upperBound", "quantityUpperBound"), ("lowerBound", "quantityLowerBound")):
                if val.get(key) is not None:
                    tr.append(f'{node} wikibase:{pred} {lit(str(val[key]).lstrip("+"), dtype="xsd:decimal")} .')
            unit = val.get("unit") or "1"
            tr.append(f"{node} wikibase:quantityUnit {iri(UNITLESS if unit == '1' else unit)} .")
        else:  # globecoordinate
            tr.append(f"{node} a wikibase:GlobecoordinateValue .")
            tr.append(f'{node} wikibase:geoLatitude "{val.get("latitude")}"^^xsd:double .')
            tr.append(f'{node} wikibase:geoLongitude "{val.get("longitude")}"^^xsd:double .')
            if val.get("precision") is not None:
                tr.append(f'{node} wikibase:geoPrecision "{val.get("precision")}"^^xsd:double .')
            tr.append(f"{node} wikibase:geoGlobe {iri(val.get('globe') or EARTH)} .")
        self.value_nodes[h] = tr
        return node

    def emit_snak(self, subject: str, snak: dict, ns: str, ns_value: str | None, bnode_seed: str) -> None:
        """Schreibt <subject> ns:P.. value (+ ns_value:P.. Wertknoten, falls ns_value) oder some/novalue."""
        prop = snak.get("property")
        if not prop:
            return
        st = snak.get("snaktype")
        if st == "novalue":
            self.t(subject, "a", f"wdno:{prop}")
            return
        if st == "somevalue":
            self.t(subject, f"{ns}:{prop}", "_:b" + sha1([bnode_seed, ns, prop])[:16])
            return
        obj = self.simple_value(snak)
        if obj is None:
            return
        self.t(subject, f"{ns}:{prop}", obj)
        if ns_value:
            vn = self.value_node(snak)
            if vn:
                self.t(subject, f"{ns_value}:{prop}", vn)

    # -- Entität -----------------------------------------------------------------

    def emit_entity(self, ent: dict) -> None:
        eid = ent.get("id")
        etype = ent.get("type")
        if not eid or etype not in ("item", "property"):
            return
        subj = wd(eid, self.base)
        self.t(subj, "a", "wikibase:Item" if etype == "item" else "wikibase:Property")

        # Labels / Beschreibungen / Aliase
        for lang, obj in (ent.get("labels") or {}).items():
            val = lit(obj.get("value", ""), lang=obj.get("language", lang))
            self.t(subj, "rdfs:label", val)
            if self.opts.full_labels:
                self.t(subj, "skos:prefLabel", val)
                self.t(subj, "schema:name", val)
        for lang, obj in (ent.get("descriptions") or {}).items():
            self.t(subj, "schema:description", lit(obj.get("value", ""), lang=obj.get("language", lang)))
        for lang, objs in (ent.get("aliases") or {}).items():
            for obj in objs or []:
                self.t(subj, "skos:altLabel", lit(obj.get("value", ""), lang=obj.get("language", lang)))

        # Property-Definition
        if etype == "property":
            dt = ent.get("datatype") or "string"
            ptype = PROPERTY_TYPES.get(dt) or "".join(w.capitalize() for w in re.split(r"[-_]", dt))
            self.t(subj, "wikibase:propertyType", f"wikibase:{ptype}")
            for pred, ns in (
                ("directClaim", "wdt"), ("claim", "p"), ("statementProperty", "ps"),
                ("statementValue", "psv"), ("qualifier", "pq"), ("qualifierValue", "pqv"),
                ("reference", "pr"), ("referenceValue", "prv"), ("novalue", "wdno"),
            ):
                self.t(subj, f"wikibase:{pred}", f"{ns}:{eid}")

        # Statements
        claims = ent.get("claims") or {}
        n_statements = 0
        n_identifiers = 0
        for prop, statements in claims.items():
            if not statements:
                continue
            ranks = [s.get("rank", "normal") for s in statements]
            best = "preferred" if "preferred" in ranks else "normal"
            for s in statements:
                n_statements += 1
                mainsnak = s.get("mainsnak") or {}
                if mainsnak.get("datatype") == "external-id":
                    n_identifiers += 1
                rank = s.get("rank", "normal")
                is_best = rank == best and rank != "deprecated"
                guid = (s.get("id") or "").replace("$", "-")

                # Truthy (Wertknoten hängen nur am vollen Statement)
                if is_best:
                    self.emit_snak(subj, mainsnak, "wdt", None, guid)

                if self.opts.flavor == "truthy" or not guid:
                    continue

                # Volles Statement
                st = "s:" + guid if _SAFE_LOCAL.match(guid) else iri(self.base + "entity/statement/" + guid)
                self.t(subj, f"p:{prop}", st)
                self.t(st, "a", "wikibase:Statement")
                if is_best:
                    self.t(st, "a", "wikibase:BestRank")
                self.t(st, "wikibase:rank", RANK_IRI.get(rank, "wikibase:NormalRank"))
                self.emit_snak(st, mainsnak, "ps", "psv", guid)

                for qprop, qsnaks in (s.get("qualifiers") or {}).items():
                    for i, q in enumerate(qsnaks or []):
                        self.emit_snak(st, q, "pq", "pqv", f"{guid}/q{i}")

                if self.opts.no_references:
                    continue
                for ref in s.get("references") or []:
                    rh = ref.get("hash") or sha1(ref.get("snaks") or {})
                    rnode = "ref:" + rh
                    self.t(st, "prov:wasDerivedFrom", rnode)
                    self.t(rnode, "a", "wikibase:Reference")
                    for rprop, rsnaks in (ref.get("snaks") or {}).items():
                        for i, r in enumerate(rsnaks or []):
                            self.emit_snak(rnode, r, "pr", "prv", f"{rh}/r{i}")

        # Sitelinks
        n_sitelinks = 0
        if not self.opts.no_sitelinks:
            for site, sl in (ent.get("sitelinks") or {}).items():
                url, group, lang = sitelink_url(site, sl)
                if not url:
                    continue
                n_sitelinks += 1
                page = iri(url)
                self.t(page, "a", "schema:Article")
                self.t(page, "schema:about", subj)
                if lang:
                    self.t(page, "schema:inLanguage", lit(lang))
                self.t(page, "schema:isPartOf", iri(url.split("/wiki/")[0] + "/"))
                self.t(page, "schema:name", lit(sl.get("title", ""), lang=lang or None))
                self.t(iri(url.split("/wiki/")[0] + "/"), "wikibase:wikiGroup", lit(group))

        # Metadaten
        self.t(subj, "wikibase:statements", f'"{n_statements}"^^xsd:integer')
        self.t(subj, "wikibase:sitelinks", f'"{n_sitelinks}"^^xsd:integer')
        self.t(subj, "wikibase:identifiers", f'"{n_identifiers}"^^xsd:integer')
        data = "data:" + eid
        self.t(data, "a", "schema:Dataset")
        self.t(data, "schema:about", subj)
        if ent.get("modified"):
            self.t(data, "schema:dateModified", lit(ent["modified"], dtype="xsd:dateTime"))
        if ent.get("lastrevid"):
            self.t(data, "schema:version", f'"{ent["lastrevid"]}"^^xsd:integer')

    def render(self) -> str:
        out = self.lines
        for tr in self.value_nodes.values():
            out.extend(tr)
        return "\n".join(out) + ("\n" if out else "")


def sitelink_url(site: str, sl: dict) -> tuple[str | None, str, str | None]:
    if sl.get("url"):
        url = sl["url"]
        group = "wikipedia" if "wikipedia.org" in url else url.split("//")[-1].split(".")[0]
        return url, group, None
    title = quote((sl.get("title") or "").replace(" ", "_"), safe="/:()',!*")
    if site == "wikidatawiki":
        return "https://www.wikidata.org/wiki/" + title, "wikidata", "en"
    if site == "commonswiki":
        return "https://commons.wikimedia.org/wiki/" + title, "commons", "en"
    if site.endswith("wiki") and len(site) > 4:
        lang = site[:-4].replace("_", "-")
        return f"https://{lang}.wikipedia.org/wiki/" + title, "wikipedia", lang
    return None, "", None


# --------------------------------------------------------------------------- #
# Streaming über den Dump
# --------------------------------------------------------------------------- #

def open_dump(path: str) -> io.TextIOBase:
    if path == "-":
        raw = sys.stdin.buffer
        head = raw.peek(2) if hasattr(raw, "peek") else b""
        if head[:2] == b"\x1f\x8b":
            return io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8")
        return io.TextIOWrapper(raw, encoding="utf-8")
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def entity_lines(fh: io.TextIOBase) -> Iterator[str]:
    """Wikibase-JSON-Dumps: '[' , eine Entität pro Zeile (mit Komma), ']'."""
    for line in fh:
        line = line.strip()
        if not line or line in ("[", "]"):
            continue
        if line.endswith(","):
            line = line[:-1]
        yield line


_WORKER_OPTS: argparse.Namespace | None = None


def _init_worker(opts: argparse.Namespace) -> None:
    global _WORKER_OPTS
    _WORKER_OPTS = opts


def convert_line(line: str) -> str:
    try:
        ent = json.loads(line)
    except json.JSONDecodeError:
        return ""
    em = Emitter(_WORKER_OPTS.base, _WORKER_OPTS)
    em.emit_entity(ent)
    return em.render()


def header(base: str) -> str:
    return "".join(f"@prefix {k}: <{v}> .\n" for k, v in prefixes(base).items()) + "\n"


def run(opts: argparse.Namespace) -> None:
    out = sys.stdout if opts.output in (None, "-") else open(opts.output, "w", encoding="utf-8")
    out.write(header(opts.base))
    fh = open_dump(opts.input)
    n = 0
    if opts.workers <= 1:
        _init_worker(opts)
        for line in entity_lines(fh):
            out.write(convert_line(line))
            n += 1
            if n % 50000 == 0:
                print(f"[wb2rdf] {n} Entitäten", file=sys.stderr)
    else:
        with mp.Pool(opts.workers, initializer=_init_worker, initargs=(opts,)) as pool:
            for chunk in pool.imap(convert_line, entity_lines(fh), chunksize=256):
                out.write(chunk)
                n += 1
                if n % 50000 == 0:
                    print(f"[wb2rdf] {n} Entitäten", file=sys.stderr)
    out.flush()
    if out is not sys.stdout:
        out.close()
    print(f"[wb2rdf] fertig: {n} Entitäten", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="JSON-Dump (.json oder .json.gz) oder '-' für stdin")
    ap.add_argument("-o", "--output", default="-", help="Turtle-Ausgabe (Standard: stdout)")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"Wikibase-Basis-URL (Standard: {DEFAULT_BASE})")
    ap.add_argument("--flavor", choices=["full", "truthy"], default="full",
                    help="full = Statements+Qualifier+Referenzen, truthy = nur wdt: + Labels")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--no-sitelinks", action="store_true")
    ap.add_argument("--no-references", action="store_true")
    ap.add_argument("--full-labels", action="store_true", help="zusätzlich skos:prefLabel und schema:name")
    opts = ap.parse_args(argv)
    if not opts.base.endswith("/"):
        opts.base += "/"
    run(opts)


if __name__ == "__main__":
    main()
