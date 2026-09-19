"""
Tests für den MediaWiki-Spiegel: Dump-Filter und Bereinigung (scripts/mw_load.py), die
Hilfsfunktionen in factgrid_mcp/mwdb.py und – wenn ein MariaDB-Adminzugang da ist – die
MCP-Tools edit_history / mw_sql / mw_schema end-to-end gegen einen synthetischen Mini-Dump
in der Datenbank factgrid_mw_test (wird am Ende wieder gelöscht).
"""
from __future__ import annotations

import gzip
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.setdefault("QLEVER_ENDPOINT", "http://127.0.0.1:1")  # QLever wird hier nicht gebraucht
os.environ["FACTGRID_CACHE_DIR"] = str(HERE / ".cache-test")
os.environ["MW_DB_PASSWORD"] = ""  # nie die echte Datenbank treffen

import mw_load  # noqa: E402
from factgrid_mcp import mwdb  # noqa: E402

TEST_DB = "factgrid_mw_test"
TEST_USER = "factgrid_ro_test"

# --------------------------------------------------------------------------- #
# Synthetischer mysqldump (Format wie MariaDB 10.3 mysqldump 10.19)
# --------------------------------------------------------------------------- #
def _table(name: str, create_body: str, rows: str, engine: str = "InnoDB", extra: str = "") -> str:
    return f"""
--
-- Table structure for table `{name}`
--

DROP TABLE IF EXISTS `{name}`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `{name}` (
{create_body}
) ENGINE={engine} DEFAULT CHARSET=binary{extra};
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `{name}`
--

LOCK TABLES `{name}` WRITE;
/*!40000 ALTER TABLE `{name}` DISABLE KEYS */;
{rows}
/*!40000 ALTER TABLE `{name}` ENABLE KEYS */;
UNLOCK TABLES;
"""


