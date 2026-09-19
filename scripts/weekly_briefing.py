#!/usr/bin/env python3
"""
weekly_briefing.py – wöchentliches Briefing über die Arbeit in FactGrid, auf Englisch.

Freitags 08:00 Ortszeit (ops/factgrid-briefing.timer): die Zahlen der vergangenen Woche
(Freitag bis Donnerstag, UTC) aus dem lokalen MediaWiki-Spiegel holen, von einem Sprachmodell
formulieren lassen und als Mail an die Liste schicken.

    make briefing                 # Probelauf: Briefing auf die Konsole, nichts geht raus
    make briefing-send            # wirklich verschicken (sonst macht das der Timer)
    python3 scripts/weekly_briefing.py --facts           # nur die Zahlen, ohne Modell
    python3 scripts/weekly_briefing.py --weeks-back 2    # die vorletzte Woche
    python3 scripts/weekly_briefing.py --send --to ich@example.org   # Testempfänger

Grundsatz: **Zahlen kommen aus SQL, Sprache aus dem Modell.** Das Modell bekommt einen Block
mit fertigen Zahlen und formuliert sie aus; es hat keinen Zugriff auf die Datenbank und kann
nichts nachschlagen. Was es trotzdem dazuerfindet, steht in keiner Zeile der Fakten – deshalb
die Regel im Prompt und der Hinweis am Kopf jeder Mail, dass niemand gegenliest.

Nichts wird verschickt, wenn etwas schiefgeht: leere oder zu kurze Antwort des Modells, keine
Bearbeitungen in der Woche, veralteter Spiegel – in all diesen Fällen bricht das Skript mit
einem Fehler ab (der Timer meldet es im journal), statt eine halbe Mail an die Liste zu geben.

Konfiguration (.env): das Postfach (host_name, mailadresse, mail_password, SMTP), der
Empfänger BRIEFING_TO, das Modell BRIEFING_MODEL (deepseek-flash) und DEEPSEEK_API_KEY.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from factgrid_mcp.envfile import load_env  # noqa: E402

load_env(ROOT / ".env")

from factgrid_mcp import mwdb  # noqa: E402  (liest MW_DB_* beim Import)

SITE = os.environ.get("FACTGRID_SITE_URL", "https://database.factgrid.de/")
MODEL = os.environ.get("BRIEFING_MODEL", "deepseek-flash").strip()
DEFAULT_TO = os.environ.get("BRIEFING_TO", "factgrid-community@listserv.dfn.de").strip()
ITEM_NS, PROPERTY_NS = 120, 122

# Der Hinweis am Kopf der Mail steht fest im Skript und kommt NICHT aus dem Modell: wer eine
# automatisch erzeugte Mail bekommt, soll das aus einer Quelle erfahren, die nicht halluzinieren
# kann. Erste Zeile des Mailtexts, in HTML kursiv abgesetzt.
PREAMBLE = (
    "Automatically generated: the figures below are read from a local mirror of the FactGrid "
    "MediaWiki database with SQL, and a language model ({model}) turns them into this text. "
    "Nobody proofreads it before it goes out, and the model has no access to the wiki itself – "
    "so trust the numbers, treat the wording with the usual caution, and reply on the list if "
    "something looks wrong."
)


# --------------------------------------------------------------------------- #
# Zeitraum: die letzte abgeschlossene Woche, Freitag 00:00 bis Freitag 00:00 (UTC)
# --------------------------------------------------------------------------- #
def week_range(now: datetime | None = None, weeks_back: int = 1) -> tuple[datetime, datetime]:
    """(Beginn, Ende) der Berichtswoche: Freitag bis Donnerstag. weeks_back=1 ist die letzte
    abgeschlossene Woche – läuft der Timer Freitag früh, endet sie am Donnerstag davor, also
    genau dort, wo der MediaWiki-Spiegel vom selben Morgen aufhört."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # (weekday() - 4) % 7 = Tage seit dem letzten Freitag; freitags selbst ist das 0.
    friday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=(now.weekday() - 4) % 7)
    end = friday - timedelta(days=7 * (weeks_back - 1))
    return end - timedelta(days=7), end


def stamp(t: datetime) -> str:
    """datetime → MediaWiki-Zeitstempel 'YYYYMMDDHHMMSS' (UTC)."""
    return t.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def pretty_day(t: datetime) -> str:
    return t.strftime("%a %d %b %Y")


