from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import pytest

from custom_components.tago import TagoNet
from custom_components.tago.TagoNet import TagoGateway
from fakes import FakeWSConnection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEVICE_ID = "TAGO_TEST_001"


def _device_info_with_loads(loads: list[dict]) -> dict:
    return {
        "firmware_rev": "1.0.0",
        "model_num": "dimac8",
        "serial_num": DEVICE_ID,
        "nodes": {"n1": {"type": "dimac", "ch": 8, "loads": loads}},
    }


def _make_ws(
    *,
    iter_messages: list[str | Exception] | None = None,
    loads: list[dict] | None = None,
) -> FakeWSConnection:
    """A fake WS scripted for the gateway+device discovery dance.

    Auto-responds to `list_devices` (one device, available) and
    `get_device_info` (with whichever `loads` the test wants), then
    plays any additional `iter_messages` for the test's per-event
    assertions.
    """
    return FakeWSConnection(
        iter_messages=iter_messages or [],
        auto_respond={
            "list_devices": {"devices": [{"id": DEVICE_ID, "available": True}]},
            "get_device_info": _device_info_with_loads(loads or []),
        },
        hold_open=True,
    )


# ---------------------------------------------------------------------------
# Pure unit tests — no WS involved
# ---------------------------------------------------------------------------

def test_startup_signal_set_once() -> None:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        gateway = TagoGateway("fake.local:1", authkey="k")
        fut = loop.create_future()
        gateway._startup_future = fut

        gateway._signal_startup_success()
        assert fut.done() and fut.exception() is None

        # second signal should be ignored
        gateway._signal_startup_error(RuntimeError("late"))
        assert fut.done() and fut.exception() is None
    finally:
        loop.close()


def test_log_throttle_suppresses_and_resets(monkeypatch) -> None:
    gateway = TagoGateway("fake.local:1", authkey="k")
    logged = []
    t = 100.0

    def fake_exception(fmt, message, err, suffix):
        logged.append((message, str(err), suffix))

    def fake_monotonic():
        return t

    monkeypatch.setattr(TagoNet.logging, "exception", fake_exception)
    monkeypatch.setattr(TagoNet.time, "monotonic", fake_monotonic)

    gateway._log_exception_throttled("k", "msg", RuntimeError("e1"))
    gateway._log_exception_throttled("k", "msg", RuntimeError("e2"))

    assert len(logged) == 1
    assert "suppressed" not in logged[0][2]

    t += 61.0
    gateway._log_exception_throttled("k", "msg", RuntimeError("e3"))
    assert len(logged) == 2
    assert "suppressed 1" in logged[1][2]


@pytest.mark.asyncio
async def test_random_payload_objects_do_not_break_dispatch() -> None:
    gateway = TagoGateway("dummy:1", authkey="k")
    device = TagoNet.TagoDevice(gateway, {"id": "x", "available": True})
    entity = TagoNet.TagoEntity(
        {"id": "x", "type": "unknown", "name": "n", "location": "l"}, device,
    )

    # pseudo-fuzz across varied shapes that are still JSON objects
    for i in range(100):
        payload = {
            "src": "x" if (i % 2 == 0) else "y",
            "evt": "state_changed" if (i % 3 == 0) else "config_changed",
            "noise": i,
            "nested": {"a": i % 5},
        }
        msg = TagoNet.TagoMessage.from_payload(json.dumps(payload))
        handler = entity.handle_message(msg)
        if handler is not None:
            await handler


# ---------------------------------------------------------------------------
# Connect / disconnect lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_during_active_connect(monkeypatch) -> None:
    class HangingCM:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: HangingCM())

    gateway = TagoGateway("fake.local:1", authkey="k")
    connect_task = asyncio.create_task(gateway.connect(timeout=5.0))
    await asyncio.sleep(0.05)
    await gateway.disconnect(timeout=5.0)

    assert gateway._task is None
    assert gateway.is_connected is False
    with contextlib.suppress(Exception):
        await connect_task


