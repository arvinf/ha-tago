from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from custom_components.tago.TagoNet import (
    TagoCover,
    TagoEntity,
    TagoFan,
    TagoGateway,
    TagoLight,
    TagoMessage,
    TagoSwitch,
)
from custom_components.tago import TagoNet
from fakes import FakeWSConnection


DEVICE_ID = "TAGO_TEST_001"


def _event_payload(src: str, evt: str, **data) -> str:
    payload = {"src": src, "evt": evt}
    payload.update(data)
    return json.dumps(payload)


def _device_info(loads: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "firmware_rev": "1.0.0",
        "model_num": "TAGO-X",
        "serial_num": DEVICE_ID,
        "name": "Test Device",
        "location": "Test Lab",
        "nodes": {"n1": {"type": "dimac", "ch": 8, "loads": loads}},
    }


def _ws(
    *,
    loads: list[dict[str, Any]],
    iter_messages: list[str | Exception] | None = None,
    hold_open: bool = False,
) -> FakeWSConnection:
    """A fake WS scripted to auto-respond to the gateway's
    `list_devices` + per-device `get_device_info` discovery, then play
    `iter_messages` for the test's per-event assertions."""
    return FakeWSConnection(
        iter_messages=iter_messages or [],
        auto_respond={
            "list_devices": {"devices": [{"id": DEVICE_ID, "available": True}]},
            "get_device_info": _device_info(loads),
        },
        hold_open=hold_open,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_DISCOVERY_LOADS = [
    {"id": "light-1", "type": "light_dimmable", "name": "Kitchen Main",
     "location": "Kitchen", "tag": "L1", "brightness": 500},
    {"id": "switch-1", "type": "outlet_onoff", "name": "Pump",
     "location": "Plant", "tag": "S1", "is_on": False},
    {"id": "cover-1", "type": "cover_blind", "name": "Blind",
     "location": "Bedroom", "tag": "C1", "position": 50, "target": 50},
    {"id": "fan-1", "type": "fan_onoff", "name": "Ceiling",
     "location": "Bedroom", "tag": "F1", "is_on": True},
    {"id": "unknown-1", "type": "unknown_type", "name": "Mystery",
     "location": "Lab", "tag": "U1"},
]


@pytest.mark.asyncio
async def test_connect_and_discover_entities(patch_wsconnect) -> None:
    ws = patch_wsconnect(_ws(loads=_DISCOVERY_LOADS, hold_open=True))

    gateway = TagoGateway("fake.local:1234", authkey="test-auth")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)

    assert gateway.devices, "gateway should report at least one device"
    device = gateway.devices[0]
    assert device.serial_num == DEVICE_ID
    assert device.model_num == "TAGO-X"
    assert len(device.entities) == 5

    discovered = {type(entity) for entity in device.entities}
    assert TagoLight in discovered
    assert TagoSwitch in discovered
    assert TagoCover in discovered
    assert TagoFan in discovered
    assert TagoEntity in discovered

    assert any(msg.get("req") == "list_devices" for msg in ws.sent)
    assert any(msg.get("req") == "get_device_info" for msg in ws.sent)


@pytest.mark.asyncio
async def test_abrupt_disconnect_does_not_deadlock(patch_wsconnect) -> None:
    ws = patch_wsconnect(_ws(loads=_DISCOVERY_LOADS, hold_open=True))

    gateway = TagoGateway("fake.local:1234", authkey="test-auth")
    await gateway.connect(timeout=2.0)
    assert gateway.is_connected

    # Simulate the firmware abruptly dropping the socket after
    # discovery completed.
    await ws.close()

    for _ in range(40):
        if not gateway.is_connected:
            break
        await asyncio.sleep(0.05)

    assert ws.closed
    await gateway.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_invalid_message_in_stream_does_not_hang(patch_wsconnect) -> None:
    patch_wsconnect(_ws(
        loads=_DISCOVERY_LOADS,
        iter_messages=["this is not json"],
        hold_open=True,
    ))

    gateway = TagoGateway("fake.local:1234", authkey="test-auth")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_message_routed_to_matching_entity_only() -> None:
    class TrackingEntity(TagoEntity):
        def __init__(self, payload: dict, device):
            self.count = 0
            super().__init__(payload, device)

        def handle_state_change(self, data: dict) -> None:
            self.count += 1
            super().handle_state_change(data)

    gateway = TagoGateway("dummy:1", authkey="k")
    device = TagoNet.TagoDevice(gateway, {"id": "x", "available": True})
    e1 = TrackingEntity(
        {"id": "e1", "type": "unknown_type", "name": "A", "location": "X"},
        device,
    )
    e2 = TrackingEntity(
        {"id": "e2", "type": "unknown_type", "name": "B", "location": "Y"},
        device,
    )
    e1.count = 0
    e2.count = 0

    msg = TagoMessage.from_payload(_event_payload("e1", TagoEntity.EVT_STATE_CHANGED))

    handlers = []
    for entity in (e1, e2):
        handler = entity.handle_message(msg)
        if handler is not None:
            handlers.append(handler)

    await asyncio.gather(*handlers)

    assert e1.count == 1
    assert e2.count == 0


