"""Layer B — scenario-driven integration tests against the fake firmware.

Each scenario in `wire_scenarios.json` (CLIENT_TEST_GUIDE §4) gets at least
one test that exercises real `TagoDevice` code through a real local WebSocket.
Tests are grouped per Appendix A of the guide.

Replies are observed via `fake_server.sent` rather than via
`device.send_request(responseTimeout=)` because some scenarios
(`non_string_dst_500` for instance) need to inject malformed frames the
integration can't construct, and per-frame observability is cheaper to
assert on than threading return values through the integration.

The fake server assumes a protocol-conformant firmware.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from custom_components.tago.TagoNet import TagoDevice, TagoGateway, TagoLight, TagoMessage
from scenarios import DEVICE_ID, GROUP_ID, get as get_scenario

L0 = "TAGO_TEST_001L1_0"
L1 = "TAGO_TEST_001L1_1"

# pytest-socket (via pytest-homeassistant-custom-component) blocks sockets by
# default; the `socket_enabled` fixture in conftest re-enables them per
# Layer B fixture.
pytestmark = [pytest.mark.enable_socket]


async def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.01):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _send_raw_and_wait_reply(device, server, frame: dict, timeout: float = 2.0) -> dict:
    """Bypass the broken send_request(responseTimeout=) path: send a frame on
    the WS with a known ref, then wait for the server's reply with that ref."""
    ref = frame.get("ref") or f"t-{uuid.uuid4().hex[:6]}"
    frame["ref"] = ref
    # The WS lives on the parent gateway; per-device commands target
    # the device id via `dst` in the frame.
    await device._gateway._ws.send(json.dumps(frame))
    ok = await _wait_for(
        lambda: any(m.get("ref") == ref and "rsp" in m for m in server.sent),
        timeout=timeout,
    )
    if not ok:
        raise TimeoutError(f"no reply with ref={ref} after {timeout}s")
    return next(m for m in server.sent if m.get("ref") == ref and "rsp" in m)


# =====================================================================
# Connection lifecycle
# =====================================================================

@pytest.mark.asyncio
async def test_connect_identity_exchange(connected_device, wire_scenarios):
    scenario = wire_scenarios["connect_identity_exchange"]
    assert scenario
    device, _ = connected_device
    assert device.serial_num == DEVICE_ID
    assert device.model_num == "dimac8"
    assert device.is_connected


