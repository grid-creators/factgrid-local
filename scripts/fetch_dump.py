#!/usr/bin/env python3
"""
fetch_dump.py – holt den neuesten *vollständigen* FactGrid-JSON-Dump.

FactGrid schreibt täglich (~22:00 UTC) nach https://database.factgrid.de/dumps/YYYY-MM-DD.json.gz
und hält 90 Tage vor. Vereinzelt sind Dumps deutlich zu klein (abgebrochene Läufe, z. B. 139 MB
statt ~1 GB) – solche Dateien werden anhand ihres Content-Length übersprungen.

Serverlast: ein Verzeichnis-Listing, wenige HEAD-Requests, ein Download (fortsetzbar).
Ergebnis: <dump-dir>/<datum>.json.gz, Symlink latest.json.gz, Datei DUMP_DATE.
"""
from __future__ import annotations

import argparse
import gzip
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

UA = "factgrid-local-mirror/0.1 (lokaler QLever-Spiegel; ein Download pro Aktualisierung)"
NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.json\.gz")


def http(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=60)


def listing(base: str) -> list[str]:
    with http(base) as r:
        html = r.read().decode("utf-8", "replace")
    return sorted(set(NAME_RE.findall(html)), reverse=True)


def size_of(url: str) -> int:
    with http(url, "HEAD") as r:
        return int(r.headers.get("Content-Length") or 0)


def gzip_ok(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)  # erzwingt Dekompression bis zum Ende inkl. CRC
        return True
    except Exception:
        return False


def download(url: str, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Range": f"bytes={have}-"} if have else {})})
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:  # Range jenseits des Dateiendes → von vorn beginnen
            part.unlink()
            return download(url, dest)
        raise
    with r, open(part, "ab" if have else "wb") as out:
        if have and r.status != 206:
            out.seek(0); out.truncate()
        total = have + int(r.headers.get("Content-Length") or 0)
        done = have
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r[fetch] {done / 1e6:8.0f} / {total / 1e6:.0f} MB", end="", file=sys.stderr)
    print(file=sys.stderr)
    if not gzip_ok(part):
        part.unlink()
        raise SystemExit(f"[fetch] {part.name} war kein intaktes gzip – verworfen, bitte erneut starten")
    part.rename(dest)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("FACTGRID_DUMP_URL", "https://database.factgrid.de/dumps/"))
    ap.add_argument("--dump-dir", default=os.environ.get("FACTGRID_DUMP_DIR", str(Path(__file__).resolve().parent.parent / "dumps")))
    ap.add_argument("--min-ratio", type=float, default=0.8, help="Mindestgröße relativ zum größten der letzten Dumps")
    ap.add_argument("--probe", type=int, default=6, help="so viele jüngste Dumps per HEAD prüfen")
    ap.add_argument("--keep", type=int, default=2, help="so viele lokale Dumps behalten")
    ap.add_argument("--date", help="bestimmten Dump (YYYY-MM-DD) erzwingen")
    a = ap.parse_args()

    base = a.base_url if a.base_url.endswith("/") else a.base_url + "/"
    ddir = Path(a.dump_dir)
    ddir.mkdir(parents=True, exist_ok=True)

    if a.date:
        chosen = a.date
    else:
        names = listing(base)
        if not names:
            raise SystemExit("[fetch] keine Dumps im Listing gefunden")
        probe = names[: a.probe]
        sizes = {d: size_of(f"{base}{d}.json.gz") for d in probe}
        ref = max(sizes.values())
        chosen = None
        for d in probe:  # neueste zuerst
            if sizes[d] >= ref * a.min_ratio:
                chosen = d
                break
            print(f"[fetch] überspringe {d} ({sizes[d] / 1e6:.0f} MB, verdächtig klein)", file=sys.stderr)
        if not chosen:
            raise SystemExit("[fetch] kein brauchbarer Dump")

    dest = ddir / f"{chosen}.json.gz"
    if dest.exists() and gzip_ok(dest):
        print(f"[fetch] {dest.name} bereits vorhanden und intakt", file=sys.stderr)
    else:
        print(f"[fetch] lade {base}{dest.name}", file=sys.stderr)
        download(f"{base}{dest.name}", dest)

    latest = ddir / "latest.json.gz"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(dest.name)
    (ddir / "DUMP_DATE").write_text(chosen + "\n")
    print(f"[fetch] aktuell: {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)", file=sys.stderr)

    old = [p for p in sorted(ddir.glob("????-??-??.json.gz"), reverse=True)[a.keep:] if p != dest]
    for p in old:
        p.unlink()
        print(f"[fetch] entfernt {p.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
