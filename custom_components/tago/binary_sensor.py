from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN

from .entity import TagoEntityHA
from .TagoNet import TagoDevice, TagoSensor, TagoVirtualSensor
from . import generate_device_info

PARALLEL_UPDATES = 0


# PROTOCOL_PROPOSALS §P4.2: map each known real-sensor wire type to the
# HA `BinarySensorDeviceClass` that matches. Unknown `sensor_*` types
# fall through to `None` so users see a generic binary sensor.
_SENSOR_DEVICE_CLASS_BY_TYPE: dict[str, BinarySensorDeviceClass] = {
    TagoSensor.SENSOR_LIGHT: BinarySensorDeviceClass.LIGHT,
    TagoSensor.SENSOR_MOTION: BinarySensorDeviceClass.MOTION,
    TagoSensor.SENSOR_OCCUPANCY: BinarySensorDeviceClass.OCCUPANCY,
    TagoSensor.SENSOR_OPENING: BinarySensorDeviceClass.OPENING,
    TagoSensor.SENSOR_PRESENCE: BinarySensorDeviceClass.PRESENCE,
    TagoSensor.SENSOR_DOOR: BinarySensorDeviceClass.DOOR,
    TagoSensor.SENSOR_WINDOW: BinarySensorDeviceClass.WINDOW,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device: TagoDevice = config_entry.runtime_data
    items: list[BinarySensorEntity] = [
        OfflineSensor(device, hass),
        FirmwareUpdateAvailableSensor(device),
    ]
    for e in device.entities:
        if isinstance(e, TagoVirtualSensor):
            items.append(TagoVirtualSensorHA(e))
        elif isinstance(e, TagoSensor):
            items.append(TagoSensorHA(e))
    async_add_entities(items)


class OfflineSensor(BinarySensorEntity):
    """A binary sensor to indicate if the device is offline."""

    # Diagnostic category — surfaces device health under the diagnostic
    # section of the device page rather than mixing with the main controls.
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: TagoDevice, hass: HomeAssistant):
        self._device = device
        self._hass = hass
        self._attr_unique_id = f"{device.unique_id}:connstate"
        self._attr_name = "Device Status"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_is_on = self._device.is_connected
        self._attr_device_info = generate_device_info(device)
        self._device.set_on_state_changed(self.on_state_updated)

    @property
    def is_on(self) -> bool:
        return self._device.is_connected

    def on_state_updated(self):
        self._attr_is_on = self._device.is_connected
        self.async_write_ha_state()


class FirmwareUpdateAvailableSensor(BinarySensorEntity):
    """Indicates whether the device firmware has a newer release available.

    Per PROTOCOL_PROPOSALS §P5 the device reports `latest_firmware_rev`
    alongside `firmware_rev` on its `get_config` response (and again via
    `config_changed`). HA exposes this as a diagnostic-category binary
    sensor with device class `UPDATE` on the gateway device card.

    The actual firmware update is not performed through HA — the user
    triggers it via the device's web UI or native OTA mechanism, and
    the sensor flips off the next time the device reports a fresh
    `firmware_rev`."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.UPDATE

    def __init__(self, device: TagoDevice) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}:firmware_update"
        self._attr_name = "Firmware Update Available"
        self._attr_device_info = generate_device_info(device)
        self._device.set_on_state_changed(self.on_state_updated)

    @property
    def is_on(self) -> bool:
        return self._device.firmware_update_available

    @property
    def available(self) -> bool:
        return self._device.is_connected

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "current_version": self._device.firmware_rev,
            "latest_version": self._device.latest_firmware_rev,
        }

    def on_state_updated(self) -> None:
        self.async_write_ha_state()


class TagoVirtualSensorHA(TagoEntityHA, BinarySensorEntity):
    """HA binary_sensor for a TagoVirtualSensor — a read-only on/off
    flag set by the firmware automation engine.

    Per PROTOCOL_PROPOSALS §P3.5 these are DIAGNOSTIC-grade entities
    and disabled-by-default."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, entity: TagoVirtualSensor):
        super().__init__(entity)

    @property
    def is_on(self) -> bool:
        return self._entity.is_on


class TagoSensorHA(TagoEntityHA, BinarySensorEntity):
    """HA binary_sensor for a physical Tago sensor (motion, contact, etc.).

    PROTOCOL_PROPOSALS §P4. Unlike virtual sensors these are first-class
    controls — enabled by default with the appropriate `BinarySensorDeviceClass`
    inferred from the wire type. Unknown `sensor_*` types come through
    with `device_class = None` so they still surface to the user."""

    def __init__(self, entity: TagoSensor):
        super().__init__(entity)
        self._attr_device_class = _SENSOR_DEVICE_CLASS_BY_TYPE.get(entity.type)

    @property
    def is_on(self) -> bool:
        return self._entity.is_on
