# FactGrid lokal: Dump → Turtle → QLever → MCP → LLM (per API)
#
#   make fetch      JSON-Dump holen (dumps/latest.json.gz)
#   make convert    Turtle erzeugen (qlever/factgrid.ttl.gz)
#   make index      QLever-Index bauen (im Verzeichnis qlever/)
#   make start      QLever-Server starten (Port 7003)
#   make ui         QLever-UI starten (Port 8176) – trägt vorher die FactGrid-Prefixe ein
#   make refresh    fetch + convert + index + Neustart (z. B. wöchentlich per systemd-timer/cron)
#   make mcp        MCP-Server im HTTP-Modus (für Open WebUI); Claude Code nutzt .mcp.json (stdio)
#   make chat       Web-Chatbot mit Modellauswahl (Anthropic/OpenAI) auf Port 8177
#   make test       Unit-/Integrationstests ohne QLever (rdflib-Mock)
#   make smoke      Referenzabfragen gegen den laufenden QLever
#   make agent      Plan B: leichtgewichtiger Agent auf der Kommandozeile (OpenRouter/Anthropic)
#   make mwdb       MediaWiki-SQL-Dump (Bearbeitungsgeschichte) in die lokale MariaDB laden
#   make mwdb-list  nur die Tabellen des SQL-Dumps mit Größen anzeigen

# Alle uv-Aufrufe nutzen --all-extras: mcp/.venv ist EINE gemeinsame Umgebung.
# 'uv sync --extra dev' wuerde die chat-Pakete daraus entfernen und den
# laufenden Dienst factgrid-chat mitten im Betrieb zerlegen.
SHELL      := /bin/bash
.SHELLFLAGS := -o pipefail -ec
ROOT       := $(abspath .)
QDIR       := qlever
DUMPS      := dumps
WORKERS    ?= $(shell nproc)
FLAVOR     ?= full          # full | truthy (truthy = kleiner Index zum Ausprobieren)
PY         ?= python3
QLEVER     ?= qlever        # pipx install qlever
# harte Grenze des QLever-Containers (Host hat 15 GB)
CONTAINER_MEMORY ?= 11g

.PHONY: fetch convert index start stop status ui refresh mcp chat test smoke agent mwdb mwdb-list clean

fetch:
	$(PY) scripts/fetch_dump.py --dump-dir $(DUMPS)

convert: $(DUMPS)/latest.json.gz
	mkdir -p $(QDIR)
	cp $(DUMPS)/DUMP_DATE $(QDIR)/DUMP_DATE
	$(PY) scripts/wb2rdf.py $(DUMPS)/latest.json.gz --flavor $(FLAVOR) --workers $(WORKERS) \
	  | (command -v pigz >/dev/null && pigz -p $(WORKERS) || gzip) > $(QDIR)/factgrid.ttl.gz.part
	mv $(QDIR)/factgrid.ttl.gz.part $(QDIR)/factgrid.ttl.gz
	ls -la $(QDIR)/factgrid.ttl.gz

# Qleverfile.in ist die Vorlage; die benutzbare Datei entsteht in qlever/. Die Vorlage heißt
# bewusst NICHT "Qleverfile": sonst nimmt ein "qlever start" im Wurzelverzeichnis sie her und
# mountet das Wurzelverzeichnis als /index – der Server findet dann keinen Index (Abschnitt 7).
$(QDIR)/Qleverfile: Qleverfile.in $(QDIR)/DUMP_DATE
	mkdir -p $(QDIR)
# Der ACCESS_TOKEN kommt aus .env (nicht versioniert), damit in Qleverfile.in kein
# Geheimnis steht. qlever/ ist ebenfalls in .gitignore.
	TOKEN=$$(sed -n "s/^QLEVER_ACCESS_TOKEN=//p" .env 2>/dev/null | sed "s/[[:space:]]*#.*//" | head -1); \
	[ -n "$$TOKEN" ] || TOKEN=factgrid_lokal_bitte_aendern; \
	sed -e "s/__DUMP_DATE__/$$(cat $(QDIR)/DUMP_DATE 2>/dev/null || echo unbekannt)/" \
	    -e "s|__ACCESS_TOKEN__|$$TOKEN|" Qleverfile.in > $(QDIR)/Qleverfile

