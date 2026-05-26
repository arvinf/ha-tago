"""TAGO hosts integration."""
from __future__ import annotations

import asyncio
import logging

import time
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_AUTHKEY,
    CONF_HOSTSTR,
    CONF_PIN,
    DOMAIN,
    MIN_FIRMWARE_VERSION,
)
from .TagoNet import TagoDevice, TagoEntity, TagoKeypad, TagoScene

PLATFORMS: list[str] = [Platform.LIGHT, Platform.FAN,
                        Platform.SWITCH, Platform.COVER, Platform.BUTTON,
                        Platform.BINARY_SENSOR, Platform.SCENE,
                        Platform.SENSOR]

# Event fired on hass.bus when a key event arrives from a keypad. Used
# by HA automations to trigger on keypad presses. PROTOCOL_PROPOSALS §P2.4.
EVENT_TAGO_KEY = "tago_key_event"

# Event fired on hass.bus when a scene is activated on the device by
# any source (remote `activate`, keypad press, schedule, etc.).
# PROTOCOL_PROPOSALS §P1.4. Distinct from HA's own scene-domain events
# (which fire only on `scene.turn_on` from HA) so automations can react
# to scenes that were triggered by the device itself.
EVENT_TAGO_SCENE = "tago_scene_activated"

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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to the current schema version.

    8 -> 9
      The credential storage key was renamed `authkey` -> `pin` to reflect
      the firmware's move from api_key auth to PIN auth. The stored value
      is carried over verbatim — it'll still be valid against pre-PIN
      firmware. Once the device is upgraded to PIN auth, the next
      `device.connect()` will raise `PermissionError`, async_setup_entry
      will surface that as `ConfigEntryAuthFailed`, and HA will trigger the
      reauth flow (config_flow.async_step_reauth) which prompts the user
      for the actual PIN.
    """
    _LOGGER.debug(
        "Migrating Tago entry %s from version %d", entry.entry_id, entry.version
    )

    if entry.version < 9:
        new_data = dict(entry.data)
        if CONF_AUTHKEY in new_data and CONF_PIN not in new_data:
            new_data[CONF_PIN] = new_data.pop(CONF_AUTHKEY)
        else:
            new_data.pop(CONF_AUTHKEY, None)
            new_data.setdefault(CONF_PIN, "")
        hass.config_entries.async_update_entry(entry, data=new_data, version=9)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hoststr = entry.data.get(CONF_HOSTSTR) or ''
    # CONF_AUTHKEY is the legacy key; if migration hasn't run yet (e.g.
    # restoring an old backup) fall back to it transparently.
    pin = entry.data.get(CONF_PIN) or entry.data.get(CONF_AUTHKEY) or ''

    entry_data = hass.data[DOMAIN].setdefault(entry.entry_id, {})

    device = TagoDevice(hoststr, pin)

    try:
        await device.connect(timeout=10.0)
    except PermissionError as err:
        # Credentials no longer work. Surface to HA as auth-failed so it
        # triggers the reauth flow (config_flow.async_step_reauth).
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
            translation_placeholders={"host": hoststr},
        ) from err
    except (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": hoststr},
        ) from err

    entry.runtime_data = device
    _async_prune_stale_devices(hass, entry, device)
    _async_register_keypads_and_dispatch_events(hass, entry, device)
    _async_register_scene_event_dispatch(hass, device)

    # `firmware_rev` arrives via the post-connect `get_config` response
    # (fire-and-forget; see TagoDevice.connection_task). It usually lands
    # within a few ms but `device.connect()` doesn't wait for it. Allow
    # up to 1s so the repair-issue check below has a value to compare.
    deadline = time.monotonic() + 1.0
    while device.firmware_rev is None and time.monotonic() < deadline:
        await asyncio.sleep(0.02)

    _async_check_firmware_repair(hass, entry, device)

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS
    )

    return True


def _parse_version(v: str | None) -> tuple[int, ...] | None:
    """Parse a dotted version string into a tuple for comparison.
    Returns None if the input isn't a clean dotted-decimal version."""
    if not v:
        return None
    try:
        return tuple(int(p) for p in v.split("."))
    except (ValueError, AttributeError):
        return None


def _async_register_keypads_and_dispatch_events(
    hass: HomeAssistant, entry: ConfigEntry, device: TagoDevice
) -> None:
    """Register each TagoKeypad as a HA device-registry entry and wire its
    key events to the HA bus so users can author automations against
    keypad presses without per-key entities (PROTOCOL_PROPOSALS §P2.6)."""
    registry = dr.async_get(hass)
    for kpd in device.entities:
        if not isinstance(kpd, TagoKeypad):
            continue
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, kpd.unique_id)},
            manufacturer=device.manufacturer,
            model=kpd.model_num,
            name=kpd.name or f"Keypad {kpd._tag}",
            suggested_area=kpd.location,
            via_device=(DOMAIN, device.unique_id),
        )
        kpd.set_on_key_event(_make_key_event_dispatcher(hass, kpd))


