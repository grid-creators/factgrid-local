#!/usr/bin/env bash
# Traegt einen API-Schluessel in .env ein, ohne ihn anzuzeigen oder in die
# Shell-History zu schreiben. .env steht in .gitignore.
#
#   scripts/set_api_key.sh openrouter|anthropic|openai|deepseek [--show]
#
# Ohne Argument werden nur die aktuell gesetzten Anbieter aufgelistet.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVFILE="$ROOT/.env"
[ -f "$ENVFILE" ] || { echo "Keine .env in $ROOT (cp .env.example .env)"; exit 1; }

declare -A VARS=(
  [openrouter]=OPENROUTER_API_KEY
  [anthropic]=ANTHROPIC_API_KEY
  [openai]=OPENAI_API_KEY
  [deepseek]=DEEPSEEK_API_KEY
)

status() {
  echo "Status in $ENVFILE:"
  for p in openrouter anthropic openai deepseek; do
    v="${VARS[$p]}"
    line="$(sed -n "s/^${v}=//p" "$ENVFILE" | head -1)"
    if [ -n "${line// }" ]; then printf "  %-11s %-20s gesetzt (%s Zeichen)\n" "$p" "$v" "${#line}"
    else printf "  %-11s %-20s leer\n" "$p" "$v"; fi
  done
}

[ $# -ge 1 ] || { status; exit 0; }
PROVIDER="$1"
VAR="${VARS[$PROVIDER]:-}"
[ -n "$VAR" ] || { echo "Unbekannter Anbieter: $PROVIDER (openrouter|anthropic|openai|deepseek)"; exit 1; }

read -rsp "Schluessel fuer $PROVIDER ($VAR), Eingabe bleibt unsichtbar: " KEY
echo
[ -n "$KEY" ] || { echo "Leer - nichts geaendert."; exit 1; }

KEY="$KEY" VAR="$VAR" ENVFILE="$ENVFILE" python3 - <<'PY'
import os, re, pathlib
p = pathlib.Path(os.environ["ENVFILE"]); s = p.read_text()
var, key = os.environ["VAR"], os.environ["KEY"]
line = f"{var}={key}"
if re.search(rf"^{re.escape(var)}=.*$", s, flags=re.M):
    s = re.sub(rf"^{re.escape(var)}=.*$", lambda _: line, s, flags=re.M)
else:
    s = s.rstrip("\n") + "\n" + line + "\n"
p.write_text(s)
PY
unset KEY
chmod 600 "$ENVFILE"
echo "$VAR eingetragen."
status
