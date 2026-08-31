"""Namensräume von FactGrid (identisch zu Special:EntityData/*.ttl) und Hilfen zum Kürzen/Injizieren."""
from __future__ import annotations

import os
import re

BASE = os.environ.get("FACTGRID_BASE", "https://database.factgrid.de/")
if not BASE.endswith("/"):
    BASE += "/"

PREFIXES: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "schema": "http://schema.org/",
    "prov": "http://www.w3.org/ns/prov#",
    "geo": "http://www.opengis.net/ont/geosparql#",
    "geof": "http://www.opengis.net/def/function/geosparql/",
    "wikibase": "http://wikiba.se/ontology#",
    "bd": "http://www.bigdata.com/rdf#",  # nur damit alte WDQS-Queries parsen
    "ql": "http://qlever.cs.uni-freiburg.de/builtin-functions/",
    "wd": f"{BASE}entity/",
    "data": f"{BASE}wiki/Special:EntityData/",
    "s": f"{BASE}entity/statement/",
    "ref": f"{BASE}reference/",
    "v": f"{BASE}value/",
    "wdt": f"{BASE}prop/direct/",
    "wdtn": f"{BASE}prop/direct-normalized/",
    "p": f"{BASE}prop/",
    "ps": f"{BASE}prop/statement/",
    "psv": f"{BASE}prop/statement/value/",
    "psn": f"{BASE}prop/statement/value-normalized/",
    "pq": f"{BASE}prop/qualifier/",
    "pqv": f"{BASE}prop/qualifier/value/",
    "pqn": f"{BASE}prop/qualifier/value-normalized/",
    "pr": f"{BASE}prop/reference/",
    "prv": f"{BASE}prop/reference/value/",
    "prn": f"{BASE}prop/reference/value-normalized/",
    "wdno": f"{BASE}prop/novalue/",
}

# Kompakter Prefix-Block für System-Prompts / Doku
PREFIX_BLOCK = "\n".join(f"PREFIX {k}: <{v}>" for k, v in PREFIXES.items())

_DECLARED_RE = re.compile(r"^\s*PREFIX\s+([A-Za-z][\w\-]*)?:\s*<", re.I | re.M)
_USED_RE = re.compile(r"(?<![\w<:/#\-])([A-Za-z][\w\-]*):(?=[A-Za-z0-9_])")

# Längste IRIs zuerst, damit z. B. prop/statement/value/ vor prop/statement/ gekürzt wird
_SORTED = sorted(PREFIXES.items(), key=lambda kv: -len(kv[1]))


def inject_prefixes(query: str) -> str:
    """Stellt PREFIX-Zeilen für alle benutzten, aber nicht deklarierten Präfixe voran."""
    declared = {m.group(1) or "" for m in _DECLARED_RE.finditer(query)}
    # Kommentare und String-Literale grob ausblenden, damit "http:" o. ä. nicht als Präfix zählt
    scrub = re.sub(r'"(?:\\.|[^"\\])*"', '""', query)
    scrub = re.sub(r"#[^\n]*", "", scrub)
    used = {m.group(1) for m in _USED_RE.finditer(scrub)}
    missing = [p for p in PREFIXES if p in used and p not in declared]
    if not missing:
        return query
    return "\n".join(f"PREFIX {p}: <{PREFIXES[p]}>" for p in missing) + "\n" + query


def shorten(iri: str) -> str:
    for pfx, ns in _SORTED:
        if iri.startswith(ns):
            local = iri[len(ns):]
            if re.match(r"^[\w\-.]*$", local):
                return f"{pfx}:{local}"
    return f"<{iri}>"


def expand(curie: str) -> str:
    if ":" in curie and not curie.startswith("<"):
        pfx, local = curie.split(":", 1)
        if pfx in PREFIXES:
            return PREFIXES[pfx] + local
    return curie.strip("<>")


def entity_iri(eid: str) -> str:
    return f"{BASE}entity/{eid}"