# --------------------------------------------------------------------------- #
# Die Zahlen. Jede Abfrage steht für eine Aussage im Briefing; was hier nicht
# herauskommt, kann das Modell nicht schreiben.
# --------------------------------------------------------------------------- #
def rows(sql: str, params: tuple = (), timeout_s: int = 120) -> list[list]:
    return mwdb.query(sql, params, limit=None, timeout_s=timeout_s, raw=True).rows


def one(sql: str, params: tuple = (), timeout_s: int = 120) -> list:
    got = rows(sql, params, timeout_s)
    return got[0] if got else []


def first(sql: str, params: tuple = (), timeout_s: int = 120) -> str:
    """Erster Wert der ersten Zeile als Text ("" wenn nichts kommt)."""
    got = one(sql, params, timeout_s)
    return mwdb.decode(got[0]) if got else ""


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def labels(ids: list[int], kind: str) -> dict[int, str]:
    """Labels zu Item- oder Property-Nummern, Englisch bevorzugt, sonst Deutsch."""
    if not ids:
        return {}
    view, col = ("v_item_terms", "item_id") if kind == "item" else ("v_property_terms", "property_id")
    marks = ", ".join(["%s"] * len(ids))
    out: dict[int, str] = {}
    for nr, lang, text in rows(f"SELECT {col}, language, text FROM {view} "
                               f"WHERE term_type = 'label' AND language IN ('en', 'de') "
                               f"AND {col} IN ({marks})", tuple(ids)):
        nr = _int(nr)
        if mwdb.decode(lang) == "en" or nr not in out:   # Englisch schlägt Deutsch
            out[nr] = mwdb.decode(text)
    return out


