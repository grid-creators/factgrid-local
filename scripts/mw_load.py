#!/usr/bin/env python3
"""
mw_load.py – lädt den monatlichen MediaWiki-SQL-Dump von FactGrid in die lokale MariaDB,
und zwar nur die ÖFFENTLICHEN Tabellen der Bearbeitungsgeschichte (wer hat wann welche
Seite bearbeitet, Logbuch, Benutzerkonten ohne Geheimnisse, Wikibase-Terme).

    python3 scripts/mw_load.py                     # neuesten Dump aus MW_DUMP_DIR laden
    python3 scripts/mw_load.py --list              # nur Tabellen und Größen anzeigen (7 min)
    python3 scripts/mw_load.py --dump pfad.sql.gz  # bestimmten Dump laden
    python3 scripts/mw_load.py --with-text         # zusätzlich text/content/slots (≈ 160 GB!)

Ablauf: pigz -dc dump | (Filter: nur erlaubte Tabellen, ENGINE=Aria) | mariadb <db>_new
→ Bereinigung (user-Spalten, unterdrückte Einträge, Kommentare gelöschter Seiten)
→ Meta-Tabelle → atomarer Tausch nach <db> → Views → Lesebenutzer (SELECT-only), dessen
Passwort in .env (MW_DB_PASSWORD) steht bzw. dort eingetragen wird → Aufräumen: ältere *.sql.gz
im Dump-Verzeichnis werden gelöscht, nur der neueste bleibt (--keep N behält N, --keep 0 alle).

Nicht geladen werden Tabellen mit privaten Daten (user_password/E-Mail, account_*,
oauth_*, bot_passwords, watchlist, user_properties, recentchanges mit IPs, archive =
gelöschte Versionen, echo_*, block/ipblocks …) und Ballast (objectcache, l10n_cache,
searchindex, querycache*, job, pagelinks …). Der Dump selbst bleibt unverändert.

Die Tabellen bekommen ENGINE=Aria (nicht transaktional) und nur normale Sekundärindizes
(UNIQUE → KEY): der Spiegel ist read-only und wird monatlich neu gebaut; so lädt Aria mit
DISABLE/ENABLE KEYS in einem Rutsch und braucht wenig RAM.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOADER_VERSION = "1"

# --------------------------------------------------------------------------- #
# Tabellenauswahl (Allowlist – alles andere bleibt draußen, auch neue Tabellen)
# --------------------------------------------------------------------------- #
PUBLIC_TABLES = [
    # Seiten und Versionen
    "page", "revision", "actor", "comment", "redirect", "page_props", "page_restrictions",
    "protected_titles", "category", "categorylinks", "image",
    # Logbuch und Markierungen
    "logging", "change_tag", "change_tag_def",
    # Benutzerkonten (user wird auf öffentliche Spalten reduziert, s. USER_KEEP)
    "user", "user_groups", "user_former_groups",
    # Wikibase: Terme (Labels/Beschreibungen/Aliase), Sitelinks, Property-Typen, Constraints
    "wbt_item_terms", "wbt_property_terms", "wbt_term_in_lang", "wbt_text_in_lang", "wbt_text",
    "wbt_type", "wb_items_per_site", "wb_property_info", "wb_id_counters", "wbqc_constraints",
    # Kleinkram
    "site_stats", "sites", "site_identifiers", "interwiki",
]
# Nur temporär geladen, um Kommentare gelöschter Seiten aus `comment` zu entfernen
TEMP_TABLES = ["archive"]
# Seiteninhalte (alle Versionen als JSON): optional, ≈ 160 GB
TEXT_TABLES = ["text", "content", "slots", "content_models", "slot_roles"]

USER_KEEP = ["user_id", "user_name", "user_registration", "user_editcount", "user_is_temp"]
PRIVATE_LOG_TYPES = ["suppress", "oath", "spamblacklist", "titleblacklist",
                     "abusefilterprivatedetails", "checkuser-private-event",
                     "checkuser-temporary-account"]

VIEWS = {
    "v_revision": """
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_revision AS
SELECT r.rev_id, r.rev_page, p.page_namespace, p.page_title, r.rev_timestamp,
       r.rev_actor, a.actor_name AS user_name, a.actor_user AS user_id,
       r.rev_comment_id, c.comment_text AS comment,
       r.rev_len, r.rev_parent_id, r.rev_minor_edit, r.rev_deleted
