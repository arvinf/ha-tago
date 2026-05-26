"""Small focused tests that close out specific coverage gaps the larger
test files don't naturally hit. Each test cites the line range it covers.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from custom_components.tago import TagoNet as _tn
from custom_components.tago.const import CONF_HOSTSTR, CONF_PIN, DOMAIN
from custom_components.tago.entity import TagoEntityHA
from custom_components.tago.light import TagoLightHA
from custom_components.tago.TagoNet import (
    TagoCover,
    TagoDevice,
    TagoEntity,
    TagoFan,
    TagoLight,
    TagoMessage,
    TagoSwitch,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

L0 = "TAGO_TEST_001L1_0"

pytestmark = [pytest.mark.enable_socket]


# =====================================================================
# TagoMessage helpers — refers_to / is_response / is_event / is_request
# (TagoNet.py:90-108)
# =====================================================================

def test_tagomessage_refers_to_matches_only_with_set_ref():
    msg = TagoMessage()
    msg.ref = "abc"
    assert msg.refers_to("abc") is True
    assert msg.refers_to("xyz") is False
    msg.ref = None
    assert not msg.refers_to("abc")


def test_tagomessage_is_response_with_and_without_filter():
    msg = TagoMessage()
    msg.rsp = "ping"
    assert msg.is_response() is True
    assert msg.is_response("ping")
    assert msg.is_response("set_light") is False
    msg.rsp = None
    assert msg.is_response() is False


def test_tagomessage_is_event_with_and_without_filter():
    msg = TagoMessage()
    msg.evt = "state_changed"
    assert msg.is_event() is True
    assert msg.is_event("state_changed")
    assert msg.is_event("other") is False
    msg.evt = None
    assert msg.is_event() is False


def test_tagomessage_is_request_with_and_without_filter():
    msg = TagoMessage()
    msg.req = "turn_on"
    assert msg.is_request() is True
    assert msg.is_request("turn_on")
    assert msg.is_request("turn_off") is False
    msg.req = None
    assert msg.is_request() is False


# =====================================================================
# TagoLight error paths — set_brightness/set_ct/set_colour with bad input
# (TagoNet.py:938, 958, 967)
# =====================================================================

@pytest.mark.asyncio
async def test_tagolight_set_brightness_requires_value():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    with pytest.raises(ValueError, match="Brightness must be specified"):
        await light.set_brightness(brightness=None)


@pytest.mark.asyncio
async def test_tagolight_set_ct_requires_value():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_ww", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    with pytest.raises(ValueError, match="Colour Temperature must be specified"):
        await light.set_ct(ct=None)


@pytest.mark.asyncio
async def test_tagolight_set_colour_requires_pair():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_rgb", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    with pytest.raises(ValueError, match="Colour XY pair must be specified"):
        await light.set_colour(colour=None)
    with pytest.raises(ValueError, match="Colour XY pair must be specified"):
        await light.set_colour(colour=(0.4,))


# =====================================================================
# TagoLight `is_dimmable` property + non-CCT property None returns
# (light.py:32, 105, 112, 119, 125)
# =====================================================================

def _make_light_for(typ: str) -> TagoLight:
    device = TagoDevice("dummy:1", authkey="k")
    return TagoLight(
        {"id": "L0", "type": typ, "name": "x", "location": "y", "tag": "1A"},
        device,
    )


def test_tagolighthа_is_dimmable_property():
    onoff = _make_light_for(TagoLight.LIGHT_ONOFF)
    dimmable = _make_light_for(TagoLight.LIGHT_DIMMABLE)
    assert TagoLightHA(onoff).is_dimmable is False
    assert TagoLightHA(dimmable).is_dimmable is True


def test_non_cct_light_color_temp_properties_return_none():
    light = _make_light_for(TagoLight.LIGHT_DIMMABLE)
    ha = TagoLightHA(light)
    assert ha.color_temp_kelvin is None
    assert ha.min_color_temp_kelvin is None
    assert ha.max_color_temp_kelvin is None


def test_non_rgb_light_xy_property_returns_none():
    light = _make_light_for(TagoLight.LIGHT_DIMMABLE)
    assert TagoLightHA(light).xy_color is None


def test_light_color_mode_unknown_type_returns_onoff():
    """`color_mode` and `supported_color_modes` default to ONOFF for any
    unrecognised type. (light.py:98 / 56 / 74)"""
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "totally_unknown", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    from homeassistant.components.light import ColorMode
    ha = TagoLightHA(light)
    assert ha.color_mode == ColorMode.ONOFF
    assert ha.supported_color_modes == [ColorMode.ONOFF]
    assert ha.type_to_string == ""


def test_light_rgb_color_mode_is_xy(monkeypatch):
    """light.py:87 — color_mode for LIGHT_RGB returns XY."""
    from homeassistant.components.light import ColorMode
    light = _make_light_for(TagoLight.LIGHT_RGB)
    light._colour_x = 0.5
    light._colour_y = 0.4
    assert TagoLightHA(light).color_mode == ColorMode.XY


def test_light_rgb_cct_xy_mode_when_xy_nonzero():
    """light.py:95 — LIGHT_RGB_CCT with non-zero x/y returns XY."""
    from homeassistant.components.light import ColorMode
    light = _make_light_for(TagoLight.LIGHT_RGB_CCT)
    light._colour_x = 0.5
    light._colour_y = 0.4
    assert TagoLightHA(light).color_mode == ColorMode.XY


# =====================================================================
# async_stop_transition — calls TagoLight.stop_ramp (light.py:175)
# =====================================================================

class _CaptureDevice:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_request(self, req: str, data=None, dst=None, **kw):
        self.calls.append({"req": req, "data": data or {}, "dst": dst})


@pytest.mark.asyncio
async def test_async_stop_transition_sends_stop_ramp():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    light._device = _CaptureDevice()
    ha = TagoLightHA(light)
    await ha.async_stop_transition()
    assert any(c["req"] == TagoLight.REQ_STOP_RAMP for c in light._device.calls)


# =====================================================================
# TagoLight.adjust_brightness — wrapper around set_light (TagoNet.py:952-953)
# =====================================================================

@pytest.mark.asyncio
async def test_tagolight_adjust_brightness_uses_set_light():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    light._device = _CaptureDevice()
    await light.adjust_brightness(brightness=0.5)
    call = light._device.calls[0]
    assert call["req"] == TagoLight.REQ_SET_LIGHT
    assert call["data"][TagoLight.PROP_BRIGHTNESS] == 500


# =====================================================================
# TagoLight.stop_ramp — direct send_request (TagoNet.py:976)
# =====================================================================

@pytest.mark.asyncio
async def test_tagolight_stop_ramp_sends_stop_ramp_request():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    light._device = _CaptureDevice()
    await light.stop_ramp()
    assert light._device.calls[0]["req"] == TagoLight.REQ_STOP_RAMP


# =====================================================================
# TagoLight.set_light_flash + ATTR_RATE on set_brightness (TagoNet.py:914, 904)
# =====================================================================

@pytest.mark.asyncio
async def test_tagolight_set_light_flash_sends_effect():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    light._device = _CaptureDevice()
    await light.set_light_flash(duration=5)
    call = light._device.calls[0]
    assert call["req"] == TagoLight.REQ_LIGHT_EFFECT
    assert call["data"][TagoLight.PROP_EFFECT] == TagoLight.VALUE_FLASH
    assert call["data"][TagoLight.PROP_DURATION] == 5


@pytest.mark.asyncio
async def test_tagolight_set_brightness_with_rate_emits_rate_field():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    light._device = _CaptureDevice()
    await light.set_brightness(brightness=0.5, rate=2.5)
    call = light._device.calls[0]
    assert call["data"][TagoLight.PROP_RATE] == 2500


# =====================================================================
# TagoLight.ramp_update — incremental property updates (TagoNet.py:1000-1006)
# =====================================================================

def test_tagolight_ramp_update_writes_all_four_fields():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_rgbww", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    light.ramp_update([800, 500, 0.5, 0.4])
    assert light._brightness == 800
    assert light._ct == 500
    assert light._colour_x == 0.5
    assert light._colour_y == 0.4


def test_tagolight_ramp_update_skips_none_values():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_rgbww", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    light._brightness = 100
    light._ct = 200
    light._colour_x = 0.1
    light._colour_y = 0.2
    light.ramp_update([None, None, None, None])
    assert light._brightness == 100
    assert light._ct == 200
    assert light._colour_x == 0.1
    assert light._colour_y == 0.2


# =====================================================================
# TagoFan.toggle (TagoNet.py:1146)
# =====================================================================

@pytest.mark.asyncio
async def test_tagofan_toggle_sends_toggle_request():
    device = TagoDevice("dummy:1", authkey="k")
    fan = TagoFan(
        {"id": "F0", "type": "fan_onoff", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    fan._device = _CaptureDevice()
    await fan.toggle()
    assert fan._device.calls[0]["req"] == TagoFan.REQ_TOGGLE


# =====================================================================
# TagoDevice.reboot/identify no-op when disconnected (TagoNet.py:749, 755)
# =====================================================================

@pytest.mark.asyncio
async def test_device_reboot_noop_when_disconnected():
    device = TagoDevice("dummy:1", authkey="k")
    assert not device.is_connected
    # Should not raise; should not attempt to send anything.
    await device.reboot()


@pytest.mark.asyncio
async def test_device_identify_noop_when_disconnected():
    device = TagoDevice("dummy:1", authkey="k")
    assert not device.is_connected
    await device.identify()


# =====================================================================
# entity.py — __repr__, is_of_domain, type_to_string default,
# device_info for unused entities (entity.py:24, 33, 40, 72)
# =====================================================================

def test_tagoentityha_repr_returns_json_blob():
    device = TagoDevice("dummy:1", authkey="k")
    device._eid = "TAGO_TEST_001"
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "Kitchen", "location": "K", "tag": "1A"},
        device,
    )
    ha = TagoLightHA(light)
    rendered = repr(ha)
    parsed = json.loads(rendered)
    assert parsed["id"] == "L0"
    assert parsed["name"] == "Kitchen"


def test_tagoentityha_is_of_domain_default_false():
    device = TagoDevice("dummy:1", authkey="k")
    light = TagoLight(
        {"id": "L0", "type": "light_dimmable", "name": "x", "location": "y", "tag": "1A"},
        device,
    )
    ha = TagoLightHA(light)
    assert ha.is_of_domain("anything") is False


def test_tagoentityha_type_to_string_base_returns_empty_string():
    device = TagoDevice("dummy:1", authkey="k")
    entity = TagoEntity(
        {"id": "E0", "type": "UNUSED", "name": "x", "location": "y", "tag": ""},
        device,
    )
    base = TagoEntityHA(entity)
    assert base.type_to_string == ""


def test_tagoentityha_device_info_none_when_entity_unused():
    device = TagoDevice("dummy:1", authkey="k")
    device._eid = "TAGO_TEST_001"
    entity = TagoEntity(
        {"id": "E0", "type": "UNUSED", "name": "x", "location": "y", "tag": ""},
        device,
    )
    assert TagoEntityHA(entity).device_info is None


# =====================================================================
# __init__.py — unused load cleanup (__init__.py:55-62)
# =====================================================================

@pytest.mark.asyncio
async def test_unused_loads_trigger_device_registry_cleanup(
    hass, enable_custom_integrations, fake_server
):
    """Seeded UNUSED entities exercise the cleanup loop in async_setup_entry.
    The loop tries to remove the device-registry entry for each unused load;
    if none exists, the `except` branch swallows the AttributeError."""
    fake_server.seed({
        L0: {"type": "UNUSED", "name": "Unused Slot", "tag": "1A"},
    })
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        # The UNUSED entity exists in the device's entity list but has no
        # HA-side platform wrapper.
        device = entry.runtime_data
        assert any(e.is_unused() for e in device.entities)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


# =====================================================================
# __init__.py — async_setup_entry raises ConfigEntryNotReady on bad host
# (__init__.py:47-48)
# =====================================================================

@pytest.mark.asyncio
async def test_async_setup_entry_raises_not_ready_on_unreachable_host(
    hass, enable_custom_integrations, socket_enabled
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: "127.0.0.1:1", CONF_PIN: ""},  # bogus port
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    # async_setup eats the ConfigEntryNotReady and sets entry state to retry.
    # We assert async_setup returns False (failed setup).
    result = await hass.config_entries.async_setup(entry.entry_id)
    assert result is False


# =====================================================================
# TagoNet.py — TagoEntity.name default fallback when name unset
# (TagoNet.py:267 ish)
# =====================================================================

def test_tagoentity_name_falls_back_to_device_and_tag_when_unnamed():
    device = TagoDevice("dummy:1", authkey="k")
    device._eid = "TAGO_DEV"
    entity = TagoEntity(
        {"id": "E0", "type": "light_dimmable", "name": "", "location": "", "tag": "1A"},
        device,
    )
    ha = TagoEntityHA(entity)
    # No name on entity → fallback to "<device_id> <tag>"
    assert ha.name == "TAGO_DEV 1A"


def test_tagoentity_name_falls_back_to_unique_id_when_no_tag():
    device = TagoDevice("dummy:1", authkey="k")
    device._eid = "TAGO_DEV"
    entity = TagoEntity(
        {"id": "E0", "type": "light_dimmable", "name": "", "location": "", "tag": ""},
        device,
    )
    # tag is "" which is falsy → fallback further to unique_id
    ha = TagoEntityHA(entity)
    assert ha.name == "E0"
