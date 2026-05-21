from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.tago import generate_device_info
from custom_components.tago.TagoNet import TagoCover, TagoDevice, TagoFan, TagoLight, TagoSwitch
from custom_components.tago.cover import TagoCoverHA, async_setup_entry as setup_cover
from custom_components.tago.entity import TagoEntityHA
from custom_components.tago.fan import TagoFanHA, async_setup_entry as setup_fan
from custom_components.tago.light import TagoLightHA, async_setup_entry as setup_light
from custom_components.tago.switch import TagoSwitchHA, async_setup_entry as setup_switch


@pytest.mark.asyncio
async def test_platform_setup_creates_expected_ha_entity_types() -> None:
    device = TagoDevice("dummy:1", authkey="k")
    device._eid = "SN-1234"
    device._serialnum = "SN-1234"
    device._modelnum = "TAGO-X"
    device._firmware_rev = "1.0"

    light = TagoLight(
        {"id": "l1", "type": "light_dimmable", "name": "Light", "location": "Kitchen", "tag": "L"},
        device,
    )
    fan = TagoFan(
        {"id": "f1", "type": "fan_onoff", "name": "Fan", "location": "Bedroom", "tag": "F"},
        device,
    )
    cover = TagoCover(
        {"id": "c1", "type": "cover_shades", "name": "Cover", "location": "Office", "tag": "C"},
        device,
    )
    switch = TagoSwitch(
        {"id": "s1", "type": "relay_switch", "name": "Switch", "location": "Plant", "tag": "S"},
        device,
    )

    device._entities = [light, fan, cover, switch]
    entry = SimpleNamespace(runtime_data=device)

    added: list[object] = []

    def add_entities(items):
        added.extend(items)

    await setup_light(None, entry, add_entities)
    await setup_fan(None, entry, add_entities)
    await setup_cover(None, entry, add_entities)
    await setup_switch(None, entry, add_entities)

    assert any(isinstance(entity, TagoLightHA) for entity in added)
    assert any(isinstance(entity, TagoFanHA) for entity in added)
    assert any(isinstance(entity, TagoCoverHA) for entity in added)
    assert any(isinstance(entity, TagoSwitchHA) for entity in added)


def test_generate_device_info_uses_device_identity() -> None:
    device = TagoDevice("dummy:1", authkey="k")
    device._eid = "SN-1234"
    device._serialnum = "SN-1234"
    device._modelnum = "TAGO-X"
    device._firmware_rev = "1.0.0"

    info = generate_device_info(device)

    assert info["name"] == "Device SN-1234"
    assert info["manufacturer"] == "TAGO"
    assert info["model"] == "TAGO-X"
    assert info["serial_number"] == "SN-1234"


def test_entity_device_info_includes_suggested_area_when_location_set() -> None:
    device = TagoDevice("dummy:1", authkey="k")
    device._eid = "SN-1234"

    light = TagoLight(
        {
            "id": "l1",
            "type": "light_dimmable",
            "name": "Kitchen Main",
            "location": "Kitchen",
            "tag": "L1",
        },
        device,
    )

    wrapper = TagoEntityHA(light)
    info = wrapper.device_info

    assert info is not None
    assert info["suggested_area"] == "Kitchen"
