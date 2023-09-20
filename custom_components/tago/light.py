"""Platform for light integration."""
from __future__ import annotations

from .const import DOMAIN

import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    COLOR_MODE_BRIGHTNESS,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)

_LOGGER = logging.getLogger(__name__)

from .TagoNet import TagoLight
from .models import TagoPeripheralHA


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    tagonet = hass.data[DOMAIN][entry.entry_id]
    new_lights = list()

    for device in tagonet.devices:
        for light in device.lights:
            new_lights.append(TagoLightHA(proxy=light))

    if len(new_lights):
        async_add_entities(new_lights)


class TagoLightHA(TagoPeripheralHA, LightEntity):
    def __init__(self, proxy):
        super().__init__(proxy)

        self._attr_supported_color_modes = {COLOR_MODE_BRIGHTNESS}
        self._attr_color_mode = COLOR_MODE_BRIGHTNESS

    @property
    def is_on(self) -> str:
        return self._proxy.is_on and self._proxy._intensity > 0

    @property
    def supported_features(self) -> int | None:
        return LightEntityFeature.TRANSITION if self._proxy.is_dimmable else 0

    @property
    def color_mode(self):
        return ColorMode.BRIGHTNESS if self._proxy.is_dimmable else 0

    @property
    def supported_color_modes(self) -> set[ColorMode] | set[str] | None:
        return (
            {ColorMode.BRIGHTNESS, ColorMode.ONOFF}
            if self._proxy.is_dimmable
            else {ColorMode.ONOFF}
        )

    @property
    def brightness(self) -> int:
        return TagoLight.convert_intensity_from_device(self._proxy._intensity)

    async def async_turn_on(self, **kwargs):
        brightness = TagoLight.convert_intensity_to_device(
            kwargs.pop(ATTR_BRIGHTNESS, 255)
        )
        if ATTR_TRANSITION in kwargs:
            transition_time = kwargs[ATTR_TRANSITION]
        else:
            transition_time = 0.5

        await self._proxy.fade_to(intensity=brightness, time=transition_time)

    async def async_turn_off(self, **kwargs):
        await self._proxy.turn_off()
