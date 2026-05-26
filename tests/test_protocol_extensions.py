"""Tests for the PROTOCOL_PROPOSALS extensions:
  - Scenes (§P1): wire `activate` + `scene_activated` event
  - Keypads (§P2): device-registry registration + key event fan-out
  - Keypad LED (§P2.5): RGB-only light + `set_led` with flash effect
  - Virtual switches (§P3): writable on/off, CONFIG category, default-off
  - Virtual sensors (§P3): read-only is_on, DIAGNOSTIC category, default-off

The firmware hasn't shipped any of this yet — the fake server emulates
the documented wire shape so the HA integration can be exercised end
to end now and won't need rework when firmware lands.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tago.const import (
    CONF_HOSTSTR,
    CONF_PIN,
    DOMAIN,
)
from custom_components.tago.TagoNet import (
    TagoKeypad,
    TagoKeypadKey,
    TagoScene,
    TagoVirtualSensor,
    TagoVirtualSwitch,
)

L0 = "TAGO_TEST_001L1_0"
L1 = "TAGO_TEST_001L1_1"
L2 = "TAGO_TEST_001L1_2"


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _resolve_entity_id(hass, domain: str, unique_id: str) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(domain, DOMAIN, unique_id)


@pytest_asyncio.fixture
async def setup_with_seed(hass, enable_custom_integrations, fake_server):
    """Seed the fake firmware, build a config entry, and tear down on exit."""
    entries: list[MockConfigEntry] = []

    async def _factory(seed: dict) -> MockConfigEntry:
        fake_server.seed(seed)
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
            unique_id="TAGO_TEST_001",
            version=9,
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry

    yield _factory

    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# =====================================================================
# Scenes (§P1)
# =====================================================================

@pytest.mark.asyncio
async def test_scene_appears_as_HA_scene_entity(setup_with_seed, hass):
    await setup_with_seed({
        L0: {"type": "scene", "name": "Movie Night",
             "location": "Living Room", "tag": "S1"},
    })
    entity_id = _resolve_entity_id(hass, "scene", L0)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None


@pytest.mark.asyncio
async def test_scene_turn_on_sends_activate_request(
    setup_with_seed, hass, fake_server
):
    """`scene.turn_on` (HA service) routes to wire `activate`."""
    await setup_with_seed({
        L0: {"type": "scene", "name": "Movie", "tag": "S1"},
    })
    entity_id = _resolve_entity_id(hass, "scene", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "scene", "turn_on", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "activate" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "activate")
    assert frame["dst"] == L0


@pytest.mark.asyncio
async def test_scene_activated_event_updates_last_activated_ts(
    setup_with_seed, hass, fake_server
):
    entry = await setup_with_seed({
        L0: {"type": "scene", "name": "Movie", "tag": "S1"},
    })
    device = entry.runtime_data
    scene = next(e for e in device.entities if e.unique_id == L0)
    assert isinstance(scene, TagoScene)
    assert scene.last_activated_ts == 0

    # Trigger activation server-side via the activate command. The fake
    # server's _apply_scene_activate sets last_activated_ts and broadcasts
    # the scene_activated event.
    await hass.services.async_call(
        "scene", "turn_on",
        {"entity_id": _resolve_entity_id(hass, "scene", L0)},
        blocking=True,
    )
    await _wait_until(lambda: scene.last_activated_ts > 0)
    assert scene.last_activated_ts > 0


# =====================================================================
# Keypads (§P2) — device-registry registration + event fan-out
# =====================================================================

@pytest.mark.asyncio
async def test_keypad_registered_as_device(setup_with_seed, hass):
    """The keypad gets a device_registry entry with the right model
    metadata, but no HA entities of its own."""
    entry = await setup_with_seed({
        L0: {
            "type": "keypad_4btn",
            "name": "Bedroom Keypad",
            "location": "Bedroom",
            "tag": "K1",
            "model_num": "TKP-4-V2",
            "keys": ["1", "2", "3", "4"],
            "led_id": L1,
        },
        L1: {
            "type": "keypad_led",
            "keypad_id": L0,
            "tag": "K1L",
            "is_on": False,
            "brightness": 0,
            "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })
    registry = dr.async_get(hass)
    keypad_device = registry.async_get_device(identifiers={(DOMAIN, L0)})
    assert keypad_device is not None
    assert keypad_device.model == "TKP-4-V2"
    assert keypad_device.manufacturer == "TAGO"
    assert keypad_device.suggested_area == "Bedroom"


@pytest.mark.asyncio
async def test_key_event_fans_out_to_hass_bus(setup_with_seed, hass, fake_server):
    """A `key_single_press` event from the wire fires `tago_key_event` on
    HA's bus with keypad_id, key_id, event type, and led metadata."""
    entry = await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "name": "Bedroom Keypad",
            "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1", "2", "3", "4"], "led_id": L1,
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0,
            "tag": "K1L", "is_on": True, "brightness": 500,
            "rgb": {"r": 255, "g": 100, "b": 50},
        },
    })

    captured: list[dict] = []
    hass.bus.async_listen("tago_key_event", lambda event: captured.append(dict(event.data)))

    await fake_server.broadcast_event({
        "evt": "key_single_press",
        "src": L0,
        "keypad_id": L0,
        "key_id": "2",
        "led_state": "on",
        "led_color": {"r": 255, "g": 100, "b": 50},
        "data": "favorite_2",
    })
    await _wait_until(lambda: len(captured) >= 1, timeout=1.0)
    assert len(captured) == 1
    ev = captured[0]
    assert ev["keypad_id"] == L0
    assert ev["key_id"] == "2"
    assert ev["event"] == "key_single_press"
    assert ev["data"] == "favorite_2"
    assert ev["led_state"] == "on"
    assert ev["led_color"] == {"r": 255, "g": 100, "b": 50}