DUMP = """-- MySQL dump 10.19  Distrib 10.3.39-MariaDB, for Linux (x86_64)
--
-- Host: localhost    Database: mediawiki
-- ------------------------------------------------------

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES utf8mb4 */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
""" + _table("account_credentials",
             "  `acd_id` int(10) unsigned NOT NULL,\n  `acd_email` tinyblob NOT NULL,\n  PRIMARY KEY (`acd_id`)",
             "INSERT INTO `account_credentials` VALUES (1,'geheim@example.org');") \
    + _table("actor",
             "  `actor_id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,\n  `actor_user` int(10) unsigned DEFAULT NULL,\n"
             "  `actor_name` varbinary(255) NOT NULL,\n  PRIMARY KEY (`actor_id`),\n  UNIQUE KEY `actor_name` (`actor_name`)",
             "INSERT INTO `actor` VALUES (1,10,'Olaf Simons'),(2,11,'Bot Alpha'),(3,12,'Versteckt');", extra=" AUTO_INCREMENT=4") \
    + _table("archive",
             "  `ar_id` int(10) unsigned NOT NULL,\n  `ar_comment_id` bigint(20) unsigned NOT NULL,\n  PRIMARY KEY (`ar_id`)",
             "INSERT INTO `archive` VALUES (1,900),(2,2);") \
    + _table("comment",
             "  `comment_id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,\n  `comment_hash` int(11) NOT NULL,\n"
             "  `comment_text` blob NOT NULL,\n  `comment_data` blob DEFAULT NULL,\n  PRIMARY KEY (`comment_id`)",
             "INSERT INTO `comment` VALUES (1,0,'/* wbeditentity-create:0| */ Goethe, Dichter',NULL),"
             "(2,0,'/* wbsetclaim-create:2||1 */ [[Property:P2]]: [[Item:Q7]]',NULL),"
             "(3,0,'/* wbsetlabel-set:1|de */ Johann Wolfgang von Goethe',NULL),"
             "(4,0,'Seite angelegt',NULL),(900,0,'/* wbeditentity-create:0| */ Max Mustermann, geb. 1990, wohnt in …',NULL);",
             extra=" ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8") \
    + _table("logging",
             "  `log_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `log_type` varbinary(32) NOT NULL DEFAULT '',\n"
             "  `log_action` varbinary(32) NOT NULL DEFAULT '',\n  `log_timestamp` binary(14) NOT NULL,\n"
             "  `log_actor` bigint(20) unsigned NOT NULL,\n  `log_namespace` int(11) NOT NULL DEFAULT 0,\n"
             "  `log_title` varbinary(255) NOT NULL DEFAULT '',\n  `log_page` int(10) unsigned DEFAULT NULL,\n"
             "  `log_comment_id` bigint(20) unsigned NOT NULL,\n  `log_params` blob NOT NULL,\n"
             "  `log_deleted` tinyint(3) unsigned NOT NULL DEFAULT 0,\n  PRIMARY KEY (`log_id`)",
             "INSERT INTO `logging` VALUES (1,'create','create','20190101120000',1,120,'Q42',42,4,'',0),"
             "(2,'suppress','delete','20190102120000',1,120,'Q43',43,4,'',0),"
             "(3,'delete','delete','20190103120000',1,120,'Q44',44,4,'',1),"
             "(4,'newusers','create','20180101000000',1,2,'Olaf_Simons',NULL,4,'',0);") \
    + _table("page",
             "  `page_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `page_namespace` int(11) NOT NULL,\n"
             "  `page_title` varbinary(255) NOT NULL,\n  `page_is_redirect` tinyint(3) unsigned NOT NULL DEFAULT 0,\n"
             "  `page_latest` int(10) unsigned NOT NULL,\n  `page_len` int(10) unsigned NOT NULL,\n"
             "  PRIMARY KEY (`page_id`),\n  UNIQUE KEY `name_title` (`page_namespace`,`page_title`)",
             "INSERT INTO `page` VALUES (42,120,'Q42',0,3,300),(7,120,'Q7',0,4,100),(2,122,'P2',0,5,50),"
             "(100,4,'Directory_of_Properties',0,6,1000);") \
    + _table("revision",
             "  `rev_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `rev_page` int(10) unsigned NOT NULL,\n"
             "  `rev_comment_id` bigint(20) unsigned NOT NULL DEFAULT 0,\n  `rev_actor` bigint(20) unsigned NOT NULL DEFAULT 0,\n"
             "  `rev_timestamp` binary(14) NOT NULL,\n  `rev_minor_edit` tinyint(3) unsigned NOT NULL DEFAULT 0,\n"
             "  `rev_deleted` tinyint(3) unsigned NOT NULL DEFAULT 0,\n  `rev_len` int(10) unsigned DEFAULT NULL,\n"
             "  `rev_parent_id` int(10) unsigned DEFAULT NULL,\n  `rev_sha1` varbinary(32) NOT NULL DEFAULT '',\n"
             "  PRIMARY KEY (`rev_id`),\n  KEY `rev_timestamp` (`rev_timestamp`),\n"
             "  KEY `rev_page_timestamp` (`rev_page`,`rev_timestamp`),\n  KEY `rev_actor_timestamp` (`rev_actor`,`rev_timestamp`,`rev_id`)",
             "INSERT INTO `revision` VALUES (1,42,1,1,'20190101120000',0,0,100,0,''),(2,42,2,2,'20190105080000',0,0,200,1,''),"
             "(3,42,3,3,'20200301100000',0,6,300,2,''),(4,7,4,1,'20180101000000',0,0,100,0,''),"
             "(5,2,4,1,'20180101000100',0,0,50,0,''),(6,100,4,1,'20180101000200',0,0,1000,0,'');") \
    + _table("text",
             "  `old_id` int(10) unsigned NOT NULL,\n  `old_text` mediumblob NOT NULL,\n  PRIMARY KEY (`old_id`)",
             "INSERT INTO `text` VALUES (1,'{\"type\":\"item\"}');") \
    + _table("user",
             "  `user_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `user_name` varbinary(255) NOT NULL DEFAULT '',\n"
             "  `user_real_name` varbinary(255) NOT NULL DEFAULT '',\n  `user_password` tinyblob NOT NULL,\n"
             "  `user_email` tinyblob NOT NULL,\n  `user_token` binary(32) NOT NULL DEFAULT '',\n"
             "  `user_registration` binary(14) DEFAULT NULL,\n  `user_editcount` int(11) DEFAULT NULL,\n  PRIMARY KEY (`user_id`)",
             "INSERT INTO `user` VALUES (10,'Olaf Simons','Olaf S.',':pbkdf2:geheim','olaf@example.org','tok',"
             "'20180101000000',3),(11,'Bot Alpha','',':x','bot@example.org','tok','20190101000000',1),"
             "(12,'Versteckt','',':x','v@example.org','tok','20190101000000',1);") \
    + _table("user_groups",
             "  `ug_user` int(10) unsigned NOT NULL DEFAULT 0,\n  `ug_group` varbinary(255) NOT NULL DEFAULT '',\n"
             "  `ug_expiry` varbinary(14) DEFAULT NULL,\n  PRIMARY KEY (`ug_user`,`ug_group`)",
             "INSERT INTO `user_groups` VALUES (10,'sysop',NULL),(11,'bot',NULL);") \
    + _table("wbt_item_terms",
             "  `wbit_id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,\n  `wbit_item_id` int(10) unsigned NOT NULL,\n"
             "  `wbit_term_in_lang_id` int(10) unsigned NOT NULL,\n  PRIMARY KEY (`wbit_id`),\n  KEY `wbt_item_terms_item_id` (`wbit_item_id`)",
             "INSERT INTO `wbt_item_terms` VALUES (1,42,1),(2,42,2),(3,7,3);") \
    + _table("wbt_property_terms",
             "  `wbpt_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `wbpt_property_id` int(10) unsigned NOT NULL,\n"
             "  `wbpt_term_in_lang_id` int(10) unsigned NOT NULL,\n  PRIMARY KEY (`wbpt_id`)",
             "INSERT INTO `wbt_property_terms` VALUES (1,2,4);") \
    + _table("wbt_term_in_lang",
             "  `wbtl_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `wbtl_type_id` int(10) unsigned NOT NULL,\n"
             "  `wbtl_text_in_lang_id` int(10) unsigned NOT NULL,\n  PRIMARY KEY (`wbtl_id`)",
             "INSERT INTO `wbt_term_in_lang` VALUES (1,1,1),(2,1,2),(3,1,3),(4,1,4);") \
    + _table("wbt_text",
             "  `wbx_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `wbx_text` varbinary(255) NOT NULL,\n  PRIMARY KEY (`wbx_id`)",
             "INSERT INTO `wbt_text` VALUES (1,'Johann Wolfgang von Goethe'),(2,'Goethe'),(3,'Mensch'),(4,'Ist ein(e)');") \
    + _table("wbt_text_in_lang",
             "  `wbxl_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `wbxl_language` varbinary(20) NOT NULL,\n"
             "  `wbxl_text_id` int(10) unsigned NOT NULL,\n  PRIMARY KEY (`wbxl_id`)",
             "INSERT INTO `wbt_text_in_lang` VALUES (1,'de',1),(2,'en',2),(3,'de',3),(4,'de',4);") \
    + _table("wbt_type",
             "  `wby_id` int(10) unsigned NOT NULL AUTO_INCREMENT,\n  `wby_name` varbinary(45) NOT NULL,\n  PRIMARY KEY (`wby_id`)",
             "INSERT INTO `wbt_type` VALUES (1,'label'),(2,'description'),(3,'alias');") \
    + """
/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;

-- Dump completed on 2026-09-01  1:00:00
"""


