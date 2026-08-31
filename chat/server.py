#!/usr/bin/env python3
"""
server.py – Web-Chatbot über dem lokalen FactGrid-MCP mit LLM-Auswahl.

Drei Provider, ein Tool-Loop:
  * openrouter – Responses-API (POST /api/v1/responses), wenn OPENROUTER_API_KEY gesetzt ist
  * anthropic  – Messages-API, wenn ein Key auflösbar ist (ANTHROPIC_API_KEY / .env / ant-Profil)
  * openai     – Responses-API (POST /v1/responses), wenn OPENAI_API_KEY gesetzt ist

Alle drei laufen über eine Cloud-API: Fragen und Query-Ergebnisse verlassen den Rechner,
nur der QLever-Index bleibt lokal.

Der MCP-Server factgrid_mcp läuft in-process (fastmcp In-Memory-Transport) wie in
agent/mini_agent.py; QLEVER_ENDPOINT usw. gelten wie in .mcp.json. Jede Antwort wird
als JSONL nach eval/runs/ protokolliert (dieselbe Struktur wie beim mini_agent).

    make chat            # http://127.0.0.1:8177
    FACTGRID_CHAT_PORT=8178 make chat

Der Gesprächsverlauf liegt pro Sitzung in einem neutralen Format auf dem Server;
erst beim Request wird er in das Format des gewählten Providers übersetzt. Dadurch
kann das Modell mitten im Gespräch gewechselt werden – auch zwischen Providern.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "eval" / "runs"
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))
TOOL_RESULT_LIMIT = 12000  # Zeichen pro Tool-Ergebnis im Kontext (wie mini_agent)

sys.path.insert(0, str(ROOT / "mcp"))
from factgrid_mcp.envfile import load_env  # noqa: E402

load_env(ROOT / ".env")

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastmcp import Client  # noqa: E402

from factgrid_mcp.server import mcp as SERVER  # noqa: E402

SYSTEM = (ROOT / "CLAUDE.md").read_text(encoding="utf-8") if (ROOT / "CLAUDE.md").exists() else (
    "Du beantwortest Fragen zu FactGrid über die bereitgestellten Tools. Erst search_entities, "
    "dann get_entity, dann sparql. Immer LIMIT. Keine IDs raten."
)

# ---------------------------------------------------------------------------
# Neutrales Gesprächsformat (Sitzung → Provider-Format erst beim Request):
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
#   {"role": "tool", "id": str, "name": str, "content": str}
# ---------------------------------------------------------------------------
SESSIONS: dict[str, list[dict]] = {}
MCP_TOOLS: list = []  # von client.list_tools(), einmal beim Start

OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "z-ai/glm-5.3-flash")


def _tools_responses() -> list[dict]:
    """Tool-Format der Responses-API: flach, ohne die "function"-Verschachtelung."""
    return [{"type": "function", "name": t.name, "description": (t.description or "")[:600],
             "parameters": t.inputSchema or {"type": "object", "properties": {}}}
            for t in MCP_TOOLS]


def _tools_anthropic() -> list[dict]:
    return [{"name": t.name, "description": (t.description or "")[:600],
             "input_schema": t.inputSchema or {"type": "object", "properties": {}}}
            for t in MCP_TOOLS]


def _parse_args(args) -> dict:
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return {"query": args}
    return dict(args or {})


# ---------------------------------------------------------------------------
# Provider-Backends. step() ist ein Async-Generator über SSE-Events und liefert
# als letztes Event {"type": "_step_end", "msg": <nativ>, "text": str,
# "calls": [{"id","name","args"}]}. Innerhalb eines Tool-Loops bleiben die
# Nachrichten im nativen Format (Anthropic braucht z. B. seine thinking-Blöcke
# zwischen tool_use und tool_result unverändert zurück).
# ---------------------------------------------------------------------------
class ResponsesBackend:
    """Gemeinsame Basis für Anbieter mit der Responses-API (POST /v1/responses): OpenAI selbst
    und OpenRouter. Wir fahren zustandslos – `store=False`, der komplette Verlauf geht in jedem
    Request mit. Bei Reasoning-Modellen müssen die reasoning-Items dabei mitwandern; damit das
    ohne serverseitigen Zustand geht, fordert `include` ihren verschlüsselten Inhalt an
    (OpenAI: reasoning.encrypted_content)."""

    client = None          # in der Unterklasse gesetzt
    include: list[str] = []

    def history(self, history: list[dict]) -> list[dict]:
        # Neutral → input-Items der Responses-API.
        items: list[dict] = []
        for m in history:
            if m["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": m["id"], "output": m["content"]})
            elif m["role"] == "assistant":
                if m["content"].strip():
                    items.append({"role": "assistant", "content": [{"type": "output_text", "text": m["content"]}]})
                for c in m.get("tool_calls", []):
                    item = {"type": "function_call", "call_id": c["id"], "name": c["name"],
                            "arguments": json.dumps(c["args"], ensure_ascii=False)}
                    if c.get("item_id"):  # Item-id aus der ursprünglichen Antwort mitgeben
                        item["id"] = c["item_id"]
                    items.append(item)
            else:
                items.append({"role": "user", "content": m["content"]})
        return items

    def tool_results(self, results: list[dict]) -> list[dict]:
        return [{"type": "function_call_output", "call_id": r["id"], "output": r["content"]} for r in results]

    async def step(self, model: str, msgs: list[dict]):
        text, items = "", []
        stream = await self.client.responses.create(
            model=model, input=msgs, instructions=SYSTEM, tools=_tools_responses(),
            stream=True, store=False, **({"include": self.include} if self.include else {}),
        )
        async for ev in stream:
            kind = getattr(ev, "type", "")
            if kind == "response.output_text.delta":
                text += ev.delta
                yield {"type": "token", "text": ev.delta}
            elif kind == "response.output_item.done":
                items.append(ev.item)
            elif kind == "response.completed":
                items = list(ev.response.output)  # vollständige Liste, schlägt die Einzel-Items
        # Die Output-Items (auch reasoning-Items) unverändert zurückgeben – der nächste
        # Request muss den kompletten Verlauf enthalten.
        native = [i.model_dump(exclude_none=True) if hasattr(i, "model_dump") else dict(i) for i in items]
        calls = [{"id": i["call_id"], "item_id": i.get("id"), "name": i["name"],
                  "args": _parse_args(i.get("arguments"))}
                 for i in native if i.get("type") == "function_call"]
        yield {"type": "_step_end", "msg": native, "text": text, "calls": calls}


class OpenRouterBackend(ResponsesBackend):
    """OpenRouter über die Responses-API (OpenAI-SDK mit eigener base_url). Die Beta ist strikt
    zustandslos: `store: true`/`previous_response_id` werden mit 400 abgelehnt."""

    name = "openrouter"

    def __init__(self):
        import openai
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise RuntimeError("OPENROUTER_API_KEY fehlt")
        headers = {"X-Title": os.environ.get("OPENROUTER_SITE_NAME", "FactGrid lokal")}
        if os.environ.get("OPENROUTER_SITE_URL"):  # HTTP-Referer ist optional (Rankings)
            headers["HTTP-Referer"] = os.environ["OPENROUTER_SITE_URL"]
        self.client = openai.AsyncOpenAI(base_url=OPENROUTER_BASE_URL,
                                         api_key=os.environ["OPENROUTER_API_KEY"],
                                         default_headers=headers)

    async def models(self) -> list[dict]:
        # OpenRouter führt hunderte Modelle; die Auswahl im Dropdown kommt deshalb aus
        # OPENROUTER_MODELS (Komma-Liste), nicht aus /v1/models.
        ids = [m.strip() for m in os.environ.get("OPENROUTER_MODELS", OPENROUTER_MODEL).split(",") if m.strip()]
        return [{"id": f"openrouter/{i}", "label": i, "provider": "OpenRouter (Cloud)", "note": "Cloud"}
                for i in ids]


class AnthropicBackend:
    name = "anthropic"

    def __init__(self):
        import anthropic
        self.client = anthropic.AsyncAnthropic()

    async def models(self) -> list[dict]:
        out = []
        async for m in self.client.models.list():
            if m.id.startswith("claude"):
                out.append({"id": f"anthropic/{m.id}", "label": m.display_name or m.id,
                            "provider": "Anthropic (Cloud)", "note": "Cloud"})
        return out

    def history(self, history: list[dict]) -> list[dict]:
        # Neutral → Anthropic-Blöcke. thinking-Blöcke früherer Antworten dürfen
        # entfallen; nur innerhalb eines laufenden Tool-Loops (siehe run_turn)
        # müssen die Original-Blöcke erhalten bleiben – dort wird msg nativ angehängt.
        msgs, pending_results = [], []
        for m in history:
            if m["role"] == "tool":
                pending_results.append({"type": "tool_result", "tool_use_id": m["id"], "content": m["content"]})
                continue
            if pending_results:  # alle tool_results EINER Assistant-Runde in EINER User-Nachricht
                msgs.append({"role": "user", "content": pending_results})
                pending_results = []
            if m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m["content"].strip() else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                           for c in m.get("tool_calls", [])]
                if blocks:
                    msgs.append({"role": "assistant", "content": blocks})
            else:
                msgs.append({"role": "user", "content": m["content"]})
        if pending_results:
            msgs.append({"role": "user", "content": pending_results})
        return msgs

    def tool_results(self, results: list[dict]) -> list[dict]:
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in results]}]

    async def step(self, model: str, msgs: list[dict]):
        # Kein temperature (auf Opus 5/Sonnet 5 entfernt), kein thinking-Parameter
        # (Opus 5 denkt per Default adaptiv; ältere Modelle wie Haiku 4.5 würden
        # {"type":"adaptive"} ablehnen). System-Prompt mit Cache-Breakpoint.
        async with self.client.messages.stream(
            model=model, max_tokens=64000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=_tools_anthropic(), messages=msgs,
        ) as stream:
            async for delta in stream.text_stream:
                yield {"type": "token", "text": delta}
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            why = getattr(final.stop_details, "explanation", None) if final.stop_details else None
            yield {"type": "error", "message": f"Das Modell hat die Anfrage abgelehnt. {why or ''}".strip()}
        text = "".join(b.text for b in final.content if b.type == "text")
        calls = [{"id": b.id, "name": b.name, "args": dict(b.input)}
                 for b in final.content if b.type == "tool_use"]
        # final.content nativ zurückgeben – enthält ggf. thinking-Blöcke, die im
        # Tool-Loop unverändert zurückgeschickt werden müssen.
        yield {"type": "_step_end", "msg": {"role": "assistant", "content": final.content},
               "text": text, "calls": calls}


class OpenAIBackend(ResponsesBackend):
    """OpenAI direkt – ebenfalls über die Responses-API: Reasoning-Modelle (gpt-5.x, o-Serie)
    lehnen Function-Tools in /v1/chat/completions ab, sobald reasoning_effort nicht "none" ist,
    und nur die Responses-API kann die reasoning-Items über Tool-Aufrufe hinweg mitführen."""

    name = "openai"
    include = ["reasoning.encrypted_content"]  # nötig, weil wir store=False fahren
    _SKIP = ("embedding", "audio", "realtime", "tts", "whisper", "image", "dall-e",
             "moderation", "transcribe", "search", "instruct")

    def __init__(self):
        import openai
        self.client = openai.AsyncOpenAI()

    async def models(self) -> list[dict]:
        out = []
        async for m in self.client.models.list():
            if (m.id.startswith("gpt-") or re.match(r"^o\d", m.id)) and not any(s in m.id for s in self._SKIP):
                out.append({"id": f"openai/{m.id}", "label": m.id, "provider": "OpenAI (Cloud)", "note": "Cloud"})
        return sorted(out, key=lambda m: m["label"])


def _backends() -> dict[str, object]:
    out = {}
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            out["openrouter"] = OpenRouterBackend()
        except Exception:
            pass
    # Anthropic: Key kann auch aus einem `ant auth login`-Profil kommen – einfach probieren.
    try:
        out["anthropic"] = AnthropicBackend()
    except Exception:
        pass
    if os.environ.get("OPENAI_API_KEY"):
        try:
            out["openai"] = OpenAIBackend()
        except Exception:
            pass
    return out


BACKENDS = _backends()

# ---------------------------------------------------------------------------
# Tool-Loop (providerneutral) + SSE
# ---------------------------------------------------------------------------
MCP_CLIENT: Client | None = None


async def run_turn(backend, model: str, history: list[dict], question: str):
    """Async-Generator über SSE-Events; hängt das Ergebnis an history an."""
    msgs = backend.history(history) + [{"role": "user", "content": question}]
    history.append({"role": "user", "content": question})
    log = {"ts": datetime.now().isoformat(timespec="seconds"), "model": f"{backend.name}/{model}",
           "question": question, "calls": [], "ui": "chat"}
    t0 = time.time()
    answer = ""
    for _ in range(MAX_STEPS):
        end = None
        async for ev in backend.step(model, msgs):
            if ev["type"] == "_step_end":
                end = ev
            else:
                yield ev
        # Anthropic/OpenAI liefern eine Nachricht, die Responses-API eine Liste von Items.
        msgs.extend(end["msg"] if isinstance(end["msg"], list) else [end["msg"]])
        history.append({"role": "assistant", "content": end["text"], "tool_calls": end["calls"]})
        if not end["calls"]:
            answer = end["text"]
            break
        results = []
        for c in end["calls"]:
            yield {"type": "tool_call", "name": c["name"], "args": c["args"]}
            try:
                res = await MCP_CLIENT.call_tool(c["name"], c["args"])
                text = "\n".join(getattr(b, "text", "") for b in res.content) if hasattr(res, "content") else str(res)
            except Exception as e:  # Tool-Fehler zurück ans Modell, nicht abbrechen
                text = f"FEHLER: {e}"
            text = text[:TOOL_RESULT_LIMIT]
            yield {"type": "tool_result", "name": c["name"], "head": text[:1200]}
            log["calls"].append({"tool": c["name"], "args": c["args"], "result_head": text[:2000]})
            results.append({"id": c["id"], "name": c["name"], "content": text})
            history.append({"role": "tool", "id": c["id"], "name": c["name"], "content": text})
        msgs.extend(backend.tool_results(results))
    else:
        answer = "(Abbruch: zu viele Schritte)"
        yield {"type": "token", "text": answer}
    log["answer"], log["seconds"] = answer, round(time.time() - t0, 1)
    RUNS.mkdir(parents=True, exist_ok=True)
    with open(RUNS / f"{datetime.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(log, ensure_ascii=False, default=str) + "\n")
    yield {"type": "done", "seconds": log["seconds"], "calls": len(log["calls"]),
           "model": f"{backend.name}/{model}"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MCP_CLIENT, MCP_TOOLS
    async with Client(SERVER) as client:
        MCP_CLIENT = client
        MCP_TOOLS = await client.list_tools()
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/models")
async def models():
    groups, errors = [], {}
    for name, backend in BACKENDS.items():
        try:
            groups.extend(await backend.models())
        except Exception as e:
            errors[name] = str(e)[:200]
    default = next((m["id"] for m in groups if m["id"] == f"openrouter/{OPENROUTER_MODEL}"),
                   next((m["id"] for m in groups if m["id"] == "anthropic/claude-opus-5"),
                        groups[0]["id"] if groups else None))
    return {"models": groups, "default": default, "errors": errors}


@app.get("/api/info")
async def info():
    """Steckbrief des Spiegels für die Kopfzeile – kommt aus dem MCP-Tool get_wikibase_info
    (gecacht), damit die Oberfläche denselben Datenstand zeigt wie das Modell sieht."""
    try:
        res = await MCP_CLIENT.call_tool("get_wikibase_info", {})
        text = "\n".join(getattr(b, "text", "") for b in res.content)
    except Exception as e:
        return {"error": str(e)[:200]}

    def field(name: str) -> str:
        m = re.search(rf"^\s*{name}:\s*(.+)$", text, re.M)
        return m.group(1).strip() if m else ""

    return {"base": field("Basis-IRI"), "endpoint": field("Abgefragt wird").replace("QLever unter ", ""),
            "dump_date": field("Datenstand").replace("Dump vom ", ""),
            "triples": field("Tripel"), "items": field("Items"), "text": text}


@app.get("/api/history")
async def get_history(session: str = "default"):
    """Verlauf einer Sitzung im neutralen Format – die Oberfläche stellt ihn nach einem
    Neuladen wieder her (die Sitzungen liegen im Speicher, ein Serverneustart leert sie)."""
    return {"history": SESSIONS.get(session, [])}


@app.post("/api/reset")
async def reset(request: Request):
    body = await request.json()
    SESSIONS.pop(body.get("session") or "", None)
    return {"ok": True}


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    session, model_id = body.get("session") or "default", body.get("model") or ""
    question = (body.get("message") or "").strip()
    provider, _, model = model_id.partition("/")
    history = SESSIONS.setdefault(session, [])

    async def sse():
        if not question or provider not in BACKENDS or not model:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Ungültige Anfrage (Modell/Frage).'})}\n\n"
            return
        try:
            async for ev in run_turn(BACKENDS[provider], model, history, question):
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:500]})}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main() -> None:
    import uvicorn
    host = os.environ.get("FACTGRID_CHAT_HOST", "127.0.0.1")
    port = int(os.environ.get("FACTGRID_CHAT_PORT", "8177"))
    print(f"FactGrid-Chat: http://{host}:{port}  (Provider: {', '.join(BACKENDS) or 'KEINE!'})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