FROM revision r
JOIN page p ON p.page_id = r.rev_page
LEFT JOIN actor a ON a.actor_id = r.rev_actor
LEFT JOIN comment c ON c.comment_id = r.rev_comment_id""",
    "v_log": """
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_log AS
SELECT l.log_id, l.log_type, l.log_action, l.log_timestamp,
       l.log_actor, a.actor_name AS user_name, a.actor_user AS user_id,
       l.log_namespace, l.log_title, l.log_page, c.comment_text AS comment, l.log_params
FROM logging l
LEFT JOIN actor a ON a.actor_id = l.log_actor
LEFT JOIN comment c ON c.comment_id = l.log_comment_id""",
    "v_item_terms": """
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_item_terms AS
SELECT it.wbit_item_id AS item_id, ty.wby_name AS term_type, xl.wbxl_language AS language,
       x.wbx_text AS text
FROM wbt_item_terms it
JOIN wbt_term_in_lang tl ON tl.wbtl_id = it.wbit_term_in_lang_id
JOIN wbt_type ty ON ty.wby_id = tl.wbtl_type_id
JOIN wbt_text_in_lang xl ON xl.wbxl_id = tl.wbtl_text_in_lang_id
JOIN wbt_text x ON x.wbx_id = xl.wbxl_text_id""",
    "v_property_terms": """
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_property_terms AS
SELECT pt.wbpt_property_id AS property_id, ty.wby_name AS term_type, xl.wbxl_language AS language,
       x.wbx_text AS text
