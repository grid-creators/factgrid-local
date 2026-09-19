"""
Tests für die Anmeldung über FactGrid (MediaWiki-OAuth 1.0a, chat/mwoauth.py):
die Signatur nach RFC 5849 gegen das Beispiel aus der Spezifikation, die Prüfung des
Identitäts-JWT (Signatur, Empfänger, Aussteller, Gültigkeit, Nonce) und der ganze Ablauf
über /api/oauth/login und /api/oauth/callback gegen ein nachgebautes Wiki
(httpx.MockTransport, das jede Signatur nachrechnet) – ohne Netz.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

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
    "FACTGRID_CHAT_MODEL": "openai/gpt-5.6-luna",
    "FACTGRID_CHAT_MODELS": "openai/gpt-5.6-luna",
    "FACTGRID_OAUTH_KEY": "", "FACTGRID_OAUTH_SECRET": "",
    "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "ANTHROPIC_API_KEY": "",
    "OPENROUTER_API_KEY": "", "MW_DB_PASSWORD": "",
})

import mwoauth  # noqa: E402  (chat/mwoauth.py)
import server  # noqa: E402  (chat/server.py)
from fastapi.testclient import TestClient  # noqa: E402

WIKI = "https://wiki.example.org/w/index.php"
KEY, SECRET = "consumer-key", "consumer-geheimnis"
REQ_TOKEN, REQ_SECRET = "anfrage-token", "anfrage-geheimnis"
ACC_TOKEN, ACC_SECRET = "zugriffs-token", "zugriffs-geheimnis"
CALLBACK = "https://chat.example.org/api/oauth/callback"   # beim Consumer eingetragen


def _fehler(fn, wort: str) -> None:
    """fn() muss mit einem OAuthError scheitern, in dem `wort` vorkommt."""
    try:
        fn()
    except mwoauth.OAuthError as e:
        assert wort in str(e), f"{wort!r} fehlt in {e!s}"
        return
    raise AssertionError(f"kein Fehler, erwartet war {wort!r}")


# ---------------------------------------------------------------------------
# Signatur
# ---------------------------------------------------------------------------
def test_signature_matches_the_spec_example():
    """Beispiel aus der OAuth-1.0a-Spezifikation (Anhang A.5): stimmt die Signatur hier,
    stimmt die Signaturbasis – daran scheitert sonst der ganze Ablauf."""
    url = "http://photos.example.net/photos"
    params = {"file": "vacation.jpg", "size": "original",
              "oauth_consumer_key": "dpf43f3p2l4k3l03", "oauth_token": "nnch734d00sl2jdk",
              "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": "1191242096",
              "oauth_nonce": "kllo9940pd9333jh", "oauth_version": "1.0"}
    assert mwoauth.base_string("GET", url, params) == (
        "GET&http%3A%2F%2Fphotos.example.net%2Fphotos&file%3Dvacation.jpg"
        "%26oauth_consumer_key%3Ddpf43f3p2l4k3l03%26oauth_nonce%3Dkllo9940pd9333jh"
        "%26oauth_signature_method%3DHMAC-SHA1%26oauth_timestamp%3D1191242096"
        "%26oauth_token%3Dnnch734d00sl2jdk%26oauth_version%3D1.0%26size%3Doriginal")
    assert mwoauth.sign("GET", url, params, "kd94hf93k423kf44",
                        "pfkkdhi9sl3r4s00") == "tR3+Ty81lMeYAr/Fid0kMTYa/WM="


def test_percent_encoding_follows_rfc_3986():
    assert mwoauth.percent("a b/c~d-e_f.g") == "a%20b%2Fc~d-e_f.g"
    assert mwoauth.percent("Olaf Simons") == "Olaf%20Simons"


def test_signed_params_carry_everything_the_wiki_checks():
    p = mwoauth.signed_params("GET", WIKI, {"title": "Special:OAuth/initiate"}, KEY, SECRET)
    assert p["oauth_consumer_key"] == KEY and p["oauth_signature_method"] == "HMAC-SHA1"
    assert p["oauth_version"] == "1.0" and len(p["oauth_nonce"]) >= 16
    assert int(p["oauth_timestamp"]) > 1700000000 and p["oauth_signature"]
    assert "oauth_token" not in p                      # der erste Schritt hat noch keinen


# ---------------------------------------------------------------------------
# Identitäts-JWT
# ---------------------------------------------------------------------------
def _jwt(claims: dict, secret: str = SECRET, alg: str = "HS256") -> str:
    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    head_payload = f"{seg({'typ': 'JWT', 'alg': alg})}.{seg(claims)}"
    sig = hmac.new(secret.encode(), head_payload.encode(), hashlib.sha256).digest()
    return f"{head_payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def _claims(**over) -> dict:
    now = int(time.time())
    base = {"aud": KEY, "iss": "https://wiki.example.org", "iat": now, "exp": now + 100,
            "nonce": "abc123", "username": "Olaf Simons", "blocked": False,
            "confirmed_email": True, "groups": ["user"], "sub": 42}
    base.update(over)
    return base


def test_identify_accepts_a_sound_token():
    claims = mwoauth.verify_identify(_jwt(_claims()), KEY, SECRET, WIKI, "abc123")
    assert claims["username"] == "Olaf Simons" and claims["blocked"] is False


def test_identify_rejects_what_it_should():
    alt = int(time.time()) - 900
    faelle = [
        (_jwt(_claims(), secret="falsch"), "abc123", "Signatur"),                  # anderes Geheimnis
        (_jwt(_claims(aud="anderer-consumer")), "abc123", "Consumer"),             # für jemand anderen
        (_jwt(_claims(iss="https://boeses.example.net")), "abc123", "kommt nicht"),  # falsches Wiki
        (_jwt(_claims(iat=alt, exp=alt + 100)), "abc123", "abgelaufen"),           # zu alt
        (_jwt(_claims()), "andere-nonce", "gehört nicht"),                         # aufgezeichnet
        (_jwt(_claims(), alg="none"), "abc123", "Signaturverfahren"),              # unsigniert
        ("gar-kein-jwt", "abc123", "JWT"),
    ]
    for jws, nonce, wort in faelle:
        _fehler(lambda: mwoauth.verify_identify(jws, KEY, SECRET, WIKI, nonce), wort)


def test_both_token_formats_are_read():
    """Mit format=json antwortet MediaWiki {"key": …, "secret": …} - so kommt es von FactGrid -,
    ohne das formularkodierte oauth_token=… des OAuth-Standards."""
    assert mwoauth._tokens('{"key": "t", "secret": "g", "oauth_callback_confirmed": "true"}') == ("t", "g")
    assert mwoauth._tokens("oauth_token=t&oauth_token_secret=g&oauth_callback_confirmed=true") == ("t", "g")
    _fehler(lambda: mwoauth._tokens('{"error": "mwoauth-oauth-exception", '
                                    '"message": "Invalid consumer key"}'), "Invalid consumer key")
    _fehler(lambda: mwoauth._tokens("<html>Fehlerseite des Proxys</html>"), "html")


# ---------------------------------------------------------------------------
# Nachgebautes Wiki: prüft jede Signatur und antwortet wie Special:OAuth
# ---------------------------------------------------------------------------
def wiki_transport(blocked: bool = False, username: str = "Olaf Simons",
                   akzeptiert: str = "oob") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        title = params.get("title", "")
        signature = params.pop("oauth_signature", "")
        geheimnis = {"Special:OAuth/initiate": "", "Special:OAuth/token": REQ_SECRET,
                     "Special:OAuth/identify": ACC_SECRET}
        if title not in geheimnis:
            return httpx.Response(404, text=json.dumps({"error": "unbekannte Seite"}))
        if signature != mwoauth.sign("GET", WIKI, params, SECRET, geheimnis[title]):
            return httpx.Response(400, text=json.dumps(
                {"error": "mwoauth-oauth-exception", "message": "Invalid signature"}))
        if title == "Special:OAuth/initiate":
            # Ein Consumer ohne "callback is prefix" nimmt nur oob (mwoauth-callback-not-oob)
            if params.get("oauth_callback") != akzeptiert:
                return httpx.Response(400, text=json.dumps(
                    {"error": "mwoauth-callback-not-oob",
                     "message": 'oauth_callback must be set, and must be set to "oob" '
                                '(case-sensitive)'}))
            return httpx.Response(200, text=json.dumps(
                {"key": REQ_TOKEN, "secret": REQ_SECRET, "oauth_callback_confirmed": "true"}))
        if title == "Special:OAuth/token":
            if params.get("oauth_verifier") != "verifier-xyz":
                return httpx.Response(400, text=json.dumps(
                    {"error": "mwoauth-oauth-exception", "message": "Invalid verifier"}))
            assert params.get("oauth_token") == REQ_TOKEN
            return httpx.Response(200, text=json.dumps({"key": ACC_TOKEN, "secret": ACC_SECRET}))
        assert params.get("oauth_token") == ACC_TOKEN
        return httpx.Response(200, text=_jwt(_claims(nonce=params["oauth_nonce"],
                                                     username=username, blocked=blocked)))
    return httpx.MockTransport(handler)


@contextmanager
def wiki(sendet: str = "oob", **kw):
    """server.OAUTH auf das nachgebaute Wiki umstellen (und hinterher zurück). `sendet` ist,
    was der Chat als oauth_callback schickt, `akzeptiert` (an wiki_transport), was das Wiki
    durchgehen lässt."""
    vorher = server.OAUTH
    server.OAUTH = mwoauth.MwOAuth(KEY, SECRET, WIKI, sendet, transport=wiki_transport(**kw))
    try:
        yield server.OAUTH
    finally:
        server.OAUTH = vorher
        server.PENDING.clear()
        server.LOGINS.clear()


def _anmelden(c: TestClient) -> httpx.Response:
    """Beide Schritte durchlaufen, wie der Browser sie geht."""
    c.get("/api/oauth/login", follow_redirects=False)
    return c.get("/api/oauth/callback", follow_redirects=False,
                 params={"oauth_token": REQ_TOKEN, "oauth_verifier": "verifier-xyz"})


def test_initiate_sends_oob_so_the_wiki_takes_the_registered_callback():
    """Ohne Haken bei „callback is prefix“ verlangt MediaWiki oauth_callback=oob und schickt den
    Browser danach an die beim Consumer eingetragene Adresse (Consumer::generateCallbackUrl:
    `if ($callback === 'oob') { $callback = $this->getCallbackUrl(); }`) – mit oauth_token und
    oauth_verifier angehängt. Vorgabe ist darum oob."""
    with wiki(), TestClient(server.app) as c:
        assert server.OAUTH.callback == "oob"
        assert c.get("/api/oauth/login", follow_redirects=False).status_code == 303
    # Eine eigene Adresse an so einen Consumer: genau der Fehler, den das Wiki dann meldet
    with wiki(sendet=CALLBACK), TestClient(server.app) as c:
        ort = c.get("/api/oauth/login", follow_redirects=False).headers["location"]
        assert "fehler=" in ort and "oob" in ort
    # Ein Consumer MIT „callback is prefix“ nimmt sie (FACTGRID_OAUTH_CALLBACK)
    with wiki(sendet=CALLBACK, akzeptiert=CALLBACK), TestClient(server.app) as c:
        assert c.get("/api/oauth/login", follow_redirects=False).status_code == 303


def test_the_whole_flow_logs_a_factgrid_account_in():
    with wiki(), TestClient(server.app) as c:
        me = c.get("/api/me").json()
        assert me["auth"] is True and me["user"] is None
        assert c.get("/api/chats").status_code == 401          # ohne Anmeldung zu

        # 1. /api/oauth/login schickt zum Wiki – mit Anfrage-Token und Consumer
        r = c.get("/api/oauth/login", follow_redirects=False)
        assert r.status_code == 303
        ziel = urlparse(r.headers["location"])
        frage = {k: v[0] for k, v in parse_qs(ziel.query).items()}
        assert ziel.netloc == "wiki.example.org" and frage["title"] == "Special:OAuth/authorize"
        assert frage["oauth_token"] == REQ_TOKEN and frage["oauth_consumer_key"] == KEY
        assert server.PENDING[REQ_TOKEN]["secret"] == REQ_SECRET

        # 2. Das Wiki ruft zurück: Token tauschen, Identität prüfen, Sitzung setzen
        r = c.get("/api/oauth/callback", follow_redirects=False,
                  params={"oauth_token": REQ_TOKEN, "oauth_verifier": "verifier-xyz"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert r.cookies.get("fg_session")
        assert not server.PENDING                              # Anfrage-Token ist verbraucht

        # 3. Angemeldet als der FactGrid-Benutzer
        me = c.get("/api/me").json()
        assert me["user"] == "Olaf Simons" and me["wiki"] == "wiki.example.org"
        assert c.get("/api/chats").status_code == 200
        assert c.post("/api/logout").status_code == 200
        assert c.get("/api/me").json()["user"] is None
        assert c.get("/api/chats").status_code == 401


def test_the_session_expires():
    with wiki(), TestClient(server.app) as c:
        _anmelden(c)
        for eintrag in server.LOGINS.values():
            eintrag["expires"] = time.time() - 1
        assert c.get("/api/me").json()["user"] is None
        assert c.get("/api/chats").status_code == 401
        assert not server.LOGINS                               # abgelaufenes wird weggeräumt


def test_blocked_accounts_stay_outside():
    with wiki(blocked=True), TestClient(server.app) as c:
        r = _anmelden(c)
        assert r.status_code == 303 and "gesperrt" in r.headers["location"]
        assert not r.cookies.get("fg_session")
        assert c.get("/api/chats").status_code == 401


def test_callback_without_a_pending_login_is_refused():
    with wiki(), TestClient(server.app) as c:
        r = c.get("/api/oauth/callback", follow_redirects=False,
                  params={"oauth_token": "erfunden", "oauth_verifier": "verifier-xyz"})
        assert r.status_code == 303 and "fehler=" in r.headers["location"]
        assert not r.cookies.get("fg_session")
        # Im Wiki abgebrochen: Anfrage-Token da, aber kein Verifier
        c.get("/api/oauth/login", follow_redirects=False)
        r = c.get("/api/oauth/callback", follow_redirects=False, params={"oauth_token": REQ_TOKEN})
        assert r.status_code == 303 and "abgebrochen" in r.headers["location"]
        assert c.get("/api/me").json()["user"] is None


def test_expired_login_attempts_are_dropped():
    with wiki(), TestClient(server.app) as c:
        c.get("/api/oauth/login", follow_redirects=False)
        server.PENDING[REQ_TOKEN]["expires"] = time.time() - 1
        r = c.get("/api/oauth/callback", follow_redirects=False,
                  params={"oauth_token": REQ_TOKEN, "oauth_verifier": "verifier-xyz"})
        assert r.status_code == 303 and "abgelaufen" in r.headers["location"]


def test_login_survives_an_unreachable_wiki():
    def kaputt(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("keine Verbindung")

    with wiki() as oauth, TestClient(server.app) as c:
        oauth.transport = httpx.MockTransport(kaputt)
        r = c.get("/api/oauth/login", follow_redirects=False)
        assert r.status_code == 303 and "nicht%20erreichbar" in r.headers["location"]


def test_next_stays_on_this_server():
    """Sonst wäre /api/oauth/login?next=… eine Weiterleitung auf beliebige fremde Adressen."""
    assert server._safe_next("/s/abc?weiter=1") == "/s/abc?weiter=1"
    for boese in ["https://boese.example.net", "//boese.example.net", "/\\boese.example.net", ""]:
        assert server._safe_next(boese) == "/"
    with wiki(), TestClient(server.app) as c:
        c.get("/api/oauth/login", follow_redirects=False, params={"next": "//boese.example.net"})
        r = c.get("/api/oauth/callback", follow_redirects=False,
                  params={"oauth_token": REQ_TOKEN, "oauth_verifier": "verifier-xyz"})
        assert r.headers["location"] == "/"


def test_without_a_consumer_the_chat_stays_open():
    """Rein lokaler Betrieb: keine Anmeldung, alle teilen sich ein Konto."""
    assert not server.OAUTH.enabled          # aus der Testumgebung: kein Schlüssel gesetzt
    with TestClient(server.app) as c:
        me = c.get("/api/me").json()
        assert me["auth"] is False and me["user"] is None
        assert c.get("/api/chats").status_code == 200
        assert c.get("/api/oauth/login").status_code == 503


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
