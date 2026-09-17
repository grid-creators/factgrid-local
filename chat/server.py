#!/usr/bin/env python3
"""
server.py – Web-Chatbot über dem lokalen FactGrid-MCP mit LLM-Auswahl.

Vier Provider, ein Tool-Loop:
  * openrouter – Responses-API (POST /api/v1/responses), wenn OPENROUTER_API_KEY gesetzt ist
  * anthropic  – Messages-API, wenn ein Key auflösbar ist (ANTHROPIC_API_KEY / .env / ant-Profil)
  * openai     – Responses-API (POST /v1/responses), wenn OPENAI_API_KEY gesetzt ist
  * deepseek   – Chat-Completions-API von api.deepseek.com (z. B. deepseek-flash = V4.1 Flash),
                 Schlüssel DEEPSEEK_API_KEY aus .env oder aus dem Benutzerprofil

Freigeschaltet sind nur die Modelle aus FACTGRID_CHAT_MODELS (Komma-Liste "anbieter/modell");
FACTGRID_CHAT_MODEL ist die Vorauswahl. Der API-Schlüssel eines Anbieters kommt aus dem
Benutzerprofil (.chat-profiles.json) oder – wenn dort keiner liegt – aus .env.

Alle drei laufen über eine Cloud-API: Fragen und Query-Ergebnisse verlassen den Rechner,
nur der QLever-Index bleibt lokal.

Der MCP-Server factgrid_mcp läuft in-process (fastmcp In-Memory-Transport) wie in
agent/mini_agent.py; QLEVER_ENDPOINT usw. gelten wie in .mcp.json. Jede Antwort wird
als JSONL nach eval/runs/ protokolliert (dieselbe Struktur wie beim mini_agent).

    make chat            # http://127.0.0.1:8177
    FACTGRID_CHAT_PORT=8178 make chat

Der Gesprächsverlauf liegt pro Gespräch in einem neutralen Format auf dem Server
(.chat-history/<benutzer>/<id>.json, FACTGRID_CHAT_HISTORY); erst beim Request wird
er in das Format des gewählten Providers übersetzt. Dadurch kann das Modell mitten im
Gespräch gewechselt werden – auch zwischen Providern. Die Oberfläche listet die
Gespräche in einer Seitenleiste (/api/chats) und kann einzelne löschen.

Jedes gespeicherte Gespräch hat einen permanenten Link /s/<token>, unter dem es ohne
Anmeldung gelesen (nicht geschrieben) werden kann; wer angemeldet ist, kann es über
/api/shared/continue als eigenes Gespräch mit neuer ID und neuem Link weiterführen.

An eine Frage lassen sich Dateien anhängen (/api/upload). Sie werden beim Hochladen in
Text verwandelt und wandern mit der Frage in den Verlauf; das Modell sieht sie als Teil
der Benutzer-Nachricht. Umgekehrt bietet die Oberfläche jede Tabelle und jeden
```tsv/```csv-Codeblock einer Antwort als Datei zum Herunterladen an (im Browser aus
dem Angezeigten gebaut, ohne Umweg über den Server).
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
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

import base64  # noqa: E402
import secrets  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastmcp import Client  # noqa: E402

from factgrid_mcp.server import mcp as SERVER  # noqa: E402

SYSTEM = (ROOT / "CLAUDE.md").read_text(encoding="utf-8") if (ROOT / "CLAUDE.md").exists() else (
    "Du beantwortest Fragen zu FactGrid über die bereitgestellten Tools. Erst search_entities, "
    "dann get_entity, dann sparql. Immer LIMIT. Keine IDs raten."
)

# ---------------------------------------------------------------------------
# Neutrales Gesprächsformat (Sitzung → Provider-Format erst beim Request):
#   {"role": "user", "content": str, "files": [{"name", "size", "chars", "lines",
#                                               "truncated", "text"}]}
#   {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
#   {"role": "tool", "id": str, "name": str, "content": str}
# Gespräche liegen in der Ablage unten (get_chat/save_chat), je Benutzer und Gespräch.
# ---------------------------------------------------------------------------
MCP_TOOLS: list = []  # von client.list_tools(), einmal beim Start


def attachment_text(f: dict) -> str:
    """Eine angehängte Datei im Prompt: Steckbrief und die ersten UPLOAD_PREVIEW Zeichen.
    Der ganze Inhalt bleibt auf dem Server – dafür sind die Werkzeuge da, sonst läge die
    Datei in jeder Folgefrage wieder im Kontext."""
    facts = [f"{f.get('lines', 0)} Zeilen", f"{f.get('chars', 0)} Zeichen"]
    if f.get("part"):      # der Browser hat nur den Anfang einer zu großen Datei geschickt
        facts.insert(0, "nur der Anfang der hochgeladenen Datei")
    if f.get("columns"):
        facts.append(f"Trennzeichen {f.get('delim_label', '?')}, {len(f['columns'])} Spalten: "
                     + ", ".join(f["columns"]))
    return (f"--- Angehängte Datei: {f['name']} ({'; '.join(facts)}) ---\n"
            f"{f.get('head') or ''}\n"
            f"--- Ende des Anfangs von {f['name']}. Die ganze Datei liegt auf dem Server: "
            f"read_attachment (Zeilen lesen), search_attachment (Zeilen suchen), "
            f"column_stats (Spalte auszählen). ---")


def user_content(m: dict) -> str:
    """Neutrale Benutzer-Nachricht → Text für den Provider: die Frage, darunter die
    angehängten Dateien. Alle vier Backends gehen hier durch, damit ein Anhang in jedem
    Format ankommt (keiner der Provider bekommt Dateien als eigenen Block)."""
    text = (m.get("content") or "").strip()
    files = m.get("files") or []
    if not files:
        return m.get("content") or ""
    return "\n\n".join([text or "(keine Frage – siehe die angehängte(n) Datei(en))"]
                       + [attachment_text(f) for f in files])


OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "z-ai/glm-5.3-flash")


def tool_specs(with_attachments: bool = False) -> list[dict]:
    """Die Werkzeuge für diesen Request: die MCP-Tools, und in einem Gespräch mit Anhängen
    zusätzlich die Werkzeuge auf den Dateien (LOCAL_TOOLS). Ohne Anhang bleiben sie weg –
    sie kosten sonst in jedem Request Kontext und laden zu sinnlosen Aufrufen ein."""
    specs = [{"name": t.name, "description": (t.description or "")[:600],
              "schema": t.inputSchema or {"type": "object", "properties": {}}}
             for t in MCP_TOOLS]
    if with_attachments:
        specs += [{"name": name, "description": t["description"][:600], "schema": t["schema"]}
                  for name, t in LOCAL_TOOLS.items()]
    return specs


def _tools_responses(specs: list[dict]) -> list[dict]:
    """Tool-Format der Responses-API: flach, ohne die "function"-Verschachtelung."""
    return [{"type": "function", "name": s["name"], "description": s["description"],
             "parameters": s["schema"]} for s in specs]


def _tools_chat(specs: list[dict]) -> list[dict]:
    """Tool-Format der Chat-Completions-API (OpenAI-kompatibel, z. B. DeepSeek)."""
    return [{"type": "function", "function": {
                "name": s["name"], "description": s["description"], "parameters": s["schema"]}}
            for s in specs]


def _tools_anthropic(specs: list[dict]) -> list[dict]:
    return [{"name": s["name"], "description": s["description"], "input_schema": s["schema"]}
            for s in specs]


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
                items.append({"role": "user", "content": user_content(m)})
        return items

    def tool_results(self, results: list[dict]) -> list[dict]:
        return [{"type": "function_call_output", "call_id": r["id"], "output": r["content"]} for r in results]

    async def step(self, model: str, msgs: list[dict], specs: list[dict]):
        text, items = "", []
        stream = await self.client.responses.create(
            model=model, input=msgs, instructions=SYSTEM, tools=_tools_responses(specs),
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

    def __init__(self, api_key: str | None = None):
        import openai
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY fehlt")
        headers = {"X-Title": os.environ.get("OPENROUTER_SITE_NAME", "FactGrid lokal")}
        if os.environ.get("OPENROUTER_SITE_URL"):  # HTTP-Referer ist optional (Rankings)
            headers["HTTP-Referer"] = os.environ["OPENROUTER_SITE_URL"]
        self.client = openai.AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key,
                                         default_headers=headers)

    async def models(self) -> list[dict]:
        # OpenRouter führt hunderte Modelle; die Auswahl im Dropdown kommt deshalb aus
        # OPENROUTER_MODELS (Komma-Liste), nicht aus /v1/models.
        ids = [m.strip() for m in os.environ.get("OPENROUTER_MODELS", OPENROUTER_MODEL).split(",") if m.strip()]
        return [{"id": f"openrouter/{i}", "label": i, "provider": "OpenRouter (Cloud)", "note": "Cloud"}
                for i in ids]


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

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
                msgs.append({"role": "user", "content": user_content(m)})
        if pending_results:
            msgs.append({"role": "user", "content": pending_results})
        return msgs

    def tool_results(self, results: list[dict]) -> list[dict]:
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in results]}]

    async def step(self, model: str, msgs: list[dict], specs: list[dict]):
        # Kein temperature (auf Opus 5/Sonnet 5 entfernt), kein thinking-Parameter
        # (Opus 5 denkt per Default adaptiv; ältere Modelle wie Haiku 4.5 würden
        # {"type":"adaptive"} ablehnen). System-Prompt mit Cache-Breakpoint.
        async with self.client.messages.stream(
            model=model, max_tokens=64000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=_tools_anthropic(specs), messages=msgs,
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

    def __init__(self, api_key: str | None = None):
        import openai
        self.client = openai.AsyncOpenAI(api_key=api_key) if api_key else openai.AsyncOpenAI()

    async def models(self) -> list[dict]:
        out = []
        async for m in self.client.models.list():
            if (m.id.startswith("gpt-") or re.match(r"^o\d", m.id)) and not any(s in m.id for s in self._SKIP):
                out.append({"id": f"openai/{m.id}", "label": m.id, "provider": "OpenAI (Cloud)", "note": "Cloud"})
        return sorted(out, key=lambda m: m["label"])


class DeepSeekBackend:
    """DeepSeek (V4.1 Flash = deepseek-flash u. a.) über die OpenAI-kompatible Chat-Completions-API
    von https://api.deepseek.com. Der Denkmodus ist dort standardmäßig an (DEEPSEEK_THINKING=disabled
    schaltet ihn ab, DEEPSEEK_REASONING_EFFORT=low|high steuert ihn). Regel der API: bei Requests
    mit tools muss reasoning_content in allen Folge-Requests zurückgegeben werden – deshalb wandert
    es als "reasoning" in den neutralen Verlauf und wird in history() wieder eingesetzt."""

    name = "deepseek"

    def __init__(self, api_key: str | None = None):
        import openai
        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY fehlt")
        self.client = openai.AsyncOpenAI(
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), api_key=api_key)
        self.thinking = os.environ.get("DEEPSEEK_THINKING", "enabled").strip() or "enabled"
        self.effort = os.environ.get("DEEPSEEK_REASONING_EFFORT", "").strip()
        self.max_tokens = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "16384"))

    async def models(self) -> list[dict]:
        out = []
        async for m in self.client.models.list():
            out.append({"id": f"deepseek/{m.id}", "label": m.id, "provider": "DeepSeek (Cloud)", "note": "Cloud"})
        return out

    def history(self, history: list[dict]) -> list[dict]:
        # Neutral → Chat-Completions-Nachrichten; der System-Prompt steht hier in messages.
        msgs: list[dict] = [{"role": "system", "content": SYSTEM}]
        for m in history:
            if m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict = {"role": "assistant", "content": m["content"] or ""}
                if m.get("reasoning"):
                    msg["reasoning_content"] = m["reasoning"]
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"],
                                                       "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                                         for c in m["tool_calls"]]
                msgs.append(msg)
            else:
                msgs.append({"role": "user", "content": user_content(m)})
        return msgs

    def tool_results(self, results: list[dict]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in results]

    async def step(self, model: str, msgs: list[dict], specs: list[dict]):
        kw: dict = {"model": model, "messages": msgs, "tools": _tools_chat(specs), "stream": True,
                    "max_tokens": self.max_tokens, "extra_body": {"thinking": {"type": self.thinking}}}
        if self.effort:
            kw["reasoning_effort"] = self.effort
        stream = await self.client.chat.completions.create(**kw)
        text, reasoning = "", ""
        calls: dict[int, dict] = {}  # index → {id, name, arguments}
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue  # z. B. der abschließende usage-Chunk
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            piece = getattr(delta, "reasoning_content", None)
            if piece:
                reasoning += piece
            if delta.content:
                text += delta.content
                yield {"type": "token", "text": delta.content}
            for tc in delta.tool_calls or []:
                cur = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    cur["id"] = tc.id
                if tc.function is not None:
                    if tc.function.name:
                        cur["name"] = tc.function.name
                    if tc.function.arguments:
                        cur["arguments"] += tc.function.arguments
        native: dict = {"role": "assistant", "content": text}
        if reasoning:
            native["reasoning_content"] = reasoning
        tool_calls = [calls[i] for i in sorted(calls)]
        if tool_calls:
            native["tool_calls"] = [{"id": c["id"], "type": "function",
                                     "function": {"name": c["name"], "arguments": c["arguments"]}}
                                    for c in tool_calls]
        yield {"type": "_step_end", "msg": native, "text": text, "reasoning": reasoning,
               "calls": [{"id": c["id"], "name": c["name"], "args": _parse_args(c["arguments"])}
                         for c in tool_calls]}


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
    if os.environ.get("DEEPSEEK_API_KEY"):
        try:
            out["deepseek"] = DeepSeekBackend()
        except Exception:
            pass
    return out


BACKEND_CLASSES = {"openrouter": OpenRouterBackend,
                   "anthropic": AnthropicBackend,
                   "openai": OpenAIBackend,
                   "deepseek": DeepSeekBackend}
PROVIDER_LABELS = {"openai": "OpenAI (Cloud)", "anthropic": "Anthropic (Cloud)",
                   "openrouter": "OpenRouter (Cloud)", "deepseek": "DeepSeek (Cloud)"}
SERVER_KEY_VARS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                   "openrouter": "OPENROUTER_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}

# ---------------------------------------------------------------------------
# Freigeschaltete Modelle: FACTGRID_CHAT_MODELS (Komma-Liste "anbieter/modell"),
# Vorauswahl FACTGRID_CHAT_MODEL. Alles andere bleibt in der Oberfläche unsichtbar
# UND wird serverseitig abgelehnt - eine unbekannte Modell-ID aus dem Browser wird
# durch die Vorauswahl ersetzt.
# ---------------------------------------------------------------------------
CHAT_MODEL = os.environ.get("FACTGRID_CHAT_MODEL", "openai/gpt-5.6-luna").strip()
CHAT_MODELS = [m.strip() for m in os.environ.get("FACTGRID_CHAT_MODELS", CHAT_MODEL).split(",") if m.strip()]
if CHAT_MODEL not in CHAT_MODELS:
    CHAT_MODELS.insert(0, CHAT_MODEL)


def split_model(model: str) -> tuple[str, str]:
    """'deepseek/deepseek-flash' → ('deepseek', 'deepseek-flash')."""
    provider, _, model_id = model.partition("/")
    return provider, model_id


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider)


CHAT_PROVIDER, CHAT_MODEL_ID = split_model(CHAT_MODEL)
PROVIDER_LABEL = provider_label(CHAT_PROVIDER)
CHAT_PROVIDERS = []  # in Reihenfolge der Modelle, ohne Dubletten
for _m in CHAT_MODELS:
    _p = split_model(_m)[0]
    if _p not in CHAT_PROVIDERS:
        CHAT_PROVIDERS.append(_p)

# ---------------------------------------------------------------------------
# Benutzerprofile: je Anmeldename ein eigener API-Schlüssel. Liegt als JSON
# neben .env (Modus 600), damit ein Neustart die Schlüssel nicht verliert.
# Schlüssel gehen nie an den Browser zurück - die Oberfläche sieht nur, ob
# einer hinterlegt ist.
# ---------------------------------------------------------------------------
PROFILES_PATH = Path(os.environ.get("FACTGRID_CHAT_PROFILES", str(ROOT / ".chat-profiles.json")))


def _load_profiles() -> dict:
    try:
        return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_profiles(data: dict) -> None:
    tmp = PROFILES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(PROFILES_PATH)


def user_key(user: str, provider: str = "") -> str:
    return (_load_profiles().get(user) or {}).get(provider or CHAT_PROVIDER, "")


def set_user_key(user: str, key: str, provider: str = "") -> None:
    data = _load_profiles()
    data.setdefault(user, {})[provider or CHAT_PROVIDER] = key
    _save_profiles(data)


def clear_user_key(user: str, provider: str = "") -> None:
    data = _load_profiles()
    if user in data:
        data[user].pop(provider or CHAT_PROVIDER, None)
        _save_profiles(data)


def server_key(provider: str) -> str:
    """Schlüssel aus .env für alle Benutzer (z. B. DEEPSEEK_API_KEY)."""
    return os.environ.get(SERVER_KEY_VARS.get(provider, ""), "").strip()


def key_for(user: str, provider: str) -> tuple[str, str]:
    """(Schlüssel, Quelle): erst das Benutzerprofil, sonst der Server-Schlüssel aus .env."""
    key = user_key(user, provider)
    if key:
        return key, "profil"
    key = server_key(provider)
    return (key, "server") if key else ("", "")


_BACKEND_CACHE: dict[tuple, object] = {}


def backend_for(user: str, provider: str = ""):
    """Backend mit dem Schlüssel DIESES Benutzers (oder dem Server-Schlüssel des Anbieters).
    (None, Grund) wenn keiner da ist."""
    provider = provider or CHAT_PROVIDER
    if provider not in BACKEND_CLASSES:
        return None, f"Unbekannter Anbieter {provider!r}."
    key, _source = key_for(user, provider)
    if not key:
        return None, (f"Kein API-Schlüssel für {provider_label(provider)} hinterlegt (Profil von {user!r} "
                      f"oder {SERVER_KEY_VARS.get(provider, '?')} in .env).")
    cached = _BACKEND_CACHE.get((user, provider, key))
    if cached is not None:
        return cached, None
    try:
        backend = BACKEND_CLASSES[provider](api_key=key)
    except Exception as e:
        return None, str(e)[:300]
    _BACKEND_CACHE[(user, provider, key)] = backend
    return backend, None


def model_info(user: str) -> list[dict]:
    """Die freigeschalteten Modelle mit Anbieter und Schlüsselstatus für die Oberfläche."""
    out = []
    for m in CHAT_MODELS:
        provider, model_id = split_model(m)
        key, source = key_for(user, provider)
        out.append({"id": m, "label": model_id, "provider": provider_label(provider),
                    "provider_id": provider, "note": "Cloud", "has_key": bool(key), "key_source": source})
    return out


# ---------------------------------------------------------------------------
# Tool-Loop (providerneutral) + SSE
# ---------------------------------------------------------------------------
MCP_CLIENT: Client | None = None


# ---------------------------------------------------------------------------
# Gesprächsablage: je Benutzer ein Ordner, je Gespräch eine JSON-Datei
# (.chat-history/<benutzer>/<id>.json). Die Oberfläche zeigt sie in der
# Seitenleiste und kann einzelne löschen; ein Neustart des Dienstes verliert
# nichts mehr. Geöffnete Gespräche bleiben im Speicher, geschrieben wird nach
# jeder Antwort (auch nach einem Abbruch). Angelegt wird die Datei erst mit
# der ersten Frage - ein leeres "Neu" hinterlässt nichts.
# ---------------------------------------------------------------------------
HISTORY_DIR = Path(os.environ.get("FACTGRID_CHAT_HISTORY", str(ROOT / ".chat-history")))
TITLE_LEN = 60
_CHAT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # UUIDs aus dem Browser - nichts mit Pfadanteilen
CHATS: dict[tuple[str, str], dict] = {}           # (ordner, id) -> Gespräch


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _owner(user: str) -> str:
    """Ordnername des Benutzers – und der Schlüssel im Speicher. Idempotent, damit ein aus dem
    Ordnernamen zurückgewonnener Benutzer (Share-Index unten) dasselbe Gespräch trifft."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", user) or "_"