def _write_dump(tmp: Path) -> Path:
    p = tmp / "monthly_mediawiki_2026-09-01_01h00m_test.sql.gz"
    with gzip.open(p, "wb") as fh:
        fh.write(DUMP.encode("utf-8"))
    return p


# --------------------------------------------------------------------------- #
# Filter (ohne Datenbank)
# --------------------------------------------------------------------------- #
def test_stream_dump_filters_and_rewrites_engine():
    with tempfile.TemporaryDirectory() as d:
        dump = _write_dump(Path(d))
        sink = io.BytesIO()
        include = set(mw_load.PUBLIC_TABLES) | set(mw_load.TEMP_TABLES)
        sizes = mw_load.stream_dump(dump, include, sink, "Aria TRANSACTIONAL=0 PAGE_CHECKSUM=0")
    out = sink.getvalue().decode()
    assert "account_credentials" not in out and "geheim@example.org" not in out
    assert "CREATE TABLE `text`" not in out and '{"type":"item"}' not in out
    assert "CREATE TABLE `revision`" in out and "INSERT INTO `revision` VALUES" in out
    assert "ENGINE=InnoDB" not in out and out.count("ENGINE=Aria TRANSACTIONAL=0 PAGE_CHECKSUM=0") == len(
        [t for t in include if t in sizes])
    assert "ROW_FORMAT" not in out and "KEY_BLOCK_SIZE" not in out  # InnoDB-Optionen entfernt
    assert "SET NAMES utf8mb4" in out  # Kopf bleibt
    assert "Dump completed" in out    # Fuß bleibt
    assert sizes["text"] > 0 and sizes["account_credentials"] > 0 and "revision" in sizes
    # --with-text nimmt text mit
    sink = io.BytesIO()
    with tempfile.TemporaryDirectory() as d:
        mw_load.stream_dump(_write_dump(Path(d)), include | set(mw_load.TEXT_TABLES), sink, "Aria")
    assert "CREATE TABLE `text`" in sink.getvalue().decode()


