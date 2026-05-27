"""Platform for sensor integration.

Currently exposes a single sensor type: per-entity signal strength
(PROTOCOL_PROPOSALS §P6). Created for every TagoEntity whose discovery
payload (or subsequent state events) carries an `rsi` value — wired
entities don't get one.
"""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import TagoEntityHA
from .TagoNet import TagoEntity, TagoGateway

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    gateway: TagoGateway = config_entry.runtime_data
    items: list[SensorEntity] = []
    for e in gateway.entities:
        # PROTOCOL_PROPOSALS §P6: only entities with a reported `rsi`
        # get a signal-strength companion sensor. An entity that
        # reports rsi=None has no wireless link to measure.
        if e.is_unused():
            continue
        if e.rsi is not None:
            items.append(SignalStrengthSensor(e))
    async_add_entities(items)


class SignalStrengthSensor(TagoEntityHA, SensorEntity):
    """Per-entity RSSI sensor (PROTOCOL_PROPOSALS §P6).

    Attaches to the parent entity's device-registry entry so it shows
    up under the same device card. Diagnostic + disabled-by-default
    — most users won't care about RSSI day-to-day, but the data is
    there when diagnosing dropouts."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._entity.unique_id}:rsi"

    @property
    def name(self) -> str:
        # `has_entity_name=True` (inherited from TagoEntityHA) makes HA
        # compose `friendly_name = <device_card_name> + " " + name`. The
        # device card already carries the parent entity's name, so we
        # return only the suffix here — returning `"<parent> Signal
        # Strength"` would double the parent label on the entity row.
        return "Signal Strength"

    @property
    def native_value(self) -> int | None:
        return self._entity.rsi

    @property
    def device_info(self) -> DeviceInfo | None:
        # For keypads the integration registers the device card up
        # front (see __init__._async_register_keypads_and_dispatch_events);
        # this sensor's `identifiers` already match that card, so HA
        # merges them onto a single entry rather than creating a sibling.
        return super().device_info