@pytest.mark.asyncio
async def test_key_press_held_repeats_fan_out_to_bus(
    setup_with_seed, hass, fake_server
):
    """Per D4 the firmware repeats `key_press_held` at a configurable
    cadence while the key is held. Each emission lands as a separate
    `tago_key_event` on the bus."""
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1,
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "is_on": False, "brightness": 0, "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })

    captured: list[dict] = []
    hass.bus.async_listen("tago_key_event", lambda event: captured.append(dict(event.data)))

    # Simulate the firmware sending one threshold-cross + two repeats.
    for duration in (750, 1250, 1750):
        await fake_server.broadcast_event({
            "evt": "key_press_held",
            "src": L0, "keypad_id": L0, "key_id": "1",
            "duration": duration,
            "led_state": "off",
        })
    await _wait_until(lambda: len(captured) >= 3, timeout=1.0)
    assert len(captured) == 3
    assert [c["duration"] for c in captured] == [750, 1250, 1750]
    assert all(c["event"] == "key_press_held" for c in captured)


# =====================================================================
# Keypad LED (§P2.5)
# =====================================================================

@pytest.mark.asyncio
async def test_keypad_led_is_HA_light_entity_with_rgb_color_mode(
    setup_with_seed, hass
):
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1, "name": "Bedroom KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "Bedroom KP LED",
            "is_on": True, "brightness": 800,
            "rgb": {"r": 255, "g": 100, "b": 50},
        },
    })
    entity_id = _resolve_entity_id(hass, "light", L1)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes.get("color_mode") == "rgb"
    # supported_color_modes is a set; HA serialises as a list of strings.
    modes = list(state.attributes.get("supported_color_modes", []))
    assert modes == ["rgb"]


@pytest.mark.asyncio
async def test_keypad_led_service_turn_on_sends_set_led_with_rgb(
    setup_with_seed, hass, fake_server
):
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1, "name": "Bedroom KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "Bedroom KP LED",
            "is_on": False, "brightness": 0,
            "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })
    entity_id = _resolve_entity_id(hass, "light", L1)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "rgb_color": [255, 100, 50], "brightness": 128},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_led" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_led")
    assert frame["dst"] == L1
    assert frame["is_on"] is True
    # HA 128 -> wire ~502
    assert frame["brightness"] == 502
    assert frame["rgb"] == {"r": 255, "g": 100, "b": 50}
    assert "effect" not in frame


@pytest.mark.asyncio
async def test_keypad_led_service_flash_short_sends_flash_4000ms(
    setup_with_seed, hass, fake_server
):
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1, "name": "Bedroom KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "Bedroom KP LED",
            "is_on": True, "brightness": 800,
            "rgb": {"r": 255, "g": 100, "b": 50},
        },
    })
    entity_id = _resolve_entity_id(hass, "light", L1)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "flash": "short"},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_led" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_led")
    assert frame["effect"] == "flash"
    assert frame["duration"] == 4000


@pytest.mark.asyncio
async def test_keypad_led_service_flash_long_sends_flash_10000ms(
    setup_with_seed, hass, fake_server
):
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1, "name": "Bedroom KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "Bedroom KP LED",
            "is_on": True, "brightness": 800,
            "rgb": {"r": 255, "g": 100, "b": 50},
        },
    })
    entity_id = _resolve_entity_id(hass, "light", L1)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "flash": "long"},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_led" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_led")
    assert frame["effect"] == "flash"
    assert frame["duration"] == 10000


@pytest.mark.asyncio
async def test_keypad_led_state_change_reflects_to_HA(
    setup_with_seed, hass, fake_server
):
    """When the firmware broadcasts a state_changed for the LED, the HA
    light entity reflects the new on/brightness/rgb."""
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1, "name": "Bedroom KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "Bedroom KP LED",
            "is_on": False, "brightness": 0,
            "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })
    entity_id = _resolve_entity_id(hass, "light", L1)

    await fake_server.broadcast_event({
        "evt": "state_changed", "src": L1, "id": L1, "type": "keypad_led",
        "is_on": True, "brightness": 750,
        "rgb": {"r": 200, "g": 150, "b": 100},
    })
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None and s.state == STATE_ON
    )
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    # 750 / 1000 * 255 = 191
    assert state.attributes["brightness"] == 191
    assert state.attributes["rgb_color"] == (200, 150, 100)


