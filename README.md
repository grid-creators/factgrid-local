# FactGrid lokal: Dump → QLever → MCP → LLM (OpenRouter/Anthropic)

Framework für einen vollständigen, lokalen Spiegel von [FactGrid](https://database.factgrid.de), der mit
[QLever](https://github.com/ad-freiburg/qlever) indexiert und über einen MCP-Server von einem Sprachmodell
abgefragt wird. Die Live-Instanz wird dabei nur noch für einen Dump-Download pro Aktualisierung berührt –
nicht mehr pro Frage.

Lokal ist der **Datenbestand**: Dump, Konvertierung, Index und MCP-Server laufen auf dem eigenen Rechner.
Das Modell läuft dagegen über eine Cloud-API – standardmäßig **OpenRouter** (`z-ai/glm-5.3-flash` über die
Responses-API), wahlweise Anthropic oder OpenAI; Fragen und Query-Ergebnisse verlassen also den Rechner.
Ein lokales Modell wird nicht mehr unterstützt.

Stand: 31. August 2026. Alle Zahlen zu FactGrid (Dump-Größe, Entitäten) sind an diesem Tag beobachtet;
Zahlen zu Indexgröße und Laufzeiten sind Schätzungen, die beim ersten Lauf zu messen und hier
einzutragen sind. Ergänzung vom 10. September 2026: Spiegel der MediaWiki-Datenbank für die
Bearbeitungsgeschichte (Abschnitt 3.7) mit gemessenen Zahlen.

## 1. Ziel und Randbedingungen

FactGrid stellt unter `https://database.factgrid.de/dumps/` täglich (≈ 22:00 UTC) einen gzip-komprimierten
**JSON-Dump** bereit (`YYYY-MM-DD.json.gz`, aktuell ≈ 1,0 GB, 90 Tage Vorhaltung). Einen RDF-Dump gibt es
nicht; der öffentliche SPARQL-Endpunkt ist ein Blazegraph mit 60 s Zeitlimit. Die Wiki-Statistik weist rund
2,0 Mio. Seiten aus, darunter geschätzt 1,3–1,5 Mio. Items – daraus entstehen im Wikibase-RDF-Modell
grob 150–300 Mio. Tripel. Das ist für QLever eine kleine Datenmenge (Wikidata: ~20 Mrd. Tripel).

Der Zielrechner (`tdsminisforum`, Linux x64) hat höchstens 32 GB RAM und keine große GPU. Da das Modell
über die API läuft, betrifft das nur noch QLever: Indexbau und Serverbetrieb müssen in dieses Budget passen
(Abschnitt 5). Was auf der Client-Seite zählt, ist stattdessen der Token-Verbrauch pro Frage (Abschnitt 6).

Gewählte Optionen (Rückfragen vom 31.08.2026): eigener JSON→RDF-Konverter statt eines serverseitigen
RDF-Dumps; Modelle über die API statt lokal (OpenRouter als Default, Anthropic/Claude Code daneben);
Lieferung als Architekturdoku plus lauffähiges Gerüst.

## 2. Architektur auf einen Blick

```
   database.factgrid.de/dumps/YYYY-MM-DD.json.gz        (1 Download pro Aktualisierung, HEAD-Prüfung auf Vollständigkeit)
                    │
                    ▼  scripts/fetch_dump.py
          dumps/latest.json.gz  ──►  scripts/wb2rdf.py  (Streaming, Multiprocessing, Wikibase-RDF-Modell 1:1)
                                              │
                                              ▼
                                   qlever/factgrid.ttl.gz  ──►  qlever index  ──►  qlever start
                                                                                        │  http://localhost:7003
                                                                                        ▼
                                                            mcp/factgrid_mcp  (FastMCP, 10 Tools, stdio oder HTTP)
                                                            • sparql          (Prefix-Injektion, Label-Service-Umschreibung, LIMIT-Deckel)
                                                            • search_entities (Text → Q-/P-ID)
                                                            • get_entity      (Item/Property kompakt, Labels aufgelöst)
                                                            • schema_overview (Klassen/Properties, gecacht)
                                                            • edit_history / mw_sql / mw_schema  (Bearbeitungsgeschichte, s. u.)
                                                                                        ▲
   /srv/data/factgrid/mediawiki/monthly_mediawiki_<datum>.sql.gz   (MediaWiki-SQL-Dump, monatlich)      │
                    │                                                                                   │
                    ▼  scripts/mw_load.py  (nur öffentliche Tabellen, Aria, Bereinigung, atomarer Tausch)│
          MariaDB factgrid_mw  (revision, page, actor, comment, logging, user, wbt_* …)  ◄── mwdb.py ───┘
                                                                                         (Lesebenutzer, SELECT-only)
                                                                                        │
                                              ┌─────────────────────────────────────────┴────────────────────────┐
                                              ▼                                                                  ▼
                    chat/server.py ⇄ OpenRouter · Anthropic · OpenAI          agent/mini_agent.py ⇄ OpenRouter · Anthropic
                    (make chat, Port 8177)                                    (Plan B: 1 k Token System-Prompt)
                                          Claude Code ⇄ Anthropic-API  (.mcp.json, CLAUDE.md)
```

Alle Komponenten sind austauschbar: der Konverter kann durch einen offiziellen `dumpRdf.php`-Dump ersetzt
werden, QLever durch jede SPARQL-1.1-Engine mit demselben Graphen, Claude Code durch den Web-Chat
(`make chat`), Open WebUI (MCP über Streamable HTTP, `factgrid-mcp --http`) oder den Mini-Agenten.

## 3. Komponenten

### 3.1 Dump-Beschaffung – `scripts/fetch_dump.py`

Liest das Verzeichnis-Listing, prüft die jüngsten sechs Dumps per `HEAD` auf ihre Größe und überspringt
Dateien, die kleiner als 80 % des größten dieser Dumps sind. Das ist nötig, weil im Listing vereinzelt
abgebrochene Dumps liegen (z. B. `2026-06-19` mit 139 MB und `2026-08-14` mit 217 MB statt ≈ 1 GB).
Der Download ist fortsetzbar (`Range`), wird per vollständiger gzip-Dekompression verifiziert und als
`dumps/<datum>.json.gz` plus Symlink `latest.json.gz` und Datei `DUMP_DATE` abgelegt. Ältere Dumps werden
auf zwei Stück reduziert. Der User-Agent nennt den Zweck des Spiegels.

### 3.2 Konverter – `scripts/wb2rdf.py`

Ein reiner Python-Streaming-Konverter ohne Abhängigkeiten. Wikibase-JSON-Dumps enthalten pro Zeile eine
Entität; jede Zeile wird in einem Worker-Prozess in Turtle-Zeilen übersetzt (eine Aussage pro Zeile, alle
Präfixe im Kopf – so kann QLever parallel parsen). Das Mapping folgt dem Wikibase-RDF-Dump-Format mit den
FactGrid-Namensräumen, wie sie `Special:EntityData/Q7.ttl` liefert (`wd: <https://database.factgrid.de/entity/>`,
`wdt: …/prop/direct/`, `p:`, `ps:`, `psv:`, `pq:`, `pqv:`, `pr:`, `prv:`, `wdno:`, `s: …/entity/statement/`,
`ref: …/reference/`, `v: …/value/`). Dadurch laufen bestehende FactGrid-Abfragen unverändert.

Abgebildet werden Labels/Beschreibungen/Aliase (`rdfs:label`, `schema:description`, `skos:altLabel`),
Truthy-Aussagen (`wdt:`, nur Best-Rank, nie „deprecated“), volle Aussagen mit Rang und `wikibase:BestRank`,
Qualifikatoren, Referenzen (Hash aus dem Dump), Wertknoten für Zeit, Menge und Koordinaten
(`wikibase:TimeValue` mit Präzision und Kalendermodell usw.), `somevalue` als Blank Node, `novalue` als
`rdf:type wdno:P…`, Property-Definitionen (`wikibase:propertyType`, `directClaim`, `claim`, …), Sitelinks und
die Zähler `wikibase:statements/sitelinks/identifiers` (als Relevanzmaß für die Suche).

Zeitwerte werden wie in Wikibase (`DateTimeValueCleaner`) bereinigt: Jahrespräzision → `-01-01`,
Monatspräzision → Tag `01`, ungültige Tage werden auf die Monatslänge geklemmt, Jahre v. Chr. erhalten die
XSD-1.1-Verschiebung, julianische Tagesdaten werden in das proleptisch-gregorianische `xsd:dateTime`
umgerechnet (das Kalendermodell bleibt am Wertknoten sichtbar).
Koordinaten werden als `geo:wktLiteral` (`Point(lon lat)`) geschrieben – QLever kann darauf GeoSPARQL
ausführen. Mengen werden als `xsd:decimal` geschrieben.

Bewusst weggelassen (selten abgefragt, spart ein Drittel der Tripel): normalisierte Werte (`wdtn:`, `psn:`,
`pqn:`, `prn:`), OWL-Definitionen der `wdno:`-Klassen und die Label-Dubletten `skos:prefLabel`/`schema:name`
(per `--full-labels` zuschaltbar). `--flavor truthy` erzeugt einen kleinen Index nur aus `wdt:` + Labels –
praktisch zum Ausprobieren.

Durchsatz (gemessen mit synthetischen Entitäten à 13 Aussagen, ≈ 120 Tripel/Entität): rund 5 000
Entitäten pro Sekunde und Kern. Ein FactGrid-Dump ist damit in wenigen Minuten konvertiert; der
Flaschenhals ist eher das gzip (deshalb `pigz`).

Tests: `tests/test_wb2rdf.py` baut eine Entität mit allen Datentypen, Rängen, Qualifikator, Referenz,
`somevalue`/`novalue` und prüft den Graphen mit rdflib.

### 3.3 QLever – `Qleverfile.in`

Die Konfiguration ist vom offiziellen Wikidata-Qleverfile abgeleitet und auf 32 GB RAM skaliert:
`STXXL_MEMORY = 4G` für die Indexierung, `MEMORY_FOR_QUERIES = 8G`, `CACHE_MAX_SIZE = 4G`, `TIMEOUT = 120s`,
Port 7003. `languages-internal: ["de","en"]` hält die Label-Literale dieser Sprachen im schnellen internen
Vokabular, `prefixes-external: [""]` lagert IRIs aus (spart RAM), `ascii-prefixes-only: true` ist erlaubt,
weil der Konverter nur ASCII-Präfixnamen erzeugt.

`SYSTEM = docker` ist die portable Voreinstellung; mit dem nativen Debian/Ubuntu-Paket (`SYSTEM = native`)
entfällt der Container-Overhead. `CAT_INPUT_FILES = zcat …` (mit pigz: `pigz -dc …`) – qlever hängt daran
`| IndexBuilderMain` an, deshalb dort keine Shell-Konstrukte mit `||`. Die Datei ist mit qlever-control 0.6.0
geprüft (`qlever index --show`, `qlever start --show` erzeugen die erwarteten Kommandos). Ein Volltextindex über alle Literale (`TEXT_INDEX = from_literals`) ist
optional; der MCP-Server nutzt ihn nur mit `FACTGRID_TEXT_INDEX=1` (Wort-/Präfixsuche mit
`ql:contains-word`). Ohne Textindex verwendet die Suche eine Präfix-`REGEX` (die QLever auf dem sortierten
Vokabular auswertet) und danach eine `CONTAINS`-Suche.

### 3.3a QLever-UI – `make ui`

`make ui` startet die QLever-UI auf Port 8176 (`UI_PORT`) gegen denselben Index. Zwei FactGrid-Anpassungen
schreibt `scripts/ui_prefixes.py` vorher in `qlever/Qleverfile-ui.yml` – Quelle ist derselbe
Namensraum-Katalog wie im MCP-Server (`mcp/factgrid_mcp/prefixes.py`), damit beide nicht auseinanderlaufen:

* **`suggestedPrefixes`**: alle 29 FactGrid-Prefixe (`wd`, `wdt`, `p`, `ps`, `psv`, `pq`, `pr`, …). Ohne
  sie scheitert in der UI jede Query mit „Prefix wd was not registered", und die Autovervollständigung
  kann IRIs nicht zu `wd:Q409` abkürzen.
* **`examples`**: sechs FactGrid-Abfragen (Startvorlage, Personen zählen, häufigste Klassen, alle Aussagen
  zu einem Item, Unterklassen, Statement mit Qualifikatoren), jede mit vollständigem PREFIX-Block.

Wichtig zu wissen: die UI ergänzt PREFIX-Zeilen **nur beim Übernehmen eines Autocomplete-Vorschlags**
(`fillPrefixes`), nicht beim Ausführen einer selbst getippten Query – deshalb die Beispiele als
Startpunkte. Wer lieber mit einer leeren, aber vorbereiteten Query anfängt, legt sich ein Lesezeichen auf
`http://<host>:8176/default?query=<urlencodierter PREFIX-Block>`. Der MCP-Server hat das Problem nicht: er
injiziert die benötigten Prefixe in jede Query (Abschnitt 3.4).

### 3.4 MCP-Server – `mcp/factgrid_mcp`

FastMCP-Server mit zehn Tools und einem Prompt (sieben für den QLever-Index, drei für die
MediaWiki-Datenbank, Abschnitt 3.7). Die Zahl bleibt bewusst klein, weil jedes Tool-Schema
bei jedem Modellaufruf im Kontext steht – der Funktionsumfang entspricht dem gehosteten
[wb-mcp.wmcloud.org/factgrid](https://wb-mcp.wmcloud.org/factgrid/docs) (Wikibase MCP auf Wikimedia Cloud),
nur gegen den lokalen Spiegel statt gegen die Live-Instanz. Dessen `search_items`/`search_properties` sind
hier ein Parameter von `search_entities` (`entity_type`), dessen `execute_sparql` ist `sparql` (Zeilenzahl
über `limit`), dessen `get_statements` ist `get_entity`.

`sparql` nimmt WDQS-Syntax entgegen und macht sie QLever-tauglich: fehlende `PREFIX`-Zeilen werden
ergänzt, `SERVICE wikibase:label { bd:serviceParam wikibase:language "…" }` wird in `OPTIONAL`/`FILTER(LANG)`/
`BIND(COALESCE(…))` umgeschrieben (implizite `?xLabel`-Konvention und explizite `?x rdfs:label ?name`-Form),
`[AUTO_LANGUAGE]` wird zur konfigurierten Sprache, und wie im WDQS fällt ein fehlendes Label auf die ID
zurück. Die erzeugten Muster landen am Ende der Gruppe, in der die Variable gebunden wird – innerhalb eines
`OPTIONAL`, in jedem Zweig eines `UNION`, hinter einer Subquery, die die Variable projiziert –, sonst
entstünde bei ungebundenen Variablen ein Kreuzprodukt mit allen Labels; Variablen, die nur in `MINUS` oder
`NOT EXISTS` vorkommen, erhalten kein Label-Muster. Kommentare werden entfernt, ein `LIMIT` wird auf
höchstens 200 Zeilen gedeckelt (auch vor einem abschließenden `VALUES`-Block), Update-Operationen werden
abgewiesen, und QLever-Fehlermeldungen gehen wörtlich an das Modell zurück, damit es die Query korrigieren
kann. Ergebnisse kommen als TSV mit gekürzten IRIs (`wd:Q7` statt der vollen Form). Der Client spricht das
QLever-Protokoll korrekt: alle Parameter (`query`, `timeout`, `access-token`) stehen im POST-Body; ohne
Token wird das Zeitlimit auf den Server-Default gekappt, weil QLever höhere Werte sonst mit 403 ablehnt.

`search_entities` löst Namen in IDs auf (Label und Alias, sprachgefiltert, nach `wikibase:statements`
sortiert, `entity_type=item|property|any`); `get_entity` zeigt ein Item mit allen Aussagen, Rängen und
Qualifikatoren in aufgelöster Form bzw. für eine Property Datentyp, Verwendungszahl und häufigste Werte –
Normdaten-IDs (external-id) bleiben weg, bis `include_external_ids=true` gesetzt wird.

`get_statement_values` ist die Detailsicht auf ein Paar Entität/Property: alle Statements mit Rang,
Qualifikatoren, Referenzen und dem Wertknoten – bei Zeitangaben also Präzision, Zeitzone und
Kalendermodell, bei Mengen die Einheit, bei Koordinaten Länge und Breite. `get_property_hierarchy` läuft
eine Property ebenenweise ab und gibt einen eingerückten Baum zurück (`direction="down"` folgt den
eingehenden Kanten, also z. B. allen Unterklassen einer Klasse, `"up"` der Kette nach oben); Zyklen werden
markiert, abgebrochen wird bei `max_depth` (Default 5) oder `max_nodes` (Default 200).

`schema_overview` berechnet einmalig die 60 häufigsten Klassen (Werte von `wdt:P2`) und die 150
meistgenutzten Properties; `get_wikibase_info` nennt Instanz, Endpunkt, Dump-Datum und die Größe des Index
(Tripel, Items, Properties) – und sagt dem Modell ausdrücklich, dass es einen Spiegel mit Datenstand
abfragt, nicht die Live-Instanz. Beide cachen unter `~/.cache/factgrid-mcp/` (`make refresh` leert das).

Konfiguration über Umgebungsvariablen (`.env.example`): `QLEVER_ENDPOINT`, `FACTGRID_LANG`,
`FACTGRID_INSTANCE_OF` (P2), `FACTGRID_MAX_ROWS`, `FACTGRID_TEXT_INDEX`. Start als stdio-Server (Claude Code)
oder mit `--http` (Open WebUI: Streamable HTTP unter `http://127.0.0.1:8765/mcp`).

Tests: `tests/test_chat_backends.py` prüft Modellliste, Schlüsselauflösung, Gesprächsablage, die permanenten
Gesprächslinks (Nur-Lesen ohne Anmeldung, Weiterführen als eigene Kopie), die Datei-Anhänge (Textgewinnung
aus TXT/CSV/PDF, Abweisen von Bild- und Office-Dateien, `/api/upload`, der Weg des Anhangs in die Frage
jedes Providers) und das DeepSeek-Backend
(Nachrichtenformat, Zusammensetzen gestreamter Tool-Aufrufe, Endpunkte `/api/models` und `/api/keys`) ohne
echte API-Aufrufe. `tests/test_mcp.py` startet einen rdflib-Mock-Endpunkt mit dem konvertierten Testgraphen und prüft
alle vier Tools sowie die Umschreibungen (implizit/explizit, UNION/OPTIONAL/MINUS/Subquery, Kommentare,
LIMIT, Update-Sperre) end-to-end – ohne laufenden QLever. Der Mock spricht dasselbe Protokoll, kennt aber
QLevers Parameterregeln nicht; der erste Lauf gegen den echten QLever ist deshalb Teil von `make smoke`.

### 3.5 LLM-Client

**`agent/mini_agent.py`** ist der schlanke Weg: ein Tool-Loop mit denselben MCP-Tools
(In-Process-Transport von fastmcp), System-Prompt ist nur `CLAUDE.md`. Zwei Anbieter, aus der Modell-ID
abgeleitet – IDs mit `/` gehen an **OpenRouter** (Responses-API `POST /api/v1/responses`, OpenAI-SDK mit
eigener `base_url`, Header `HTTP-Referer`/`X-Title` optional), IDs ohne `/` an **Anthropic**
(Messages-API, Streaming, System-Prompt mit Cache-Breakpoint). Default ist `OPENROUTER_MODEL`
(`z-ai/glm-5.3-flash`), Umschalten per `-m` (`-m anthropic/claude-opus-5` erzwingt Anthropic) oder
`AGENT_MODEL`. Jeder Lauf wird als JSONL nach `eval/runs/` protokolliert – Frage, Tool-Aufrufe, SPARQL,
Antwort, Laufzeit – und ist damit zugleich die Datensammlung für Regressionstests und spätere Auswertungen.

Daneben: **Claude Code** mit `.mcp.json` (startet `factgrid-mcp` per `uv`) und `CLAUDE.md`
(Arbeitsanweisung für das Modell: Reihenfolge der Tools, SPARQL-Konventionen); Zugang über
`ANTHROPIC_API_KEY` oder ein Profil aus `ant auth login`. Bequem, aber die teuerste Variante
(Abschnitt 6, „Kostenfalle").

### 3.6 Web-Chatbot – `chat/`

`make chat` startet einen kleinen FastAPI-Server (Port 8177, `FACTGRID_CHAT_PORT`) mit Browser-Chat
über demselben In-Process-MCP wie der Mini-Agent. Modellauswahl per Dropdown, auch mitten im Gespräch:
Der Verlauf liegt providerneutral auf dem Server und wird erst pro Anfrage in das Format des gewählten
Anbieters übersetzt. Drei Provider werden beim Start erkannt: **OpenRouter** (`OPENROUTER_API_KEY`;
Responses-API, Auswahl im Dropdown aus `OPENROUTER_MODELS`, Default `OPENROUTER_MODEL`), **Anthropic**
(sobald `ANTHROPIC_API_KEY` in `.env` steht oder ein `ant auth login`-Profil existiert; offizielles SDK,
Antwort-Streaming, System-Prompt mit Cache-Breakpoint) und **OpenAI** (`OPENAI_API_KEY`, ebenfalls Responses-API: Reasoning-Modelle wie `gpt-5.6-terra` lehnen
Function-Tools in `/v1/chat/completions` ab, sobald `reasoning_effort` nicht `none` ist, und nur die
Responses-API führt die reasoning-Items über Tool-Aufrufe hinweg mit – zustandslos mit
`store=false` plus `include: ["reasoning.encrypted_content"]`).
Vierter Anbieter ist **DeepSeek** (`DEEPSEEK_API_KEY`; OpenAI-kompatible Chat-Completions-API unter
`https://api.deepseek.com`, Modell `deepseek-flash` = DeepSeek V4.1 Flash mit 1 M Kontext). Der Denkmodus ist
dort standardmäßig an (`DEEPSEEK_THINKING=disabled` schaltet ihn ab, `DEEPSEEK_REASONING_EFFORT=low|high`
steuert ihn); die API verlangt, dass bei Requests mit Tools das `reasoning_content` in allen Folge-Requests
zurückkommt – es wandert deshalb als Feld `reasoning` in den neutralen Verlauf und wird beim nächsten
DeepSeek-Request wieder eingesetzt (andere Anbieter ignorieren das Feld).

**Freigeschaltet** sind nur die Modelle aus `FACTGRID_CHAT_MODELS` (Komma-Liste `anbieter/modell`, etwa
`openai/gpt-5.6-luna,deepseek/deepseek-flash`), Vorauswahl ist `FACTGRID_CHAT_MODEL`; eine andere Modell-ID aus
dem Browser wird serverseitig durch die Vorauswahl ersetzt. Den Schlüssel eines Anbieters nimmt der Server
zuerst aus dem Benutzerprofil (Dialog „Schlüssel“, `.chat-profiles.json`) und sonst aus `.env`
(`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, …) – so kann ein Anbieter für alle Angemeldeten über einen zentralen
Schlüssel laufen, während ein anderer pro Person abgerechnet wird. Die Oberfläche zeigt je Modell, ob ein
Schlüssel da ist, und der Schlüssel-Dialog gilt immer für den Anbieter des gewählten Modells.
Anthropic und OpenAI melden ihre Modelle live, bei OpenRouter wäre die Liste mit einigen hundert Einträgen
unbrauchbar – deshalb die Auswahl aus `.env`.
Tool-Aufrufe erscheinen aufklappbar mit dem erzeugten SPARQL (Kopierknopf) und dem Ergebnis: TSV wird als
Tabelle gerendert, Q-/P-IDs werden auf die Live-Instanz verlinkt, damit sich jede Antwort in einem Klick
gegenprüfen lässt. Die Kopfzeile zeigt den Datenstand des Spiegels (`/api/info` → `get_wikibase_info`),
eine laufende Antwort lässt sich abbrechen, und wenn kein Anbieter konfiguriert ist, sagt ein
Banner welcher Schlüssel fehlt statt eines leeren Dropdowns.
Die **Seitenleiste** listet alle bisherigen Gespräche des angemeldeten Benutzers (Titel = erste Frage,
Antworten werden als Markdown-Teilmenge gerendert (Überschriften, Listen, Zitate, Trennlinien, Codeblöcke,
Inline-Code, Fett/Kursiv, Links, GFM-Tabellen);
zuletzt benutzte zuerst); ein Klick öffnet eines, „+ Neues Gespräch“ beginnt ein leeres, das × am Eintrag
löscht es nach einer Rückfrage. Die Gespräche liegen auf dem Server unter `.chat-history/<benutzer>/<id>.json`
(`FACTGRID_CHAT_HISTORY`) und überstehen damit Neuladen wie Neustart; die Datei entsteht mit der ersten
Frage und wird nach jeder Antwort geschrieben (`/api/chats`, `/api/history`, `DELETE /api/chats/{id}`).
Jeder sieht nur die eigenen Gespräche; ohne Anmeldung teilen sich alle ein gemeinsames Konto. Jede Antwort wird wie beim Mini-Agenten
als JSONL nach `eval/runs/` protokolliert (Feld `"ui": "chat"`).

### 3.7 MediaWiki-Datenbank – Bearbeitungsgeschichte (`scripts/mw_load.py`, `mcp/factgrid_mcp/mwdb.py`)

**Dateien anhängen.** An eine Frage lassen sich Dateien hängen (Büroklammer, Ziehen ins Fenster oder
Einfügen aus der Zwischenablage). Der Server macht daraus beim Hochladen **Text** (`POST /api/upload`, der
Datei-Inhalt ist der Request-Body – kein multipart, das spart eine Abhängigkeit): TXT, CSV/TSV, JSON,
XML/TTL, Markdown, SPARQL direkt (UTF-8, BOM und die Windows-Kodierung, in der Excel CSV schreibt), PDF
über `pypdf`. Bilder und Office-Dateien nimmt der Chat **nicht** – dafür hätte jeder der vier Anbieter ein
eigenes Format; statt Kauderwelsch anzuhängen, sagt die Fehlermeldung, was da hochgeladen wurde („Tabellen
aus Excel bitte als CSV oder TSV speichern“).

**Die Datei bleibt auf dem Server, nicht im Kontext.** Mit dem Absenden zieht die Textdatei neben die
Gesprächsdatei (`.chat-history/<benutzer>/<gespräch>.files/<id>.txt`) und wird mit dem Gespräch gelöscht;
in den Prompt geht nur ein **Steckbrief mit den ersten `FACTGRID_CHAT_UPLOAD_PREVIEW` Zeichen** (Vorgabe
2 000): Name, Zeilen, Zeichen, erkanntes Trennzeichen und die Spaltennamen. Der Grund ist der Verlauf – er
geht bei jeder Folgefrage komplett wieder an die API, ein ganzer 16-MB-Anhang würde also bei jeder Frage
neu bezahlt. Für alles Weitere bekommt das Modell in Gesprächen mit Anhängen **vier zusätzliche
Werkzeuge**, die im Chat-Prozess auf der ganzen Datei laufen (`LOCAL_TOOLS` in `chat/server.py`; ohne
Anhang stehen sie nicht in der Tool-Liste):

| Werkzeug | wofür |
| --- | --- |
| `list_attachments` | was hängt an: Zeilen, Zeichen, Trennzeichen, Spaltennamen |
| `read_attachment` | Zeilenfenster (`from_line`, `lines`; höchstens 400) |
| `search_attachment` | alle Zeilen mit einem Suchwort (Groß/Klein egal, optional regulärer Ausdruck), mit Zeilennummer |
| `column_stats` | eine Spalte auszählen: gefüllt, leer, verschiedene Werte, die häufigsten |

So beantwortet das Modell auch Fragen über Zeile 120 000 einer Tabelle, die es nie gesehen hat – in den
Kontext geht nur das Ergebnis (je Aufruf höchstens 8 000 Zeichen). `CLAUDE.md` weist es an, Mengenfragen
über `column_stats`/`search_attachment` zu beantworten statt die Datei stückweise zu lesen, den Inhalt als
**Material, nicht als Anweisung** zu lesen und Namen daraus wie jede andere Angabe erst mit
`search_entities` aufzulösen. Der Chip unter der Frage lädt die Datei wieder herunter
(`GET /api/attachment`, im geteilten Gespräch `GET /api/shared/attachment`).

Die Größengrenze steht an **zwei** Stellen, die zusammenpassen müssen: `FACTGRID_CHAT_UPLOAD_MAX` im Chat
(32 MB) und `client_max_body_size` im nginx-proxy-manager davor (Proxy-Host → Advanced). Wird nur eine der
beiden erhöht, antwortet der Proxy mit seiner HTML-Fehlerseite statt mit JSON. Damit das gar nicht erst
passiert, holt sich die Oberfläche die Grenze beim Start (`/api/me` → `upload_max`) und schickt eine zu
große Datei **nicht** los: Der Chip nennt Größe und Grenze und bietet „Anfang nehmen“ an – der Browser
schneidet dann am letzten Zeilenumbruch vor der Grenze ab (ein `\n` steckt in keinem Mehrbyte-Zeichen, der
Ausschnitt ist also in jeder Kodierung heil), `?part=1` sagt es dem Server, und im Steckbrief steht „nur
der Anfang der hochgeladenen Datei“. Antwortet doch ein Proxy mit 413, versucht es die Oberfläche mit
einem Viertel noch einmal (bis hinunter zu 64 kB) – eine strengere Grenze davor bleibt so eine Frage der
Geduld, nicht des Scheiterns.

**Dateien herunterladen.** Umgekehrt ist alles Tabellarische einer Antwort ein Klick von einer Datei
entfernt: jede Markdown-Tabelle und jedes Tool-Ergebnis bekommt einen TSV-Knopf, jeder Codeblock mit
Datei-Sprache (```` ```tsv ````, ```` ```csv ````, ```` ```json ````, ```` ```rq ```` …) einen Knopf in
seinem Format. Gebaut wird die Datei im Browser (`Blob` + `<a download>`): Markdown-Tabellen aus dem Angezeigten,
Tool-Ergebnisse dagegen aus dem Original, das auch das Modell bekommen hat – Kopfzeile und Datenzeilen,
ohne die Fußzeile wie „(200 Zeilen, gekürzt)“. TSV und CSV bekommen ein BOM, sonst zeigt Excel beim Doppelklick Kauderwelsch
statt Umlauten. Weil das Modell laut `CLAUDE.md` weiß, dass ein ```` ```tsv ````-Block ein Download-Knopf
wird, reicht als Frage „… als Tabelle zum Herunterladen“.

**Permanenter Link (geteilte Gespräche).** Jedes gespeicherte Gespräch hat einen Link `/s/<token>`; der
Knopf „Teilen“ zeigt ihn und kopiert ihn in die Ablage (`GET /api/share`). Unter diesem Link ist das
Gespräch **ohne Anmeldung lesbar – und nur lesbar**: dieselbe Oberfläche ohne Eingabefeld, Seitenleiste und
Modellwahl, dafür mit dem vollen Verlauf samt aufklappbaren Tool-Aufrufen und SPARQL
(`GET /api/shared?token=…` liefert den Verlauf ohne den Namen des Besitzers und ohne das Feld `reasoning`).
In der Kopfzeile steht das Datum des Gesprächs, nicht der heutige Datenstand – die Antworten stammen aus
dem Spiegel von damals. Angehängte Dateien gehören zum Gespräch und sind damit auch über den Link lesbar
(und herunterladbar) – wer etwas anhängt, was nicht weitergegeben werden soll, teilt dieses Gespräch besser
nicht. Das Token (`secrets.token_urlsafe(16)`, in der Gesprächsdatei unter `share`) ändert
sich nie und ist der ganze Zugangsschutz: erraten oder aufzählen lässt sich der Link nicht, öffentlich ist
er so weit, wie man ihn selbst weitergibt; `X-Robots-Tag: noindex` hält Suchmaschinen draußen, und Löschen
des Gesprächs macht den Link tot. Geschrieben wird nie im Original: „Weiterführen“
(`POST /api/shared/continue`, nur angemeldet) legt eine Kopie mit neuer ID und eigenem Link an, die der
Angemeldete als eigenes Gespräch fortsetzt – zwei Leute können denselben Link unabhängig voneinander
weiterspinnen, das Original bleibt, wie es war.

Der JSON-Dump enthält nur den aktuellen Zustand der Entitäten; **wer wann was bearbeitet hat**, steht
allein in der MediaWiki-Datenbank. FactGrid liefert davon monatlich einen vollständigen MariaDB-Dump
(`monthly_mediawiki_<datum>_….sql.gz`, aktuell 19,5 GB gzip = 171 GB SQL, 84 Tabellen; geladen
werden daraus 21,1 Mio. Versionen, 2,0 Mio. Seiten, 8,4 Mio. Kommentare, 1 000 Benutzerkonten – 11 GB
in MariaDB, 23 Minuten), der nach
`/srv/data/factgrid/mediawiki/` (`MW_DUMP_DIR`) kopiert wird. `make mwdb` (`scripts/mw_load.py`) lädt
daraus einen **Spiegel in die lokale MariaDB** (Datenbank `factgrid_mw`), gegen den der MCP-Server mit
einem eigenen Lesebenutzer arbeitet.

**Was geladen wird – und was bewusst nicht.** Der Dump ist die komplette Produktionsdatenbank, also auch
Passwort-Hashes, E-Mail-Adressen, IP-Adressen, Beobachtungslisten und OAuth-Geheimnisse. Der Loader
arbeitet deshalb mit einer **Allowlist** von 32 öffentlichen Tabellen (alles, was in der Wiki-Oberfläche
ohnehin jeder sieht): `page`, `revision`, `actor`, `comment`, `logging`, `change_tag(_def)`, `redirect`,
`page_props`, `page_restrictions`, `protected_titles`, `category(links)`, `image`, `user` (nur
`user_id`, `user_name`, `user_registration`, `user_editcount`), `user_groups`, `user_former_groups`,
die Wikibase-Termtabellen `wbt_*` (Labels, Beschreibungen, Aliase), `wb_items_per_site`,
`wb_property_info`, `wb_id_counters`, `wbqc_constraints`, `site_stats`, `sites`, `site_identifiers`,
`interwiki`. Unbekannte oder neue Tabellen bleiben automatisch draußen. Nicht geladen werden:

* **privat:** die übrigen `user`-Spalten, `account_credentials`/`account_requests` (ConfirmAccount:
  Klarnamen, E-Mails, IPs, Bewerbungstexte), `oauth_*`, `oauth2_access_tokens`, `bot_passwords`,
  `watchlist(_expiry)`, `user_properties`, `user_newtalk`, `recentchanges` (enthält `rc_ip`),
  `ip_changes`, `block`/`block_target`/`ipblocks_restrictions`, `echo_*` (Benachrichtigungen),
  `archive` (gelöschte Versionen – nur vorübergehend geladen, s. u.), `log_search`;
* **Ballast:** `text`, `content`, `slots` (alle Versionen als JSON, 160 GB der 171 GB – optional
  mit `MW_TEXT=1`), `objectcache`, `l10n_cache`, `searchindex`, `querycache*`, `job`, `module_deps`,
  `uploadstash`, `updatelog`, `pagelinks`/`linktarget`/`templatelinks`/`imagelinks`/`externallinks`/
  `iwlinks`/`langlinks` (der Graph im QLever-Index deckt das ab), `wb_changes*`, `wbc_entity_usage`.

Nach dem Laden **bereinigt** der Loader in der Staging-Datenbank: die `user`-Tabelle wird auf die vier
öffentlichen Spalten reduziert; Logbucheinträge mit `log_deleted` oder privaten Typen (`suppress`,
`oath`, …) werden gelöscht; Versionen mit verstecktem Benutzer oder Kommentar (`rev_deleted`) werden
anonymisiert; und aus `comment` fliegen alle Kommentare, die nur noch an gelöschten oder versteckten
Versionen hängen – Wikibase-Autokommentare enthalten Labels und Beschreibungen, bei Löschungen aus
Datenschutzgründen also genau das Problem. `mw_meta` hält fest, welcher Dump wann geladen wurde und
welche Tabellen fehlen; `get_wikibase_info` und `mw_schema` zeigen das dem Modell.

**Technik.** `pigz -dc dump | Filter | mariadb factgrid_mw_new`: ein Zustandsautomat über den
Tabellenabschnitten des mysqldump-Formats reicht nur erlaubte Abschnitte durch, setzt `ENGINE=Aria
TRANSACTIONAL=0` (der Spiegel ist read-only und wird monatlich neu gebaut; Aria lädt mit
`DISABLE/ENABLE KEYS` in einem Rutsch und kommt mit dem 128-MB-Pagecache aus) und macht aus
`UNIQUE KEY` normale `KEY`s – `DISABLE KEYS` schaltet bei Aria/MyISAM nur nicht-eindeutige Indizes ab,
UNIQUE-Indizes würden zeilenweise gepflegt (gemessen: zehnmal langsamer). Der Dump ist ohnehin konsistent.
Geladen wird in `factgrid_mw_new`; erst nach der Bereinigung werden die Tabellen mit einem einzigen
`RENAME TABLE` (atomar) nach `factgrid_mw` verschoben, dann entstehen die Views `v_revision`
(revision ⋈ page ⋈ actor ⋈ comment), `v_log`, `v_item_terms` und `v_property_terms`, und der
Lesebenutzer `factgrid_ro` bekommt `SELECT` auf `factgrid_mw.*` – sein Passwort erzeugt der Loader beim
ersten Lauf und trägt es als `MW_DB_PASSWORD` in `.env` ein (Modus 600). Der laufende Chat-Dienst merkt
vom Tausch nichts, weil jede Abfrage eine eigene Verbindung öffnet; nur der Schema-Cache wird geleert.
Ein bereits geladener Dump wird übersprungen (`MW_FORCE=1` erzwingt), `make mwdb-list` zeigt die
Tabellen mit Größen, `ops/factgrid-mwdb.timer` ist eine tägliche Prüfung auf neue Dumps (06:00).
Nach erfolgreichem Laden räumt der Loader das Dump-Verzeichnis auf: ältere `*.sql.gz` werden
gelöscht, nur der neueste bleibt (`MW_KEEP=n` behält n, `MW_KEEP=0` alle; bei `--no-swap` oder
einem bereits geladenen Dump wird nichts gelöscht).

**Tools.** `edit_history(page=…, user=…, since=…, until=…, namespace=…)` beantwortet die
Kernfrage direkt: für eine Seite (Q-ID, P-ID oder Titel wie `FactGrid:Directory of Properties`) alle
Versionen mit Zeitpunkt (UTC), Benutzer, lesbar aufbereitetem Wikibase-Autokommentar („Aussage
angelegt: P2: Q7“, „Label gesetzt [de]: …“), Größe und Differenz – dazu Anlage, Zahl der Bearbeitungen
und Bearbeiter, Weiterleitungsziel bei Zusammenführungen und die Logbucheinträge der Seite; für einen
Benutzer dessen Bearbeitungen mit Kontodaten und Gruppen; beides kombinierbar und zeitlich eingrenzbar.
`mw_sql` führt beliebige `SELECT`s aus (Statistiken: Bearbeitungen pro Monat, aktivste Benutzer,
meistbearbeitete Seiten, Logbuch), `mw_schema` liefert dem Modell vorher Konventionen (Zeitstempel
`YYYYMMDDHHMMSS`, Namensräume, Titel), Views, Tabellen mit Spalten und Zeilenzahlen, Logbuchtypen,
Markierungen und Beispielabfragen (gecacht je Dump). Lesend in vier Schichten: DB-Benutzer nur mit
`SELECT`, Verbindung mit `TRANSACTION READ ONLY`, `max_statement_time` und `sql_select_limit`,
Schlüsselwort-Sperre vor dem Verbindungsaufbau (kein `INSERT`/`UPDATE`/`INTO OUTFILE`/`SLEEP` …, nur
ein Statement) und ein `LIMIT`-Deckel von 200 Zeilen. Die Item-/Property-Namensräume (120/122) werden
aus der `page`-Tabelle bestimmt, nicht geraten. Konfiguration: `MW_DB_HOST/PORT/NAME/USER/PASSWORD`,
`MW_DB_TIMEOUT`, `MW_DUMP_DIR`, `MW_DB_ADMIN` (`.env.example`); der MCP-Server liest `.env` jetzt
selbst, bereits gesetzte Variablen (etwa aus `.mcp.json`) gewinnen.

Tests: `tests/test_mwdb.py` prüft Filter, Bereinigung, Tausch, Views und Rechte an einem synthetischen
Mini-Dump in `factgrid_mw_test` (wird wieder gelöscht) und lässt die drei Tools darauf laufen.

### 3.8 Wochenbriefing an die Liste – `scripts/weekly_briefing.py`

Freitags 08:00 Ortszeit (`Europe/Berlin`) schickt `ops/factgrid-briefing.timer` ein Briefing über
die vergangene Woche (Freitag bis Donnerstag, UTC) an `BRIEFING_TO` – eingerichtet ist die
Community-Liste `factgrid-community@listserv.dfn.de`. Die Woche endet am Donnerstag und damit
genau dort, wo der MediaWiki-Spiegel vom selben Morgen (06:00) aufhört. Der Ablauf in einem
Satz: **die Zahlen kommen aus SQL, die Sprache aus dem Modell.**

`collect()` holt aus dem MediaWiki-Spiegel (Abschnitt 3.7) alles, was im Briefing vorkommen darf:
Bearbeitungen, berührte und neu angelegte Seiten, aktive Konten, der Vergleich zur Vorwoche, die
Tageswerte, Namensräume, die aktivsten Konten, die Art der Änderungen aus den Wikibase-
Autokommentaren, die dort genannten Properties (mit Label), die meistbearbeiteten Items (mit
Label), je ein Beispielkommentar der größten Konten, das Logbuch und die Markierungen
(Rücksetzungen, Tool-Bearbeitungen). Daraus entsteht ein Faktenblock in englischer Sprache, und
**nur** dieser Block geht an das Modell (`BRIEFING_MODEL`, Vorgabe `deepseek-flash`): es hat
keinen Datenbankzugriff und kann nichts nachschlagen, es formuliert. Der Prompt verlangt Fließtext
mit zwei Konventionen – `## ` für Zwischenüberschriften und `**…**` um jeden Kontennamen und jede
Zahl, die zählt –, verbietet Deutungen und Lob und schreibt vor, Items und Properties mit Label
**und** Kennung zu nennen (`Sophie Schwarz (née Becker) (Q1196919)`), weil eine nackte Q-ID
niemandem etwas sagt.

Verschickt wird als `multipart/alternative`: reiner Text (die `**` fallen weg) und HTML, in dem
die Hervorhebungen fett stehen und Q-/P-IDs auf die Live-Instanz verlinkt sind; alles andere ist
escapet, ein spitzer Winkel in einem Item-Label kann das Layout also nicht aufbrechen. Über dem
Text steht in jeder Mail ein **fest im Skript verdrahteter Hinweis**, dass sie automatisch
entsteht, woher die Zahlen kommen und dass niemand gegenliest – der darf nicht aus dem Modell
kommen, sonst könnte ausgerechnet diese Angabe halluziniert sein. Der Header `Auto-Submitted:
auto-generated` hält Abwesenheitsantworten von der Liste fern.

Nichts wird verschickt, wenn etwas nicht stimmt: keine Bearbeitungen im Zeitraum, leere oder zu
kurze Antwort des Modells, fehlendes Format (`SUBJECT:` / `---`), fehlende Hervorhebungen oder
kein Postfach in `.env` – jedes Mal Abbruch mit Meldung in `journalctl -u factgrid-briefing`
statt einer halben Mail an ein paar hundert Leute. Endet der Spiegel vor dem Ende der
Berichtswoche, bricht das Skript nicht ab, schreibt dem Modell aber ein `CAVEAT` in die Fakten,
das im Briefing landet.

```
make briefing                          # Probelauf: Briefing auf die Konsole, nichts geht raus
make briefing BRIEFING_ARGS=--facts    # nur die Zahlen, ohne Modell
make briefing-send                     # wirklich verschicken (sonst macht das der Timer)
python3 scripts/weekly_briefing.py --send --to ich@example.org   # Testempfänger
python3 scripts/weekly_briefing.py --weeks-back 2                # die vorletzte Woche
```

Tests: `tests/test_briefing.py` prüft die Berechnung der Berichtswoche (auch über Zeitzonen), den
Faktenblock, die Abbruchregeln, das Rendern (fett, verlinkte IDs, Escaping) und die fertige
zweiteilige Nachricht – ohne Datenbank, ohne Modell, ohne SMTP.

```bash
pipx install qlever                       # QLever-CLI
cd factgrid-local
cp .env.example .env                      # OPENROUTER_API_KEY eintragen (oder ANTHROPIC_API_KEY)
make fetch                                # Dump holen (≈ 1 GB)
make convert                              # → qlever/factgrid.ttl.gz  (FLAVOR=truthy für einen kleinen Testindex)
make index                                # QLever-Index bauen (Zeit/Platz beim ersten Lauf messen!)
make start                                # Endpunkt http://localhost:7003
make smoke                                # Referenzabfragen aus eval/questions.jsonl
make mwdb                                 # optional: MediaWiki-SQL-Dump → MariaDB (Bearbeitungsgeschichte, Abschnitt 3.7)
cd mcp && uv sync && cd ..                # MCP-Server installieren
make agent                                # Mini-Agent auf der Kommandozeile (Default: OpenRouter)
make chat                                 # oder Web-Chat mit Modellauswahl auf http://127.0.0.1:8177
claude                                    # oder Claude Code im Projekt; .mcp.json wird erkannt
```

Innerhalb von Claude Code: `/mcp` zeigt `factgrid-local`; eine erste Frage etwa „Wie viele Menschen sind in
FactGrid erfasst?“ sollte über `schema_overview` → `sparql` in einem Zug beantwortet werden.

## 5. Ressourcenbudget (32 GB RAM)

| Schritt | RAM | Platte | Dauer (Schätzung) |
|---|---|---|---|
| Dump-Download | – | 1 GB | Minuten (Bandbreite) |
| Konvertierung (`wb2rdf.py`, alle Kerne) | < 2 GB | ≈ 1,5–2,5 GB `.ttl.gz` | 3–10 min |
| `qlever index` | 6–12 GB | 30–60 GB Index | 20–60 min |
| `qlever start` | 8–12 GB (Cache + Abfragen) | – | Sekunden |
| `make mwdb` (MediaWiki-DB, ohne `text`) | < 1 GB (Aria-Pagecache 128 MB + Sortierpuffer) | ≈ 11 GB (32 Tabellen, Aria) in `/var/lib/mysql` | 23 min gemessen (7 min davon Dekompression der 171 GB) |

Das Modell braucht auf diesem Rechner keinen Speicher mehr – es läuft beim Anbieter; der Client
(Claude Code, `make chat`, `make agent`) ist vernachlässigbar. Das gesamte Budget steht damit QLever zur
Verfügung. Während `qlever index` läuft, sollte der Server gestoppt sein (`make refresh` tut das).

## 6. Text-zu-SPARQL: Arbeitsablauf, Kosten, Modellwahl

Modelle scheitern an Wikibase-SPARQL selten an der Syntax, sondern an geratenen IDs und am
Datenmodell (welche Property heißt hier „Geburtsort“?). Das Framework begegnet dem mit einem festen
Arbeitsablauf, der in `CLAUDE.md` und in der MCP-`instructions` steht: erst `schema_overview`, dann Namen
per `search_entities` auflösen, bei Unsicherheit ein Beispiel-Item mit `get_entity` anschauen, erst dann
`sparql` – und bei Fehlern die zurückgegebene QLever-Meldung zur Korrektur nutzen. Ergebnisse über 200 Zeilen
werden abgeschnitten, was das Modell zu Aggregationen zwingt statt Rohdaten in den Kontext zu ziehen.

## 4. Schnellstart

Voraussetzungen: Python ≥ 3.10, `uv` (oder `pipx`), `pigz` (optional), Docker oder das native QLever-Paket,
ein OpenRouter-Schlüssel (oder ein Anthropic-Schlüssel bzw. `ant auth login`).

**Kostenfalle.** Was früher Rechenzeit war, ist jetzt Token-Verbrauch. Claude Code bringt einen
System-Prompt von grob 15–20 k Token mit, die pro Frage neu im Kontext stehen; jeder Tool-Aufruf hängt
ein Ergebnis daran. Zwei Hebel dagegen sind eingebaut: der Deckel von 200 Zeilen pro Ergebnis
(`FACTGRID_MAX_ROWS`, dazu 12 k Zeichen pro Tool-Ergebnis im Agenten und im Web-Chat) und der
Cache-Breakpoint auf dem System-Prompt – er ist über alle Schritte und alle Fragen identisch und wird
so nur einmal voll bezahlt (Anthropic; bei OpenRouter hängt das Caching am jeweiligen Anbieter).
Wer viele Fragen hintereinander stellt, fährt mit `make agent` oder `make chat` deutlich günstiger als
mit Claude Code: deren System-Prompt ist nur `CLAUDE.md` (≈ 1 k Token).

**Modellwahl.** Voraussetzung ist zuverlässiges Tool-Calling: korrektes JSON mit allen Argumenten, und
zwar über mehrere Runden hinweg (`search_entities` → `get_entity` → `sparql` → Korrektur nach einer
QLever-Fehlermeldung). Default ist `z-ai/glm-5.3-flash` über OpenRouter – schnell und billig; wenn
Fragen an geratenen IDs oder an kaputtem SPARQL scheitern, ist ein größeres Modell (`-m
anthropic/claude-opus-5`, oder ein anderes OpenRouter-Modell mit Tool-Support) der nächste Schritt.
Welches Modell für den eigenen Fragentyp genügt, entscheidet `eval/run_eval.py --agent --model …` –
nicht die Rangliste. Umgestellt wird über `OPENROUTER_MODEL`/`AGENT_MODEL` in `.env`, `-m` beim
Mini-Agenten oder das Dropdown im Web-Chat.

**Evaluation.** `eval/questions.jsonl` enthält Fragen mit Referenz-SPARQL (Checks: Zeilenzahl, enthält ID)
und offene Fragen ohne Referenz. `make smoke` prüft Index und Umschreibungen ohne LLM; `--agent` lässt den
Mini-Agenten antworten und schreibt Antwort, Tool-Aufrufe und Laufzeit nach `eval/runs/`. Mit der Zeit wird
daraus ein FactGrid-spezifischer Datensatz „Frage → funktionierende SPARQL“, der sich als Beispielsammlung
in `CLAUDE.md` oder für ein späteres Fine-Tuning verwenden lässt.

## 7. Betrieb und Aktualisierung

`make refresh` führt Download, Konvertierung, Neuindexierung und Neustart aus und löscht den
Schema-Cache (zuletzt gemessen: Download 1 GB, Konvertierung 3,5 min, Index 6,5 min). `ops/factgrid-refresh.service`
und `.timer` sind System-Units dafür (nach `/etc/systemd/system/` kopieren, `systemctl enable --now
factgrid-refresh.timer`): täglich 02:00, weil FactGrid den JSON-Dump ab ~22:00 UTC schreibt; um 06:00 folgt
`factgrid-mwdb.timer`. QLever kann während der Indexierung weiterlaufen, da `qlever index
--overwrite-existing` in neue Dateien schreibt; `make refresh` stoppt den Server dennoch vor dem Index, um
RAM zu sparen.

Last auf FactGrid: pro Aktualisierung ein Listing, sechs `HEAD`-Anfragen und ein Download. Das ist
weniger als eine einzige interaktive Sitzung gegen den Live-Endpunkt.

## 8. Grenzen und Abweichungen zum offiziellen RDF

Der Konverter ist eine Reimplementierung, kein Aufruf von Wikibase-Code. Bekannte Abweichungen:
Wertknoten-IRIs (`v:<hash>`) und Blank-Node-Namen sind lokal berechnet und nicht mit denen der
Live-Instanz identisch (Referenz-Hashes stammen dagegen aus dem Dump); normalisierte Werte und die
OWL-Definitionen fehlen; `skos:prefLabel`/`schema:name` fehlen ohne `--full-labels`; Sitelinks werden nur
für Wikipedia-, Wikidata- und Commons-Sites gebildet; julianische Daten v. Chr. werden nicht umgerechnet;
Einheit „1“ wird wie in Wikidata auf `wd:Q199` abgebildet (konfigurierbar). Wer die Live-Semantik exakt
braucht, ersetzt die Konvertierung durch einen serverseitigen `dumpRdf.php`-Lauf – das Qleverfile bleibt
gleich. Sollte FactGrid eines Tages RDF-Dumps anbieten, entfällt `make convert`.

Nicht unterstützt sind Blazegraph-Erweiterungen jenseits des Label-Service (z. B. `SERVICE wikibase:mwapi`,
`bd:sample`); `#defaultView`-Kommentare werden entfernt.

## 9. Roadmap

Nächste sinnvolle Schritte, jeweils unabhängig voneinander: die Seiteninhalte (`MW_TEXT=1`, 160 GB)
mitladen und ein Tool für Versionsvergleiche darauf setzen („was genau wurde an Q… geändert“); den
Textindex aktivieren und die Suche darauf umstellen; die `FactGrid:Directory of Properties`-Seite als Resource in den MCP-Server holen (das ergänzt
`schema_overview` um die redaktionellen Erklärungen); Open WebUI als Mehrbenutzer-Oberfläche mit
`factgrid-mcp --http`; die `eval/runs/`-Protokolle in ein Fine-Tuning-Set überführen; GeoSPARQL-Beispiele
in `eval/questions.jsonl` aufnehmen.

## 10. Verzeichnisstruktur

```
factgrid-local/
├── README.md               diese Architekturdoku
├── Makefile                fetch · convert · index · start · refresh · mcp · chat · test · smoke · agent
├── Qleverfile.in           Vorlage der QLever-Konfiguration (32-GB-Profil) → qlever/Qleverfile
├── .mcp.json               Claude-Code-Registrierung des MCP-Servers
├── CLAUDE.md               Arbeitsanweisung für das Modell (auch System-Prompt des Mini-Agenten)
├── .env.example            Umgebungsvariablen (QLever, FactGrid, API-Schlüssel/Modelle)
├── scripts/fetch_dump.py   Dump-Beschaffung mit Vollständigkeitsprüfung
├── scripts/wb2rdf.py       JSON → Turtle (Wikibase-RDF-Modell)
├── scripts/ui_prefixes.py  FactGrid-Prefixe und Beispielabfragen für die QLever-UI
├── scripts/mw_load.py      MediaWiki-SQL-Dump → MariaDB (Allowlist, Bereinigung, Views, Lesebenutzer)
├── mcp/                    factgrid-mcp (FastMCP, 10 Tools): server.py, qlever.py, labelservice.py, prefixes.py, mwdb.py
├── agent/mini_agent.py     Tool-Loop ohne Claude Code (OpenRouter/Anthropic)
├── chat/                   Web-Chatbot mit Modellauswahl (server.py, index.html)
├── eval/                   questions.jsonl, run_eval.py, runs/
├── ops/                    systemd-Units: täglicher QLever-Refresh (02:00), Chat-Dienst, täglicher SQL-Dump-Import (06:00)
├── tests/                  test_wb2rdf.py, test_mcp.py, test_mwdb.py, mock_sparql.py
└── LICENSE                 MIT
```

## 11. Lizenz

MIT, siehe `LICENSE`.
├── scripts/weekly_briefing.py  Wochenbriefing an die Community-Liste (freitags 08:00 Ortszeit)
