from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

from .TagoNet import TagoDevice
from . import generate_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities([
        OfflineSensor(device, hass)
    ])


class OfflineSensor(BinarySensorEntity):
    """A binary sensor to indicate if the device is offline."""

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
