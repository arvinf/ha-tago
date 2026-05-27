from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant  # noqa: F401  (re-exported via platform signature)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .TagoNet import TagoDevice, TagoGateway
from . import generate_device_info

PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    gateway: TagoGateway = config_entry.runtime_data
    items = []
    # One reboot + one identify button per TagoDevice — each maps to a
    # device-specific wire request, not a gateway-wide one.
    for device in gateway.devices:
        items.append(RebootButton(device))
        items.append(IdentifyButton(device))
    async_add_entities(items)


class _DeviceLevelButton(ButtonEntity):
    """Base for buttons that ride on a TagoDevice's state-change
    callbacks. Subscription is wired in `async_added_to_hass` (when
    `self.hass` is guaranteed set) and torn down in
    `async_will_remove_from_hass` so callbacks never fire against a
    half-bound entity."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device: TagoDevice):
        self._device = device
        # Cache the device_info snapshot at construction. Safe per D7
        # (config is frozen for the HA entry's lifetime): the device's
        # firmware/location/identity won't change beneath us. If the
        # device hasn't reported its firmware yet (offline at initial
        # connect) the dict may carry None for some fields — HA renders
        # those slots blank, the card just looks unpopulated until the
        # device comes online and the integration reloads.
        self._attr_device_info = generate_device_info(device)

    @property
    def available(self) -> bool:
        return self._device.is_connected

    async def async_added_to_hass(self) -> None:
        # Registration deferred to `async_added_to_hass` so callbacks
        # never fire against an entity HA hasn't bound (`self.hass`
        # would be None, breaking `async_write_ha_state`). The
        # construction → registration window is sub-millisecond and
        # entity setup runs *after* the gateway's initial connect, so
        # the entity can't miss a state event in that window — by the
        # time we get here, `device.is_connected` already reflects the
        # current transport state.
        self._device.set_on_state_changed(self._on_device_state_changed)

    async def async_will_remove_from_hass(self) -> None:
        self._device.remove_on_state_changed(self._on_device_state_changed)

    def _on_device_state_changed(self) -> None:
        self.async_write_ha_state()


class RebootButton(_DeviceLevelButton):
    """A button to reboot the device."""

    # Reboot is destructive (drops the WebSocket, resets state). Default
    # disabled so it only appears for users who explicitly want it.
    _attr_entity_registry_enabled_default = False

    def __init__(self, device: TagoDevice):
        super().__init__(device)
        self._attr_unique_id = f"{device.unique_id}:reboot"
        self._attr_name = "Reboot Device"

    async def async_press(self):
        await self._device.reboot()


class IdentifyButton(_DeviceLevelButton):
    """A button to identify the device."""

    def __init__(self, device: TagoDevice):
        super().__init__(device)
        self._attr_unique_id = f"{device.unique_id}:identify"
        self._attr_name = "Identify Device"

    async def async_press(self):
        await self._device.identify()
