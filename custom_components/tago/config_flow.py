"""Config flow for Tago integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import zeroconf
from homeassistant.data_entry_flow import FlowResult

from .TagoNet import TagoGateway

from .const import (
    CONF_AUTHKEY,
    CONF_DEVICENAME,
    CONF_HOSTSTR,
    CONF_PIN,
    DOMAIN,
)


_LOGGER = logging.getLogger(__name__)


class TagoConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    # Bumped to 9: credential storage key renamed `authkey` -> `pin`.
    # See custom_components/tago/__init__.py::async_migrate_entry.
    VERSION = 9

    def __init__(self):
        self.errors: dict[str, str] = {}
        self.device_name: str | None = None
        self.hoststr: str | None = None
        self.pin: str = ""
        # Set during reauth flow.
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self.pin = user_input.get(CONF_PIN, "").strip()
            self.hoststr = user_input[CONF_HOSTSTR].strip()
            return await self.async_step_test_connection()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOSTSTR): str,
                    vol.Optional(CONF_PIN): str,
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
            # Gold-tier rule `discovery-update-info`: when a re-discovery
            # lands for an already-configured device, refresh the stored
            # hostname/port so the entry heals automatically if the
            # device moved networks or changed port.
            self._abort_if_unique_id_configured(
                updates={CONF_HOSTSTR: self.hoststr},
            )

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
            self.pin = user_input.get(CONF_PIN, '').strip()
            return await self.async_step_test_connection()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PIN): str,
                }
            ),
            description_placeholders={
                CONF_DEVICENAME: self.device_name
            },
            errors=self.errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow the user to change hostname/PIN on an already-configured
        entry. Triggered from the device's "Reconfigure" menu."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            new_host = user_input[CONF_HOSTSTR].strip()
            new_pin = user_input.get(CONF_PIN, "").strip()
            self.errors = {}
            gateway: TagoGateway | None = None
            try:
                gateway = TagoGateway(new_host, new_pin)
                await gateway.connect(timeout=5.0)
                # Same gateway? The entry's unique_id was set from the first
                # device's serial when the entry was created; verify it's
                # still present so the user can't repoint at a different
                # physical Tago and overwrite the entry by mistake.
                primary = gateway.devices[0] if gateway.devices else None
                primary_serial = primary.serial_num if primary else None
                if entry.unique_id and primary_serial != entry.unique_id:
                    return self.async_abort(reason="wrong_device")
            except PermissionError:
                self.errors["base"] = "invalid_auth"
            except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError):
                self.errors["base"] = "cannot_connect"
            finally:
                if gateway is not None:
                    try:
                        await gateway.disconnect(timeout=3.0)
                    except Exception as err:
                        _LOGGER.debug("Connection cleanup failed: %s", err)

            if not self.errors:
                new_data = {
                    **entry.data,
                    CONF_HOSTSTR: new_host,
                    CONF_PIN: new_pin,
                }
                new_data.pop(CONF_AUTHKEY, None)
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOSTSTR,
                                 default=entry.data.get(CONF_HOSTSTR, "")): str,
                    vol.Optional(CONF_PIN,
                                 default=entry.data.get(CONF_PIN, "")): str,
                }
            ),
            errors=self.errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Start a reauth flow: stored credentials stopped working
        (typically because the device's firmware was upgraded to the
        PIN-based auth model and the saved api_key no longer authenticates)."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="unknown")
        self.hoststr = entry_data.get(CONF_HOSTSTR) or self._reauth_entry.data.get(CONF_HOSTSTR)
        self.device_name = self._reauth_entry.unique_id or "Tago Device"
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Prompt the user for the device PIN and re-test the connection."""
        if user_input is not None:
            self.pin = user_input.get(CONF_PIN, "").strip()
            self.errors = {}
            gateway: TagoGateway | None = None
            try:
                gateway = TagoGateway(self.hoststr, self.pin)
                await gateway.connect(timeout=5.0)
            except PermissionError:
                self.errors["base"] = "invalid_auth"
            except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError):
                self.errors["base"] = "cannot_connect"
            finally:
                if gateway is not None:
                    try:
                        await gateway.disconnect(timeout=3.0)
                    except Exception as err:
                        _LOGGER.debug("Connection cleanup failed: %s", err)

            if not self.errors and self._reauth_entry is not None:
                new_data = {**self._reauth_entry.data, CONF_PIN: self.pin}
                # Strip the legacy key if it's still hanging around.
                new_data.pop(CONF_AUTHKEY, None)
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data=new_data
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PIN): str}),
            description_placeholders={CONF_DEVICENAME: self.device_name or "Tago Device"},
            errors=self.errors,
        )

    async def async_step_test_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Test the connection to the gateway."""
        self.errors = {}
        gateway: TagoGateway | None = None
        primary_serial: str | None = None
        primary_model: str | None = None
        primary_id: str | None = None

        try:
            gateway = TagoGateway(self.hoststr, self.pin)
            await gateway.connect(timeout=5.0)
            primary = gateway.devices[0] if gateway.devices else None
            if primary is not None:
                primary_serial = primary.serial_num
                primary_model = primary.model_num
                primary_id = primary.unique_id

        except PermissionError as e:
            _LOGGER.debug("Authentication failed: %s", str(e))
            self.errors["base"] = "invalid_auth"

            if "zeroconf" in self.context.get("source", ""):
                return await self.async_step_zeroconf_confirm()
            return await self.async_step_user()
        except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError) as e:
            _LOGGER.debug("Connection failed: %s", str(e))
            self.errors["base"] = "cannot_connect"

            if "zeroconf" in self.context.get("source", ""):
                return await self.async_step_zeroconf_confirm()
            return await self.async_step_user()
        finally:
            if gateway is not None:
                try:
                    await gateway.disconnect(timeout=3.0)
                except Exception as err:
                    _LOGGER.debug("Connection cleanup failed: %s", err)

        # Use the first device's serial as the entry's unique_id so the
        # value is stable across HA reloads (matches the pre-gateway
        # semantics when there was only one device). Fall back to the
        # hoststr if no devices reported back.
        unique_id = primary_serial or self.hoststr
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        title = (
            f'{primary_model} {primary_serial}'
            if primary_model and primary_serial
            else f'TAGO Gateway @ {self.hoststr}'
        )
        _LOGGER.debug("Successfully connected to Tago gateway %s", self.hoststr)
        return self.async_create_entry(
            title=title,
            data={
                CONF_PIN: self.pin,
                CONF_DEVICENAME: primary_id or self.hoststr,
                CONF_HOSTSTR: self.hoststr,
            },
        )
