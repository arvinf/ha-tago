"""Layer A — HA-specific wire invariants (CLIENT_TEST_GUIDE §1a).

Pure unit tests against the integration's frame builders and parsers. No
WebSocket. No fake server. Just the conversion math and the parsing logic
sitting between HA's domain types (0..255 brightness, seconds transition,
Kelvin CT, xy color) and the wire (0..1000 ints, ms ints, ratio CT, xy floats).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.tago.TagoNet import TagoDevice, TagoLight, TagoMessage
from custom_components.tago.entity import TagoEntityHA


def _make_light(typ: str = TagoLight.LIGHT_DIMMABLE, **extra) -> TagoLight:
    device = TagoDevice("dummy:1", authkey="k")
    payload = {"id": "L0", "type": typ, "name": "Test", "location": "Lab", "tag": "1A"}
    payload.update(extra)
    return TagoLight(payload, device)


class _Capture:
    """Stand-in for TagoDevice that records `send_request` instead of sending."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_request(self, req: str, data: dict | None = None, dst: str = None, **kw):
        self.calls.append({"req": req, "data": data or {}, "dst": dst})


# ---------------- brightness 0..255 ↔ 0..1000 ----------------

@pytest.mark.parametrize("ha_val, wire_val", [(255, 1000), (128, 502), (0, 0), (1, 4), (200, 784)])
def test_brightness_ha_to_wire_round_consistently(ha_val, wire_val):
    fraction = TagoEntityHA.convert_value_to_device(ha_val)
    wire = TagoLight.convert_value_from_float(fraction)
    assert wire == wire_val


@pytest.mark.parametrize("wire_val, ha_val", [(1000, 255), (750, 191), (500, 128), (0, 0)])
def test_brightness_wire_to_ha_round_consistently(wire_val, ha_val):
    fraction = TagoLight.convert_value_to_float(wire_val)
    ha = TagoEntityHA.convert_value_from_device(fraction)
    assert ha == ha_val


def test_brightness_round_trip_at_endpoints():
    for ha in (0, 1, 128, 254, 255):
        f = TagoEntityHA.convert_value_to_device(ha)
        wire = TagoLight.convert_value_from_float(f)
        back = TagoEntityHA.convert_value_from_device(TagoLight.convert_value_to_float(wire))
        assert abs(back - ha) <= 1


# ---------------- transition (s) → duration (ms) ----------------

@pytest.mark.asyncio
async def test_transition_seconds_to_duration_ms_basic():
    light = _make_light()
    light._device = _Capture()
    await light.set_brightness(brightness=0.5, duration=1.5)
    payload = light._device.calls[0]["data"]
    assert payload[TagoLight.PROP_DURATION] == 1500


@pytest.mark.asyncio
async def test_transition_below_min_is_dropped_as_instant():
    """CLIENT_TEST_GUIDE §1a: HA must clamp transition to [300, 10000] ms.
    Below the minimum → omit `duration` (apply instantly)."""
    light = _make_light()
    light._device = _Capture()
    await light.set_brightness(brightness=0.5, duration=0.05)
    payload = light._device.calls[0]["data"]
    # Below CONFIG_RAMP_DURATION_MIN → no duration on the wire.
    assert TagoLight.PROP_DURATION not in payload


@pytest.mark.asyncio
async def test_transition_above_max_clamped_to_ten_seconds():
    light = _make_light()
    light._device = _Capture()
    await light.set_brightness(brightness=0.5, duration=99.0)  # 99s → above 10s
    payload = light._device.calls[0]["data"]
    assert payload[TagoLight.PROP_DURATION] == 10000


# ---------------- Kelvin ↔ percent CT ----------------

@pytest.mark.asyncio
async def test_kelvin_to_percent_uses_ratio_against_ct_range():
    """K=4600 with ct_range [2700, 6500] → 0.5 → 500/1000."""
    from custom_components.tago.light import TagoLightHA
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    light._ct_range_min = 2700
    light._ct_range_max = 6500
    light._device = _Capture()
    ha = TagoLightHA(light)

    from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN
    await ha.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 4600})

    payload = light._device.calls[0]["data"]
    assert payload[TagoLight.PROP_CT] == 500


@pytest.mark.asyncio
async def test_kelvin_at_warm_endpoint_maps_to_ct_zero():
    from custom_components.tago.light import TagoLightHA
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    light._ct_range_min = 2700
    light._ct_range_max = 6500
    light._device = _Capture()
    ha = TagoLightHA(light)

    from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN
    await ha.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 2700})

    assert light._device.calls[0]["data"][TagoLight.PROP_CT] == 0


@pytest.mark.asyncio
async def test_kelvin_at_cool_endpoint_maps_to_ct_thousand():
    from custom_components.tago.light import TagoLightHA
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    light._ct_range_min = 2700
    light._ct_range_max = 6500
    light._device = _Capture()
    ha = TagoLightHA(light)

    from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN
    await ha.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 6500})

    assert light._device.calls[0]["data"][TagoLight.PROP_CT] == 1000


def test_percent_to_kelvin_read_back_via_color_temp_kelvin():
    from custom_components.tago.light import TagoLightHA
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    light._ct_range_min = 2700
    light._ct_range_max = 6500
    light._ct = 500
    ha = TagoLightHA(light)
    assert ha.color_temp_kelvin == int(0.5 * (6500 - 2700)) + 2700


def test_ct_range_from_get_config_is_applied_to_entity():
    """ct_range arrives as `[warm_K, cool_K]` per PROTOCOL.md §13.1."""
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    light.parse_state_json({TagoLight.PROP_CT_RANGE: [2700, 6500]})
    assert (light._ct_range_min, light._ct_range_max) == (2700, 6500)


