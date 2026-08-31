"""
factgrid-mcp – MCP-Server, der einem LLM den lokalen QLever-Index von FactGrid
zugänglich macht. Sieben Tools (jedes Tool kostet Kontextfenster, deshalb keins doppelt):

  sparql                 – SPARQL ausführen (WDQS-kompatibel: Label-Service wird umgeschrieben)
  search_entities        – Items/Properties per Label/Alias finden (Text → Q-/P-ID)
  get_entity             – Kompakte Sicht auf ein Item oder eine Property
  get_statement_values   – Alle Statements zu einem Paar Entität/Property, mit Wertknoten,
                           Qualifikatoren und Referenzen
  get_property_hierarchy – Baum entlang einer Property (Ober-/Unterbegriffe, Teil-von, …)
  schema_overview        – Häufigste Klassen und Properties (gecacht) als Orientierung
  get_wikibase_info      – Welche Instanz, welcher Stand, wie groß

Start (stdio):   factgrid-mcp            (Konfiguration über Umgebungsvariablen, s. README)
Start (HTTP):    factgrid-mcp --http     (für Open WebUI u. a.: http://127.0.0.1:8765/mcp)
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

from fastmcp import FastMCP

if __package__:
    from . import prefixes, qlever
    from .prefixes import PREFIX_BLOCK
else:  # direkt als Skript gestartet (python server.py)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from factgrid_mcp import prefixes, qlever  # type: ignore
    from factgrid_mcp.prefixes import PREFIX_BLOCK  # type: ignore

INSTANCE_OF = os.environ.get("FACTGRID_INSTANCE_OF", "P2")
USE_TEXT_INDEX = os.environ.get("FACTGRID_TEXT_INDEX", "0") == "1"
CACHE_DIR = Path(os.environ.get("FACTGRID_CACHE_DIR", Path.home() / ".cache" / "factgrid-mcp"))

_ID_RE = re.compile(r"^[QP]\d+$")

mcp = FastMCP(
    "factgrid-local",
    instructions=(
        "Lokaler FactGrid-Spiegel (Wikibase-RDF in QLever). Vorgehen: 1) schema_overview für "
        "Klassen/Properties, 2) search_entities um Namen in Q-/P-IDs aufzulösen, 3) get_entity um "
        "die tatsächlich verwendeten Properties eines Beispiel-Items zu sehen, 4) sparql. "
        "Detailsicht auf ein Paar Entität/Property (Wertknoten, Qualifikatoren, Referenzen): "
        "get_statement_values; Klassen- und Teil-von-Bäume: get_property_hierarchy; Instanz und "
        "Datenstand des Spiegels: get_wikibase_info. "
        f"Instanz-Property ist wdt:{INSTANCE_OF}. Prefixe (wd, wdt, p, ps, pq, …) werden automatisch "
        "ergänzt; SERVICE wikibase:label wird unterstützt. Immer LIMIT setzen."
    ),
)


def _lang_filter(var: str, lang: str) -> str:
    return "" if lang == "any" else f'FILTER(LANG({var}) = "{lang}")'


def _strip_lang(label: str) -> str:
    return re.sub(r"@\w+(-\w+)*$", "", label or "")


def _fmt_value(value: str, label: str = "") -> str:
    """Wert mit aufgelöstem Label; Blank Nodes sind unbekannte/fehlende Werte."""
    if value.startswith("_:"):
        return "[unbekannter Wert]"
    label = _strip_lang(label)
    return f"{label} ({value})" if label and label != value else value


# --------------------------------------------------------------------------- #
@mcp.tool()
def sparql(query: str, limit: int = 50, timeout_s: int = 60) -> str:
    """Führt eine SPARQL-SELECT/ASK-Abfrage gegen den lokalen FactGrid-QLever aus.
    WDQS-Syntax ist erlaubt (SERVICE wikibase:label, fehlende PREFIXe). Ergebnis als TSV;
    bei Fehlern kommt die QLever-Fehlermeldung zurück (dann Query korrigieren und erneut senden)."""
    try:
        res = qlever.run_query(query, limit=limit, timeout_s=timeout_s)
    except qlever.QLeverError as e:
        return f"FEHLER: {e}"
    return res.as_tsv()


# --------------------------------------------------------------------------- #
@mcp.tool()
def search_entities(text: str, lang: str = "de", entity_type: str = "item", limit: int = 10) -> str:
    """Findet FactGrid-Entitäten über Label oder Alias (Präfixsuche, dann Teilstringsuche).
    entity_type: item | property | any. lang: de | en | any. Liefert ID, Label, Beschreibung,
    Anzahl Statements (als Relevanzmaß)."""
    text = text.strip()
    if not text:
        return "Leerer Suchtext."
    if _ID_RE.match(text):
        return get_entity(text, lang=lang if lang != "any" else "de", max_statements=15)
    limit = max(1, min(limit, 50))
    type_pat = {"item": "?e a wikibase:Item .", "property": "?e a wikibase:Property ."}.get(entity_type, "")
    def sparql_str(v: str) -> str:  # String-Literal-Escaping für SPARQL
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    esc = sparql_str(text)
    regex_esc = sparql_str(re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text))  # nur Regex-Metazeichen, dann SPARQL
    lf = _lang_filter("?label", lang)
    lang_for_desc = "de" if lang == "any" else lang

    def q(match_clause: str) -> str:
        return f"""
