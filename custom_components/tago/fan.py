"""Platform for fan integration.

PROTOCOL.md §7a — `fan_onoff` is strictly on/off. There is no speed control
on the wire (the firmware has no `set_fan` dispatcher).
"""
from __future__ import annotations

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry

from .entity import TagoEntityHA
from .TagoNet import TagoFan, TagoGateway

# Single shared WebSocket; no per-platform serialization needed.
PARALLEL_UPDATES = 0


class TagoFanHA(TagoEntityHA, FanEntity):
    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    def __init__(self, entity: TagoFan):
        super().__init__(entity)

    @property
    def type_to_string(self) -> str:
        return 'ON/OFF Fan'

    @property
    def is_on(self) -> bool:
        return self._entity.is_on

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        await self._entity.turn_on()

    async def async_turn_off(self, **kwargs):
        await self._entity.turn_off()


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    items: list[TagoFanHA] = list()
    gateway: TagoGateway = entry.runtime_data
    for e in gateway.entities:
        if isinstance(e, TagoFan):
            items.append(TagoFanHA(e))

    async_add_entities(items)
