"""
factgrid-mcp – MCP-Server, der einem LLM den lokalen QLever-Index von FactGrid
zugänglich macht – und den lokalen Spiegel der MediaWiki-Datenbank (Bearbeitungsgeschichte).
Zehn Tools (jedes Tool kostet Kontextfenster, deshalb keins doppelt):

  sparql                 – SPARQL ausführen (WDQS-kompatibel: Label-Service wird umgeschrieben)
  search_entities        – Items/Properties per Label/Alias finden (Text → Q-/P-ID)
  get_entity             – Kompakte Sicht auf ein Item oder eine Property
  get_statement_values   – Alle Statements zu einem Paar Entität/Property, mit Wertknoten,
                           Qualifikatoren und Referenzen
  get_property_hierarchy – Baum entlang einer Property (Ober-/Unterbegriffe, Teil-von, …)
  schema_overview        – Häufigste Klassen und Properties (gecacht) als Orientierung
  get_wikibase_info      – Welche Instanz, welcher Stand, wie groß
  edit_history           – Wer hat wann welche Seite bearbeitet (MediaWiki-DB: revision/actor/…)
  mw_sql                 – SELECT gegen den MediaWiki-Spiegel (nur lesend, gedeckelt)
  mw_schema              – Tabellen, Views, Namensräume und Beispielabfragen des MediaWiki-Spiegels

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

if not __package__:  # direkt als Skript gestartet (python server.py)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "factgrid_mcp"

from .envfile import load_env  # noqa: E402

# .env im Projektverzeichnis (Zugangsdaten der MediaWiki-DB u. a.); bereits gesetzte Variablen –
# etwa aus .mcp.json – gewinnen. Muss vor dem Import von qlever/mwdb passieren.
load_env(Path(__file__).resolve().parents[2] / ".env")

from . import mwdb, prefixes, qlever  # noqa: E402
from .prefixes import PREFIX_BLOCK  # noqa: E402

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
        "ergänzt; SERVICE wikibase:label wird unterstützt. Immer LIMIT setzen. "
        "Bearbeitungsgeschichte (wer hat wann eine Seite bearbeitet, Benutzer, Logbuch): "
        "edit_history; Statistiken dazu per mw_schema + mw_sql (MediaWiki-Datenbank, nur lesend)."
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
    out += _mw_info_lines()
    text = "\n".join(out)
    cache.write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------- #
# MediaWiki-Datenbank: Bearbeitungsgeschichte (wer hat wann welche Seite bearbeitet)
# --------------------------------------------------------------------------- #
_PAGE_COLS = "page_id, page_namespace, page_title, page_is_redirect, page_latest, page_len"


def _find_page(text: str) -> dict | None:
    """Seite per Q-/P-ID oder Titel (mit/ohne Namensraum) finden; None wenn es sie nicht gibt."""
    ns, title = mwdb.split_title(text)
    if ns is not None:
        res = mwdb.query(f"SELECT {_PAGE_COLS} FROM page WHERE page_namespace = %s AND page_title = %s",
                         (ns, title), limit=1)
    else:  # Namensraum unbekannt: alle bekannten per Index durchprobieren, Hauptnamensraum zuerst
        mwdb.entity_namespace("Q")  # stellt sicher, dass Item-/Property-Namensraum bekannt sind
        mwdb.entity_namespace("P")
        known = sorted(mwdb.NAMESPACES)
        res = mwdb.query(f"SELECT {_PAGE_COLS} FROM page WHERE page_title = %s AND page_namespace IN "
                         f"({', '.join(['%s'] * len(known))}) ORDER BY page_namespace",
                         (title, *known), limit=1)
    if not res.rows:
        return None
    row = dict(zip(res.columns, res.rows[0]))
    for k in ("page_id", "page_namespace", "page_is_redirect", "page_latest", "page_len"):
        row[k] = int(row[k] or 0)
    return row


def _find_actor(name: str) -> dict | None:
    n = re.sub(r"^(User|Benutzer)\s*:\s*", "", name.strip(), flags=re.I).replace("_", " ").strip()
    if not n:
        return None
    n = n[0].upper() + n[1:]  # MediaWiki: erster Buchstabe groß
    res = mwdb.query("SELECT actor_id, actor_user, actor_name FROM actor WHERE actor_name = %s", (n,), limit=1)
    if not res.rows:  # Groß-/Kleinschreibung tolerieren (actor ist klein: ein paar hundert Zeilen)
        res = mwdb.query("SELECT actor_id, actor_user, actor_name FROM actor "
                         "WHERE LOWER(CONVERT(actor_name USING utf8mb4)) = LOWER(%s)", (n,), limit=2)
    if not res.rows:
        return None
    row = dict(zip(res.columns, res.rows[0]))
    row["actor_id"] = int(row["actor_id"])
    row["actor_user"] = int(row["actor_user"] or 0)
    return row


def _entity_labels(ids: list[str], lang: str) -> dict[str, str]:
    """{'Q7': 'Mensch', 'P2': 'Ist ein(e)'} aus dem Wikibase-Termspeicher (v_item_terms/v_property_terms)."""
    fallback = "de" if lang == "en" else "en"
    out: dict[str, str] = {}
    for kind, view, col in (("Q", "v_item_terms", "item_id"), ("P", "v_property_terms", "property_id")):
        nums = sorted({int(i[1:]) for i in ids if i.startswith(kind) and i[1:].isdigit()})
        if not nums:
            continue
        try:
            res = mwdb.query(
                f"SELECT {col}, language, text FROM {view} WHERE term_type = 'label' AND language IN (%s, %s) "
                f"AND {col} IN ({', '.join(['%s'] * len(nums))})", (lang, fallback, *nums), limit=None, timeout_s=20)
        except mwdb.MWDBError:
            return out
        for num, lg, text in res.rows:
            key = f"{kind}{num}"
            if lg == lang or key not in out:
                out[key] = text
    return out


def _entity_id(ns: int, title: str) -> str:
    if ns in (mwdb.entity_namespace("Q"), mwdb.entity_namespace("P")) and _ID_RE.match(title):
        return title
    return ""


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _edit_history(page: str, user: str, since: str, until: str, namespace: str,
                  limit: int, order: str, lang: str) -> str:
    page, user = page.strip(), user.strip()
    t_from, t_to = mwdb.parse_date(since), mwdb.parse_date(until, end=True)
    if not (page or user or t_from or t_to):
        return ("Erwartet page (Q-ID, P-ID oder Seitentitel) und/oder user (Benutzername), "
                "wahlweise mit since/until.")
    limit = max(1, min(limit, mwdb.MAX_ROWS))
    desc = (order or "desc").lower() != "asc"
    where, params, notes = [], [], []

    page_row = _find_page(page) if page else None
    if page and not page_row:
        return (f"Seite {page!r} nicht gefunden. Titel exakt angeben (Q-/P-IDs ohne Namensraum, sonst "
                f"z. B. 'FactGrid:Directory of Properties'); Datenstand ist der Dump, s. get_wikibase_info.")
    if page_row:
        where.append("r.rev_page = %s")
        params.append(page_row["page_id"])
    actor = _find_actor(user) if user else None
    if user and not actor:
        return (f"Benutzer {user!r} nicht gefunden (Schreibweise wie in FactGrid). Suche: "
                "mw_sql(\"SELECT actor_name FROM actor WHERE actor_name LIKE '%…%'\")")
    if actor:
        where.append("r.rev_actor = %s")
        params.append(actor["actor_id"])
    if t_from:
        where.append("r.rev_timestamp >= %s")
        params.append(t_from)
    if t_to:
        where.append("r.rev_timestamp <= %s")
        params.append(t_to)
    ns_filter = None
    if namespace and not page_row:
        ns_filter = mwdb.ns_id(namespace)
        if ns_filter is None:
            return f"Namensraum {namespace!r} unbekannt (z. B. Item, Property, FactGrid, User, Category oder Nummer)."
        where.append("p.page_namespace = %s")
        params.append(ns_filter)
    cond = " AND ".join(where) if where else "1=1"
    direction = "DESC" if desc else "ASC"

    res = mwdb.query(f"""