def _chat_dir(user: str) -> Path:
    return HISTORY_DIR / _owner(user)


def _chat_path(user: str, chat_id: str) -> Path | None:
    return _chat_dir(user) / f"{chat_id}.json" if _CHAT_ID.match(chat_id or "") else None


def _chat_meta(chat: dict) -> dict:
    return {"id": chat["id"], "title": chat.get("title") or "", "created": chat.get("created", ""),
            "updated": chat.get("updated", ""), "model": chat.get("model", ""),
            "count": sum(1 for m in chat.get("messages", []) if m.get("role") == "user")}


def get_chat(user: str, chat_id: str, create: bool = False) -> dict | None:
    """Gespräch aus dem Speicher oder von der Platte; create=True legt ein neues (noch
    ungespeichertes) an. None bei ungültiger ID oder unbekanntem Gespräch."""
    path = _chat_path(user, chat_id)
    if path is None:
        return None
    chat = CHATS.get((_owner(user), chat_id))
    if chat is None and path.exists():
        try:
            chat = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            chat = None
    if chat is None and create:
        chat = {"id": chat_id, "title": "", "created": _now(), "updated": _now(), "model": "", "messages": []}
    if chat is not None:
        CHATS[(_owner(user), chat_id)] = chat
    return chat


def _trim_open_calls(messages: list[dict]) -> None:
    """Nach einem Abbruch mitten im Tool-Loop fehlen zu den letzten Tool-Aufrufen die Ergebnisse;
    so einen Rest entfernen, damit der Verlauf beim nächsten Request für jeden Anbieter gültig bleibt."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "tool":
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            have = {t.get("id") for t in messages[i + 1:]}
            if any(c.get("id") not in have for c in m["tool_calls"]):
                del messages[i:]
        break


def _write_chat(user: str, chat: dict) -> None:
    """Gesprächsdatei schreiben, ohne Titel und Zeitstempel anzurühren."""
    path = _chat_path(user, chat["id"])
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(chat, ensure_ascii=False, default=str), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save_chat(user: str, chat: dict) -> None:
    _trim_open_calls(chat["messages"])
    if _chat_path(user, chat["id"]) is None or not chat["messages"]:
        return
    if not chat.get("title"):
        msg = next((m for m in chat["messages"] if m.get("role") == "user"), {})
        first = " ".join((msg.get("content") or "").split())
        if not first:                     # Frage nur mit Anhang: die Dateinamen als Titel
            first = ", ".join(f["name"] for f in msg.get("files") or [])
        chat["title"] = first if len(first) <= TITLE_LEN else first[:TITLE_LEN - 1].rstrip() + "…"
    chat["updated"] = _now()
    ensure_share(user, chat)
    _write_chat(user, chat)


def list_chats(user: str) -> list[dict]:
    """Metadaten aller gespeicherten Gespräche des Benutzers, zuletzt benutzte zuerst."""
    out = []
    if _chat_dir(user).is_dir():
        for p in _chat_dir(user).glob("*.json"):
            chat = get_chat(user, p.stem)
            if chat:
                out.append(_chat_meta(chat))
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def delete_chat(user: str, chat_id: str) -> bool:
    chat = get_chat(user, chat_id)                  # auch für das Token, falls es nur auf der Platte steht
    CHATS.pop((_owner(user), chat_id), None)
    if chat:
        SHARES.pop(chat.get("share") or "", None)   # der Link ist damit tot
    folder = _files_dir(user, chat_id)              # und die angehängten Dateien
    if folder is not None and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    path = _chat_path(user, chat_id)
    if path is not None and path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Permanenter Link je Gespräch. Jedes gespeicherte Gespräch bekommt ein
# unerratbares Token (secrets.token_urlsafe), das in der Gesprächsdatei steht
# und sich nie ändert: /s/<token> zeigt das Gespräch ohne Anmeldung, aber nur
# lesend. Das Token IST der Zugang - der Link ist so öffentlich wie die Stelle,
# an die man ihn schreibt; erraten lässt er sich nicht, aufzählen auch nicht.
# Weitergeschrieben wird nie im Original: /api/shared/continue legt für den
# angemeldeten Leser eine Kopie mit neuer ID und eigenem Link an.
# ---------------------------------------------------------------------------
SHARE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SHARES: dict[str, tuple[str, str]] = {}   # token -> (ordner, gespräch-id)
_SHARES_SCANNED = False


def ensure_share(user: str, chat: dict) -> str:
    """Token des Gesprächs; legt es beim ersten Mal an (Aufrufer speichert die Datei)."""
    token = chat.get("share") or ""
    if not SHARE_TOKEN.match(token):
        token = chat["share"] = secrets.token_urlsafe(16)
    SHARES[token] = (_owner(user), chat["id"])
    return token


def _share_index() -> dict[str, tuple[str, str]]:
    """Token → Gespräch. Einmal aus allen Gesprächsdateien aufgebaut (ältere Gespräche und
    ein Neustart), danach hält ensure_share() den Index aktuell."""
    global _SHARES_SCANNED
    if not _SHARES_SCANNED:
        for path in sorted(HISTORY_DIR.glob("*/*.json")):
            try:
                token = (json.loads(path.read_text(encoding="utf-8")).get("share") or "")
            except Exception:
                continue
            if SHARE_TOKEN.match(token):
                SHARES.setdefault(token, (path.parent.name, path.stem))
        _SHARES_SCANNED = True
    return SHARES


def shared_chat(token: str) -> tuple[str, dict] | None:
    """(Ordner, Gespräch) zu einem Link-Token; None bei unbekanntem oder veraltetem Token."""
    if not SHARE_TOKEN.match(token or ""):
        return None
    hit = _share_index().get(token)
    if hit is None:
        return None
    chat = get_chat(*hit)
    if chat is None or chat.get("share") != token:   # Gespräch gelöscht oder Index veraltet
        SHARES.pop(token, None)
        return None
    return hit[0], chat


# ---------------------------------------------------------------------------
# Datei-Anhänge. Hochgeladene Dateien werden beim Upload in Text verwandelt
# (Text/CSV/TSV/JSON/XML/Markdown direkt, PDF über pypdf) und liegen bis zum Absenden
# je Benutzer in .chat-uploads/<benutzer>/<id>.{json,txt}. Mit der Frage zieht die
# Textdatei ins Gespräch (.chat-history/<benutzer>/<gespräch>.files/<id>.txt) und wird
# mit ihm gelöscht.
#
# In den Prompt geht NUR der Steckbrief und der Anfang der Datei (UPLOAD_PREVIEW
# Zeichen): der Verlauf wandert bei jeder Folgefrage komplett wieder zum Anbieter, ein
# ganzer 16-MB-Anhang würde also bei jeder Frage neu bezahlt. Für alles Weitere
# bekommt das Modell Werkzeuge auf der Datei (LOCAL_TOOLS unten): Zeilenfenster lesen,
# Zeilen suchen, eine Spalte auszählen - die laufen hier im Prozess auf der ganzen
# Datei, und nur das Ergebnis kostet Kontext.
#
# Bilder und Office-Dateien nimmt der Chat nicht an: dafür hat jeder der vier Provider
# ein eigenes Format, und der Weg über CSV/TSV ist für Tabellen ohnehin der bessere.
# ---------------------------------------------------------------------------
UPLOAD_DIR = Path(os.environ.get("FACTGRID_CHAT_UPLOADS", str(ROOT / ".chat-uploads")))
UPLOAD_MAX = int(os.environ.get("FACTGRID_CHAT_UPLOAD_MAX", str(32 * 1024 * 1024)))
UPLOAD_PREVIEW = int(os.environ.get("FACTGRID_CHAT_UPLOAD_PREVIEW", "2000"))
UPLOAD_TTL = int(os.environ.get("FACTGRID_CHAT_UPLOAD_TTL", str(24 * 3600)))
UPLOAD_MAX_FILES = 10
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
# Anfangsbytes, an denen ein Nicht-Text-Format sicher zu erkennen ist - damit die
# Fehlermeldung sagt, WAS da hochgeladen wurde, statt Kauderwelsch anzuhängen.
BINARY_MAGIC = [(b"PK\x03\x04", "Eine Office-/ZIP-Datei (docx, xlsx, odt …)"),
                (b"\x89PNG", "Ein Bild"), (b"\xff\xd8\xff", "Ein Bild"), (b"GIF8", "Ein Bild"),
                (b"\x1f\x8b", "Ein gzip-Archiv"), (b"Rar!", "Ein Archiv"), (b"\x7fELF", "Ein Programm"),
                (b"{\\rtf", "Eine RTF-Datei"), (b"\xd0\xcf\x11\xe0", "Eine alte Office-Datei (doc, xls)")]
TEXT_HINT = ("Der Chat liest Text (TXT, CSV/TSV, JSON, XML/TTL, Markdown, SPARQL) und PDF. "
             "Tabellen aus Excel bitte als CSV oder TSV speichern.")


def _decode(data: bytes) -> str | None:
    """Bytes → Text. UTF-8 (auch mit BOM) zuerst, dann die Windows-Kodierung, in der Excel
    CSV schreibt. NUL-Bytes heißen: keine Textdatei."""
    if b"\x00" in data[:8192]:
        return None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _pdf_text(data: bytes) -> tuple[str, str]:
    """Text eines PDFs, Seite für Seite. Ohne pypdf (Extra "chat") geht es nicht, und aus
    einem Scan ohne OCR kommt nichts heraus - beides sagt die Fehlermeldung."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "PDF-Dateien brauchen das Paket pypdf auf dem Server (uv sync --all-extras)."
    import io
    try:
        pages = [(page.extract_text() or "").strip() for page in PdfReader(io.BytesIO(data)).pages]
    except Exception as e:
        return "", f"Das PDF ließ sich nicht lesen: {str(e)[:150]}"
    text = "\n\n".join(f"[Seite {i}]\n{t}" for i, t in enumerate(pages, 1) if t)
    if not text:
        return "", "Aus diesem PDF kommt kein Text - vermutlich ein Scan ohne Texterkennung."
    return text, ""


