"""
mwdb.py – Zugriff auf den lokalen Spiegel der MediaWiki-Datenbank von FactGrid (MariaDB).

Der Spiegel entsteht aus dem monatlichen SQL-Dump (scripts/mw_load.py) und enthält nur die
öffentlichen Tabellen der Bearbeitungsgeschichte: page, revision, actor, comment, logging,
user (ohne Passwörter/E-Mails) usw. – siehe README, Abschnitt 3.7. Hier stehen:

  * die Verbindung (nur lesend: eigener DB-Benutzer mit SELECT, READ-ONLY-Transaktion,
    Zeitlimit pro Statement, Zeilendeckel)
  * der Schutz vor schreibenden SQL-Statements
  * Hilfen für MediaWiki-Eigenheiten: Zeitstempel "YYYYMMDDHHMMSS", Namensräume,
    Seitentitel mit Unterstrichen, Wikibase-Autokommentare (/* wbsetclaim-create:1| */ …)

Konfiguration über Umgebungsvariablen (siehe .env.example):
  MW_DB_HOST (127.0.0.1) MW_DB_PORT (3306) MW_DB_NAME (factgrid_mw)
  MW_DB_USER (factgrid_ro) MW_DB_PASSWORD  MW_DB_TIMEOUT (60 s)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

HOST = os.environ.get("MW_DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("MW_DB_PORT", "3306"))
NAME = os.environ.get("MW_DB_NAME", "factgrid_mw")
USER = os.environ.get("MW_DB_USER", "factgrid_ro")
PASSWORD = os.environ.get("MW_DB_PASSWORD", "")
DEFAULT_TIMEOUT = int(os.environ.get("MW_DB_TIMEOUT", "60"))
MAX_ROWS = int(os.environ.get("FACTGRID_MAX_ROWS", "200"))
SITE_NAME = os.environ.get("MW_SITE_NAME", "FactGrid")  # Name des Projekt-Namensraums (ns 4)

# Kanonische MediaWiki-Namensräume (englische Namen, wie FactGrid sie benutzt). Die Wikibase-
# Namensräume Item/Property werden beim ersten Zugriff aus der page-Tabelle bestimmt, weil sie
# je Installation verschieden sein können (Wikidata: 0/120, wikibase-docker: 120/122).
NAMESPACES: dict[int, str] = {
    0: "", 1: "Talk", 2: "User", 3: "User talk", 4: SITE_NAME, 5: f"{SITE_NAME} talk",
    6: "File", 7: "File talk", 8: "MediaWiki", 9: "MediaWiki talk", 10: "Template",
    11: "Template talk", 12: "Help", 13: "Help talk", 14: "Category", 15: "Category talk",
    120: "Item", 121: "Item talk", 122: "Property", 123: "Property talk",
    146: "Lexeme", 147: "Lexeme talk", 828: "Module", 829: "Module talk", 2300: "Gadget", 2301: "Gadget talk",
    2302: "Gadget definition", 2303: "Gadget definition talk",
}
_ENTITY_RE = re.compile(r"^([QP])\d+$")
_ID_NS: dict[str, int] = {}  # "Q" → Item-Namensraum, "P" → Property-Namensraum (per DB ermittelt)

# Nur lesende Statements; alles andere wird schon vor dem Verbindungsaufbau abgewiesen
_READ_RE = re.compile(r"^\s*(SELECT|WITH|SHOW|EXPLAIN|DESCRIBE|DESC)\b", re.I)
_FORBIDDEN_RE = re.compile(
    r"\b(INTO\s+(OUTFILE|DUMPFILE)|LOAD_FILE|INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|"
    r"TRUNCATE|RENAME|GRANT|REVOKE|LOCK|UNLOCK|CALL|HANDLER|SET\s+(GLOBAL|SESSION|@@)|"
    r"KILL|SHUTDOWN|FLUSH|RESET|PURGE|BENCHMARK|SLEEP)\b", re.I)


class MWDBError(RuntimeError):
    pass


@dataclass
class Result:
    columns: list[str]
    rows: list[list[str]]
    truncated: bool = False
    meta: dict = field(default_factory=dict)

    def as_tsv(self, max_cell: int = 300, footer: str = "") -> str:
        def cell(v: str) -> str:
            v = v.replace("\t", " ").replace("\r", "").replace("\n", " ")
            return v if len(v) <= max_cell else v[: max_cell - 1] + "…"

        lines = ["\t".join(self.columns)]
        lines += ["\t".join(cell(c) for c in r) for r in self.rows]
        tail = f"({len(self.rows)} Zeilen" + (
            ", abgeschnitten – LIMIT erhöhen, eingrenzen oder aggregieren)" if self.truncated else ")")
        return "\n".join(lines) + "\n" + tail + (("\n" + footer) if footer else "")


# --------------------------------------------------------------------------- #
# Hilfen für MediaWiki-Eigenheiten
# --------------------------------------------------------------------------- #
def decode(v) -> str:
    """MediaWiki speichert Text in BINARY-Spalten; pymysql liefert dafür bytes."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", errors="replace")
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


