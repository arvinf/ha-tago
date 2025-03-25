"""Platform for light integration."""
from __future__ import annotations

import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_FLASH,
    ATTR_TRANSITION,
    ATTR_WHITE,
    ATTR_XY_COLOR,
    FLASH_SHORT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry

from .const import ATTR_RATE
from .entity import TagoEntityHA
from .TagoNet import TagoDevice, TagoLight

_LOGGER = logging.getLogger(__name__)


class TagoLightHA(TagoEntityHA, LightEntity):
    _attr_supported_color_modes = [ColorMode.XY, ColorMode.COLOR_TEMP]
    _attr_supported_features = LightEntityFeature.TRANSITION | LightEntityFeature.FLASH

    def __init__(self, entity: TagoLight):        
        super().__init__(entity)
        self._last_brightness = 255

    @property
    def is_dimmable(self):
        return self._entity.type != TagoLight.LIGHT_ONOFF

    @property
    def is_on(self):
        return self._entity.brightness > 0

    @property
    def supported_features(self) -> int | None:
        return LightEntityFeature.TRANSITION

    @property
    def supported_color_modes(self) -> set[ColorMode] | set[str] | None:
        if self._entity.type == TagoLight.LIGHT_ONOFF:
            return [ColorMode.ONOFF]
        if self._entity.type == TagoLight.LIGHT_MONO or self._entity.type == TagoLight.LIGHT_DIMMABLE:
            return [ColorMode.BRIGHTNESS]
        if self._entity.type == TagoLight.LIGHT_CCT:
            return [ColorMode.COLOR_TEMP]
        if self._entity.type == TagoLight.LIGHT_RGB:
            return [ColorMode.XY]
        if self._entity.type == TagoLight.LIGHT_RGBW:
            return [ColorMode.XY, ColorMode.WHITE]
        if self._entity.type == TagoLight.LIGHT_RGB_CCT:
            return [ColorMode.XY, ColorMode.COLOR_TEMP]
        return [ColorMode.ONOFF]

    @property
    def type_to_string(self) -> int:
        if self._entity.type == TagoLight.LIGHT_ONOFF:
            return 'ON/OFF Relay'
        if self._entity.type == TagoLight.LIGHT_DIMMABLE:
            return 'Dimmer'
        if self._entity.type == TagoLight.LIGHT_MONO:
            return 'Single Colour LED Driver'
        if self._entity.type == TagoLight.LIGHT_CCT:
            return 'Tunable White LED Driver'
        if self._entity.type == TagoLight.LIGHT_RGB:
            return 'RGB LED Driver'
        if self._entity.type == TagoLight.LIGHT_RGBW:
            return 'RGB+W LED Driver'
        if self._entity.type == TagoLight.LIGHT_RGB_CCT:
            return 'RGB+Tunable White LED Driver'
        return ''

    @property
    def brightness(self) -> int:
        return self.convert_value_from_device(self._entity.brightness)

    @property
    def color_mode(self):
        if self._entity.type == TagoLight.LIGHT_ONOFF:
            return ColorMode.ONOFF
        if self._entity.type == TagoLight.LIGHT_MONO or self._entity.type == TagoLight.LIGHT_DIMMABLE:
            return ColorMode.BRIGHTNESS
        if self._entity.type == TagoLight.LIGHT_RGB:
            return ColorMode.XY
        if self._entity.type == TagoLight.LIGHT_RGBW:
            if self.xy_color[0] == 0 and self.xy_color[1] == 0:
                return ColorMode.WHITE
            return ColorMode.XY
        if self._entity.type == TagoLight.LIGHT_RGB_CCT:
            if self.xy_color[0] == 0 and self.xy_color[1] == 0:
                return ColorMode.COLOR_TEMP
            return ColorMode.XY
        if self._entity.type == TagoLight.LIGHT_CCT:
            return ColorMode.COLOR_TEMP
        return ColorMode.ONOFF

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._entity.type in [TagoLight.LIGHT_RGB_CCT, TagoLight.LIGHT_CCT]:
            return int(self._entity.ct * (self._entity.colour_temp_range[1] - self._entity.colour_temp_range[0])) + self._entity.colour_temp_range[0]

        return None

    @property
    def min_color_temp_kelvin(self) -> int | None:
        if self._entity.type in [TagoLight.LIGHT_RGB_CCT, TagoLight.LIGHT_CCT]:
            return self._entity.colour_temp_range[0]

        return None

    @property
    def max_color_temp_kelvin(self) -> int | None:
        if self._entity.type in [TagoLight.LIGHT_RGB_CCT, TagoLight.LIGHT_CCT]:
            return self._entity.colour_temp_range[1]

        return None

    @property
    def xy_color(self) -> tuple[float, float] | None:
        if self._entity.type in [TagoLight.LIGHT_RGB, TagoLight.LIGHT_RGB_CCT, TagoLight.LIGHT_RGBW]:
            return self._entity.colour_xy
        return None

    async def async_turn_on(self, **kwargs):        
        rate: float = kwargs.pop(ATTR_RATE, None)
        transition_time: float = kwargs.pop(ATTR_TRANSITION, None)
        brightness: float = kwargs.pop(ATTR_BRIGHTNESS, None)
        xy_color: tuple[float, float] | None = kwargs.pop(ATTR_XY_COLOR, None)
        white = kwargs.get(ATTR_WHITE)
        color_temp: int | None = kwargs.pop(ATTR_COLOR_TEMP_KELVIN, None)
        flash = kwargs.get(ATTR_FLASH)
        
        ## if all parametes are 'None' then this is a turn on to last brightness
        if brightness is None and xy_color is None and white is None and color_temp is None:
            brightness = self._last_brightness or 255

        if flash is not None:
            await self._entity.set_light_flash(4 if flash == FLASH_SHORT else 10)
            return

        if white is not None:
            brightness = white

        if brightness is not None:
            brightness = self.convert_value_to_device(brightness)

        if color_temp is not None:
            # convert absolute colour temp to a ratio-metric value
            color_temp = (color_temp - self.min_color_temp_kelvin) / \
                (self.max_color_temp_kelvin - self.min_color_temp_kelvin)
            await self._entity.set_ct(ct=color_temp, brightness=brightness, duration=transition_time, rate=rate)
            return

        if xy_color is not None:
            # colour doesn't have a "rate" only a duration
            await self._entity.set_colour(colour=xy_color, brightness=brightness, duration=transition_time)
            return

        if brightness is not None:
            await self._entity.set_brightness(brightness=brightness, duration=transition_time, rate=rate)
            return

    async def async_turn_off(self, **kwargs):
        rate: float = kwargs.pop(ATTR_RATE, None)
        transition_time: float = kwargs.pop(ATTR_TRANSITION, None)
        ## store current brightness level, to restore it in event of a turn on without any parameters
        if self.brightness > 0:
            self._last_brightness = self.brightness
        await self._entity.set_brightness(brightness=0, duration=transition_time, rate=rate)

    async def async_stop_transition(self):
        await self._entity.stop_ramp()


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    lights: list[TagoLightHA] = list()
    device: TagoDevice = entry.runtime_data
    for e in device.entities:
        if type(e) == TagoLight:
            lights.append(TagoLightHA(e))

    async_add_entities(lights)
