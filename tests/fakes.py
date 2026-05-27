from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class Response:
    headers: dict[str, str]


class FakeWSConnection:
    """A scripted websocket connection for deterministic protocol tests.

    `auto_respond` is a `{req: response_template}` map. When the
    integration sends a frame with a matching `req`, this fake fills
    in the request's `ref` (and a `src`, defaulting to `DEVICE_ID`)
    and queues the response as the next frame the integration will
    read from the dispatch iterator. Use it to script the gateway
    `list_devices` + per-device `get_device_info` discovery without
    having to predict the integration's random `ref` values.
    """

    def __init__(
        self,
        recv_messages: list[str | Exception] | None = None,
        iter_messages: list[str | Exception] | None = None,
        headers: dict[str, str] | None = None,
        hold_open: bool = False,
        auto_respond: dict[str, dict] | None = None,
    ) -> None:
        self._recv_messages = list(recv_messages or [])
        self._iter_messages = list(iter_messages or [])
        self._auto_respond = dict(auto_respond or {})
        self.response = Response(headers=headers or {})
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._hold_open = hold_open
        self._closed_event = asyncio.Event()
        self._frame_available = asyncio.Event()
        if self._iter_messages:
            self._frame_available.set()

    async def send(self, payload: str) -> None:
        frame = json.loads(payload)
        self.sent.append(frame)
        req = frame.get("req")
        if req and req in self._auto_respond:
            template = dict(self._auto_respond[req])
            template.setdefault("rsp", req)
            template.setdefault("src", template.get("src", "TAGO_TEST_001"))
            template["ref"] = frame.get("ref")
            # Queue at the head so the integration sees the response
            # before any later scripted events.
            self._iter_messages.insert(0, json.dumps(template))
            self._frame_available.set()

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
        import contextlib as _cl

        while not self._iter_messages:
            if self.closed:
                raise StopAsyncIteration
            if self._hold_open or self._auto_respond:
                # Wait for either an auto-response to land (via send())
                # or the connection to close.
                self._frame_available.clear()
                close_wait = asyncio.create_task(self._closed_event.wait())
                frame_wait = asyncio.create_task(self._frame_available.wait())
                try:
                    done, pending = await asyncio.wait(
                        {close_wait, frame_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (close_wait, frame_wait):
                        if not t.done():
                            t.cancel()
                            with _cl.suppress(asyncio.CancelledError, Exception):
                                await t
                if self.closed and not self._iter_messages:
                    raise StopAsyncIteration
                continue
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