_TS_RE = re.compile(r"^\d{14}$")


def ts_to_iso(ts: str) -> str:
    """'20180112134423' → '2018-01-12 13:44:23' (andere Werte unverändert)."""
    ts = decode(ts)
    if _TS_RE.match(ts):
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
    return ts


def parse_date(text: str, end: bool = False) -> str:
    """Datumsangabe eines Nutzers → MediaWiki-Zeitstempel (14 Ziffern).
    Erlaubt: 2024 | 2024-05 | 2024-05-17 | 2024-05-17 13:00[:00] | 20240517130000.
    end=True liefert das Ende des angegebenen Zeitraums (für 'bis')."""
    t = (text or "").strip()
    if not t:
        return ""
    if _TS_RE.match(t):
        return t
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?(?:[ T](\d{1,2})(?::(\d{2}))?(?::(\d{2}))?)?Z?$", t)
    if not m:
        raise MWDBError(f"Datum nicht verstanden: {text!r} (erwartet z. B. 2024, 2024-05, 2024-05-17)")
    y, mo, d, h, mi, s = m.groups()
    if not end:
        return f"{int(y):04d}{int(mo or 1):02d}{int(d or 1):02d}{int(h or 0):02d}{int(mi or 0):02d}{int(s or 0):02d}"
    if mo is None:
        return f"{int(y):04d}1231235959"
    if d is None:
        # letzter Tag des Monats
        mo_i = int(mo)
        nxt = datetime(int(y) + (mo_i == 12), 1 if mo_i == 12 else mo_i + 1, 1)
        last = (nxt.toordinal() - 1)
        return datetime.fromordinal(last).strftime("%Y%m%d") + "235959"
    if h is None:
        return f"{int(y):04d}{int(mo):02d}{int(d):02d}235959"
    return f"{int(y):04d}{int(mo):02d}{int(d):02d}{int(h):02d}{int(mi or 59):02d}{int(s or 59):02d}"


def ns_name(ns: int) -> str:
    return NAMESPACES.get(int(ns), f"ns{ns}")


def ns_id(name: str) -> int | None:
    """'Item' → 120, 'item_talk' → 121, '4' → 4, '' → 0; None wenn unbekannt."""
    n = (name or "").strip().replace("_", " ")
    if n.lstrip("-").isdigit():
        return int(n)
    low = n.lower()
    for k, v in NAMESPACES.items():
        if v.lower() == low:
            return k
    aliases = {"main": 0, "project": 4, "image": 6, "wp": 4, "": 0}
    return aliases.get(low)


def full_title(ns: int, title: str) -> str:
    """(120, 'Q7') → 'Item:Q7'; (0, 'Main_Page') → 'Main Page'."""
    t = decode(title).replace("_", " ")
    n = ns_name(ns)
    return f"{n}:{t}" if n else t