SELECT ?e ?label ?description ?n WHERE {{
  {type_pat}
  ?e ?pred ?label . VALUES ?pred {{ rdfs:label skos:altLabel }}
  {lf}
  {match_clause}
  ?e wikibase:statements ?n .
  OPTIONAL {{ ?e schema:description ?description . FILTER(LANG(?description) = "{lang_for_desc}") }}
}} ORDER BY DESC(?n) LIMIT {limit}"""

    clauses = []
    if USE_TEXT_INDEX:
        words = " ".join(w + "*" for w in re.findall(r"\w+", esc))
        clauses.append(f'?label ql:contains-word "{words}" .')
    clauses.append(f'FILTER(REGEX(STR(?label), "^{regex_esc}"))')  # QLever: Präfix-Regex ist indexoptimiert
    clauses.append(f'FILTER(CONTAINS(LCASE(STR(?label)), "{esc.lower()}"))')

    seen: "OrderedDict[str, list[str]]" = OrderedDict()
    errors = []
    for clause in clauses:
        try:
            res = qlever.run_query(q(clause), limit=limit, timeout_s=30)
        except qlever.QLeverError as e:
            errors.append(str(e).splitlines()[0])
            continue
        for row in res.rows:
            seen.setdefault(row[0], row)
        if len(seen) >= limit:
            break
    if not seen:
        return "Keine Treffer." + (" (" + "; ".join(errors) + ")" if errors else "")
    def clean(v: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"@\w+(-\w+)*$", "", v)).strip()

    lines = ["id\tlabel\tdescription\tstatements"]
    lines += ["\t".join(clean(c) for c in r) for r in list(seen.values())[:limit]]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
@mcp.tool()
def get_entity(entity_id: str, lang: str = "de", max_statements: int = 60,
               include_external_ids: bool = False) -> str:
    """Kompakte Darstellung eines Items (Q…) oder einer Property (P…): Label, Beschreibung,
    Aliase, Statements mit Qualifikatoren (Labels aufgelöst). Für Properties zusätzlich Datentyp
    und Verwendungszahl. Ideal, um zu lernen, welche Properties FactGrid tatsächlich nutzt.
    include_external_ids=true nimmt Normdaten-IDs (GND, VIAF …) mit auf; sie sind für die
    Recherche meist Ballast und bleiben deshalb per Default weg."""
    eid = entity_id.strip().upper()
    if not _ID_RE.match(eid):
        return "Erwartet eine ID wie Q12345 oder P2."
    fallback = "de" if lang == "en" else "en"

    head_q = f"""