@pytest.mark.asyncio
async def test_disconnect_raises_task_exception_when_finished_with_error() -> None:
    gateway = TagoGateway("fake.local:1", authkey="k")

    async def boom():
        raise RuntimeError("task failed")

    t = asyncio.create_task(boom())
    await asyncio.sleep(0)
    gateway._task = t

    with pytest.raises(RuntimeError, match="task failed"):
        await gateway.disconnect(timeout=1.0)


@pytest.mark.asyncio
async def test_connection_task_retries_until_stopped(monkeypatch) -> None:
    sleep_calls = 0
    real_sleep = asyncio.sleep

    class FailingCM:
        async def __aenter__(self):
            raise OSError("down")

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    async def short_sleep(seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        await real_sleep(0)

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: FailingCM())
    monkeypatch.setattr(TagoNet.asyncio, "sleep", short_sleep)

    gateway = TagoGateway("fake.local:1", authkey="k")
    gateway._running = True
    gateway._task = asyncio.create_task(gateway.connection_task())

    await asyncio.sleep(0.05)
    await gateway.disconnect(timeout=5.0)

    assert sleep_calls > 0
    assert gateway._task is None


# ---------------------------------------------------------------------------
# send_request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_request_disconnected_returns_none() -> None:
    gateway = TagoGateway("fake.local:1", authkey="k")
    assert await gateway.send_request(req="ping") is None


@pytest.mark.asyncio
async def test_send_request_contains_req_ref_and_dst() -> None:
    ws = FakeWSConnection()
    gateway = TagoGateway("fake.local:1", authkey="k")
    gateway._ws = ws

    await gateway.send_request(req="hello", data={"x": 1}, dst="node-1")

    assert ws.sent
    payload = ws.sent[0]
    assert payload["req"] == "hello"
    assert payload["dst"] == "node-1"
    assert "ref" in payload
    assert payload["x"] == 1


@pytest.mark.asyncio
async def test_send_request_response_timeout_raises() -> None:
    ws = FakeWSConnection()
    gateway = TagoGateway("fake.local:1", authkey="k")
    gateway._ws = ws

    with pytest.raises(TimeoutError):
        await gateway.send_request(req="wait", responseTimeout=0.01)


# ---------------------------------------------------------------------------
# Dispatch-loop resilience under odd payload shapes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsolicited_unknown_source_message_no_crash(patch_wsconnect) -> None:
    ws = patch_wsconnect(
        _make_ws(
            loads=[{"id": "switch-1", "type": "outlet_onoff",
                    "name": "S", "location": "A", "tag": "S1"}],
            iter_messages=[
                json.dumps({"src": "different", "evt": "state_changed", "state": "ON"}),
            ],
        )
    )

    gateway = TagoGateway("fake.local:1", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)

    assert gateway._task is None
    assert gateway.is_connected is False


@pytest.mark.asyncio
async def test_non_object_json_payloads_do_not_hang_disconnect(patch_wsconnect) -> None:
    ws = patch_wsconnect(
        _make_ws(
            loads=[{"id": "switch-1", "type": "outlet_onoff",
                    "name": "S", "location": "A", "tag": "S1"}],
            iter_messages=["[]", "123", '"x"'],
        )
    )

    gateway = TagoGateway("fake.local:1", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)

    assert gateway._task is None
    assert gateway.is_connected is False


@pytest.mark.asyncio
async def test_large_valid_payload_does_not_stall_shutdown(patch_wsconnect) -> None:
    large_value = "x" * 20000
    ws = patch_wsconnect(
        _make_ws(
            loads=[{"id": "switch-1", "type": "outlet_onoff",
                    "name": "S", "location": "A", "tag": "S1"}],
            iter_messages=[
                json.dumps({"src": "unknown", "evt": "state_changed",
                            "blob": large_value}),
            ],
        )
    )

    gateway = TagoGateway("fake.local:1", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)

    assert gateway._task is None
    assert gateway.is_connected is False