def split_title(text: str) -> tuple[int | None, str]:
    """Nutzereingabe → (Namensraum-ID oder None, DB-Titel mit Unterstrichen).
    'Q7' / 'Item:Q7' → (Item-ns, 'Q7'); 'P2' → (Property-ns, 'P2');
    'FactGrid:Directory of Properties' → (4, 'Directory_of_Properties'); 'Main Page' → (None, 'Main_Page')."""
    t = (text or "").strip().replace("_", " ")
    t = re.sub(r"\s+", " ", t)
    # Volle URL oder wd:-Prefix zulassen
    m = re.search(r"/(?:wiki|entity)/([^/?#]+)$", t)
    if m:
        t = m.group(1)
    t = re.sub(r"^wd:", "", t)
    if _ENTITY_RE.match(t.upper()):
        eid = t.upper()
        return entity_namespace(eid[0]), eid
    if ":" in t:
        prefix, rest = t.split(":", 1)
        ns = ns_id(prefix)
        if ns is not None:
            rest = rest.strip()
            if _ENTITY_RE.match(rest.upper()) and ns in (entity_namespace("Q"), entity_namespace("P")):
                rest = rest.upper()
            return ns, _db_title(rest)
    return None, _db_title(t)


def _db_title(t: str) -> str:
    t = t.strip().replace(" ", "_")
    return (t[0].upper() + t[1:]) if t else t  # MediaWiki: erster Buchstabe groß


def entity_namespace(kind: str) -> int:
    """Namensraum der Items ('Q') bzw. Properties ('P') – aus der page-Tabelle ermittelt."""
    kind = kind.upper()
    if kind not in _ID_NS:
        default = {"Q": 120, "P": 122}[kind]
        try:
            res = query(
                "SELECT page_namespace, COUNT(*) AS n FROM page WHERE page_title REGEXP %s "
                "GROUP BY page_namespace ORDER BY n DESC LIMIT 3", (f"^{kind}[0-9]+$",), limit=3)
            ns = int(res.rows[0][0]) if res.rows else default
        except MWDBError:
            ns = default
        _ID_NS[kind] = ns
        if ns not in NAMESPACES:
            NAMESPACES[ns] = "Item" if kind == "Q" else "Property"
            NAMESPACES[ns + 1] = NAMESPACES[ns] + " talk"
    return _ID_NS[kind]


# Wikibase-Autokommentare: "/* wbsetclaim-create:2||1 */ [[Property:P2]]: [[Item:Q7]]"
_AUTOCOMMENT_RE = re.compile(r"^/\*\s*([\w-]+)(?::([^*]*?))?\s*\*/\s*(.*)$", re.S)
AUTOCOMMENT_DE = {
    "wbeditentity-create": "Eintrag angelegt",
    "wbeditentity-create-item": "Item angelegt",
    "wbeditentity-create-property": "Property angelegt",
    "wbeditentity-update": "Eintrag bearbeitet",
    "wbeditentity-update-languages": "Labels/Beschreibungen geändert",
    "wbeditentity-update-languages-short": "Labels/Beschreibungen geändert",
    "wbeditentity-update-languages-and-other": "Labels/Beschreibungen u. a. geändert",
    "wbeditentity-update-languages-and-other-short": "Labels/Beschreibungen u. a. geändert",
    "wbeditentity-override": "Eintrag überschrieben",
    "wbsetlabel-set": "Label gesetzt", "wbsetlabel-add": "Label hinzugefügt", "wbsetlabel-remove": "Label entfernt",
    "wbsetdescription-set": "Beschreibung gesetzt", "wbsetdescription-add": "Beschreibung hinzugefügt",
    "wbsetdescription-remove": "Beschreibung entfernt",
    "wbsetaliases-set": "Aliase gesetzt", "wbsetaliases-add": "Aliase hinzugefügt",
    "wbsetaliases-remove": "Aliase entfernt", "wbsetaliases-update": "Aliase geändert",
    "wbsetaliases-add-remove": "Aliase geändert",
    "wbsetlabeldescriptionaliases": "Label/Beschreibung/Aliase geändert",
    "wbsetsitelink-add": "Sitelink hinzugefügt", "wbsetsitelink-set": "Sitelink geändert",
    "wbsetsitelink-remove": "Sitelink entfernt", "wbsetsitelink-add-both": "Sitelink hinzugefügt",
    "wbsetsitelink-set-badges": "Sitelink-Badges geändert",
    "wbcreateclaim-create": "Aussage angelegt", "wbsetclaim-create": "Aussage angelegt",
    "wbsetclaim-update": "Aussage geändert", "wbsetclaim-update-qualifiers": "Qualifikatoren geändert",
    "wbsetclaim-update-references": "Referenzen geändert", "wbsetclaim-update-rank": "Rang geändert",
    "wbsetclaimvalue": "Wert der Aussage geändert", "wbsetstatementrank": "Rang geändert",
    "wbsetstatementrank-deprecated": "Rang: veraltet", "wbsetstatementrank-normal": "Rang: normal",
    "wbsetstatementrank-preferred": "Rang: bevorzugt",
    "wbremoveclaims-remove": "Aussage entfernt", "wbremoveclaims": "Aussagen entfernt",
    "wbsetqualifier-add": "Qualifikator hinzugefügt", "wbsetqualifier-update": "Qualifikator geändert",
    "wbremovequalifiers-remove": "Qualifikator entfernt",
    "wbsetreference-add": "Referenz hinzugefügt", "wbsetreference-set": "Referenz geändert",
    "wbremovereferences-remove": "Referenz entfernt",
    "wbmergeitems-from": "zusammengeführt (Quelle)", "wbmergeitems-to": "zusammengeführt (Ziel)",
    "wbcreateredirect": "Weiterleitung angelegt (Zusammenführung)",
    "wblinktitles-create": "Sitelinks verknüpft", "wblinktitles-connect": "Sitelinks verknüpft",
    "special-create-item": "Item angelegt (Spezialseite)", "special-create-property": "Property angelegt",
    "clientsitelink-update": "Sitelink (Client) geändert", "clientsitelink-remove": "Sitelink (Client) entfernt",
    "undo": "Bearbeitung rückgängig", "restore": "Version wiederhergestellt",
    "wbsetentity": "Entität gesetzt",
}
_LINK_RE = re.compile(r"\[\[(?:Property|Item|Lexeme):([QPL]\d+)(?:\|[^\]]*)?\]\]")


