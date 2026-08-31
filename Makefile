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

SHELL      := /bin/bash
.SHELLFLAGS := -o pipefail -ec
ROOT       := $(abspath .)
QDIR       := qlever
DUMPS      := dumps
WORKERS    ?= $(shell nproc)
FLAVOR     ?= full          # full | truthy (truthy = kleiner Index zum Ausprobieren)
PY         ?= python3
QLEVER     ?= qlever        # pipx install qlever

.PHONY: fetch convert index start stop status ui refresh mcp chat test smoke agent clean

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
	sed "s/__DUMP_DATE__/$$(cat $(QDIR)/DUMP_DATE 2>/dev/null || echo unbekannt)/" Qleverfile.in > $(QDIR)/Qleverfile

$(QDIR)/DUMP_DATE:
	mkdir -p $(QDIR) && (cp $(DUMPS)/DUMP_DATE $(QDIR)/DUMP_DATE 2>/dev/null || echo unbekannt > $(QDIR)/DUMP_DATE)

index: $(QDIR)/Qleverfile
	cd $(QDIR) && $(QLEVER) index --overwrite-existing

start: $(QDIR)/Qleverfile
	cd $(QDIR) && $(QLEVER) start

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
	cd mcp && uv run factgrid-mcp --http

chat:
	cd mcp && uv sync --extra chat >/dev/null && uv run --extra chat python ../chat/server.py

test:
	cd mcp && uv sync --extra dev >/dev/null
	cd mcp && uv run --extra dev python ../tests/test_wb2rdf.py
	cd mcp && uv run --extra dev python ../tests/test_mcp.py

smoke:
	$(PY) eval/run_eval.py --smoke

agent:
	cd mcp && uv run --with openai --with anthropic python ../agent/mini_agent.py

clean:
	rm -rf $(QDIR)/factgrid.ttl.gz.part
