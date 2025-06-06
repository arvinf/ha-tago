"""Platform for fan integration."""
from __future__ import annotations

import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.util.percentage import ranged_value_to_percentage

from .entity import TagoEntityHA
from .TagoNet import TagoDevice, TagoFan

class TagoFanHA(TagoEntityHA, FanEntity):
    def __init__(self, entity: TagoFan):
        super().__init__(entity)
        
    @property
    def type_to_string(self) -> int:
        if self._entity.type == TagoFan.ONOFF:
            return 'ON/OFF Fan'
        if self._entity.type == TagoFan.DIMMABLE:
            return 'Adjustable Fan'
        return ''

    @property
    def is_on(self):
        return self._entity.state == TagoFan.STATE_ON 

    @property
    def supported_features(self) -> int | None:
        return FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF if self._entity.type == TagoFan.ONOFF else FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF | FanEntityFeature.SET_SPEED

    @property
    def percentage(self) -> int | None:
        return ranged_value_to_percentage(TagoFan.MAX_VALUE, self._entity.value)

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        if percentage:
            return await self._entity.set_speed(percentage)

        await self._entity.turn_on()

    async def async_turn_off(self, **kwargs):
        await self._entity.turn_off()

    async def async_set_percentage(self, percentage: int) -> None:
        await self._entity.set_speed(percentage)

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):    
    items: list[TagoFanHA] = list()
    device : TagoDevice = entry.runtime_data
    for e in device.entities:
        if type(e) == TagoFan:
            items.append(TagoFanHA(e))

    async_add_entities(items)
