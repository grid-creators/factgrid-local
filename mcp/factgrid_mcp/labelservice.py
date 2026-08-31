"""
Ersetzt den Blazegraph-spezifischen Label-Service des Wikibase-Query-Service

    SERVICE wikibase:label { bd:serviceParam wikibase:language "[AUTO_LANGUAGE],de,en". }

durch Standard-SPARQL, das QLever versteht:

    OPTIONAL { ?x rdfs:label ?xLabel__de . FILTER(LANG(?xLabel__de) = "de") }
    OPTIONAL { ?x rdfs:label ?xLabel__en . FILTER(LANG(?xLabel__en) = "en") }
    BIND(COALESCE(?xLabel__de, ?xLabel__en) AS ?xLabel)

Unterstützt wird die implizite Form (Variablen ?xLabel / ?xDescription / ?xAltLabel
werden automatisch erkannt) und die explizite Form mit Tripeln im SERVICE-Block
(?x rdfs:label ?name . ?x schema:description ?desc .). Die erzeugten Muster werden
ans Ende der umschließenden Gruppe gesetzt, damit die Bindung von ?x bereits
vorliegt (Left-Join-Reihenfolge).
"""
from __future__ import annotations

import re

DEFAULT_LANGS = ["de", "en"]

_SERVICE_RE = re.compile(r"SERVICE\s+wikibase:label\s*\{", re.I)
_LANG_RE = re.compile(r'wikibase:language\s+"([^"]*)"', re.I)
_EXPLICIT_RE = re.compile(
    r"\?(\w+)\s+(rdfs:label|schema:description|skos:altLabel)\s+\?(\w+)\s*[.;]?", re.I
)
_IMPLICIT_RE = re.compile(r"\?(\w+?)(Label|Description|AltLabel)\b")

PRED_FOR_SUFFIX = {"Label": "rdfs:label", "Description": "schema:description", "AltLabel": "skos:altLabel"}


class _Group:
    __slots__ = ("open", "close", "subquery")

    def __init__(self, open_idx: int):
        self.open = open_idx
        self.close = -1
        self.subquery = False


def _groups(text: str) -> list[_Group]:
    """Alle {…}-Gruppen (ignoriert Strings/Kommentare); markiert Subqueries ({ SELECT …})."""
    groups: list[_Group] = []
    stack: list[_Group] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "{":
            g = _Group(i)
            g.subquery = bool(re.match(r"\s*SELECT\b", text[i + 1:i + 40], re.I))
            groups.append(g)
            stack.append(g)
        elif c == "}":
            if not stack:
                raise ValueError("Unbalancierte Klammern im SPARQL-Text")
            stack.pop().close = i
        i += 1
    if stack:
        raise ValueError("Unbalancierte Klammern im SPARQL-Text")
    return groups


def _innermost(groups: list[_Group], pos: int) -> _Group | None:
    cands = [g for g in groups if g.open < pos < g.close]
    return max(cands, key=lambda g: g.open) if cands else None


def _matching_brace(text: str, open_idx: int) -> int:
    for g in _groups(text):
        if g.open == open_idx:
            return g.close
    raise ValueError("Unbalancierte Klammern im SPARQL-Text")


_KW_BEFORE_RE = re.compile(r"(OPTIONAL|MINUS|EXISTS|UNION|GRAPH\s+\S+|SERVICE\s+\S+)\s*$", re.I)
_UNION_AFTER_RE = re.compile(r"^\s*UNION\b", re.I)


def _kind(text: str, g: _Group) -> str:
    """optional | minus | exists | union | subquery | plain"""
    if g.subquery:
        return "subquery"
    before = text[: g.open].rstrip()
    if _UNION_AFTER_RE.match(text[g.close + 1:]) or before.upper().endswith("UNION"):
        return "union"
    m = _KW_BEFORE_RE.search(before)
    if m:
        kw = m.group(1).split()[0].upper()
        return {"OPTIONAL": "optional", "MINUS": "minus", "EXISTS": "exists"}.get(kw, "plain")
    return "plain"


def _parent(groups: list[_Group], g: _Group) -> _Group | None:
    return _innermost(groups, g.open)


def _union_branches(text: str, groups: list[_Group], g: _Group) -> list[_Group]:
    """Alle Zweige der UNION-Kette, zu der g gehört (in Textreihenfolge)."""
    by_close = {x.close: x for x in groups}
    by_open = {x.open: x for x in groups}
    chain = [g]
    cur = g  # nach links
    while True:
        before = text[: cur.open].rstrip()
        if not before.upper().endswith("UNION"):
            break
        prev_close = before[: -len("UNION")].rstrip()
        prev = by_close.get(len(prev_close) - 1)
        if not prev:
            break
        chain.insert(0, prev)
        cur = prev
    cur = g  # nach rechts
    while True:
        m = _UNION_AFTER_RE.match(text[cur.close + 1:])
        if not m:
            break
        nxt_open = cur.close + 1 + m.end()
        while nxt_open < len(text) and text[nxt_open].isspace():
            nxt_open += 1
        nxt = by_open.get(nxt_open)
        if not nxt:
            break
        chain.append(nxt)
        cur = nxt
    return chain


