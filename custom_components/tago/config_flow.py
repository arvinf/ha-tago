"""Config flow for Tago integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components import zeroconf

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import (
    AsyncServiceBrowser,
    AsyncServiceInfo,
    AsyncZeroconf,
    AsyncZeroconfServiceTypes,
)

from .TagoNet import TagoNet
import asyncio

from .const import (
    DOMAIN,
    CONF_NET_KEY,
    CONF_NET_KEY_DEFAULT,
    CONF_HOSTS,
    CONF_NET_ID,
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {vol.Required(CONF_NET_KEY, default=CONF_NET_KEY_DEFAULT): str}
)


class TagoConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.data = {}

    async def zeroconf_discover_hosts(self, network_id: str, wait: int) -> None:
        hosts = list()
        network_id = network_id.replace(":", "").encode()

        def async_on_service_state_change(
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            if state_change is not ServiceStateChange.Added:
                return
            self.hass.async_create_task(
                self.async_display_service_info(
                    zeroconf, service_type, name, hosts, network_id
                )
            )

        ## discover other servers over zeroconf
        aiozc = await zeroconf.async_get_async_instance(self.hass)
        services = ["_tagonet._tcp.local."]

        aiobrowser = AsyncServiceBrowser(
            aiozc.zeroconf, services, handlers=[async_on_service_state_change]
        )
        ## after 10 seconds, stop scanning
        await asyncio.sleep(wait)
        await aiobrowser.async_cancel()

        return hosts

    async def async_display_service_info(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        hosts: list,
        network_id: str,
    ) -> None:
        info = AsyncServiceInfo(service_type, name)
        await info.async_request(zeroconf, 3000)
        if info:
            nwid = info.properties.get(b"nwid", None)
            _LOGGER.info(
                f"Found {name} {info.server} network: {nwid} {network_id}"
            )

            ## add device to the list if the network id matches
            if nwid == network_id and not info.server in hosts:
                if info.server.endswith('.'):
                    info.server = info.server[:-1]
                hosts.append(info.server)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors = {}

        self._async_abort_entries_match(
            {CONF_NET_KEY: self.data.get(CONF_NET_KEY)}
        )

        if user_input is not None:
            self.data[CONF_NET_KEY] = user_input[CONF_NET_KEY]

            netwk_id = ""
            try:
                ## validate network key
                netwk_id = TagoNet.generate_network_id(
                    self.data.get(CONF_NET_KEY, "").strip()
                )

                await self.async_set_unique_id(user_input[CONF_NET_KEY])
                self._abort_if_unique_id_configured(
                    {CONF_NET_KEY: user_input[CONF_NET_KEY]}
                )

                hosts = list()
                hosts.extend(await self.zeroconf_discover_hosts(netwk_id, 5))
                self.data[CONF_HOSTS] = hosts
                return self.async_create_entry(
                    title=f"Network {netwk_id}", data=self.data
                )
            except Exception:
                errors["base"] = "bad_network_key"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )
