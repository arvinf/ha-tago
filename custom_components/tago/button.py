"""Platform for light integration."""
from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

import logging

_LOGGER = logging.getLogger(__name__)

from .TagoNet import TagoDevice
from .models import TagoPeripheralHA


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    tagonet = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [TagoDeviceIdentifyButton(device) for device in tagonet.devices]
    )

    async_add_entities(
        [TagoDeviceRebootButton(device) for device in tagonet.devices]
    )


class TagoDeviceIdentifyButton(ButtonEntity):
    _attr_translation_key = "identify"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = ButtonDeviceClass.IDENTIFY

    def __init__(
        self,
        device: TagoDevice,
    ) -> None:
        self._device = device
        self._attr_unique_id = f"{self._device.uid}_identify"

    async def async_press(self) -> None:
        await self._device.identify()

    @property
    def icon(self) -> str:
        return "mdi:led-on"

    @property
    def name(self) -> str:
        return f"Identify {self._device._name}"


class TagoDeviceRebootButton(ButtonEntity):
    _attr_translation_key = "reboot"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(
        self,
        device: TagoDevice,
    ) -> None:
        self._device = device
        self._attr_unique_id = f"{self._device.uid}_reboot"

    async def async_press(self) -> None:
        await self._device.reboot()

    @property
    def icon(self) -> str:
        return "mdi:restart"

    @property
    def name(self) -> str:
        return f"Restart {self._device._name}"
