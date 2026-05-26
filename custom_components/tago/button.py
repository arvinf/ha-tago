from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .TagoNet import TagoDevice
from . import generate_device_info

PARALLEL_UPDATES = 0

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

    # Configuration action category — surfaces under the device page's
    # "Configuration" section rather than mixing with regular controls.
    _attr_entity_category = EntityCategory.CONFIG
    # Reboot is destructive (drops the WebSocket, resets state). Default
    # disabled so it only appears for users who explicitly want it.
    _attr_entity_registry_enabled_default = False

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

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device: TagoDevice, hass: HomeAssistant):
        self._device = device
        self._hass = hass
        self._attr_unique_id = f"{device.unique_id}:identify"
        self._attr_name = "Identify Device"
        self._attr_device_info = generate_device_info(device)

    async def async_press(self):
        await self._device.identify()
        