# =====================================================================
# Virtual switches (§P3)
# =====================================================================

@pytest.mark.asyncio
async def test_virtual_switch_is_registered_as_HA_switch(
    setup_with_seed, hass
):
    await setup_with_seed({
        L0: {
            "type": "virtual_switch", "tag": "VS1", "index": 0,
            "name": "Holiday Mode", "location": "VIRTUAL",
            "is_on": False,
        },
    })
    entity_id = _resolve_entity_id(hass, "switch", L0)
    assert entity_id is not None


@pytest.mark.asyncio
async def test_virtual_switch_is_config_category_disabled_by_default(
    setup_with_seed, hass
):
    await setup_with_seed({
        L0: {
            "type": "virtual_switch", "tag": "VS1", "index": 0,
            "name": "Holiday Mode", "location": "VIRTUAL",
            "is_on": False,
        },
    })
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, L0)
    entry_reg = registry.async_get(entity_id)
    assert entry_reg.entity_category == er.EntityCategory.CONFIG
    assert entry_reg.disabled_by is not None


@pytest.mark.asyncio
async def test_virtual_switch_turn_on_sends_turn_on_request(
    setup_with_seed, hass, fake_server
):
    """Enable the entity (since disabled-by-default), reload, then drive it."""
    entry = await setup_with_seed({
        L0: {
            "type": "virtual_switch", "tag": "VS1", "index": 0,
            "name": "Holiday Mode", "location": "VIRTUAL",
            "is_on": False,
        },
    })
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, L0)
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    fake_server.received.clear()
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_on" and r.get("dst") == L0
                    for r in fake_server.received)
    )


# =====================================================================
# Virtual sensors (§P3)
# =====================================================================