def extract_text(name: str, data: bytes) -> tuple[str, str]:
    """(Text, Fehlermeldung) einer hochgeladenen Datei; genau eins von beiden ist gefüllt."""
    if data[:5] == b"%PDF-" or name.lower().endswith(".pdf"):
        return _pdf_text(data)
    for magic, what in BINARY_MAGIC:
        if data.startswith(magic):
            return "", f"{what} - damit kann das Modell hier nichts anfangen. {TEXT_HINT}"
    text = _decode(data)
    if text is None:
        return "", f"Das ist keine Textdatei. {TEXT_HINT}"
    return text.replace("\r\n", "\n").replace("\r", "\n"), ""


def _safe_name(name: str) -> str:
    """Dateiname nur zur Anzeige - Pfadanteile, Steuerzeichen und Überlänge weg."""
    name = re.sub(r"[\x00-\x1f]", "", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:120] or "datei.txt"


def _upload_dir(user: str) -> Path:
    return UPLOAD_DIR / _owner(user)


def _upload_path(user: str, upload_id: str, suffix: str = ".json") -> Path | None:
    """Zwischenablage eines Uploads: <id>.json der Steckbrief, <id>.txt der volle Text."""
    return _upload_dir(user) / f"{upload_id}{suffix}" if _UPLOAD_ID.match(upload_id or "") else None


