"""Platform for light integration."""
from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_FLASH,
    ATTR_RGB_COLOR,
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
from .TagoNet import TagoDevice, TagoKeypad, TagoLight

# Commands and state echoes share a single WebSocket whose writes are
# serialized internally; entity updates can fan out without per-platform
# rate limiting. (Silver tier rule `parallel-updates`.)
PARALLEL_UPDATES = 0

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
        return LightEntityFeature.TRANSITION | LightEntityFeature.FLASH

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
            return 'ON/OFF Light'
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


class TagoKeypadLEDHA(TagoEntityHA, LightEntity):
    """HA light entity for a keypad's onboard LED. RGB is the only
    supported color mode (PROTOCOL_PROPOSALS §P2.5). Flash effect is
    translated to `set_led effect=flash duration=...`.

    `device_info` nests this entity under the parent keypad's
    device-registry entry so HA users see one device card (the keypad)
    containing both the LED control and the key triggers, rather than
    two siblings."""

    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.FLASH

    FLASH_DURATION_SHORT_MS = 4000
    FLASH_DURATION_LONG_MS = 10000

    def __init__(self, entity: TagoKeypadKey):
        super().__init__(entity)

    @property
    def device_info(self):
        # If the LED knows its parent keypad, attach to that device entry
        # rather than letting the default TagoEntityHA.device_info create
        # a sibling entry.
        keypad_id = self._entity.keypad_id
        if keypad_id:
            from homeassistant.helpers.entity import DeviceInfo
            from .const import DOMAIN
            return DeviceInfo(identifiers={(DOMAIN, keypad_id)})
        return super().device_info

    @property
    def is_on(self) -> bool:
        return self._entity.is_on

    @property
    def brightness(self) -> int:
        # Convert wire 0..1000 → HA 0..255.
        return self.convert_value_from_device(
            self._entity.brightness / TagoKeypad.TagoKeypadKey.BRIGHTNESS_MAX
        )

    @property
    def rgb_color(self) -> tuple[int, int, int]:
        return self._entity.rgb

    async def async_turn_on(self, **kwargs) -> None:
        flash = kwargs.get(ATTR_FLASH)
        if flash is not None:
            duration_ms = (
                self.FLASH_DURATION_SHORT_MS if flash == FLASH_SHORT
                else self.FLASH_DURATION_LONG_MS
            )
            await self._entity.flash(duration_ms)
            return

        ha_brightness = kwargs.get(ATTR_BRIGHTNESS)
        wire_brightness: int | None = None
        if ha_brightness is not None:
            # HA 0..255 → wire 0..1000.
            wire_brightness = int(round(
                self.convert_value_to_device(ha_brightness)
                * TagoKeypad.TagoKeypadKey.BRIGHTNESS_MAX
            ))

        rgb = kwargs.get(ATTR_RGB_COLOR)
        await self._entity.set_led(
            is_on=True,
            brightness=wire_brightness,
            rgb=rgb,
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self._entity.turn_off()


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    items: list[LightEntity] = []
    device: TagoDevice = entry.runtime_data
    for e in device.entities:
        if isinstance(e, TagoLight):
            items.append(TagoLightHA(e))
        elif isinstance(e, TagoKeypad):
            for led in e.leds:
                items.append(led)

    async_add_entities(items)