SELECT r.rev_id, r.rev_timestamp, a.actor_name, p.page_namespace, p.page_title, c.comment_text,
       r.rev_len, r.rev_minor_edit, r.rev_parent_id, r.rev_deleted,
       (SELECT pr.rev_len FROM revision pr WHERE pr.rev_id = r.rev_parent_id) AS parent_len
FROM revision r
JOIN page p ON p.page_id = r.rev_page
LEFT JOIN actor a ON a.actor_id = r.rev_actor
LEFT JOIN comment c ON c.comment_id = r.rev_comment_id
WHERE {cond}
ORDER BY r.rev_timestamp {direction}, r.rev_id {direction}""", params, limit=limit, timeout_s=90)

    show_page = page_row is None
    show_user = actor is None
    rows_out = []
    ids: list[str] = []
    for rev_id, ts, actor_name, ns, title, comment, rev_len, minor, parent_id, deleted, parent_len in res.rows:
        ns = int(ns)
        deleted = int(deleted or 0)
        who = "(versteckt)" if deleted & 4 else (actor_name or "(unbekannt)")
        what = "(versteckt)" if deleted & 2 else mwdb.humanize_comment(comment)
        if int(minor or 0):
            what = "[klein] " + what
        size = rev_len or "0"
        if not parent_id or parent_id in ("0", ""):
            diff = "neu"
        elif parent_len not in ("", None):
            diff = f"{int(size) - int(parent_len):+d}"
        else:
            diff = "?"
        cells = [mwdb.ts_to_iso(ts)]
        if show_user:
            cells.append(who)
        if show_page:
            cells.append(mwdb.full_title(ns, title))
            eid = _entity_id(ns, title)
            if eid:
                ids.append(eid)
        cells += [what, size, diff, rev_id]
        rows_out.append(cells)
    if show_page and ids:  # Labels der Items/Properties anhängen
        labels = _entity_labels(ids, lang)
        for cells in rows_out:
            eid = cells[2 if show_user else 1].split(":")[-1]
            if eid in labels:
                cells[2 if show_user else 1] += f" ({labels[eid]})"
    header = ["zeit_utc"] + (["benutzer"] if show_user else []) + (["seite"] if show_page else []) + \
             ["bearbeitung", "bytes", "diff", "rev_id"]
    out = mwdb.Result(columns=header, rows=rows_out, truncated=res.truncated).as_tsv()

    # Zusammenfassungen (nur wenn Seite oder Benutzer feststehen – sonst wäre COUNT über alles zu teuer)
    if page_row:
        notes += _page_summary(page_row, actor, t_from, t_to, lang)
    if actor:
        notes += _user_summary(actor, page_row, t_from, t_to)
    if not page_row and not actor:
        span = f"{mwdb.ts_to_iso(t_from) if t_from else '…'} bis {mwdb.ts_to_iso(t_to) if t_to else '…'}"
        notes.append(f"Zeitraum {span}" + (f", Namensraum {mwdb.ns_name(ns_filter) or 'Haupt'}" if ns_filter is not None else "")
                     + "; Gesamtzahlen dazu per mw_sql (COUNT auf revision).")
    notes.append("Zeiten in UTC; Datenstand = Datum des SQL-Dumps (get_wikibase_info).")
    return out + "\n" + "\n".join(notes)


def _page_summary(pg: dict, actor: dict | None, t_from: str, t_to: str, lang: str) -> list[str]:
    ns, title, pid = pg["page_namespace"], pg["page_title"], pg["page_id"]
    name = mwdb.full_title(ns, title)
    eid = _entity_id(ns, title)
    if eid:
        label = _entity_labels([eid], lang).get(eid)
        if label:
            name += f" ({label})"
    lines = []
    stats = mwdb.query("""
