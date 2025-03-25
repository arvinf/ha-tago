from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

from .TagoNet import TagoDevice
from . import generate_device_info

def generate_device_info(device: TagoDevice) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, device.unique_id)},
        name=device.name,
        manufacturer=device.manufacturer,
        model=device.model_num,
        configuration_url=device.dashboard_uri,
    )

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities([
        RebootButton(device, hass),
        IdentifyButton(device, hass)
    ])


class RebootButton(ButtonEntity):
    """A button to reboot the device."""

    def __init__(self, device: TagoDevice, hass: HomeAssistant):
        self._device = device
        self._hass = hass
        self._attr_unique_id = f"{device.unique_id}:reboot"
        self._attr_name = "Reboot Device"
        self._attr_device_info = generate_device_info(device)

    async def async_press(self):
        await self._device.reboot()


class IdentifyButton(ButtonEntity):
    """A button to identify the device."""

    def __init__(self, device: TagoDevice, hass: HomeAssistant):
        self._device = device
        self._hass = hass
        self._attr_unique_id = f"{device.unique_id}:identify"
        self._attr_name = "Identify Device"
        self._attr_device_info = generate_device_info(device)

    async def async_press(self):
        await self._device.identify()
        