def test_ct_range_clamped_to_safe_bounds():
    """Bounds outside the HA-safe range get clamped to [CT_MIN, CT_MAX]."""
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    light.parse_state_json({TagoLight.PROP_CT_RANGE: [800, 99999]})
    assert light._ct_range_min == TagoLight.CT_MIN
    assert light._ct_range_max == TagoLight.CT_MAX


@pytest.mark.asyncio
async def test_handle_config_change_applies_ct_range_indexes():
    """config_changed event with `ct_range` should set min from [0] and max
    from [1] — fixes the bug where both were assigned the whole list."""
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    msg = TagoMessage.from_payload(
        json.dumps({"evt": "config_changed", "src": "L0", "ct_range": [2200, 5000]})
    )
    await light.handle_config_change(msg)
    assert (light._ct_range_min, light._ct_range_max) == (2200, 5000)


# ---------------- RGB → xy color ----------------

@pytest.mark.asyncio
async def test_xy_color_forwarded_to_wire_as_x_y_floats():
    """HA passes xy directly (RGB→xy done in HA core color util); integration forwards."""
    from custom_components.tago.light import TagoLightHA
    from homeassistant.components.light import ATTR_XY_COLOR
    light = _make_light(typ=TagoLight.LIGHT_RGB)
    light._device = _Capture()
    ha = TagoLightHA(light)

    await ha.async_turn_on(**{ATTR_XY_COLOR: (0.64, 0.33)})
    payload = light._device.calls[0]["data"]
    assert payload[TagoLight.PROP_X] == pytest.approx(0.64)
    assert payload[TagoLight.PROP_Y] == pytest.approx(0.33)


# ---------------- ramp parsing ----------------

@pytest.mark.asyncio
async def test_state_changed_with_ramp_starts_local_ramp():
    light = _make_light()
    msg = TagoMessage.from_payload(
        json.dumps(
            {
                "evt": "state_changed", "src": "L0", "id": "L0", "type": "light_dimmable",
                "brightness": 800,
                "ramp": {"duration": 1000, "elapsed": 0,
                         "start": {"brightness": 0}, "end": {"brightness": 800}},
            }
        )
    )
    await light.handle_state_change(msg)
    assert light.is_ramp_active
    # Clean up the spawned ramp task before the loop exits.
    if light._ramp:
        light._ramp.cancel()


@pytest.mark.asyncio
async def test_state_changed_without_ramp_clears_ramp():
    light = _make_light()
    # Prime a ramp.
    msg1 = TagoMessage.from_payload(
        json.dumps({
            "evt": "state_changed", "src": "L0",
            "brightness": 0,
            "ramp": {"duration": 1000, "elapsed": 0,
                     "start": {"brightness": 0}, "end": {"brightness": 800}},
        })
    )
    await light.handle_state_change(msg1)
    assert light.is_ramp_active

    # End-of-ramp state_changed has no ramp object.
    msg2 = TagoMessage.from_payload(
        json.dumps({"evt": "state_changed", "src": "L0", "brightness": 800})
    )
    await light.handle_state_change(msg2)
    assert not light.is_ramp_active


# ---------------- state_changed self-echo handling ----------------

@pytest.mark.asyncio
async def test_self_echo_state_changed_updates_internal_state_only():
    """The HA platform should accept the broadcast echo of its own set_light;
    parse_state_json mutates internal state without raising or clearing other
    fields the echo doesn't carry."""
    light = _make_light(typ=TagoLight.LIGHT_CCT)
    light._brightness = 800
    light._ct = 400
    msg = TagoMessage.from_payload(
        json.dumps({"evt": "state_changed", "src": "L0", "brightness": 800})  # no ct
    )
    await light.handle_state_change(msg)
    assert light._brightness == 800
    assert light._ct == 400  # not clobbered by absent key


# ---------------- reconnect preserves HA unique_id mapping ----------------

@pytest.mark.asyncio
async def test_unique_id_is_entity_id_and_stable_across_reparse():
    light = _make_light()
    first_uid = light.unique_id
    light.update_from_discovery_payload(
        {"id": "L0", "type": "light_dimmable", "name": "Renamed", "location": "L2", "tag": "1B"}
    )
    assert light.unique_id == first_uid


# ---------------- Wire-layer HA convention checks ----------------

@pytest.mark.asyncio
async def test_set_brightness_does_not_send_brightness_when_only_color_provided():
    """xy without brightness should not bundle a stale brightness on the wire."""
    from custom_components.tago.light import TagoLightHA
    from homeassistant.components.light import ATTR_XY_COLOR
    light = _make_light(typ=TagoLight.LIGHT_RGB)
    light._device = _Capture()
    ha = TagoLightHA(light)

    await ha.async_turn_on(**{ATTR_XY_COLOR: (0.5, 0.4)})
    payload = light._device.calls[0]["data"]
    assert TagoLight.PROP_X in payload and TagoLight.PROP_Y in payload
    # No brightness key when HA didn't supply one.
    assert TagoLight.PROP_BRIGHTNESS not in payload or payload.get(TagoLight.PROP_BRIGHTNESS) is None


@pytest.mark.asyncio
async def test_async_turn_off_zero_brightness_round_trips():
    from custom_components.tago.light import TagoLightHA
    light = _make_light()
    light._device = _Capture()
    light._brightness = 500
    ha = TagoLightHA(light)
    await ha.async_turn_off()
    payload = light._device.calls[0]["data"]
    assert payload[TagoLight.PROP_BRIGHTNESS] == 0
