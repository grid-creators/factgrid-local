"""
envfile.py – sehr einfache .env-Unterstützung für die Skripte um den MCP-Server herum
(agent/mini_agent.py, chat/server.py): KEY=VALUE, '#'-Kommentare, kein Überschreiben
bereits gesetzter Variablen. Bewusst ohne python-dotenv, damit kein weiteres Paket
nötig ist.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def load_env(path: Path | str) -> None:
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = re.split(r"\s+#", v, maxsplit=1)[0].strip().strip('"').strip("'")
        if k.strip() and v and k.strip() not in os.environ:
            os.environ[k.strip()] = v
