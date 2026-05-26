"""Platform for cover integration."""
from __future__ import annotations

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry

from .entity import TagoEntityHA
from .TagoNet import TagoCover, TagoDevice

# Single shared WebSocket; no per-platform serialization needed.
PARALLEL_UPDATES = 0

class TagoCoverHA(TagoEntityHA, CoverEntity):
    # Map TagoCover type → HA CoverDeviceClass. Keep aligned with
    # TagoCover.types (TagoNet.py:1088).
    _DEVICE_CLASS_BY_TYPE = {
        TagoCover.COVER_SHADE: CoverDeviceClass.SHADE,
        TagoCover.COVER_BLIND: CoverDeviceClass.BLIND,
        TagoCover.COVER_CURTAIN: CoverDeviceClass.CURTAIN,
    }

    def __init__(self, entity: TagoCover):
        super().__init__(entity)
        self._attr_device_class = self._DEVICE_CLASS_BY_TYPE.get(
            self._entity.type, CoverDeviceClass.SHADE
        )

    @property
    def current_cover_position(self) -> int:
        return 100 - self._entity.position

    @property
    def _target_cover_position(self) -> int:
        return 100 - self._entity.target

    @property
    def is_closed(self) -> bool:
        return self.current_cover_position == 0

    @property
    def is_closing(self) -> bool:
        return self._target_cover_position < self.current_cover_position

    @property
    def is_opening(self) -> bool:
        return self._target_cover_position > self.current_cover_position

    @property
    def supported_features(self) -> int | None:
        return CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION

    async def async_open_cover(self, **kwargs):
        """Open the cover."""
        await self._entity.move_to(target=0)

    async def async_close_cover(self, **kwargs):
        """Close cover."""
        await self._entity.move_to(target=100)

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        await self._entity.move_to(target=100-kwargs[ATTR_POSITION])

    async def async_stop_cover(self, **kwargs):
        """Stop the cover."""
        await self._entity.stop_move()

async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    items: list[TagoCoverHA] = list()
    device : TagoDevice = entry.runtime_data
    for e in device.entities:
        if isinstance(e, TagoCover):
            items.append(TagoCoverHA(e))

    async_add_entities(items)