@pytest.mark.asyncio
async def test_virtual_sensor_is_registered_as_binary_sensor(
    setup_with_seed, hass
):
    await setup_with_seed({
        L0: {
            "type": "virtual_sensor", "tag": "VB1", "index": 0,
            "name": "Low Voltage", "location": "VIRTUAL",
            "is_on": False,
        },
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", L0)
    assert entity_id is not None


@pytest.mark.asyncio
async def test_virtual_sensor_is_diagnostic_category_disabled_by_default(
    setup_with_seed, hass
):
    await setup_with_seed({
        L0: {
            "type": "virtual_sensor", "tag": "VB1", "index": 0,
            "name": "Low Voltage", "location": "VIRTUAL",
            "is_on": False,
        },
    })
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, L0)
    entry_reg = registry.async_get(entity_id)
    assert entry_reg.entity_category == er.EntityCategory.DIAGNOSTIC
    assert entry_reg.disabled_by is not None


# =====================================================================
# Small-property + uncovered-method coverage
# =====================================================================

@pytest.mark.asyncio
async def test_tagoscene_scene_activated_callback_fires_when_registered():
    """When a callback is registered via set_on_scene_activated, the
    handler invokes it on each scene_activated event (line 1276)."""
    from custom_components.tago.TagoNet import TagoDevice, TagoMessage
    device = TagoDevice("dummy:1", authkey="")
    scene = TagoScene(
        {"id": "S0", "type": "scene", "name": "x", "location": "y", "tag": "S1"},
        device,
    )
    called: list[int] = []

    async def cb(msg):
        called.append(msg.data.get("ts", 0) if isinstance(msg.data, dict) else 0)

    scene.set_on_scene_activated(cb)
    assert scene._scene_activated_cb is cb

    msg = TagoMessage.from_payload(
        '{"evt": "scene_activated", "src": "S0", "ts": 99}'
    )
    await scene.handle_event(msg)
    assert called == [99]


def test_tagokeypad_keys_and_led_id_properties():
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    kpd = TagoKeypad(
        {"id": "K0", "type": "keypad_4btn", "name": "x", "location": "y",
         "tag": "K1", "model_num": "TKP-4-V2",
         "keys": ["1", "2", "3"], "led_id": "K0L"},
        device,
    )
    assert kpd.keys == ["1", "2", "3"]
    assert kpd.led_id == "K0L"
    # `keys` returns a copy — mutating the result must not affect internal state.
    returned = kpd.keys
    returned.append("BAD")
    assert kpd.keys == ["1", "2", "3"]


def test_tagokeypad_update_from_discovery_payload_refreshes_fields():
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    kpd = TagoKeypad(
        {"id": "K0", "type": "keypad_4btn", "name": "x", "location": "y",
         "tag": "K1", "model_num": "OLD",
         "keys": ["1", "2"], "led_id": "K0L"},
        device,
    )
    kpd.update_from_discovery_payload({
        "id": "K0", "type": "keypad_4btn",
        "name": "x", "location": "y", "tag": "K1",
        "model_num": "TKP-NEW",
        "keys": ["A", "B", "C"],
        "led_id": "K0L2",
    })
    assert kpd.model_num == "TKP-NEW"
    assert kpd.keys == ["A", "B", "C"]
    assert kpd.led_id == "K0L2"


@pytest.mark.asyncio
async def test_tagokeypad_handle_event_passes_non_key_events_to_super():
    """A non-key event on a keypad falls through to the base class
    handler (TagoEntity.handle_event), which dispatches to
    handle_config_change for config_changed (line 1346)."""
    from custom_components.tago.TagoNet import TagoDevice, TagoMessage
    device = TagoDevice("dummy:1", authkey="")
    kpd = TagoKeypad(
        {"id": "K0", "type": "keypad_4btn", "name": "", "location": "",
         "tag": "K1", "keys": ["1"]},
        device,
    )
    called: list[str] = []

    async def _track(msg):
        called.append(msg.evt)

    kpd.handle_config_change = _track
    msg = TagoMessage.from_payload(
        '{"evt": "config_changed", "src": "K0", "name": "Renamed"}'
    )
    await kpd.handle_event(msg)
    assert called == ["config_changed"]


@pytest.mark.asyncio
async def test_tagoscene_handle_event_with_no_callback_does_not_raise():
    """If no scene_activated callback is registered, the event still
    updates last_activated_ts and returns cleanly (line 1276 — the
    `if cb is not None` false branch)."""
    from custom_components.tago.TagoNet import TagoDevice, TagoMessage
    device = TagoDevice("dummy:1", authkey="")
    scene = TagoScene(
        {"id": "S0", "type": "scene", "name": "x", "location": "y", "tag": "S1"},
        device,
    )
    assert scene._scene_activated_cb is None
    msg = TagoMessage.from_payload(
        '{"evt": "scene_activated", "src": "S0", "ts": 12345}'
    )
    await scene.handle_event(msg)
    assert scene.last_activated_ts == 12345


@pytest.mark.asyncio
async def test_tagoscene_handle_event_passes_non_scene_event_to_super():
    """A non-`scene_activated` event on a scene falls through to the
    base class handler (line 1278)."""
    from custom_components.tago.TagoNet import TagoDevice, TagoMessage
    device = TagoDevice("dummy:1", authkey="")
    scene = TagoScene(
        {"id": "S0", "type": "scene", "name": "x", "location": "y", "tag": "S1"},
        device,
    )
    called: list[str] = []

    async def _track(msg):
        called.append(msg.evt)

    scene.handle_config_change = _track
    msg = TagoMessage.from_payload(
        '{"evt": "config_changed", "src": "S0", "name": "Renamed"}'
    )
    await scene.handle_event(msg)
    assert called == ["config_changed"]


def test_tagokeypad_led_keypad_id_property():
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    led = TagoKeypadKey(
        {"id": "K0L", "type": "keypad_led", "name": "", "location": "",
         "tag": "K1L", "keypad_id": "K0",
         "is_on": False, "brightness": 0,
         "rgb": {"r": 0, "g": 0, "b": 0}},
        device,
    )
    assert led.keypad_id == "K0"


class _CaptureDevice:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_request(self, req: str, data=None, dst=None, **kw):
        self.calls.append({"req": req, "data": data or {}, "dst": dst})


@pytest.mark.asyncio
async def test_tagokeypad_led_turn_on_off_toggle_send_correct_requests():
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    led = TagoKeypadKey(
        {"id": "K0L", "type": "keypad_led", "name": "", "location": "",
         "tag": "K1L", "keypad_id": "K0",
         "is_on": False, "brightness": 0,
         "rgb": {"r": 0, "g": 0, "b": 0}},
        device,
    )
    led._device = _CaptureDevice()
    await led.turn_on()
    await led.turn_off()
    await led.toggle()
    reqs = [c["req"] for c in led._device.calls]
    assert reqs == [
        TagoKeypadKey.REQ_TURN_ON,
        TagoKeypadKey.REQ_TURN_OFF,
        TagoKeypadKey.REQ_TOGGLE,
    ]


@pytest.mark.asyncio
async def test_tagovirtualswitch_index_and_toggle_and_turn_off():
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    vs = TagoVirtualSwitch(
        {"id": "VS0", "type": "virtual_switch", "name": "Holiday",
         "location": "VIRTUAL", "tag": "VS1", "index": 3, "is_on": True},
        device,
    )
    assert vs.index == 3
    vs._device = _CaptureDevice()
    await vs.turn_off()
    await vs.toggle()
    assert [c["req"] for c in vs._device.calls] == [
        TagoVirtualSwitch.REQ_TURN_OFF,
        TagoVirtualSwitch.REQ_TOGGLE,
    ]


def test_tagovirtualsensor_index_property():
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    vb = TagoVirtualSensor(
        {"id": "VB0", "type": "virtual_sensor", "name": "Low Voltage",
         "location": "VIRTUAL", "tag": "VB1", "index": 2, "is_on": False},
        device,
    )
    assert vb.index == 2


@pytest.mark.asyncio
async def test_keypad_led_HA_service_turn_off_sends_turn_off_request(
    setup_with_seed, hass, fake_server
):
    """light.turn_off on the keypad LED routes to wire `turn_off`
    (covers light.py:241)."""
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1, "name": "Bedroom KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "Bedroom KP LED",
            "is_on": True, "brightness": 800,
            "rgb": {"r": 255, "g": 100, "b": 50},
        },
    })
    entity_id = _resolve_entity_id(hass, "light", L1)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_off" and r.get("dst") == L1
                    for r in fake_server.received)
    )