def _insertion_points(text: str, service_pos: int, var: str) -> list[int]:
    """
    Wo müssen die Label-Muster für ?var hin? Grundregel: an das Ende der Gruppe, in der ?var
    gebunden wird – sonst entsteht bei ungebundenem ?var ein Kreuzprodukt mit allen Labels.
      * innerhalb OPTIONAL / GRAPH / einfacher Untergruppe: dort hinein
      * innerhalb MINUS / (NOT) EXISTS / Subquery: nach außen wandern (BIND wäre dort unsichtbar)
      * UNION: in jeden Zweig, der ?var erwähnt
    Fallback: die Gruppe, in der der SERVICE-Block stand.
    """
    groups = _groups(text)
    enclosing = _innermost(groups, service_pos)
    if enclosing is None:
        raise ValueError("SERVICE wikibase:label außerhalb einer Gruppe")
    var_re = re.compile(rf"\?{re.escape(var)}\b")
    seen_any = False
    for m in var_re.finditer(text):
        p = m.start()
        if not (enclosing.open < p < enclosing.close):
            continue
        seen_any = True
        # Vorkommen innerhalb MINUS / (NOT) EXISTS / Subquery bindet ?var außen nicht → überspringen
        chain, g = [], _innermost(groups, p)
        while g is not None and g is not enclosing:
            chain.append(g)
            g = _parent(groups, g)
        kinds = [_kind(text, a) for a in chain]
        if any(k in ("minus", "exists") for k in kinds):
            continue
        target: _Group | None = chain[0] if chain else None
        if "subquery" in kinds:
            # Subquery projiziert ?var nach außen → Muster in die Gruppe, die die (äußerste) Subquery enthält
            top = max(i for i, k in enumerate(kinds) if k == "subquery")
            target = chain[top + 1] if top + 1 < len(chain) else None
        if target is None:
            return [enclosing.close]
        if _kind(text, target) == "union":
            return [b.close for b in _union_branches(text, groups, target) if var_re.search(text[b.open:b.close])]
        return [target.close]
    # ?var nur in SELECT/ORDER BY oder Subquery-Projektion → Bindung kommt von außen/innen: ans Ende;
    # ?var ausschließlich in MINUS/EXISTS → nie gebunden: keine Label-Muster erzeugen
    return [] if seen_any else [enclosing.close]


def _langs(block: str, default_lang: str) -> list[str]:
    m = _LANG_RE.search(block)
    raw = m.group(1) if m else ",".join(DEFAULT_LANGS)
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.upper() == "[AUTO_LANGUAGE]":
            part = default_lang
        if part not in out:
            out.append(part)
    return out or [default_lang]


def _build_pattern(subj: str, pred: str, target: str, langs: list[str]) -> str:
    lines = []
    tmp_vars = []
    for lang in langs:
        tmp = f"?{target}__{re.sub(r'[^A-Za-z0-9]', '_', lang)}"
        tmp_vars.append(tmp)
        lines.append(f'  OPTIONAL {{ ?{subj} {pred} {tmp} . FILTER(LANG({tmp}) = "{lang}") }}')
    if pred == "rdfs:label":
        # WDQS-Verhalten: ohne Label die ID (Q…/P…) bzw. bei Literalen den Wert selbst zeigen
        tmp_vars.append(f'IF(isIRI(?{subj}), STRAFTER(STR(?{subj}), "/entity/"), STR(?{subj}))')
    if len(tmp_vars) == 1:
        lines.append(f"  BIND({tmp_vars[0]} AS ?{target})")
    else:
        lines.append(f"  BIND(COALESCE({', '.join(tmp_vars)}) AS ?{target})")
    return "\n".join(lines)


def rewrite_label_service(query: str, default_lang: str = "de") -> str:
    """Gibt die Query ohne SERVICE wikibase:label zurück (idempotent, wenn kein Service vorhanden)."""
    while True:
        m = _SERVICE_RE.search(query)
        if not m:
            return query
        open_idx = m.end() - 1
        close_idx = _matching_brace(query, open_idx)
        block = query[open_idx + 1:close_idx]
        langs = _langs(block, default_lang)

        # SERVICE-Block entfernen
        without = query[:m.start()] + query[close_idx + 1:]
        service_pos = m.start()

        explicit = _EXPLICIT_RE.findall(block)
        if explicit:
            mappings = [(s, p, t) for s, p, t in explicit]
        else:
            all_vars = set(re.findall(r"\?(\w+)", without))
            mappings, seen = [], set()
            for base, suffix in _IMPLICIT_RE.findall(without):
                target = base + suffix
                if base in all_vars and target not in seen:
                    seen.add(target)
                    mappings.append((base, PRED_FOR_SUFFIX[suffix], target))

        # Ersatzmuster einfügen – je Variable an das Ende der Gruppe, in der sie gebunden wird
        for subj, pred, target in mappings:
            pattern = _build_pattern(subj, pred, target, langs)
            # von hinten nach vorn einfügen, damit frühere Positionen gültig bleiben
            for end in sorted(_insertion_points(without, service_pos, subj), reverse=True):
                head = without[:end].rstrip()
                insert = "\n" + pattern + "\n"
                without = head + insert + without[end:]
                if end <= service_pos:
                    service_pos += len(head) + len(insert) - end
        query = without


# Kleine Zusatzbereinigungen für WDQS-Queries
def strip_wdqs_specifics(query: str) -> str:
    q = re.sub(r"^\s*#defaultView:[^\n]*\n?", "", query, flags=re.M)
    # bd:serviceParam außerhalb des Label-Service (z. B. mwapi) wird nicht unterstützt
    return q
