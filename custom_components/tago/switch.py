"""Platform for switch integration."""
from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory

from .entity import TagoEntityHA
from .TagoNet import TagoGateway, TagoSwitch, TagoVirtualSwitch

# Single shared WebSocket; no per-platform serialization needed.
PARALLEL_UPDATES = 0


class TagoSwitchHA(TagoEntityHA, SwitchEntity):
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, entity: TagoSwitch):
        super().__init__(entity)

    @property
    def is_on(self) -> bool:
        return self._entity.is_on

    async def async_turn_on(self, **kwargs):
        await self._entity.turn_on()

    async def async_turn_off(self, **kwargs):
        await self._entity.turn_off()


class TagoVirtualSwitchHA(TagoEntityHA, SwitchEntity):
    """HA switch entity for a TagoVirtualSwitch — a writable on/off flag
    consumed by the firmware's automation engine.

    Per PROTOCOL_PROPOSALS §P3.5 these are CONFIG-grade entities and
    disabled-by-default; users opt in by enabling the entity once
    they've wired it into an automation."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, entity: TagoVirtualSwitch):
        super().__init__(entity)

    @property
    def is_on(self) -> bool:
        return self._entity.is_on

    async def async_turn_on(self, **kwargs):
        await self._entity.turn_on()

    async def async_turn_off(self, **kwargs):
        await self._entity.turn_off()


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    items: list[SwitchEntity] = []
    gateway: TagoGateway = entry.runtime_data
    for e in gateway.entities:
        if isinstance(e, TagoSwitch):
            items.append(TagoSwitchHA(e))
        elif isinstance(e, TagoVirtualSwitch):
            items.append(TagoVirtualSwitchHA(e))

    async_add_entities(items)