def test_dump_date_and_env_helpers():
    with tempfile.TemporaryDirectory() as d:
        dump = _write_dump(Path(d))
        assert mw_load.dump_date(dump) == "2026-09-01"
        env = Path(d) / ".env"
        env.write_text("A=1\nMW_DB_USER=alt   # Kommentar\n", encoding="utf-8")
        assert mw_load.env_get(env, "MW_DB_USER") == "alt"
        mw_load.env_set(env, "MW_DB_USER", "neu")
        mw_load.env_set(env, "MW_DB_PASSWORD", "pw")
        assert mw_load.env_get(env, "MW_DB_USER") == "neu" and mw_load.env_get(env, "MW_DB_PASSWORD") == "pw"
        assert env.read_text().count("MW_DB_USER=") == 1 and (env.stat().st_mode & 0o777) == 0o600


# --------------------------------------------------------------------------- #
# Hilfsfunktionen in mwdb (ohne Datenbank)
# --------------------------------------------------------------------------- #
def test_prune_dumps():
    with tempfile.TemporaryDirectory() as d:
        ddir = Path(d)
        names = ["monthly_2026-07-01.sql.gz", "monthly_2026-08-01.sql.gz", "monthly_2026-09-01.sql.gz",
                 "daily_2026-09-11.sql.gz", "daily_2026-09-12.sql.gz"]
        for i, n in enumerate(names):  # mtime steigt mit i
            f = ddir / n
            f.write_bytes(b"x" * (i + 1))
            os.utime(f, (1_800_000_000 + i * 3600, 1_800_000_000 + i * 3600))
        (ddir / "notiz.txt").write_text("bleibt")
        loaded = ddir / "daily_2026-09-11.sql.gz"

        # keep=0: nichts löschen
        assert mw_load.prune_dumps(loaded, ddir, 0) == []
        assert sorted(p.name for p in ddir.glob("*.sql.gz")) == sorted(names)
        # Dump außerhalb des Verzeichnisses: nichts anfassen
        with tempfile.TemporaryDirectory() as other:
            ext = Path(other) / "fremd.sql.gz"
            ext.write_bytes(b"y")
            assert mw_load.prune_dumps(ext, ddir, 1) == []
        assert len(list(ddir.glob("*.sql.gz"))) == 5
        # keep=2: die zwei neuesten bleiben, der geladene sowieso
        removed = mw_load.prune_dumps(loaded, ddir, 2)
        assert sorted(p.name for p in removed) == ["monthly_2026-07-01.sql.gz", "monthly_2026-08-01.sql.gz",
                                                   "monthly_2026-09-01.sql.gz"], removed
        # keep=1: nur der neueste – der geladene ist älter, bleibt aber trotzdem; Neueres bleibt
        removed = mw_load.prune_dumps(loaded, ddir, 1)
        assert removed == [], removed
        assert sorted(p.name for p in ddir.glob("*.sql.gz")) == ["daily_2026-09-11.sql.gz", "daily_2026-09-12.sql.gz"]
        # keep=1 mit dem neuesten als geladenem Dump: alles Ältere weg
        newest = ddir / "daily_2026-09-12.sql.gz"
        removed = mw_load.prune_dumps(newest, ddir, 1)
        assert [p.name for p in removed] == ["daily_2026-09-11.sql.gz"], removed
        assert [p.name for p in ddir.glob("*.sql.gz")] == ["daily_2026-09-12.sql.gz"]
        assert (ddir / "notiz.txt").exists()


