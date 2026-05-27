"""Diagnostics support for the Tago integration.

Returned by HA when a user clicks "Download Diagnostics" on the device
page. Credentials are redacted before serialisation. Gold-tier rule
`diagnostics`.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_AUTHKEY, CONF_PIN
from .TagoNet import TagoGateway

# Fields to scrub before handing the payload back to the user.
REDACT_KEYS = {CONF_PIN, CONF_AUTHKEY, "api_key", "authkey", "pin"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a diagnostic snapshot of one Tago config entry."""
    # `runtime_data` is only attached after a successful setup; if the
    # entry is in error/setup-retry state it may not exist on the entry
    # object at all. Use getattr to handle both.
    gateway: TagoGateway | None = getattr(entry, "runtime_data", None)

    payload: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "unique_id": entry.unique_id,
            "title": entry.title,
            "source": entry.source,
            "data": async_redact_data(dict(entry.data), REDACT_KEYS),
        },
        "gateway": None,
        "devices": [],
    }

    if gateway is None:
        return payload

    payload["gateway"] = {
        "is_connected": gateway.is_connected,
        # `hoststr` is the LAN hostname/IP — not strictly sensitive but
        # redact anyway in case the user shares the dump publicly.
        "host": async_redact_data({"host": gateway.hoststr}, {"host"})["host"],
        "device_count": len(gateway.devices),
    }

    payload["devices"] = [
        {
            "id": device.unique_id,
            "serial_num": device.serial_num,
            "model_num": device.model_num,
            "firmware_rev": device.firmware_rev,
            "latest_firmware_rev": device.latest_firmware_rev,
            "location": device.location,
            "available": device.available,
            "is_connected": device.is_connected,
            "entities": [
                {
                    "id": e.unique_id,
                    "type": e.type,
                    "name": e.name,
                    "location": e.location,
                    "tag": e.tag,
                    "fault": list(getattr(e, "fault", []) or []),
                    "is_unused": e.is_unused(),
                }
                for e in device.entities
            ],
        }
        for device in gateway.devices
    ]

    return payload
