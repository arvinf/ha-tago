"""Tests for Gold-tier rules `diagnostics` and `stale-devices`.

`diagnostics`: HA's "Download diagnostics" button calls
`async_get_config_entry_diagnostics`, which returns a structured snapshot
with sensitive fields redacted.

`stale-devices`: on each setup, the integration prunes `device_registry`
entries that don't correspond to a currently-configured load. This is
how the reload-required topology-change model keeps HA's device list
in sync with the actual hardware.
"""
from __future__ import annotations

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tago.const import (
    CONF_AUTHKEY,
    CONF_HOSTSTR,
    CONF_PIN,
    DOMAIN,
)
from custom_components.tago.diagnostics import async_get_config_entry_diagnostics

L0 = "TAGO_TEST_001L1_0"
L1 = "TAGO_TEST_001L1_1"


async def _setup(hass, fake_server, seed: dict) -> MockConfigEntry:
    fake_server.seed(seed)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# =====================================================================
# Diagnostics
# =====================================================================

@pytest.mark.asyncio
async def test_diagnostics_contains_entry_device_and_entity_fields(
    hass, enable_custom_integrations, fake_server
):
    entry = await _setup(hass, fake_server, {
        L0: {"type": "light_dimmable", "brightness": 500,
             "name": "Kitchen Light", "tag": "1A"},
        L1: {"type": "fan_onoff", "is_on": True,
             "name": "Ceiling Fan", "tag": "1B"},
    })

    payload = await async_get_config_entry_diagnostics(hass, entry)

    # Entry block
    assert payload["entry"]["unique_id"] == "TAGO_TEST_001"
    assert payload["entry"]["version"] == 9
    assert "data" in payload["entry"]

    # Gateway block
    assert payload["gateway"]["is_connected"] is True
    assert payload["gateway"]["device_count"] >= 1

    # Devices block — exactly one device, with its entities nested
    assert len(payload["devices"]) == 1
    dev = payload["devices"][0]
    assert dev["serial_num"] == "TAGO_TEST_001"
    assert dev["model_num"] == "dimac8"
    assert dev["firmware_rev"] == "1.0.0"
    assert dev["is_connected"] is True

    ids = {e["id"] for e in dev["entities"]}
    assert L0 in ids
    assert L1 in ids

    entity_l0 = next(e for e in dev["entities"] if e["id"] == L0)
    assert entity_l0["type"] == "light_dimmable"
    assert entity_l0["name"] == "Kitchen Light"
    assert entity_l0["is_unused"] is False

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_diagnostics_redacts_pin_and_host_and_api_key_fields(
    hass, enable_custom_integrations, fake_server
):
    """A non-empty PIN must be redacted to `**REDACTED**` (HA's
    `async_redact_data` is a no-op on empty/None values, so the
    fixture has to use a real secret to exercise the redaction)."""
    # Configure the fake server with the same PIN so auth succeeds.
    fake_server.pin = "secret-pin"
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "secret-pin"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    # PIN field must be redacted; the literal value must not leak.
    assert payload["entry"]["data"][CONF_PIN] == "**REDACTED**"
    assert "secret-pin" not in str(payload["entry"]["data"])

    # Host on the gateway block is redacted too (defensive — might be public).
    assert payload["gateway"]["host"] == "**REDACTED**"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_diagnostics_handles_entry_with_no_runtime_data(
    hass, enable_custom_integrations
):
    """If diagnostics is called for an entry that hasn't been set up
    (or was unloaded), the runtime_data is None — diagnostics still
    returns a valid (partial) payload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: "1.2.3.4", CONF_PIN: "x"},
        unique_id="TAGO_X",
        version=9,
    )
    entry.add_to_hass(hass)
    # No runtime_data assigned — entry not set up.

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["entry"]["unique_id"] == "TAGO_X"
    assert payload["gateway"] is None
    assert payload["devices"] == []
    # PIN still redacted.
    assert payload["entry"]["data"][CONF_PIN] == "**REDACTED**"


@pytest.mark.asyncio
async def test_diagnostics_legacy_authkey_field_also_redacted(
    hass, enable_custom_integrations
):
    """Defensive: if a pre-migration entry data dict still has the legacy
    `authkey` key (e.g. someone restored from a v8 backup), it must be
    redacted by `REDACT_KEYS`."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: "1.2.3.4", CONF_AUTHKEY: "leaky-old-api-key"},
        unique_id="TAGO_X",
        version=9,
    )
    entry.add_to_hass(hass)

    payload = await async_get_config_entry_diagnostics(hass, entry)
    assert "leaky-old-api-key" not in str(payload["entry"]["data"])
    assert payload["entry"]["data"][CONF_AUTHKEY] == "**REDACTED**"


# =====================================================================
# Stale-devices
# =====================================================================

