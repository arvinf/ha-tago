"""HA platform roundtrip tests against the fake firmware.

Drives the HA wrappers (TagoLightHA / TagoSwitchHA / TagoFanHA / TagoCoverHA)
over a real local WebSocket. Verifies both directions:

  - HA → wire: HA `async_turn_on(...)` / `async_set_cover_position(...)`
    etc. translate into the documented JSON frames.
  - wire → HA: `state_changed` / `config_changed` events from the firmware
    are reflected in HA-facing properties (`brightness`, `is_on`,
    `color_temp_kelvin`, `xy_color`, `min/max_color_temp_kelvin`, ...).

These complement the unit-style tests in `test_ha_invariants.py` (which
exercise the same conversions with a mock device) by running through real
network plumbing and through the integration's full dispatch loop.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from custom_components.tago.TagoNet import (
    TagoCover,
    TagoDevice,
    TagoFan,
    TagoLight,
    TagoSwitch,
)
from custom_components.tago.cover import TagoCoverHA
from custom_components.tago.entity import TagoEntityHA
from custom_components.tago.fan import TagoFanHA
from custom_components.tago.light import TagoLightHA
from custom_components.tago.switch import TagoSwitchHA
from scenarios import DEVICE_ID

L0 = "TAGO_TEST_001L1_0"

pytestmark = [pytest.mark.enable_socket]


@pytest.fixture(autouse=True)
def _silence_ha_state_writes(monkeypatch):
    """The HA wrappers call `schedule_update_ha_state()` via `update()` when
    an entity event fires. Outside a real HA instance that explodes — patch
    it to a no-op for this file only."""
    monkeypatch.setattr(TagoEntityHA, "update", lambda self: None)


@pytest.fixture(autouse=True)
def _patch_ramp_to_not_spawn_task(monkeypatch):
    """`Ramp.__init__` spawns a perpetual asyncio task that pytest-HA flags as
    a lingering task on teardown. For HA-platform tests we don't care about
    the local interpolation; replace the task spawn with a no-op so the
    Ramp object exists (so `entity.is_ramp_active` still reports correctly)
    but doesn't keep the event loop alive."""
    from custom_components.tago import TagoNet as _tn

    original_init = _tn.Ramp.__init__

    def _patched_init(self, start, end, duration, elapsed, update_interval, callback):
        self.start = start
        self.end = end
        self.duration = duration
        self.elapsed = elapsed
        import time as _time
        self.start_time = round(_time.time() * 1000)
        self.update_interval = update_interval
        self.cb = callback
        self.task = None  # no spawned task — see fixture docstring

    monkeypatch.setattr(_tn.Ramp, "__init__", _patched_init)
    yield
    monkeypatch.setattr(_tn.Ramp, "__init__", original_init)


async def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.01):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _settle_initial_state(server) -> None:
    """Wait for the post-connect get_state round-trip to drain so state we
    push afterward isn't clobbered by a stale reply."""
    await _wait_for(
        lambda: any(r.get("req") == "get_state" and r.get("dst") == L0
                    for r in server.received),
        timeout=1.0,
    )
    await asyncio.sleep(0.05)


def _wrap_ha(device, entity_cls, ha_cls):
    """Find the TagoX entity for L0 on the device, wrap it in the HA class."""
    entity = next(e for e in device.entities if e.unique_id == L0)
    assert isinstance(entity, entity_cls), f"expected {entity_cls.__name__}, got {type(entity).__name__}"
    return ha_cls(entity), entity


# =====================================================================
# HA → wire: TagoLightHA — dimmable
# =====================================================================

