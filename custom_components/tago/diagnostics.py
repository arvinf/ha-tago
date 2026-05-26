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
from .TagoNet import TagoDevice

# Fields to scrub before handing the payload back to the user.
REDACT_KEYS = {CONF_PIN, CONF_AUTHKEY, "api_key", "authkey", "pin"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a diagnostic snapshot of one Tago config entry."""
    # `runtime_data` is only attached after a successful setup; if the
    # entry is in error/setup-retry state it may not exist on the entry
    # object at all. Use getattr to handle both.
    device: TagoDevice | None = getattr(entry, "runtime_data", None)

    payload: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "unique_id": entry.unique_id,
            "title": entry.title,
            "source": entry.source,
            "data": async_redact_data(dict(entry.data), REDACT_KEYS),
        },
        "device": None,
        "entities": [],
    }

    if device is None:
        return payload

    payload["device"] = {
        "serial_num": device.serial_num,
        "model_num": device.model_num,
        "firmware_rev": device.firmware_rev,
        "is_connected": device.is_connected,
        # `_hoststr` may contain a LAN hostname/IP — not strictly sensitive
        # but redact anyway in case the user shares the dump publicly.
        "host": async_redact_data({"host": device._hoststr}, {"host"})["host"],
    }

    payload["entities"] = [
        {
            "id": e.unique_id,
            "type": e.type,
            "name": e.name,
            "location": e.location,
            "tag": getattr(e, "_tag", None),
            "fault": list(getattr(e, "fault", []) or []),
            "is_unused": e.is_unused(),
        }
        for e in device.entities
    ]

    return payload