def prune_uploads() -> None:
    """Zwischenablage nach UPLOAD_TTL leeren. Abgeschickte Anhänge liegen dann längst im
    Gespräch; hier verschwindet nur, was nie abgeschickt wurde (und die Kopiervorlage)."""
    cutoff = time.time() - UPLOAD_TTL
    for path in UPLOAD_DIR.glob("*/*.*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


# Trennzeichen einer Tabelle, in der Reihenfolge, in der wir sie ausprobieren.
DELIMITERS = [("\t", "Tabulator"), (";", "Semikolon"), (",", "Komma"), ("|", "Strich")]


def file_profile(text: str) -> dict:
    """Steckbrief einer Textdatei: Zeilen, Zeichen und - wenn es eine Tabelle ist -
    Trennzeichen und Spaltennamen. Als Tabelle gilt nur, was in der zweiten Zeile
    genauso viele Trennzeichen hat wie in der ersten; Fließtext fällt so nicht herein."""
    lines = text.split("\n")
    first = lines[0] if lines else ""
    prof = {"chars": len(text), "lines": len(text.splitlines()) or 1}
    delim, label = max(DELIMITERS, key=lambda d: first.count(d[0]))
    if first.count(delim) and len(lines) > 1 and lines[1].count(delim) == first.count(delim):
        prof["delim"] = delim
        prof["delim_label"] = label
        prof["columns"] = [c.strip().strip('"') for c in first.split(delim)][:60]
    return prof


def save_upload(user: str, name: str, size: int, text: str, part: bool = False) -> dict:
    """Anhang in die Zwischenablage legen; zurück kommt der Steckbrief für die Oberfläche."""
    prune_uploads()
    meta = dict(file_profile(text), id=uuid.uuid4().hex, name=_safe_name(name), size=size, part=part,
                head=text[:UPLOAD_PREVIEW])
    path = _upload_path(user, meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(_upload_path(user, meta["id"], ".txt"), text)
    _write_private(path, json.dumps(meta, ensure_ascii=False))
    return meta


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def read_upload(user: str, upload_id: str) -> dict | None:
    """Steckbrief eines Anhangs aus der Zwischenablage; None bei unbekannter ID."""
    path = _upload_path(user, str(upload_id or ""))
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# --- Anhänge eines Gesprächs ----------------------------------------------
# Die Textdateien liegen neben der Gesprächsdatei in <gespräch>.files/ - ein Gespräch
# löschen heißt damit auch seine Anhänge löschen, eine Kopie (Weiterführen eines
# geteilten Gesprächs) bekommt eigene Dateien.
def _files_dir(user: str, chat_id: str) -> Path | None:
    path = _chat_path(user, chat_id)
    return path.with_suffix(".files") if path is not None else None


def attach_upload(user: str, chat_id: str, upload_id: str) -> dict | None:
    """Anhang aus der Zwischenablage in das Gespräch übernehmen: Textdatei kopieren (die
    Vorlage bleibt liegen, damit ein zweiter Versuch nach einem Fehler noch geht), Steckbrief
    für den Verlauf zurückgeben."""
    meta = read_upload(user, upload_id)
    src = _upload_path(user, str(upload_id or ""), ".txt")
    folder = _files_dir(user, chat_id)
    if meta is None or src is None or not src.exists() or folder is None:
        return None
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    dst = folder / f"{meta['id']}.txt"
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    return meta


def attachment_file(user: str, chat_id: str, file_id: str) -> Path | None:
    folder = _files_dir(user, chat_id)
    if folder is None or not _UPLOAD_ID.match(file_id or ""):
        return None
    path = folder / f"{file_id}.txt"
    return path if path.exists() else None


_TEXT_CACHE: dict[str, tuple[float, str]] = {}   # Pfad -> (mtime, Text), höchstens 4 Dateien


def attachment_content(user: str, chat_id: str, file_id: str) -> str:
    """Der volle Text eines Anhangs. Gecacht, weil ein Modell in einer Antwort mehrmals
    in dieselbe Datei schaut und eine 16-MB-Datei sonst jedes Mal neu von der Platte käme."""
    path = attachment_file(user, chat_id, file_id)
    if path is None:
        return ""
    key, mtime = str(path), path.stat().st_mtime
    hit = _TEXT_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(_TEXT_CACHE) >= 4:
        _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
    _TEXT_CACHE[key] = (mtime, text)
    return text


def chat_attachments(chat: dict) -> list[dict]:
    """Alle Anhänge eines Gesprächs, in der Reihenfolge der Fragen."""
    return [f for m in chat.get("messages", []) if m.get("role") == "user" for f in m.get("files") or []]


def find_attachment(chat: dict, name: str) -> dict | None:
    """Anhang nach Namen: genau, sonst als Teilstring (Groß/Klein egal). Ohne Namen der
    einzige - das Modell soll nicht am Dateinamen scheitern."""
    files = chat_attachments(chat)
    if not files:
        return None
    if not (name or "").strip():
        return files[-1] if len(files) == 1 else None
    name = name.strip().lower()
    return (next((f for f in files if f["name"].lower() == name), None)
            or next((f for f in files if name in f["name"].lower()), None))


# --- Werkzeuge auf den Anhängen -------------------------------------------
# Laufen hier im Prozess auf der ganzen Datei; in den Kontext geht nur das Ergebnis.
# Sie stehen dem Modell nur in Gesprächen mit Anhängen zur Verfügung (tool_specs).
TOOL_OUT_MAX = 8000          # Zeichen je Werkzeug-Antwort
READ_LINES_MAX = 400         # Zeilen je read_attachment
HITS_MAX = 200               # Treffer je search_attachment


def _cut(text: str, note: str = "… (gekürzt, bitte gezielter fragen)") -> str:
    return text if len(text) <= TOOL_OUT_MAX else text[:TOOL_OUT_MAX].rsplit("\n", 1)[0] + "\n" + note


def _no_file(chat: dict, name: str) -> str:
    havent = ", ".join(f["name"] for f in chat_attachments(chat)) or "keine"
    return f"FEHLER: Kein Anhang {name!r} in diesem Gespräch. Vorhanden: {havent}."


def _columns_of(f: dict) -> list[str]:
    return f.get("columns") or []


def _column_index(f: dict, column: str) -> int:
    """Spaltennummer (0-basiert) aus Name oder 1-basierter Nummer; -1 wenn es sie nicht gibt."""
    cols = _columns_of(f)
    column = str(column or "").strip()
    if column.isdigit():
        nr = int(column) - 1
        return nr if 0 <= nr < len(cols) else -1
    low = [c.lower() for c in cols]
    return low.index(column.lower()) if column.lower() in low else -1


def tool_list_attachments(ctx) -> str:
    """Alle Anhänge des Gesprächs mit Steckbrief."""
    user, chat = ctx
    files = chat_attachments(chat)
    if not files:
        return "Dieses Gespräch hat keine angehängten Dateien."
    out = []
    for f in files:
        line = f"{f['name']}: {f.get('lines', 0)} Zeilen, {f.get('chars', 0)} Zeichen"
        if f.get("columns"):
            line += (f", Trennzeichen {f.get('delim_label')}, {len(f['columns'])} Spalten: "
                     + ", ".join(f["columns"]))
        if f.get("part"):
            line += " (nur der Anfang der hochgeladenen Datei)"
        out.append(line)
    return "\n".join(out)


def tool_read_attachment(ctx, name: str = "", from_line: int = 1, lines: int = 100) -> str:
    """Ein Zeilenfenster aus einem Anhang."""
    user, chat = ctx
    f = find_attachment(chat, name)
    if f is None:
        return _no_file(chat, name)
    all_lines = attachment_content(user, chat["id"], f["id"]).splitlines()
    total = len(all_lines)
    start = max(1, int(from_line or 1))
    count = max(1, min(int(lines or 100), READ_LINES_MAX))
    window = all_lines[start - 1:start - 1 + count]
    if not window:
        return f"{f['name']} hat {total} Zeilen – ab Zeile {start} steht nichts mehr."
    header = f"{f['name']}, Zeilen {start}–{start + len(window) - 1} von {total}:"
    return _cut(header + "\n" + "\n".join(window))


def tool_search_attachment(ctx, name: str = "", query: str = "", max_hits: int = 50,
                           regex: bool = False) -> str:
    """Zeilen eines Anhangs, die ein Suchwort (oder einen regulären Ausdruck) enthalten."""
    user, chat = ctx
    f = find_attachment(chat, name)
    if f is None:
        return _no_file(chat, name)
    if not str(query or "").strip():
        return "FEHLER: Kein Suchwort übergeben (query)."
    try:
        rx = re.compile(query if regex else re.escape(query), re.I)
    except re.error as e:
        return f"FEHLER: Der reguläre Ausdruck ist ungültig ({e})."
    limit = max(1, min(int(max_hits or 50), HITS_MAX))
    hits, found = [], 0
    for nr, line in enumerate(attachment_content(user, chat["id"], f["id"]).splitlines(), 1):
        if rx.search(line):
            found += 1
            if len(hits) < limit:
                hits.append(f"{nr}: {line}")
    if not found:
        return f"Keine Zeile in {f['name']} enthält {query!r}."
    head = f"{found} Treffer für {query!r} in {f['name']}" + (f", die ersten {limit}:" if found > limit else ":")
    return _cut(head + "\n" + "\n".join(hits))


def tool_column_stats(ctx, name: str = "", column: str = "", top: int = 30) -> str:
    """Eine Spalte auszählen: gefüllt/leer, verschiedene Werte, die häufigsten."""
    user, chat = ctx
    f = find_attachment(chat, name)
    if f is None:
        return _no_file(chat, name)
    cols = _columns_of(f)
    if not cols:
        return (f"{f['name']} ist keine Tabelle mit erkennbaren Spalten – "
                "read_attachment oder search_attachment benutzen.")
    nr = _column_index(f, column)
    if nr < 0:
        return f"FEHLER: Spalte {column!r} gibt es nicht. Spalten: " + ", ".join(cols)
    delim = f.get("delim", "\t")
    values: dict[str, int] = {}
    filled = empty = 0
    for line in attachment_content(user, chat["id"], f["id"]).splitlines()[1:]:
        if not line.strip():
            continue
        parts = line.split(delim)
        value = parts[nr].strip().strip('"') if nr < len(parts) else ""
        if value:
            filled += 1
            values[value] = values.get(value, 0) + 1
        else:
            empty += 1
    if not filled and not empty:
        return f"{f['name']} hat außer der Kopfzeile keine Zeilen."
    best = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, min(int(top or 30), 200))]
    out = [f"{f['name']}, Spalte {nr + 1} ({cols[nr]}): {filled + empty} Zeilen, "
           f"{filled} gefüllt, {empty} leer, {len(values)} verschiedene Werte.",
           "Häufigste Werte (Wert\tAnzahl):"]
    out += [f"{v}\t{n}" for v, n in best]
    return _cut("\n".join(out))


