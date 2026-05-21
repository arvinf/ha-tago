from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from custom_components.tago.TagoNet import (
    TagoCover,
    TagoDevice,
    TagoEntity,
    TagoFan,
    TagoLight,
    TagoMessage,
    TagoSwitch,
)
from custom_components.tago import TagoNet
from fakes import FakeWSConnection


def _event_payload(src: str, evt: str, **data) -> str:
    payload = {"src": src, "evt": evt}
    payload.update(data)
    return json.dumps(payload)


def _nodes_payload(loads: list[dict[str, Any]]) -> str:
    return json.dumps({"rsp": "list_nodes", "nodes": {"n1": {"loads": loads}}})


@pytest.mark.asyncio
async def test_connect_and_discover_entities(
    patch_wsconnect, login_ok_payload, nodes_payload
) -> None:
    ws = patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[nodes_payload],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1234", authkey="test-auth")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)

    assert device.serial_num == "SN-1234"
    assert device.model_num == "TAGO-X"
    assert len(device.entities) == 5

    discovered = {type(entity) for entity in device.entities}
    assert TagoLight in discovered
    assert TagoSwitch in discovered
    assert TagoCover in discovered
    assert TagoFan in discovered
    assert TagoEntity in discovered

    assert any(msg.get("req") == "list_nodes" for msg in ws.sent)


@pytest.mark.asyncio
async def test_abrupt_disconnect_does_not_deadlock(
    patch_wsconnect, login_ok_payload, nodes_payload
) -> None:
    ws = patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[nodes_payload, OSError("socket dropped")],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1234", authkey="test-auth")
    await device.connect(timeout=1.0)

    for _ in range(20):
        if not device.is_connected:
            break
        await asyncio.sleep(0.05)

    assert ws.closed or not device.is_connected
    await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_invalid_message_in_stream_does_not_hang(
    patch_wsconnect, login_ok_payload, nodes_payload
) -> None:
    patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[nodes_payload, "this is not json"],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1234", authkey="test-auth")
    await device.connect(timeout=1.0)

    for _ in range(20):
        if not device.is_connected:
            break
        await asyncio.sleep(0.05)

    # Regression guard: malformed payloads should not deadlock shutdown.
    await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_message_routed_to_matching_entity_only() -> None:
    class TrackingEntity(TagoEntity):
        def __init__(self, payload: dict, device: TagoDevice):
            super().__init__(payload, device)
            self.count = 0

        async def handle_state_change(self, msg: TagoMessage) -> None:
            self.count += 1
            await super().handle_state_change(msg)

    device = TagoDevice("dummy:1", authkey="k")
    e1 = TrackingEntity(
        {"id": "e1", "type": "unknown_type", "name": "A", "location": "X"},
        device,
    )
    e2 = TrackingEntity(
        {"id": "e2", "type": "unknown_type", "name": "B", "location": "Y"},
        device,
    )

    msg = TagoMessage.from_payload(_event_payload("e1", TagoEntity.EVT_STATE_CHANGED))

    handlers = []
    for entity in (e1, e2):
        handler = entity.handle_message(msg)
        if handler is not None:
            handlers.append(handler)

    await asyncio.gather(*handlers)

    assert e1.count == 1
    assert e2.count == 0


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (os.getenv("TAGO_LIVE_HOST") and os.getenv("TAGO_LIVE_AUTHKEY")),
    reason="Set TAGO_LIVE_HOST and TAGO_LIVE_AUTHKEY to run live login test",
)
async def test_live_device_login_optional() -> None:
    device = TagoDevice(os.environ["TAGO_LIVE_HOST"], authkey=os.environ["TAGO_LIVE_AUTHKEY"])
    await device.connect(timeout=10.0)
    try:
        assert device.serial_num is not None
        assert device.model_num is not None
    finally:
        await device.disconnect(timeout=10.0)


@pytest.mark.asyncio
async def test_connect_timeout_cleans_up_task(monkeypatch) -> None:
    class HangingCM:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: HangingCM())

    device = TagoDevice("fake.local:1234", authkey="k")
    with pytest.raises(TimeoutError):
        await device.connect(timeout=0.05)

    assert device._task is None
    assert device._startup_future is None
    assert device.is_connected is False


@pytest.mark.asyncio
async def test_repeated_connect_disconnect_cycles_no_task_leak(
    patch_wsconnect, login_ok_payload, nodes_payload
) -> None:
    device = TagoDevice("fake.local:1234", authkey="k")

    for _ in range(10):
        patch_wsconnect(
            FakeWSConnection(
                recv_messages=[login_ok_payload],
                iter_messages=[nodes_payload],
                headers={"x-tago-auth": "legacy"},
            )
        )
        await device.connect(timeout=1.0)
        await device.disconnect(timeout=5.0)

        assert device._task is None
        assert device._startup_future is None
        assert device.is_connected is False