def test_mwdb_dates_and_titles():
    mwdb._ID_NS.update({"Q": 120, "P": 122})  # Namensräume ohne DB festlegen
    assert mwdb.ts_to_iso("20180112134423") == "2018-01-12 13:44:23"
    assert mwdb.ts_to_iso(b"20180112134423") == "2018-01-12 13:44:23"
    assert mwdb.parse_date("2024") == "20240101000000" and mwdb.parse_date("2024", end=True) == "20241231235959"
    assert mwdb.parse_date("2024-02", end=True) == "20240229235959"
    assert mwdb.parse_date("2024-05-17") == "20240517000000" and mwdb.parse_date("2024-05-17", end=True) == "20240517235959"
    assert mwdb.parse_date("2024-05-17 13:05") == "20240517130500"
    assert mwdb.parse_date("") == ""
    try:
        mwdb.parse_date("gestern")
        assert False
    except mwdb.MWDBError:
        pass
    assert mwdb.split_title("Q7") == (120, "Q7") and mwdb.split_title("q7") == (120, "Q7")
    assert mwdb.split_title("Item:Q7") == (120, "Q7") and mwdb.split_title("wd:P2") == (122, "P2")
    assert mwdb.split_title("https://database.factgrid.de/wiki/Item:Q409") == (120, "Q409")
    assert mwdb.split_title("FactGrid:Directory of Properties") == (4, "Directory_of_Properties")
    assert mwdb.split_title("Main Page") == (None, "Main_Page")
    assert mwdb.split_title("User talk:Olaf Simons") == (3, "Olaf_Simons")
    assert mwdb.full_title(120, b"Q7") == "Item:Q7" and mwdb.full_title(0, "Main_Page") == "Main Page"
    assert mwdb.ns_id("Item") == 120 and mwdb.ns_id("122") == 122 and mwdb.ns_id("") == 0 and mwdb.ns_id("Nix") is None


def test_mwdb_humanize_comment():
    h = mwdb.humanize_comment
    assert h("/* wbsetclaim-create:2||1 */ [[Property:P2]]: [[Item:Q7]]") == "Aussage angelegt: P2: Q7"
    assert h("/* wbsetlabel-set:1|de */ Johann Wolfgang von Goethe") == "Label gesetzt [de]: Johann Wolfgang von Goethe"
    assert h("/* wbeditentity-create:0| */ Goethe, Dichter") == "Eintrag angelegt: Goethe, Dichter"
    assert h("/* wbremoveclaims-remove:3| */ [[Property:P5]]: x") == "Aussage entfernt: P5: x"
    assert h("/* wbeditentity-update:0| */") == "Eintrag bearbeitet"
    assert h("/* wbunbekannt-foo:1| */ rest") == "unbekannt-foo: rest"
    assert h("Tippfehler korrigiert, siehe [[Item:Q7|Goethe]]") == "Tippfehler korrigiert, siehe Q7"
    assert h(b"") == "" and h(None) == ""


