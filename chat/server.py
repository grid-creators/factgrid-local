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

Angemeldet wird sich mit dem FactGrid-Konto (MediaWiki-OAuth 1.0a, chat/mwoauth.py),
sobald FACTGRID_OAUTH_KEY/_SECRET gesetzt sind; ohne die beiden läuft der Chat offen,
aber nur auf 127.0.0.1. Profil (API-Schlüssel) und Gespräche hängen am FactGrid-Namen.

Jedes gespeicherte Gespräch hat einen permanenten Link /s/<token>, unter dem es ohne
Anmeldung gelesen (nicht geschrieben) werden kann; wer angemeldet ist, kann es über
/api/shared/continue als eigenes Gespräch mit neuer ID und neuem Link weiterführen.

Die Oberfläche bietet jede Tabelle und jeden ```tsv/```csv-Codeblock einer Antwort als
Datei zum Herunterladen an (im Browser aus dem Angezeigten gebaut, ohne Umweg über den
Server). Der umgekehrte Weg - Dateien an eine Frage hängen - ist bewusst nicht da.
"""
from __future__ import annotations

import copy
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

import secrets  # noqa: E402
from urllib.parse import quote  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastmcp import Client  # noqa: E402

from factgrid_mcp.server import mcp as SERVER  # noqa: E402
from mwoauth import MwOAuth, OAuthError  # noqa: E402  (chat/mwoauth.py)

SYSTEM = (ROOT / "CLAUDE.md").read_text(encoding="utf-8") if (ROOT / "CLAUDE.md").exists() else (
    "Du beantwortest Fragen zu FactGrid über die bereitgestellten Tools. Erst search_entities, "
    "dann get_entity, dann sparql. Immer LIMIT. Keine IDs raten."
)

# ---------------------------------------------------------------------------
# Neutrales Gesprächsformat (Sitzung → Provider-Format erst beim Request):
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
#   {"role": "tool", "id": str, "name": str, "content": str}
# Gespräche liegen in der Ablage unten (get_chat/save_chat), je Benutzer und Gespräch.
# ---------------------------------------------------------------------------
MCP_TOOLS: list = []  # von client.list_tools(), einmal beim Start


def user_content(m: dict) -> str:
    """Neutrale Benutzer-Nachricht → Text für den Provider. Alle vier Backends gehen hier
    durch, damit eine Frage überall gleich ankommt."""
    return m.get("content") or ""


OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "z-ai/glm-5.3-flash")


def tool_specs() -> list[dict]:
    """Die Werkzeuge für diesen Request: die Tools des MCP-Servers."""
    return [{"name": t.name, "description": (t.description or "")[:600],
             "schema": t.inputSchema or {"type": "object", "properties": {}}}
            for t in MCP_TOOLS]


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
# Token-Verbrauch. Jeder Anbieter zählt anders; für die Buchhaltung (.chat-usage.jsonl)
# werden die Zahlen auf eine gemeinsame Form gebracht:
#   input        alles, was in den Request ging, einschließlich der aus dem Cache
#                gelesenen Tokens. OpenAI und DeepSeek zählen sie ohnehin mit, bei
#                Anthropic stehen sie daneben und werden hier dazugerechnet - sonst
#                sähe ein gecachter Request nach fast keiner Eingabe aus.
#   cached       davon aus dem Prompt-Cache gelesen (billiger als frische Eingabe)
#   cache_write  in den Cache geschrieben (nur Anthropic, teurer als frische Eingabe)
#   output       erzeugte Tokens
# Ein Preis steht bewusst nicht dabei: Preislisten veralten, die Tokenzahlen nicht.
# ---------------------------------------------------------------------------
USAGE_FIELDS = ("input", "cached", "cache_write", "output")


def _num(obj, *names) -> int:
    """Erste vorhandene Zahl aus einem Usage-Objekt (SDK-Objekt oder dict)."""
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def zero_usage() -> dict:
    return dict.fromkeys(USAGE_FIELDS, 0)


def add_usage(total: dict, part: dict | None) -> dict:
    """Die Schritte eines Tool-Loops aufaddieren - jeder Schritt ist ein eigener Request."""
    for field in USAGE_FIELDS:
        total[field] += int((part or {}).get(field, 0) or 0)
    return total


def usage_responses(u) -> dict:
    """Responses-API (OpenAI, OpenRouter): input_tokens enthält die gecachten schon."""
    details = getattr(u, "input_tokens_details", None) or {}
    return {"input": _num(u, "input_tokens"), "cached": _num(details, "cached_tokens"),
            "cache_write": 0, "output": _num(u, "output_tokens")}


def usage_anthropic(u) -> dict:
    """Anthropic: input_tokens sind NUR die frischen - Cache-Lesen und -Schreiben zählen
    daneben und werden für `input` dazugerechnet."""
    cached, written = _num(u, "cache_read_input_tokens"), _num(u, "cache_creation_input_tokens")
    return {"input": _num(u, "input_tokens") + cached + written, "cached": cached,
            "cache_write": written, "output": _num(u, "output_tokens")}


def usage_chat(u) -> dict:
    """Chat-Completions (DeepSeek): prompt_tokens enthält Cache-Treffer und -Schreiben schon
    (prompt_cache_hit_tokens + prompt_cache_miss_tokens = prompt_tokens)."""
    details = getattr(u, "prompt_tokens_details", None) or {}
    return {"input": _num(u, "prompt_tokens"),
            "cached": _num(u, "prompt_cache_hit_tokens") or _num(details, "cached_tokens"),
            "cache_write": _num(details, "cache_write_tokens"),
            "output": _num(u, "completion_tokens")}


# ---------------------------------------------------------------------------
# Provider-Backends. step() ist ein Async-Generator über SSE-Events und liefert
# als letztes Event {"type": "_step_end", "msg": <nativ>, "text": str,
# "calls": [{"id","name","args"}], "usage": {...}}. Innerhalb eines Tool-Loops bleiben die
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
        text, items, usage, used = "", [], zero_usage(), ""
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
                usage = usage_responses(getattr(ev.response, "usage", None))
                used = getattr(ev.response, "model", "") or ""   # aufgelöster Schnappschuss
        # Die Output-Items (auch reasoning-Items) unverändert zurückgeben – der nächste
        # Request muss den kompletten Verlauf enthalten.
        native = [i.model_dump(exclude_none=True) if hasattr(i, "model_dump") else dict(i) for i in items]
        calls = [{"id": i["call_id"], "item_id": i.get("id"), "name": i["name"],
                  "args": _parse_args(i.get("arguments"))}
                 for i in native if i.get("type") == "function_call"]
        yield {"type": "_step_end", "msg": native, "text": text, "calls": calls,
               "usage": usage, "model_used": used}


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
               "text": text, "calls": calls, "usage": usage_anthropic(final.usage),
               "model_used": getattr(final, "model", "") or ""}


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
                    "stream_options": {"include_usage": True},   # sonst kein Schluss-Chunk mit usage
                    "max_tokens": self.max_tokens, "extra_body": {"thinking": {"type": self.thinking}}}
        if self.effort:
            kw["reasoning_effort"] = self.effort
        stream = await self.client.chat.completions.create(**kw)
        text, reasoning, usage, used = "", "", zero_usage(), ""
        calls: dict[int, dict] = {}  # index → {id, name, arguments}
        async for chunk in stream:
            # Die Tokenzahlen stehen im Schluss-Chunk - bei DeepSeek zusammen mit choices
            # (finish_reason), bei anderen OpenAI-kompatiblen APIs in einem eigenen Chunk
            # ohne choices. Darum hier und nicht erst im choices-losen Zweig; sie sind
            # kumulativ für die ganze Antwort, also setzen statt addieren.
            if getattr(chunk, "usage", None):
                usage = usage_chat(chunk.usage)
                used = getattr(chunk, "model", "") or used
            if not getattr(chunk, "choices", None):
                continue
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
               "usage": usage, "model_used": used,
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
# Buchhaltung: je Antwort eine Zeile in .chat-usage.jsonl (FACTGRID_CHAT_USAGE) - wer hat
# mit welchem Modell wie viele Tokens verbraucht und auf wessen Schlüssel (Profil oder
# Server). Eine Zeile je Antwort statt fortlaufender Summen, damit sich hinterher jeder
# Zeitraum, jedes Modell und jede Person einzeln auszählen lässt. Fragen und Antworten
# stehen nicht darin - die liegen im Gespräch. Ausgewertet mit `make usage`
# (scripts/chat_usage.py).
# ---------------------------------------------------------------------------
USAGE_PATH = Path(os.environ.get("FACTGRID_CHAT_USAGE", str(ROOT / ".chat-usage.jsonl")))


def record_usage(user: str, model: str, chat_id: str, usage: dict, steps: int,
                 seconds: float, key_source: str, model_api: str = "") -> None:
    """Eine Zeile anhängen. Die Buchhaltung darf nie eine Antwort kaputtmachen: schlägt das
    Schreiben fehl, steht das im Journal des Dienstes und sonst passiert nichts."""
    line = {"ts": datetime.now().isoformat(timespec="seconds"), "user": user, "model": model,
            **{field: int(usage.get(field, 0)) for field in USAGE_FIELDS},
            "steps": steps, "seconds": seconds, "chat": chat_id, "key": key_source}
    # Was der Anbieter tatsächlich bedient hat - nur wenn es vom angefragten Namen abweicht
    # (OpenAI löst einen Alias in einen datierten Schnappschuss auf, OpenRouter kann umleiten).
    if model_api and model_api != model.split("/", 1)[-1]:
        line["model_api"] = model_api
    try:
        neu = not USAGE_PATH.exists()
        with open(USAGE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        if neu:
            os.chmod(USAGE_PATH, 0o600)     # wer was verbraucht, geht sonst niemanden an
    except OSError as e:
        print(f"Verbrauch nicht notiert ({USAGE_PATH}): {e}", file=sys.stderr)


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


async def run_turn(backend, model: str, user: str, chat: dict, question: str):
    """Async-Generator über SSE-Events; hängt das Ergebnis an den Verlauf des Gesprächs an."""
    history = chat["messages"]
    history.append({"role": "user", "content": question})
    specs = tool_specs()
    msgs = backend.history(history)      # die neue Frage ist darin schon enthalten
    log = {"ts": datetime.now().isoformat(timespec="seconds"), "model": f"{backend.name}/{model}",
           "question": question, "calls": [], "ui": "chat"}
    t0 = time.time()
    answer, usage, steps, model_api = "", zero_usage(), 0, ""
    for _ in range(MAX_STEPS):
        end = None
        async for ev in backend.step(model, msgs, specs):
            if ev["type"] == "_step_end":
                end = ev
            else:
                yield ev
        add_usage(usage, end.get("usage"))    # jeder Schritt ist ein eigener Request
        model_api = end.get("model_used") or model_api
        steps += 1
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
    record_usage(user, f"{backend.name}/{model}", chat["id"], usage, steps, log["seconds"],
                 key_for(user, backend.name)[1], model_api)
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
# Anmeldung über FactGrid (MediaWiki-OAuth 1.0a, Ablauf in chat/mwoauth.py). Aktiv,
# sobald FACTGRID_OAUTH_KEY und FACTGRID_OAUTH_SECRET gesetzt sind - noetig, weil
# der Dienst hinter dem nginx-proxy-manager oeffentlich steht: jede Frage kostet
# API-Guthaben, und ueber /api/keys liesse sich sonst der hinterlegte Schluessel
# ersetzen oder loeschen. Ohne die beiden Variablen (rein lokaler Betrieb) bleibt
# alles offen wie bisher.
#
# Herein kommt, wer ein FactGrid-Konto hat; gesperrte Konten nicht. Profil
# (API-Schluessel) und Gespraeche haengen am FactGrid-Benutzernamen - wer dort
# umbenannt wird, faengt hier neu an.
# ---------------------------------------------------------------------------
FACTGRID_BASE = os.environ.get("FACTGRID_BASE", "https://database.factgrid.de/").rstrip("/")
OAUTH = MwOAuth(
    key=os.environ.get("FACTGRID_OAUTH_KEY", ""),
    secret=os.environ.get("FACTGRID_OAUTH_SECRET", ""),
    index_url=os.environ.get("FACTGRID_OAUTH_URL", "") or f"{FACTGRID_BASE}/w/index.php",
    # "oob" = die beim Consumer eingetragene Rueckruf-Adresse nehmen; das Wiki haengt
    # oauth_token und oauth_verifier an und schickt den Browser dorthin. Eine eigene
    # Adresse nimmt es nur von einem Consumer mit "callback is prefix".
    callback=os.environ.get("FACTGRID_OAUTH_CALLBACK", "").strip() or "oob",
)

# ---------------------------------------------------------------------------
# Sitzungscookie: die Anmeldung beim Wiki gilt hier, bis das Cookie ablaeuft. Die
# Sitzungen liegen im Speicher - ein Neustart meldet alle ab, was hier vertretbar
# ist. Der Zugriffs-Token von FactGrid wird nach dem identify nicht aufgehoben:
# der Chat schreibt nichts im Wiki, er will nur den Namen.
# ---------------------------------------------------------------------------
COOKIE = "fg_session"
SESSION_TTL = int(os.environ.get("FACTGRID_CHAT_SESSION_TTL", str(12 * 3600)))
LOGIN_TTL = 600                  # so lange darf das Bestaetigen im Wiki dauern
LOGINS: dict[str, dict] = {}
PENDING: dict[str, dict] = {}    # Anfrage-Token -> {secret, expires, next}
# Ohne Anmeldung erreichbar. Dazu gehören die geteilten Gespräche: /s/<token> (die
# Oberfläche) und /api/shared (der Inhalt) - nur lesend, das Weiterführen unter
# /api/shared/continue verlangt wie alles andere eine Anmeldung.
OPEN_PATHS = {"/", "/api/oauth/login", "/api/oauth/callback", "/api/me", "/favicon.ico",
              "/api/shared"}
OPEN_PREFIXES = ("/s/",)


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
    if not OAUTH.enabled:         # ohne eingerichtetes OAuth bleibt alles offen
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
    if not OAUTH.enabled:
        return {"auth": False, "user": None, "has_key": _any_key("_offen"), "model": CHAT_MODEL}
    user = _current_user(request)
    if user is None:
        return {"auth": True, "user": None, "wiki": OAUTH.wiki}
    return {"auth": True, "user": user, "wiki": OAUTH.wiki, "has_key": _any_key(user),
            "model": CHAT_MODEL}


def _safe_next(target: str) -> str:
    """Nur Pfade auf diesem Server - sonst waere /api/oauth/login eine Weiterleitung
    auf beliebige fremde Adressen."""
    return target if re.match(r"^/(?!/)[^\\]*$", target or "") else "/"


def _login_error(message: str):
    """Zurueck zur Oberflaeche, die die Meldung im Anmeldedialog zeigt."""
    return RedirectResponse("/?fehler=" + quote(message[:300]), status_code=303)


def _drop_stale() -> None:
    now = time.time()
    for token in [t for t, e in PENDING.items() if e["expires"] < now]:
        PENDING.pop(token, None)


@app.get("/api/oauth/login")
async def oauth_login(request: Request):
    """Schritt 1: Anfrage-Token holen und den Browser zur Bestaetigung ins Wiki schicken."""
    if not OAUTH.enabled:
        return JSONResponse({"error": "FactGrid-Anmeldung ist nicht eingerichtet."}, status_code=503)
    try:
        token, secret = await OAUTH.initiate()
    except OAuthError as e:
        return _login_error(str(e))
    _drop_stale()
    PENDING[token] = {"secret": secret, "expires": time.time() + LOGIN_TTL,
                      "next": _safe_next(request.query_params.get("next", "/"))}
    return RedirectResponse(OAUTH.authorize_url(token), status_code=303)


@app.get("/api/oauth/callback")
async def oauth_callback(request: Request):
    """Schritt 2: Verifier gegen den Zugriffs-Token tauschen, Identitaet pruefen, anmelden."""
    _drop_stale()
    entry = PENDING.pop(request.query_params.get("oauth_token", ""), None)
    if entry is None:
        return _login_error("Die Anmeldung ist abgelaufen oder gehoert nicht hierher - bitte noch einmal.")
    verifier = request.query_params.get("oauth_verifier", "")
    if not verifier:
        return _login_error("Die Anmeldung wurde in FactGrid abgebrochen.")
    try:
        token, secret = await OAUTH.access_token(request.query_params["oauth_token"],
                                                 entry["secret"], verifier)
        claims = await OAUTH.identify(token, secret)
    except OAuthError as e:
        return _login_error(str(e))
    user = str(claims.get("username") or "").strip()
    if not user:
        return _login_error(f"{OAUTH.wiki} hat keinen Benutzernamen geliefert.")
    if claims.get("blocked"):
        return _login_error(f"Das FactGrid-Konto {user} ist gesperrt.")
    session = secrets.token_urlsafe(32)
    LOGINS[session] = {"user": user, "expires": time.time() + SESSION_TTL}
    res = RedirectResponse(entry["next"], status_code=303)
    res.set_cookie(COOKIE, session, max_age=SESSION_TTL, httponly=True,
                   samesite="lax", secure=_is_https(request), path="/")
    return res


@app.post("/api/logout")
async def logout(request: Request):
    """Nur hier abmelden - die Anmeldung im Wiki selbst bleibt davon unberuehrt."""
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
    user = (_current_user(request) if OAUTH.enabled else "_offen") or ""
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
    return (_current_user(request) or "") if OAUTH.enabled else "_offen"


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
    save_chat(user, chat)                    # schreibt die Datei und legt das eigene Token an
    return {"ok": True, "session": chat["id"], "share": chat.get("share", "")}


@app.delete("/api/chats/{chat_id}")
async def remove_chat(request: Request, chat_id: str):
    return {"ok": True, "deleted": delete_chat(_chat_user(request), chat_id)}


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    question = (body.get("message") or "").strip()
    user = _chat_user(request)
    conv = get_chat(user, body.get("session") or "", create=True)
    # Nur freigeschaltete Modelle; alles andere fällt auf die Vorauswahl zurück.
    model = str(body.get("model") or "").strip()
    if model not in CHAT_MODELS:
        model = CHAT_MODEL
    provider, model_id = split_model(model)
    backend, why = backend_for(user, provider)

    async def sse():
        if not question:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Keine Frage übergeben.'})}\n\n"
            return
        if conv is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Ungültige Gesprächs-ID.'})}\n\n"
            return
        if backend is None:
            yield f"data: {json.dumps({'type': 'error', 'message': why})}\n\n"
            return
        conv["model"] = model
        try:
            async for ev in run_turn(backend, model_id, user, conv, question):
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
    user = _current_user(request) if OAUTH.enabled else "_offen"
    p = _provider_arg(provider)
    if p is None:
        return JSONResponse({"error": "Unbekannter Anbieter."}, status_code=400)
    return {"user": user, "provider": p, "provider_label": provider_label(p),
            "model": CHAT_MODEL, "has_key": bool(user_key(user, p)),
            "server_key": bool(server_key(p)), "key_var": SERVER_KEY_VARS.get(p, "")}


@app.post("/api/keys")
async def set_key(request: Request):
    user = _current_user(request) if OAUTH.enabled else "_offen"
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
    user = _current_user(request) if OAUTH.enabled else "_offen"
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
    # Ohne eingerichtetes OAuth ist der Chat offen. Am Arbeitsplatz ist das gewollt; auf einer
    # Adresse, die andere erreichen, waere es ein offenes Scheunentor (jede Frage kostet
    # API-Guthaben) - dann lieber gar nicht erst starten.
    if OAUTH.enabled and OAUTH.callback != "oob" \
            and not OAUTH.callback.startswith(("http://", "https://")):
        sys.exit(f"FACTGRID_OAUTH_CALLBACK={OAUTH.callback!r} ist keine vollstaendige Adresse. "
                 f"Leer lassen (dann gilt die beim Consumer eingetragene Rueckruf-Adresse) oder "
                 f"die volle https-Adresse eintragen - letzteres nur, wenn der Consumer mit "
                 f"'callback is prefix' angelegt wurde.")
    if not OAUTH.enabled and host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit(f"FACTGRID_CHAT_HOST={host} ist auch von aussen erreichbar, aber die "
                 f"FactGrid-Anmeldung ist nicht eingerichtet. Entweder FACTGRID_OAUTH_KEY und "
                 f"FACTGRID_OAUTH_SECRET in .env setzen (Consumer anlegen unter "
                 f"{FACTGRID_BASE}/wiki/Special:OAuthConsumerRegistration/propose) oder "
                 f"FACTGRID_CHAT_HOST=127.0.0.1 setzen.")
    print(f"FactGrid-Chat: http://{host}:{port}  (Modelle: {', '.join(CHAT_MODELS)}, Vorauswahl: {CHAT_MODEL}, "
          f"Anmeldung: {'FactGrid-OAuth über ' + OAUTH.wiki if OAUTH.enabled else 'KEINE - offen!'})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