# ---------------------------------------------------------------------------
# Live device fixture-gated test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.skipif(
    not (os.getenv("TAGO_LIVE_HOST") and os.getenv("TAGO_LIVE_AUTHKEY")),
    reason="Set TAGO_LIVE_HOST and TAGO_LIVE_AUTHKEY to run live login test",
)
async def test_live_device_login_optional() -> None:
    gateway = TagoGateway(os.environ["TAGO_LIVE_HOST"], authkey=os.environ["TAGO_LIVE_AUTHKEY"])
    await gateway.connect(timeout=10.0)
    try:
        assert gateway.devices
        device = gateway.devices[0]
        assert device.serial_num is not None
        assert device.model_num is not None
    finally:
        await gateway.disconnect(timeout=10.0)


# ---------------------------------------------------------------------------
# Connect lifecycle edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_timeout_cleans_up_task(monkeypatch) -> None:
    class HangingCM:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: HangingCM())

    gateway = TagoGateway("fake.local:1234", authkey="k")
    with pytest.raises(TimeoutError):
        await gateway.connect(timeout=0.05)

    assert gateway._task is None
    assert gateway._startup_future is None
    assert gateway.is_connected is False


@pytest.mark.asyncio
async def test_repeated_connect_disconnect_cycles_no_task_leak(patch_wsconnect) -> None:
    gateway = TagoGateway("fake.local:1234", authkey="k")

    for _ in range(10):
        patch_wsconnect(_ws(loads=_DISCOVERY_LOADS, hold_open=True))
        await gateway.connect(timeout=2.0)
        await gateway.disconnect(timeout=5.0)

        assert gateway._task is None
        assert gateway._startup_future is None
        assert gateway.is_connected is False


@pytest.mark.asyncio
async def test_double_disconnect_is_idempotent(patch_wsconnect) -> None:
    patch_wsconnect(_ws(loads=_DISCOVERY_LOADS, hold_open=True))

    gateway = TagoGateway("fake.local:1234", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)
    await gateway.disconnect(timeout=5.0)

    assert gateway.is_connected is False
    assert gateway._task is None


@pytest.mark.asyncio
async def test_concurrent_connect_calls_share_single_startup(monkeypatch) -> None:
    ws = _ws(loads=_DISCOVERY_LOADS, hold_open=True)
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

    gateway = TagoGateway("fake.local:1234", authkey="k")
    await asyncio.gather(gateway.connect(timeout=2.0), gateway.connect(timeout=2.0))
    await gateway.disconnect(timeout=5.0)

    assert connect_calls == 1


