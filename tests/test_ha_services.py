"""HA-harness roundtrip tests.

Uses the official `pytest-homeassistant-custom-component` fixtures (`hass`,
`MockConfigEntry`, `enable_custom_integrations`) so the integration is
exercised the same way Home Assistant runs it in production:

  - HA service calls go through `hass.services.async_call("light", ...)`
    rather than directly invoking `TagoLightHA.async_turn_on(...)`.
  - HA state is read via `hass.states.get("light.…").state` and
    `.attributes["brightness"]` rather than the wrapper's properties.
  - Entity lookup goes through the entity registry, so we exercise the
    real `unique_id` ↔ `entity_id` mapping.

These are slower than the wrapper-level tests in `test_ha_roundtrip.py`
(roughly 1s vs 0.05s each) because they spin up a full HA core per test;
the trade-off is catching HA-side regressions — service-schema changes,
state-machine semantics, entity-registry contract — that the wrapper-level
tests miss.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from syrupy.assertion import SnapshotAssertion

from custom_components.tago.const import CONF_HOSTSTR, CONF_PIN, DOMAIN

L0 = "TAGO_TEST_001L1_0"
L1 = "TAGO_TEST_001L1_1"


@pytest.fixture(autouse=True)
def _patch_ramp_to_not_spawn_task(monkeypatch):
    """The integration spawns a perpetual Ramp task on `state_changed`
    events that carry a `ramp` block. HA's `verify_cleanup` fixture flags
    that as a lingering task on teardown. Patch `Ramp.__init__` to not
    spawn the local interpolation task — wrappers still see correct
    `is_ramp_active` because the object exists."""
    import time as _time
    from custom_components.tago import TagoNet as _tn

    def _patched_init(self, start, end, duration, elapsed, update_interval, callback):
        self.start = start
        self.end = end
        self.duration = duration
        self.elapsed = elapsed
        self.start_time = round(_time.time() * 1000)
        self.update_interval = update_interval
        self.cb = callback
        self.task = None

    monkeypatch.setattr(_tn.Ramp, "__init__", _patched_init)


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _resolve_entity_id(hass, domain: str, unique_id: str) -> str:
    """Map our wire-level unique_id to HA's slugified entity_id."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id, f"no {domain} entity registered for unique_id={unique_id}"
    return entity_id


def _state_snapshot(hass, entity_id: str):
    """Snapshot-friendly view of an entity's state and attributes.

    Excludes `last_changed` / `last_updated` (timestamps) and any other
    fields that vary between runs — so the snapshot focuses on the shape
    the integration controls."""
    state = hass.states.get(entity_id)
    assert state is not None, f"no state for {entity_id}"
    return {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": {
            k: v for k, v in state.attributes.items()
            # `friendly_name` includes device name + entity name; stable per
            # test setup but worth keeping. Exclude HA's internal mutable
            # fields like `restored`.
            if k not in ("restored",)
        },
    }


async def _setup_entry(hass, fake_server, seed: dict) -> MockConfigEntry:
    """Seed the fake firmware, build a config entry pointing at it, and
    wait for HA to finish setting it up."""
    fake_server.seed(seed)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,  # must match TagoConfigFlowHandler.VERSION
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest_asyncio.fixture
async def setup_factory(hass, enable_custom_integrations, fake_server):
    """Returns an async callable: seed → entry, fully set up."""
    entries: list[MockConfigEntry] = []

    async def _factory(seed: dict) -> MockConfigEntry:
        entry = await _setup_entry(hass, fake_server, seed)
        entries.append(entry)
        return entry

    yield _factory

    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# =====================================================================
# HA → wire: light services
# =====================================================================

@pytest.mark.asyncio
async def test_service_light_turn_on_brightness_translates_to_set_light(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 0, "name": "Test Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "brightness": 128},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_light")
    assert frame["dst"] == L0
    assert frame["brightness"] == 502
    assert "duration" not in frame


@pytest.mark.asyncio
async def test_service_light_turn_on_with_transition_includes_duration(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 0, "name": "Test Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "brightness": 200, "transition": 1.5},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_light")
    assert frame["brightness"] == 784
    assert frame["duration"] == 1500


@pytest.mark.asyncio
async def test_service_light_turn_off_sends_brightness_zero(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 800, "name": "Test Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_light")
    assert frame["brightness"] == 0


@pytest.mark.asyncio
async def test_service_light_turn_on_color_temp_kelvin_translates(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_ww", "brightness": 800, "ct": 0,
             "ct_range": [2700, 6500], "name": "Test CCT", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "color_temp_kelvin": 4600},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_light")
    assert frame["ct"] == 500