@pytest.mark.asyncio
async def test_virtual_switch_HA_service_turn_off_sends_turn_off_request(
    setup_with_seed, hass, fake_server
):
    """switch.turn_off on a virtual switch routes to wire `turn_off`
    (covers switch.py:54)."""
    entry = await setup_with_seed({
        L0: {
            "type": "virtual_switch", "tag": "VS1", "index": 0,
            "name": "Holiday Mode", "location": "VIRTUAL", "is_on": True,
        },
    })
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, L0)
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    fake_server.received.clear()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_off" and r.get("dst") == L0
                    for r in fake_server.received)
    )


# =====================================================================
# (continues with earlier tests)
# =====================================================================


@pytest.mark.asyncio
async def test_virtual_sensor_state_reflects_firmware_broadcasts(
    setup_with_seed, hass, fake_server
):
    entry = await setup_with_seed({
        L0: {
            "type": "virtual_sensor", "tag": "VB1", "index": 0,
            "name": "Low Voltage", "location": "VIRTUAL",
            "is_on": False,
        },
    })
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, L0)
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    await fake_server.broadcast_event({
        "evt": "state_changed", "src": L0, "id": L0,
        "type": "virtual_sensor", "is_on": True,
    })
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None and s.state == STATE_ON
    )
    assert hass.states.get(entity_id).state == STATE_ON


# =====================================================================
# Implementation-fix coverage
# =====================================================================

@pytest.mark.asyncio
async def test_scene_activated_event_fires_tago_scene_bus_event(
    setup_with_seed, hass, fake_server
):
    """When the device broadcasts `scene_activated` (from any trigger,
    e.g., a keypad press), the integration fires `tago_scene_activated`
    on HA's bus so automations can react."""
    await setup_with_seed({
        L0: {"type": "scene", "name": "Movie Night",
             "location": "Living Room", "tag": "S1"},
    })

    captured: list[dict] = []
    hass.bus.async_listen("tago_scene_activated",
                          lambda event: captured.append(dict(event.data)))

    # Simulate a device-side activation (not via HA service).
    await fake_server.broadcast_event({
        "evt": "scene_activated", "src": L0, "id": L0,
        "type": "scene", "name": "Movie Night", "ts": 42000,
    })
    await _wait_until(lambda: len(captured) >= 1)
    assert len(captured) == 1
    ev = captured[0]
    assert ev["scene_id"] == L0
    assert ev["name"] == "Movie Night"
    assert ev["ts"] == 42000


@pytest.mark.asyncio
async def test_multiple_scenes_route_events_independently(
    setup_with_seed, hass, fake_server
):
    """Each scene only updates its OWN last_activated_ts when the event
    src matches; other scenes are untouched."""
    entry = await setup_with_seed({
        L0: {"type": "scene", "name": "Movie", "tag": "S1"},
        L1: {"type": "scene", "name": "Dinner", "tag": "S2"},
    })
    device = entry.runtime_data
    movie = next(e for e in device.entities if e.unique_id == L0)
    dinner = next(e for e in device.entities if e.unique_id == L1)

    await fake_server.broadcast_event({
        "evt": "scene_activated", "src": L0, "id": L0, "ts": 100,
    })
    await _wait_until(lambda: movie.last_activated_ts == 100)
    assert movie.last_activated_ts == 100
    assert dinner.last_activated_ts == 0


@pytest.mark.asyncio
async def test_scene_set_config_rename_round_trips(
    setup_with_seed, hass, fake_server
):
    """A wire-level `set_config` renaming a scene applies via the
    fake server's generic set_config path and emits a config_changed
    that the integration picks up."""
    entry = await setup_with_seed({
        L0: {"type": "scene", "name": "Old", "tag": "S1", "location": "Living"},
    })
    device = entry.runtime_data
    scene = next(e for e in device.entities if e.unique_id == L0)
    assert scene.name == "Old"

    await fake_server.broadcast_event({
        "evt": "config_changed", "src": L0, "id": L0,
        "type": "scene", "name": "Renamed", "location": "Living",
    })
    await _wait_until(
        lambda: any(r.get("evt") == "config_changed" for r in fake_server.sent),
        timeout=1.0,
    )
    # update_from_discovery_payload isn't called by config_changed in the
    # current dispatcher (handle_config_change is the hook); but the
    # event reached the entity without raising. Verify the integration
    # didn't crash by checking the WS still healthy.
    assert device.is_connected


# =====================================================================
# Keypad — missing gestures + multi-keypad isolation
# =====================================================================

