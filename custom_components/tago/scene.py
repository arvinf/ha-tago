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
from .TagoNet import TagoGateway, TagoScene, TagoMessage

PARALLEL_UPDATES = 0


class TagoSceneHA(TagoEntityHA, Scene):
    """A Tago scene exposed as a HA scene entity. `Scene.async_activate`
    routes to the wire-level `activate` command on the device.

    Scenes can also be fired outside of HA — by a physical keypad, an
    automation running on the device's own engine, or a third-party
    integration talking to the same device. The firmware reports those
    via the `scene_activated` event, and `_on_scene_activated` uses
    HA's `_async_record_activation` hook to advance the scene's state
    without re-entering `async_activate` (which would loop the wire
    command back to the device)."""

    def __init__(self, entity: TagoScene):
        super().__init__(entity)
        entity.set_on_scene_activated(self._on_scene_activated)

    async def async_activate(self, **kwargs: Any) -> None:
        await self._entity.activate()

    async def _on_scene_activated(self, msg: TagoMessage) -> None:
        self._async_record_activation()
        self.async_write_ha_state()


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    items: list[TagoSceneHA] = []
    gateway: TagoGateway = entry.runtime_data
    for e in gateway.entities:
        if isinstance(e, TagoScene):
            items.append(TagoSceneHA(e))
    async_add_entities(items)