LOCAL_TOOLS: dict[str, dict] = {
    "list_attachments": {
        "fn": tool_list_attachments,
        "description": ("Die an dieses Gespräch angehängten Dateien mit Umfang, Trennzeichen und "
                        "Spaltennamen. Erster Griff, bevor du in einen Anhang schaust."),
        "schema": {"type": "object", "properties": {}},
    },
    "read_attachment": {
        "fn": tool_read_attachment,
        "description": ("Zeilen aus einer angehängten Datei, von from_line an (1-basiert), höchstens "
                        "400 auf einmal. Damit liest du die Datei stückweise, statt sie ganz in den "
                        "Kontext zu holen."),
        "schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Dateiname (Teil genügt); bei nur einem Anhang optional."},
            "from_line": {"type": "integer", "description": "Erste Zeile, 1-basiert (Vorgabe 1)."},
            "lines": {"type": "integer", "description": "Anzahl Zeilen, höchstens 400 (Vorgabe 100)."}}},
    },
    "search_attachment": {
        "fn": tool_search_attachment,
        "description": ("Zeilen einer angehängten Datei, die query enthalten (Groß/Klein egal, mit "
                        "regex=true als regulärer Ausdruck). Antwort: Zahl der Treffer und die Zeilen "
                        "mit ihrer Zeilennummer. So prüfst du, ob ein Name in einer Liste steht."),
        "schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Dateiname (Teil genügt); bei nur einem Anhang optional."},
            "query": {"type": "string", "description": "Suchwort oder regulärer Ausdruck."},
            "max_hits": {"type": "integer", "description": "Höchstens so viele Zeilen zeigen (Vorgabe 50)."},
            "regex": {"type": "boolean", "description": "query als regulären Ausdruck lesen."}},
            "required": ["query"]},
    },
    "column_stats": {
        "fn": tool_column_stats,
        "description": ("Eine Spalte einer angehängten Tabelle auszählen: wie viele Zeilen gefüllt bzw. "
                        "leer sind, wie viele verschiedene Werte es gibt und die häufigsten. Damit "
                        "beantwortest du Mengenfragen über die ganze Datei, ohne sie zu lesen."),
        "schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Dateiname (Teil genügt); bei nur einem Anhang optional."},
            "column": {"type": "string", "description": "Spaltenname oder Spaltennummer (1-basiert)."},
            "top": {"type": "integer", "description": "Wie viele der häufigsten Werte (Vorgabe 30)."}},
            "required": ["column"]},
    },
}


