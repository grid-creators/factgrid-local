#!/usr/bin/env python3
"""
ui_prefixes.py – trägt die FactGrid-Prefixe in die Konfiguration der QLever-UI ein.

Die UI schlägt beim Tippen nur Prefixe vor, die unter `suggestedPrefixes` stehen, und ergänzt
auch nur die die PREFIX-Zeilen einer Query (`fillPrefixes`). Ohne diesen Schritt scheitert dort
jede Query mit „Prefix wd was not registered" – während der MCP-Server sie automatisch ergänzt.
Quelle ist derselbe Namensraum-Katalog wie im MCP-Server (mcp/factgrid_mcp/prefixes.py).

    python3 scripts/ui_prefixes.py qlever/Qleverfile-ui.yml     # danach: make ui (importiert neu)

Die Datei entsteht beim ersten `qlever ui`; fehlt sie noch, meldet das Skript das und tut nichts.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from factgrid_mcp.prefixes import PREFIXES  # noqa: E402

SKIP = {"bd"}  # nur für das Parsen alter WDQS-Queries; in der UI ein irreführender Vorschlag

# Beispielabfragen für das "Examples"-Menü. Jede bringt ihre PREFIX-Zeilen mit – die UI ergänzt
# sie nur beim Übernehmen eines Autocomplete-Vorschlags, nicht beim Ausführen einer getippten
# Query. Achtung: SERVICE wikibase:label gibt es hier nicht (das schreibt nur der MCP-Server um),
# Labels deshalb per OPTIONAL + FILTER(LANG(...)).
EXAMPLES: list[tuple[str, str]] = [
    ("Startvorlage mit allen Prefixen", """%PREFIXES%
# wd:Q7 = Mensch, wdt:P2 = "Ist ein(e)". Labels gibt es nur per OPTIONAL,
# SERVICE wikibase:label funktioniert im QLever nicht.
SELECT ?item ?itemLabel WHERE {
  ?item wdt:P2 wd:Q7 .
  OPTIONAL { ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "de") }
}
LIMIT 10"""),
    ("Personen zählen", """%PREFIXES%
SELECT (COUNT(?person) AS ?anzahl) WHERE {
  ?person wdt:P2 wd:Q7 .
}"""),
    ("Häufigste Klassen", """%PREFIXES%
SELECT ?klasse ?klasseLabel (COUNT(?x) AS ?anzahl) WHERE {
  ?x wdt:P2 ?klasse .
  OPTIONAL { ?klasse rdfs:label ?klasseLabel . FILTER(LANG(?klasseLabel) = "de") }
}
GROUP BY ?klasse ?klasseLabel
ORDER BY DESC(?anzahl)
LIMIT 20"""),
    ("Alle Aussagen zu einem Item (Goethe, Q409)", """%PREFIXES%
SELECT ?prop ?propLabel ?wert WHERE {
  wd:Q409 ?wdtProp ?wert .
  ?prop wikibase:directClaim ?wdtProp .
  OPTIONAL { ?prop rdfs:label ?propLabel . FILTER(LANG(?propLabel) = "de") }
}
LIMIT 100"""),
    ("Unterklassen einer Klasse (wdt:P3)", """%PREFIXES%
SELECT ?unterklasse ?unterklasseLabel WHERE {
  ?unterklasse wdt:P3 wd:Q7 .
  OPTIONAL { ?unterklasse rdfs:label ?unterklasseLabel . FILTER(LANG(?unterklasseLabel) = "de") }
}
LIMIT 100"""),
    ("Statement mit Qualifikatoren (Geburtsdatum, P77)", """%PREFIXES%
SELECT ?wert ?qualProp ?qualPropLabel ?qualWert WHERE {
  wd:Q409 p:P77 ?statement .
  ?statement ps:P77 ?wert .
  OPTIONAL {
    ?statement ?pq ?qualWert .
    ?qualProp wikibase:qualifier ?pq .
    OPTIONAL { ?qualProp rdfs:label ?qualPropLabel . FILTER(LANG(?qualPropLabel) = "de") }
  }
}
LIMIT 20"""),
]

# Nur diese Prefixe in die Beispiele schreiben – der volle Katalog wäre 29 Zeilen Rauschen.
EXAMPLE_PREFIXES = ["wd", "wdt", "p", "ps", "psv", "pq", "wikibase", "rdfs", "xsd"]


def example_query(query: str) -> str:
    block = "\n".join(f"PREFIX {p}: <{PREFIXES[p]}>" for p in EXAMPLE_PREFIXES)
    return query.replace("%PREFIXES%", block)


def prefix_lines() -> list[str]:
    return [f"@prefix {p}: <{ns}> ." for p, ns in PREFIXES.items() if p not in SKIP]


def examples_block(indent: str) -> str:
    """YAML-Liste für das Examples-Menü: name, sort_key, query (Blockskalar)."""
    out = []
    for i, (name, query) in enumerate(EXAMPLES, 1):
        out.append(f'{indent}  - name: "{name}"\n')
        out.append(f"{indent}    sort_key: '{i}'\n")
        out.append(f"{indent}    query: |-\n")
        out += [f"{indent}      {line}\n".rstrip() + "\n" for line in example_query(query).splitlines()]
    return "".join(out)


def patch(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^(?P<indent>[ ]*)suggestedPrefixes: \|-\n(?P<body>(?:(?P=indent)[ ]{2}.*\n)*)",
                  text, re.M)
    if not m:
        print(f"{path}: Feld 'suggestedPrefixes' nicht gefunden – Konfiguration unverändert.")
        return 1
    body_indent = m.group("indent") + "  "
    new_body = "".join(body_indent + line + "\n" for line in prefix_lines())
    changed = m.group("body") != new_body
    text = text[:m.start("body")] + new_body + text[m.end("body"):]

    e = re.search(r"^(?P<indent>[ ]*)examples:(?:[ ]*\[\])?[ ]*\n"
                  r"(?P<body>(?:(?P=indent)[ ].*\n|[ ]*\n)*)", text, re.M)
    if not e:
        print(f"{path}: Feld 'examples' nicht gefunden – nur die Prefixe geschrieben.")
    else:
        indent = e.group("indent")
        block = f"{indent}examples:\n" + examples_block(indent)   # ersetzt auch ein "examples: []"
        changed = changed or text[e.start():e.end()] != block
        text = text[:e.start()] + block + text[e.end():]

    if not changed:
        print(f"{path}: Prefixe ({len(prefix_lines())}) und Beispiele ({len(EXAMPLES)}) sind aktuell.")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"{path}: {len(prefix_lines())} Prefixe (wd, wdt, p, ps, pq, …) und {len(EXAMPLES)} "
          f"Beispielabfragen eingetragen. Mit 'make ui' neu importieren.")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "qlever" / "Qleverfile-ui.yml"
    if not target.exists():
        print(f"{target} gibt es noch nicht – sie entsteht beim ersten 'make ui'; "
              f"danach 'make ui' erneut aufrufen, dann stehen die Prefixe drin.")
        sys.exit(0)
    sys.exit(patch(target))
