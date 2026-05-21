from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class Response:
    headers: dict[str, str]


class FakeWSConnection:
    """A scripted websocket connection for deterministic protocol tests."""

    def __init__(
        self,
        recv_messages: list[str | Exception],
        iter_messages: list[str | Exception],
        headers: dict[str, str] | None = None,
        hold_open: bool = False,
    ) -> None:
        self._recv_messages = list(recv_messages)
        self._iter_messages = list(iter_messages)
        self.response = Response(headers=headers or {})
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._hold_open = hold_open
        self._closed_event = asyncio.Event()

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if not self._recv_messages:
            raise RuntimeError("No scripted recv messages left")

        next_msg = self._recv_messages.pop(0)
        if isinstance(next_msg, Exception):
            raise next_msg

        return next_msg

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._iter_messages:
            if self._hold_open and not self.closed:
                await self._closed_event.wait()
            raise StopAsyncIteration

        next_msg = self._iter_messages.pop(0)
        if isinstance(next_msg, Exception):
            raise next_msg

        return next_msg


class FakeWSConnectCM:
    def __init__(self, ws: FakeWSConnection) -> None:
        self._ws = ws

    async def __aenter__(self) -> FakeWSConnection:
        return self._ws

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False