SELECT ?p ?o WHERE {{
  wd:{eid} ?p ?o .
  VALUES ?p {{ rdfs:label schema:description skos:altLabel wikibase:statements wikibase:propertyType rdf:type }}
  FILTER(!isLiteral(?o) || LANG(?o) = "{lang}" || LANG(?o) = "{fallback}" || LANG(?o) = "")
}} LIMIT 200"""
    try:
        head = qlever.run_query(head_q, limit=200, timeout_s=30)
    except qlever.QLeverError as e:
        return f"FEHLER: {e}"
    if not head.rows:
        return f"{eid} nicht im Index."

    info: dict[str, list[str]] = {}
    for p, o in head.rows:
        info.setdefault(p, []).append(o)

    def pick(pred: str) -> str:
        vals = info.get(pred, [])
        for v in vals:
            if v.endswith(f"@{lang}"):
                return v[: -len(lang) - 1]
        for v in vals:
            if v.endswith(f"@{fallback}"):
                return v
        return vals[0] if vals else ""

    out = [f"{eid}: {pick('rdfs:label')}"]
    if info.get("schema:description"):
        out.append("  Beschreibung: " + re.sub(r"\s+", " ", pick("schema:description")))
    aliases = [v for v in info.get("skos:altLabel", []) if v.endswith(f"@{lang}")]
    if aliases:
        out.append("  Aliase: " + ", ".join(a[: -len(lang) - 1] for a in aliases[:10]))
    if eid.startswith("P"):
        out.append(f"  Datentyp: {pick('wikibase:propertyType').replace('wikibase:', '')}")
        try:
            cnt = qlever.run_query(f"SELECT (COUNT(*) AS ?n) WHERE {{ ?s wdt:{eid} ?o }}", limit=None, timeout_s=30)
            out.append(f"  Verwendungen (wdt:{eid}): {cnt.rows[0][0] if cnt.rows else '?'}")
            ex = qlever.run_query(
                f"""SELECT ?o ?oLabel (COUNT(?s) AS ?n) WHERE {{ ?s wdt:{eid} ?o .
                    SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang},{fallback}". }} }}
                    GROUP BY ?o ?oLabel ORDER BY DESC(?n) LIMIT 8""", limit=8, timeout_s=30)
            if ex.rows:
                out.append("  Häufigste Werte: " + "; ".join(f"{r[0]} {r[1]}".strip() + f" ({r[2]})" for r in ex.rows))
        except qlever.QLeverError as e:
            out.append(f"  (Statistik nicht verfügbar: {str(e).splitlines()[0]})")
        return "\n".join(out)

    out.append(f"  Statements: {pick('wikibase:statements')}")
    ext_filter = "" if include_external_ids else (
        "?prop wikibase:propertyType ?ptype . FILTER(?ptype != wikibase:ExternalId)")
    st_q = f"""
SELECT ?prop ?propLabel ?st ?rank ?value ?valueLabel ?qprop ?qpropLabel ?qvalue ?qvalueLabel WHERE {{
  wd:{eid} ?p ?st .
  ?prop wikibase:claim ?p ; wikibase:statementProperty ?ps .
  {ext_filter}
  ?st ?ps ?value ; wikibase:rank ?rank .
  OPTIONAL {{ ?st ?pq ?qvalue . ?qprop wikibase:qualifier ?pq . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang},{fallback}". }}
}} ORDER BY ?prop ?st LIMIT 1500"""
    try:
        res = qlever.run_query(st_q, limit=None, timeout_s=60)
    except qlever.QLeverError as e:
        out.append(f"FEHLER bei Statements: {e}")
        return "\n".join(out)

    statements: "OrderedDict[str, dict]" = OrderedDict()
    for prop, propLabel, st, rank, value, valueLabel, qprop, qpropLabel, qvalue, qvalueLabel in res.rows:
        s = statements.setdefault(st, {"prop": prop, "propLabel": propLabel, "rank": rank,
                                       "value": value, "valueLabel": valueLabel, "quals": []})
        if qprop:
            s["quals"].append((qprop, qpropLabel, qvalue, qvalueLabel))

    def strip_lang(label: str) -> str:
        return re.sub(r"@\w+(-\w+)*$", "", label)

    def fmt(val: str, label: str) -> str:
        if val.startswith("_:"):
            return "[unbekannter Wert]"
        label = strip_lang(label)
        return f"{label} ({val})" if label and label != val else val

    shown = 0
    for st, s in statements.items():
        if shown >= max_statements:
            out.append(f"  … {len(statements) - shown} weitere Statements (max_statements erhöhen)")
            break
        rank = "" if s["rank"] == "wikibase:NormalRank" else " [" + s["rank"].replace("wikibase:", "") + "]"
        line = "  " + s["prop"] + " " + strip_lang(s["propLabel"]) + ": " + fmt(s["value"], s["valueLabel"]) + rank
        if s["quals"]:
            quals = "; ".join(qp + " " + strip_lang(ql) + ": " + fmt(qv, qvl) for qp, ql, qv, qvl in s["quals"])
            line += "  {" + quals + "}"
        out.append(line)
        shown += 1
    return "\n".join(out)


# --------------------------------------------------------------------------- #
def _label_of(eid: str, lang: str) -> str:
    fallback = "de" if lang == "en" else "en"
    try:
        res = qlever.run_query(
            f"""SELECT ?l WHERE {{ wd:{eid} rdfs:label ?l .
                FILTER(LANG(?l) = "{lang}" || LANG(?l) = "{fallback}") }} LIMIT 2""",
            limit=2, timeout_s=15)
    except qlever.QLeverError:
        return ""
    for row in res.rows:  # bevorzugte Sprache zuerst
        if row[0].endswith(f"@{lang}"):
            return _strip_lang(row[0])
    return _strip_lang(res.rows[0][0]) if res.rows else ""


@mcp.tool()
def get_statement_values(entity_id: str, property_id: str, lang: str = "de",
                         max_statements: int = 25) -> str:
    """Alle Statements einer Entität zu genau einer Property – vollständig: Rang, Wert,
    Qualifikatoren, Referenzen und der Wertknoten (bei Zeitangaben Präzision und Kalender,
    bei Mengen Einheit, bei Koordinaten Länge/Breite). Das ist die Detailsicht zu einer Zeile
    aus get_entity; für den Überblick über eine Entität get_entity nehmen."""
    eid, pid = entity_id.strip().upper(), property_id.strip().upper()
    if not _ID_RE.match(eid) or not pid.startswith("P") or not _ID_RE.match(pid):
        return "Erwartet eine Entitäts-ID (Q… oder P…) und eine Property-ID (P…)."
    fallback = "de" if lang == "en" else "en"
    label_service = f'SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang},{fallback}". }}'

    try:
        res = qlever.run_query(f"""
SELECT ?st ?rank ?value ?valueLabel ?qprop ?qpropLabel ?qvalue ?qvalueLabel WHERE {{
  wd:{eid} p:{pid} ?st .
  ?st ps:{pid} ?value ; wikibase:rank ?rank .
  OPTIONAL {{ ?st ?pq ?qvalue . ?qprop wikibase:qualifier ?pq . }}
  {label_service}
}} ORDER BY ?st LIMIT 800""", limit=None, timeout_s=60)
    except qlever.QLeverError as e:
        return f"FEHLER: {e}"
    if not res.rows:
        return f"{eid} hat keine Statements zu wd:{pid} (oder eines von beiden ist nicht im Index)."

    statements: "OrderedDict[str, dict]" = OrderedDict()
    for st, rank, value, valueLabel, qprop, qpropLabel, qvalue, qvalueLabel in res.rows:
        cur = statements.setdefault(st, {"rank": rank, "value": value, "valueLabel": valueLabel,
                                         "quals": [], "refs": [], "node": []})
        if qprop:
            cur["quals"].append((qprop, qpropLabel, qvalue, qvalueLabel))

    notes = []
    try:  # Wertknoten (nur bei Zeit-, Mengen- und Koordinaten-Properties vorhanden)
        for st, vp, vo in qlever.run_query(f"""
SELECT ?st ?vp ?vo WHERE {{ wd:{eid} p:{pid} ?st . ?st psv:{pid} ?v . ?v ?vp ?vo . }}
LIMIT 300""", limit=None, timeout_s=30).rows:
            if st in statements and vp != "rdf:type":
                statements[st]["node"].append((vp, vo))
    except qlever.QLeverError as e:
        notes.append(f"(Wertknoten nicht abrufbar: {str(e).splitlines()[0]})")
    try:
        for st, rprop, rpropLabel, rvalue, rvalueLabel in qlever.run_query(f"""
SELECT ?st ?rprop ?rpropLabel ?rvalue ?rvalueLabel WHERE {{
  wd:{eid} p:{pid} ?st .
  ?st prov:wasDerivedFrom ?ref .
  ?ref ?pr ?rvalue . ?rprop wikibase:reference ?pr .
  {label_service}
}} LIMIT 300""", limit=None, timeout_s=30).rows:
            if st in statements:
                statements[st]["refs"].append((rprop, rpropLabel, rvalue, rvalueLabel))
    except qlever.QLeverError as e:
        notes.append(f"(Referenzen nicht abrufbar: {str(e).splitlines()[0]})")

    out = [f"{eid} {_label_of(eid, lang)} – wd:{pid} {_label_of(pid, lang)}: "
           f"{len(statements)} Statement(s)".replace("  ", " ")]
    for i, (st, cur) in enumerate(statements.items(), 1):
        if i > max_statements:
            out.append(f"  … {len(statements) - max_statements} weitere (max_statements erhöhen)")
            break
        rank = cur["rank"].replace("wikibase:", "").replace("Rank", "")
        out.append(f"{i}. {_fmt_value(cur['value'], cur['valueLabel'])}  [{rank}]  {st}")
        for vp, vo in cur["node"]:
            out.append(f"     Wertknoten: {vp} = {vo}")
        for qp, ql, qv, qvl in cur["quals"]:
            out.append(f"     Qualifikator {qp} {_strip_lang(ql)}: {_fmt_value(qv, qvl)}")
        for rp, rl, rv, rvl in cur["refs"]:
            out.append(f"     Referenz {rp} {_strip_lang(rl)}: {_fmt_value(rv, rvl)}")
    return "\n".join(out + notes)


# --------------------------------------------------------------------------- #
@mcp.tool()
def get_property_hierarchy(entity_id: str, property_id: str, max_depth: int = 5,
                           direction: str = "down", lang: str = "de", max_nodes: int = 200) -> str:
    """Baum entlang einer Property, ausgehend von einer Entität – für Ober-/Unterbegriffe,
    Teil-von-Ketten, Nachfahren usw. direction="down" folgt den eingehenden Kanten
    (?kind wdt:P ?knoten, also z. B. alle Unterklassen), direction="up" den ausgehenden
    (?knoten wdt:P ?ziel, also die Kette nach oben). Abgebrochen wird bei max_depth Ebenen
    oder max_nodes Knoten; Zyklen werden erkannt."""
    eid, pid = entity_id.strip().upper(), property_id.strip().upper()
    if not _ID_RE.match(eid) or not pid.startswith("P") or not _ID_RE.match(pid):
        return "Erwartet eine Entitäts-ID (Q… oder P…) und eine Property-ID (P…)."
    down = direction != "up"
    max_depth, max_nodes = max(1, min(max_depth, 10)), max(1, min(max_nodes, 1000))
    fallback = "de" if lang == "en" else "en"
    pattern = f"?child wdt:{pid} ?from ." if down else f"?from wdt:{pid} ?child ."

    tree: dict[str, list[tuple[str, str]]] = {}
    root = f"wd:{eid}"
    frontier, seen, total, truncated = [root], {root}, 0, False
    for _ in range(max_depth):
        if not frontier or truncated:
            break
        values = " ".join(frontier[:100])
        try:
            res = qlever.run_query(f"""
SELECT ?from ?child ?childLabel WHERE {{
  VALUES ?from {{ {values} }}
  {pattern}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang},{fallback}". }}
}} LIMIT 1000""", limit=None, timeout_s=60)
        except qlever.QLeverError as e:
            return f"FEHLER: {e}"
        nxt = []
        for frm, child, childLabel in res.rows:
            if total >= max_nodes:
                truncated = True
                break
            tree.setdefault(frm, []).append((child, childLabel))
            total += 1
            if child not in seen:
                seen.add(child)
                nxt.append(child)
        frontier = nxt

    arrow = "eingehend" if down else "ausgehend"
    out = [f"{root} {_label_of(eid, lang)} – Hierarchie über wdt:{pid} ({arrow}, "
           f"max_depth={max_depth}, {total} Knoten)"]
    if not tree:
        out.append("  (keine verbundenen Entitäten – Richtung oder Property prüfen)")
        return "\n".join(out)

    def walk(node: str, depth: int, path: set) -> None:
        for child, label in tree.get(node, []):
            cycle = " ↺" if child in path else ""
            out.append("  " * (depth + 1) + "└ " + _fmt_value(child, label) + cycle)
            if not cycle and depth + 1 < max_depth:
                walk(child, depth + 1, path | {child})

    walk(root, 0, {root})
    if truncated:
        out.append(f"  … abgeschnitten bei max_nodes={max_nodes}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
@mcp.tool()
def schema_overview(lang: str = "de", refresh: bool = False) -> str:
    """Überblick über das FactGrid-Datenmodell: die 60 häufigsten Klassen (Werte von
    wdt:{INSTANCE_OF}) und die 150 meistgenutzten Properties mit Datentyp. Wird beim
    ersten Aufruf berechnet und gecacht (refresh=true erzwingt Neuberechnung)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{qlever.ENDPOINT}|{lang}|{INSTANCE_OF}".encode()).hexdigest()[:12]
    cache = CACHE_DIR / f"schema_{key}.txt"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")

    fallback = "de" if lang == "en" else "en"
    classes_q = f"""
SELECT ?class ?classLabel (COUNT(?x) AS ?n) WHERE {{
  ?x wdt:{INSTANCE_OF} ?class .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang},{fallback}". }}
}} GROUP BY ?class ?classLabel ORDER BY DESC(?n) LIMIT 60"""
    props_q = f"""
SELECT ?prop ?propLabel ?type (COUNT(?s) AS ?n) WHERE {{
  ?prop wikibase:directClaim ?wdt ; wikibase:propertyType ?type .
  ?s ?wdt ?o .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{lang},{fallback}". }}
}} GROUP BY ?prop ?propLabel ?type ORDER BY DESC(?n) LIMIT 150"""
    try:
        classes = qlever.run_query(classes_q, limit=None, timeout_s=300)
        props = qlever.run_query(props_q, limit=None, timeout_s=600)
    except qlever.QLeverError as e:
        return f"FEHLER: {e}"

    def clean(s: str) -> str:
        return re.sub(r"@\w+(-\w+)*$", "", s)

    out = [f"# FactGrid – Schemaüberblick (Instanz-Property: wdt:{INSTANCE_OF})", "",
           "## Häufigste Klassen (id  label  anzahl)"]
    out += [f"{r[0]}\t{clean(r[1])}\t{r[2]}" for r in classes.rows]
    out += ["", "## Meistgenutzte Properties (id  label  datentyp  verwendungen)"]
    out += [f"{r[0]}\t{clean(r[1])}\t{r[2].replace('wikibase:', '')}\t{r[3]}" for r in props.rows]
    out += ["", "## Prefixe", PREFIX_BLOCK]
    text = "\n".join(out)
    cache.write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------- #
@mcp.tool()
def get_wikibase_info(refresh: bool = False) -> str:
    """Welche Wikibase-Instanz hängt hier dran, von wann sind die Daten und wie groß sind sie.
    Wichtig zu wissen: abgefragt wird ein lokaler Spiegel (QLever-Index aus dem JSON-Dump),
    nicht die Live-Instanz – Änderungen nach dem Dump-Datum fehlen."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(qlever.ENDPOINT.encode()).hexdigest()[:12]
    cache = CACHE_DIR / f"info_{key}.txt"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")

    dump_date = "unbekannt"
    dump_file = Path(__file__).resolve().parents[2] / "qlever" / "DUMP_DATE"
    if dump_file.exists():
        dump_date = dump_file.read_text(encoding="utf-8").strip() or dump_date

    def count(where: str, timeout_s: int = 120) -> str:
        try:
            res = qlever.run_query(f"SELECT (COUNT(*) AS ?n) WHERE {{ {where} }}", limit=None,
                                   timeout_s=timeout_s)
            return res.rows[0][0] if res.rows else "?"
        except qlever.QLeverError as e:
            return f"? ({str(e).splitlines()[0]})"

    out = [
        "FactGrid (Wikibase) – lokaler Spiegel",
        f"  Basis-IRI:        {prefixes.BASE}",
        f"  Live-Instanz:     {prefixes.BASE} (nur für den Dump-Download; Fragen gehen NICHT dorthin)",
        f"  Abgefragt wird:   QLever unter {qlever.ENDPOINT}",
        f"  Datenstand:       Dump vom {dump_date}",
        f"  Instanz-Property: wdt:{INSTANCE_OF}",
        f"  Sprache/Deckel:   Default {qlever.DEFAULT_LANG}, max. {qlever.MAX_ROWS} Zeilen je Ergebnis",
        f"  Tripel:           {count('?s ?p ?o')}",
        f"  Items:            {count('?e a wikibase:Item')}",
        f"  Properties:       {count('?e a wikibase:Property')}",
        "  Hinweis:          Wertknoten-IRIs und Blank Nodes sind lokal berechnet und weichen",
        "                    von der Live-Instanz ab (s. README, Abschnitt 8).",
    ]
    text = "\n".join(out)
    cache.write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------- #
@mcp.prompt()
def sparql_guidelines() -> str:
    """Kurzanleitung für Text-zu-SPARQL gegen den lokalen FactGrid-Index."""
    return (
        "Du fragst einen lokalen QLever-Index von FactGrid (Wikibase) ab.\n"
        f"- Klassenzugehörigkeit: ?x wdt:{INSTANCE_OF} wd:Q… (Q7 = Mensch).\n"
        "- Erst search_entities / get_entity, dann sparql. Nie IDs raten.\n"
        "- Labels: SERVICE wikibase:label { bd:serviceParam wikibase:language \"de,en\". } oder "
        "OPTIONAL { ?x rdfs:label ?l FILTER(LANG(?l)=\"de\") }.\n"
        "- Zeitwerte sind xsd:dateTime; YEAR(?d) funktioniert. Koordinaten sind geo:wktLiteral.\n"
        "- Qualifikatoren: ?x p:P… ?st . ?st ps:P… ?v ; pq:P… ?q .\n"
        "- Immer LIMIT; bei Fehlermeldung Query korrigieren statt neu raten."
    )


def main() -> None:
    if "--http" in sys.argv:
        port = int(os.environ.get("FACTGRID_MCP_PORT", "8765"))
        mcp.run(transport="http", host="127.0.0.1", port=port, path="/mcp")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
