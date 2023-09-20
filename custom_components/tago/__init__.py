"""TAGO hosts integration."""
from __future__ import annotations
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr

from homeassistant.const import (
    ATTR_ID,
    ATTR_DEVICE_ID,
    ATTR_SUGGESTED_AREA,
    CONF_HOST,
    Platform,
)
import voluptuous as vol

from typing import Any, Optional, cast, Dict

from .const import DOMAIN, CONF_NET_KEY, CONF_HOSTS
from .TagoNet import TagoNet, TagoBridge, TagoModbus
import asyncio

PLATFORMS: list[str] = [Platform.LIGHT, Platform.BUTTON]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                {
                    vol.Required(CONF_NET_KEY): cv.string,
                }
            ],
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, base_config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    tagonet = None

    try:
        tagonet = TagoNet(network_key=entry.data.get(CONF_NET_KEY, ""))
    except ValueError:
        _LOGGER.error("Invalid network key provided")
        return False

    ## Save tagonet instance, to be used by platforms
    hass.data[DOMAIN][entry.entry_id] = tagonet

    ## connect to all provided hosts
    for host in entry.data.get(CONF_HOSTS):
        tagonet.add_host(host)

    ## wait a few seconds, for all connected hosts to be enumerated
    try:
        async with asyncio.timeout(10):
            while not tagonet.enumeration_complete:
                await asyncio.sleep(1)
            _LOGGER.info("All devices enumerated.")

            def bridge_events(
                entity: TagoBridge, entity_id: str, message: Dict[str, str]
            ) -> None:
                data = {
                    ATTR_ID: entity_id,
                    "action": message.get("type"),
                    "keypad": "0x{:2x}".format(message.get("address")),
                    "key": message.get("key"),
                    "duration": "long"
                    if message.get("duration") > 1
                    else "short",
                }
                hass.bus.fire("tago_event", data)

            device_registry = dr.async_get(hass)

            ## Set handler to catch messages from bridges, such as modbus keypads
            for device in tagonet.devices:
                for bridge in device.bridges:
                    bridge.set_message_handler(bridge_events)

                ## register device
                device_registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    configuration_url=f"http://{device.host}/",
                    identifiers={(DOMAIN, device.uid)},
                    manufacturer=device.manufacturer,
                    name=device.name,
                    model=device.model_desc,
                    sw_version=device.fw_rev,
                    hw_version=device.model_name,
                )

            await hass.config_entries.async_forward_entry_setups(
                entry, PLATFORMS
            )
    except TimeoutError:
        _LOGGER.error("Timedout waiting to connect to all TagoNet devices.")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    tagonet = hass.data[DOMAIN][entry.entry_id]
    await tagonet.close()
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