def call_local_tool(ctx, name: str, args: dict) -> str:
    """Ein Werkzeug auf den Anhängen aufrufen; unbekannte Argumente fallen weg, damit ein
    erfundener Parameter nicht den ganzen Aufruf sprengt."""
    tool = LOCAL_TOOLS[name]
    allowed = set(tool["schema"].get("properties", {}))
    return tool["fn"](ctx, **{k: v for k, v in (args or {}).items() if k in allowed})


async def run_turn(backend, model: str, user: str, chat: dict, question: str,
                   files: list[dict] | None = None):
    """Async-Generator über SSE-Events; hängt das Ergebnis an den Verlauf des Gesprächs an.
    Benutzer und Gespräch braucht der Tool-Loop für die Werkzeuge auf den Anhängen."""
    history = chat["messages"]
    entry: dict = {"role": "user", "content": question}
    if files:
        entry["files"] = files
    history.append(entry)
    specs = tool_specs(bool(chat_attachments(chat)))
    msgs = backend.history(history)      # die neue Frage ist darin schon enthalten
    log = {"ts": datetime.now().isoformat(timespec="seconds"), "model": f"{backend.name}/{model}",
           "question": question, "calls": [], "ui": "chat"}
    if files:
        log["files"] = [{"name": f["name"], "chars": f.get("chars", 0)} for f in files]
    t0 = time.time()
    answer = ""
    for _ in range(MAX_STEPS):
        end = None
        async for ev in backend.step(model, msgs, specs):
            if ev["type"] == "_step_end":
                end = ev
            else:
                yield ev
        # Anthropic/OpenAI liefern eine Nachricht, die Responses-API eine Liste von Items.
        msgs.extend(end["msg"] if isinstance(end["msg"], list) else [end["msg"]])
        entry = {"role": "assistant", "content": end["text"], "tool_calls": end["calls"]}
        if end.get("reasoning"):  # DeepSeek: reasoning_content muss in Folge-Requests zurück
            entry["reasoning"] = end["reasoning"]
        history.append(entry)
        if not end["calls"]:
            answer = end["text"]
            break
        results = []
        for c in end["calls"]:
            yield {"type": "tool_call", "name": c["name"], "args": c["args"]}
            try:
                if c["name"] in LOCAL_TOOLS:      # Werkzeuge auf den Anhängen, ohne MCP
                    text = call_local_tool((user, chat), c["name"], c["args"])
                else:
                    res = await MCP_CLIENT.call_tool(c["name"], c["args"])
                    text = ("\n".join(getattr(b, "text", "") for b in res.content)
                            if hasattr(res, "content") else str(res))
            except Exception as e:  # Tool-Fehler zurück ans Modell, nicht abbrechen
                text = f"FEHLER: {e}"
            text = text[:TOOL_RESULT_LIMIT]
            yield {"type": "tool_result", "name": c["name"], "text": text}
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


# ---------------------------------------------------------------------------
# HTTP-Basic-Auth. Aktiv, sobald FACTGRID_CHAT_USER und FACTGRID_CHAT_PASSWORD
# gesetzt sind - noetig, weil der Dienst hinter dem nginx-proxy-manager
# oeffentlich steht: jede Frage kostet API-Guthaben, und ueber /api/keys liesse
# sich sonst der hinterlegte Schluessel ersetzen oder loeschen. Ohne die beiden
# Variablen (rein lokaler Betrieb) bleibt alles offen wie bisher.
# ---------------------------------------------------------------------------
def _load_users() -> dict[str, str]:
    """Zugangsdaten aus .env: ein Paar über FACTGRID_CHAT_USER/_PASSWORD, weitere
    über FACTGRID_CHAT_USERS als "name:passwort,name2:passwort2". Ein Passwort darf
    ":" enthalten (nur das erste trennt), aber kein Komma."""
    users: dict[str, str] = {}
    user = os.environ.get("FACTGRID_CHAT_USER", "").strip()
    password = os.environ.get("FACTGRID_CHAT_PASSWORD", "")
    if user and password:
        users[user] = password
    for pair in os.environ.get("FACTGRID_CHAT_USERS", "").split(","):
        name, sep, secret = pair.strip().partition(":")
        if sep and name.strip() and secret.strip():
            users[name.strip()] = secret.strip()
    return users


