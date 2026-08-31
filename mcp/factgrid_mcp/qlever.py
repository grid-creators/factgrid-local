"""Schlanker Client für den QLever-SPARQL-Endpunkt (SPARQL 1.1 Protocol)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import httpx

from .labelservice import rewrite_label_service, strip_wdqs_specifics
from .prefixes import inject_prefixes, shorten

ENDPOINT = os.environ.get("QLEVER_ENDPOINT", "http://localhost:7003")
ACCESS_TOKEN = os.environ.get("QLEVER_ACCESS_TOKEN", "")          # = ACCESS_TOKEN im Qleverfile
SERVER_TIMEOUT = int(os.environ.get("QLEVER_SERVER_TIMEOUT", "120"))  # = TIMEOUT im Qleverfile (Sekunden)
DEFAULT_LANG = os.environ.get("FACTGRID_LANG", "de")
MAX_ROWS = int(os.environ.get("FACTGRID_MAX_ROWS", "200"))
DEFAULT_TIMEOUT = int(os.environ.get("FACTGRID_TIMEOUT", "60"))

# Update-Schlüsselwörter kommen in keiner lesenden Query vor (außer in Strings/Kommentaren, die vorher entfernt werden)
_UPDATE_RE = re.compile(r"(?<![?$:\w])(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)\b", re.I)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)(\s+OFFSET\s+\d+)?\s*$", re.I)
_TRAILING_VALUES_RE = re.compile(r"\bVALUES\s*(?:\?\w+|\([^)]*\))\s*\{[^}]*\}\s*$", re.I | re.S)
_SELECT_RE = re.compile(r"\bSELECT\b", re.I)


def strip_comments(q: str) -> str:
    """Entfernt #-Kommentare außerhalb von String-Literalen und <IRIs>."""
    out, i, n = [], 0, len(q)
    while i < n:
        c = q[i]
        if c in "\"'":
            j = i + 1
            while j < n and q[j] != c:
                j += 2 if q[j] == "\\" else 1
            out.append(q[i:j + 1]); i = j + 1
        elif c == "<":
            j = q.find(">", i)
            if j < 0 or "\n" in q[i:j]:  # kein IRI (z. B. Vergleich a < b)
                out.append(c); i += 1
            else:
                out.append(q[i:j + 1]); i = j + 1
        elif c == "#":
            j = q.find("\n", i)
            i = n if j < 0 else j
        else:
            out.append(c); i += 1
    return "".join(out)


def _without_strings(q: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', q)


class QLeverError(RuntimeError):
    pass


@dataclass
class Result:
    columns: list[str]
    rows: list[list[str]]
    truncated: bool = False
    query_sent: str = ""
    meta: dict = field(default_factory=dict)

    def as_tsv(self, max_cell: int = 200) -> str:
        def cell(v: str) -> str:
            v = v.replace("\t", " ").replace("\n", " ")
            return v if len(v) <= max_cell else v[: max_cell - 1] + "…"

        lines = ["\t".join(self.columns)]
        lines += ["\t".join(cell(c) for c in r) for r in self.rows]
        tail = f"\n({len(self.rows)} Zeilen" + (", abgeschnitten – LIMIT erhöhen oder Frage eingrenzen)" if self.truncated else ")")
        return "\n".join(lines) + tail


def prepare(query: str, limit: int | None, default_lang: str = DEFAULT_LANG) -> str:
    """WDQS-Query → QLever-Query: Prefixe, Label-Service, LIMIT-Deckel."""
    q = strip_comments(strip_wdqs_specifics(query)).strip()
    q = re.sub(r"\n[ \t]*\n+", "\n", q)  # Leerzeilen (spart Kontext)
    if _UPDATE_RE.search(_without_strings(q)):
        raise QLeverError("Nur lesende Abfragen (SELECT/ASK/CONSTRUCT/DESCRIBE) sind erlaubt.")
    q = rewrite_label_service(q, default_lang)
    q = inject_prefixes(q)
    if limit is not None and _SELECT_RE.search(q):
        cap = min(limit, MAX_ROWS) + 1  # +1, um Abschneiden zu erkennen
        m = _LIMIT_RE.search(q)
        if m:
            if int(m.group(1)) > cap:
                q = q[: m.start()] + f"LIMIT {cap}" + (m.group(2) or "")
        else:
            q = q.rstrip().rstrip(";")
            tv = _TRAILING_VALUES_RE.search(q)  # LIMIT gehört vor einen abschließenden VALUES-Block
            q = (q[: tv.start()].rstrip() + f"\nLIMIT {cap}\n" + q[tv.start():]) if tv else q + f"\nLIMIT {cap}"
    return q


def _term(binding: dict | None) -> str:
    if not binding:
        return ""
    t = binding.get("type")
    v = binding.get("value", "")
    if t == "uri":
        return shorten(v)
    if t == "bnode":
        return "_:" + v
    lang = binding.get("xml:lang")
    dt = binding.get("datatype")
    if lang:
        return f"{v}@{lang}"
    if dt and not dt.endswith(("#string", "#integer", "#decimal", "#double", "#boolean", "#dateTime", "#date", "#int", "#long")):
        return f"{v}^^{shorten(dt)}"
    return v


def run_query(query: str, limit: int | None = 50, timeout_s: int = DEFAULT_TIMEOUT,
              endpoint: str = ENDPOINT, default_lang: str = DEFAULT_LANG) -> Result:
    q = prepare(query, limit, default_lang)
    headers = {"Accept": "application/sparql-results+json"}
    # QLever: bei urlencoded POST müssen ALLE Parameter im Body stehen (URL-Parameter → Fehler);
    # ein timeout über dem Server-Default ist nur mit access-token erlaubt (sonst 403).
    if not ACCESS_TOKEN:
        timeout_s = min(timeout_s, SERVER_TIMEOUT)
    data = {"query": q, "timeout": f"{timeout_s}s"}
    if ACCESS_TOKEN:
        data["access-token"] = ACCESS_TOKEN
    try:
        r = httpx.post(endpoint, data=data, headers=headers, timeout=timeout_s + 5)
    except httpx.HTTPError as e:
        raise QLeverError(f"QLever nicht erreichbar unter {endpoint}: {e}") from e
    if r.status_code != 200:
        msg = r.text
        try:
            j = r.json()
            msg = j.get("exception") or j.get("error") or msg
        except Exception:
            pass
        raise QLeverError(f"QLever-Fehler ({r.status_code}): {msg}\n--- gesendete Query ---\n{q}")
    data = r.json()
    if "boolean" in data:  # ASK
        return Result(columns=["boolean"], rows=[[str(data["boolean"]).lower()]], query_sent=q)
    cols = data.get("head", {}).get("vars", [])
    bindings = data.get("results", {}).get("bindings", [])
    truncated = False
    if limit is not None:
        cap = min(limit, MAX_ROWS)
        if len(bindings) > cap:
            truncated = True
            bindings = bindings[:cap]
    rows = [[_term(b.get(c)) for c in cols] for b in bindings]
    return Result(columns=cols, rows=rows, truncated=truncated, query_sent=q)


def ping(endpoint: str = ENDPOINT) -> str:
    try:
        r = httpx.get(endpoint, params={"cmd": "stats"}, timeout=10)
        r.raise_for_status()
        return r.text
    except httpx.HTTPError as e:
        raise QLeverError(f"QLever nicht erreichbar unter {endpoint}: {e}") from e