@pytest.mark.asyncio
async def test_stale_device_registry_entry_pruned_when_load_now_unused(
    hass, enable_custom_integrations, fake_server
):
    """A device_registry entry that existed in a prior session but
    whose corresponding load is now UNUSED gets removed on setup."""
    fake_server.seed({L0: {"type": "UNUSED", "name": "", "tag": "1A"}})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    # Pre-create a stale device_registry entry for L0 as if it was
    # configured last session.
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, L0)},
        manufacturer="TAGO",
        name="Old Load",
    )
    assert registry.async_get_device(identifiers={(DOMAIN, L0)}) is not None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Stale entry should be gone — L0 is UNUSED now.
    assert registry.async_get_device(identifiers={(DOMAIN, L0)}) is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_stale_device_registry_keeps_currently_configured_loads(
    hass, enable_custom_integrations, fake_server
):
    """A device_registry entry for a load that IS currently configured
    must not be pruned."""
    fake_server.seed({
        L0: {"type": "light_dimmable", "name": "Living Room", "tag": "1A"},
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

    # Setup creates a device_registry entry for L0 via the entity's
    # DeviceInfo. Verify it stayed.
    registry = dr.async_get(hass)
    assert registry.async_get_device(identifiers={(DOMAIN, L0)}) is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_stale_device_registry_does_not_touch_other_entries(
    hass, enable_custom_integrations, fake_server
):
    """Pruning is scoped to entries belonging to THIS config entry —
    devices from other integrations or other Tago entries are untouched."""
    fake_server.seed({L0: {"type": "UNUSED"}})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    # Register a device from a different (mock) config entry.
    other_entry = MockConfigEntry(
        domain="other_integration",
        data={},
        unique_id="other-thing",
    )
    other_entry.add_to_hass(hass)

    registry = dr.async_get(hass)
    other = registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={("other_integration", "external-device-1")},
        manufacturer="Other",
        name="Other Device",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Other entry's device is still there.
    assert registry.async_get_device(
        identifiers={("other_integration", "external-device-1")}
    ) is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reload_removes_entity_when_load_no_longer_reported_by_firmware(
    hass, enable_custom_integrations, fake_server
):
    """Regression: an HA entity backed by a load the firmware stops
    reporting (load deleted on the device side, or the whole device
    factory-reset between sessions) is removed from BOTH the
    device_registry AND the entity_registry on the next reload.

    Flow exercised:
      1. Setup with L0 present  → entity-registry row + device-registry row created.
      2. Firmware drops L0 (simulated by removing it from fake_server.state).
      3. `async_reload(entry)` re-runs setup; `list_nodes` no longer mentions L0.
      4. `_async_prune_stale_devices` removes L0's device-registry row.
      5. HA's entity_registry listens for device removals and cascades —
         L0's entity-registry row is removed automatically.
    """
    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 500,
             "name": "Kitchen Light", "tag": "1A"},
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

    dev_registry = dr.async_get(hass)
    ent_registry = er.async_get(hass)

    # Sanity — L0 is in both registries after initial setup.
    initial_device = dev_registry.async_get_device(identifiers={(DOMAIN, L0)})
    assert initial_device is not None, (
        "expected device_registry row for L0 after initial setup"
    )
    initial_entities = [
        e for e in ent_registry.entities.values()
        if e.config_entry_id == entry.entry_id and e.device_id == initial_device.id
    ]
    assert initial_entities, (
        "expected at least one entity_registry row linked to L0 device card"
    )

    # Firmware no longer reports L0 — load was deleted device-side.
    del fake_server.state[L0]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # After reload: device_registry row for L0 is gone (pruned)…
    assert dev_registry.async_get_device(identifiers={(DOMAIN, L0)}) is None, (
        "device_registry row for L0 should be pruned after reload"
    )
    # …and so are the entity_registry rows that pointed at it (cascade).
    orphaned = [
        e for e in ent_registry.entities.values()
        if e.config_entry_id == entry.entry_id and e.device_id == initial_device.id
    ]
    assert orphaned == [], (
        f"entity_registry rows survived device removal: {orphaned}"
    )

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reload_rehydrates_existing_entity_registry_rows_for_same_unique_ids(
    hass, enable_custom_integrations, fake_server
):
    """Regression: when a reload (or a same-domain code swap) re-creates
    entities that report the same `unique_id`, HA's entity_registry
    rehydrates the **existing** row rather than minting a new one.

    This is the contract that makes user customizations (custom name,
    area, enabled/disabled) survive both:
      - vanilla `hass.config_entries.async_reload(entry_id)`, and
      - swapping the integration codebase out for a different one
        registered under the same DOMAIN.

    Verified by user-customizing the entity between setup and reload
    and asserting `entity_id` + the user's custom `name` (the
    user-set-name field is the one preserved on rehydrate; the
    integration-supplied `original_name` is updated freely)."""
    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 500,
             "name": "Kitchen Light", "tag": "1A"},
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

    ent_registry = er.async_get(hass)
    matching = [
        e for e in ent_registry.entities.values()
        if e.config_entry_id == entry.entry_id and e.unique_id == L0
    ]
    assert len(matching) == 1, (
        f"expected exactly one entity_registry row for L0; got {matching}"
    )
    original_row = matching[0]
    original_entity_id = original_row.entity_id
    original_registry_id = original_row.id  # internal UUID

    # User customizes: override the name. This is the field HA
    # preserves across rehydrate (unlike `original_name`, which the
    # integration is allowed to overwrite on every refresh).
    ent_registry.async_update_entity(original_entity_id, name="My Custom Name")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    rehydrated = [
        e for e in ent_registry.entities.values()
        if e.config_entry_id == entry.entry_id and e.unique_id == L0
    ]
    assert len(rehydrated) == 1, (
        f"reload should rehydrate, not duplicate; got {rehydrated}"
    )
    row = rehydrated[0]

    # entity_id and internal registry-row id unchanged → same row, not
    # a freshly-minted one.
    assert row.entity_id == original_entity_id
    assert row.id == original_registry_id
    # User customization survived rehydrate.
    assert row.name == "My Custom Name"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_stale_device_registry_preserves_gateway(
    hass, enable_custom_integrations, fake_server
):
    """The gateway device entry must never be pruned, even if no loads
    are configured (e.g., a freshly factory-reset Tago)."""
    fake_server.seed({L0: {"type": "UNUSED"}})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    # Pre-register the gateway device.
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "TAGO_TEST_001")},
        manufacturer="TAGO",
        name="Gateway",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Gateway is still in the registry — it was preserved even though
    # no real loads are configured.
    assert registry.async_get_device(
        identifiers={(DOMAIN, "TAGO_TEST_001")}
    ) is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
