from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from types import SimpleNamespace

import pytest

from custom_components.tago import TagoNet
from custom_components.tago.TagoNet import TagoDevice
from fakes import FakeWSConnection


def _nodes(loads: list[dict]) -> str:
    return json.dumps({"rsp": "list_nodes", "nodes": {"n1": {"loads": loads}}})


@pytest.mark.asyncio
async def test_auth_strategy_selection_matrix(caplog) -> None:
    device = TagoDevice("fake.local:1", authkey="k")

    assert device._select_auth_strategy({}) == device.AUTH_LEGACY
    assert device._select_auth_strategy({device.AUTH_HEADER: "legacy"}) == device.AUTH_LEGACY
    assert (
        device._select_auth_strategy({device.AUTH_HEADER: "hmac_tls_v2,nonce-v2"})
        == device.AUTH_HMAC_TLS_V2
    )

    with caplog.at_level(logging.WARNING):
        chosen = device._select_auth_strategy({device.AUTH_HEADER: "future_mode"})
    assert chosen == device.AUTH_LEGACY
    assert "Unsupported auth mode" in caplog.text


@pytest.mark.asyncio
async def test_get_server_handshake_headers_missing_parts() -> None:
    device = TagoDevice("fake.local:1", authkey="k")

    ws_no_response = SimpleNamespace()
    assert device._get_server_handshake_headers(ws_no_response) == {}

    ws_no_headers = SimpleNamespace(response=SimpleNamespace())
    assert device._get_server_handshake_headers(ws_no_headers) == {}

    ws_headers = SimpleNamespace(response=SimpleNamespace(headers={"X-Tago-Auth": "legacy"}))
    assert device._get_server_handshake_headers(ws_headers) == {"x-tago-auth": "legacy"}


@pytest.mark.asyncio
async def test_authenticate_legacy_missing_nonce_fails() -> None:
    ws = FakeWSConnection(
        recv_messages=[json.dumps({"status": 401})],
        iter_messages=[],
    )
    device = TagoDevice("fake.local:1", authkey="k")

    with pytest.raises(PermissionError, match="No login message"):
        await device._authenticate_legacy(ws)


@pytest.mark.asyncio
async def test_authenticate_legacy_second_status_failure() -> None:
    ws = FakeWSConnection(
        recv_messages=[
            json.dumps({"status": 401, "nonce": "server-nonce"}),
            json.dumps({"status": 401}),
        ],
        iter_messages=[],
    )
    device = TagoDevice("fake.local:1", authkey="k")

    with pytest.raises(PermissionError, match="Legacy login failed"):
        await device._authenticate_legacy(ws)


@pytest.mark.asyncio
async def test_authenticate_legacy_allows_missing_identity_fields() -> None:
    ws = FakeWSConnection(
        recv_messages=[json.dumps({"status": 200})],
        iter_messages=[],
    )
    device = TagoDevice("fake.local:1", authkey="k")

    login_data = await device._authenticate_legacy(ws)
    assert login_data["serialnum"] is None
    assert login_data["model"] is None
    assert login_data["firmware"] is None


def test_startup_signal_set_once() -> None:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        device = TagoDevice("fake.local:1", authkey="k")
        fut = loop.create_future()
        device._startup_future = fut

        device._signal_startup_success()
        assert fut.done() and fut.exception() is None

        # second signal should be ignored
        device._signal_startup_error(RuntimeError("late"))
        assert fut.done() and fut.exception() is None
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_disconnect_during_active_connect(monkeypatch) -> None:
    class HangingCM:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: HangingCM())

    device = TagoDevice("fake.local:1", authkey="k")
    connect_task = asyncio.create_task(device.connect(timeout=5.0))
    await asyncio.sleep(0.05)
    await device.disconnect(timeout=5.0)

    assert device._task is None
    assert device.is_connected is False
    with contextlib.suppress(Exception):
        await connect_task


@pytest.mark.asyncio
async def test_disconnect_raises_task_exception_when_finished_with_error() -> None:
    device = TagoDevice("fake.local:1", authkey="k")

    async def boom():
        raise RuntimeError("task failed")

    t = asyncio.create_task(boom())
    await asyncio.sleep(0)
    device._task = t

    with pytest.raises(RuntimeError, match="task failed"):
        await device.disconnect(timeout=1.0)