@pytest.mark.asyncio
async def test_entities_toggle_online_offline_and_back_online(monkeypatch) -> None:
    loads = [
        {"id": "light-1", "type": "light_dimmable", "name": "Kitchen",
         "location": "Kitchen", "tag": "L1"},
        {"id": "switch-1", "type": "outlet_onoff", "name": "Pump",
         "location": "Plant", "tag": "S1"},
    ]

    ws_sessions = [
        _ws(loads=loads, hold_open=True),
        _ws(loads=loads, hold_open=True),
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

    gateway = TagoGateway("fake.local:1234", authkey="k")
    try:
        await gateway.connect(timeout=2.0)
        device = gateway.devices[0]
        assert device.entities
        first_entity_ids = [e.unique_id for e in device.entities]
        for entity in device.entities:
            assert entity.is_connected is True

        await gateway.disconnect(timeout=5.0)
        for entity in device.entities:
            assert entity.is_connected is False

        await gateway.connect(timeout=2.0)
        for entity in device.entities:
            assert entity.is_connected is True
        assert [e.unique_id for e in device.entities][:2] == first_entity_ids[:2]
    finally:
        await gateway.disconnect(timeout=5.0)


# ---------------------------------------------------------------------------
# Frozen-topology contract (D7)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconnect_keeps_topology_frozen_when_device_adds_entities(monkeypatch) -> None:
    """Per D7 (config frozen) the host re-reads `list_devices` on every
    reconnect (to refresh `available`) but does NOT re-run
    `get_device_info`. New entities reported on a reconnect-only
    discovery never reach the host until reload."""
    first_loads = [
        {"id": "light-1", "type": "light_dimmable", "name": "Kitchen",
         "location": "Kitchen", "tag": "L1"},
        {"id": "switch-1", "type": "outlet_onoff", "name": "Pump",
         "location": "Plant", "tag": "S1"},
    ]
    second_loads = first_loads + [
        {"id": "fan-1", "type": "fan_onoff", "name": "Ceiling",
         "location": "Bedroom", "tag": "F1"},
    ]

    ws_sessions = [
        _ws(loads=first_loads),
        _ws(loads=second_loads),
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

    gateway = TagoGateway("fake.local:1234", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)

    device = gateway.devices[0]
    initial_ids = {e.unique_id for e in device.entities}
    assert initial_ids == {"light-1", "switch-1"}

    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)

    final_ids = [e.unique_id for e in device.entities]
    assert set(final_ids) == {"light-1", "switch-1"}
    assert final_ids.count("light-1") == 1
    assert final_ids.count("switch-1") == 1


@pytest.mark.asyncio
async def test_reconnect_keeps_topology_frozen_when_device_drops_entities(monkeypatch) -> None:
    """Mirror of the addition case: if a reconnect's get_device_info
    response (were it actually re-fetched) would omit an entity, the
    host retains the original. Surfaces only after reload."""
    first_loads = [
        {"id": "light-1", "type": "light_dimmable", "name": "Kitchen",
         "location": "Kitchen", "tag": "L1"},
        {"id": "switch-1", "type": "outlet_onoff", "name": "Pump",
         "location": "Plant", "tag": "S1"},
    ]
    second_loads = [
        {"id": "light-1", "type": "light_dimmable", "name": "Kitchen",
         "location": "Kitchen", "tag": "L1"},
    ]

    ws_sessions = [
        _ws(loads=first_loads),
        _ws(loads=second_loads),
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

    gateway = TagoGateway("fake.local:1234", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)
    device = gateway.devices[0]
    assert {e.unique_id for e in device.entities} == {"light-1", "switch-1"}

    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)
    assert {e.unique_id for e in device.entities} == {"light-1", "switch-1"}


@pytest.mark.asyncio
async def test_reconnect_keeps_entity_class_frozen_across_type_change(monkeypatch) -> None:
    """A device-side type change is a config change (D7) — it doesn't
    re-classify the entity on the host. The original Python class is
    retained until the integration is reloaded."""
    first_loads = [
        {"id": "load-1", "type": "outlet_onoff", "name": "Load",
         "location": "Area", "tag": "A1"},
    ]
    second_loads = [
        {"id": "load-1", "type": "fan_onoff", "name": "Load",
         "location": "Area", "tag": "A1"},
    ]

    ws_sessions = [
        _ws(loads=first_loads),
        _ws(loads=second_loads),
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

    gateway = TagoGateway("fake.local:1234", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)
    assert isinstance(gateway.devices[0].entities[0], TagoSwitch)

    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)
    # Still a TagoSwitch — the new type is not applied within this device's lifetime.
    assert isinstance(gateway.devices[0].entities[0], TagoSwitch)


# ---------------------------------------------------------------------------
# Failure-during-connect handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_propagates_transport_error_and_cleans_up(monkeypatch) -> None:
    """When the WS upgrade fails with a transport-layer OSError, the
    error is surfaced via the startup future to `connect()` and the
    connection manager is fully torn down."""
    class FailingCM:
        async def __aenter__(self):
            raise OSError("socket dropped during upgrade")

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: FailingCM())

    gateway = TagoGateway("fake.local:1234", authkey="k")
    with pytest.raises(OSError, match="socket dropped"):
        await gateway.connect(timeout=2.0)

    assert gateway._task is None
    assert gateway._startup_future is None
    assert gateway.is_connected is False


@pytest.mark.asyncio
async def test_disconnect_while_waiting_for_discovery_cleans_up_startup_task(monkeypatch) -> None:
    """ws opens, but no list_devices response ever arrives. connect()
    must time out cleanly without leaking the connection task."""
    ws = FakeWSConnection(
        iter_messages=[_event_payload("something", "state_changed", state="ON")],
        hold_open=True,
    )

    class _CM:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(TagoNet, "wsconnect", lambda **kwargs: _CM())

    gateway = TagoGateway("fake.local:1234", authkey="k")
    with pytest.raises(TimeoutError):
        await gateway.connect(timeout=0.3)

    assert gateway._task is None
    assert gateway._startup_future is None
    assert gateway.is_connected is False


@pytest.mark.asyncio
async def test_malformed_message_flood_does_not_hang_disconnect(patch_wsconnect) -> None:
    malformed = ["{", "not-json", "[]", "}"] * 25
    patch_wsconnect(_ws(
        loads=_DISCOVERY_LOADS,
        iter_messages=malformed,
        hold_open=True,
    ))

    gateway = TagoGateway("fake.local:1234", authkey="k")
    await gateway.connect(timeout=2.0)
    await gateway.disconnect(timeout=5.0)
    assert gateway._task is None
