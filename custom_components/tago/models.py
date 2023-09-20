"""HA equivalent models for various TAGO entities"""
from __future__ import annotations

from .TagoNet import TagoPeripheral
from .const import DOMAIN


class TagoPeripheralHA(TagoPeripheral):
    def __init__(self, proxy):
        self._proxy = proxy
        self._proxy.set_update_handle(self.on_updated)

    def on_updated(self):
        self.schedule_update_ha_state()

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def has_entity_name(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return self._proxy._name

    @property
    def unique_id(self) -> str:
        return self._proxy._uid

    @property
    def available(self) -> bool:
        return self._proxy._device.is_connected