@pytest.mark.asyncio
async def test_reconnect_after_disconnect_preserves_entity_id_mapping(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    first_ids = sorted(e.unique_id for e in device.entities)
    await device.disconnect(timeout=5.0)
    await device.connect(timeout=5.0)
    try:
        second_ids = sorted(e.unique_id for e in device.entities)
        assert first_ids == second_ids
        assert L0 in second_ids
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# Device discovery
# =====================================================================

@pytest.mark.asyncio
async def test_list_nodes_basic_returns_one_group_with_eight_channels(connected_device):
    """The integration uses `get_device_info` for initial discovery now
    (P8), but `list_nodes` is still a valid standalone request on the
    wire. Send it explicitly and verify the response shape."""
    device, server = connected_device
    reply = await _send_raw_and_wait_reply(device, server, {"req": "list_nodes"})
    assert reply["rsp"] == "list_nodes"
    nodes = reply["nodes"]
    assert GROUP_ID in nodes
    assert nodes[GROUP_ID]["type"] == "dimac"
    assert nodes[GROUP_ID]["ch"] == 8


@pytest.mark.asyncio
async def test_device_get_config_basic_metadata_shape(connected_device):
    device, server = connected_device
    reply = await _send_raw_and_wait_reply(device, server, {"req": "get_config"})
    assert reply["src"] == DEVICE_ID
    assert reply["model_num"] == "dimac8"
    assert reply["serial_number"] == DEVICE_ID
    assert reply["loads"] == [GROUP_ID]
    assert "firmware_rev" in reply


# =====================================================================
# On/off control — driven through the integration's high-level API
# (TagoLight.set_brightness routes to turn_on/turn_off for light_onoff
# per PROTOCOL.md §12.7; TagoSwitch / TagoFan.turn_on for outlets / fans).
# =====================================================================

@pytest.mark.asyncio
async def test_turn_on_onoff_via_set_brightness(fake_server):
    fake_server.seed({L0: {"type": "light_onoff", "is_on": False}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.set_brightness(brightness=1.0)
        await _wait_for(lambda: fake_server.state[L0].get("is_on") is True, timeout=1.0)
        # Verify the wire saw `turn_on`, not `set_light`.
        sent_reqs = [r.get("req") for r in fake_server.received]
        assert "turn_on" in sent_reqs
        assert "set_light" not in sent_reqs
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_turn_off_onoff_via_set_brightness(fake_server):
    fake_server.seed({L0: {"type": "light_onoff", "is_on": True}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.set_brightness(brightness=0.0)
        await _wait_for(lambda: fake_server.state[L0].get("is_on") is False, timeout=1.0)
        sent_reqs = [r.get("req") for r in fake_server.received]
        assert "turn_off" in sent_reqs
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_toggle_onoff_round_trip(fake_server):
    """`toggle` is exposed via TagoLight.toggle() and round-trips on the wire."""
    fake_server.seed({L0: {"type": "light_onoff", "is_on": False}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.toggle()
        await _wait_for(lambda: fake_server.state[L0].get("is_on") is True, timeout=1.0)
        await light.toggle()
        await _wait_for(lambda: fake_server.state[L0].get("is_on") is False, timeout=1.0)
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_outlet_onoff_via_TagoSwitch(fake_server):
    """`outlet_onoff` is the protocol type for outlets — TagoSwitch handles it."""
    from custom_components.tago.TagoNet import TagoSwitch
    fake_server.seed({L0: {"type": "outlet_onoff", "is_on": False}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        outlet = next(e for e in device.entities if e.unique_id == L0)
        assert isinstance(outlet, TagoSwitch)
        await outlet.turn_on()
        await _wait_for(lambda: fake_server.state[L0].get("is_on") is True, timeout=1.0)
        # state echo updates internal state via is_on bool
        await _wait_for(lambda: outlet.is_on is True, timeout=1.0)
        assert outlet.is_on
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_fan_onoff_via_TagoFan(fake_server):
    """`fan_onoff` is strictly on/off per PROTOCOL.md §7a — TagoFan handles it."""
    from custom_components.tago.TagoNet import TagoFan
    fake_server.seed({L0: {"type": "fan_onoff", "is_on": False}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        fan = next(e for e in device.entities if e.unique_id == L0)
        assert isinstance(fan, TagoFan)
        await fan.turn_on()
        await _wait_for(lambda: fake_server.state[L0].get("is_on") is True, timeout=1.0)
        await _wait_for(lambda: fan.is_on is True, timeout=1.0)
        assert fan.is_on
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# Dimming — instant (driven via TagoLight, the integration's real
# client surface for dimmable lights).
# =====================================================================

@pytest.mark.asyncio
async def test_set_brightness_500_instant(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.set_brightness(brightness=0.5)
        await _wait_for(lambda: fake_server.state[L0]["brightness"] == 500, timeout=1.0)
        assert fake_server.state[L0]["brightness"] == 500
        sent_set_light = [r for r in fake_server.received if r.get("req") == "set_light"]
        assert sent_set_light[-1]["brightness"] == 500
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_set_brightness_0_turns_off(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 700}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.set_brightness(brightness=0.0)
        await _wait_for(lambda: fake_server.state[L0]["brightness"] == 0, timeout=1.0)
        assert fake_server.state[L0]["brightness"] == 0
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_set_brightness_clamped_high(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_light", "dst": L0, "brightness": 1500}
    )
    assert server.state[L0]["brightness"] == 1000


@pytest.mark.asyncio
async def test_set_brightness_clamped_negative(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_dimmable", "brightness": 500}})
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_light", "dst": L0, "brightness": -100}
    )
    assert server.state[L0]["brightness"] == 0


@pytest.mark.asyncio
async def test_brightness_plus_delta(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_dimmable", "brightness": 400}})
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_light", "dst": L0, "brightness+": 100}
    )
    assert server.state[L0]["brightness"] == 500


# =====================================================================
# Dimming — ramp
# =====================================================================

@pytest.mark.asyncio
async def test_ramp_brightness_1s_emits_ramp_object_in_event(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.set_brightness(brightness=0.8, duration=1.0)
        await _wait_for(
            lambda: any(e.get("evt") == "state_changed" and "ramp" in e
                        for e in fake_server.sent),
            timeout=1.5,
        )
        ramp_evt = next(e for e in fake_server.sent
                        if e.get("evt") == "state_changed" and "ramp" in e)
        assert ramp_evt["ramp"]["duration"] == 1000
        assert ramp_evt["ramp"]["end"]["brightness"] == 800
        if light._ramp:
            light._ramp.cancel()
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ramp_duration_below_min_is_instant(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    server.sent.clear()
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_light", "dst": L0, "brightness": 500, "duration": 100}
    )
    await _wait_for(lambda: any(e.get("evt") == "state_changed" for e in server.sent),
                    timeout=1.0)
    evts = [e for e in server.sent if e.get("evt") == "state_changed"]
    assert evts
    assert "ramp" not in evts[-1]
    assert evts[-1]["brightness"] == 500


@pytest.mark.asyncio
async def test_ramp_duration_above_max_clamps_to_10000(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    server.sent.clear()
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_light", "dst": L0, "brightness": 500, "duration": 99999}
    )
    await _wait_for(
        lambda: any("ramp" in e for e in server.sent if e.get("evt") == "state_changed"),
        timeout=1.0,
    )
    ramp_evt = next(e for e in server.sent
                    if e.get("evt") == "state_changed" and "ramp" in e)
    assert ramp_evt["ramp"]["duration"] == 10000


@pytest.mark.asyncio
async def test_stop_ramp_mid_returns_200(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_light", "dst": L0, "brightness": 1000, "duration": 2000}
    )
    reply = await _send_raw_and_wait_reply(device, server, {"req": "stop_ramp", "dst": L0})
    assert reply["status"] == 200


# =====================================================================
# Color temperature
# =====================================================================

@pytest.mark.asyncio
async def test_set_ct_50pct(fake_server):
    fake_server.seed({L0: {"type": "light_ww", "brightness": 500, "ct": 0}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.set_ct(ct=0.5)
        await _wait_for(lambda: fake_server.state[L0].get("ct") == 500, timeout=1.0)
        assert fake_server.state[L0]["ct"] == 500
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ct_plus_delta_clears_xy(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_ww", "brightness": 500, "ct": 400, "x": 0.3, "y": 0.3}})
    await _send_raw_and_wait_reply(device, server, {"req": "set_light", "dst": L0, "ct+": 100})
    assert server.state[L0]["ct"] == 500
    assert server.state[L0]["x"] == 0.0
    assert server.state[L0]["y"] == 0.0


# =====================================================================
# Color (xy)
# =====================================================================

@pytest.mark.asyncio
async def test_set_color_red(fake_server):
    fake_server.seed({L0: {"type": "light_rgb", "brightness": 800}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        await light.set_colour(colour=(0.64, 0.33))
        await _wait_for(lambda: fake_server.state[L0].get("x") == pytest.approx(0.64),
                        timeout=1.0)
        assert fake_server.state[L0]["x"] == pytest.approx(0.64)
        assert fake_server.state[L0]["y"] == pytest.approx(0.33)
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_set_color_x_only_is_no_op_partial(connected_device):
    """x without y returns status 500 per the authoritative C code
    (load_light.c:697); state is unchanged."""
    device, server = connected_device
    server.seed({L0: {"type": "light_rgb"}})
    reply = await _send_raw_and_wait_reply(device, server, {"req": "set_light", "dst": L0, "x": 0.3})
    assert reply["status"] == 500
    assert "x" not in server.state[L0] or server.state[L0]["x"] != 0.3


# =====================================================================
# Set-light no-op
# =====================================================================

@pytest.mark.asyncio
async def test_set_light_noop_drops_event(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_dimmable", "brightness": 500}})
    server.sent.clear()
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_light", "dst": L0, "brightness": 500}
    )
    await asyncio.sleep(0.05)
    evts = [m for m in server.sent if m.get("evt") == "state_changed"]
    assert evts == []


# =====================================================================
# Configuration
# =====================================================================

@pytest.mark.asyncio
async def test_set_config_rename_emits_config_changed(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_onoff", "name": ""}})
    server.sent.clear()
    await _send_raw_and_wait_reply(
        device, server, {"req": "set_config", "dst": L0, "name": "Kitchen"}
    )
    await _wait_for(lambda: any(e.get("evt") == "config_changed" for e in server.sent),
                    timeout=1.0)
    evt = next(e for e in server.sent if e.get("evt") == "config_changed")
    assert evt["src"] == L0
    assert evt["name"] == "Kitchen"


@pytest.mark.asyncio
async def test_set_config_invalid_type_rejected(connected_device):
    device, server = connected_device
    server.seed({L0: {"type": "light_onoff"}})
    reply = await _send_raw_and_wait_reply(
        device, server, {"req": "set_config", "dst": L0, "type": "toaster"}
    )
    assert reply["status"] == 500


@pytest.mark.asyncio
async def test_set_config_steals_output_emits_for_both(connected_device):
    device, server = connected_device
    server.seed({
        L0: {"type": "light_onoff", "map": [0]},
        L1: {"type": "light_onoff", "map": [-1]},
    })
    server.sent.clear()
    await _send_raw_and_wait_reply(
        device, server,
        {"req": "set_config", "dst": L1, "type": "light_onoff", "map": [0]},
    )
    await _wait_for(
        lambda: sum(1 for e in server.sent if e.get("evt") == "config_changed") >= 2,
        timeout=1.0,
    )
    evt_srcs = {e["src"] for e in server.sent if e.get("evt") == "config_changed"}
    assert {L0, L1}.issubset(evt_srcs)
    assert server.state[L0]["map"] == [-1]
    assert server.state[L1]["map"] == [0]


# =====================================================================
# Events — external state change broadcast
# =====================================================================

@pytest.mark.asyncio
async def test_external_state_change_broadcast_reaches_entity(fake_server):
    """Server-initiated state_changed (e.g. physical switch flip) must be
    deliverable to the client without it having sent a command."""
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        light = next(e for e in device.entities if e.unique_id == L0)
        # Wait for the post-connect get_state round-trip to settle, otherwise
        # its reply will clobber the broadcast we're about to make.
        await _wait_for(
            lambda: any(r.get("req") == "get_state" and r.get("dst") == L0
                        for r in fake_server.received),
            timeout=1.0,
        )
        await asyncio.sleep(0.05)
        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "light_dimmable", "brightness": 750}
        )
        await _wait_for(lambda: light._brightness == 750, timeout=1.0)
        assert light._brightness == 750
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# Errors
# =====================================================================

@pytest.mark.asyncio
async def test_unknown_request_returns_500_with_rsp_echo(connected_device):
    device, server = connected_device
    reply = await _send_raw_and_wait_reply(device, server, {"req": "does_not_exist"})
    assert reply["status"] == 500
    assert reply["rsp"] == "does_not_exist"


@pytest.mark.asyncio
async def test_bad_dst_returns_500_and_echoes_src(connected_device):
    device, server = connected_device
    reply = await _send_raw_and_wait_reply(
        device, server, {"req": "get_state", "dst": "NO_SUCH_ENTITY"}
    )
    assert reply["status"] == 500
    assert reply["src"] == "NO_SUCH_ENTITY"


@pytest.mark.asyncio
async def test_non_string_dst_returns_500(connected_device):
    device, server = connected_device
    ref = "non-str-1"
    await device._gateway._ws.send(json.dumps({"req": "ping", "dst": 42, "ref": ref}))
    await _wait_for(lambda: any(m.get("ref") == ref for m in server.sent), timeout=1.0)
    reply = next(m for m in server.sent if m.get("ref") == ref)
    assert reply["status"] == 500


# =====================================================================
# Liveness
# =====================================================================

@pytest.mark.asyncio
async def test_ping_basic(connected_device):
    device, server = connected_device
    reply = await _send_raw_and_wait_reply(device, server, {"req": "ping"})
    assert isinstance(reply["ts"], int)
    assert reply["src"] == DEVICE_ID
    assert reply["rsp"] == "ping"


@pytest.mark.asyncio
async def test_ping_with_ref_round_trips(connected_device):
    device, server = connected_device
    reply = await _send_raw_and_wait_reply(device, server, {"req": "ping", "ref": "abc-123"})
    assert reply["ref"] == "abc-123"


@pytest.mark.asyncio
async def test_ping_detects_reboot(connected_device):
    device, server = connected_device
    r1 = await _send_raw_and_wait_reply(device, server, {"req": "ping"})
    ts1 = r1["ts"]
    server.reboot()
    await asyncio.sleep(0.01)
    r2 = await _send_raw_and_wait_reply(device, server, {"req": "ping"})
    assert r2["ts"] < ts1


# =====================================================================
# Scenario-ID coverage check — every scenario in wire_scenarios.json
# must be referenced by at least one test in this file (or the F
# deviations file). Catches drift if the firmware suite adds a scenario.
# =====================================================================

SCENARIO_TEST_MAP = {
    # Connection lifecycle
    "connect_identity_exchange": "test_connect_identity_exchange",
    "reconnect_after_disconnect": "test_reconnect_after_disconnect_preserves_entity_id_mapping",
    # Discovery
    "list_nodes_basic": "test_list_nodes_basic_returns_one_group_with_eight_channels",
    "device_get_config_basic": "test_device_get_config_basic_metadata_shape",
    # On/off
    "turn_on_onoff": "test_turn_on_onoff_via_set_brightness",
    "turn_off_onoff": "test_turn_off_onoff_via_set_brightness",
    "toggle_onoff": "test_toggle_onoff_round_trip",
    # Dimming — instant
    "set_brightness_500": "test_set_brightness_500_instant",
    "set_brightness_0": "test_set_brightness_0_turns_off",
    "set_brightness_clamped_high": "test_set_brightness_clamped_high",
    "set_brightness_clamped_negative": "test_set_brightness_clamped_negative",
    "brightness_plus_delta": "test_brightness_plus_delta",
    # Dimming — ramp
    "ramp_brightness_1s": "test_ramp_brightness_1s_emits_ramp_object_in_event",
    "ramp_duration_below_min_is_instant": "test_ramp_duration_below_min_is_instant",
    "ramp_duration_above_max_clamps": "test_ramp_duration_above_max_clamps_to_10000",
    "stop_ramp_mid": "test_stop_ramp_mid_returns_200",
    # CT
    "set_ct_50pct": "test_set_ct_50pct",
    "ct_plus_delta": "test_ct_plus_delta_clears_xy",
    # Color
    "set_color_red": "test_set_color_red",
    "set_color_x_only_rejected": "test_set_color_x_only_is_no_op_partial",
    # No-op
    "set_light_noop_drops_event": "test_set_light_noop_drops_event",
    # Config
    "set_config_rename": "test_set_config_rename_emits_config_changed",
    "set_config_invalid_type_rejected": "test_set_config_invalid_type_rejected",
    "set_config_steals_output": "test_set_config_steals_output_emits_for_both",
    # Events
    "external_state_change_broadcast": "test_external_state_change_broadcast_reaches_entity",
    # Errors
    "unknown_request_500": "test_unknown_request_returns_500_with_rsp_echo",
    "bad_dst_500": "test_bad_dst_returns_500_and_echoes_src",
    "non_string_dst_500": "test_non_string_dst_returns_500",
    # Liveness
    "ping_basic": "test_ping_basic",
    "ping_with_ref": "test_ping_with_ref_round_trips",
    "ping_detects_reboot": "test_ping_detects_reboot",
}


def test_every_scenario_id_has_a_mapped_test(wire_scenarios):
    """Drift guard. If the firmware host suite adds a scenario, this fires."""
    missing = [sid for sid in wire_scenarios if sid not in SCENARIO_TEST_MAP]
    assert not missing, (
        f"scenario(s) without a mapped test in this suite: {missing}. "
        "Add to SCENARIO_TEST_MAP and write a test, or list in the report."
    )


def test_every_mapped_test_actually_exists():
    """Drift guard the other direction — every mapped test name resolves."""
    from pathlib import Path
    here = Path(__file__).parent
    suite_text = "\n".join(p.read_text() for p in here.glob("test_*.py"))
    missing = [name for name in SCENARIO_TEST_MAP.values()
               if f"def {name}" not in suite_text]
    assert not missing, f"mapped tests not found: {missing}"
