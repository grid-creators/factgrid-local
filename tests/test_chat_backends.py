"""
Tests für den Web-Chat (chat/server.py): Modellliste, Schlüsselauflösung (Profil vor
Server-Schlüssel), die Gesprächsablage (Seitenleiste, Löschen), die permanenten Links
(/s/<token>, Nur-Lesen, Weiterführen als eigene Kopie) und das DeepSeek-Backend
(Chat-Completions-Format, reasoning_content im Tool-Loop, Zusammensetzen gestreamter
Tool-Aufrufe) sowie die Datei-Anhänge (Textgewinnung, Upload-Endpunkt, Weg in die Frage
jedes Providers) – ohne echte API-Aufrufe.
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

# Testumgebung VOR dem Import festlegen (load_env überschreibt gesetzte Variablen nicht)
_TMP = tempfile.mkdtemp()
os.environ.update({
    "QLEVER_ENDPOINT": "http://127.0.0.1:1",
    "FACTGRID_CACHE_DIR": os.path.join(_TMP, "cache"),
    "FACTGRID_CHAT_PROFILES": os.path.join(_TMP, "profiles.json"),
    "FACTGRID_CHAT_HISTORY": os.path.join(_TMP, "history"),
    "FACTGRID_CHAT_UPLOADS": os.path.join(_TMP, "uploads"),
    "FACTGRID_CHAT_USER": "", "FACTGRID_CHAT_PASSWORD": "", "FACTGRID_CHAT_USERS": "",
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
    """Mit konfigurierten Benutzern bleibt nur die Nur-Lese-Ansicht offen – nicht das Weiterführen."""
    from fastapi.testclient import TestClient
    user, cid = "_offen", "cccc1111-2222-4333-8444-555555555555"
    chat = _saved_chat(user, cid)
    server.CHAT_USERS = {"olaf": "geheim"}
    try:
        with TestClient(server.app) as c:
            token = chat["share"]
            assert c.get("/s/" + token).status_code == 200
            assert c.get("/api/shared", params={"token": token}).status_code == 200
            assert c.get("/api/chats").status_code == 401                 # alles andere zu
            assert c.get("/api/share", params={"session": cid}).status_code == 401
            assert c.post("/api/shared/continue", json={"token": token}).status_code == 401
            assert c.post("/api/login", json={"user": "olaf", "password": "geheim"}).status_code == 200
            f = c.post("/api/shared/continue", json={"token": token}).json()
            assert f["ok"] and f["session"] != cid
            assert [x["id"] for x in c.get("/api/chats").json()["chats"]] == [f["session"]]
            server.delete_chat("olaf", f["session"])
    finally:
        server.CHAT_USERS = {}
        server.LOGINS.clear()
        server.delete_chat(user, cid)


# ---------------------------------------------------------------------------
# Datei-Anhänge
# ---------------------------------------------------------------------------
def test_extract_text_reads_text_and_names_the_rest():
    assert server.extract_text("l.tsv", b"q\tname\nQ1\tGoethe\n") == ("q\tname\nQ1\tGoethe\n", "")
    # Excel schreibt CSV in der Windows-Kodierung und mit CRLF; BOM ebenfalls abfangen
    assert server.extract_text("l.csv", "Köln\r\nWien\r\n".encode("cp1252")) == ("Köln\nWien\n", "")
    assert server.extract_text("l.txt", "\ufeffmit BOM".encode("utf-8")) == ("mit BOM", "")
    # Was kein Text ist, wird benannt statt als Kauderwelsch angehängt
    assert "Office" in server.extract_text("t.xlsx", b"PK\x03\x04\x14\x00")[1]
    assert "Bild" in server.extract_text("b.png", b"\x89PNG\r\n\x1a\n")[1]
    assert server.extract_text("x.bin", b"eins\x00zwei")[1]
    assert server.extract_text("kaputt.pdf", b"%PDF-1.4 kein echtes PDF")[1]   # mit wie ohne pypdf
    assert server._safe_name("../../ liste.tsv") == "liste.tsv" and server._safe_name("") == "datei.txt"


def test_upload_endpoint_and_storage():
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        r = c.post("/api/upload", params={"name": "/tmp/liste.tsv"}, content=b"q\tname\nQ1\tGoethe\n")
        d = r.json()
        assert r.status_code == 200 and d["name"] == "liste.tsv" and d["lines"] == 2 and d["size"] == 17
        assert server._UPLOAD_ID.match(d["id"]) and d["columns"] == ["q", "name"]
        assert d["delim"] == "\t" and d["head"].startswith("q\tname")
        assert server.read_upload("_offen", d["id"])["name"] == "liste.tsv"
        assert server.read_upload("_offen", "keine-id") is None
        assert server.read_upload("andere", d["id"]) is None          # jeder sieht nur den eigenen Ordner
        assert c.post("/api/upload", params={"name": "b.xlsx"}, content=b"PK\x03\x04\x14\x00").status_code == 415
        assert c.post("/api/upload", params={"name": "leer.txt"}, content=b"").status_code == 400
        limit = server.UPLOAD_MAX
        server.UPLOAD_MAX = 8
        try:
            assert c.post("/api/upload", params={"name": "gross.txt"}, content=b"zu viele Zeichen").status_code == 413
        finally:
            server.UPLOAD_MAX = limit
        # In den Prompt geht nur der Anfang; die ganze Datei bleibt auf der Platte
        preview = server.UPLOAD_PREVIEW
        server.UPLOAD_PREVIEW = 5
        try:
            meta = c.post("/api/upload", params={"name": "lang.txt"}, content=b"abcdefghij").json()
        finally:
            server.UPLOAD_PREVIEW = preview
        assert meta["chars"] == 10 and meta["head"] == "abcde"
        assert server._upload_path("_offen", meta["id"], ".txt").read_text() == "abcdefghij"
        # Ausschnitt vom Anfang einer zu großen Datei (die Oberfläche schneidet, der Server merkt es sich)
        head = c.post("/api/upload", params={"name": "gross.csv", "part": "1"}, content=b"a;b\n1;2\n").json()
        assert head["part"] is True and head["columns"] == ["a", "b"] and head["delim"] == ";"
        assert "nur der Anfang der hochgeladenen Datei" in server.attachment_text(head)
        # Die Oberfläche braucht die Grenze, um Zugroßes gar nicht erst loszuschicken
        me = c.get("/api/me").json()
        assert me["upload_max"] == server.UPLOAD_MAX and me["upload_preview"] == server.UPLOAD_PREVIEW


def test_attachment_reaches_every_provider():
    f = {"id": "a" * 32, "name": "liste.tsv", "size": 17, "chars": 60, "lines": 3,
         "columns": ["q", "name"], "delim": "\t", "delim_label": "Tabulator", "head": "q\tname"}
    msg = {"role": "user", "content": "Wer fehlt davon in FactGrid?", "files": [f]}
    text = server.user_content(msg)
    assert "Wer fehlt davon in FactGrid?" in text and "liste.tsv" in text and "q\tname" in text
    assert "3 Zeilen; 60 Zeichen" in text and "Trennzeichen Tabulator, 2 Spalten: q, name" in text
    assert "read_attachment" in text and "column_stats" in text
    assert server.user_content({"role": "user", "content": "ohne Anhang"}) == "ohne Anhang"
    assert "(keine Frage" in server.user_content({"role": "user", "content": "", "files": [f]})
    # Derselbe Text geht an alle vier Anbieter (OpenAI und OpenRouter teilen sich die Basis)
    assert server.DeepSeekBackend(api_key="x").history([msg])[1]["content"] == text
    assert server.AnthropicBackend(api_key="x").history([msg])[0]["content"] == text
    assert server.ResponsesBackend().history([msg])[0]["content"] == text


def test_attachment_tools_see_the_whole_file():
    """Der Kern des Anhang-Umbaus: im Prompt steht nur der Anfang, die Werkzeuge arbeiten
    auf der ganzen Datei – auch auf Zeilen, die das Modell nie zu Gesicht bekommt."""
    from fastapi.testclient import TestClient
    user, cid = "_offen", "eeee1111-2222-4333-8444-555555555555"
    rows = ["nachname;vorname;ort;qid"]
    for i in range(1, 501):
        rows.append(f"Muster{i};Hans;{'Göttingen' if i % 2 else 'Weimar'};"
                    f"{'Q' + str(1000 + i) if i % 5 == 0 else ''}")
    csv = ("\n".join(rows) + "\n").encode()
    with TestClient(server.app) as c:
        up = c.post("/api/upload", params={"name": "studenten.csv"}, content=csv).json()
        assert up["lines"] == 501 and up["columns"] == ["nachname", "vorname", "ort", "qid"]
        assert len(up["head"]) <= server.UPLOAD_PREVIEW            # nur der Anfang geht in den Prompt
        chat = server.get_chat(user, cid, create=True)
        meta = server.attach_upload(user, cid, up["id"])
        chat["messages"].append({"role": "user", "content": "Wer davon fehlt in FactGrid?",
                                 "files": [meta]})
        server.save_chat(user, chat)
        ctx = (user, chat)
        assert len(server.user_content(chat["messages"][0])) < 3000  # die Datei liegt NICHT im Prompt

        out = server.tool_list_attachments(ctx)
        assert "studenten.csv" in out and "501 Zeilen" in out and "Semikolon" in out

        out = server.tool_read_attachment(ctx, name="studenten", from_line=2, lines=3)
        assert out.splitlines()[0] == "studenten.csv, Zeilen 2–4 von 501:"
        assert out.splitlines()[1].startswith("Muster1;Hans;Göttingen")
        assert server.tool_read_attachment(ctx, from_line=9000).startswith("studenten.csv hat 501 Zeilen")
        assert len(server.tool_read_attachment(ctx, lines=99999).splitlines()) <= server.READ_LINES_MAX + 1

        # Eine Zeile weit hinten – im Prompt stand sie nie
        out = server.tool_search_attachment(ctx, query="Muster499;")
        assert "1 Treffer" in out and out.splitlines()[1].startswith("500: Muster499;")
        out = server.tool_search_attachment(ctx, query="göttingen", max_hits=5)
        assert "250 Treffer" in out and "die ersten 5" in out and len(out.splitlines()) == 6
        assert "Keine Zeile" in server.tool_search_attachment(ctx, query="Schiller")
        assert "1 Treffer" in server.tool_search_attachment(ctx, query="^Muster499;", regex=True)
        assert "ungültig" in server.tool_search_attachment(ctx, query="Muster(", regex=True)

        # Mengenfrage über die ganze Datei, ohne sie zu lesen
        out = server.tool_column_stats(ctx, column="qid")
        assert "500 Zeilen, 100 gefüllt, 400 leer, 100 verschiedene Werte" in out
        out = server.tool_column_stats(ctx, column="3")                # Spalte auch per Nummer
        assert "Göttingen\t250" in out and "Weimar\t250" in out
        assert "gibt es nicht" in server.tool_column_stats(ctx, column="wohnort")
        assert "Kein Anhang" in server.tool_read_attachment(ctx, name="andere.csv")
        # Erfundene Argumente sprengen den Aufruf nicht
        assert "studenten.csv" in server.call_local_tool(ctx, "list_attachments", {"foo": 1})

        # Herunterladen: die ganze Datei, nicht der Auszug
        r = c.get("/api/attachment", params={"session": cid, "file": meta["id"]})
        assert r.status_code == 200 and r.content == csv and "studenten.csv" in r.headers["content-disposition"]
        assert c.get("/api/attachment", params={"session": cid, "file": "f" * 32}).status_code == 404
        # Über den geteilten Link liegt der Anhang mit
        token = c.get("/api/share", params={"session": cid}).json()["token"]
        r = c.get("/api/shared/attachment", params={"token": token, "file": meta["id"]})
        assert r.status_code == 200 and len(r.content) == len(csv)
        # Weiterführen macht eine eigene Kopie der Dateien
        kopie = c.post("/api/shared/continue", json={"token": token}).json()["session"]
        assert server.attachment_file(user, kopie, meta["id"]) is not None
        server.delete_chat(user, kopie)

        folder = server._files_dir(user, cid)
        assert folder.is_dir()
        server.delete_chat(user, cid)
        assert not folder.exists()                                  # Gespräch weg, Anhänge weg


def test_question_with_file_only_and_missing_attachment():
    from fastapi.testclient import TestClient
    user, cid = "_offen", "dddd1111-2222-4333-8444-555555555555"
    chat = server.get_chat(user, cid, create=True)
    chat["messages"].append({"role": "user", "content": "",
                             "files": [{"id": "b" * 32, "name": "liste.tsv", "size": 9, "chars": 9,
                                        "lines": 2, "head": "q\nQ1"}]})
    server.save_chat(user, chat)
    assert chat["title"] == "liste.tsv"                       # Titel aus dem Anhang, wenn die Frage leer ist
    with TestClient(server.app) as c:
        assert c.get("/api/history", params={"session": cid}).json()["history"][0]["files"][0]["name"] == "liste.tsv"
        # Ein verfallener Anhang geht nicht still verloren, die Frage läuft aber weiter
        r = c.post("/api/chat", json={"session": "neu-mit-datei", "model": "openai/gpt-5.6-luna",
                                      "message": "hi", "files": ["0" * 32]})
        assert "nicht mehr da" in r.text and "OPENAI_API_KEY" in r.text
        # Weder Frage noch Anhang: gar nichts zu tun
        r = c.post("/api/chat", json={"session": "neu-leer", "model": "openai/gpt-5.6-luna", "message": " "})
        assert "Keine Frage" in json.loads(r.text.split("data: ", 1)[1].split("\n\n")[0])["message"]
    server.delete_chat(user, cid)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
