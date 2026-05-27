from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import TagoEntityHA
from .TagoNet import TagoDevice, TagoGateway, TagoSensor, TagoVirtualSensor
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
    gateway: TagoGateway = config_entry.runtime_data
    items: list[BinarySensorEntity] = []
    # One offline + one firmware-update sensor per TagoDevice — both
    # track per-device state, not gateway-wide.
    for device in gateway.devices:
        items.append(OfflineSensor(device))
        items.append(FirmwareUpdateAvailableSensor(device))
    for e in gateway.entities:
        if isinstance(e, TagoVirtualSensor):
            items.append(TagoVirtualSensorHA(e))
        elif isinstance(e, TagoSensor):
            items.append(TagoSensorHA(e))
    async_add_entities(items)


class _DeviceLevelBinarySensor(BinarySensorEntity):
    """Base for diagnostic binary sensors that ride on a TagoDevice's
    state-change callbacks. Callback subscription is wired in
    `async_added_to_hass` (when `self.hass` is guaranteed set) and
    torn down in `async_will_remove_from_hass`."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: TagoDevice):
        self._device = device
        # See `button._DeviceLevelButton.__init__` for the caching
        # rationale — same reasoning applies here.
        self._attr_device_info = generate_device_info(device)

    async def async_added_to_hass(self) -> None:
        # Deferred registration: `self.hass` is guaranteed bound here.
        # The window between construction and this method is too
        # narrow (and runs after the gateway's first connect) to lose
        # a state event in practice.
        self._device.set_on_state_changed(self._on_device_state_changed)

    async def async_will_remove_from_hass(self) -> None:
        self._device.remove_on_state_changed(self._on_device_state_changed)

    def _on_device_state_changed(self) -> None:
        self.async_write_ha_state()


class OfflineSensor(_DeviceLevelBinarySensor):
    """A binary sensor to indicate if the device is offline."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, device: TagoDevice):
        super().__init__(device)
        self._attr_unique_id = f"{device.unique_id}:connstate"
        self._attr_name = "Device Status"

    @property
    def is_on(self) -> bool:
        return self._device.is_connected


class FirmwareUpdateAvailableSensor(_DeviceLevelBinarySensor):
    """Indicates whether the device firmware has a newer release available.

    Per PROTOCOL_PROPOSALS §P5.3 the device emits a
    `firmware_update_available` event carrying the new revision when
    it discovers an update. HA exposes this as a diagnostic-category
    binary sensor with device class `UPDATE` on the device card.

    The actual update is not performed through HA — the user triggers
    it via the device's web UI or native OTA mechanism. The sensor
    starts OFF on each connect; the event flips it ON. Reload (or
    reconnect after the OTA reboot) clears it."""

    _attr_device_class = BinarySensorDeviceClass.UPDATE

    def __init__(self, device: TagoDevice) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.unique_id}:firmware_update"
        self._attr_name = "Firmware Update Available"

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
