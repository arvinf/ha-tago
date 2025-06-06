"""Config flow for Tago integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import zeroconf
from homeassistant.data_entry_flow import FlowResult

from .TagoNet import TagoDevice

from .const import (
    CONF_AUTHKEY,
    CONF_DEVICENAME,
    CONF_HOSTSTR,
    DOMAIN,
)


class TagoConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 8

    def __init__(self):
        self.data = {}
        self.link_task: asyncio.Task | None = None
        self.errors = {}
        self.device_name = None  # Ensure device_name is initialized
        self.hoststr = None  # Store URI for connection testing

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self.authkey = user_input[CONF_AUTHKEY].strip()
            self.hoststr  = user_input[CONF_HOSTSTR].strip()

            return await self.async_step_test_connection()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOSTSTR): str,
                    vol.Optional(CONF_AUTHKEY): str,
                }
            ),
            errors=self.errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> FlowResult:
        if discovery_info:
            self.hoststr = f"{discovery_info.hostname.removesuffix('.').strip()}:{discovery_info.port}"
            self.device_name = discovery_info.properties.get(
                'serialnum', 'UNKNOWN')

            await self.async_set_unique_id(self.device_name)
            self._abort_if_unique_id_configured()

            self.context.update(
                {
                    "title_placeholders": {
                        "device_name": f'Device {self.device_name}'
                    }
                }
            )

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm zeroconf configuration."""
        if user_input is not None:
            self.authkey = user_input.get(CONF_AUTHKEY, '').strip()
            return await self.async_step_test_connection()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_AUTHKEY): str,
                }
            ),
            description_placeholders={
                CONF_DEVICENAME: self.device_name
            },
            errors=self.errors,
        )

    async def async_step_test_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Test the connection to the device."""
        self.errors = {}  # Reset errors

        try:
            device = TagoDevice(self.hoststr, self.authkey)
            await device.connect(timeout=5.0)
            await device.disconnect(timeout=3.0)
            device_id = device.unique_id
            # Proceed to the final step

        except (ConnectionError, asyncio.TimeoutError) as e:
            logging.debug(f"Connection failed: {str(e)}")
            self.errors["base"] = "cannot_connect"

            # Return to the previous step with an error
            if "zeroconf" in self.context.get("source", ""):
                return await self.async_step_zeroconf_confirm()
            return await self.async_step_user()

        except PermissionError as e:
            logging.exception(e)
            logging.debug(f"Authentication failed: {str(e)}")
            self.errors["base"] = "invalid_auth"

            # Return to the previous step with an error
            if "zeroconf" in self.context.get("source", ""):
                return await self.async_step_zeroconf_confirm()
            return await self.async_step_user()

        """Finalize the configuration after a successful connection."""
        await self.async_set_unique_id(device.serial_num)
        self._abort_if_unique_id_configured()

        logging.debug(
            f"Successfully connected to Tago device {device.serial_num}"
        )
        return self.async_create_entry(
            title=f'{device.model_num} {device.serial_num}',
            data={
                CONF_AUTHKEY: self.authkey,
                CONF_DEVICENAME: device_id,
                CONF_HOSTSTR: self.hoststr},
        )
