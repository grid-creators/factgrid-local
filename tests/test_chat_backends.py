"""
Tests für den Web-Chat (chat/server.py): Modellliste, Schlüsselauflösung (Profil vor
Server-Schlüssel), die Gesprächsablage (Seitenleiste, Löschen), die permanenten Links
(/s/<token>, Nur-Lesen, Weiterführen als eigene Kopie) und das DeepSeek-Backend
(Chat-Completions-Format, reasoning_content im Tool-Loop, Zusammensetzen gestreamter
Tool-Aufrufe) – ohne echte API-Aufrufe.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "chat"))

# Testumgebung VOR dem Import festlegen (load_env überschreibt gesetzte Variablen nicht)
_TMP = tempfile.mkdtemp()
os.environ.update({
    "QLEVER_ENDPOINT": "http://127.0.0.1:1",
    "FACTGRID_CACHE_DIR": os.path.join(_TMP, "cache"),
    "FACTGRID_CHAT_PROFILES": os.path.join(_TMP, "profiles.json"),
    "FACTGRID_CHAT_HISTORY": os.path.join(_TMP, "history"),
    "FACTGRID_OAUTH_KEY": "", "FACTGRID_OAUTH_SECRET": "",     # ohne Consumer: offen
    "FACTGRID_CHAT_MODEL": "openai/gpt-5.6-luna",
    "FACTGRID_CHAT_MODELS": "openai/gpt-5.6-luna, deepseek/deepseek-flash",
    "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "sk-server-test", "ANTHROPIC_API_KEY": "",
    "OPENROUTER_API_KEY": "", "MW_DB_PASSWORD": "",
})

import server  # noqa: E402  (chat/server.py)


def test_models_and_providers():
    assert server.CHAT_MODELS == ["openai/gpt-5.6-luna", "deepseek/deepseek-flash"]
    assert server.CHAT_PROVIDERS == ["openai", "deepseek"]
    assert server.split_model("deepseek/deepseek-flash") == ("deepseek", "deepseek-flash")
    assert server.provider_label("deepseek") == "DeepSeek (Cloud)"
    infos = server.model_info("jemand")
    assert [m["id"] for m in infos] == server.CHAT_MODELS
    assert infos[0]["has_key"] is False and infos[1]["has_key"] is True and infos[1]["key_source"] == "server"


def test_key_resolution_profile_before_server():
    assert server.key_for("jemand", "deepseek") == ("sk-server-test", "server")
    assert server.key_for("jemand", "openai") == ("", "")
    server.set_user_key("jemand", "sk-profil", "deepseek")
    try:
        assert server.key_for("jemand", "deepseek") == ("sk-profil", "profil")
        assert server.user_key("jemand", "openai") == ""
    finally:
        server.clear_user_key("jemand", "deepseek")
    assert server.key_for("jemand", "deepseek") == ("sk-server-test", "server")
    backend, why = server.backend_for("jemand", "openai")
    assert backend is None and "OPENAI_API_KEY" in why, why
    backend, why = server.backend_for("jemand", "deepseek")
    assert isinstance(backend, server.DeepSeekBackend) and why is None
    assert server.backend_for("jemand", "gibtsnicht")[0] is None
    assert server._provider_arg("") == "openai" and server._provider_arg("deepseek") == "deepseek"
    assert server._provider_arg("anthropic") is None  # nicht freigeschaltet


def test_deepseek_history_format():
    b = server.DeepSeekBackend(api_key="x")
    neutral = [
        {"role": "user", "content": "Wer hat Q7 bearbeitet?"},
        {"role": "assistant", "content": "", "reasoning": "Ich brauche die Historie.",
         "tool_calls": [{"id": "call_1", "name": "edit_history", "args": {"page": "Q7"}}]},
        {"role": "tool", "id": "call_1", "name": "edit_history", "content": "zeit\tbenutzer\n…"},
        {"role": "assistant", "content": "Olaf Simons, zuletzt 2025.", "tool_calls": []},
    ]
    msgs = b.history(neutral)
    assert msgs[0]["role"] == "system" and "FactGrid" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "Wer hat Q7 bearbeitet?"}
    a = msgs[2]
    assert a["role"] == "assistant" and a["reasoning_content"] == "Ich brauche die Historie."
    assert a["tool_calls"] == [{"id": "call_1", "type": "function",
                                "function": {"name": "edit_history", "arguments": json.dumps({"page": "Q7"})}}]
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "zeit\tbenutzer\n…"}
    assert msgs[4] == {"role": "assistant", "content": "Olaf Simons, zuletzt 2025."}
    assert b.tool_results([{"id": "c", "name": "x", "content": "y"}]) == [{"role": "tool", "tool_call_id": "c", "content": "y"}]


class _FakeStream:
    """Async-Iterator über Chat-Completions-Chunks, wie sie das OpenAI-SDK liefert."""

    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        return self.chunks.pop(0)


def _chunk(content=None, reasoning=None, tool_calls=None, choices=True):
    if not choices:
        return NS(choices=[], usage=NS(total_tokens=1))
    delta = NS(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    return NS(choices=[NS(delta=delta, finish_reason=None)])


def _tc(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


def test_deepseek_step_assembles_stream():
    b = server.DeepSeekBackend(api_key="x")
    seen = {}

    async def fake_create(**kw):
        seen.update(kw)
        return _FakeStream([
            _chunk(reasoning="Erst "), _chunk(reasoning="nachschlagen."),
            _chunk(content="Ich "), _chunk(content="suche."),
            _chunk(tool_calls=[_tc(0, id="call_a", name="edit_history", arguments="")]),
            _chunk(tool_calls=[_tc(0, arguments='{"page": "Q')]),
            _chunk(tool_calls=[_tc(0, arguments='7"}'), _tc(1, id="call_b", name="mw_schema", arguments="{}")]),
            _chunk(choices=False),
        ])

    b.client = NS(chat=NS(completions=NS(create=fake_create)))
    server.MCP_TOOLS = [NS(name="edit_history", description="d", inputSchema={"type": "object", "properties": {}})]

    async def run():
        return [ev async for ev in b.step("deepseek-flash", [{"role": "user", "content": "hi"}],
                                          server.tool_specs())]

    events = asyncio.run(run())
    assert [e["text"] for e in events if e["type"] == "token"] == ["Ich ", "suche."]
    end = events[-1]
    assert end["type"] == "_step_end" and end["text"] == "Ich suche." and end["reasoning"] == "Erst nachschlagen."
    assert end["calls"] == [{"id": "call_a", "name": "edit_history", "args": {"page": "Q7"}},
                            {"id": "call_b", "name": "mw_schema", "args": {}}]
    assert end["msg"]["reasoning_content"] == "Erst nachschlagen." and end["msg"]["content"] == "Ich suche."
    assert end["msg"]["tool_calls"][0] == {"id": "call_a", "type": "function",
                                           "function": {"name": "edit_history", "arguments": '{"page": "Q7"}'}}
    # Request-Parameter: Chat-Completions mit Tools, Denkmodus als extra_body, Streaming
    assert seen["model"] == "deepseek-flash" and seen["stream"] is True
    assert seen["tools"][0]["type"] == "function" and seen["tools"][0]["function"]["name"] == "edit_history"
    assert seen["extra_body"] == {"thinking": {"type": "enabled"}} and "reasoning_effort" not in seen


def test_api_models_and_keys_endpoints():
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        data = c.get("/api/models").json()
        assert [m["id"] for m in data["models"]] == server.CHAT_MODELS and data["default"] == "openai/gpt-5.6-luna"
        assert data["models"][1]["has_key"] is True and data["has_key"] is True
        k = c.get("/api/keys", params={"provider": "deepseek"}).json()
        assert k["server_key"] is True and k["has_key"] is False and k["key_var"] == "DEEPSEEK_API_KEY"
        assert c.get("/api/keys", params={"provider": "anthropic"}).status_code == 400
        me = c.get("/api/me").json()
        assert me["has_key"] is True and me["model"] == "openai/gpt-5.6-luna"
        # Chat mit Modell ohne Schlüssel: klare Fehlermeldung als SSE-Event
        r = c.post("/api/chat", json={"session": "t", "model": "openai/gpt-5.6-luna", "message": "hi"})
        assert '"type": "error"' in r.text and "OPENAI_API_KEY" in r.text, r.text[:300]
        # unbekanntes Modell fällt auf die Vorauswahl zurück (ebenfalls ohne Schlüssel → Fehler)
        r = c.post("/api/chat", json={"session": "t", "model": "anthropic/claude-opus-5", "message": "hi"})
        assert '"type": "error"' in r.text and "OPENAI_API_KEY" in r.text


def test_chat_store_roundtrip():
    user, cid = "jemand", "11111111-2222-4333-8444-555555555555"
    assert server.get_chat(user, cid) is None
    assert server.get_chat(user, "../boese", create=True) is None       # keine Pfadanteile
    chat = server.get_chat(user, cid, create=True)
    server.save_chat(user, chat)                                          # leer → keine Datei
    assert server.list_chats(user) == []
    chat["messages"] += [{"role": "user", "content": "  Wer   war\nGoethe?  "},
                         {"role": "assistant", "content": "Ein Dichter.", "tool_calls": []}]
    chat["model"] = "deepseek/deepseek-flash"
    server.save_chat(user, chat)
    assert chat["title"] == "Wer war Goethe?"
    server.CHATS.clear()                                                  # von der Platte lesen
    lst = server.list_chats(user)
    assert [c["id"] for c in lst] == [cid] and lst[0]["count"] == 1 and lst[0]["title"] == "Wer war Goethe?"
    assert lst[0]["model"] == "deepseek/deepseek-flash" and lst[0]["updated"]
    assert server.get_chat(user, cid)["messages"][1]["content"] == "Ein Dichter."
    assert server.list_chats("andere") == []                              # nur eigene Gespräche
    # langer Titel wird gekürzt
    other = server.get_chat(user, "zweites", create=True)
    other["messages"].append({"role": "user", "content": "x" * 200})
    server.save_chat(user, other)
    assert len(other["title"]) == server.TITLE_LEN and other["title"].endswith("…")
    assert [c["id"] for c in server.list_chats(user)] == ["zweites", cid]  # zuletzt benutzte zuerst
    assert server.delete_chat(user, cid) is True and server.delete_chat(user, cid) is False
    assert server.delete_chat(user, "../boese") is False
    assert [c["id"] for c in server.list_chats(user)] == ["zweites"] and server.get_chat(user, cid) is None
    server.delete_chat(user, "zweites")


def test_trim_open_calls():
    tc = lambda i: {"id": i, "name": "sparql", "args": {"query": "…"}}  # noqa: E731
    msgs = [{"role": "user", "content": "a"},
            {"role": "assistant", "content": "", "tool_calls": [tc("c1")]},
            {"role": "tool", "id": "c1", "name": "sparql", "content": "x"},
            {"role": "assistant", "content": "", "tool_calls": [tc("c2"), tc("c3")]},
            {"role": "tool", "id": "c2", "name": "sparql", "content": "y"}]  # c3 fehlt (Abbruch)
    server._trim_open_calls(msgs)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    server._trim_open_calls(msgs)                                         # vollständig → unverändert
    assert len(msgs) == 3
    msgs.append({"role": "assistant", "content": "fertig", "tool_calls": []})
    server._trim_open_calls(msgs)
    assert len(msgs) == 4
    only_user = [{"role": "user", "content": "a"}]
    server._trim_open_calls(only_user)
    assert len(only_user) == 1


def test_api_chats_endpoints():
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        assert c.get("/api/chats").json() == {"chats": []}
        assert c.get("/api/history", params={"session": "gibtsnicht"}).json() == {"history": [], "title": ""}
        # Ein Gespräch anlegen wie nach einer Antwort; die Liste und der Verlauf kommen daraus.
        chat = server.get_chat("_offen", "abc-123", create=True)
        chat["messages"] += [{"role": "user", "content": "Wie viele Menschen?"},
                             {"role": "assistant", "content": "Viele.", "tool_calls": []}]
        server.save_chat("_offen", chat)
        lst = c.get("/api/chats").json()["chats"]
        assert len(lst) == 1 and lst[0]["id"] == "abc-123" and lst[0]["title"] == "Wie viele Menschen?"
        h = c.get("/api/history", params={"session": "abc-123"}).json()
        assert h["title"] == "Wie viele Menschen?" and [m["role"] for m in h["history"]] == ["user", "assistant"]
        # Fehlgeschlagener Chat-Aufruf (kein Schlüssel) legt kein Gespräch an
        r = c.post("/api/chat", json={"session": "neu-1", "model": "openai/gpt-5.6-luna", "message": "hi"})
        assert '"type": "error"' in r.text
        assert [x["id"] for x in c.get("/api/chats").json()["chats"]] == ["abc-123"]
        r = c.post("/api/chat", json={"session": "../x", "model": "deepseek/deepseek-flash", "message": "hi"})
        ev = json.loads(r.text.split("data: ", 1)[1].split("\n\n")[0])   # Umlaute kommen \u-escaped
        assert ev == {"type": "error", "message": "Ungültige Gesprächs-ID."}
        assert c.delete("/api/chats/abc-123").json() == {"ok": True, "deleted": True}
        assert c.delete("/api/chats/abc-123").json() == {"ok": True, "deleted": False}
        assert c.get("/api/chats").json() == {"chats": []}


def _saved_chat(user: str, cid: str, frage: str = "Wer war Goethe?") -> dict:
    """Ein gespeichertes Gespräch wie nach einer Antwort (mit Tool-Aufruf und reasoning)."""
    chat = server.get_chat(user, cid, create=True)
    chat["messages"] = [
        {"role": "user", "content": frage},
        {"role": "assistant", "content": "", "reasoning": "geheimer Denkschritt",
         "tool_calls": [{"id": "c1", "name": "sparql", "args": {"query": "SELECT * WHERE {} LIMIT 1"}}]},
        {"role": "tool", "id": "c1", "name": "sparql", "content": "x\n1"},
        {"role": "assistant", "content": "Ein Dichter (Q1234).", "tool_calls": []},
    ]
    server.save_chat(user, chat)
    return chat


def test_share_token_is_permanent():
    user, cid = "teiler", "aaaa1111-2222-4333-8444-555555555555"
    chat = _saved_chat(user, cid)
    token = chat["share"]
    assert server.SHARE_TOKEN.match(token), token
    chat["messages"].append({"role": "user", "content": "Und wann?"})
    server.save_chat(user, chat)
    assert chat["share"] == token                                  # bleibt über Antworten hinweg
    # Nach einem Neustart kommt der Index von der Platte
    server.CHATS.clear(), server.SHARES.clear()
    server._SHARES_SCANNED = False
    hit = server.shared_chat(token)
    assert hit is not None and hit[1]["id"] == cid and hit[0] == "teiler"
    assert server.shared_chat("kurz") is None                      # Token-Form
    assert server.shared_chat("../../etc/passwd-aaaaaaaaaaaaaa") is None
    assert server.shared_chat(token[:-1] + ("x" if token[-1] != "x" else "y")) is None
    # Löschen macht den Link tot, auch wenn ihn noch jemand hat
    assert server.delete_chat(user, cid) is True
    assert server.shared_chat(token) is None


def test_share_endpoints_read_only_and_continue():
    from fastapi.testclient import TestClient
    user = "_offen"
    with TestClient(server.app) as c:
        cid = "bbbb1111-2222-4333-8444-555555555555"
        chat = _saved_chat(user, cid, "Wie viele Menschen?")
        # Der Link kommt aus /api/share und ist derselbe wie in der Datei
        d = c.get("/api/share", params={"session": cid}).json()
        assert d["token"] == chat["share"] and d["path"] == "/s/" + chat["share"]
        assert c.get("/api/share", params={"session": cid}).json()["token"] == d["token"]
        # Noch nicht gespeicherte oder fremde Gespräche haben keinen Link
        r = c.get("/api/share", params={"session": "gibtsnicht"})
        assert r.status_code == 404 and "erste" in r.json()["error"]
        # /s/<token> liefert die Oberfläche, die sich selbst als Nur-Lese-Ansicht erkennt
        page = c.get("/s/" + d["token"])
        assert page.status_code == 200 and "noindex" in page.headers.get("x-robots-tag", "")
        assert "/s/" in page.text and "FactGrid Chat" in page.text
        # Der Inhalt: ohne reasoning, mit Tool-Aufruf und Ergebnis
        shared = c.get("/api/shared", params={"token": d["token"]}).json()
        assert shared["title"] == "Wie viele Menschen?" and len(shared["messages"]) == 4
        assert [m["role"] for m in shared["messages"]] == ["user", "assistant", "tool", "assistant"]
        assert all("reasoning" not in m for m in shared["messages"])
        assert shared["messages"][1]["tool_calls"][0]["name"] == "sparql"
        assert c.get("/api/shared", params={"token": "unbekannt-aaaaaaaaaaaa"}).status_code == 404
        # Weiterführen: eigene Kopie mit neuer ID und neuem Link, Original unberührt
        f = c.post("/api/shared/continue", json={"token": d["token"]}).json()
        assert f["ok"] and f["session"] != cid and f["share"] != d["token"]
        kopie = server.get_chat(user, f["session"])
        assert [m["content"] for m in kopie["messages"]] == [m["content"] for m in chat["messages"]]
        assert kopie["source"] == d["token"] and kopie["title"] == chat["title"]
        kopie["messages"].append({"role": "user", "content": "Und Schiller?"})
        server.save_chat(user, kopie)
        assert len(server.get_chat(user, cid)["messages"]) == 4          # Original unverändert
        assert c.get("/api/shared", params={"token": d["token"]}).json()["messages"][-1]["content"] \
            == "Ein Dichter (Q1234)."
        assert c.post("/api/shared/continue", json={"token": "weg-aaaaaaaaaaaaaaaa"}).status_code == 404
        for x in (cid, f["session"]):
            server.delete_chat(user, x)


def test_shared_view_open_without_login():
    """Mit eingerichteter FactGrid-Anmeldung bleibt nur die Nur-Lese-Ansicht offen – nicht das
    Weiterführen. Den Weg über FactGrid selbst prüft tests/test_oauth.py; hier zählt nur, dass
    eine Anmeldung verlangt wird."""
    from fastapi.testclient import TestClient
    user, cid = "_offen", "cccc1111-2222-4333-8444-555555555555"
    angemeldet, chat = "Olaf Simons", _saved_chat(user, cid)
    server.OAUTH.key, server.OAUTH.secret = "consumer", "geheim"      # damit OAUTH.enabled gilt
    try:
        with TestClient(server.app) as c:
            token = chat["share"]
            assert c.get("/s/" + token).status_code == 200
            assert c.get("/api/shared", params={"token": token}).status_code == 200
            assert c.get("/api/chats").status_code == 401                 # alles andere zu
            assert c.get("/api/share", params={"session": cid}).status_code == 401
            assert c.post("/api/shared/continue", json={"token": token}).status_code == 401
            sitzung = "sitzung-fuer-den-test"
            server.LOGINS[sitzung] = {"user": angemeldet, "expires": time.time() + 600}
            c.cookies.set(server.COOKIE, sitzung)
            f = c.post("/api/shared/continue", json={"token": token}).json()
            assert f["ok"] and f["session"] != cid
            assert [x["id"] for x in c.get("/api/chats").json()["chats"]] == [f["session"]]
            server.delete_chat(angemeldet, f["session"])
    finally:
        server.OAUTH.key = server.OAUTH.secret = ""
        server.LOGINS.clear()
        server.delete_chat(user, cid)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