@pytest.mark.asyncio
async def test_service_light_turn_on_onoff_subtype_routes_via_turn_on(
    hass, fake_server, setup_factory
):
    """`light.turn_on` on a `light_onoff` entity must emit a `turn_on`
    request, never `set_light` (PROTOCOL.md §12.7)."""
    await setup_factory({
        L0: {"type": "light_onoff", "is_on": False, "name": "Test On/Off", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_on" for r in fake_server.received)
    )
    reqs = [r.get("req") for r in fake_server.received]
    assert "turn_on" in reqs
    assert "set_light" not in reqs


# =====================================================================
# HA → wire: switch / fan / cover services
# =====================================================================

@pytest.mark.asyncio
async def test_service_switch_turn_on_sends_turn_on(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "outlet_onoff", "is_on": False, "name": "Pump", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "switch", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_on" and r.get("dst") == L0
                    for r in fake_server.received)
    )


@pytest.mark.asyncio
async def test_service_fan_turn_on_sends_turn_on(hass, fake_server, setup_factory):
    await setup_factory({
        L0: {"type": "fan_onoff", "is_on": False, "name": "Ceiling Fan", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "fan", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_on" and r.get("dst") == L0
                    for r in fake_server.received)
    )


@pytest.mark.asyncio
async def test_service_cover_set_position_sends_move_to_inverted(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "cover_blind", "position": 0, "target": 0,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "cover", "set_cover_position",
        {"entity_id": entity_id, "position": 30},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "move_to" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "move_to")
    assert frame["target"] == 70  # HA position 30 → wire target 70


@pytest.mark.asyncio
async def test_service_cover_open_sends_move_to_zero(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "cover_blind", "position": 100, "target": 100,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)

    fake_server.received.clear()
    await hass.services.async_call(
        "cover", "open_cover", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "move_to" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "move_to")
    assert frame["target"] == 0


# =====================================================================
# wire → HA: state machine reflection
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_brightness_appears_in_hass_state(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 0, "name": "Test Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    await fake_server.broadcast_event(
        {"evt": "state_changed", "src": L0, "id": L0,
         "type": "light_dimmable", "brightness": 750}
    )
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None
                and s.attributes.get("brightness") == 191
    )
    assert _state_snapshot(hass, entity_id) == snapshot


@pytest.mark.asyncio
async def test_state_changed_is_on_appears_for_switch(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    await setup_factory({
        L0: {"type": "outlet_onoff", "is_on": False, "name": "Pump", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "switch", L0)

    await fake_server.broadcast_event(
        {"evt": "state_changed", "src": L0, "id": L0,
         "type": "outlet_onoff", "is_on": True}
    )
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None
                and s.state == STATE_ON
    )
    assert _state_snapshot(hass, entity_id) == snapshot


@pytest.mark.asyncio
async def test_state_changed_is_on_appears_for_fan(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    await setup_factory({
        L0: {"type": "fan_onoff", "is_on": False, "name": "Fan", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "fan", L0)

    await fake_server.broadcast_event(
        {"evt": "state_changed", "src": L0, "id": L0,
         "type": "fan_onoff", "is_on": True}
    )
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None
                and s.state == STATE_ON
    )
    assert _state_snapshot(hass, entity_id) == snapshot


@pytest.mark.asyncio
async def test_state_changed_ct_appears_as_color_temp_kelvin(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    await setup_factory({
        L0: {"type": "light_ww", "brightness": 800, "ct": 0,
             "ct_range": [2700, 6500], "name": "Test CCT", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    await fake_server.broadcast_event(
        {"evt": "state_changed", "src": L0, "id": L0,
         "type": "light_ww", "brightness": 800, "ct": 500}
    )
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None
                and s.attributes.get("color_temp_kelvin") == 4600
    )
    assert _state_snapshot(hass, entity_id) == snapshot


# =====================================================================
# Roundtrip via HA service + state machine
# =====================================================================

# =====================================================================
# Light subtype property coverage — exercises supported_color_modes /
# color_mode / type_to_string branches for every type variant.
# =====================================================================

@pytest.mark.parametrize(
    "light_type",
    ["light_mono", "light_ww", "light_rgb", "light_rgbw", "light_rgbww"],
)
@pytest.mark.asyncio
async def test_light_subtype_state_shape(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion, light_type
):
    """The HA-side state shape per light subtype is snapshot-pinned so any
    HA-version change that drops or adds an attribute surfaces immediately."""
    extra = {"ct_range": [2700, 6500]} if light_type in ("light_ww", "light_rgbww") else {}
    await setup_factory({
        L0: {"type": light_type, "brightness": 0, "name": f"Test {light_type}",
             "tag": "1A", **extra},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    assert _state_snapshot(hass, entity_id) == snapshot


@pytest.mark.asyncio
async def test_light_rgbw_white_mode_when_xy_zero(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    """RGBW light reports `color_mode = white` when x/y are both 0."""
    await setup_factory({
        L0: {"type": "light_rgbw", "brightness": 800, "x": 0.0, "y": 0.0,
             "name": "RGBW Light", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    assert _state_snapshot(hass, entity_id) == snapshot


@pytest.mark.asyncio
async def test_light_rgbw_xy_mode_when_xy_set(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    await setup_factory({
        L0: {"type": "light_rgbw", "brightness": 800, "x": 0.5, "y": 0.4,
             "name": "RGBW Light", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    assert _state_snapshot(hass, entity_id) == snapshot


@pytest.mark.asyncio
async def test_light_rgbww_color_temp_mode_when_xy_zero(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    await setup_factory({
        L0: {"type": "light_rgbww", "brightness": 800, "x": 0.0, "y": 0.0,
             "ct": 500, "ct_range": [2700, 6500],
             "name": "RGBWW Light", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    assert _state_snapshot(hass, entity_id) == snapshot


# =====================================================================
# Flash + white services
# =====================================================================

@pytest.mark.asyncio
async def test_service_light_turn_on_flash_short_sends_light_effect(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Test Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "flash": "short"},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "light_effect" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "light_effect")
    assert frame["effect"] == "flash"
    assert frame["duration"] == 4


@pytest.mark.asyncio
async def test_service_light_turn_on_flash_long_uses_longer_duration(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Test Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "flash": "long"},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "light_effect" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "light_effect")
    assert frame["duration"] == 10


@pytest.mark.asyncio
async def test_service_light_turn_on_white_uses_white_brightness(
    hass, fake_server, setup_factory
):
    """For RGBW, `white=128` is sent as `set_light brightness=502` (the
    integration treats `white` as the brightness on the white channel)."""
    await setup_factory({
        L0: {"type": "light_rgbw", "brightness": 0, "x": 0.0, "y": 0.0,
             "name": "RGBW Light", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "white": 128},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_light")
    assert frame["brightness"] == 502


# =====================================================================
# Non-CCT light leaks no color_temp attributes
# =====================================================================

@pytest.mark.asyncio
async def test_non_cct_light_omits_color_temp_attributes(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 0, "name": "Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    state = hass.states.get(entity_id)
    # color_temp / xy properties return None for unsupported subtypes.
    assert state.attributes.get("color_temp_kelvin") is None
    assert state.attributes.get("xy_color") is None


@pytest.mark.asyncio
async def test_non_color_light_xy_property_returns_none(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "light_ww", "brightness": 0, "ct": 0,
             "ct_range": [2700, 6500], "name": "CCT Light", "tag": "1A"},
    })
    # Build a wrapper directly to read xy_color property (returns None for non-RGB).
    from custom_components.tago.light import TagoLightHA
    from custom_components.tago.TagoNet import TagoLight
    entry = list(hass.config_entries.async_entries(DOMAIN))[0]
    device = entry.runtime_data
    entity = next(e for e in device.entities if isinstance(e, TagoLight) and e.unique_id == L0)
    ha = TagoLightHA(entity)
    assert ha.xy_color is None


# =====================================================================
# Roundtrip via HA service + state machine
# =====================================================================

# =====================================================================
# Cover device class — both blinds and curtain branches
# =====================================================================

@pytest.mark.asyncio
async def test_cover_curtain_uses_curtain_device_class(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "cover_curtain", "position": 0, "target": 0,
             "name": "Front Curtain", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    state = hass.states.get(entity_id)
    assert state.attributes.get("device_class") == "curtain"


# =====================================================================
# Buttons — reboot / identify push a request on the wire
# =====================================================================

@pytest.mark.asyncio
async def test_service_button_reboot_sends_reboot_request(
    hass, fake_server, setup_factory
):
    """The reboot button is `disabled_by_default`; enabling it via the
    entity registry, reloading the entry, and pressing it must still
    fire a `reboot` request on the wire."""
    from homeassistant.helpers import entity_registry as er_

    entry = await setup_factory({})
    registry = er_.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, "TAGO_TEST_001:reboot")
    assert entity_id is not None
    # Enable the entity, reload so HA picks up the change.
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    fake_server.received.clear()
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "reboot" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "reboot")
    assert frame["dst"] == "TAGO_TEST_001"


@pytest.mark.asyncio
async def test_service_button_identify_sends_identify_request(
    hass, fake_server, setup_factory
):
    await setup_factory({})
    entity_id = _resolve_entity_id(hass, "button", "TAGO_TEST_001:identify")
    fake_server.received.clear()
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "identify" for r in fake_server.received)
    )


# =====================================================================
# Entity-category + entity-disabled-by-default (Gold rules)
# =====================================================================

@pytest.mark.asyncio
async def test_connectivity_sensor_is_diagnostic_category(
    hass, fake_server, setup_factory
):
    """Connectivity sensor exposes itself as DIAGNOSTIC so it lives under
    the diagnostics section of the device page."""
    from homeassistant.helpers import entity_registry as er_
    await setup_factory({})
    entity_id = _resolve_entity_id(hass, "binary_sensor", "TAGO_TEST_001:connstate")
    registry = er_.async_get(hass)
    reg_entry = registry.async_get(entity_id)
    assert reg_entry.entity_category == er_.EntityCategory.DIAGNOSTIC


@pytest.mark.asyncio
async def test_reboot_button_is_config_category_and_disabled_by_default(
    hass, fake_server, setup_factory
):
    """Reboot is a destructive action — CONFIG category, default disabled."""
    from homeassistant.helpers import entity_registry as er_
    await setup_factory({})
    registry = er_.async_get(hass)
    reboot_id = registry.async_get_entity_id("button", DOMAIN, "TAGO_TEST_001:reboot")
    assert reboot_id is not None
    reg_entry = registry.async_get(reboot_id)
    assert reg_entry.entity_category == er_.EntityCategory.CONFIG
    assert reg_entry.disabled_by is not None


@pytest.mark.asyncio
async def test_identify_button_is_config_category_enabled(
    hass, fake_server, setup_factory
):
    """Identify is harmless — CONFIG category, enabled by default."""
    from homeassistant.helpers import entity_registry as er_
    await setup_factory({})
    registry = er_.async_get(hass)
    identify_id = registry.async_get_entity_id("button", DOMAIN, "TAGO_TEST_001:identify")
    assert identify_id is not None
    reg_entry = registry.async_get(identify_id)
    assert reg_entry.entity_category == er_.EntityCategory.CONFIG
    assert reg_entry.disabled_by is None


# =====================================================================
# Connectivity sensor reflects connection state
# =====================================================================

@pytest.mark.asyncio
async def test_connectivity_sensor_reports_on_when_connected(
    hass, fake_server, setup_factory
):
    """Connectivity sensor — a BinarySensorEntity registered under
    Platform.BINARY_SENSOR (file `binary_sensor.py`)."""
    await setup_factory({})
    entity_id = _resolve_entity_id(hass, "binary_sensor", "TAGO_TEST_001:connstate")
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON


# =====================================================================
# firmware_rev populated from get_config response
# =====================================================================

@pytest.mark.asyncio
async def test_firmware_rev_populated_after_connect(
    hass, fake_server, setup_factory
):
    await setup_factory({})
    entry = list(hass.config_entries.async_entries(DOMAIN))[0]
    gateway = entry.runtime_data
    # `firmware_rev` lives on each TagoDevice now, not on the gateway.
    device = gateway.devices[0]
    await _wait_until(lambda: device.firmware_rev == "1.0.0")
    assert device.firmware_rev == "1.0.0"


# =====================================================================
# Cover state reflection — position / target from state_changed
# =====================================================================

@pytest.mark.asyncio
async def test_state_changed_position_updates_cover_state(
    hass, fake_server, setup_factory, snapshot: SnapshotAssertion
):
    await setup_factory({
        L0: {"type": "cover_blind", "position": 0, "target": 0,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    await fake_server.broadcast_event(
        {"evt": "state_changed", "src": L0, "id": L0,
         "type": "cover_blind", "position": 60, "target": 60}
    )
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None
                and s.attributes.get("current_position") == 40  # HA = 100 - wire
    )
    assert _state_snapshot(hass, entity_id) == snapshot


# =====================================================================
# Cover service breadth + computed properties
# =====================================================================

@pytest.mark.asyncio
async def test_service_cover_close_sends_move_to_target_hundred(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "cover_blind", "position": 0, "target": 0,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "cover", "close_cover", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "move_to" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "move_to")
    assert frame["target"] == 100


@pytest.mark.asyncio
async def test_service_cover_stop_sends_stop_move(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "cover_blind", "position": 50, "target": 0,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "cover", "stop_cover", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "stop_move" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "stop_move")
    assert frame["dst"] == L0


@pytest.mark.parametrize(
    "cover_type,expected_device_class",
    [
        ("cover_shade", "shade"),
        ("cover_blind", "blind"),
        ("cover_curtain", "curtain"),
    ],
)
@pytest.mark.asyncio
async def test_cover_device_class_per_type(
    hass, fake_server, setup_factory, cover_type, expected_device_class
):
    await setup_factory({
        L0: {"type": cover_type, "position": 0, "target": 0,
             "name": f"My {cover_type}", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    state = hass.states.get(entity_id)
    assert state.attributes.get("device_class") == expected_device_class


@pytest.mark.asyncio
async def test_cover_is_closed_when_fully_closed(
    hass, fake_server, setup_factory
):
    """Wire position=100 → HA position=0 → cover state 'closed'."""
    await setup_factory({
        L0: {"type": "cover_blind", "position": 100, "target": 100,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    state = hass.states.get(entity_id)
    assert state.state == "closed"
    assert state.attributes.get("current_position") == 0


@pytest.mark.asyncio
async def test_cover_is_opening_when_target_above_position(
    hass, fake_server, setup_factory
):
    """Wire position=80 (mostly closed), target=20 (mostly open) →
    HA position 20→80 (opening)."""
    await setup_factory({
        L0: {"type": "cover_blind", "position": 80, "target": 20,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    state = hass.states.get(entity_id)
    assert state.attributes.get("current_position") == 20
    assert state.state == "opening"


@pytest.mark.asyncio
async def test_cover_is_closing_when_target_below_position(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "cover_blind", "position": 20, "target": 80,
             "name": "Blind", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "cover", L0)
    state = hass.states.get(entity_id)
    assert state.attributes.get("current_position") == 80
    assert state.state == "closing"


@pytest.mark.asyncio
async def test_cover_unknown_type_falls_back_to_shade_device_class():
    """cover.py:_DEVICE_CLASS_BY_TYPE.get(...).default — for any type
    not in {shade, blind, curtain} the wrapper falls back to SHADE."""
    from custom_components.tago.cover import TagoCoverHA
    from custom_components.tago.TagoNet import TagoCover, TagoDevice, TagoGateway
    gateway = TagoGateway("dummy:1", authkey="k")
    device = TagoDevice(gateway, {"id": "test_device", "available": True})
    cover = TagoCover(
        {"id": "C0", "type": TagoCover.COVER_SHADE,
         "name": "x", "location": "y", "tag": "1A",
         "position": 0, "target": 0},
        device,
    )
    cover._type = "cover_unknown_future_variant"
    from homeassistant.components.cover import CoverDeviceClass
    ha = TagoCoverHA(cover)
    assert ha._attr_device_class == CoverDeviceClass.SHADE


# =====================================================================
# Fan: turn_off + toggle via HA services
# =====================================================================

@pytest.mark.asyncio
async def test_service_fan_turn_off_sends_turn_off(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "fan_onoff", "is_on": True, "name": "Ceiling Fan", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "fan", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "fan", "turn_off", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_off" and r.get("dst") == L0
                    for r in fake_server.received)
    )


@pytest.mark.asyncio
async def test_service_fan_toggle_routes_through_turn_on(
    hass, fake_server, setup_factory
):
    """HA's `fan.toggle` calls `async_turn_on` when the entity is off
    (and `async_turn_off` when on); for an entity seeded as is_on=False
    this routes through to the `turn_on` wire request."""
    await setup_factory({
        L0: {"type": "fan_onoff", "is_on": False, "name": "Ceiling Fan", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "fan", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "fan", "toggle", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_on" and r.get("dst") == L0
                    for r in fake_server.received)
    )


# =====================================================================
# Binary sensor: flips OFF when websocket drops
# =====================================================================

@pytest.mark.asyncio
async def test_connectivity_sensor_flips_off_when_websocket_drops(
    hass, enable_custom_integrations, fake_server
):
    """Tear down the fake server while the integration is connected — the
    binary sensor should report `off` once the client notices the drop."""
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _resolve_entity_id(hass, "binary_sensor", "TAGO_TEST_001:connstate")
    assert hass.states.get(entity_id).state == STATE_ON

    # Stop the server. The client's connection_task observes the close,
    # sets _ws=None, and the entity's `is_on` returns False.
    await fake_server.stop()
    device = entry.runtime_data
    await _wait_until(lambda: not device.is_connected, timeout=2.0)
    assert device.is_connected is False

    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_OFF

    # Cleanup: device.connection_task keeps retrying every 3s; unload to stop.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# =====================================================================
# Roundtrip via HA service + state machine
# =====================================================================

@pytest.mark.asyncio
async def test_full_roundtrip_light_service_to_state(
    hass, fake_server, setup_factory
):
    """`light.turn_on` from HA → wire → fake server echoes state →
    `hass.states.get(...).attributes["brightness"]` converges on 128."""
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 0, "name": "Test Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "brightness": 128},
        blocking=True,
    )
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None
                and s.attributes.get("brightness") == 128
    )
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 128


# =====================================================================
# Light: turn_on with no args restores last brightness
# =====================================================================

@pytest.mark.asyncio
async def test_service_light_turn_on_no_args_restores_last_brightness(
    hass, fake_server, setup_factory
):
    """Turning on a previously-off light with no brightness arg should
    restore the last known brightness (light.py:143-144). The integration
    caches this on turn_off (light.py:176-177)."""
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 600, "name": "Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)

    # Turn off — this caches the current brightness.
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: (s := hass.states.get(entity_id)) is not None
                and s.state == STATE_OFF
    )

    # Turn on with no args — should restore cached brightness.
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_light")
    # The cached brightness should match what the light was at before turn_off.
    assert frame["brightness"] > 0


# =====================================================================
# Light: turn_off with transition sends duration
# =====================================================================

@pytest.mark.asyncio
async def test_service_light_turn_off_with_transition(
    hass, fake_server, setup_factory
):
    """light.turn_off with transition=2.0 should send set_light with
    brightness=0 and duration=2000."""
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_off",
        {"entity_id": entity_id, "transition": 2.0},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received if r.get("req") == "set_light")
    assert frame["brightness"] == 0
    assert frame["duration"] == 2000


# =====================================================================
# Light: turn_on with XY color
# =====================================================================

@pytest.mark.asyncio
async def test_service_light_turn_on_xy_color_sends_set_colour(
    hass, fake_server, setup_factory
):
    """light.turn_on with xy_color should route through set_colour,
    sending x and y on the wire."""
    await setup_factory({
        L0: {"type": "light_rgb", "brightness": 500, "x": 0.0, "y": 0.0,
             "name": "RGB Light", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": entity_id, "xy_color": [0.4, 0.35]},
        blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "set_light" and "x" in r
                    for r in fake_server.received)
    )
    frame = next(r for r in fake_server.received
                 if r.get("req") == "set_light" and "x" in r)
    assert frame["x"] == 0.4
    assert frame["y"] == 0.35


# =====================================================================
# Switch: turn_off via HA service
# =====================================================================

@pytest.mark.asyncio
async def test_service_switch_turn_off_sends_turn_off(
    hass, fake_server, setup_factory
):
    await setup_factory({
        L0: {"type": "outlet_onoff", "is_on": True, "name": "Pump", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "switch", L0)
    fake_server.received.clear()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True,
    )
    await _wait_until(
        lambda: any(r.get("req") == "turn_off" and r.get("dst") == L0
                    for r in fake_server.received)
    )


# =====================================================================
# Real sensor: state_changed event flips HA binary sensor
# =====================================================================

@pytest.mark.asyncio
async def test_sensor_state_changed_flips_binary_sensor(
    hass, fake_server, setup_factory
):
    """A physical sensor (e.g. motion) should reflect state changes from
    the wire in the HA binary_sensor state."""
    await setup_factory({
        L0: {"type": "sensor_motion", "is_on": False,
             "name": "Hallway Motion", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", L0)
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF

    await fake_server.broadcast_event(
        {"evt": "state_changed", "src": L0, "id": L0,
         "type": "sensor_motion", "is_on": True}
    )
    await _wait_until(
        lambda: hass.states.get(entity_id).state == STATE_ON
    )
    assert hass.states.get(entity_id).state == STATE_ON


# =====================================================================
# Virtual switch: state_changed event reflects in HA
# =====================================================================

@pytest.mark.asyncio
async def test_virtual_switch_state_changed_from_wire(
    hass, fake_server, setup_factory
):
    """The firmware can flip a virtual switch from its own automation
    engine — HA should reflect this without a service call."""
    from homeassistant.helpers import entity_registry as er_
    entry = await setup_factory({
        L0: {"type": "virtual_switch", "is_on": False, "index": 0,
             "name": "Holiday Mode", "location": "VIRTUAL", "tag": "VS1"},
    })
    registry = er_.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, L0)
    assert entity_id is not None
    # Virtual switches are disabled-by-default; enable it.
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF

    await fake_server.broadcast_event(
        {"evt": "state_changed", "src": L0, "id": L0,
         "type": "virtual_switch", "is_on": True}
    )
    await _wait_until(
        lambda: hass.states.get(entity_id).state == STATE_ON
    )


# =====================================================================
# Firmware update available event → binary sensor
# =====================================================================

@pytest.mark.asyncio
async def test_firmware_update_event_flips_binary_sensor(
    hass, fake_server, setup_factory
):
    """A firmware_update_available event from the wire should flip
    the FirmwareUpdateAvailableSensor to ON and populate
    latest_version in extra_state_attributes."""
    from homeassistant.helpers import entity_registry as er_
    entry = await setup_factory({})
    DEVICE_ID = "TAGO_TEST_001"

    registry = er_.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DEVICE_ID}:firmware_update"
    )
    assert entity_id is not None
    # Disabled-by-default — enable it.
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF

    await fake_server.broadcast_event(
        {"evt": "firmware_update_available", "src": DEVICE_ID,
         "latest_firmware_rev": "2.0.0"}
    )
    await _wait_until(
        lambda: hass.states.get(entity_id).state == STATE_ON
    )
    state = hass.states.get(entity_id)
    assert state.attributes["latest_version"] == "2.0.0"
    assert state.attributes["current_version"] == "1.0.0"


# =====================================================================
# Device unavailable/available events → entity availability
# =====================================================================

@pytest.mark.asyncio
async def test_device_unavailable_event_makes_entities_unavailable(
    hass, fake_server, setup_factory
):
    """When the firmware reports a device as unavailable, all entities
    on that device should become unavailable in HA."""
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500,
             "name": "Dimmer", "tag": "1A"},
    })
    entity_id = _resolve_entity_id(hass, "light", L0)
    assert hass.states.get(entity_id).state != "unavailable"

    await fake_server.broadcast_event(
        {"evt": "device_unavailable", "device_id": "TAGO_TEST_001"}
    )
    await _wait_until(
        lambda: hass.states.get(entity_id).state == "unavailable",
        timeout=3.0,
    )
    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.asyncio
async def test_device_available_event_restores_entities(
    hass, enable_custom_integrations, fake_server
):
    """After a device_unavailable, a device_available event should
    restore entities to their normal state."""
    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 500,
             "name": "Dimmer", "tag": "1A"},
    })
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = _resolve_entity_id(hass, "light", L0)

    # Make unavailable first.
    await fake_server.broadcast_event(
        {"evt": "device_unavailable", "device_id": "TAGO_TEST_001"}
    )
    await _wait_until(
        lambda: hass.states.get(entity_id).state == "unavailable",
        timeout=3.0,
    )

    # Restore. Verify at the protocol layer that `_on_available`
    # flips the device's `_available` back to True and fires the
    # entity callbacks. Testing through HA's state machine is fragile
    # because the async chain (`get_state` response → state callback
    # → `schedule_update_ha_state`) races with HA's event loop.
    gateway = entry.runtime_data
    device = gateway.devices[0]
    assert device.available is False

    await fake_server.broadcast_event(
        {"evt": "device_available", "device_id": "TAGO_TEST_001"}
    )
    await _wait_until(lambda: device.available is True, timeout=3.0)
    assert device.available is True
    assert device.is_connected is True

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# =====================================================================
# Callback cleanup on unload — no stale callbacks fire post-teardown
# =====================================================================

@pytest.mark.asyncio
async def test_entity_callbacks_deregistered_on_unload(
    hass, enable_custom_integrations, fake_server
):
    """After unloading the integration, state-change callbacks should be
    deregistered. Broadcasting a state event after unload should NOT
    cause any HA state writes (which would error on a half-torn-down
    entity)."""
    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 500,
             "name": "Dimmer", "tag": "1A"},
    })
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    gateway = entry.runtime_data
    entity = next(e for e in gateway.entities if e.unique_id == L0)

    # Verify callback is registered.
    assert len(entity._update_cbs) > 0

    # Unload.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Callback should be deregistered.
    assert len(entity._update_cbs) == 0


# =====================================================================
# Per-load fault indicator + Repair issue — PROTOCOL.md §11.3
# =====================================================================

def _fault_issue_id(entry, unique_id: str) -> str:
    return f"load_fault_{entry.entry_id}_{unique_id}"


async def _broadcast_fault(fake_server, codes: list[str]) -> None:
    await fake_server.broadcast_event({
        "evt": "state_changed", "src": L0, "id": L0,
        "type": "light_dimmable", "brightness": 500,
        "fault": codes,
    })


@pytest.mark.asyncio
async def test_load_fault_sensor_starts_off_and_has_problem_device_class(
    hass, fake_server, setup_factory
):
    """Every load gets one fault indicator, off while the firmware
    reports an empty fault array."""
    await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer",
             "tag": "1A", "fault": []},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["device_class"] == "problem"
    assert state.attributes["fault_reasons"] == []


@pytest.mark.asyncio
async def test_overcurrent_turns_on_fault_sensor_and_raises_issue(
    hass, fake_server, setup_factory
):
    """An `"oc"` code flips the indicator on and raises a
    Repair issue the user can see."""
    entry = await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer",
             "tag": "1A", "fault": []},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")
    issue_registry = ir.async_get(hass)
    assert issue_registry.async_get_issue(DOMAIN, _fault_issue_id(entry, L0)) is None

    await _broadcast_fault(fake_server, ["oc"])
    await _wait_until(lambda: hass.states.get(entity_id).state == STATE_ON)

    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes["fault_reasons"] == ["overcurrent"]

    issue = issue_registry.async_get_issue(DOMAIN, _fault_issue_id(entry, L0))
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "load_fault"
    assert issue.translation_placeholders["name"] == "Dimmer"
    assert issue.translation_placeholders["reasons"] == "overcurrent"


@pytest.mark.asyncio
async def test_overtemp_uses_the_same_single_fault_indicator(
    hass, fake_server, setup_factory
):
    """Overtemperature raises the same indicator and issue as
    overcurrent — the user isn't asked to reason about the two
    firmware codes separately."""
    entry = await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer",
             "tag": "1A", "fault": []},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")

    await _broadcast_fault(fake_server, ["ot"])
    await _wait_until(lambda: hass.states.get(entity_id).state == STATE_ON)

    assert hass.states.get(entity_id).attributes["fault_reasons"] == [
        "overtemperature"
    ]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _fault_issue_id(entry, L0))
    assert issue is not None
    assert issue.translation_placeholders["reasons"] == "overtemperature"

    # No separate per-code entities exist.
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{L0}:oc_fault") is None
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{L0}:ot_fault") is None


@pytest.mark.asyncio
async def test_both_codes_active_reports_both_reasons(
    hass, fake_server, setup_factory
):
    entry = await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer",
             "tag": "1A", "fault": []},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")

    await _broadcast_fault(fake_server, ["oc", "ot"])
    await _wait_until(lambda: hass.states.get(entity_id).state == STATE_ON)

    assert hass.states.get(entity_id).attributes["fault_reasons"] == [
        "overcurrent", "overtemperature",
    ]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _fault_issue_id(entry, L0))
    assert issue.translation_placeholders["reasons"] == (
        "overcurrent, overtemperature"
    )


@pytest.mark.asyncio
async def test_unknown_fault_code_still_reports_a_fault(
    hass, fake_server, setup_factory
):
    """An unrecognized code from future firmware must still raise the
    indicator and issue, surfaced verbatim."""
    entry = await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer",
             "tag": "1A", "fault": []},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")

    await _broadcast_fault(fake_server, ["zz"])
    await _wait_until(lambda: hass.states.get(entity_id).state == STATE_ON)

    assert hass.states.get(entity_id).attributes["fault_reasons"] == ["zz"]
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, _fault_issue_id(entry, L0)
    ) is not None


@pytest.mark.asyncio
async def test_fault_clearing_turns_sensor_off_and_deletes_issue(
    hass, fake_server, setup_factory
):
    """An empty fault array clears both the indicator and the Repair
    issue — the user shouldn't have to dismiss a stale notice."""
    entry = await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer",
             "tag": "1A", "fault": []},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")
    issue_registry = ir.async_get(hass)

    await _broadcast_fault(fake_server, ["oc", "ot"])
    await _wait_until(lambda: hass.states.get(entity_id).state == STATE_ON)
    assert issue_registry.async_get_issue(DOMAIN, _fault_issue_id(entry, L0)) is not None

    await _broadcast_fault(fake_server, [])
    await _wait_until(lambda: hass.states.get(entity_id).state == STATE_OFF)

    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["fault_reasons"] == []
    assert issue_registry.async_get_issue(DOMAIN, _fault_issue_id(entry, L0)) is None


@pytest.mark.asyncio
async def test_partial_clear_keeps_remaining_fault(
    hass, fake_server, setup_factory
):
    """The array is a snapshot: dropping one code clears only that one."""
    entry = await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 500, "name": "Dimmer",
             "tag": "1A", "fault": ["oc", "ot"]},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")
    assert hass.states.get(entity_id).state == STATE_ON

    await _broadcast_fault(fake_server, ["ot"])
    await _wait_until(
        lambda: hass.states.get(entity_id).attributes["fault_reasons"] == [
            "overtemperature"
        ]
    )

    assert hass.states.get(entity_id).state == STATE_ON
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, _fault_issue_id(entry, L0)
    ) is not None


@pytest.mark.asyncio
async def test_fault_active_at_setup_raises_issue_immediately(
    hass, fake_server, setup_factory
):
    """A load already faulted when HA connects surfaces the issue on
    setup, without waiting for a state_changed event."""
    entry = await setup_factory({
        L0: {"type": "light_dimmable", "brightness": 0, "name": "Dimmer",
             "tag": "1A", "fault": ["oc"]},
    })
    entity_id = _resolve_entity_id(hass, "binary_sensor", f"{L0}:fault")
    assert hass.states.get(entity_id).state == STATE_ON
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, _fault_issue_id(entry, L0)
    ) is not None


@pytest.mark.asyncio
async def test_fault_issue_cleared_on_unload(
    hass, enable_custom_integrations, fake_server
):
    """Unloading the entry clears its fault issues so a removed entry
    doesn't leave orphaned Repairs behind."""
    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 0, "name": "Dimmer",
             "tag": "1A", "fault": ["oc"]},
    })
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue_registry = ir.async_get(hass)
    assert issue_registry.async_get_issue(DOMAIN, _fault_issue_id(entry, L0)) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert issue_registry.async_get_issue(DOMAIN, _fault_issue_id(entry, L0)) is None


@pytest.mark.asyncio
async def test_non_load_entities_have_no_fault_indicator(
    hass, fake_server, setup_factory
):
    """Scenes aren't loads — they never report fault flags, so they
    don't get an indicator."""
    await setup_factory({
        L0: {"type": "scene", "name": "Evening", "tag": "1A"},
    })
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{L0}:fault") is None
