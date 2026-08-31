#!/usr/bin/env python3
"""
mini_agent.py – Plan B zu Claude Code: ein sehr schlanker Tool-Loop, der ein Modell über eine
Cloud-API direkt mit dem factgrid-mcp-Server verbindet. Der System-Prompt ist ~1 k Token statt
der ~20 k Token von Claude Code – das spart pro Frage Geld und Latenz.

    uv run --with openai --with anthropic python agent/mini_agent.py            # interaktiv
    ... python agent/mini_agent.py -q "Wie viele Menschen sind in FactGrid?"
    ... python agent/mini_agent.py -m anthropic/claude-opus-5                   # anderer Anbieter

Zwei Anbieter, aus der Modell-ID abgeleitet:
  * OpenRouter (Default, Responses-API POST /api/v1/responses) – jede ID mit "/", z. B.
    z-ai/glm-5.3-flash; Schlüssel aus OPENROUTER_API_KEY, Modell aus OPENROUTER_MODEL.
  * Anthropic (Messages-API) – IDs ohne "/", z. B. claude-opus-5, oder mit Präfix "anthropic/".
    Schlüssel aus ANTHROPIC_API_KEY oder einem Profil von `ant auth login`.

Der MCP-Server läuft in-process (fastmcp In-Memory-Transport); QLEVER_ENDPOINT usw. gelten wie in
.mcp.json, Schlüssel auch aus .env. Jeder Lauf wird als JSONL nach eval/runs/ protokolliert
(Frage, Tool-Aufrufe, SPARQL, Antwort).

Achtung: Fragen und Query-Ergebnisse verlassen den Rechner – nur der QLever-Index bleibt lokal.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from fastmcp import Client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from factgrid_mcp.envfile import load_env  # noqa: E402

load_env(ROOT / ".env")

from factgrid_mcp.server import mcp as SERVER  # noqa: E402  (In-Process-Transport, kein Subprozess)

OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "z-ai/glm-5.3-flash")
MODEL = os.environ.get("AGENT_MODEL", OPENROUTER_MODEL)
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))
TOOL_RESULT_LIMIT = 12000  # Zeichen pro Tool-Ergebnis im Kontext
RUNS = ROOT / "eval" / "runs"

SYSTEM = (ROOT / "CLAUDE.md").read_text(encoding="utf-8") if (ROOT / "CLAUDE.md").exists() else (
    "Du beantwortest Fragen zu FactGrid über die bereitgestellten Tools. Erst search_entities, "
    "dann get_entity, dann sparql. Immer LIMIT. Keine IDs raten."
)


def _parse_args(args) -> dict:
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return {"query": args}
    return dict(args or {})


# --------------------------------------------------------------------------- #
# Anbieter. step() liefert (native_items, text, calls) – native_items wird unverändert
# an den Verlauf gehängt (Anthropic braucht seine thinking-Blöcke, OpenRouter seine
# reasoning-Items unverändert zurück).
# --------------------------------------------------------------------------- #
class OpenRouterRunner:
    """Responses-API. Strikt zustandslos: `store`/`previous_response_id` werden mit 400
    abgelehnt, der komplette Verlauf geht deshalb in jedem Request mit."""

    name = "openrouter"

    def __init__(self):
        import openai
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("OPENROUTER_API_KEY fehlt – in .env eintragen (s. .env.example).")
        headers = {"X-Title": os.environ.get("OPENROUTER_SITE_NAME", "FactGrid lokal")}
        if os.environ.get("OPENROUTER_SITE_URL"):  # HTTP-Referer ist optional (Rankings)
            headers["HTTP-Referer"] = os.environ["OPENROUTER_SITE_URL"]
        self.client = openai.AsyncOpenAI(base_url=OPENROUTER_BASE_URL,
                                         api_key=os.environ["OPENROUTER_API_KEY"],
                                         default_headers=headers)

    def tools(self, mcp_tools) -> list[dict]:
        return [{"type": "function", "name": t.name, "description": (t.description or "")[:600],
                 "parameters": t.inputSchema or {"type": "object", "properties": {}}}
                for t in mcp_tools]

    def question(self, question: str) -> list[dict]:
        return [{"role": "user", "content": question}]

    def tool_results(self, results: list[dict]) -> list[dict]:
        return [{"type": "function_call_output", "call_id": r["id"], "output": r["content"]}
                for r in results]

    async def step(self, model: str, msgs: list[dict], tools: list[dict], on_text) -> tuple:
        text, items = "", []
        stream = await self.client.responses.create(
            model=model, input=msgs, instructions=SYSTEM, tools=tools, stream=True, store=False,
        )
        async for ev in stream:
            kind = getattr(ev, "type", "")
            if kind == "response.output_text.delta":
                text += ev.delta
                on_text(ev.delta)
            elif kind == "response.output_item.done":
                items.append(ev.item)
            elif kind == "response.completed":
                items = list(ev.response.output)  # vollständige Liste, schlägt die Einzel-Items
        native = [i.model_dump(exclude_none=True) if hasattr(i, "model_dump") else dict(i) for i in items]
        calls = [{"id": i["call_id"], "name": i["name"], "args": _parse_args(i.get("arguments"))}
                 for i in native if i.get("type") == "function_call"]
        return native, text, calls


class AnthropicRunner:
    name = "anthropic"

    def __init__(self):
        import anthropic
        self.client = anthropic.AsyncAnthropic()

    def tools(self, mcp_tools) -> list[dict]:
        return [{"name": t.name, "description": (t.description or "")[:600],
                 "input_schema": t.inputSchema or {"type": "object", "properties": {}}}
                for t in mcp_tools]

    def question(self, question: str) -> list[dict]:
        return [{"role": "user", "content": question}]

    def tool_results(self, results: list[dict]) -> list[dict]:
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in results]}]

    async def step(self, model: str, msgs: list[dict], tools: list[dict], on_text) -> tuple:
        # Kein temperature/thinking-Parameter (auf Opus 5 & Sonnet 5 entfernt bzw. per Default
        # adaptiv). System-Prompt mit Cache-Breakpoint: er ist über alle Schritte und alle Fragen
        # identisch und wird so nur einmal voll bezahlt.
        async with self.client.messages.stream(
            model=model, max_tokens=int(os.environ.get("ANTHROPIC_MAX_TOKENS", "64000")),
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=tools, messages=msgs,
        ) as stream:
            async for delta in stream.text_stream:
                on_text(delta)
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            why = getattr(final.stop_details, "explanation", None) if final.stop_details else None
            return [], f"(Anfrage vom Modell abgelehnt) {why or ''}".strip(), []
        text = "".join(b.text for b in final.content if b.type == "text")
        calls = [{"id": b.id, "name": b.name, "args": dict(b.input)}
                 for b in final.content if b.type == "tool_use"]
        return [{"role": "assistant", "content": final.content}], text, calls


_RUNNERS: dict[str, object] = {}  # ein Client pro Anbieter, auch über mehrere Fragen


def runner_for(model: str) -> tuple:
    """Anbieter aus der Modell-ID ableiten: OpenRouter-IDs haben ein '/', Claude-IDs nicht.
    'anthropic/claude-opus-5' geht ausdrücklich an Anthropic, nicht an OpenRouter."""
    if model.startswith("anthropic/"):
        name, model = "anthropic", model.split("/", 1)[1]
    else:
        name = "openrouter" if "/" in model else "anthropic"
    if name not in _RUNNERS:
        _RUNNERS[name] = OpenRouterRunner() if name == "openrouter" else AnthropicRunner()
    return _RUNNERS[name], model


async def answer(client: Client, question: str, model: str, verbose: bool = True) -> dict:
    runner, model_id = runner_for(model)
    tools = runner.tools(await client.list_tools())
    msgs = runner.question(question)
    log = {"ts": datetime.now().isoformat(timespec="seconds"), "model": f"{runner.name}/{model_id}",
           "question": question, "calls": []}
    t0 = time.time()

    def on_text(delta: str) -> None:
        if verbose:
            print(delta, end="", flush=True)

    for _ in range(MAX_STEPS):
        native, text, calls = await runner.step(model_id, msgs, tools, on_text)
        msgs.extend(native)
        if not calls:
            log["answer"] = text
            break
        results = []
        for call in calls:
            if verbose:
                print(f"\n→ {call['name']}({json.dumps(call['args'], ensure_ascii=False)[:300]})", file=sys.stderr)
            try:
                res = await client.call_tool(call["name"], call["args"])
                out = "\n".join(getattr(c, "text", "") for c in res.content) if hasattr(res, "content") else str(res)
            except Exception as e:  # Tool-Fehler zurück ans Modell, nicht abbrechen
                out = f"FEHLER: {e}"
            out = out[:TOOL_RESULT_LIMIT]
            if verbose:
                print(out[:1200] + ("…" if len(out) > 1200 else ""), file=sys.stderr)
            log["calls"].append({"tool": call["name"], "args": call["args"], "result_head": out[:2000]})
            results.append({"id": call["id"], "name": call["name"], "content": out})
        msgs.extend(runner.tool_results(results))  # alle Ergebnisse EINER Runde zusammen
    else:
        log["answer"] = "(Abbruch: zu viele Schritte)"
    log["seconds"] = round(time.time() - t0, 1)
    RUNS.mkdir(parents=True, exist_ok=True)
    with open(RUNS / f"{datetime.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(log, ensure_ascii=False, default=str) + "\n")
    return log


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--question")
    ap.add_argument("-m", "--model", default=MODEL,
                    help=f"Modell-ID, Default {MODEL} (OPENROUTER_MODEL bzw. AGENT_MODEL)")
    a = ap.parse_args()
    async with Client(SERVER) as client:
        if a.question:
            await answer(client, a.question, a.model)  # Antwort wird beim Streamen ausgegeben
            print()
            return
        print(f"FactGrid lokal – Modell {a.model}. Frage eingeben, leer = Ende.")
        while True:
            try:
                q = input("\n? ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            log = await answer(client, q, a.model)
            print(f"\n[{log['seconds']} s, {len(log['calls'])} Tool-Aufrufe]")


if __name__ == "__main__":
    asyncio.run(main())