$(QDIR)/DUMP_DATE:
	mkdir -p $(QDIR) && (cp $(DUMPS)/DUMP_DATE $(QDIR)/DUMP_DATE 2>/dev/null || echo unbekannt > $(QDIR)/DUMP_DATE)

index: $(QDIR)/Qleverfile
	cd $(QDIR) && $(QLEVER) index --overwrite-existing

start: $(QDIR)/Qleverfile
	cd $(QDIR) && $(QLEVER) start
# Harte Speichergrenze fuer den Container: QLevers MEMORY_FOR_QUERIES ist nur eine
# Buchhaltung und wird ueberschritten. Ohne Deckel hat der Kernel auf dieser 15-GB-
# Maschine schon einmal qlever-server per OOM-Killer erledigt (und haette ebenso gut
# einen Nachbardienst treffen koennen). Bei SYSTEM = native greift die Zeile nicht.
	-@docker update --memory $(CONTAINER_MEMORY) --memory-swap $(CONTAINER_MEMORY) \
	   qlever.server.$$(sed -n 's/^NAME *= *//p' $(QDIR)/Qleverfile) >/dev/null 2>&1 \
	   && echo "Container-Speichergrenze: $(CONTAINER_MEMORY)"

stop:
	cd $(QDIR) && $(QLEVER) stop || true

status:
	cd $(QDIR) && $(QLEVER) status

ui: $(QDIR)/Qleverfile
	$(PY) scripts/ui_prefixes.py $(QDIR)/Qleverfile-ui.yml
	cd $(QDIR) && $(QLEVER) ui

refresh: fetch convert stop index start
	rm -f ~/.cache/factgrid-mcp/*.txt
	@echo "Spiegel aktualisiert: Dump vom $$(cat $(QDIR)/DUMP_DATE)"

mcp:
	cd mcp && uv run --all-extras factgrid-mcp --http

chat:
	cd mcp && uv sync --all-extras >/dev/null && uv run --all-extras python ../chat/server.py

test:
	cd mcp && uv sync --all-extras >/dev/null
	cd mcp && uv run --all-extras python ../tests/test_wb2rdf.py
	cd mcp && uv run --all-extras python ../tests/test_mcp.py
	cd mcp && uv run --all-extras python ../tests/test_mwdb.py
	cd mcp && uv run --all-extras python ../tests/test_chat_backends.py

smoke:
	cd mcp && uv sync --all-extras >/dev/null
	cd mcp && uv run --all-extras python ../eval/run_eval.py --smoke

agent:
	cd mcp && uv run --with openai --with anthropic python ../agent/mini_agent.py

# MediaWiki-Datenbank: neuester *.sql.gz aus MW_DUMP_DIR (.env, Default /srv/data/factgrid/mediawiki)
# → MariaDB-Datenbank factgrid_mw, nur öffentliche Tabellen (README 3.7). Ein bereits geladener
# Dump wird übersprungen (MW_FORCE=1 erzwingt), MW_DUMP=pfad lädt eine bestimmte Datei,
# MW_TEXT=1 nimmt die Seiteninhalte mit (≈ 160 GB, Stunden). Der Schema-Cache wird geleert.
mwdb:
	$(PY) scripts/mw_load.py --env .env $(if $(MW_DUMP),--dump $(MW_DUMP)) \
	  $(if $(MW_FORCE),--force) $(if $(MW_TEXT),--with-text)
	rm -f ~/.cache/factgrid-mcp/*.txt
	@echo "Hinweis: laufende Dienste (factgrid-chat) sehen die neuen Tools erst nach einem Neustart."

mwdb-list:
	$(PY) scripts/mw_load.py --list $(if $(MW_DUMP),--dump $(MW_DUMP))

clean:
	rm -rf $(QDIR)/factgrid.ttl.gz.part
