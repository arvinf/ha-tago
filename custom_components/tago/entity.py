from __future__ import annotations

import json

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .TagoNet import TagoEntity

@callback
def async_get_or_create_location_area(
    hass: HomeAssistant,
    location_name: str,
) -> ar.AreaEntry:
    """Create/find an HA area and optional floor from a source location.

    Source formats:
      "Master Bedroom"             -> Area: Master Bedroom
      "First_Floor/Master Bedroom" -> Floor: First_Floor, Area: Master Bedroom

    Returns the AreaEntry that should be used for device placement.
    """

    location_name = location_name.strip()
    if not location_name:
        raise ValueError("Location name cannot be empty")

    area_registry = ar.async_get(hass)

    # Location contains no floor.
    if "/" not in location_name:
        area_name = location_name

        area = area_registry.async_get_area_by_name(area_name)
        if area is not None:
            return area

        return area_registry.async_create(area_name)

    # Location contains a floor and an area.
    floor_name, area_name = (
        part.strip() for part in location_name.split("/", 1)
    )

    if not floor_name or not area_name:
        raise ValueError(
            f"Invalid location '{location_name}'; expected 'Floor/Area'"
        )

    floor_registry = fr.async_get(hass)

    # Create or retrieve the floor.
    floor = floor_registry.async_get_floor_by_name(floor_name)
    if floor is None:
        floor = floor_registry.async_create(floor_name)

    # Create or retrieve the area.
    area = area_registry.async_get_area_by_name(area_name)

    if area is None:
        return area_registry.async_create(
            area_name,
            floor_id=floor.floor_id,
        )

    # Existing area has no floor yet: attach it to this floor.
    if area.floor_id is None:
        return area_registry.async_update(
            area.id,
            floor_id=floor.floor_id,
        )

    # Existing area already belongs to this floor: nothing to do.
    if area.floor_id == floor.floor_id:
        return area

    # # Existing area belongs to another floor: don't silently move it.
    # existing_floor = floor_registry.async_get_floor(area.floor_id)
    # existing_floor_name = (
    #     existing_floor.name if existing_floor is not None else area.floor_id
    # )

    # raise LocationConflictError(
    #     f"Area '{area_name}' is already assigned to floor "
    #     f"'{existing_floor_name}', but source location requested "
    #     f"floor '{floor_name}'"
    # )

class TagoEntityHA:
    MAX_VALUE = 10000

    def __init__(self, entity: TagoEntity):
        self._entity: TagoEntity = entity

    async def async_added_to_hass(self) -> None:
        # Register the state-change callback only once HA has bound
        # the entity (entity_id, hass) — calling schedule_update_ha_state
        # before that raises. The matching `async_will_remove_from_hass`
        # below tears it down so callbacks never fire against a
        # half-bound entity during teardown.
        self._entity.set_on_state_changed(self.on_state_updated)

    async def async_will_remove_from_hass(self) -> None:
        self._entity.remove_on_state_changed(self.on_state_updated)

    def on_state_updated(self):
        self.schedule_update_ha_state()

    def __repr__(self):
        return json.dumps({
            'id': self.unique_id,
            'name': self.name,
            'location': self._entity.location,
            'type': self.type,
            'connected': self.available
        }, indent=2)

    def is_of_domain(self, domain: str) -> bool:
        return False

    def update(self) -> None:
        self.schedule_update_ha_state()

    @property
    def type(self) -> str:
        return self._entity.type or 'UNUSED'

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def has_entity_name(self) -> bool:
        # Per-entity device-card pattern: the device card carries the
        # user-set name, the HA entity is the device's primary entity.
        # `has_entity_name=True` + `name=None` makes HA use the device
        # card name verbatim for `friendly_name`.
        return True

    @property
    def name(self) -> str | None:
        """`None` so HA uses the device-card name (which we set from
        the wire `name` in `device_info`) verbatim, instead of
        concatenating `<device> <entity>` and ending up with the
        device label doubled."""
        return None

    @property
    def _device_card_name(self) -> str:
        """Display name for this entity's device-registry card.
        Falls back to `<device-id> <tag>` then `<unique_id>` so
        unnamed entities still get something readable."""
        return self._entity.name or (
            f'{self._entity.device.unique_id} {self._entity.tag}'
            if self._entity.tag else self._entity.unique_id
        )

    @property
    def location(self) -> str | None:
        """Name"""
        return self._entity.location

    @property
    def unique_id(self) -> str:
        """Unique id"""
        return self._entity.unique_id

    @property
    def available(self) -> bool:
        """Available"""
        return self._entity.is_connected

    @property
    def type_to_string(self) -> int:
        return ''

    @property
    def device_info(self) -> DeviceInfo | None:
        if self._entity.is_unused():
            return None

        # Device-locked entity (id starts with `_`): the firmware
        # asserts there is exactly one user-facing function on this
        # TagoDevice, so HA attaches this entity directly to the
        # main TagoDevice card. No sub-card minted; the main card
        # already carries the firmware/location identity.
        if not self._entity.is_device_multichannel:
            return DeviceInfo(
                identifiers={(DOMAIN, self._entity.device.unique_id)},
            )

        # Multichannel device — mint a sub-card carrying the entity's
        # user-set name and location, with the firmware-issued `tag`
        # surfaced as the card's `serial_number` so the user can match
        # the HA entity against the device's config-UI label.
        # Pre-create the area as a side effect — but only once HA has
        # bound `hass` onto this entity (unit tests instantiate without).
        if self.location and getattr(self, "hass", None) is not None:
            async_get_or_create_location_area(self.hass, self.location)
        return DeviceInfo(
            identifiers={(DOMAIN, self._entity.unique_id)},
            name=self._device_card_name,
            manufacturer=self._entity.device.manufacturer,
            model=self.type_to_string,
            configuration_url=self._entity.dashboard_uri,
            suggested_area=self._entity.location,
            serial_number=self._entity.tag,
            via_device=(DOMAIN, self._entity.device.unique_id)
        )

    @property
    def extra_state_attributes(self) -> dict | None:
        """Surface the firmware-issued `tag` as a non-user-overridable
        attribute. Present regardless of whether the entity has its
        own sub-card — gives the user a way to cross-reference the
        HA entity against the device's config UI even when `name` has
        been renamed in HA."""
        # UNUSED entities don't get HA wrappers in practice; this is
        # defense-in-depth so a future code path that wraps one
        # doesn't surface a meaningless tag attribute.
        if self._entity.is_unused():
            return None
        tag = self._entity.tag
        if not tag:
            return None
        return {"tag": tag}

    @staticmethod
    def convert_value_to_device(
        intensity: float, srclimit: float = 255
    ) -> float:
        return (intensity / srclimit)

    @staticmethod
    def convert_value_from_device(
        intensity: float, srclimit: float = 255
    ) -> int:
        return int(round((intensity * srclimit), 0))