@pytest.mark.asyncio
async def test_double_disconnect_is_idempotent(
    patch_wsconnect, login_ok_payload, nodes_payload
) -> None:
    patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[nodes_payload],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1234", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)
    await device.disconnect(timeout=5.0)

    assert device.is_connected is False
    assert device._task is None


@pytest.mark.asyncio
async def test_concurrent_connect_calls_share_single_startup(login_ok_payload, nodes_payload, monkeypatch) -> None:
    ws = FakeWSConnection(
        recv_messages=[login_ok_payload],
        iter_messages=[nodes_payload, OSError("drop")],
        headers={"x-tago-auth": "legacy"},
    )
    connect_calls = 0

    class _CM:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    def _connect(**kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return _CM()

    monkeypatch.setattr(TagoNet, "wsconnect", _connect)

    device = TagoDevice("fake.local:1234", authkey="k")
    await asyncio.gather(device.connect(timeout=1.0), device.connect(timeout=1.0))
    await device.disconnect(timeout=5.0)

    assert connect_calls == 1


@pytest.mark.asyncio
async def test_entities_toggle_online_offline_and_back_online(monkeypatch, login_ok_payload) -> None:
    first_nodes = _nodes_payload(
        [
            {"id": "light-1", "type": "light_dimmable", "name": "Kitchen", "location": "Kitchen", "tag": "L1"},
            {"id": "switch-1", "type": "relay_switch", "name": "Pump", "location": "Plant", "tag": "S1"},
        ]
    )
    second_nodes = _nodes_payload(
        [
            {"id": "light-1", "type": "light_dimmable", "name": "Kitchen", "location": "Kitchen", "tag": "L1"},
            {"id": "switch-1", "type": "relay_switch", "name": "Pump", "location": "Plant", "tag": "S1"},
        ]
    )

    ws_sessions = [
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[first_nodes],
            headers={"x-tago-auth": "legacy"},
            hold_open=True,
        ),
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[second_nodes],
            headers={"x-tago-auth": "legacy"},
            hold_open=True,
        ),
    ]

    class _CM:
        def __init__(self, ws):
            self._ws = ws

        async def __aenter__(self):
            return self._ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    def _connect(**kwargs):
        if not ws_sessions:
            raise RuntimeError("No websocket sessions left")
        return _CM(ws_sessions.pop(0))

    monkeypatch.setattr(TagoNet, "wsconnect", _connect)

    device = TagoDevice("fake.local:1234", authkey="k")
    try:
        await device.connect(timeout=1.0)
        assert device.entities
        first_entity_ids = [e.unique_id for e in device.entities]
        for entity in device.entities:
            assert entity.is_connected is True

        await device.disconnect(timeout=5.0)
        for entity in device.entities:
            assert entity.is_connected is False

        await device.connect(timeout=1.0)
        for entity in device.entities:
            assert entity.is_connected is True
        assert [e.unique_id for e in device.entities][:2] == first_entity_ids[:2]
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_reconnect_with_topology_addition_does_not_duplicate_existing(monkeypatch, login_ok_payload) -> None:
    first_nodes = _nodes_payload(
        [
            {"id": "light-1", "type": "light_dimmable", "name": "Kitchen", "location": "Kitchen", "tag": "L1"},
            {"id": "switch-1", "type": "relay_switch", "name": "Pump", "location": "Plant", "tag": "S1"},
        ]
    )
    second_nodes = _nodes_payload(
        [
            {"id": "light-1", "type": "light_dimmable", "name": "Kitchen", "location": "Kitchen", "tag": "L1"},
            {"id": "switch-1", "type": "relay_switch", "name": "Pump", "location": "Plant", "tag": "S1"},
            {"id": "fan-1", "type": "fan_onoff", "name": "Ceiling", "location": "Bedroom", "tag": "F1"},
        ]
    )

    ws_sessions = [
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[first_nodes],
            headers={"x-tago-auth": "legacy"},
        ),
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[second_nodes],
            headers={"x-tago-auth": "legacy"},
        ),
    ]

    class _CM:
        def __init__(self, ws):
            self._ws = ws

        async def __aenter__(self):
            return self._ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    def _connect(**kwargs):
        if not ws_sessions:
            raise RuntimeError("No websocket sessions left")
        return _CM(ws_sessions.pop(0))

    monkeypatch.setattr(TagoNet, "wsconnect", _connect)

    device = TagoDevice("fake.local:1234", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)

    initial_ids = {e.unique_id for e in device.entities}
    assert initial_ids == {"light-1", "switch-1"}

    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)

    final_ids = [e.unique_id for e in device.entities]
    assert set(final_ids) == {"light-1", "switch-1", "fan-1"}
    assert final_ids.count("light-1") == 1
    assert final_ids.count("switch-1") == 1


