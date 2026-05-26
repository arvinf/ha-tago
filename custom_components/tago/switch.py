"""Platform for switch integration."""
from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry

from .entity import TagoEntityHA
from .TagoNet import TagoDevice, TagoSwitch

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

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    items: list[TagoSwitchHA] = list()
    device: TagoDevice = entry.runtime_data
    for e in device.entities:
        if isinstance(e, TagoSwitch):
            items.append(TagoSwitchHA(e))

    async_add_entities(items)