def _make_key_event_dispatcher(hass: HomeAssistant, kpd: TagoKeypad):
    """Build an async callback for `TagoKeypad.set_on_key_event` that
    fans the wire event out to hass.bus as `EVENT_TAGO_KEY`."""
    async def _dispatch(msg) -> None:
        data = msg.data if isinstance(msg.data, dict) else {}
        hass.bus.async_fire(EVENT_TAGO_KEY, {
            "keypad_id": msg.src,
            "key_id": data.get(TagoKeypad.PROP_KEY_ID),
            "event": msg.evt,
            "data": data.get("data"),
            "duration": data.get("duration"),
            "led_state": data.get("led_state"),
            "led_color": data.get("led_color"),
        })
    return _dispatch


def _async_register_scene_event_dispatch(
    hass: HomeAssistant, device: TagoDevice
) -> None:
    """Wire each TagoScene's activation callback to fire `EVENT_TAGO_SCENE`
    on HA's bus. Lets automations trigger on device-initiated scene
    activations (e.g., scene fired from a keypad press) — HA's own
    scene-domain events only fire when `scene.turn_on` is called from HA."""
    for scn in device.entities:
        if isinstance(scn, TagoScene):
            scn.set_on_scene_activated(_make_scene_event_dispatcher(hass, scn))


def _make_scene_event_dispatcher(hass: HomeAssistant, scn: TagoScene):
    async def _dispatch(msg) -> None:
        data = msg.data if isinstance(msg.data, dict) else {}
        hass.bus.async_fire(EVENT_TAGO_SCENE, {
            "scene_id": msg.src,
            "name": data.get("name") or scn.name,
            "ts": data.get("ts"),
        })
    return _dispatch


def _async_check_firmware_repair(
    hass: HomeAssistant, entry: ConfigEntry, device: TagoDevice
) -> None:
    """Raise a Repair issue if the device's firmware is older than the
    minimum supported version — Gold-tier rule `repair-issues`.

    Clears any previously-raised issue if the firmware now meets the
    minimum (e.g., user updated firmware and reloaded the integration).
    """
    issue_id = f"firmware_too_old_{entry.entry_id}"
    current = _parse_version(device.firmware_rev)
    minimum = _parse_version(MIN_FIRMWARE_VERSION)
    if current is None or minimum is None or current >= minimum:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="firmware_too_old",
        translation_placeholders={
            "current": device.firmware_rev or "unknown",
            "minimum": MIN_FIRMWARE_VERSION,
            "serial": device.serial_num or "unknown",
        },
    )


def _async_prune_stale_devices(
    hass: HomeAssistant, entry: ConfigEntry, device: TagoDevice
) -> None:
    """Remove `device_registry` entries that don't correspond to any
    currently-configured load — Gold-tier rule `stale-devices`.

    Triggers when a load slot has been UNUSED'd since the previous setup
    (or in the very rare case where the device's load list has shrunk).
    The HA reload model means this runs every time the user reloads the
    integration after reconfiguring loads via the device UI.

    Always keeps the gateway entry itself; only loads are pruned.
    """
    current_ids: set[str] = {device.unique_id}
    for e in device.entities:
        if not e.is_unused():
            current_ids.add(e.unique_id)

    registry = dr.async_get(hass)
    for dev_entry in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain_, ident in dev_entry.identifiers:
            if domain_ == DOMAIN and ident not in current_ids:
                _LOGGER.debug(
                    "Pruning stale device registry entry: %s", ident,
                )
                registry.async_remove_device(dev_entry.id)
                break


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Disconnect failures are non-fatal — we still want to unload the
    platforms so HA doesn't end up with a half-loaded entry. If
    `disconnect()` raises before it could stop the underlying
    connection task, force-cancel the task so it doesn't outlive the
    config entry.
    """
    import contextlib

    device: TagoDevice | None = entry.runtime_data
    if device is not None:
        try:
            await device.disconnect()
        except Exception as err:
            _LOGGER.debug("Disconnect during unload failed: %s", err)
            task = getattr(device, "_task", None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Repair issues are tied to the active entry — clear any we raised.
        ir.async_delete_issue(
            hass, DOMAIN, f"firmware_too_old_{entry.entry_id}",
        )
        _LOGGER.debug("Unloaded entry for %s", entry.entry_id)

    return unload_ok
