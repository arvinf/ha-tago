"""TAGO hosts integration."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_AUTHKEY, CONF_HOSTSTR, DOMAIN
from .TagoNet import TagoDevice, TagoEntity

PLATFORMS: list[str] = [Platform.LIGHT, Platform.FAN,
                        Platform.SWITCH, Platform.COVER, Platform.BUTTON, Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


def generate_device_info(device: TagoDevice) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, device.unique_id)},
        name=device.name,
        manufacturer=device.manufacturer,
        model=device.model_num,
        sw_version=device.firmware_rev,
        serial_number=device.unique_id,
        configuration_url=device.dashboard_uri,
    )

task = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:    
    hass.data.setdefault(DOMAIN, {})
    hoststr = entry.data.get(CONF_HOSTSTR) or ''
    authkey = entry.data.get(CONF_AUTHKEY) or ''

    device_registry = async_get_device_registry(hass)

    entry_data = hass.data[DOMAIN].setdefault(entry.entry_id, {})

    device = TagoDevice(hoststr, authkey)
    await device.connect()

    entry.runtime_data = device
    for e in device.entities:
        if e.is_unused():
            try:
                device_entry = device_registry.async_get_device(
                   identifiers={(DOMAIN, e.unique_id)}
                )
                device_registry.async_remove_device( device_entry.id)
                _LOGGER.debug(f"Removed unused device '{e.unique_id}'")
            except:
                pass

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    device : TagoDevice = entry.runtime_data
    await device.disconnect()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.debug("Unloaded entry for %s", entry.entry_id)

    return unload_ok
