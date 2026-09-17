"""
Tests für das wöchentliche Briefing (scripts/weekly_briefing.py): Berichtswoche, Faktenblock
aus den SQL-Zahlen, die Schutzregeln gegen eine halbgare Mail, die Darstellung (fett, Q-/P-IDs,
Escaping) und die fertige Nachricht – ohne Datenbank, ohne Modell, ohne SMTP.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "scripts"))

# Vor dem Import festlegen (load_env überschreibt gesetzte Variablen nicht)
os.environ.update({"MW_DB_PASSWORD": "", "BRIEFING_TO": "liste@example.org",
                   "DEEPSEEK_API_KEY": "sk-test", "BRIEFING_MODEL": "deepseek-flash"})

import weekly_briefing as wb  # noqa: E402

UTC = timezone.utc


def test_week_range_is_the_last_full_monday_week():
    # Montag 21.09.2026, 08:00 UTC – der Timer läuft: Berichtswoche ist Mo 14. bis So 20.
    montag = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    start, end = wb.week_range(montag)
    assert (start, end) == (datetime(2026, 9, 14, tzinfo=UTC), datetime(2026, 9, 21, tzinfo=UTC))
    assert wb.stamp(start) == "20260914000000" and wb.stamp(end) == "20260921000000"
    # Mitten in der Woche gefragt: immer noch die letzte ABGESCHLOSSENE Woche
    mittwoch = datetime(2026, 9, 23, 17, 30, tzinfo=UTC)
    assert wb.week_range(mittwoch) == (start, end)
    # Eine Woche weiter zurück
    assert wb.week_range(montag, weeks_back=2)[0] == datetime(2026, 9, 7, tzinfo=UTC)
    # Zeitzone: eine lokale Zeit wird nach UTC gerechnet, nicht abgeschnitten
    berlin = datetime(2026, 9, 21, 1, 30, tzinfo=timezone(timedelta(hours=2)))   # = 23:30 UTC am Sonntag
    assert wb.week_range(berlin)[1] == datetime(2026, 9, 14, tzinfo=UTC)


def _data() -> dict:
    """Zahlen wie sie collect() liefert – hier von Hand, damit der Test ohne Datenbank läuft."""
    return {
        "start": datetime(2026, 9, 14, tzinfo=UTC), "end": datetime(2026, 9, 21, tzinfo=UTC),
        "edits": 158801, "pages": 144869, "new_pages": 15968, "accounts": 29, "prev_edits": 111351,
        "per_day": [("20260914", 6577, 81), ("20260915", 67511, 2415)],
        "namespaces": [(120, 158696, 15943), (122, 40, 8)],
        "users": [("Alexander Wellhäuser", 111046, 0, 110169), ("Adam Anderson", 33454, 15454, 29499)],
        "kinds": [("wbremoveclaims-remove", 115565), ("wbeditentity-create-item", 15658)],
        "properties": [("P185", "Archives at", 111056, 3), ("P91", "Member of", 1704, 11)],
        "items": [("Q1196919", "Sophie Schwarz (née Becker)", 110)],
        "samples": [("Adam Anderson", "Item angelegt: Create FactGrid CDLI artifact")],
        "log": [("create", "create", 15971), ("delete", "delete", 3)],
        "log_details": [("upload", "upload", "Olaf Simons", "Screenshot.png", "why Wikibases need it")],
        "tags": [("mw-manual-revert", 303)],
        "mirror_dump": "2026-09-21", "mirror_loaded": "2026-09-21 06:31:00",
        "newest_edit": "20260921053000",
    }


def test_facts_carry_every_number_the_model_may_use():
    text = wb.facts(_data())
    for wanted in ["Mon 14 Sep 2026", "Sun 20 Sep 2026", "158,801 edits by 29 accounts",
                   "Week before: 111,351 edits (up 43 percent)", "15.09. 67,511 edits",
                   "Item: 158,696 edits", "Alexander Wellhäuser: 111,046 edits",
                   "wbremoveclaims-remove: 115,565", 'P185 "Archives at": 111,056 mentions by 3',
                   'Q1196919 "Sophie Schwarz (née Becker)": 110 edits',
                   "Adam Anderson: Item angelegt", "create/create: 15,971",
                   "upload/upload by Olaf Simons", "mw-manual-revert: 303",
                   "dump of 2026-09-21"]:
        assert wanted in text, wanted
    # Zunahme wird als solche benannt
    mehr = dict(_data(), prev_edits=100000)
    assert "(up 59 percent)" in wb.facts(mehr)


def test_check_stops_before_a_pointless_or_misleading_mail():
    leer = dict(_data(), edits=0)
    try:
        wb.check(leer)
        raise AssertionError("ohne Bearbeitungen darf nichts verschickt werden")
    except SystemExit:
        pass
    # Spiegel endet vor dem Ende der Woche: kein Abbruch, aber ein Hinweis für das Modell
    alt = dict(_data(), newest_edit="20260918120000")
    wb.check(alt)
    assert "before the end of the reporting week" in alt["gap"]
    assert "CAVEAT" in wb.facts(alt)
    frisch = _data()
    wb.check(frisch)
    assert "gap" not in frisch


class _FakeOpenAI:
    """Ersetzt das openai-Paket: liefert genau den Text, den der Test vorgibt."""

    antwort = ""

    def __init__(self, **kw):
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        _FakeOpenAI.gesehen = kw
        return NS(choices=[NS(message=NS(content=_FakeOpenAI.antwort), finish_reason="stop")],
                  usage=NS(total_tokens=1))


GUT = ("SUBJECT: FactGrid weekly briefing, 14-20 September 2026\n---\n"
       "Dear all,\n\nThis week **158,801 edits**.\n\n## Numbers of the week\n"
       "- **Adam Anderson**: **33,454 edits**.\n" + "Filler so the body is long enough. " * 12)


def test_model_answer_is_checked_before_anything_is_sent():
    sys.modules["openai"] = NS(OpenAI=_FakeOpenAI)
    _FakeOpenAI.antwort = GUT
    subject, body = wb.write_briefing("FACTS")
    assert subject == "FactGrid weekly briefing, 14-20 September 2026"
    assert body.startswith("Dear all,") and "**Adam Anderson**" in body
    assert "FACTS" in _FakeOpenAI.gesehen["messages"][0]["content"]
    assert _FakeOpenAI.gesehen["max_tokens"] >= 8000        # sonst frisst der Denkmodus die Antwort

    for kaputt, warum in [("", "leere Antwort"),
                          ("SUBJECT: x\n---\nzu kurz", "zu kurzer Text"),
                          (GUT.replace("**", ""), "keine Hervorhebungen"),
                          (GUT.replace("SUBJECT:", "Betreff:").replace("\n---\n", "\n"), "falsches Format")]:
        _FakeOpenAI.antwort = kaputt
        try:
            wb.write_briefing("FACTS")
            raise AssertionError(f"hätte abbrechen müssen: {warum}")
        except SystemExit:
            pass


def test_rendering_marks_up_without_letting_html_through():
    body = ("Dear all,\n\nAround **57,400 edits** this week.\n\n## Numbers of the week\n"
            "- **Adam Anderson**: **38,771 edits**\n- Item Q1196919 and property P185\n\n"
            "## What happened\nA label with <angle brackets> & an ampersand.")
    pre = wb.PREAMBLE.format(model="deepseek-flash")

    text = wb.to_text(pre, body)
    assert text.startswith("[ Automatically generated:")
    assert "**" not in text and "## " not in text        # reiner Text bleibt rein
    assert "Around 57,400 edits" in text and "Numbers of the week" in text

    html = wb.to_html(pre, body, "Betreff & <Zeichen>")
    assert "<strong>57,400 edits</strong>" in html and "<strong>Adam Anderson</strong>" in html
    assert 'href="https://database.factgrid.de/wiki/Item:Q1196919"' in html
    assert 'href="https://database.factgrid.de/wiki/Property:P185"' in html
    assert "<h2" in html and html.count("<li") == 2 and "<ul" in html
    assert "<hr" in wb.to_html(pre, body + "\n\n---\n\nEmbedded example follows.", "x")
    # Ein von Hand umbrochener Absatz bleibt EIN Absatz, eine umbrochene Aufzählung EIN Punkt
    umbrochen = ("Dear all,\n\nOne paragraph that happens\nto be wrapped in the source.\n\n"
                 "- a bullet that is\n  wrapped too\n- a second bullet")
    wrapped = wb.to_html(pre, umbrochen, "x")
    assert "<p style='margin:0 0 12px'>One paragraph that happens to be wrapped in the source.</p>" in wrapped
    assert wrapped.count("<li") == 2 and "a bullet that is wrapped too" in wrapped
    assert "&lt;angle brackets&gt;" in html and "&amp; an ampersand" in html
    assert "<angle brackets>" not in html                 # nichts Ungeescaptes im HTML
    assert "&lt;Zeichen&gt;" in html                      # auch nicht im Titel
    assert "Automatically generated" in html


def test_message_is_a_readable_two_part_mail():
    msg = wb.build_message("Betreff", "reiner Text", "<p>HTML</p>", "liste@example.org",
                           "factgrid@example.com")
    assert msg["To"] == "liste@example.org" and msg["Subject"] == "Betreff"
    assert msg["From"].endswith("<factgrid@example.com>")
    assert msg["Auto-Submitted"] == "auto-generated"      # kein Abwesenheits-Pingpong mit der Liste
    assert msg["Message-ID"].endswith("@example.com>")
    assert msg.get_content_type() == "multipart/alternative"
    teile = [p.get_content_type() for p in msg.iter_parts()]
    assert teile == ["text/plain", "text/html"]
    assert "reiner Text" in msg.get_body(("plain",)).get_content()
    assert "<p>HTML</p>" in msg.get_body(("html",)).get_content()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