@pytest.mark.asyncio
async def test_disconnect_during_auth_cleans_up_startup_task(monkeypatch) -> None:
    ws = FakeWSConnection(
        recv_messages=[OSError("socket dropped during auth")],
        iter_messages=[],
        headers={"x-tago-auth": "legacy"},
        hold_open=True,
    )

    class _CM:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: _CM())

    device = TagoDevice("fake.local:1234", authkey="k")
    with pytest.raises(OSError):
        await device.connect(timeout=1.0)

    assert device._task is None
    assert device._startup_future is None
    assert device.is_connected is False


@pytest.mark.asyncio
async def test_disconnect_while_waiting_for_list_nodes_cleans_up_startup_task(monkeypatch, login_ok_payload) -> None:
    ws = FakeWSConnection(
        recv_messages=[login_ok_payload],
        iter_messages=[_event_payload("something", "state_changed", state="ON"), OSError("drop before list_nodes")],
        headers={"x-tago-auth": "legacy"},
        hold_open=True,
    )

    class _CM:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: _CM())

    device = TagoDevice("fake.local:1234", authkey="k")
    with pytest.raises(OSError):
        await device.connect(timeout=1.0)

    assert device._task is None
    assert device._startup_future is None
    assert device.is_connected is False


@pytest.mark.asyncio
async def test_malformed_message_flood_does_not_hang_disconnect(patch_wsconnect, login_ok_payload, nodes_payload) -> None:
    malformed = ["{", "not-json", "[]", "}"] * 25
    patch_wsconnect(
        FakeWSConnection(
            recv_messages=[login_ok_payload],
            iter_messages=[nodes_payload, *malformed],
            headers={"x-tago-auth": "legacy"},
        )
    )

    device = TagoDevice("fake.local:1234", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)
    assert device._task is None


@pytest.mark.asyncio
async def test_reconnect_with_topology_removal_drops_missing_entities(monkeypatch, login_ok_payload) -> None:
    first_nodes = _nodes_payload(
        [
            {"id": "light-1", "type": "light_dimmable", "name": "Kitchen", "location": "Kitchen", "tag": "L1"},
            {"id": "switch-1", "type": "relay_switch", "name": "Pump", "location": "Plant", "tag": "S1"},
        ]
    )
    second_nodes = _nodes_payload(
        [
            {"id": "light-1", "type": "light_dimmable", "name": "Kitchen", "location": "Kitchen", "tag": "L1"},
        ]
    )

    ws_sessions = [
        FakeWSConnection(recv_messages=[login_ok_payload], iter_messages=[first_nodes], headers={"x-tago-auth": "legacy"}),
        FakeWSConnection(recv_messages=[login_ok_payload], iter_messages=[second_nodes], headers={"x-tago-auth": "legacy"}),
    ]

    class _CM:
        def __init__(self, ws):
            self._ws = ws

        async def __aenter__(self):
            return self._ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    def _connect(**kwargs):
        if not ws_sessions:
            raise RuntimeError("No websocket sessions left")
        return _CM(ws_sessions.pop(0))

    monkeypatch.setattr(TagoNet, "wsconnect", _connect)

    device = TagoDevice("fake.local:1234", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)
    assert {e.unique_id for e in device.entities} == {"light-1", "switch-1"}

    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)
    assert {e.unique_id for e in device.entities} == {"light-1"}


@pytest.mark.asyncio
async def test_reconnect_with_type_change_replaces_entity_class(monkeypatch, login_ok_payload) -> None:
    first_nodes = _nodes_payload(
        [{"id": "load-1", "type": "relay_switch", "name": "Load", "location": "Area", "tag": "A1"}]
    )
    second_nodes = _nodes_payload(
        [{"id": "load-1", "type": "fan_onoff", "name": "Load", "location": "Area", "tag": "A1"}]
    )

    ws_sessions = [
        FakeWSConnection(recv_messages=[login_ok_payload], iter_messages=[first_nodes], headers={"x-tago-auth": "legacy"}),
        FakeWSConnection(recv_messages=[login_ok_payload], iter_messages=[second_nodes], headers={"x-tago-auth": "legacy"}),
    ]

    class _CM:
        def __init__(self, ws):
            self._ws = ws

        async def __aenter__(self):
            return self._ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    def _connect(**kwargs):
        if not ws_sessions:
            raise RuntimeError("No websocket sessions left")
        return _CM(ws_sessions.pop(0))

    monkeypatch.setattr(TagoNet, "wsconnect", _connect)

    device = TagoDevice("fake.local:1234", authkey="k")
    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)
    assert isinstance(device.entities[0], TagoSwitch)

    await device.connect(timeout=1.0)
    await device.disconnect(timeout=5.0)
    assert isinstance(device.entities[0], TagoFan)
