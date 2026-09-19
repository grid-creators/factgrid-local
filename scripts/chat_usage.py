#!/usr/bin/env python3
"""
chat_usage.py – Token-Verbrauch des Web-Chats auszählen.

Der Chat schreibt je Antwort eine Zeile nach .chat-usage.jsonl (FACTGRID_CHAT_USAGE):

    {"ts": "2026-09-19T09:12:03", "user": "Olaf Simons", "model": "openai/gpt-5.6-luna",
     "input": 48213, "cached": 31002, "cache_write": 0, "output": 1877,
     "steps": 3, "seconds": 21.4, "chat": "5f3c…", "key": "profil"}

`input` ist alles, was in die Requests ging (die aus dem Cache gelesenen Tokens
eingerechnet), `cached` der billigere Teil davon, `cache_write` das teurere Schreiben in
den Cache, `output` das Erzeugte, `steps` die Zahl der Requests im Tool-Loop. `key` sagt,
wer bezahlt hat: "profil" = eigener Schlüssel, "server" = der aus .env. Hat der Anbieter
ein anderes Modell bedient als angefragt (datierter Schnappschuss, Umleitung), steht das
zusätzlich in "model_api"; gezählt wird nach dem angefragten "model".

    make usage                                  # alle Benutzer, ganzer Zeitraum
    python scripts/chat_usage.py --since 2026-09 --by user+model
    python scripts/chat_usage.py --user "Olaf Simons" --by day
    python scripts/chat_usage.py --json          # zum Weiterrechnen
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("input", "cached", "cache_write", "output")


def read(path: Path, since: str = "", until: str = "", user: str = "", model: str = "") -> list[dict]:
    """Die Zeilen des Zeitraums. Zeitstempel sind ISO-Strings, ein Präfixvergleich genügt
    darum für Jahr (2026), Monat (2026-09) und Tag (2026-09-19). Eine fehlende Datei ist kein
    Fehler - dann hat der Chat eben noch nichts notiert."""
    if not path.exists():
        return []
    rows = []
    for nr, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(f"{path}:{nr}: keine JSON-Zeile, übersprungen", file=sys.stderr)
            continue
        ts = str(row.get("ts", ""))
        if since and ts < since:
            continue
        if until and ts[:len(until)] > until:
            continue
        if user and row.get("user") != user:
            continue
        if model and model not in str(row.get("model", "")):
            continue
        rows.append(row)
    return rows


def group(rows: list[dict], by: str) -> dict[str, dict]:
    """Nach Benutzer, Modell, Tag, Monat oder Kombinationen davon summieren."""
    keys = {"user": lambda r: str(r.get("user") or "?"),
            "model": lambda r: str(r.get("model") or "?"),
            "key": lambda r: str(r.get("key") or "?"),
            "day": lambda r: str(r.get("ts", ""))[:10],
            "month": lambda r: str(r.get("ts", ""))[:7]}
    teile = [k.strip() for k in by.split("+") if k.strip()]
    unbekannt = [k for k in teile if k not in keys]
    if unbekannt:
        sys.exit(f"--by kennt {', '.join(unbekannt)} nicht; möglich: {', '.join(keys)} (mit + verbunden)")
    out: dict[str, dict] = {}
    for row in rows:
        name = " · ".join(keys[k](row) for k in teile)
        eintrag = out.setdefault(name, dict.fromkeys(FIELDS, 0) | {"antworten": 0, "sekunden": 0.0})
        for field in FIELDS:
            eintrag[field] += int(row.get(field, 0) or 0)
        eintrag["antworten"] += 1
        eintrag["sekunden"] += float(row.get("seconds", 0) or 0)
    return dict(sorted(out.items(), key=lambda kv: -(kv[1]["input"] + kv[1]["output"])))


def zahl(n) -> str:
    """Tausenderpunkte wie im Rest der Oberfläche."""
    return f"{round(n):,}".replace(",", ".")


def table(gruppen: dict[str, dict], titel: str) -> str:
    kopf = ("", "Antworten", "Eingabe", "davon Cache", "Cache neu", "Ausgabe", "Minuten")

    def werte(name: str, e: dict) -> tuple[str, ...]:
        return (name, zahl(e["antworten"]), zahl(e["input"]), zahl(e["cached"]),
                zahl(e["cache_write"]), zahl(e["output"]), zahl(e["sekunden"] / 60))

    zeilen = [werte(name, e) for name, e in gruppen.items()]
    spalten = (*FIELDS, "antworten", "sekunden")
    zeilen.append(werte("Summe", {f: sum(e[f] for e in gruppen.values()) for f in spalten}))
    breite = [max(len(z[i]) for z in (kopf, *zeilen)) for i in range(len(kopf))]

    def zeile(v: tuple[str, ...]) -> str:
        return "  ".join(s.ljust(breite[i]) if i == 0 else s.rjust(breite[i])
                         for i, s in enumerate(v)).rstrip()

    strich = "  ".join("-" * b for b in breite)
    return "\n".join([titel, "", zeile(kopf), strich, *map(zeile, zeilen[:-1]),
                      strich, zeile(zeilen[-1])])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=os.environ.get("FACTGRID_CHAT_USAGE", str(ROOT / ".chat-usage.jsonl")))
    ap.add_argument("--since", default="", help="ab diesem Zeitpunkt (2026, 2026-09, 2026-09-19)")
    ap.add_argument("--until", default="", help="bis einschließlich (2026, 2026-09, 2026-09-19)")
    ap.add_argument("--user", default="", help="nur dieser FactGrid-Benutzer")
    ap.add_argument("--model", default="", help="nur Modelle, in denen das vorkommt (z. B. deepseek)")
    ap.add_argument("--by", default="user", help="user, model, key, day, month – auch verbunden: user+model")
    ap.add_argument("--json", action="store_true", help="die Gruppen als JSON statt als Tabelle")
    args = ap.parse_args()

    path = Path(args.file)
    rows = read(path, args.since, args.until, args.user, args.model)
    if not rows:
        print(f"Nichts zu zählen: {path}" + ("" if path.exists() else " gibt es noch nicht")
              + (" – kein Eintrag im gewählten Zeitraum." if path.exists() else "."))
        return
    gruppen = group(rows, args.by)
    if args.json:
        print(json.dumps(gruppen, ensure_ascii=False, indent=2))
        return
    zeitraum = f"{rows[0]['ts'][:10]} bis {rows[-1]['ts'][:10]}"
    print(table(gruppen, f"Token-Verbrauch des Web-Chats nach {args.by} ({zeitraum}, "
                         f"{len(rows)} Antworten)"))
    # Antworten ohne Zahlen würden die Summen still zu niedrig machen - lieber benennen.
    ohne = [r for r in rows if not (int(r.get("input", 0) or 0) + int(r.get("output", 0) or 0))]
    if ohne:
        print(f"\nHinweis: {len(ohne)} von {len(rows)} Antworten ohne Tokenzahlen "
              f"(zuletzt {ohne[-1]['ts'][:16]}, {ohne[-1].get('model', '?')}) – der Anbieter hat "
              f"keine geliefert. Die Summen sind um diese Antworten zu niedrig.")


if __name__ == "__main__":
    main()