def test_mwdb_guard_and_limit():
    g, al = mwdb.guard, mwdb.apply_limit
    assert g("SELECT 1; -- Kommentar") == "SELECT 1"
    assert al("SELECT * FROM page", 10) == "SELECT * FROM page\nLIMIT 11"
    assert al("SELECT * FROM page LIMIT 500", 10) == "SELECT * FROM page LIMIT 11"
    assert al("SELECT * FROM page LIMIT 5", 10) == "SELECT * FROM page LIMIT 5"
    assert al("SELECT * FROM page LIMIT 20, 500", 10) == "SELECT * FROM page LIMIT 20, 11"
    assert al("SHOW TABLES", 10) == "SHOW TABLES"
    for bad in ["DELETE FROM page", "SELECT 1; DROP TABLE page", "SELECT * INTO OUTFILE '/tmp/x' FROM page",
                "UPDATE page SET page_len=0", "SET GLOBAL max_connections=1", "", "SELECT SLEEP(10)"]:
        try:
            g(bad)
            assert False, bad
        except mwdb.MWDBError:
            pass
    assert g("SELECT 'DROP TABLE x' AS s") == "SELECT 'DROP TABLE x' AS s"  # Schlüsselwort im String erlaubt
    assert g("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH")


# --------------------------------------------------------------------------- #
# Integration: Mini-Dump laden und die MCP-Tools darauf laufen lassen
# --------------------------------------------------------------------------- #
def _admin_available() -> bool:
    try:
        return subprocess.run(["mariadb", "-e", "SELECT 1"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _load_test_db(tmp: Path) -> mw_load.Admin:
    admin = mw_load.Admin("mariadb")
    dump = _write_dump(tmp)
    include = set(mw_load.PUBLIC_TABLES) | set(mw_load.TEMP_TABLES)
    staging = TEST_DB + "_new"
    admin.run(f"DROP DATABASE IF EXISTS `{staging}`; CREATE DATABASE `{staging}` CHARACTER SET binary")
    mw_load.load(admin, dump, staging, include, "Aria TRANSACTIONAL=0 PAGE_CHECKSUM=0")
    mw_load.curate(admin, staging)
    mw_load.write_meta(admin, staging, dump, {"page": 1, "revision": 1, "text": 5}, include, False)
    mw_load.swap(admin, staging, TEST_DB)
    mw_load.create_views(admin, TEST_DB)
    env = tmp / ".env"
    mw_load.grant(admin, TEST_DB, TEST_USER, env)
    mwdb.NAME, mwdb.USER, mwdb.PASSWORD = TEST_DB, TEST_USER, mw_load.env_get(env, "MW_DB_PASSWORD")
    mwdb._ID_NS.clear()
    return admin


def _cleanup(admin: mw_load.Admin) -> None:
    admin.run(f"DROP DATABASE IF EXISTS `{TEST_DB}`; DROP DATABASE IF EXISTS `{TEST_DB}_new`;"
              f"DROP USER IF EXISTS '{TEST_USER}'@'localhost'; DROP USER IF EXISTS '{TEST_USER}'@'127.0.0.1'")


def test_integration_load_curate_and_tools():
    if not _admin_available():
        print("  (übersprungen: kein MariaDB-Adminzugang über `mariadb`)")
        return
    with tempfile.TemporaryDirectory() as d:
        admin = _load_test_db(Path(d))
        try:
            # Bereinigung
            cols = {r[0] for r in admin.rows("SHOW COLUMNS FROM `user`", TEST_DB)}
            assert cols == {"user_id", "user_name", "user_registration", "user_editcount"}, cols
            assert admin.scalar("SELECT COUNT(*) FROM logging", TEST_DB) == "2"  # suppress + log_deleted weg
            assert admin.scalar("SELECT rev_actor FROM revision WHERE rev_id=3", TEST_DB) == "0"
            assert admin.scalar("SELECT COUNT(*) FROM comment WHERE comment_id=900", TEST_DB) == "0"  # nur archive
            assert admin.scalar("SELECT COUNT(*) FROM comment WHERE comment_id=2", TEST_DB) == "1"    # auch revision
            assert admin.scalar("SELECT COUNT(*) FROM comment WHERE comment_id=3", TEST_DB) == "0"    # nur an versteckter Version
            assert admin.scalar("SELECT COUNT(*) FROM comment WHERE comment_id=4", TEST_DB) == "1"    # sichtbar
            assert "archive" not in {r[0] for r in admin.rows("SHOW TABLES", TEST_DB)}
            assert admin.scalar("SELECT v FROM mw_meta WHERE k='dump_date'", TEST_DB) == "2026-09-01"
            assert admin.scalar("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA='%s' AND TABLE_NAME='revision'" % TEST_DB) == "Aria"
            # Lesebenutzer: nur SELECT
            assert mwdb.scalar("SELECT COUNT(*) FROM v_revision") == "6"
            assert mwdb.entity_namespace("Q") == 120 and mwdb.entity_namespace("P") == 122
            try:
                mwdb.query("DELETE FROM page")
                assert False
            except mwdb.MWDBError:
                pass
            res = mwdb.query("SELECT rev_timestamp, user_name FROM v_revision WHERE page_title='Q42' ORDER BY rev_id")
            assert res.rows[0] == ["2019-01-01 12:00:00", "Olaf Simons"], res.rows
            assert res.rows[2][1] == ""  # versteckter Benutzer → kein Actor mehr

            # MCP-Tools end-to-end
            from factgrid_mcp import server
            tool = lambda t: getattr(t, "fn", t)  # noqa: E731
            out = tool(server.edit_history)(page="Q42")
            assert "Item:Q42" in out and "Johann Wolfgang von Goethe" in out, out
            assert "Olaf Simons" in out and "Bot Alpha" in out and "Aussage angelegt: P2: Q7" in out, out
            assert "Eintrag angelegt: Goethe, Dichter" in out and "3 Bearbeitungen" in out, out
            out = tool(server.edit_history)(user="olaf simons", since="2018", until="2018-12-31")
            assert "Item:Q7" in out and "Property:P2" in out and "Q42" not in out, out
            assert "sysop" in out, out
            out = tool(server.edit_history)(page="Q42", user="Bot Alpha")
            assert "Bot Alpha" in out and "1 Bearbeitung" in out and "Olaf Simons" not in out.split("\n")[1], out
            out = tool(server.edit_history)(page="FactGrid:Directory of Properties")
            assert "Directory of Properties" in out and "Olaf Simons" in out, out
            assert "nicht gefunden" in tool(server.edit_history)(page="Q999999")
            assert "Erwartet" in tool(server.edit_history)()
            out = tool(server.edit_history)(since="2019-01", until="2019-01", namespace="Item")
            rows = [l.split("\t") for l in out.split("\n")[1:] if l.count("\t") >= 3]
            assert len(rows) == 2 and all(r[2].startswith("Item:Q42") for r in rows), out
            out = tool(server.mw_sql)("SELECT page_title, COUNT(*) AS n FROM v_revision GROUP BY page_title ORDER BY n DESC")
            assert out.startswith("page_title\tn\nQ42\t3"), out
            assert "FEHLER" in tool(server.mw_sql)("DROP TABLE page")
            assert "FEHLER" in tool(server.mw_sql)("SELECT * FROM gibtsnicht")
            out = tool(server.mw_schema)(refresh=True)
            assert "revision" in out and "v_revision" in out and "Item = 120" in out and "2026-09-01" in out, out
        finally:
            _cleanup(admin)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
