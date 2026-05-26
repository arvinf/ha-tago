"""Platform for scene integration.

PROTOCOL_PROPOSALS §P1 — scenes are device-curated entities that, when
activated, run a sequence of firmware-local actions. HA exposes them
through the standard `scene` domain so users can fire them via the UI,
automations, or service calls.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry

from .entity import TagoEntityHA
from .TagoNet import TagoDevice, TagoScene

PARALLEL_UPDATES = 0


class TagoSceneHA(TagoEntityHA, Scene):
    """A Tago scene exposed as a HA scene entity. `Scene.async_activate`
    routes to the wire-level `activate` command on the device."""

    def __init__(self, entity: TagoScene):
        super().__init__(entity)

    async def async_activate(self, **kwargs: Any) -> None:
        await self._entity.activate()


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    items: list[TagoSceneHA] = []
    device: TagoDevice = entry.runtime_data
    for e in device.entities:
        if isinstance(e, TagoScene):
            items.append(TagoSceneHA(e))
    async_add_entities(items)