def collect(start: datetime, end: datetime) -> dict:
    """Alle Zahlen der Woche in einem Wörterbuch – die einzige Quelle für den Faktenblock."""
    a, b = stamp(start), stamp(end)
    prev_a = stamp(start - timedelta(days=7))
    data: dict = {"start": start, "end": end}

    total = one("SELECT COUNT(*), COUNT(DISTINCT rev_page), SUM(rev_parent_id = 0), "
                "COUNT(DISTINCT rev_actor) FROM revision "
                "WHERE rev_timestamp >= %s AND rev_timestamp < %s", (a, b))
    data["edits"], data["pages"], data["new_pages"], data["accounts"] = [_int(v) for v in total]
    data["prev_edits"] = _int(first("SELECT COUNT(*) FROM revision "
                                    "WHERE rev_timestamp >= %s AND rev_timestamp < %s", (prev_a, a)))

    data["per_day"] = [(mwdb.decode(d), _int(n), _int(neu)) for d, n, neu in
                       rows("SELECT LEFT(rev_timestamp, 8), COUNT(*), SUM(rev_parent_id = 0) "
                            "FROM revision WHERE rev_timestamp >= %s AND rev_timestamp < %s "
                            "GROUP BY 1 ORDER BY 1", (a, b))]

    data["namespaces"] = [(_int(ns), _int(n), _int(neu)) for ns, n, neu in
                          rows("SELECT page_namespace, COUNT(*), SUM(rev_parent_id = 0) "
                               "FROM v_revision WHERE rev_timestamp >= %s AND rev_timestamp < %s "
                               "GROUP BY 1 ORDER BY 2 DESC LIMIT 6", (a, b))]

    data["users"] = [(mwdb.decode(u), _int(n), _int(neu), _int(seiten)) for u, n, neu, seiten in
                     rows("SELECT user_name, COUNT(*), SUM(rev_parent_id = 0), COUNT(DISTINCT rev_page) "
                          "FROM v_revision WHERE rev_timestamp >= %s AND rev_timestamp < %s "
                          "GROUP BY user_name ORDER BY 2 DESC LIMIT 12", (a, b))]

    data["kinds"] = [(mwdb.decode(k) or "(no comment)", _int(n)) for k, n in
                     rows("SELECT SUBSTRING_INDEX(TRIM(SUBSTRING_INDEX("
                          "SUBSTRING_INDEX(comment, '*/', 1), '/*', -1)), ':', 1), COUNT(*) "
                          "FROM v_revision WHERE rev_timestamp >= %s AND rev_timestamp < %s "
                          "GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (a, b))]

    props = [(mwdb.decode(p), _int(n), _int(leute)) for p, n, leute in
             rows("SELECT REGEXP_SUBSTR(comment, 'P[0-9]+'), COUNT(*), COUNT(DISTINCT user_name) "
                  "FROM v_revision WHERE rev_timestamp >= %s AND rev_timestamp < %s "
                  "AND comment LIKE '%%Property:P%%' GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (a, b))]
    props = [(p, n, leute) for p, n, leute in props if p]
    plabels = labels([_int(p[1:]) for p, _n, _l in props], "property")
    data["properties"] = [(p, plabels.get(_int(p[1:]), ""), n, leute) for p, n, leute in props]

    items = [(mwdb.decode(t), _int(n)) for t, n in
             rows("SELECT page_title, COUNT(*) FROM v_revision "
                  "WHERE rev_timestamp >= %s AND rev_timestamp < %s AND page_namespace = %s "
                  "GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (a, b, ITEM_NS))]
    ilabels = labels([_int(t[1:]) for t, _n in items if t.startswith("Q")], "item")
    data["items"] = [(t, ilabels.get(_int(t[1:]), ""), n) for t, n in items if t.startswith("Q")]

    # Woran die beiden größten Konten saßen: je ein Beispielkommentar sagt mehr als die Zahl.
    data["samples"] = []
    for user, _n, neu, _s in data["users"][:3]:
        sample = first("SELECT LEFT(comment, 130) FROM v_revision "
                       "WHERE rev_timestamp >= %s AND rev_timestamp < %s AND user_name = %s "
                       + ("AND rev_parent_id = 0 " if neu else "") +
                       "ORDER BY rev_timestamp DESC LIMIT 1", (a, b, user))
        if sample:
            data["samples"].append((user, mwdb.humanize_comment(sample)))

    data["log"] = [(mwdb.decode(t), mwdb.decode(ac), _int(n)) for t, ac, n in
                   rows("SELECT log_type, log_action, COUNT(*) FROM v_log "
                        "WHERE log_timestamp >= %s AND log_timestamp < %s "
                        "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8", (a, b))]
    data["log_details"] = [tuple(mwdb.decode(v) for v in row) for row in
                           rows("SELECT log_type, log_action, user_name, log_title, LEFT(comment, 120) "
                                "FROM v_log WHERE log_timestamp >= %s AND log_timestamp < %s "
                                "AND log_type IN ('newusers', 'delete', 'upload', 'move', 'rights') "
                                "ORDER BY log_timestamp LIMIT 12", (a, b))]

    data["tags"] = [(mwdb.decode(t), _int(n)) for t, n in
                    rows("SELECT d.ctd_name, COUNT(*) FROM change_tag c "
                         "JOIN change_tag_def d ON d.ctd_id = c.ct_tag_id "
                         "JOIN revision r ON r.rev_id = c.ct_rev_id "
                         "WHERE r.rev_timestamp >= %s AND r.rev_timestamp < %s "
                         "GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (a, b), 180)]

    data["mirror_dump"] = first("SELECT v FROM mw_meta WHERE k = 'dump_date'")
    data["mirror_loaded"] = first("SELECT v FROM mw_meta WHERE k = 'loaded_at'")
    data["newest_edit"] = first("SELECT MAX(rev_timestamp) FROM revision")
    return data


def check(data: dict) -> None:
    """Abbrechen, statt eine sinnlose Mail zu schicken."""
    if data["edits"] <= 0:
        raise SystemExit("Keine Bearbeitungen in diesem Zeitraum – nichts zu berichten.")
    newest = re.sub(r"\D", "", data["newest_edit"] or "")[:14]
    if newest and newest < stamp(data["end"]):
        # Der Spiegel endet vor dem Ende der Berichtswoche: das MUSS in der Mail stehen.
        data["gap"] = (f"The mirror ends on {mwdb.ts_to_iso(newest)}, before the end of the "
                       f"reporting week – the last days may be incomplete.")


# --------------------------------------------------------------------------- #
# Faktenblock für das Modell (Englisch, damit nichts übersetzt werden muss)
# --------------------------------------------------------------------------- #
def facts(data: dict) -> str:
    def nr(n: int) -> str:
        return f"{n:,}"

    lines = [f"PERIOD: {pretty_day(data['start'])} 00:00 UTC to "
             f"{pretty_day(data['end'] - timedelta(days=1))} 24:00 UTC (7 days, Friday to Thursday).",
             f"SOURCE: local mirror of the FactGrid MediaWiki database, dump of "
             f"{data['mirror_dump']}, loaded {data['mirror_loaded']}.", ""]
    if data.get("gap"):
        lines += [f"CAVEAT: {data['gap']}", ""]

    change = data["edits"] - data["prev_edits"]
    percent = round(100 * change / data["prev_edits"]) if data["prev_edits"] else 0
    lines += ["TOTALS",
              f"- {nr(data['edits'])} edits by {nr(data['accounts'])} accounts, "
              f"{nr(data['pages'])} pages touched, {nr(data['new_pages'])} pages created.",
              f"- Week before: {nr(data['prev_edits'])} edits "
              f"({'up' if change >= 0 else 'down'} {abs(percent)} percent).", ""]

    lines.append("EDITS PER DAY (date, edits, of which new pages)")
    for day, n, neu in data["per_day"]:
        pretty = f"{day[6:8]}.{day[4:6]}."
        lines.append(f"- {pretty} {nr(n)} edits, {nr(neu)} new")
    lines.append("")

    lines.append("NAMESPACES (edits, new)")
    for ns, n, neu in data["namespaces"]:
        lines.append(f"- {mwdb.ns_name(ns) or 'Main'}: {nr(n)} edits, {nr(neu)} new")
    lines.append("")

    lines.append("MOST ACTIVE ACCOUNTS (edits, pages created, pages touched)")
    for user, n, neu, seiten in data["users"]:
        lines.append(f"- {user}: {nr(n)} edits, {nr(neu)} created, {nr(seiten)} pages")
    lines.append("")

    lines.append("KINDS OF CHANGE (from the edit summaries)")
    for kind, n in data["kinds"]:
        lines.append(f"- {kind}: {nr(n)}")
    lines.append("")

    if data["properties"]:
        lines.append("PROPERTIES MENTIONED IN EDIT SUMMARIES (property, label, mentions, accounts)")
        for pid, label, n, leute in data["properties"]:
            lines.append(f"- {pid} \"{label}\": {nr(n)} mentions by {leute} account(s)")
        lines.append("")

    if data["items"]:
        lines.append("MOST EDITED ITEMS")
        for qid, label, n in data["items"]:
            lines.append(f"- {qid} \"{label}\": {nr(n)} edits")
        lines.append("")

    if data["samples"]:
        lines.append("WHAT THE BUSIEST ACCOUNTS WERE DOING (one sample edit summary each)")
        for user, comment in data["samples"]:
            lines.append(f"- {user}: {comment}")
        lines.append("")

    lines.append("LOG BOOK")
    for typ, action, n in data["log"]:
        lines.append(f"- {typ}/{action}: {nr(n)}")
    for typ, action, user, title, comment in data["log_details"]:
        lines.append(f"- {typ}/{action} by {user}: {title} – {comment}")
    lines.append("")

    if data["tags"]:
        lines.append("TAGS (reverts and tool-made edits)")
        for tag, n in data["tags"]:
            lines.append(f"- {tag}: {nr(n)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Das Modell formuliert – aus nichts als dem Faktenblock
# --------------------------------------------------------------------------- #
PROMPT = """You write the weekly briefing for the mailing list of FactGrid, a Wikibase database
for historical research data. The readers are contributors: they know the project, they do not
know this week's figures.

Write the body of an email in English, at most 400 words. Use this structure:

Dear all,
<one paragraph, three or four sentences: what this week looked like>

## Numbers of the week
<five to seven lines, each starting with "- ". Put the daily figures into a single line and name
only the busiest days. List at most five accounts.>

## What happened
<two short paragraphs of prose about the one or two operations that dominate the week: what was
done, by whom, at what scale. Prefer names and plain sentences over figures here – two or three
numbers are enough, and do not repeat the lists from the section above. Then one or two
sentences on the smaller, hand-made work and the items people worked on. Whenever you name an
item or a property, give its label first and the identifier after it, like Sophie Schwarz (née
Becker) (Q1196919) or Archives at (P185) – an identifier on its own says nothing to a reader.>

## Also worth knowing
<three or four lines starting with "- ": reverts, deletions, uploads, new accounts, tool-made
edits – whatever the facts show.>

Formatting: plain text with two conventions only. Mark headings with "## " as shown. Put **every
account name and every figure that matters** between double asterisks, like **1,234 edits** or
**Olaf Simons** – but do not mark whole sentences. Write item and property identifiers plainly
as Q1196919 or P185, never as links.

Rules: use only the facts given below. Invent nothing – no motives, no plans, no praise, no
words like "impressive" or "busy week". If a figure stands out, say what it is, not what it
means. Round large numbers in prose ("around 111,000"), keep them exact in the bullet lines.
Names of people are written exactly as given.

Answer in exactly this format:
SUBJECT: <one line, mentioning the date range>
---
<the email body>

FACTS:
{facts}"""


def write_briefing(facts_text: str, model: str = "") -> tuple[str, str]:
    """(Betreff, Text) vom Modell. Wirft, wenn nichts Brauchbares zurückkommt – dann geht
    auch keine Mail raus."""
    import openai

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY fehlt (.env) – ohne Modell kein Briefing.")
    client = openai.OpenAI(base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                           api_key=key, timeout=300)
    # max_tokens großzügig: deepseek-flash denkt per Default, und ein zu kleines Budget geht
    # für das Denken drauf – die Antwort ist dann leer (schon passiert).
    answer = client.chat.completions.create(
        model=model or MODEL, max_tokens=16000,
        messages=[{"role": "user", "content": PROMPT.format(facts=facts_text)}])
    text = (answer.choices[0].message.content or "").strip()
    if len(text) < 400:
        raise SystemExit(f"Das Modell hat nichts Brauchbares geliefert "
                         f"(finish_reason={answer.choices[0].finish_reason}, {len(text)} Zeichen).")
    subject, _, body = text.partition("\n---")
    subject = re.sub(r"^\s*SUBJECT:\s*", "", subject, flags=re.I).strip().strip("*").strip()
    body = body.lstrip("-\n").strip()
    if not subject or len(body) < 300:
        raise SystemExit("Die Antwort des Modells hat nicht die verlangte Form (SUBJECT / --- / Text).")
    if "**" not in body:
        raise SystemExit("Die Antwort des Modells enthält keine Hervorhebungen – Formatvorgabe verfehlt.")
    return subject, body


# --------------------------------------------------------------------------- #
# Darstellung: derselbe Text als reiner Text und als HTML (fett, Q-IDs verlinkt)
# --------------------------------------------------------------------------- #
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ENTITY = re.compile(r"\b([QP])(\d{2,})\b")


def to_text(preamble: str, body: str) -> str:
    plain = _BOLD.sub(r"\1", body)
    plain = re.sub(r"(?m)^##\s*", "", plain)
    return f"[ {preamble} ]\n\n{plain.strip()}\n"


def _inline(line: str) -> str:
    """Escapen, dann fett setzen und Q-/P-IDs auf die Live-Instanz verlinken."""
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", html.escape(line))
    return _ENTITY.sub(
        lambda m: f'<a href="{SITE}wiki/{"Item" if m.group(1) == "Q" else "Property"}:{m.group(0)}" '
                  f'style="color:#7a5c2e">{m.group(0)}</a>', out)


def to_html(preamble: str, body: str, subject: str) -> str:
    """Schlichtes HTML – keine externen Stile, keine Bilder: das überlebt Thunderbird,
    Gmail und die Archivansicht der Liste."""
    # Zeilen werden zu Absätzen zusammengefasst: ein im Quelltext umbrochener Absatz ist EIN
    # <p>, und die Fortsetzung einer Aufzählungszeile gehört zu ihrem Punkt. Sonst zerfällt ein
    # von Hand umbrochener Text in lauter Einzelabsätze.
    parts, bullets, para = [], [], []

    def flush() -> None:
        if para:
            parts.append(f"<p style='margin:0 0 12px'>{_inline(' '.join(para))}</p>")
            para.clear()
        if bullets:
            parts.append("<ul style='margin:0 0 14px;padding-left:22px'>"
                         + "".join(f"<li style='margin:3px 0'>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in body.splitlines():
        line = line.strip()
        if not line:
            flush()
        elif re.fullmatch(r"-{3,}", line):
            flush()
            parts.append("<hr style='border:none;border-top:1px solid #ddd8cf;margin:22px 0'>")
        elif line.startswith("## "):
            flush()
            parts.append(f"<h2 style='font-size:16px;margin:22px 0 8px'>{_inline(line[3:])}</h2>")
        elif line.startswith("- "):
            if para:
                flush()
            bullets.append(_inline(line[2:]))
        elif bullets:
            bullets[-1] += " " + _inline(line)      # umbrochene Fortsetzung des Punkts
        else:
            para.append(line)
    flush()
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(subject)}</title>"
            f"</head><body style='margin:0;padding:18px;background:#f5f4f0'>"
            f"<div style='max-width:680px;margin:0 auto;background:#fff;border:1px solid #ddd8cf;"
            f"border-radius:10px;padding:20px 24px;font:15px/1.55 system-ui,-apple-system,"
            f"\"Segoe UI\",sans-serif;color:#1f1e1c'>"
            f"<p style='margin:0 0 18px;padding:9px 12px;background:#f0eee9;border-radius:8px;"
            f"color:#6f6b63;font-size:12.5px;line-height:1.5'><em>{html.escape(preamble)}</em></p>"
            + "".join(parts) + "</div></body></html>")


# --------------------------------------------------------------------------- #
# Versand
# --------------------------------------------------------------------------- #
def mailbox() -> tuple[str, str, str, int]:
    """(Server, Absender, Passwort, Port) aus .env; die Namen sind die des Postfach-Eintrags."""
    host = os.environ.get("host_name") or os.environ.get("MAIL_HOST", "")
    sender = os.environ.get("mailadresse") or os.environ.get("MAIL_FROM", "")
    password = os.environ.get("mail_password") or os.environ.get("MAIL_PASSWORD", "")
    port = int(os.environ.get("SMTP") or os.environ.get("MAIL_SMTP_PORT") or 465)
    return host, sender, password, port


def build_message(subject: str, text: str, html_body: str, to: str, sender: str) -> EmailMessage:
    """Die fertige Mail: Text und HTML als Alternativen, damit sie in jedem Programm lesbar ist."""
    msg = EmailMessage()
    msg["From"] = f"{os.environ.get('BRIEFING_FROM_NAME', 'FactGrid')} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"       # Abwesenheitsantworten sollen nicht antworten
    msg.set_content(text, charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
    return msg


def send(subject: str, text: str, html_body: str, to: str) -> str:
    host, sender, password, port = mailbox()
    if not (host and sender and password):
        raise SystemExit("Postfach fehlt in .env (host_name, mailadresse, mail_password, SMTP).")
    msg = build_message(subject, text, html_body, to, sender)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(sender, password)
        refused = smtp.send_message(msg)
    if refused:
        raise SystemExit(f"Empfänger abgelehnt: {refused}")
    return msg["Message-ID"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Wochenbriefing zu FactGrid erzeugen und verschicken")
    ap.add_argument("--send", action="store_true", help="wirklich verschicken (sonst nur anzeigen)")
    ap.add_argument("--to", default=DEFAULT_TO, help=f"Empfänger (Vorgabe {DEFAULT_TO})")
    ap.add_argument("--weeks-back", type=int, default=1, help="1 = letzte abgeschlossene Woche")
    ap.add_argument("--facts", action="store_true", help="nur die Zahlen zeigen, kein Modell, keine Mail")
    ap.add_argument("--model", default="", help=f"Modell (Vorgabe {MODEL})")
    ap.add_argument("--save", default="", metavar="DATEI",
                    help="Betreff und Text zusätzlich in eine Datei schreiben (Archiv)")
    args = ap.parse_args()

    if not mwdb.configured():
        raise SystemExit("Der MediaWiki-Spiegel ist nicht konfiguriert (MW_DB_PASSWORD in .env).")
    start, end = week_range(weeks_back=max(1, args.weeks_back))
    data = collect(start, end)
    check(data)
    facts_text = facts(data)
    if args.facts:
        print(facts_text)
        return

    subject, body = write_briefing(facts_text, args.model)
    preamble = PREAMBLE.format(model=(args.model or MODEL))
    text, html_body = to_text(preamble, body), to_html(preamble, body, subject)
    if args.save:
        Path(args.save).write_text(f"SUBJECT: {subject}\n---\n{body}\n", encoding="utf-8")
    if not args.send:
        print(f"--- Betreff: {subject}\n--- An: {args.to} (Probelauf, nichts verschickt)\n")
        print(text)
        return
    message_id = send(subject, text, html_body, args.to)
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  Briefing an {args.to} verschickt: "
          f"{subject}  {message_id}")


if __name__ == "__main__":
    main()