CHAT_USERS = _load_users()

# ---------------------------------------------------------------------------
# Anmeldung per Sitzungscookie statt HTTP-Basic: Basic Auth kennt kein Abmelden
# (der Browser sendet die Zugangsdaten bis zum Schliessen weiter). Die Sitzungen
# liegen im Speicher - ein Neustart meldet alle ab, was hier vertretbar ist.
# ---------------------------------------------------------------------------
COOKIE = "fg_session"
SESSION_TTL = int(os.environ.get("FACTGRID_CHAT_SESSION_TTL", str(12 * 3600)))
LOGINS: dict[str, dict] = {}
# Ohne Anmeldung erreichbar. Dazu gehören die geteilten Gespräche: /s/<token> (die
# Oberfläche) und /api/shared (der Inhalt) - nur lesend, das Weiterführen unter
# /api/shared/continue verlangt wie alles andere eine Anmeldung.
OPEN_PATHS = {"/", "/api/login", "/api/me", "/favicon.ico", "/api/shared", "/api/shared/attachment"}
OPEN_PREFIXES = ("/s/",)


def _check_login(user: str, password: str) -> bool:
    ok = False
    # Immer alle Eintraege durchlaufen und compare_digest auf beiden Feldern:
    # kein vorzeitiger Ausstieg, der ueber die Laufzeit verraet, ob es den
    # Namen gibt.
    for name, secret in CHAT_USERS.items():
        ok |= secrets.compare_digest(user, name) & secrets.compare_digest(password, secret)
    return bool(ok)


def _current_user(request: Request) -> str | None:
    token = request.cookies.get(COOKIE, "")
    entry = LOGINS.get(token)
    if not entry:
        return None
    if entry["expires"] < time.time():
        LOGINS.pop(token, None)
        return None
    return entry["user"]


def _is_https(request: Request) -> bool:
    # Hinter dem nginx-proxy-manager kommt das echte Schema im Header an.
    return (request.headers.get("x-forwarded-proto", request.url.scheme) == "https")


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not CHAT_USERS:            # ohne konfigurierte Benutzer bleibt alles offen
        return await call_next(request)
    if request.url.path in OPEN_PATHS or request.url.path.startswith(OPEN_PREFIXES):
        return await call_next(request)
    if _current_user(request) is None:
        return JSONResponse({"error": "Nicht angemeldet."}, status_code=401)
    return await call_next(request)


def _any_key(user: str) -> bool:
    return any(key_for(user, p)[0] for p in CHAT_PROVIDERS)


@app.get("/api/me")
async def me(request: Request):
    limits = {"upload_max": UPLOAD_MAX, "upload_preview": UPLOAD_PREVIEW}
    if not CHAT_USERS:
        return {"auth": False, "user": None, "has_key": _any_key("_offen"), "model": CHAT_MODEL, **limits}
    user = _current_user(request)
    if user is None:
        return {"auth": True, "user": None, **limits}
    return {"auth": True, "user": user, "has_key": _any_key(user), "model": CHAT_MODEL, **limits}


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    user = str(body.get("user") or "").strip()
    password = str(body.get("password") or "")
    if not _check_login(user, password):
        return JSONResponse({"error": "Benutzername oder Passwort falsch."}, status_code=401)
    token = secrets.token_urlsafe(32)
    LOGINS[token] = {"user": user, "expires": time.time() + SESSION_TTL}
    res = JSONResponse({"ok": True, "user": user, "has_key": _any_key(user), "model": CHAT_MODEL})
    res.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True,
                   samesite="lax", secure=_is_https(request), path="/")
    return res


@app.post("/api/logout")
async def logout(request: Request):
    LOGINS.pop(request.cookies.get(COOKIE, ""), None)
    res = JSONResponse({"ok": True})
    res.delete_cookie(COOKIE, path="/")
    return res


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/models")
async def models(request: Request):
    """Nur die freigeschalteten Modelle (FACTGRID_CHAT_MODELS), je mit Schlüsselstatus."""
    user = (_current_user(request) if CHAT_USERS else "_offen") or ""
    infos = model_info(user) if user else [dict(m, has_key=False, key_source="") for m in model_info("")]
    return {"models": infos, "default": CHAT_MODEL,
            "has_key": any(m["has_key"] for m in infos), "errors": {}}


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


def _chat_user(request: Request) -> str:
    """Besitzer der Gespräche: der angemeldete Benutzer, ohne Anmeldung ein gemeinsames Konto."""
    return (_current_user(request) or "") if CHAT_USERS else "_offen"


@app.get("/api/chats")
async def chats(request: Request):
    """Seitenleiste: alle gespeicherten Gespräche des Benutzers, zuletzt benutzte zuerst."""
    return {"chats": list_chats(_chat_user(request))}


@app.get("/api/history")
async def get_history(request: Request, session: str = ""):
    """Verlauf eines Gesprächs im neutralen Format – die Oberfläche stellt ihn beim Öffnen wieder her."""
    chat = get_chat(_chat_user(request), session)
    return {"history": chat["messages"] if chat else [], "title": chat["title"] if chat else ""}


@app.get("/api/share")
async def share_link(request: Request, session: str = ""):
    """Permanenter Link des Gesprächs für den Teilen-Dialog. Das Token entsteht beim Speichern;
    für ältere Gespräche (und wenn noch keins da ist) wird es hier angelegt und nachgetragen –
    ohne Titel und Zeitstempel zu ändern, damit die Reihenfolge in der Seitenleiste bleibt."""
    user = _chat_user(request)
    chat = get_chat(user, session)
    if chat is None or not chat.get("messages"):
        return JSONResponse({"error": "Das Gespräch wird erst mit der ersten Frage gespeichert."},
                            status_code=404)
    if not SHARE_TOKEN.match(chat.get("share") or ""):
        ensure_share(user, chat)
        _write_chat(user, chat)
    return {"token": chat["share"], "path": f"/s/{chat['share']}", "title": chat.get("title", "")}


@app.get("/s/{token}")
async def shared_page(token: str):
    """Geteiltes Gespräch: dieselbe Oberfläche, die sich am Pfad /s/… als Nur-Lese-Ansicht
    erkennt. Nicht indexieren – der Link gehört dem, der ihn weitergibt, nicht einer Suchmaschine."""
    return FileResponse(Path(__file__).parent / "index.html",
                        headers={"X-Robots-Tag": "noindex, nofollow"})


@app.get("/api/shared")
async def shared_history(token: str = ""):
    """Inhalt eines geteilten Gesprächs – ohne Anmeldung, wer das Token hat. Ohne den Namen des
    Besitzers und ohne das Feld reasoning (das zeigt die Oberfläche auch im eigenen Verlauf nicht)."""
    hit = shared_chat(token)
    if hit is None:
        return JSONResponse({"error": "Dieser Link führt zu keinem Gespräch (mehr)."}, status_code=404)
    chat = hit[1]
    return {"token": token, "title": chat.get("title", ""), "created": chat.get("created", ""),
            "updated": chat.get("updated", ""),
            "messages": [{k: v for k, v in m.items() if k != "reasoning"}
                         for m in chat.get("messages", [])]}


def _send_attachment(user: str, chat: dict, file_id: str):
    """Die Textdatei eines Anhangs zum Herunterladen; nur Anhänge, die wirklich zu diesem
    Gespräch gehören (die ID muss im Verlauf stehen)."""
    f = next((x for x in chat_attachments(chat) if x.get("id") == file_id), None)
    path = attachment_file(user, chat["id"], file_id) if f else None
    if path is None:
        return JSONResponse({"error": "Diesen Anhang gibt es nicht (mehr)."}, status_code=404)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f["name"])


