"""Demo mode: no key on the computer; each new conversation gets a session token from the token server,
and resuming that conversation reuses its token."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from google.genai import errors as genai_errors

from voicecompanion import config
from voicecompanion.app import VoiceCompanionApp
from voicecompanion.backends import gemini_backend
from voicecompanion.backends.gemini_backend import fetch_token
from voicecompanion.config import ConfigError, load_profiles, load_settings
from voicecompanion.errors import BackendError, FatalBackendError
from voicecompanion.memory import MemoryStore


@pytest.fixture
def no_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # no .env here
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_no_key_with_a_token_server_means_demo_mode(no_key, monkeypatch):
    assert config.load_api_key("gemini", "https://tokens.example/token") == ""
    with pytest.raises(ConfigError, match="GEMINI_API_KEY is missing"):
        config.load_api_key("gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "real-key")
    assert config.load_api_key("gemini", "https://tokens.example/token") == "real-key"  # a key always wins


@pytest.fixture
def token_server():
    """A local stand-in for token-server/: answers with the status and body set in `reply`."""
    reply = {"status": 200, "body": {"token": "auth_tokens/abc"}}
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(dict(self.headers))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))  # unread bodies can reset the socket
            body = json.dumps(reply["body"]).encode()
            self.send_response(reply["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/token", reply, seen
    server.shutdown()


def test_fetch_token(token_server):
    url, reply, seen = token_server
    assert fetch_token(url) == "auth_tokens/abc"
    assert seen[0]["X-Mi-Asistente"] == "1"  # the server only answers the app

    reply.update(status=429, body={"error": "Too many new sessions from this computer."})
    with pytest.raises(BackendError, match="Too many") as caught:
        fetch_token(url)
    assert not isinstance(caught.value, FatalBackendError)  # the app retries later

    reply.update(status=503, body={"error": "The Mi Asistente demo is turned off."})
    with pytest.raises(FatalBackendError, match="turned off"):
        fetch_token(url)


def test_fetch_token_when_the_server_is_unreachable():
    with pytest.raises(BackendError, match="Can't reach the Mi Asistente demo server"):
        fetch_token("http://127.0.0.1:9/token", timeout=2)


class FakeLive:
    def __init__(self, client):
        self.client = client

    def connect(self, model, config):
        client = self.client

        class Session:
            async def __aenter__(self):
                client.connects.append(config.session_resumption.handle)
                if client.fail:
                    raise client.fail
                return object()

            async def __aexit__(self, *exc):
                pass

        return Session()


class FakeClient:
    made: list["FakeClient"] = []
    fail: Exception | None = None

    def __init__(self, api_key, http_options=None):
        self.api_key, self.connects = api_key, []
        self.aio = type("Aio", (), {"live": FakeLive(self)})()
        FakeClient.made.append(self)


@pytest.fixture
def demo_app(tmp_path, monkeypatch):
    tokens = iter(f"auth_tokens/{n}" for n in range(1, 10))
    monkeypatch.setattr(gemini_backend, "fetch_token", lambda url: next(tokens))
    monkeypatch.setattr(gemini_backend.genai, "Client", FakeClient)
    FakeClient.made, FakeClient.fail = [], None
    settings = load_settings()
    settings.gemini.token_url = "https://tokens.example/token"
    profiles = load_profiles()
    app = VoiceCompanionApp(settings, "", profiles, profiles["Default"], MemoryStore(tmp_path / "m.db"))
    yield app
    app.memory.close()


def test_each_new_conversation_gets_a_token_and_resuming_reuses_it(demo_app):
    backend = demo_app.backend
    assert backend.client is None and backend.token_url and not backend.google_search  # free demo key

    asyncio.run(backend._open(first=True))
    backend.resume_handle = "handle-1"  # Google sent a resumption handle
    asyncio.run(backend._open(first=False))  # the ~10-minute rotation
    assert [c.api_key for c in FakeClient.made] == ["auth_tokens/1"]  # a new token would start from scratch
    assert FakeClient.made[0].connects == [None, "handle-1"]

    backend.forget_session()  # e.g. a new voice: a fresh conversation
    asyncio.run(backend._open(first=False))
    assert [c.api_key for c in FakeClient.made] == ["auth_tokens/1", "auth_tokens/2"]


def test_expired_conversation_token_starts_fresh_with_a_new_one(demo_app, monkeypatch):
    backend = demo_app.backend
    asyncio.run(backend._open(first=True))
    backend.resume_handle = "handle-1"
    FakeClient.fail = genai_errors.APIError(1008, {"message": "API key expired. Please renew the API key."})

    def recover(*args):
        FakeClient.fail = None  # only the old token is refused
        return "auth_tokens/2"

    monkeypatch.setattr(gemini_backend, "fetch_token", recover)
    assert asyncio.run(backend._open(first=False)) is False  # not resumed, but not stopped either
    assert FakeClient.made[-1].api_key == "auth_tokens/2" and backend.resume_handle is None


def test_rejected_new_token_stops_with_a_demo_message(demo_app):
    FakeClient.fail = genai_errors.APIError(1008, {"message": "API key not valid."})
    with pytest.raises(FatalBackendError, match="demo may be turned off"):
        asyncio.run(demo_app.backend._open(first=True))
