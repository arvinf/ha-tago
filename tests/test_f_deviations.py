"""Protocol-correct behaviors for the request paths that CLIENT_TEST_GUIDE §5
previously flagged as firmware deviations (F1–F6). Under the working
assumption that the C firmware is bug-free these are simply positive
behavior tests against the protocol.

Naming kept as `test_protocol_*` for searchability against historical
CLIENT_TEST_GUIDE §5 references — none of these xfail.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest

from custom_components.tago.TagoNet import TagoDevice
from scenarios import DEVICE_ID

L0 = "TAGO_TEST_001L1_0"

pytestmark = [pytest.mark.enable_socket]


async def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.01):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _send_raw(device, server, frame: dict, timeout: float = 2.0) -> dict:
    ref = frame.get("ref") or f"f-{uuid.uuid4().hex[:6]}"
    frame["ref"] = ref
    await device._ws.send(json.dumps(frame))
    await _wait_for(
        lambda: any(m.get("ref") == ref and "rsp" in m for m in server.sent),
        timeout=timeout,
    )
    return next(m for m in server.sent if m.get("ref") == ref and "rsp" in m)


# =====================================================================
# §12.4–§12.6 — turn_on/turn_off/toggle for on/off entities emit
# state_changed.
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_emitted_on_turn_on_for_onoff(fake_server):
    fake_server.seed({L0: {"type": "light_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        fake_server.sent.clear()
        await _send_raw(device, fake_server, {"req": "turn_on", "dst": L0})
        await _wait_for(
            lambda: any(e.get("evt") == "state_changed" and e.get("src") == L0
                        for e in fake_server.sent),
            timeout=1.0,
        )
        evts = [e for e in fake_server.sent if e.get("evt") == "state_changed"]
        assert evts and evts[0].get("is_on") is True
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# §13.8 — stop_ramp emits state_changed.
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_emitted_on_stop_ramp(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        await _send_raw(device, fake_server,
                        {"req": "set_light", "dst": L0, "brightness": 1000, "duration": 2000})
        await asyncio.sleep(0.05)
        fake_server.sent.clear()
        await _send_raw(device, fake_server, {"req": "stop_ramp", "dst": L0})
        await _wait_for(
            lambda: any(e.get("evt") == "state_changed" for e in fake_server.sent),
            timeout=1.0,
        )
        assert any(e.get("evt") == "state_changed" for e in fake_server.sent)
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# §7a — set_config accepts multi-channel types.
# =====================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("type_str", ["light_ww", "light_rgb", "light_rgbw", "light_rgbww"])
async def test_set_config_accepts_multichannel_types(fake_server, type_str):
    fake_server.seed({L0: {"type": "light_dimmable"}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        reply = await _send_raw(device, fake_server,
                                {"req": "set_config", "dst": L0, "type": type_str})
        assert reply["status"] == 200
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# §9 — regen_api_key returns a 32-char lowercase hex string.
# =====================================================================

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.mark.asyncio
async def test_regen_api_key_returns_hex32(fake_server):
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        reply = await _send_raw(device, fake_server, {"req": "regen_api_key"})
        assert _HEX32.match(reply["api_key"])
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# §12.3 — get_state on on/off entities returns canonical is_on.
# =====================================================================

@pytest.mark.asyncio
async def test_get_state_after_turn_on_reports_is_on_true(fake_server):
    fake_server.seed({L0: {"type": "light_onoff", "is_on": False, "brightness": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        await _send_raw(device, fake_server, {"req": "turn_on", "dst": L0})
        reply = await _send_raw(device, fake_server, {"req": "get_state", "dst": L0})
        assert reply["is_on"] is True
    finally:
        await device.disconnect(timeout=5.0)