def humanize_comment(comment: str) -> str:
    """Wikibase-Autokommentar → lesbare Kurzform: 'Aussage angelegt: P2: Q7 (1 Aussage)'.
    Normale Bearbeitungskommentare bleiben unverändert (Wikilinks werden gekürzt)."""
    c = decode(comment).strip()
    if not c:
        return ""
    m = _AUTOCOMMENT_RE.match(c)
    if not m:
        return _LINK_RE.sub(r"\1", c)
    key, args, rest = m.group(1), (m.group(2) or "").strip(), m.group(3).strip()
    label = AUTOCOMMENT_DE.get(key)
    if label is None:  # unbekannter Schlüssel: wenigstens die Aktion nennen
        label = key.replace("wb", "", 1) if key.startswith("wb") else key
    # Format "schlüssel:anzahl|sprache|…" – die Zahl ist die Zahl der Summary-Argumente (für
    # Pluralformen), die Sprache steht bei Label/Beschreibung/Aliasen
    parts = args.split("|")
    lang = parts[1].strip() if len(parts) > 1 else ""
    out = label + (f" [{lang}]" if lang else "")
    rest = _LINK_RE.sub(r"\1", rest)
    if rest:
        out += ": " + rest
    return out


# --------------------------------------------------------------------------- #
# Verbindung und Abfrage
# --------------------------------------------------------------------------- #
def configured() -> bool:
    return bool(PASSWORD or os.environ.get("MW_DB_PASSWORD"))


def _connect(timeout_s: int):
    try:
        import pymysql
    except ImportError as e:  # pragma: no cover
        raise MWDBError("pymysql fehlt (cd mcp && uv sync --all-extras)") from e
    if not configured():
        raise MWDBError("MediaWiki-Datenbank nicht konfiguriert: MW_DB_PASSWORD fehlt in .env "
                        "(make mwdb lädt den Dump und legt den Lesebenutzer an).")
    try:
        conn = pymysql.connect(
            host=HOST, port=PORT, user=USER, password=PASSWORD or os.environ.get("MW_DB_PASSWORD", ""),
            database=NAME, charset="utf8mb4", connect_timeout=5, read_timeout=timeout_s + 5,
            autocommit=True, binary_prefix=True,
        )
    except Exception as e:
        raise MWDBError(f"MediaWiki-Datenbank nicht erreichbar ({USER}@{HOST}:{PORT}/{NAME}): {e}") from e
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute("SET SESSION max_statement_time = %s", (float(timeout_s),))
        cur.execute("SET SESSION sql_select_limit = %s", (MAX_ROWS * 50,))  # Notbremse hinter dem LIMIT
    return conn