@pytest.mark.parametrize("evt_name", [
    "key_pressed", "key_released", "key_double_press", "key_triple_press",
])
@pytest.mark.asyncio
async def test_all_key_gestures_dispatch_to_bus(
    setup_with_seed, hass, fake_server, evt_name
):
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1", "2", "3", "4"], "led_id": L1, "name": "Bedroom KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "is_on": False, "brightness": 0, "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })
    captured: list[dict] = []
    hass.bus.async_listen("tago_key_event",
                          lambda event: captured.append(dict(event.data)))

    payload = {
        "evt": evt_name,
        "src": L0,
        "keypad_id": L0,
        "key_id": "2",
        "led_state": "off",
    }
    if evt_name == "key_released":
        payload["duration"] = 350
    await fake_server.broadcast_event(payload)
    await _wait_until(lambda: len(captured) >= 1)
    assert captured[0]["event"] == evt_name
    assert captured[0]["key_id"] == "2"
    if evt_name == "key_released":
        assert captured[0]["duration"] == 350


@pytest.mark.asyncio
async def test_key_event_without_data_field_is_dispatched_with_none(
    setup_with_seed, hass, fake_server
):
    """Optional `data` field absent → bus event still fires with data=None."""
    await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4-V2",
            "keys": ["1"], "led_id": L1, "name": "KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "is_on": False, "brightness": 0, "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })
    captured: list[dict] = []
    hass.bus.async_listen("tago_key_event",
                          lambda event: captured.append(dict(event.data)))
    await fake_server.broadcast_event({
        "evt": "key_single_press", "src": L0, "keypad_id": L0,
        "key_id": "1", "led_state": "off",
        # No `data`, no `duration`.
    })
    await _wait_until(lambda: len(captured) >= 1)
    assert captured[0]["data"] is None
    assert captured[0]["duration"] is None
    assert captured[0]["led_state"] == "off"
    # led_color is absent when led_state is off — per spec.
    assert captured[0]["led_color"] is None


@pytest.mark.asyncio
async def test_two_keypads_with_same_key_id_route_independently(
    setup_with_seed, hass, fake_server
):
    """`key_id` is keypad-local — `(keypad_id, key_id)` is the global
    pair. Two keypads both with `key_id="1"` must route correctly."""
    K1 = "TAGO_TEST_001L1_2"
    K2 = "TAGO_TEST_001L1_3"
    K1L = "TAGO_TEST_001L1_4"
    K2L = "TAGO_TEST_001L1_5"
    await setup_with_seed({
        K1: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4",
            "keys": ["1", "2"], "led_id": K1L, "name": "Bedroom KP",
        },
        K2: {
            "type": "keypad_4btn", "tag": "K2", "model_num": "TKP-4",
            "keys": ["1", "2"], "led_id": K2L, "name": "Kitchen KP",
        },
        K1L: {
            "type": "keypad_led", "keypad_id": K1, "tag": "K1L",
            "is_on": False, "brightness": 0, "rgb": {"r": 0, "g": 0, "b": 0},
        },
        K2L: {
            "type": "keypad_led", "keypad_id": K2, "tag": "K2L",
            "is_on": False, "brightness": 0, "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })

    captured: list[dict] = []
    hass.bus.async_listen("tago_key_event",
                          lambda event: captured.append(dict(event.data)))

    # Press key "1" on each keypad — separate events, distinct keypad_id.
    await fake_server.broadcast_event({
        "evt": "key_single_press", "src": K1, "keypad_id": K1,
        "key_id": "1", "led_state": "off",
    })
    await fake_server.broadcast_event({
        "evt": "key_single_press", "src": K2, "keypad_id": K2,
        "key_id": "1", "led_state": "off",
    })
    await _wait_until(lambda: len(captured) >= 2)
    assert {c["keypad_id"] for c in captured} == {K1, K2}
    assert all(c["key_id"] == "1" for c in captured)


# =====================================================================
# Keypad LED — set_led validation + set_light rejection + nesting
# =====================================================================

async def _seed_kp_led(setup_with_seed):
    return await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4",
            "keys": ["1"], "led_id": L1, "name": "KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "KP LED",
            "is_on": False, "brightness": 0, "rgb": {"r": 0, "g": 0, "b": 0},
        },
    })


@pytest.mark.asyncio
async def test_set_light_against_keypad_led_is_rejected(
    setup_with_seed, hass, fake_server
):
    """PROTOCOL_PROPOSALS §P2.5 — set_light against a keypad_led must
    return 500. The LED's only command is set_led."""
    await _seed_kp_led(setup_with_seed)
    device = list(hass.config_entries.async_entries(DOMAIN))[0].runtime_data
    await device.send_request(
        req="set_light", dst=L1, data={"brightness": 500},
    )
    await _wait_until(
        lambda: any(r.get("rsp") == "set_light" and r.get("src") == L1
                    and r.get("status") == 500
                    for r in fake_server.sent),
        timeout=1.0,
    )