SELECT COUNT(*), MIN(rev_timestamp), MAX(rev_timestamp), COUNT(DISTINCT rev_actor)
FROM revision WHERE rev_page = %s""", (pid,), limit=None, timeout_s=60).rows[0]
    total, first, last, editors = int(stats[0]), stats[1], stats[2], int(stats[3])
    creator = mwdb.query("""
SELECT a.actor_name FROM revision r LEFT JOIN actor a ON a.actor_id = r.rev_actor
WHERE r.rev_page = %s ORDER BY r.rev_timestamp ASC, r.rev_id ASC""", (pid,), limit=1).rows
    top = mwdb.query("""
SELECT a.actor_name, COUNT(*) AS n FROM revision r LEFT JOIN actor a ON a.actor_id = r.rev_actor
WHERE r.rev_page = %s GROUP BY r.rev_actor ORDER BY n DESC""", (pid,), limit=5, timeout_s=60).rows
    line = (f"Seite: {name} · angelegt {mwdb.ts_to_iso(first)} von {creator[0][0] if creator and creator[0][0] else '?'}"
            f" · {_plural(total, 'Bearbeitung', 'Bearbeitungen')} von {_plural(editors, 'Benutzer', 'Benutzern')}")
    if top:
        line += " (" + ", ".join(f"{n or '?'} {c}" for n, c in top) + (", …" if editors > len(top) else "") + ")"
    line += f" · letzte {mwdb.ts_to_iso(last)} · aktuell {pg['page_len']} Bytes (rev {pg['page_latest']})"
    if pg["page_is_redirect"]:
        rd = mwdb.query("SELECT rd_namespace, rd_title FROM redirect WHERE rd_from = %s", (pid,), limit=1).rows
        if rd:
            line += f" · WEITERLEITUNG auf {mwdb.full_title(int(rd[0][0]), rd[0][1])} (zusammengeführt)"
    lines.append(line)
    if actor or t_from or t_to:
        lines.append("(Die Tabelle oben ist gefiltert; die Zahlen in dieser Zeile gelten für die ganze Seite.)")
    try:
        logs = mwdb.query("""
