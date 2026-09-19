"""
Tests für die Buchhaltung des Web-Chats: die Tokenzahlen der vier Anbieter auf eine
gemeinsame Form bringen (chat/server.py), je Antwort eine Zeile in .chat-usage.jsonl
schreiben – auch über einen Tool-Loop mit mehreren Requests hinweg – und das Auszählen
mit scripts/chat_usage.py. Ohne Netz, ohne echte API-Aufrufe.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "chat"))
sys.path.insert(0, str(ROOT / "scripts"))

# Testumgebung VOR dem Import festlegen (load_env überschreibt gesetzte Variablen nicht)
_TMP = tempfile.mkdtemp()
os.environ.update({
    "QLEVER_ENDPOINT": "http://127.0.0.1:1",
    "FACTGRID_CACHE_DIR": os.path.join(_TMP, "cache"),
    "FACTGRID_CHAT_PROFILES": os.path.join(_TMP, "profiles.json"),
    "FACTGRID_CHAT_HISTORY": os.path.join(_TMP, "history"),
    "FACTGRID_CHAT_USAGE": os.path.join(_TMP, "usage.jsonl"),
    "FACTGRID_CHAT_MODEL": "openai/gpt-5.6-luna",
    "FACTGRID_CHAT_MODELS": "openai/gpt-5.6-luna",
    "FACTGRID_OAUTH_KEY": "", "FACTGRID_OAUTH_SECRET": "",
    "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "ANTHROPIC_API_KEY": "",
    "OPENROUTER_API_KEY": "", "MW_DB_PASSWORD": "",
})

import chat_usage  # noqa: E402  (scripts/chat_usage.py)
import server  # noqa: E402  (chat/server.py)

server.RUNS = Path(_TMP) / "runs"          # nicht ins echte eval/runs schreiben
USER = "Olaf Simons"


# ---------------------------------------------------------------------------
# Die Zahlen der Anbieter auf eine Form bringen
# ---------------------------------------------------------------------------
def test_responses_usage_counts_cached_tokens_as_part_of_the_input():
    """OpenAI/OpenRouter: input_tokens enthält die gecachten schon."""
    u = NS(input_tokens=48213, output_tokens=1877,
           input_tokens_details=NS(cached_tokens=31002))
    assert server.usage_responses(u) == {"input": 48213, "cached": 31002,
                                         "cache_write": 0, "output": 1877}
    # Ohne usage (abgebrochener Stream) darf nichts krachen
    assert server.usage_responses(None) == server.zero_usage()


def test_anthropic_usage_adds_the_cache_to_the_input():
    """Anthropic zählt Cache-Lesen und -Schreiben NEBEN input_tokens – sonst sähe ein
    gecachter Request nach fast keiner Eingabe aus."""
    u = NS(input_tokens=1200, output_tokens=3000,
           cache_read_input_tokens=90000, cache_creation_input_tokens=12000)
    assert server.usage_anthropic(u) == {"input": 103200, "cached": 90000,
                                         "cache_write": 12000, "output": 3000}


def test_chat_completions_usage():
    """DeepSeek, Form wie im echten Schluss-Chunk: prompt_tokens enthält Cache-Treffer und
    -Schreiben schon (prompt_cache_hit_tokens + prompt_cache_miss_tokens = prompt_tokens)."""
    u = NS(prompt_tokens=1500, completion_tokens=3, total_tokens=1503,
           prompt_cache_hit_tokens=1280, prompt_cache_miss_tokens=220,
           prompt_tokens_details=NS(cached_tokens=1280, cache_write_tokens=None))
    assert server.usage_chat(u) == {"input": 1500, "cached": 1280, "cache_write": 0, "output": 3}
    # Wird in den Cache geschrieben, steht das in prompt_tokens_details
    u2 = NS(prompt_tokens=9000, completion_tokens=400, prompt_cache_hit_tokens=2000,
            prompt_tokens_details=NS(cached_tokens=2000, cache_write_tokens=6500))
    assert server.usage_chat(u2) == {"input": 9000, "cached": 2000,
                                     "cache_write": 6500, "output": 400}
    assert server.usage_chat({"prompt_tokens": 5, "completion_tokens": 1}) == {
        "input": 5, "cached": 0, "cache_write": 0, "output": 1}


def test_add_usage_sums_the_steps():
    total = server.zero_usage()
    server.add_usage(total, {"input": 1000, "cached": 800, "cache_write": 0, "output": 20})
    server.add_usage(total, {"input": 1500, "output": 60})          # fehlende Felder = 0
    server.add_usage(total, None)                                   # Schritt ohne Zahlen
    assert total == {"input": 2500, "cached": 800, "cache_write": 0, "output": 80}


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)


def _deepseek_lauf(chunks) -> tuple[dict, dict]:
    b = server.DeepSeekBackend(api_key="x")
    seen: dict = {}

    async def fake_create(**kw):
        seen.update(kw)
        return Stream(list(chunks))

    b.client = NS(chat=NS(completions=NS(create=fake_create)))
    server.MCP_TOOLS = [NS(name="sparql", description="d",
                           inputSchema={"type": "object", "properties": {}})]

    async def run():
        return [ev async for ev in b.step("deepseek-flash", [{"role": "user", "content": "hi"}],
                                          server.tool_specs())]

    return asyncio.run(run())[-1], seen


def _text_chunk(text, finish=None, usage=None, model=None):
    chunk = NS(choices=[NS(delta=NS(content=text, reasoning_content=None, tool_calls=None),
                           finish_reason=finish)])
    if usage is not None:
        chunk.usage, chunk.model = usage, model
    return chunk


def test_deepseek_step_reads_usage_from_the_chunk_that_carries_it():
    """DeepSeek hängt usage an den LETZTEN Chunk - der hat noch choices (finish_reason).
    Wer nur choices-lose Chunks anschaut, notiert für DeepSeek ewig Nullen."""
    echt = NS(prompt_tokens=1500, completion_tokens=3, total_tokens=1503,
              prompt_cache_hit_tokens=1280, prompt_cache_miss_tokens=220,
              prompt_tokens_details=NS(cached_tokens=1280, cache_write_tokens=None))
    end, seen = _deepseek_lauf([
        _text_chunk("Hal"), _text_chunk("lo"),
        _text_chunk("", finish="stop", usage=echt, model="deepseek-flash"),
    ])
    assert seen["stream_options"] == {"include_usage": True}   # sonst kommt gar keine usage
    assert end["text"] == "Hallo"
    assert end["usage"] == {"input": 1500, "cached": 1280, "cache_write": 0, "output": 3}
    assert end["model_used"] == "deepseek-flash"


def test_deepseek_step_also_reads_a_separate_usage_chunk():
    """Andere OpenAI-kompatible APIs schicken sie in einem eigenen Chunk ohne choices."""
    end, _ = _deepseek_lauf([
        _text_chunk("Fünf."),
        NS(choices=[], model="deepseek-flash",
           usage=NS(prompt_tokens=9000, completion_tokens=400, prompt_cache_hit_tokens=2000)),
    ])
    assert end["usage"] == {"input": 9000, "cached": 2000, "cache_write": 0, "output": 400}


# ---------------------------------------------------------------------------
# Eine Zeile je Antwort
# ---------------------------------------------------------------------------
class FakeBackend:
    """Ein Tool-Loop aus vorgegebenen Schritten – jeder Schritt ist ein eigener Request
    mit eigenen Tokenzahlen."""

    name = "openai"

    def __init__(self, schritte):
        self.schritte = list(schritte)

    def history(self, history):
        return [{"role": m["role"], "content": m.get("content", "")} for m in history]

    def tool_results(self, results):
        return [{"role": "tool", "content": r["content"]} for r in results]

    async def step(self, model, msgs, specs):
        schritt = self.schritte.pop(0)
        if schritt["text"]:
            yield {"type": "token", "text": schritt["text"]}
        yield {"type": "_step_end", "msg": {"role": "assistant", "content": schritt["text"]},
               "text": schritt["text"], "calls": schritt["calls"], "usage": schritt["usage"],
               "model_used": schritt.get("model_used", "")}


def _zwei_schritte() -> FakeBackend:
    return FakeBackend([
        {"text": "", "calls": [{"id": "c1", "name": "sparql", "args": {"query": "SELECT 1"}}],
         "usage": {"input": 1000, "cached": 800, "cache_write": 0, "output": 20}},
        {"text": "Fünf (Q1).", "calls": [],
         "usage": {"input": 1500, "cached": 1200, "cache_write": 0, "output": 60}},
    ])


def _lauf(backend, chat_id: str, frage: str = "Wie viele?") -> list[dict]:
    server.MCP_CLIENT = NS(call_tool=_fake_tool)
    server.MCP_TOOLS = [NS(name="sparql", description="d",
                           inputSchema={"type": "object", "properties": {}})]
    chat = server.get_chat(USER, chat_id, create=True)

    async def run():
        return [ev async for ev in server.run_turn(backend, "gpt-5.6-luna", USER, chat, frage)]

    return asyncio.run(run())


async def _fake_tool(name, args):
    return NS(content=[NS(text="anzahl\n5")])


def _zeilen() -> list[dict]:
    if not server.USAGE_PATH.exists():
        return []
    return [json.loads(z) for z in server.USAGE_PATH.read_text(encoding="utf-8").splitlines() if z.strip()]


def test_one_line_per_answer_sums_the_whole_tool_loop():
    cid = "aaaa1111-2222-4333-8444-555555555555"
    server.USAGE_PATH = Path(_TMP) / "usage.jsonl"
    server.USAGE_PATH.unlink(missing_ok=True)
    server.set_user_key(USER, "sk-eigener", "openai")
    try:
        events = _lauf(_zwei_schritte(), cid)
    finally:
        server.clear_user_key(USER, "openai")
    assert events[-1]["type"] == "done"

    zeilen = _zeilen()
    assert len(zeilen) == 1
    z = zeilen[0]
    assert z["user"] == USER and z["model"] == "openai/gpt-5.6-luna" and z["chat"] == cid
    assert z["steps"] == 2                                  # zwei Requests im Tool-Loop
    assert (z["input"], z["cached"], z["cache_write"], z["output"]) == (2500, 2000, 0, 80)
    assert z["key"] == "profil"                             # auf eigenen Schlüssel gegangen
    assert isinstance(z["seconds"], float) and z["ts"][:4].isdigit()
    # In der Buchhaltung steht nichts vom Inhalt - der liegt im Gespräch
    assert "question" not in z and "answer" not in z and "Wie viele" not in json.dumps(z)
    assert oct(server.USAGE_PATH.stat().st_mode)[-3:] == "600"

    _lauf(_zwei_schritte(), cid, "Und Sechs?")              # zweite Antwort, zweite Zeile
    assert len(_zeilen()) == 2
    server.delete_chat(USER, cid)


def test_the_model_the_api_really_served_is_noted_when_it_differs():
    """OpenAI löst einen Alias in einen datierten Schnappschuss auf, OpenRouter kann umleiten.
    Stimmen angefragter und bedienter Name überein, bleibt das Feld weg."""
    cid = "cccc1111-2222-4333-8444-555555555555"
    server.USAGE_PATH = Path(_TMP) / "modell.jsonl"
    server.USAGE_PATH.unlink(missing_ok=True)
    backend = FakeBackend([{"text": "Fünf.", "calls": [], "model_used": "gpt-5.6-luna-2026-04-01",
                            "usage": {"input": 10, "cached": 0, "cache_write": 0, "output": 2}}])
    _lauf(backend, cid)
    z = _zeilen()[-1]
    assert z["model"] == "openai/gpt-5.6-luna" and z["model_api"] == "gpt-5.6-luna-2026-04-01"

    backend = FakeBackend([{"text": "Sechs.", "calls": [], "model_used": "gpt-5.6-luna",
                            "usage": {"input": 10, "cached": 0, "cache_write": 0, "output": 2}}])
    _lauf(backend, cid, "Und?")
    assert "model_api" not in _zeilen()[-1]
    server.delete_chat(USER, cid)
    server.USAGE_PATH = Path(_TMP) / "usage.jsonl"


def test_a_broken_ledger_never_breaks_an_answer():
    """Wenn die Datei nicht geschrieben werden kann, kommt die Antwort trotzdem an."""
    cid = "bbbb1111-2222-4333-8444-555555555555"
    echt, server.USAGE_PATH = server.USAGE_PATH, Path("/proc/gibtesnicht/usage.jsonl")
    try:
        events = _lauf(_zwei_schritte(), cid)
        assert [e["text"] for e in events if e["type"] == "token"] == ["Fünf (Q1)."]
        assert events[-1]["type"] == "done"
    finally:
        server.USAGE_PATH = echt
        server.delete_chat(USER, cid)


# ---------------------------------------------------------------------------
# Auszählen (scripts/chat_usage.py)
# ---------------------------------------------------------------------------
ZEILEN = [
    {"ts": "2026-09-18T10:00:00", "user": "Olaf Simons", "model": "openai/gpt-5.6-luna",
     "input": 48213, "cached": 31002, "cache_write": 0, "output": 1877, "steps": 3,
     "seconds": 21.4, "chat": "a", "key": "profil"},
    {"ts": "2026-09-19T09:00:00", "user": "Tinghui Duan", "model": "deepseek/deepseek-flash",
     "input": 9000, "cached": 2000, "cache_write": 0, "output": 400, "steps": 1,
     "seconds": 6.0, "chat": "b", "key": "server"},
    {"ts": "2026-09-19T09:30:00", "user": "Olaf Simons", "model": "anthropic/claude-opus-5",
     "input": 120000, "cached": 90000, "cache_write": 12000, "output": 3000, "steps": 4,
     "seconds": 44.2, "chat": "c", "key": "profil"},
]


def _ledger() -> Path:
    path = Path(_TMP) / "bericht.jsonl"
    path.write_text("\n".join(json.dumps(z, ensure_ascii=False) for z in ZEILEN) + "\n",
                    encoding="utf-8")
    return path


def test_report_filters_by_period_user_and_model():
    path = _ledger()
    assert len(chat_usage.read(path)) == 3
    assert len(chat_usage.read(path, since="2026-09-19")) == 2
    assert len(chat_usage.read(path, until="2026-09-18")) == 1
    assert [r["chat"] for r in chat_usage.read(path, user="Tinghui Duan")] == ["b"]
    assert [r["chat"] for r in chat_usage.read(path, model="anthropic")] == ["c"]
    assert chat_usage.read(path, since="2026-10") == []


def test_report_groups_and_sums():
    rows = chat_usage.read(_ledger())
    nach_benutzer = chat_usage.group(rows, "user")
    assert list(nach_benutzer) == ["Olaf Simons", "Tinghui Duan"]        # größter zuerst
    olaf = nach_benutzer["Olaf Simons"]
    assert olaf["antworten"] == 2 and olaf["input"] == 168213 and olaf["output"] == 4877
    assert olaf["cached"] == 121002 and olaf["cache_write"] == 12000
    assert round(olaf["sekunden"], 1) == 65.6

    kombiniert = chat_usage.group(rows, "user+model")
    assert "Olaf Simons · anthropic/claude-opus-5" in kombiniert
    assert set(chat_usage.group(rows, "key")) == {"profil", "server"}
    assert set(chat_usage.group(rows, "day")) == {"2026-09-18", "2026-09-19"}
    assert set(chat_usage.group(rows, "month")) == {"2026-09"}


def test_report_table_has_a_total_row():
    text = chat_usage.table(chat_usage.group(chat_usage.read(_ledger()), "user"), "Titel")
    assert "Titel" in text and "Olaf Simons" in text
    assert "177.213" in text                        # Summe der Eingabe, mit Tausenderpunkten
    assert text.strip().splitlines()[-1].startswith("Summe")


def test_report_names_answers_without_numbers():
    """Eine Antwort ohne Tokenzahlen darf die Summe nicht still zu niedrig machen."""
    import io
    import contextlib
    path = Path(_TMP) / "luecke.jsonl"
    zeilen = ZEILEN + [{"ts": "2026-09-19T09:12:31", "user": "Tinghui Duan",
                        "model": "deepseek/deepseek-flash", "input": 0, "cached": 0,
                        "cache_write": 0, "output": 0, "steps": 9, "seconds": 45.9,
                        "chat": "d", "key": "server"}]
    path.write_text("\n".join(json.dumps(z, ensure_ascii=False) for z in zeilen), encoding="utf-8")
    puffer = io.StringIO()
    argv = sys.argv
    sys.argv = ["chat_usage.py", "--file", str(path)]
    try:
        with contextlib.redirect_stdout(puffer):
            chat_usage.main()
    finally:
        sys.argv = argv
    text = puffer.getvalue()
    assert "1 von 4 Antworten ohne Tokenzahlen" in text and "deepseek/deepseek-flash" in text
    # Ohne Lücke steht der Hinweis nicht da
    puffer2 = io.StringIO()
    sys.argv = ["chat_usage.py", "--file", str(_ledger())]
    try:
        with contextlib.redirect_stdout(puffer2):
            chat_usage.main()
    finally:
        sys.argv = argv
    assert "ohne Tokenzahlen" not in puffer2.getvalue()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
