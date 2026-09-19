#!/usr/bin/env python3
"""
mwoauth.py – Anmeldung über FactGrid (MediaWiki-OAuth 1.0a).

Warum 1.0a und nicht 2.0: FactGrid hat die OAuth-Erweiterung, aber der Token-Endpunkt von
OAuth 2.0 (POST /w/rest.php/oauth2/access_token) antwortet dort mit
`Key path "file://" does not exist or is not readable` – auf dem Wiki ist kein RSA-Schlüssel
für die JWT-Signatur hinterlegt, ohne den OAuth 2.0 keinen Token ausstellen kann. Der Weg
über 1.0a (Special:OAuth/initiate → /authorize → /token → /identify) läuft.

Drei Schritte, alle Anfragen mit HMAC-SHA1 signiert (RFC 5849; MediaWiki kennt für OAuth 1.0a
nur HMAC-SHA1 und RSA-SHA1 – SHA-1 ist hier Protokoll, keine Wahl):

  1. initiate()      Anfrage-Token holen (signiert mit dem Consumer-Geheimnis).
  2. authorize_url() Der Browser geht zum Wiki und bestätigt dort.
  3. access_token()  Verifier gegen den Zugriffs-Token tauschen, dann identify():
                     Special:OAuth/identify liefert ein JWT (HS256, signiert mit dem
                     Consumer-Geheimnis) mit username, groups, blocked …

Der Consumer wird einmal unter Special:OAuthConsumerRegistration/propose angelegt; nötig ist
nur das Recht "Basic rights" (Identität), keine Bearbeitungsrechte. Consumer-Token und
-Geheimnis stehen als FACTGRID_OAUTH_KEY/_SECRET in .env.

Die Rückruf-Adresse wird NICHT mitgeschickt, sondern steht beim Consumer: initiate sendet
oauth_callback=oob. Wer dort keinen Haken bei "Allow consumer to specify a callback in
requests" gesetzt hat, bekommt sonst `mwoauth-callback-not-oob` ("oauth_callback must be set,
and must be set to \"oob\""). Auf oob hin nimmt das Wiki die eingetragene Adresse
(Consumer::generateCallbackUrl: `if ( $callback === 'oob' ) { $callback =
$this->getCallbackUrl(); }`) und hängt oauth_token und oauth_verifier an - der Browser landet
also trotzdem bei /api/oauth/callback. Nur ein Consumer MIT dem Haken darf eine eigene Adresse
schicken; dafür gibt es FACTGRID_OAUTH_CALLBACK.

Nichts hier hängt an FastAPI; der Ablauf ist in chat/server.py verdrahtet und in
tests/test_oauth.py gegen einen httpx-MockTransport (und die Beispielwerte aus der
OAuth-Spezifikation) geprüft.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import httpx


class OAuthError(RuntimeError):
    """Fehler im OAuth-Ablauf – die Meldung ist für die Oberfläche gedacht."""


# ---------------------------------------------------------------------------
# Signatur (RFC 5849)
# ---------------------------------------------------------------------------
def percent(value: str) -> str:
    """Prozent-Kodierung nach RFC 3986: nur A-Z a-z 0-9 - . _ ~ bleiben stehen."""
    return quote(str(value), safe="~")


def base_string(method: str, url: str, params: dict[str, str]) -> str:
    """Signaturbasis: METHODE & URL & alle Parameter (kodiert, sortiert, verkettet)."""
    pairs = sorted((percent(k), percent(v)) for k, v in params.items())
    norm = "&".join(f"{k}={v}" for k, v in pairs)
    return "&".join([method.upper(), percent(url), percent(norm)])


def sign(method: str, url: str, params: dict[str, str],
         consumer_secret: str, token_secret: str = "") -> str:
    """HMAC-SHA1 über die Signaturbasis, Schlüssel sind beide Geheimnisse."""
    key = f"{percent(consumer_secret)}&{percent(token_secret)}".encode()
    digest = hmac.new(key, base_string(method, url, params).encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def signed_params(method: str, url: str, params: dict[str, str], consumer_key: str,
                  consumer_secret: str, token: str = "", token_secret: str = "",
                  nonce: str = "") -> dict[str, str]:
    """Die übergebenen Parameter plus oauth_* plus Signatur – fertig zum Abschicken."""
    p = dict(params)
    p.update({"oauth_consumer_key": consumer_key,
              "oauth_signature_method": "HMAC-SHA1",
              "oauth_timestamp": str(int(time.time())),
              "oauth_nonce": nonce or secrets.token_hex(16),
              "oauth_version": "1.0"})
    if token:
        p["oauth_token"] = token
    p["oauth_signature"] = sign(method, url, p, consumer_secret, token_secret)
    return p


# ---------------------------------------------------------------------------
# Antworten des Wikis
# ---------------------------------------------------------------------------
def _message(text: str) -> str:
    """Fehlertext aus der JSON-Antwort von Special:OAuth (format=json)."""
    try:
        d = json.loads(text)
    except Exception:
        return text.strip()[:300]
    return str(d.get("message") or d.get("error") or text)[:300]


def _tokens(text: str) -> tuple[str, str]:
    """(Token, Geheimnis) aus der Antwort von initiate/token; sonst die Fehlermeldung.

    Zwei Formate: mit format=json antwortet MediaWiki {"key": …, "secret": …,
    "oauth_callback_confirmed": "true"} - so kommt es von FactGrid -, ohne format=json das
    formularkodierte oauth_token=…&oauth_token_secret=… des OAuth-Standards. Beides wird
    gelesen, damit der Ablauf nicht an dieser Kleinigkeit hängt."""
    try:
        data = json.loads(text)
    except Exception:
        data = dict(parse_qsl(text.strip()))
    if not isinstance(data, dict):
        raise OAuthError(_message(text))
    if data.get("key") and data.get("secret"):
        return str(data["key"]), str(data["secret"])
    if data.get("oauth_token") and data.get("oauth_token_secret"):
        return str(data["oauth_token"]), str(data["oauth_token_secret"])
    raise OAuthError(_message(text))


def _b64url(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def verify_identify(jws: str, consumer_key: str, consumer_secret: str, issuer: str,
                    nonce: str, leeway: int = 60) -> dict:
    """JWT von Special:OAuth/identify prüfen und die Angaben zurückgeben.

    Geprüft werden Signatur (HS256 mit dem Consumer-Geheimnis), Empfänger (aud = unser
    Consumer), Aussteller (iss = dieses Wiki), Gültigkeit (iat/exp) und die Nonce der
    Anfrage – ohne sie könnte eine aufgezeichnete Antwort erneut eingespielt werden."""
    parts = jws.strip().split(".")
    if len(parts) != 3:
        raise OAuthError(f"Special:OAuth/identify hat kein JWT geliefert: {_message(jws)}")
    head_b64, payload_b64, sig_b64 = parts
    try:
        head = json.loads(_b64url(head_b64))
        claims = json.loads(_b64url(payload_b64))
        signature = _b64url(sig_b64)
    except Exception:
        raise OAuthError("Identität nicht lesbar (kein gültiges JWT).")
    if head.get("alg") != "HS256":
        raise OAuthError(f"Unerwartetes Signaturverfahren {head.get('alg')!r}.")
    expected = hmac.new(consumer_secret.encode(),
                        f"{head_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise OAuthError("Signatur der Identität stimmt nicht.")
    if not hmac.compare_digest(str(claims.get("aud", "")), consumer_key):
        raise OAuthError("Die Identität ist für einen anderen Consumer ausgestellt.")
    if urlparse(str(claims.get("iss", ""))).netloc != urlparse(issuer).netloc:
        raise OAuthError(f"Die Identität kommt nicht von {urlparse(issuer).netloc}.")
    now = time.time()
    try:
        iat, exp = float(claims.get("iat", 0)), float(claims.get("exp", 0))
    except (TypeError, ValueError):
        raise OAuthError("Die Identität hat keine brauchbare Gültigkeit.")
    if iat > now + leeway or exp < now - leeway:
        raise OAuthError("Die Identität ist abgelaufen – bitte noch einmal anmelden.")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OAuthError("Die Identität gehört nicht zu dieser Anmeldung.")
    return claims


# ---------------------------------------------------------------------------
# Der Ablauf
# ---------------------------------------------------------------------------
class MwOAuth:
    """OAuth-1.0a-Client für ein MediaWiki (hier FactGrid).

    index_url ist der Einstieg (…/w/index.php). callback ist das, was bei initiate
    mitgeschickt wird: "oob" (Vorgabe) heißt "nimm die beim Consumer eingetragene Adresse";
    eine eigene Adresse nimmt das Wiki nur von einem Consumer mit "callback is prefix".
    Ohne Consumer-Token und -Geheimnis ist `enabled` falsch und der Chat läuft wie ohne
    Anmeldung (lokaler Betrieb)."""

    def __init__(self, key: str, secret: str, index_url: str, callback: str = "oob",
                 timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None):
        self.key = (key or "").strip()
        self.secret = (secret or "").strip()
        self.index_url = (index_url or "").strip()
        self.callback = (callback or "").strip()
        self.timeout = timeout
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.key and self.secret and self.index_url)

    @property
    def wiki(self) -> str:
        return urlparse(self.index_url).netloc

    async def _get(self, title: str, extra: dict[str, str], token: str = "",
                   token_secret: str = "", nonce: str = "") -> str:
        params = signed_params("GET", self.index_url, {"title": title, "format": "json", **extra},
                               self.key, self.secret, token, token_secret, nonce)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport,
                                         follow_redirects=True) as client:
                res = await client.get(self.index_url, params=params)
        except httpx.HTTPError as e:
            raise OAuthError(f"{self.wiki} nicht erreichbar ({type(e).__name__}).")
        if res.status_code >= 400:
            raise OAuthError(f"{title}: {_message(res.text)}")
        return res.text

    async def initiate(self) -> tuple[str, str]:
        """Anfrage-Token holen. Gibt (Token, Geheimnis); das Geheimnis bleibt auf dem Server."""
        return _tokens(await self._get("Special:OAuth/initiate",
                                       {"oauth_callback": self.callback or "oob"}))

    def authorize_url(self, request_token: str) -> str:
        """Adresse, an die der Browser geschickt wird – dort bestätigt der Mensch im Wiki."""
        query = urlencode({"title": "Special:OAuth/authorize", "oauth_token": request_token,
                           "oauth_consumer_key": self.key})
        return f"{self.index_url}?{query}"

    async def access_token(self, request_token: str, request_secret: str,
                           verifier: str) -> tuple[str, str]:
        """Verifier gegen den Zugriffs-Token tauschen."""
        text = await self._get("Special:OAuth/token", {"oauth_verifier": verifier},
                               request_token, request_secret)
        return _tokens(text)

    async def identify(self, token: str, token_secret: str) -> dict:
        """Wer hat sich angemeldet: geprüfte Angaben aus Special:OAuth/identify."""
        nonce = secrets.token_hex(16)
        jws = await self._get("Special:OAuth/identify", {}, token, token_secret, nonce)
        return verify_identify(jws, self.key, self.secret, self.index_url, nonce)