def guard(sql: str) -> str:
    """Nur ein lesendes Statement; Kommentare entfernt; abschließendes Semikolon weg."""
    q = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    q = re.sub(r"(?m)(--|#)[^\n]*$", "", q).strip().rstrip(";").strip()
    if not q:
        raise MWDBError("Leere Abfrage.")
    if ";" in _without_strings(q):
        raise MWDBError("Nur ein Statement pro Aufruf.")
    if not _READ_RE.match(q):
        raise MWDBError("Nur lesende Abfragen (SELECT/WITH/SHOW/EXPLAIN/DESCRIBE) sind erlaubt.")
    if _FORBIDDEN_RE.search(_without_strings(q)):
        raise MWDBError("Abgewiesen: die Abfrage enthält ein schreibendes oder administratives Schlüsselwort.")
    return q


def _without_strings(q: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`', '""', q)


_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)(?:\s*,\s*(\d+))?(\s+OFFSET\s+\d+)?\s*$", re.I)


def apply_limit(q: str, limit: int | None) -> str:
    """Deckelt/ergänzt LIMIT bei SELECT/WITH (cap + 1, damit Abschneiden erkennbar ist)."""
    if limit is None or not re.match(r"^\s*(SELECT|WITH)\b", q, re.I):
        return q
    cap = min(max(1, limit), MAX_ROWS) + 1
    m = _LIMIT_RE.search(q)
    if m:
        if m.group(2):  # LIMIT offset, count
            if int(m.group(2)) > cap:
                q = q[: m.start()] + f"LIMIT {m.group(1)}, {cap}"
        elif int(m.group(1)) > cap:
            q = q[: m.start()] + f"LIMIT {cap}" + (m.group(3) or "")
        return q
    return q + f"\nLIMIT {cap}"


def query(sql: str, params: tuple | list | None = None, limit: int | None = 50,
          timeout_s: int = DEFAULT_TIMEOUT, raw: bool = False) -> Result:
    """Lesende Abfrage; Zellen als Text (bytes dekodiert). limit=None: kein Deckel (intern)."""
    q = guard(sql)
    q = apply_limit(q, limit)
    timeout_s = max(1, min(int(timeout_s), 600))
    conn = _connect(timeout_s)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(q, params or None)
            except Exception as e:
                raise MWDBError(f"SQL-Fehler: {e}\n--- gesendete Abfrage ---\n{q}") from e
            cols = [d[0] for d in (cur.description or [])]
            data = cur.fetchall()
    finally:
        conn.close()
    truncated = False
    if limit is not None:
        cap = min(max(1, limit), MAX_ROWS)
        if len(data) > cap:
            truncated, data = True, data[:cap]
    if raw:
        rows = [list(r) for r in data]
    else:
        rows = [[_pretty(cols[i], v) for i, v in enumerate(r)] for r in data]
    return Result(columns=cols, rows=rows, truncated=truncated)


_TS_COL_RE = re.compile(r"(timestamp|_touched|_registration|_time|_expiry|_expires|_updated)$", re.I)


def _pretty(col: str, v) -> str:
    s = decode(v)
    if _TS_COL_RE.search(col) and _TS_RE.match(s):
        return ts_to_iso(s)
    return s


def scalar(sql: str, params=None, timeout_s: int = DEFAULT_TIMEOUT) -> str:
    res = query(sql, params, limit=None, timeout_s=timeout_s)
    return res.rows[0][0] if res.rows and res.rows[0] else ""


def sql_string(v: str) -> str:
    """Manuelles Escaping für Fälle ohne Platzhalter (selten)."""
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
