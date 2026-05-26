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
from .TagoNet import TagoDevice, TagoEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device: TagoDevice = config_entry.runtime_data
    items: list[SensorEntity] = []
    for e in device.entities:
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
        base = self._entity.name or self._entity.unique_id
        return f"{base} Signal Strength"

    @property
    def native_value(self) -> int | None:
        return self._entity.rsi

    @property
    def device_info(self) -> DeviceInfo | None:
        # For a keypad LED the parent device-registry card is the
        # keypad's, not the LED's. Mirror that nesting so the signal
        # strength sensor sits next to the LED light entity on the
        # keypad card rather than spawning a sibling entry.
        keypad_id = getattr(self._entity, "keypad_id", None)
        if keypad_id:
            return DeviceInfo(identifiers={(DOMAIN, keypad_id)})
        return super().device_info