FROM wbt_property_terms pt
JOIN wbt_term_in_lang tl ON tl.wbtl_id = pt.wbpt_term_in_lang_id
JOIN wbt_type ty ON ty.wby_id = tl.wbtl_type_id
JOIN wbt_text_in_lang xl ON xl.wbxl_id = tl.wbtl_text_in_lang_id
JOIN wbt_text x ON x.wbx_id = xl.wbxl_text_id""",
}

_TABLE_RE = re.compile(rb"^-- Table structure for table `([^`]+)`")
_SECTION_SKIP = (b"-- Temporary table structure", b"-- Final view structure",
                 b"-- Dumping routines", b"-- Dumping events")
_ENGINE_RE = re.compile(rb"\)\s*ENGINE=\w+")
_INNODB_OPTS_RE = re.compile(rb"\s*(ROW_FORMAT=\w+|KEY_BLOCK_SIZE=\d+)")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- #
# MariaDB-Client (Admin-Zugang, Standard: `mariadb` als root über den Unix-Socket)
# --------------------------------------------------------------------------- #
class Admin:
    def __init__(self, cmd: str):
        self.cmd = cmd.split()

    def run(self, sql: str, db: str | None = None, capture: bool = True) -> str:
        args = self.cmd + ["--batch", "--skip-column-names", "--default-character-set=utf8mb4"]
        if db:
            args += ["--database", db]
        p = subprocess.run(args, input=sql.encode("utf-8"), stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise SystemExit(f"MariaDB-Fehler bei:\n{sql[:500]}\n{p.stderr.decode(errors='replace')}")
        return p.stdout.decode("utf-8", errors="replace") if capture else ""

    def rows(self, sql: str, db: str | None = None) -> list[list[str]]:
        out = self.run(sql, db)
        return [l.split("\t") for l in out.splitlines() if l.strip()]

    def scalar(self, sql: str, db: str | None = None) -> str:
        r = self.rows(sql, db)
        return r[0][0] if r and r[0] else ""


# --------------------------------------------------------------------------- #
# Dump streamen und filtern
# --------------------------------------------------------------------------- #
def find_dump(path: str | None, dump_dir: str) -> Path:
    if path:
        p = Path(path)
        if not p.exists():
            raise SystemExit(f"Dump nicht gefunden: {p}")
        return p
    cands = sorted(Path(dump_dir).glob("*.sql.gz"), key=lambda p: (p.stat().st_mtime, p.name))
    if not cands:
        raise SystemExit(f"Kein *.sql.gz in {dump_dir} (MW_DUMP_DIR)")
    return cands[-1]


def dump_date(p: Path) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
    return m.group(1) if m else time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))


def decompressor(p: Path) -> list[str]:
    if p.suffix != ".gz":
        return ["cat", str(p)]
    return [shutil.which("pigz") or "gzip", "-dc", str(p)]


def stream_dump(dump: Path, include: set[str], sink, engine: str) -> dict[str, int]:
    """Schreibt die erlaubten Tabellenabschnitte nach sink (None = nur zählen).
    Liefert die (unkomprimierten) Bytes je Tabelle – auch der übersprungenen."""
    sizes: dict[str, int] = {}
    src = subprocess.Popen(decompressor(dump), stdout=subprocess.PIPE, bufsize=1 << 22)
    assert src.stdout is not None
    state: str | None = None   # None = Kopf (durchreichen), "" = überspringen, sonst Tabellenname
    include_current = True
    t0 = last = time.time()
    total = 0
    try:
        for line in src.stdout:
            total += len(line)
            if line.startswith(b"-- "):
                m = _TABLE_RE.match(line)
                if m:
                    name = m.group(1).decode()
                    state, include_current = name, name in include
                    if include_current:
                        log(f"  Tabelle {name} …")
                elif line.startswith(_SECTION_SKIP):
                    state, include_current = "", False
                elif line.startswith(b"-- Dump completed"):
                    state, include_current = None, True
            if state:
                sizes[state] = sizes.get(state, 0) + len(line)
            if not include_current or sink is None:
                now = time.time()
                if now - last > 60:
                    last = now
                    log(f"  … {total / 1e9:.1f} GB gelesen ({state or 'Kopf'})")
                continue
            if state and line.startswith(b") ENGINE="):
                line = _ENGINE_RE.sub(b") ENGINE=" + engine.encode(), line, count=1)
                line = _INNODB_OPTS_RE.sub(b"", line)
            elif state and line.startswith(b"  UNIQUE KEY "):
                # DISABLE KEYS schaltet bei Aria/MyISAM nur nicht-eindeutige Indizes ab; UNIQUE-Indizes
                # würden zeilenweise gepflegt (10-mal langsamer). Der Dump ist ohnehin konsistent, und
                # der Spiegel ist read-only – also alle Sekundärindizes als normale KEYs anlegen.
                line = b"  KEY " + line[len(b"  UNIQUE KEY "):]
            try:
                sink.write(line)
            except BrokenPipeError:
                raise SystemExit("MariaDB-Client hat die Verbindung beendet (Fehlermeldung oben).")
    finally:
        src.stdout.close()
        src.wait()
    log(f"  Dump gelesen: {total / 1e9:.1f} GB in {(time.time() - t0) / 60:.1f} min")
    return sizes


def load(admin: Admin, dump: Path, db: str, include: set[str], engine: str) -> dict[str, int]:
    args = admin.cmd + ["--batch", "--default-character-set=utf8mb4", "--database", db]
    client = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1 << 22)
    assert client.stdin is not None
    # Sitzungseinstellungen für den Massenimport (die Aria-Sortierpuffer sind Sitzungswerte)
    client.stdin.write(b"SET SESSION unique_checks=0, foreign_key_checks=0, sql_mode='NO_AUTO_VALUE_ON_ZERO';\n"
                       b"SET SESSION aria_sort_buffer_size=536870912;\n")
    sizes = stream_dump(dump, include, client.stdin, engine)
    client.stdin.close()
    rc = client.wait()
    err = client.stderr.read().decode(errors="replace") if client.stderr else ""
    if rc != 0:
        raise SystemExit(f"MariaDB-Client beendet mit Code {rc}:\n{err}")
    if err.strip():
        log("  Client-Meldungen: " + err.strip()[:2000])
    return sizes


# --------------------------------------------------------------------------- #
# Bereinigung, Meta, Tausch, Views, Rechte
# --------------------------------------------------------------------------- #
def curate(admin: Admin, db: str) -> None:
    tables = {r[0] for r in admin.rows("SHOW TABLES", db)}

    if "user" in tables:
        cols = [r[0] for r in admin.rows(
            f"SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='{db}' "
            "AND TABLE_NAME='user' ORDER BY ORDINAL_POSITION")]
        drop = [c for c in cols if c not in USER_KEEP]
        if drop:
            admin.run("ALTER TABLE `user` " + ", ".join(f"DROP COLUMN `{c}`" for c in drop), db)
            log(f"  user: {len(drop)} private Spalten entfernt ({', '.join(drop)})")

    # Kandidaten für zu entfernende Kommentare: an gelöschten Versionen (archive), an Versionen
    # mit verstecktem Kommentar und an unterdrückten/privaten Logbucheinträgen. Autokommentare
    # enthalten Labels und Beschreibungen – bei Löschungen aus Datenschutzgründen genau das Problem.
    prune = "comment" in tables
    if prune:
        admin.run("DROP TABLE IF EXISTS _ar; CREATE TABLE _ar (id bigint unsigned PRIMARY KEY) ENGINE=Aria", db)
        if "archive" in tables:
            admin.run("INSERT IGNORE INTO _ar SELECT DISTINCT ar_comment_id FROM archive WHERE ar_comment_id > 0", db)
        if "revision" in tables:
            admin.run("INSERT IGNORE INTO _ar SELECT DISTINCT rev_comment_id FROM revision "
                      "WHERE (rev_deleted & 2) <> 0 AND rev_comment_id > 0", db)
        if "logging" in tables:
            types = ", ".join(f"'{t}'" for t in PRIVATE_LOG_TYPES)
            admin.run("INSERT IGNORE INTO _ar SELECT DISTINCT log_comment_id FROM logging "
                      f"WHERE (log_deleted <> 0 OR log_type IN ({types})) AND log_comment_id > 0", db)

    if "logging" in tables:
        types = ", ".join(f"'{t}'" for t in PRIVATE_LOG_TYPES)
        admin.run(f"DELETE FROM logging WHERE log_deleted <> 0 OR log_type IN ({types})", db)
        log("  logging: unterdrückte und private Einträge entfernt")

    if "revision" in tables:
        n_user = admin.scalar("SELECT COUNT(*) FROM revision WHERE (rev_deleted & 4) <> 0", db)
        n_comm = admin.scalar("SELECT COUNT(*) FROM revision WHERE (rev_deleted & 2) <> 0", db)
        if n_user != "0":
            admin.run("UPDATE revision SET rev_actor = 0 WHERE (rev_deleted & 4) <> 0", db)
        if n_comm != "0":
            admin.run("UPDATE revision SET rev_comment_id = 0 WHERE (rev_deleted & 2) <> 0", db)
        log(f"  revision: {n_user} versteckte Benutzer, {n_comm} versteckte Kommentare anonymisiert")

    if prune:
        # was noch von sichtbaren Versionen/Logeinträgen referenziert wird, bleibt
        if "revision" in tables:
            admin.run("DELETE _ar FROM _ar JOIN revision r ON r.rev_comment_id = _ar.id", db)
        if "logging" in tables:
            admin.run("DELETE _ar FROM _ar JOIN logging l ON l.log_comment_id = _ar.id", db)
        if "image" in tables:
            admin.run("DELETE _ar FROM _ar JOIN image i ON i.img_description_id = _ar.id", db)
        n = admin.scalar("SELECT COUNT(*) FROM _ar", db)
        admin.run("DELETE comment FROM comment JOIN _ar ON _ar.id = comment.comment_id; DROP TABLE _ar", db)
        log(f"  comment: {n} Kommentare gelöschter/versteckter Versionen entfernt")
    for t in [t for t in TEMP_TABLES if t in tables]:
        admin.run(f"DROP TABLE `{t}`", db)

    for t in [t for t in ("revision", "page", "comment", "logging", "actor", "change_tag") if t in tables]:
        admin.run(f"ANALYZE TABLE `{t}`", db)


def write_meta(admin: Admin, db: str, dump: Path, sizes: dict[str, int], include: set[str],
               with_text: bool) -> None:
    st = dump.stat()
    loaded = sorted(t for t in include if t in sizes and t not in TEMP_TABLES)
    skipped = sorted(t for t in sizes if t not in include)
    rows = {
        "dump_file": dump.name, "dump_date": dump_date(dump),
        "dump_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "dump_size_gz": str(st.st_size), "loaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "loader_version": LOADER_VERSION, "with_text": "1" if with_text else "0",
        "tables_loaded": ",".join(loaded), "tables_skipped": ",".join(skipped),
        "table_bytes": ",".join(f"{t}:{sizes[t]}" for t in sorted(sizes)),
    }
    esc = lambda s: s.replace("\\", "\\\\").replace("'", "\\'")  # noqa: E731
    admin.run("DROP TABLE IF EXISTS mw_meta; CREATE TABLE mw_meta (k varchar(64) PRIMARY KEY, v text) ENGINE=Aria;\n"
              + "INSERT INTO mw_meta VALUES " + ", ".join(f"('{k}', '{esc(v)}')" for k, v in rows.items()), db)


def swap(admin: Admin, staging: str, db: str) -> None:
    """Tabellen aus <db>_new nach <db> verschieben (ein RENAME = atomar), Rest wegräumen."""
    admin.run(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    old = admin.rows(f"SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA='{db}'")
    for name, kind in old:
        admin.run(f"DROP {'VIEW' if kind == 'VIEW' else 'TABLE'} IF EXISTS `{db}`.`{name}`")
    new = [r[0] for r in admin.rows("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'", staging)]
    if not new:
        raise SystemExit("Staging-Datenbank ist leer – nichts zu tauschen.")
    admin.run("RENAME TABLE " + ", ".join(f"`{staging}`.`{t}` TO `{db}`.`{t}`" for t in new))
    admin.run(f"DROP DATABASE `{staging}`")
    log(f"  {len(new)} Tabellen nach {db} verschoben")


def create_views(admin: Admin, db: str) -> None:
    tables = {r[0] for r in admin.rows("SHOW TABLES", db)}
    need = {"v_revision": {"revision", "page", "actor", "comment"}, "v_log": {"logging", "actor", "comment"},
            "v_item_terms": {"wbt_item_terms", "wbt_term_in_lang", "wbt_type", "wbt_text_in_lang", "wbt_text"},
            "v_property_terms": {"wbt_property_terms", "wbt_term_in_lang", "wbt_type", "wbt_text_in_lang", "wbt_text"}}
    for name, sql in VIEWS.items():
        if need[name] <= tables:
            admin.run(sql, db)
    log("  Views angelegt: " + ", ".join(n for n in VIEWS if need[n] <= tables))


def env_get(env: Path, key: str) -> str:
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return re.split(r"\s+#", line.split("=", 1)[1], maxsplit=1)[0].strip().strip('"').strip("'")
    return ""


def env_set(env: Path, key: str, value: str) -> None:
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    env.write_text(text, encoding="utf-8")
    os.chmod(env, 0o600)


def grant(admin: Admin, db: str, user: str, env: Path) -> None:
    """Lesebenutzer anlegen/aktualisieren; Passwort aus .env oder neu erzeugt und dort eingetragen."""
    password = os.environ.get("MW_DB_PASSWORD") or env_get(env, "MW_DB_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(24)
        env_set(env, "MW_DB_PASSWORD", password)
        log(f"  MW_DB_PASSWORD neu erzeugt und in {env} eingetragen")
    for k, v in (("MW_DB_USER", user), ("MW_DB_NAME", db)):
        if env_get(env, k) != v:
            env_set(env, k, v)
    pw = password.replace("\\", "\\\\").replace("'", "\\'")
    for host in ("localhost", "127.0.0.1"):
        admin.run(f"CREATE USER IF NOT EXISTS '{user}'@'{host}' IDENTIFIED BY '{pw}';\n"
                  f"ALTER USER '{user}'@'{host}' IDENTIFIED BY '{pw}';\n"
                  f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{user}'@'{host}';\n"
                  f"GRANT SELECT, SHOW VIEW ON `{db}`.* TO '{user}'@'{host}';\nFLUSH PRIVILEGES")
    log(f"  Lesebenutzer {user}: SELECT auf {db}.* (localhost, 127.0.0.1)")


def prune_dumps(dump: Path, dump_dir: Path, keep: int) -> list[Path]:
    """Nach erfolgreichem Laden ältere *.sql.gz im Dump-Verzeichnis löschen. Die `keep` neuesten
    (nach mtime, wie find_dump) bleiben; der gerade geladene Dump und alles Neuere bleiben immer.
    keep < 1: nichts löschen. Liegt der Dump nicht im Verzeichnis, wird nichts angefasst."""
    if keep < 1:
        return []
    dump, dump_dir = dump.resolve(), dump_dir.resolve()
    if dump.parent != dump_dir:
        log(f"  Aufräumen übersprungen: {dump.name} liegt nicht in {dump_dir}")
        return []
    key = lambda p: (p.stat().st_mtime, p.name)  # noqa: E731
    files = sorted(dump_dir.glob("*.sql.gz"), key=key, reverse=True)
    removed: list[Path] = []
    freed = 0
    for p in files[keep:]:
        if p.resolve() == dump or key(p) >= key(dump):
            continue
        freed += p.stat().st_size
        p.unlink()
        removed.append(p)
    if removed:
        log("  Aufgeräumt: " + ", ".join(p.name for p in removed) + f" ({freed / 1e9:.1f} GB frei)")
    else:
        log("  Aufräumen: nichts zu löschen")
    return removed


def summary(admin: Admin, db: str) -> None:
    rows = admin.rows(
        f"SELECT TABLE_NAME, TABLE_ROWS, ROUND((DATA_LENGTH+INDEX_LENGTH)/1048576) FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA='{db}' AND TABLE_TYPE='BASE TABLE' ORDER BY DATA_LENGTH+INDEX_LENGTH DESC")
    log("Tabellen in " + db + " (Zeilen, MB):")
    for name, n, mb in rows:
        print(f"    {name:24s} {int(n):>12,d} {int(mb):>8,d}".replace(",", "."))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", help="SQL-Dump (.sql.gz); Default: neuester in --dump-dir")
    ap.add_argument("--dump-dir", default=os.environ.get("MW_DUMP_DIR", "/srv/data/factgrid/mediawiki"))
    ap.add_argument("--db", default=os.environ.get("MW_DB_NAME", "factgrid_mw"))
    ap.add_argument("--user", default=os.environ.get("MW_DB_USER", "factgrid_ro"), help="Lesebenutzer")
    ap.add_argument("--admin", default=os.environ.get("MW_DB_ADMIN", "mariadb"),
                    help="Admin-Client, z. B. 'mariadb -uroot -pGEHEIM'")
    ap.add_argument("--env", default=str(ROOT / ".env"), help=".env für MW_DB_PASSWORD")
    ap.add_argument("--engine", default="Aria TRANSACTIONAL=0 PAGE_CHECKSUM=0")
    ap.add_argument("--tables", help="eigene Tabellenliste (Komma) statt der Allowlist")
    ap.add_argument("--with-text", action="store_true", help="auch text/content/slots laden (≈ 160 GB)")
    ap.add_argument("--list", action="store_true", help="nur Tabellen und Größen anzeigen, nichts laden")
    ap.add_argument("--force", action="store_true", help="auch laden, wenn dieser Dump schon geladen ist")
    ap.add_argument("--no-swap", action="store_true", help="in <db>_new belassen (zum Prüfen)")
    ap.add_argument("--keep", type=int, default=int(os.environ.get("MW_KEEP", "1")),
                    help="nach erfolgreichem Laden so viele *.sql.gz im Dump-Verzeichnis behalten "
                         "(neueste zuerst; Default 1, 0 = nichts löschen)")
    a = ap.parse_args()

    dump = find_dump(a.dump, a.dump_dir)
    include = set(a.tables.split(",")) if a.tables else set(PUBLIC_TABLES) | set(TEMP_TABLES)
    if a.with_text:
        include |= set(TEXT_TABLES)
    log(f"Dump: {dump} ({dump.stat().st_size / 1e9:.1f} GB gz, Datum {dump_date(dump)})")

    if a.list:
        sizes = stream_dump(dump, include, None, "")
        print(f"\n{'Tabelle':40s} {'MB':>12s}  geladen?")
        for name, sz in sorted(sizes.items(), key=lambda kv: -kv[1]):
            print(f"{name:40s} {sz / 1e6:12.1f}  {'ja' if name in include else '-'}")
        return

    admin = Admin(a.admin)
    admin.run("SELECT 1")  # Zugang prüfen, bevor 7 Minuten Dekompression laufen
    if not a.force:
        try:
            prev = admin.scalar(f"SELECT v FROM `{a.db}`.mw_meta WHERE k='dump_file'")
            prev_text = admin.scalar(f"SELECT v FROM `{a.db}`.mw_meta WHERE k='with_text'")
        except SystemExit:
            prev, prev_text = "", ""
        if prev == dump.name and (prev_text == "1" or not a.with_text):
            log(f"{dump.name} ist bereits geladen (mw_meta) – nichts zu tun (--force erzwingt).")
            return

    staging = f"{a.db}_new"
    t0 = time.time()
    log(f"1/7 Staging-Datenbank {staging} anlegen")
    admin.run(f"DROP DATABASE IF EXISTS `{staging}`; CREATE DATABASE `{staging}` CHARACTER SET binary")
    log(f"2/7 Dump laden ({len(include)} Tabellen, ENGINE={a.engine.split()[0]})")
    sizes = load(admin, dump, staging, include, a.engine)
    missing = sorted(t for t in include if t not in sizes)
    if missing:
        log("  nicht im Dump enthalten: " + ", ".join(missing))
    log("3/7 Bereinigen")
    curate(admin, staging)
    write_meta(admin, staging, dump, sizes, include, a.with_text)
    if a.no_swap:
        log(f"Fertig (ohne Tausch): Daten liegen in {staging}")
        return
    log(f"4/7 Nach {a.db} tauschen")
    swap(admin, staging, a.db)
    log("5/7 Views")
    create_views(admin, a.db)
    log("6/7 Lesebenutzer")
    grant(admin, a.db, a.user, Path(a.env))
    summary(admin, a.db)
    log("7/7 Aufräumen")
    prune_dumps(dump, Path(a.dump_dir), a.keep)
    log(f"Fertig in {(time.time() - t0) / 60:.1f} min – Dump vom {dump_date(dump)} in {a.db}")


if __name__ == "__main__":
    main()