@app.get("/api/attachment")
async def attachment(request: Request, session: str = "", file: str = ""):
    """Anhang eines eigenen Gesprächs herunterladen (die Oberfläche hängt ihn an den Chip)."""
    user = _chat_user(request)
    chat = get_chat(user, session)
    if chat is None:
        return JSONResponse({"error": "Dieses Gespräch gibt es nicht."}, status_code=404)
    return _send_attachment(user, chat, file)


@app.get("/api/shared/attachment")
async def shared_attachment(token: str = "", file: str = ""):
    """Anhang eines geteilten Gesprächs – wer den Link hat, liest das Gespräch mit seinen Dateien."""
    hit = shared_chat(token)
    if hit is None:
        return JSONResponse({"error": "Dieser Link führt zu keinem Gespräch (mehr)."}, status_code=404)
    return _send_attachment(hit[0], hit[1], file)


@app.post("/api/shared/continue")
async def continue_shared(request: Request):
    """Geteiltes Gespräch als eigenes weiterführen: Kopie mit neuer ID und neuem Link. Das
    Original bleibt unberührt – zwei Leute können denselben Link unabhängig fortsetzen."""
    body = await request.json()
    hit = shared_chat(str(body.get("token") or ""))
    if hit is None:
        return JSONResponse({"error": "Dieser Link führt zu keinem Gespräch (mehr)."}, status_code=404)
    src = hit[1]
    user = _chat_user(request)
    chat = {"id": str(uuid.uuid4()), "title": src.get("title", ""), "created": _now(),
            "updated": _now(), "model": src.get("model", ""), "source": src.get("share", ""),
            "messages": copy.deepcopy(src.get("messages", []))}
    CHATS[(_owner(user), chat["id"])] = chat
    src_files, dst_files = _files_dir(hit[0], src["id"]), _files_dir(user, chat["id"])
    if src_files is not None and dst_files is not None and src_files.is_dir():
        shutil.copytree(src_files, dst_files, dirs_exist_ok=True)   # eigene Anhänge, eigenes Löschen
    save_chat(user, chat)                    # schreibt die Datei und legt das eigene Token an
    return {"ok": True, "session": chat["id"], "share": chat.get("share", "")}


@app.delete("/api/chats/{chat_id}")
async def remove_chat(request: Request, chat_id: str):
    return {"ok": True, "deleted": delete_chat(_chat_user(request), chat_id)}


@app.post("/api/upload")
async def upload(request: Request, name: str = "", part: str = ""):
    """Datei-Anhang für die nächste Frage. Der Datei-Inhalt IST der Request-Body (kein
    multipart - das spart eine Abhängigkeit und eine Parser-Angriffsfläche), der Dateiname
    steht in der Query. Zurück kommt der Steckbrief mit der ID, die /api/chat im Feld
    "files" erwartet. Der Anhang gehört dem hochladenden Benutzer; ein anderer kann mit
    der ID nichts anfangen (sie zeigt nur in seinen eigenen Ordner)."""
    user = _chat_user(request)
    if not user:
        return JSONResponse({"error": "Nicht angemeldet."}, status_code=401)
    limit_mb = max(1, UPLOAD_MAX // (1024 * 1024))
    if int(request.headers.get("content-length") or 0) > UPLOAD_MAX:
        return JSONResponse({"error": f"Die Datei ist zu groß (höchstens {limit_mb} MB)."}, status_code=413)
    data = await request.body()
    if not data:
        return JSONResponse({"error": "Die Datei ist leer."}, status_code=400)
    if len(data) > UPLOAD_MAX:
        return JSONResponse({"error": f"Die Datei ist zu groß (höchstens {limit_mb} MB)."}, status_code=413)
    text, why = extract_text(name, data)
    if why:
        return JSONResponse({"error": why}, status_code=415)
    # part=1: die Oberfläche hat eine zu große Datei am letzten Zeilenumbruch abgeschnitten
    # und nur den Anfang geschickt - das Modell erfährt es über attachment_text().
    return save_upload(user, name, len(data), text, part=part == "1")


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    question = (body.get("message") or "").strip()
    user = _chat_user(request)
    ids = [str(i) for i in (body.get("files") or [])][:UPLOAD_MAX_FILES]
    conv = get_chat(user, body.get("session") or "", create=True)
    # Anhänge dieses Benutzers (IDs aus /api/upload) in das Gespräch übernehmen;
    # unbekannte (abgelaufene) fallen weg und werden unten gemeldet.
    files = [f for f in (attach_upload(user, conv["id"], i) for i in ids) if f] if conv else []
    # Nur freigeschaltete Modelle; alles andere fällt auf die Vorauswahl zurück.
    model = str(body.get("model") or "").strip()
    if model not in CHAT_MODELS:
        model = CHAT_MODEL
    provider, model_id = split_model(model)
    backend, why = backend_for(user, provider)

    async def sse():
        if not question and not files:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Keine Frage übergeben.'})}\n\n"
            return
        if len(files) < len(ids):
            missing = {'type': 'error', 'message': 'Ein Anhang war nicht mehr da (zu alt?) und fehlt in der Frage.'}
            yield f"data: {json.dumps(missing, ensure_ascii=False)}\n\n"
        if conv is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Ungültige Gesprächs-ID.'})}\n\n"
            return
        if backend is None:
            yield f"data: {json.dumps({'type': 'error', 'message': why})}\n\n"
            return
        conv["model"] = model
        try:
            async for ev in run_turn(backend, model_id, user, conv, question, files):
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:500]})}\n\n"
        finally:                      # auch bei Abbruch durch den Browser sichern
            save_chat(user, conv)

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _provider_arg(value) -> str | None:
    """Anbieter aus Query/Body; nur freigeschaltete Anbieter, Default ist der der Vorauswahl."""
    p = str(value or "").strip() or CHAT_PROVIDER
    return p if p in CHAT_PROVIDERS and p in BACKEND_CLASSES else None


@app.get("/api/keys")
async def get_keys(request: Request, provider: str = ""):
    user = _current_user(request) if CHAT_USERS else "_offen"
    p = _provider_arg(provider)
    if p is None:
        return JSONResponse({"error": "Unbekannter Anbieter."}, status_code=400)
    return {"user": user, "provider": p, "provider_label": provider_label(p),
            "model": CHAT_MODEL, "has_key": bool(user_key(user, p)),
            "server_key": bool(server_key(p)), "key_var": SERVER_KEY_VARS.get(p, "")}


@app.post("/api/keys")
async def set_key(request: Request):
    user = _current_user(request) if CHAT_USERS else "_offen"
    body = await request.json()
    p = _provider_arg(body.get("provider"))
    if p is None:
        return JSONResponse({"error": "Unbekannter Anbieter."}, status_code=400)
    key = str(body.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "Kein Schlüssel übergeben."}, status_code=400)

    # Probelauf mit genau diesem Schlüssel, bevor er im Profil landet.
    try:
        probe = BACKEND_CLASSES[p](api_key=key)
        await probe.models()
    except Exception as e:
        return JSONResponse({"error": str(e)[:300], "has_key": bool(user_key(user, p))},
                            status_code=400)

    set_user_key(user, key, p)
    _BACKEND_CACHE.clear()
    return {"ok": True, "user": user, "provider": p, "has_key": True}


@app.delete("/api/keys")
async def delete_key(request: Request, provider: str = ""):
    user = _current_user(request) if CHAT_USERS else "_offen"
    p = _provider_arg(provider)
    if p is None:
        return JSONResponse({"error": "Unbekannter Anbieter."}, status_code=400)
    clear_user_key(user, p)
    _BACKEND_CACHE.clear()
    return {"ok": True, "user": user, "provider": p, "has_key": False}


def main() -> None:
    import uvicorn
    host = os.environ.get("FACTGRID_CHAT_HOST", "127.0.0.1")
    port = int(os.environ.get("FACTGRID_CHAT_PORT", "8177"))
    print(f"FactGrid-Chat: http://{host}:{port}  (Modelle: {', '.join(CHAT_MODELS)}, Vorauswahl: {CHAT_MODEL}, "
          f"Benutzer: {', '.join(CHAT_USERS) or 'KEINE - offen!'})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