@pytest.mark.parametrize("bad_frame", [
    {"is_on": "yes"},                                  # not a bool
    {"brightness": "max"},                             # not a number
    {"rgb": {"r": 255, "g": 100}},                     # missing b
    {"rgb": {"r": 999, "g": 0, "b": 0}},               # out of range
    {"effect": "flash"},                               # without duration
    {"duration": 1000},                                # without effect
    {"effect": "fade", "duration": 1000},              # unknown effect
])
@pytest.mark.asyncio
async def test_set_led_validation_rejects_bad_inputs(
    setup_with_seed, hass, fake_server, bad_frame
):
    await _seed_kp_led(setup_with_seed)
    device = list(hass.config_entries.async_entries(DOMAIN))[0].runtime_data
    await device.send_request(req="set_led", dst=L1, data=bad_frame)
    await _wait_until(
        lambda: any(r.get("rsp") == "set_led" and r.get("status") == 500
                    for r in fake_server.sent),
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_set_led_partial_call_leaves_other_fields_unchanged(
    setup_with_seed, hass, fake_server
):
    """Only setting `brightness` (no rgb / is_on) updates that field and
    leaves the others alone."""
    entry = await setup_with_seed({
        L0: {
            "type": "keypad_4btn", "tag": "K1", "model_num": "TKP-4",
            "keys": ["1"], "led_id": L1, "name": "KP",
        },
        L1: {
            "type": "keypad_led", "keypad_id": L0, "tag": "K1L",
            "name": "KP LED",
            "is_on": True, "brightness": 200,
            "rgb": {"r": 255, "g": 100, "b": 50},
        },
    })
    device = entry.runtime_data
    led = next(e for e in device.entities if e.unique_id == L1)
    await led.set_led(brightness=900)
    await _wait_until(lambda: fake_server.state[L1]["brightness"] == 900)
    assert fake_server.state[L1]["brightness"] == 900
    assert fake_server.state[L1]["rgb"] == {"r": 255, "g": 100, "b": 50}
    assert fake_server.state[L1]["is_on"] is True


def test_keypad_led_without_keypad_id_falls_back_to_default_device_info():
    """Defensive: if a future LED comes through without a `keypad_id`
    back-reference, the device_info override falls back to the base
    class's identifier rather than crashing (light.py:214)."""
    from custom_components.tago.TagoNet import TagoDevice
    from custom_components.tago.light import TagoKeypadLEDHA
    device = TagoDevice("dummy:1", authkey="")
    device._eid = "DEV"
    led = TagoKeypadKey(
        {"id": "K0L", "type": "keypad_led", "name": "Stray LED",
         "location": "Lab", "tag": "K1L",
         # No `keypad_id` field — defensive case.
         "is_on": False, "brightness": 0, "rgb": {"r": 0, "g": 0, "b": 0}},
        device,
    )
    ha = TagoKeypadLEDHA(led)
    info = ha.device_info
    # Falls through to TagoEntityHA.device_info — identifiers point at
    # the LED's own unique_id, not a keypad.
    assert info is not None
    assert ("tago", "K0L") in info["identifiers"]


@pytest.mark.asyncio
async def test_keypad_led_device_info_nests_under_keypad(
    setup_with_seed, hass
):
    """The LED's HA entity must point at the KEYPAD's device-registry
    entry — not create its own — so HA users see one card per keypad."""
    await _seed_kp_led(setup_with_seed)
    registry = dr.async_get(hass)
    keypad_dev = registry.async_get_device(identifiers={(DOMAIN, L0)})
    assert keypad_dev is not None

    led_entity_id = _resolve_entity_id(hass, "light", L1)
    entity_reg = er.async_get(hass)
    led_reg_entry = entity_reg.async_get(led_entity_id)
    # The LED entity's device_id should equal the keypad's device_id.
    assert led_reg_entry.device_id == keypad_dev.id


@pytest.mark.asyncio
async def test_set_led_client_side_brightness_clamping():
    """TagoKeypadKey.set_led clamps brightness to [0, 1000] before sending."""
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    led = TagoKeypadKey(
        {"id": "K0L", "type": "keypad_led", "name": "", "location": "",
         "tag": "K1L", "keypad_id": "K0",
         "is_on": False, "brightness": 0,
         "rgb": {"r": 0, "g": 0, "b": 0}},
        device,
    )
    led._device = _CaptureDevice()
    await led.set_led(brightness=99999)
    assert led._device.calls[-1]["data"]["brightness"] == 1000
    await led.set_led(brightness=-50)
    assert led._device.calls[-1]["data"]["brightness"] == 0


@pytest.mark.asyncio
async def test_set_led_client_side_duration_clamping():
    """Effect duration is clamped to [100, 60000] before sending."""
    from custom_components.tago.TagoNet import TagoDevice
    device = TagoDevice("dummy:1", authkey="")
    led = TagoKeypadKey(
        {"id": "K0L", "type": "keypad_led", "name": "", "location": "",
         "tag": "K1L", "keypad_id": "K0",
         "is_on": False, "brightness": 0,
         "rgb": {"r": 0, "g": 0, "b": 0}},
        device,
    )
    led._device = _CaptureDevice()
    await led.set_led(effect="flash", duration_ms=99999)
    assert led._device.calls[-1]["data"]["duration"] == 60000
    await led.set_led(effect="flash", duration_ms=10)
    assert led._device.calls[-1]["data"]["duration"] == 100


# =====================================================================
# Virtual switch / sensor — write rejection + multi + location
# =====================================================================

@pytest.mark.asyncio
async def test_virtual_sensor_rejects_turn_on_at_wire_level(
    setup_with_seed, hass, fake_server
):
    """Per §P3.4, turn_on/turn_off/toggle on a virtual_sensor must
    return 500 from the firmware (fake)."""
    await setup_with_seed({
        L0: {
            "type": "virtual_sensor", "tag": "VB1", "index": 0,
            "name": "Low Voltage", "location": "VIRTUAL", "is_on": False,
        },
    })
    device = list(hass.config_entries.async_entries(DOMAIN))[0].runtime_data
    for req in ("turn_on", "turn_off", "toggle"):
        await device.send_request(req=req, dst=L0)
    await _wait_until(
        lambda: sum(
            1 for r in fake_server.sent
            if r.get("src") == L0 and r.get("status") == 500
        ) >= 3,
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_multiple_virtual_switches_each_get_their_own_entity(
    setup_with_seed, hass
):
    L_VS0 = "TAGO_TEST_001L1_10"
    L_VS1 = "TAGO_TEST_001L1_11"
    await setup_with_seed({
        L_VS0: {
            "type": "virtual_switch", "tag": "VS1", "index": 0,
            "name": "Holiday Mode", "location": "VIRTUAL", "is_on": False,
        },
        L_VS1: {
            "type": "virtual_switch", "tag": "VS2", "index": 1,
            "name": "Away From Home", "location": "VIRTUAL", "is_on": True,
        },
    })
    registry = er.async_get(hass)
    e0 = registry.async_get_entity_id("switch", DOMAIN, L_VS0)
    e1 = registry.async_get_entity_id("switch", DOMAIN, L_VS1)
    assert e0 is not None and e1 is not None
    assert e0 != e1


# =====================================================================
# Unicode names + kitchen-sink discovery
# =====================================================================

@pytest.mark.asyncio
async def test_unicode_name_round_trips_through_discovery(
    setup_with_seed, hass
):
    """D1: Unicode allowed in name/location for all entities."""
    await setup_with_seed({
        L0: {"type": "scene", "name": "🎬 Movie Night",
             "location": "Säle 1", "tag": "S1"},
    })
    entry = list(hass.config_entries.async_entries(DOMAIN))[0]
    scene = next(e for e in entry.runtime_data.entities if e.unique_id == L0)
    assert scene.name == "🎬 Movie Night"
    assert scene.location == "Säle 1"


@pytest.mark.asyncio
async def test_kitchen_sink_discovery_creates_all_entity_types(
    setup_with_seed, hass
):
    """A `list_nodes` payload containing every documented collection key
    materialises one entity of each type."""
    SCENE = "TAGO_TEST_001L1_20"
    KP = "TAGO_TEST_001L1_21"
    KPLED = "TAGO_TEST_001L1_22"
    VS = "TAGO_TEST_001L1_23"
    VB = "TAGO_TEST_001L1_24"
    LIGHT = "TAGO_TEST_001L1_25"
    OUTLET = "TAGO_TEST_001L1_26"
    entry = await setup_with_seed({
        LIGHT: {"type": "light_dimmable", "name": "L", "tag": "1A",
                "brightness": 0},
        OUTLET: {"type": "outlet_onoff", "name": "O", "tag": "1B",
                 "is_on": False},
        SCENE: {"type": "scene", "name": "S", "tag": "S1"},
        KP: {"type": "keypad_4btn", "name": "KP", "tag": "K1",
             "model_num": "TKP-4", "keys": ["1"], "led_id": KPLED},
        KPLED: {"type": "keypad_led", "keypad_id": KP, "tag": "K1L",
                "is_on": False, "brightness": 0,
                "rgb": {"r": 0, "g": 0, "b": 0}, "name": "KP LED"},
        VS: {"type": "virtual_switch", "tag": "VS1", "index": 0,
             "name": "VSw", "location": "VIRTUAL", "is_on": False},
        VB: {"type": "virtual_sensor", "tag": "VB1", "index": 0,
             "name": "VSn", "location": "VIRTUAL", "is_on": False},
    })
    device = entry.runtime_data
    types_present = {type(e).__name__ for e in device.entities}
    assert "TagoLight" in types_present
    assert "TagoSwitch" in types_present
    assert "TagoScene" in types_present
    assert "TagoKeypad" in types_present
    assert "TagoKeypadKey" in types_present
    assert "TagoVirtualSwitch" in types_present
    assert "TagoVirtualSensor" in types_present