SELECT log_timestamp, log_type, log_action, user_name, comment FROM v_log
WHERE log_page = %s OR (log_namespace = %s AND log_title = %s)
ORDER BY log_timestamp""", (pid, ns, title), limit=10, timeout_s=30)
        for ts, ltype, action, who, comment in logs.rows:
            c = mwdb.humanize_comment(comment)
            lines.append(f"Logbuch: {mwdb.ts_to_iso(ts)} {ltype}/{action} von {who or '?'}" + (f" – {c}" if c else ""))
    except mwdb.MWDBError:
        pass
    return lines


def _user_summary(actor: dict, pg: dict | None, t_from: str, t_to: str) -> list[str]:
    line = f"Benutzer: {actor['actor_name']}"
    if actor["actor_user"]:
        u = mwdb.query("SELECT user_registration, user_editcount FROM user WHERE user_id = %s",
                       (actor["actor_user"],), limit=1).rows
        groups = mwdb.query("SELECT GROUP_CONCAT(ug_group ORDER BY ug_group SEPARATOR ', ') FROM user_groups "
                            "WHERE ug_user = %s", (actor["actor_user"],), limit=1).rows
        if u:
            reg = u[0][0] or "?"
            line += f" (Konto-ID {actor['actor_user']}, registriert {reg[:10] if reg != '?' else reg}"
            line += f", {u[0][1] or 0} Bearbeitungen laut Konto)"
        if groups and groups[0][0]:
            line += f" · Gruppen: {groups[0][0]}"
    else:
        line += " (kein Konto – IP oder Systemakteur)"
    where, params = ["rev_actor = %s"], [actor["actor_id"]]
    if pg:
        where.append("rev_page = %s")
        params.append(pg["page_id"])
    if t_from:
        where.append("rev_timestamp >= %s")
        params.append(t_from)
    if t_to:
        where.append("rev_timestamp <= %s")
        params.append(t_to)
    st = mwdb.query(f"SELECT COUNT(*), MIN(rev_timestamp), MAX(rev_timestamp), COUNT(DISTINCT rev_page) "
                    f"FROM revision WHERE {' AND '.join(where)}", params, limit=None, timeout_s=90).rows[0]
    scope = "auf dieser Seite" if pg else "insgesamt"
    if t_from or t_to:
        scope += " im Zeitraum"
    line += (f" · {scope}: {_plural(int(st[0]), 'Bearbeitung', 'Bearbeitungen')}"
             + (f" auf {_plural(int(st[3]), 'Seite', 'Seiten')}" if not pg else "")
             + (f", erste {mwdb.ts_to_iso(st[1])}, letzte {mwdb.ts_to_iso(st[2])}" if st[1] else ""))
    return [line]


@mcp.tool()
def edit_history(page: str = "", user: str = "", since: str = "", until: str = "",
                 namespace: str = "", limit: int = 30, order: str = "desc", lang: str = "de") -> str:
    """Wer hat wann welche Seite bearbeitet – aus der MediaWiki-Datenbank von FactGrid (Versionen,
    Benutzer, Kommentare, Logbuch). page: Q-/P-ID oder Seitentitel (Q409, P2, "FactGrid:Directory
    of Properties") → alle Versionen der Seite mit Zeit, Benutzer, Art der Änderung. user:
    Benutzername → dessen Bearbeitungen. Beides kombinierbar. since/until: 2024 | 2024-05 |
    2024-05-17 (UTC). namespace: Item | Property | FactGrid | … (ohne page). order: desc | asc.
    Namen vorher mit search_entities in Q-IDs auflösen. Statistiken (pro Monat, aktivste
    Benutzer, meistbearbeitete Seiten): mw_schema, dann mw_sql."""
    try:
        return _edit_history(page, user, since, until, namespace, limit, order, lang)
    except mwdb.MWDBError as e:
        return f"FEHLER: {e}"


@mcp.tool()
def mw_sql(query: str, limit: int = 50, timeout_s: int = 30) -> str:
    """SELECT gegen den lokalen Spiegel der MediaWiki-Datenbank von FactGrid (MariaDB): Versionen,
    Logbuch, Benutzer, Labels. Views: v_revision (rev_id, page_namespace, page_title, rev_timestamp,
    user_name, comment, rev_len, rev_parent_id), v_log (log_type, log_action, log_timestamp,
    user_name, log_namespace, log_title, comment), v_item_terms (item_id, term_type, language,
    text). Tabellen: page, revision, actor, comment, logging, user, user_groups, change_tag.
    Zeitstempel: Strings 'YYYYMMDDHHMMSS' (UTC); Items: page_namespace 120, page_title 'Q7'.
    Vorher mw_schema lesen. Nur lesend; TSV, max. 200 Zeilen; bei FEHLER Query korrigieren."""
    try:
        res = mwdb.query(query, limit=limit, timeout_s=timeout_s)
    except mwdb.MWDBError as e:
        return f"FEHLER: {e}"
    return res.as_tsv()


_TABLE_NOTES = {
    "page": "eine Zeile je Seite; page_latest = aktuelle rev_id, page_len = Größe in Bytes",
    "revision": "eine Zeile je Version; rev_page → page, rev_actor → actor, rev_comment_id → comment; rev_parent_id = 0 bei Anlage",
    "actor": "Bearbeiter (actor_name = Benutzername oder IP); actor_user → user.user_id",
    "comment": "Bearbeitungskommentare; Wikibase-Autokommentare wie '/* wbsetclaim-create:2||1 */ [[Property:P2]]: …'",
    "logging": "Logbuch: log_type/log_action (create, delete, move, protect, rights, newusers, …), log_page/log_title",
    "user": "Benutzerkonten – nur user_id, user_name, user_registration, user_editcount (keine Passwörter/E-Mails)",
    "user_groups": "Gruppen (sysop, bot, bureaucrat, …) je user_id",
    "change_tag": "Markierungen je Version (ct_rev_id) → change_tag_def.ctd_name (z. B. Tool-Tags)",
    "redirect": "Weiterleitungen (zusammengeführte Items): rd_from = page_id → rd_namespace/rd_title",
    "wb_items_per_site": "Sitelinks der Items (ips_item_id numerisch, ips_site_id, ips_site_page)",
    "wb_property_info": "Datentyp je Property (pi_property_id numerisch, pi_type)",
    "page_props": "Seiteneigenschaften, u. a. wb-claims / wb-sitelinks / wb-identifiers = Anzahl je Item",
    "categorylinks": "Kategorien von Wiki-Seiten (cl_from = page_id, cl_to = Kategorie)",
    "site_stats": "Gesamtzahlen des Wikis (ss_total_edits, ss_total_pages, ss_users, …)",
    "mw_meta": "Herkunft des Spiegels (dump_file, dump_date, loaded_at, geladene/ausgelassene Tabellen)",
}
_VIEW_NOTES = {
    "v_revision": "revision ⋈ page ⋈ actor ⋈ comment: rev_id, page_namespace, page_title, rev_timestamp, user_name, user_id, comment, rev_len, rev_parent_id, rev_minor_edit",
    "v_log": "logging ⋈ actor ⋈ comment: log_id, log_type, log_action, log_timestamp, user_name, log_namespace, log_title, log_page, comment, log_params",
    "v_item_terms": "Labels/Beschreibungen/Aliase der Items: item_id (7 für Q7), term_type (label|description|alias), language, text",
    "v_property_terms": "dasselbe für Properties: property_id (2 für P2), term_type, language, text",
}
_EXAMPLES = [
    ("Alle Versionen eines Items (neueste zuerst)",
     "SELECT rev_timestamp, user_name, comment, rev_len FROM v_revision WHERE page_namespace = 120 AND page_title = 'Q7' ORDER BY rev_timestamp DESC LIMIT 20"),
    ("Aktivste Benutzer eines Jahres",
     "SELECT user_name, COUNT(*) AS n FROM v_revision WHERE rev_timestamp BETWEEN '20240101000000' AND '20241231235959' GROUP BY user_name ORDER BY n DESC LIMIT 10"),
    ("Bearbeitungen je Monat",
     "SELECT LEFT(rev_timestamp, 6) AS monat, COUNT(*) AS n FROM revision WHERE rev_timestamp >= '20240101000000' GROUP BY monat ORDER BY monat"),
    ("Wer hat die meisten Items angelegt",
     "SELECT user_name, COUNT(*) AS n FROM v_revision WHERE rev_parent_id = 0 AND page_namespace = 120 GROUP BY user_name ORDER BY n DESC LIMIT 10"),
    ("Label eines Items / Item zu einem Label",
     "SELECT item_id, language, text FROM v_item_terms WHERE term_type = 'label' AND text = 'Johann Wolfgang von Goethe'"),
    ("Logbuch: Löschungen und Verschiebungen",
     "SELECT log_timestamp, log_type, log_action, user_name, log_namespace, log_title, comment FROM v_log WHERE log_type IN ('delete', 'move') ORDER BY log_timestamp DESC LIMIT 20"),
]


@mcp.tool()
def mw_schema(refresh: bool = False) -> str:
    """Struktur des MediaWiki-Spiegels für mw_sql: Datenstand, Konventionen (Zeitstempel,
    Namensräume, Titel), Views und Tabellen mit Spalten und Zeilenzahlen, Logbuch-Typen,
    Markierungen und Beispielabfragen. Einmal pro Sitzung vor mw_sql lesen (gecacht)."""
    try:
        meta = dict(mwdb.query("SELECT k, v FROM mw_meta", limit=None).rows)
    except mwdb.MWDBError as e:
        return f"FEHLER: {e}"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{mwdb.HOST}|{mwdb.NAME}|{meta.get('dump_file')}|{meta.get('loaded_at')}".encode()).hexdigest()[:12]
    cache = CACHE_DIR / f"mwschema_{key}.txt"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")
    try:
        text = _build_mw_schema(meta)
    except mwdb.MWDBError as e:
        return f"FEHLER: {e}"
    cache.write_text(text, encoding="utf-8")
    return text


def _build_mw_schema(meta: dict) -> str:
    q_ns, p_ns = mwdb.entity_namespace("Q"), mwdb.entity_namespace("P")
    out = [f"# MediaWiki-Datenbank von FactGrid – lokaler Spiegel (Dump vom {meta.get('dump_date', '?')}, "
           f"geladen {meta.get('loaded_at', '?')}, Datenbank {mwdb.NAME})", "",
           "Nur lesend (SELECT). Konventionen:",
           "- Zeitstempel: Strings 'YYYYMMDDHHMMSS' in UTC – als String vergleichen "
           "(rev_timestamp >= '20240101000000'), Monat = LEFT(rev_timestamp, 6), "
           "Umwandlung STR_TO_DATE(rev_timestamp, '%Y%m%d%H%i%s').",
           "- Titel: page_title ohne Namensraum-Präfix, Leerzeichen als '_', Groß-/Kleinschreibung exakt.",
           f"- Entitäten: Q7 → page_namespace = {q_ns} AND page_title = 'Q7'; P2 → page_namespace = {p_ns} "
           "AND page_title = 'P2'. Labels über v_item_terms (item_id = 7) / v_property_terms (property_id = 2).",
           "- Bearbeiter: revision.rev_actor → actor.actor_name (oder direkt v_revision.user_name). "
           "Bots/Tools erkennt man an user_groups (bot) oder change_tag.",
           "- Große Aggregationen über die ganze revision-Tabelle dauern bis zu einer Minute: timeout_s erhöhen, "
           "Zeiträume eingrenzen.", ""]
    # Namensräume mit Seitenzahlen
    ns_rows = mwdb.query("SELECT page_namespace, COUNT(*) AS n FROM page GROUP BY page_namespace ORDER BY n DESC",
                         limit=None, timeout_s=120).rows
    out.append("## Namensräume (page_namespace = Name: Seiten)")
    out.append(", ".join(f"{mwdb.ns_name(int(ns)) or 'Haupt'} = {ns}: {n}" for ns, n in ns_rows))
    out.append("")
    # Views
    views = [r[0] for r in mwdb.query("SHOW FULL TABLES WHERE Table_type = 'VIEW'", limit=None).rows]
    out.append("## Views (bevorzugt verwenden)")
    for v in views:
        out.append(f"{v}: {_VIEW_NOTES.get(v, '')}".rstrip(": "))
    out.append("")
    # Tabellen mit Spalten und Zeilenzahlen
    cols: dict[str, list[str]] = {}
    for t, c in mwdb.query(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION", (mwdb.NAME,), limit=None).rows:
        cols.setdefault(t, []).append(c)
    counts = dict(mwdb.query(
        "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
        (mwdb.NAME,), limit=None).rows)
    out.append("## Tabellen (Zeilen): Spalten")
    for t in sorted(counts, key=lambda t: (t not in _TABLE_NOTES, t)):
        note = _TABLE_NOTES.get(t, "")
        out.append(f"{t} ({counts[t]}): {', '.join(cols.get(t, []))}" + (f" — {note}" if note else ""))
    out.append("")
    # Logbuch-Typen und Markierungen
    try:
        lt = mwdb.query("SELECT log_type, log_action, COUNT(*) AS n FROM logging GROUP BY log_type, log_action "
                        "ORDER BY n DESC", limit=20, timeout_s=120).rows
        out.append("## Logbuch (log_type/log_action: Einträge)")
        out.append(", ".join(f"{t}/{a}: {n}" for t, a, n in lt))
    except mwdb.MWDBError as e:
        out.append(f"(Logbuch nicht abfragbar: {str(e).splitlines()[0]})")
    try:
        tags = mwdb.query("SELECT ctd_name, ctd_count FROM change_tag_def ORDER BY ctd_count DESC", limit=15).rows
        if tags:
            out.append("## Markierungen (change_tag_def: Versionen)")
            out.append(", ".join(f"{t}: {n}" for t, n in tags))
    except mwdb.MWDBError:
        pass
    out.append("")
    out.append("## Beispiele")
    for title, sql in _EXAMPLES:
        out.append(f"- {title}:\n  {sql}")
    skipped = meta.get("tables_skipped", "")
    if skipped:
        out += ["", "Nicht im Spiegel (privat oder Ballast): " + skipped]
    return "\n".join(out)


def _mw_info_lines() -> list[str]:
    """Zeilen für get_wikibase_info über den MediaWiki-Spiegel."""
    if not mwdb.configured():
        return ["  MediaWiki-DB:     nicht konfiguriert (make mwdb lädt den SQL-Dump; s. README 3.7)"]
    try:
        meta = dict(mwdb.query("SELECT k, v FROM mw_meta", limit=None, timeout_s=10).rows)
        n_rev = mwdb.scalar("SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'revision'",
                            (mwdb.NAME,))
        n_page = mwdb.scalar("SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'page'",
                             (mwdb.NAME,))
        n_user = mwdb.scalar("SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'user'",
                             (mwdb.NAME,))
    except mwdb.MWDBError as e:
        return [f"  MediaWiki-DB:     nicht erreichbar ({str(e).splitlines()[0][:120]})"]
    lines = [
        f"  MediaWiki-DB:     {mwdb.NAME} (MariaDB) – Dump vom {meta.get('dump_date', '?')}, geladen {meta.get('loaded_at', '?')}",
        f"                    {n_rev} Versionen, {n_page} Seiten, {n_user} Benutzerkonten; Tools: edit_history, mw_sql, mw_schema",
        "                    Ohne Seiteninhalte (text) und ohne private Tabellen (Passwörter, E-Mails, IPs, Beobachtungslisten).",
    ]
    if meta.get("with_text") == "1":
        lines[2] = "                    Mit Seiteninhalten (text/content/slots); ohne private Tabellen."
    return lines


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
        "- Immer LIMIT; bei Fehlermeldung Query korrigieren statt neu raten.\n"
        "- Bearbeitungsgeschichte (wer/wann/Benutzer/Logbuch) kommt aus der MediaWiki-DB: "
        "edit_history(page=Q…|user=…), Statistiken über mw_schema + mw_sql."
    )


def main() -> None:
    if "--http" in sys.argv:
        port = int(os.environ.get("FACTGRID_MCP_PORT", "8765"))
        mcp.run(transport="http", host="127.0.0.1", port=port, path="/mcp")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
