"""Thin WebSocket client for the OpenAI Realtime API (GA interface).

Docs: https://developers.openai.com/api/docs/guides/realtime-websocket
Endpoint: wss://api.openai.com/v1/realtime?model=<model>, auth with a Bearer header.
"""

from __future__ import annotations

import json
from typing import AsyncIterator
from urllib.parse import quote

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .errors import BackendError as RealtimeError
from .errors import FatalBackendError as FatalRealtimeError


class RealtimeClient:
    def __init__(self, api_key: str, model: str, url: str = "wss://api.openai.com/v1/realtime"):
        self.api_key = api_key
        self.model = model
        self.url = f"{url}?model={quote(model)}"
        self.ws: ClientConnection | None = None

    async def __aenter__(self) -> "RealtimeClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self.ws = await connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                max_size=None,  # audio events can be large
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            detail = _response_detail(exc)
            if status in (401, 403):
                raise FatalRealtimeError(
                    f"OpenAI rejected the API key (HTTP {status}). Check OPENAI_API_KEY in .env "
                    f"and that your account has Realtime access.{detail}"
                ) from exc
            if status in (400, 404):
                raise FatalRealtimeError(
                    f"OpenAI rejected the connection (HTTP {status}). Is the model name '{self.model}' "
                    f"in settings.yaml correct?{detail}"
                ) from exc
            if status == 429:
                raise RealtimeError(f"Rate limited or out of quota (HTTP 429).{detail}") from exc
            raise RealtimeError(f"Server returned HTTP {status}.{detail}") from exc
        except (OSError, TimeoutError) as exc:
            raise RealtimeError(f"Can't reach OpenAI ({exc}). Check your internet connection.") from exc
        except WebSocketException as exc:
            raise RealtimeError(f"WebSocket handshake failed: {exc}") from exc

    async def send(self, event: dict) -> None:
        if self.ws is None:
            raise RealtimeError("Not connected.")
        try:
            await self.ws.send(json.dumps(event))
        except ConnectionClosed as exc:
            raise _closed_error(exc, while_sending=True) from exc

    async def events(self) -> AsyncIterator[dict]:
        if self.ws is None:
            raise RealtimeError("Not connected.")
        try:
            async for raw in self.ws:
                yield json.loads(raw)
        except ConnectionClosed as exc:
            raise _closed_error(exc, while_sending=False) from exc
        # A normal close (code 1000) ends the loop without an exception; it may still carry a reason.
        raise _closed_error_from(self.ws.close_code, self.ws.close_reason, while_sending=False)

    async def close(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            finally:
                self.ws = None


def _response_detail(exc: InvalidStatus) -> str:
    body = getattr(exc.response, "body", b"") or b""
    try:
        message = json.loads(body).get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        message = body[:200].decode("utf-8", "replace")
    return f" Server said: {message}" if message else ""


# Close reasons that mean "fix your setup", not "network hiccup". OpenAI sends these as
# close code 3000 with reasons like "invalid_request_error.invalid_api_key".
_FATAL_CLOSE_HINTS = {
    "invalid_api_key": "OpenAI rejected the API key. Check OPENAI_API_KEY in .env.",
    "insufficient_quota": "Your OpenAI account is out of credit/quota. Check billing on platform.openai.com.",
    "model_not_found": "The model name in settings.yaml was not found or you don't have access to it.",
    "invalid_model": "The model name in settings.yaml is not a valid Realtime model.",
}


def _closed_error(exc: ConnectionClosed, while_sending: bool) -> RealtimeError:
    frame = exc.rcvd or exc.sent
    return _closed_error_from(frame.code if frame else None, frame.reason if frame else "", while_sending)


def _closed_error_from(code: int | None, reason: str | None, while_sending: bool) -> RealtimeError:
    if code is None:
        text = "no close frame, network dropped"
    else:
        text = f"code {code}{': ' + reason if reason else ''}"
        for key, hint in _FATAL_CLOSE_HINTS.items():
            if key in (reason or ""):
                return FatalRealtimeError(f"{hint} ({text})")
    return RealtimeError(f"Connection closed{' while sending' if while_sending else ''} ({text}).")
