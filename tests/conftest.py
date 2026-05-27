from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from custom_components.tago import TagoNet
from custom_components.tago.TagoNet import TagoGateway
from fakes import FakeWSConnectCM, FakeWSConnection
from fake_server import FakeServer
from scenarios import DEVICE_ID, GROUP_ID, load_all


@pytest.fixture(scope="session")
def wire_scenarios() -> dict:
    """Shared scenario fixtures from tests/vectors/wire_scenarios.json."""
    return load_all()


@pytest_asyncio.fixture
async def fake_server(socket_enabled):
    """Layer B fake firmware fixture.

    `socket_enabled` is from pytest-socket; the HA test plugin re-disables
    sockets in its own `pytest_runtest_setup`, so depending on the
    `socket_enabled` fixture (which runs during fixture setup, after both
    plugins' setup hooks) is the only way to keep sockets open for the
    duration of the test.
    """
    server = FakeServer()
    port = await server.start()
    server.port = port
    try:
        yield server
    finally:
        await server.stop()


@pytest_asyncio.fixture
async def connected_device(fake_server):
    """Real TagoDevice (the first device behind a connected TagoGateway)
    talking to the fake firmware over a real WebSocket."""
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        device = gateway.devices[0] if gateway.devices else None
        yield device, fake_server
    finally:
        await gateway.disconnect(timeout=5.0)


@pytest_asyncio.fixture
async def connected_gateway(fake_server):
    """Real TagoGateway connected to the fake firmware. Use when the
    test needs to talk to the gateway itself or to enumerate devices."""
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        yield gateway, fake_server
    finally:
        await gateway.disconnect(timeout=5.0)


@pytest.fixture
def patch_wsconnect(monkeypatch):
    """Patch TagoNet.wsconnect to return a scripted in-process websocket."""

    def _patch(ws: FakeWSConnection) -> FakeWSConnection:
        def _connect(**kwargs):
            return FakeWSConnectCM(ws)

        monkeypatch.setattr(TagoNet, "wsconnect", _connect)
        return ws

    return _patch


@pytest.fixture
def device_info_payload() -> str:
    """`get_device_info` payload — device-level fields plus the entity
    tree under `nodes`. Mirrors PROTOCOL_PROPOSALS §P8 + §P7."""
    return json.dumps(
        {
            "rsp": "get_device_info",
            "src": DEVICE_ID,
            "firmware_rev": "1.0.0",
            "model_num": "dimac8",
            "serial_num": DEVICE_ID,
            "name": "Test Device",
            "location": "Test Lab",
            "nodes": {
                "n1": {
                    "loads": [
                        {
                            "id": "light-1",
                            "type": "light_dimmable",
                            "name": "Kitchen Main",
                            "location": "Kitchen",
                            "tag": "L1",
                            "brightness": 500,
                        },
                        {
                            "id": "switch-1",
                            "type": "outlet_onoff",
                            "name": "Pump",
                            "location": "Plant",
                            "tag": "S1",
                            "is_on": False,
                        },
                        {
                            "id": "cover-1",
                            "type": "cover_blind",
                            "name": "Blind",
                            "location": "Bedroom",
                            "tag": "C1",
                            "position": 50,
                            "target": 50,
                        },
                        {
                            "id": "fan-1",
                            "type": "fan_onoff",
                            "name": "Ceiling",
                            "location": "Bedroom",
                            "tag": "F1",
                            "is_on": True,
                        },
                        {
                            "id": "unknown-1",
                            "type": "unknown_type",
                            "name": "Mystery",
                            "location": "Lab",
                            "tag": "U1",
                        },
                    ]
                }
            },
        }
    )


@pytest.fixture
def list_devices_payload() -> str:
    """`list_devices` payload — one available device per
    PROTOCOL_PROPOSALS §P8."""
    return json.dumps({
        "rsp": "list_devices",
        "src": DEVICE_ID,
        "devices": [{"id": DEVICE_ID, "available": True}],
    })