@pytest.mark.asyncio
async def test_send_request_disconnected_returns_none() -> None:
    device = TagoDevice("fake.local:1", authkey="k")
    assert await device.send_request(req="ping") is None


@pytest.mark.asyncio
async def test_send_request_contains_req_ref_and_dst() -> None:
    ws = FakeWSConnection(recv_messages=[], iter_messages=[])
    device = TagoDevice("fake.local:1", authkey="k")
    device._ws = ws

    await device.send_request(req="hello", data={"x": 1}, dst="node-1")

    assert ws.sent
    payload = ws.sent[0]
    assert payload["req"] == "hello"
    assert payload["dst"] == "node-1"
    assert "ref" in payload
    assert payload["x"] == 1


@pytest.mark.asyncio
async def test_send_request_response_timeout_raises() -> None:
    ws = FakeWSConnection(recv_messages=[], iter_messages=[])
    device = TagoDevice("fake.local:1", authkey="k")
    device._ws = ws

    with pytest.raises(TimeoutError):
        await device.send_request(req="wait", responseTimeout=0.01)


@pytest.mark.asyncio
async def test_out_of_order_events_before_list_nodes_ignored(patch_wsconnect, login_ok_payload) -> None:
    ws = patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[
                json.dumps({"src": "unknown", "evt": "state_changed", "state": "ON"}),
                json.dumps({"src": "unknown", "evt": "config_changed"}),
                _nodes([{"id": "switch-1", "type": "relay_switch", "name": "S", "location": "A", "tag": "S1"}]),
            ],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)

    assert any(msg.get("req") == "list_nodes" for msg in ws.sent)
    assert len(device.entities) == 1


@pytest.mark.asyncio
async def test_unsolicited_unknown_source_message_no_crash(patch_wsconnect, login_ok_payload) -> None:
    nodes = _nodes([{"id": "switch-1", "type": "relay_switch", "name": "S", "location": "A", "tag": "S1"}])
    ws = patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[
                nodes,
                json.dumps({"src": "different", "evt": "state_changed", "state": "ON"}),
            ],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)

    assert device._task is None
    assert device.is_connected is False


@pytest.mark.asyncio
async def test_non_object_json_payloads_do_not_hang_disconnect(patch_wsconnect, login_ok_payload) -> None:
    nodes = _nodes([{"id": "switch-1", "type": "relay_switch", "name": "S", "location": "A", "tag": "S1"}])
    ws = patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[nodes, "[]", "123", '"x"'],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)

    assert device._task is None
    assert device.is_connected is False


@pytest.mark.asyncio
async def test_large_valid_payload_does_not_stall_shutdown(patch_wsconnect, login_ok_payload) -> None:
    nodes = _nodes([{"id": "switch-1", "type": "relay_switch", "name": "S", "location": "A", "tag": "S1"}])
    large_value = "x" * 20000
    ws = patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[nodes, json.dumps({"src": "unknown", "evt": "state_changed", "blob": large_value})],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)

    assert device._task is None
    assert device.is_connected is False


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

    device = TagoDevice("fake.local:1", authkey="k")
    device._running = True
    device._task = asyncio.create_task(device.connection_task())

    await asyncio.sleep(0.05)
    await device.disconnect(timeout=5.0)

    assert sleep_calls > 0
    assert device._task is None


def test_log_throttle_suppresses_and_resets(monkeypatch) -> None:
    device = TagoDevice("fake.local:1", authkey="k")
    logged = []
    t = 100.0

    def fake_exception(fmt, message, err, suffix):
        logged.append((message, str(err), suffix))

    def fake_monotonic():
        return t

    monkeypatch.setattr(TagoNet.logging, "exception", fake_exception)
    monkeypatch.setattr(TagoNet.time, "monotonic", fake_monotonic)

    device._log_exception_throttled("k", "msg", RuntimeError("e1"))
    device._log_exception_throttled("k", "msg", RuntimeError("e2"))

    assert len(logged) == 1
    assert "suppressed" not in logged[0][2]

    t += 61.0
    device._log_exception_throttled("k", "msg", RuntimeError("e3"))
    assert len(logged) == 2
    assert "suppressed 1" in logged[1][2]


@pytest.mark.asyncio
async def test_random_payload_objects_do_not_break_dispatch() -> None:
    device = TagoDevice("dummy:1", authkey="k")
    entity = TagoNet.TagoEntity({"id": "x", "type": "unknown", "name": "n", "location": "l"}, device)

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