@pytest.mark.asyncio
async def test_ha_dimmable_async_turn_on_brightness_translates_to_set_light(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        from homeassistant.components.light import ATTR_BRIGHTNESS
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        fake_server.received.clear()
        await ha.async_turn_on(**{ATTR_BRIGHTNESS: 128})
        await _wait_for(
            lambda: any(r.get("req") == "set_light" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "set_light")
        assert frame["dst"] == L0
        assert frame["brightness"] == 502  # 128/255 * 1000, rounded
        assert "duration" not in frame
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_dimmable_async_turn_on_with_transition_adds_clamped_duration(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_TRANSITION
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        fake_server.received.clear()
        await ha.async_turn_on(**{ATTR_BRIGHTNESS: 200, ATTR_TRANSITION: 1.5})
        await _wait_for(
            lambda: any(r.get("req") == "set_light" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "set_light")
        assert frame["brightness"] == 784
        assert frame["duration"] == 1500
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_dimmable_async_turn_on_with_subminimum_transition_omits_duration(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_TRANSITION
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        fake_server.received.clear()
        await ha.async_turn_on(**{ATTR_BRIGHTNESS: 100, ATTR_TRANSITION: 0.05})
        await _wait_for(
            lambda: any(r.get("req") == "set_light" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "set_light")
        assert "duration" not in frame
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_dimmable_async_turn_off_sends_brightness_zero(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 500}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        fake_server.received.clear()
        await ha.async_turn_off()
        await _wait_for(
            lambda: any(r.get("req") == "set_light" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "set_light")
        assert frame["brightness"] == 0
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# HA → wire: TagoLightHA — CCT (Kelvin → ct percent)
# =====================================================================

@pytest.mark.asyncio
async def test_ha_cct_async_turn_on_kelvin_translates_to_ct_percent(fake_server):
    fake_server.seed({
        L0: {"type": "light_ww", "brightness": 800, "ct": 0,
             "ct_range": [2700, 6500]},
    })
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        # Verify ct_range arrived via list_nodes:
        assert ha.min_color_temp_kelvin == 2700
        assert ha.max_color_temp_kelvin == 6500

        fake_server.received.clear()
        await ha.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 4600})
        await _wait_for(
            lambda: any(r.get("req") == "set_light" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "set_light")
        assert frame["ct"] == 500
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# HA → wire: TagoLightHA — RGB (xy)
# =====================================================================

@pytest.mark.asyncio
async def test_ha_rgb_async_turn_on_xy_translates_to_xy(fake_server):
    fake_server.seed({L0: {"type": "light_rgb", "brightness": 800}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        from homeassistant.components.light import ATTR_XY_COLOR
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        fake_server.received.clear()
        await ha.async_turn_on(**{ATTR_XY_COLOR: (0.64, 0.33)})
        await _wait_for(
            lambda: any(r.get("req") == "set_light" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "set_light")
        assert frame["x"] == pytest.approx(0.64)
        assert frame["y"] == pytest.approx(0.33)
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# HA → wire: TagoLightHA — on/off subtype routes via turn_on / turn_off
# =====================================================================

@pytest.mark.asyncio
async def test_ha_onoff_light_async_turn_on_sends_turn_on(fake_server):
    fake_server.seed({L0: {"type": "light_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        fake_server.received.clear()
        await ha.async_turn_on()
        await _wait_for(
            lambda: any(r.get("req") == "turn_on" for r in fake_server.received),
            timeout=1.0,
        )
        # Must use the protocol-mandated request, never set_light for on/off.
        reqs = [r.get("req") for r in fake_server.received]
        assert "turn_on" in reqs
        assert "set_light" not in reqs
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_onoff_light_async_turn_off_sends_turn_off(fake_server):
    fake_server.seed({L0: {"type": "light_onoff", "is_on": True}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoLight, TagoLightHA)
        fake_server.received.clear()
        await ha.async_turn_off()
        await _wait_for(
            lambda: any(r.get("req") == "turn_off" for r in fake_server.received),
            timeout=1.0,
        )
        reqs = [r.get("req") for r in fake_server.received]
        assert "turn_off" in reqs
        assert "set_light" not in reqs
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# HA → wire: TagoSwitchHA — outlet_onoff
# =====================================================================

@pytest.mark.asyncio
async def test_ha_switch_async_turn_on_sends_turn_on(fake_server):
    fake_server.seed({L0: {"type": "outlet_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoSwitch, TagoSwitchHA)
        fake_server.received.clear()
        await ha.async_turn_on()
        await _wait_for(
            lambda: any(r.get("req") == "turn_on" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "turn_on")
        assert frame["dst"] == L0
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_switch_async_turn_off_sends_turn_off(fake_server):
    fake_server.seed({L0: {"type": "outlet_onoff", "is_on": True}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoSwitch, TagoSwitchHA)
        fake_server.received.clear()
        await ha.async_turn_off()
        await _wait_for(
            lambda: any(r.get("req") == "turn_off" for r in fake_server.received),
            timeout=1.0,
        )
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# HA → wire: TagoFanHA — fan_onoff
# =====================================================================

@pytest.mark.asyncio
async def test_ha_fan_async_turn_on_sends_turn_on(fake_server):
    fake_server.seed({L0: {"type": "fan_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoFan, TagoFanHA)
        fake_server.received.clear()
        await ha.async_turn_on()
        await _wait_for(
            lambda: any(r.get("req") == "turn_on" for r in fake_server.received),
            timeout=1.0,
        )
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_fan_async_turn_off_sends_turn_off(fake_server):
    fake_server.seed({L0: {"type": "fan_onoff", "is_on": True}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoFan, TagoFanHA)
        fake_server.received.clear()
        await ha.async_turn_off()
        await _wait_for(
            lambda: any(r.get("req") == "turn_off" for r in fake_server.received),
            timeout=1.0,
        )
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# HA → wire: TagoCoverHA
# =====================================================================

@pytest.mark.asyncio
async def test_ha_cover_async_open_sends_move_to_target_zero(fake_server):
    fake_server.seed({L0: {"type": "cover_blind", "position": 100, "target": 100}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoCover, TagoCoverHA)
        fake_server.received.clear()
        await ha.async_open_cover()
        await _wait_for(
            lambda: any(r.get("req") == "move_to" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "move_to")
        assert frame["dst"] == L0
        assert frame["target"] == 0
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_cover_async_close_sends_move_to_target_hundred(fake_server):
    fake_server.seed({L0: {"type": "cover_blind", "position": 0, "target": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoCover, TagoCoverHA)
        fake_server.received.clear()
        await ha.async_close_cover()
        await _wait_for(
            lambda: any(r.get("req") == "move_to" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "move_to")
        assert frame["target"] == 100
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_cover_set_position_inverts_percentage(fake_server):
    fake_server.seed({L0: {"type": "cover_blind", "position": 0, "target": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        from homeassistant.components.cover import ATTR_POSITION
        ha, _ = _wrap_ha(device, TagoCover, TagoCoverHA)
        fake_server.received.clear()
        # HA position 30 (mostly closed) → wire target 70 (mostly closed)
        await ha.async_set_cover_position(**{ATTR_POSITION: 30})
        await _wait_for(
            lambda: any(r.get("req") == "move_to" for r in fake_server.received),
            timeout=1.0,
        )
        frame = next(r for r in fake_server.received if r.get("req") == "move_to")
        assert frame["target"] == 70
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_ha_cover_async_stop_sends_stop_move(fake_server):
    fake_server.seed({L0: {"type": "cover_blind", "position": 50, "target": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, _ = _wrap_ha(device, TagoCover, TagoCoverHA)
        fake_server.received.clear()
        await ha.async_stop_cover()
        await _wait_for(
            lambda: any(r.get("req") == "stop_move" for r in fake_server.received),
            timeout=1.0,
        )
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: brightness echo
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_brightness_reflects_to_ha_brightness(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "light_dimmable", "brightness": 750}
        )
        await _wait_for(lambda: entity._brightness == 750, timeout=1.0)
        # HA scale 0..255 ← wire 0..1000
        assert ha.brightness == 191
        assert ha.is_on is True
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_state_changed_brightness_zero_reflects_is_off(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 1000}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "light_dimmable", "brightness": 0}
        )
        await _wait_for(lambda: entity._brightness == 0, timeout=1.0)
        assert ha.is_on is False
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: CT (percent → Kelvin)
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_ct_reflects_to_ha_color_temp_kelvin(fake_server):
    fake_server.seed({
        L0: {"type": "light_ww", "brightness": 800, "ct": 0,
             "ct_range": [2700, 6500]},
    })
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "light_ww", "brightness": 800, "ct": 500}
        )
        await _wait_for(lambda: entity._ct == 500, timeout=1.0)
        # 0.5 ratio of [2700, 6500] → 4600 K
        assert ha.color_temp_kelvin == 4600
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: xy
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_xy_reflects_to_ha_xy_color(fake_server):
    fake_server.seed({L0: {"type": "light_rgb", "brightness": 800}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "light_rgb", "brightness": 800, "x": 0.64, "y": 0.33}
        )
        await _wait_for(lambda: entity._colour_x == pytest.approx(0.64), timeout=1.0)
        x, y = ha.xy_color
        assert x == pytest.approx(0.64)
        assert y == pytest.approx(0.33)
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: switch is_on
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_is_on_reflects_to_TagoSwitchHA(fake_server):
    fake_server.seed({L0: {"type": "outlet_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoSwitch, TagoSwitchHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "outlet_onoff", "is_on": True}
        )
        await _wait_for(lambda: entity.is_on is True, timeout=1.0)
        assert ha.is_on is True

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "outlet_onoff", "is_on": False}
        )
        await _wait_for(lambda: entity.is_on is False, timeout=1.0)
        assert ha.is_on is False
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: fan is_on
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_is_on_reflects_to_TagoFanHA(fake_server):
    fake_server.seed({L0: {"type": "fan_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoFan, TagoFanHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "fan_onoff", "is_on": True}
        )
        await _wait_for(lambda: entity.is_on is True, timeout=1.0)
        assert ha.is_on is True
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: on/off light is_on
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_is_on_reflects_to_TagoLightHA_onoff(fake_server):
    fake_server.seed({L0: {"type": "light_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "light_onoff", "is_on": True}
        )
        await _wait_for(lambda: ha.is_on is True, timeout=1.0)
        assert ha.is_on is True
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: config_changed updates CT range
# =====================================================================

@pytest.mark.asyncio
async def test_config_changed_ct_range_updates_ha_bounds(fake_server):
    fake_server.seed({
        L0: {"type": "light_ww", "brightness": 0, "ct": 0,
             "ct_range": [2700, 6500]},
    })
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)
        assert ha.min_color_temp_kelvin == 2700
        assert ha.max_color_temp_kelvin == 6500

        await fake_server.broadcast_event(
            {"evt": "config_changed", "src": L0, "id": L0,
             "type": "light_ww", "ct_range": [2200, 5000]}
        )
        await _wait_for(lambda: entity._ct_range_min == 2200, timeout=1.0)
        assert ha.min_color_temp_kelvin == 2200
        assert ha.max_color_temp_kelvin == 5000
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# wire → HA: fault surfaces
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_fault_surfaces_on_entity(fake_server):
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 500}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        _, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)

        await fake_server.broadcast_event(
            {"evt": "state_changed", "src": L0, "id": L0,
             "type": "light_dimmable", "brightness": 0, "fault": "overcurrent"}
        )
        await _wait_for(lambda: entity.has_fault, timeout=1.0)
        assert "overcurrent" in entity.fault
    finally:
        await device.disconnect(timeout=5.0)


# =====================================================================
# Roundtrip: HA → wire → fake server → state_changed echo → HA
# =====================================================================

@pytest.mark.asyncio
async def test_full_roundtrip_dimmable_brightness(fake_server):
    """Drive HA-side command; the fake server's state echo should make the
    HA entity converge on the requested value."""
    fake_server.seed({L0: {"type": "light_dimmable", "brightness": 0}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        from homeassistant.components.light import ATTR_BRIGHTNESS
        ha, entity = _wrap_ha(device, TagoLight, TagoLightHA)
        await _settle_initial_state(fake_server)
        await ha.async_turn_on(**{ATTR_BRIGHTNESS: 128})
        # Wait for set_light echo (state_changed with brightness=502).
        await _wait_for(lambda: entity._brightness == 502, timeout=1.0)
        # HA brightness 128 → wire 502 → wire echo 502 → HA brightness 128
        assert ha.brightness == 128
        assert ha.is_on is True
    finally:
        await device.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_full_roundtrip_outlet_toggle(fake_server):
    fake_server.seed({L0: {"type": "outlet_onoff", "is_on": False}})
    device = TagoDevice(f"127.0.0.1:{fake_server.port}", authkey="")
    await device.connect(timeout=5.0)
    try:
        ha, entity = _wrap_ha(device, TagoSwitch, TagoSwitchHA)
        await _settle_initial_state(fake_server)
        await ha.async_turn_on()
        await _wait_for(lambda: ha.is_on is True, timeout=1.0)
        await ha.async_turn_off()
        await _wait_for(lambda: ha.is_on is False, timeout=1.0)
    finally:
        await device.disconnect(timeout=5.0)
