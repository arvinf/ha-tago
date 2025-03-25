"""Platform for switch integration."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry

from .entity import TagoEntityHA
from .TagoNet import TagoDevice, TagoSwitch

_LOGGER = logging.getLogger(__name__)


class TagoSwitchHA(TagoEntityHA, SwitchEntity):
    def __init__(self, entity: TagoSwitch):
        super().__init__(entity)

        if self._entity.type == TagoSwitch.OUTLET:
            self._attr_device_class = SwitchDeviceClass.OUTLET
        else:
            self._attr_device_class = SwitchDeviceClass.SWITCH

    @property
    def is_on(self):
        return self._entity.state == TagoSwitch.STATE_ON

    async def async_turn_on(self, **kwargs):
        await self._entity.turn_on()

    async def async_turn_off(self, **kwargs):
        await self._entity.turn_off()

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    lights: list[TagoSwitchHA] = list()
    device: TagoDevice = entry.runtime_data
    for e in device.entities:
        if type(e) == TagoSwitch:
            lights.append(TagoSwitchHA(e))

    async_add_entities(lights)
