"""ConfigEntry migration (v8 → v9: `authkey` → `pin`) and reauth flow tests.

The next-major firmware release swaps `api_key` auth for PIN-based auth.
This release rebrands the credential field in the UI as "PIN", renames the
stored key from `authkey` to `pin` via a ConfigEntry migration, and wires
up a reauth flow so a user upgrading their firmware is prompted for the
new PIN when the saved api_key stops authenticating.

Tests cover:
  - Migration 8 → 9 renames `authkey` → `pin` and preserves the value
  - Migration when the entry already has `pin` is a no-op
  - `async_setup_entry` raises `ConfigEntryAuthFailed` when device.connect()
    raises PermissionError → triggers HA's reauth flow
  - `async_step_reauth` + `async_step_reauth_confirm` happy path
  - reauth_confirm with wrong PIN shows `invalid_auth` error
  - reauth_confirm with unreachable host shows `cannot_connect`
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tago.const import (
    CONF_AUTHKEY,
    CONF_HOSTSTR,
    CONF_PIN,
    DOMAIN,
)


# =====================================================================
# Migration 8 → 9
# =====================================================================

@pytest.mark.asyncio
async def test_migration_v8_to_v9_renames_authkey_to_pin(
    hass, enable_custom_integrations, fake_server
):
    """A v8 entry with `authkey="old-api-key"` migrates to v9 with
    `pin="old-api-key"` (value carried over verbatim, key renamed)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}",
              CONF_AUTHKEY: "old-api-key"},
        unique_id="TAGO_TEST_001",
        version=8,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 9
    assert CONF_PIN in entry.data
    assert entry.data[CONF_PIN] == "old-api-key"
    assert CONF_AUTHKEY not in entry.data

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_migration_v8_to_v9_empty_authkey_becomes_empty_pin(
    hass, enable_custom_integrations, fake_server
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_AUTHKEY: ""},
        unique_id="TAGO_TEST_001",
        version=8,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 9
    assert entry.data[CONF_PIN] == ""

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_migration_v8_with_no_authkey_field_sets_empty_pin(
    hass, enable_custom_integrations, fake_server
):
    """If somehow the v8 entry has neither `authkey` nor `pin`, migration
    fills in an empty `pin` rather than failing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}"},
        unique_id="TAGO_TEST_001",
        version=8,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data[CONF_PIN] == ""

    await hass.config_entries.async_unload(entry.entry_id)


# =====================================================================
# async_setup_entry surfaces PermissionError as ConfigEntryAuthFailed →
# HA starts the reauth flow.
# =====================================================================

@pytest.mark.asyncio
async def test_setup_entry_raises_auth_failed_when_device_connect_raises_permission_error(
    hass, enable_custom_integrations, fake_server, socket_enabled
):
    """If the stored credentials fail (PermissionError on connect), HA
    receives a ConfigEntryAuthFailed and starts the reauth flow."""
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "wrong"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.tago.TagoDevice.connect",
        side_effect=PermissionError("bad PIN"),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # async_setup returns False on auth-failed; HA marks the entry as
    # needing reauth and a reauth flow is started.
    assert result is False
    flows_in_progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"].get("source") == SOURCE_REAUTH for f in flows_in_progress)


# =====================================================================
# Reauth flow — happy path
# =====================================================================

@pytest.mark.asyncio
async def test_reauth_flow_updates_pin_and_reloads_entry(
    hass, enable_custom_integrations, fake_server
):
    """User completes reauth with the correct PIN → entry data is updated
    and the entry reloads successfully."""
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "stale"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    # Start a reauth flow manually (in production HA does this on
    # ConfigEntryAuthFailed).
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"

    # Submit the new PIN; the fake server accepts anything for PIN today.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PIN: "new-correct-pin"},
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PIN] == "new-correct-pin"


@pytest.mark.asyncio
async def test_reauth_flow_invalid_auth_shows_error(
    hass, enable_custom_integrations, fake_server, socket_enabled
):
    """If the new PIN also fails to authenticate, the form re-renders
    with `invalid_auth`."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "stale"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )

    with patch(
        "custom_components.tago.config_flow.TagoDevice.connect",
        side_effect=PermissionError("still bad"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIN: "still-wrong"},
        )

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"]["base"] == "invalid_auth"
    # Entry data unchanged.
    assert entry.data[CONF_PIN] == "stale"


@pytest.mark.asyncio
async def test_reauth_flow_aborts_if_entry_id_no_longer_exists(
    hass, enable_custom_integrations,
):
    """If HA fires a reauth for an entry_id that's been removed since the
    flow was queued, the flow aborts with `unknown` instead of crashing
    (config_flow.py:113)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": "this-entry-does-not-exist",
            "unique_id": "TAGO_TEST_001",
        },
        data={},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_reauth_disconnect_cleanup_exception_is_swallowed(
    hass, enable_custom_integrations, fake_server, socket_enabled, caplog
):
    """A disconnect() that raises during the reauth finally-block must not
    propagate — it logs at debug level (config_flow.py:137-138).

    We exercise the failure path (auth fails, so no entry reload happens)
    so the test doesn't have to manage the lifecycle of a successfully
    reloaded entry that would otherwise leak a task."""
    import logging

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "stale"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )

    async def _raising_disconnect(self, timeout: float = 0):
        raise RuntimeError("disconnect failed unexpectedly")

    with patch(
        "custom_components.tago.config_flow.TagoDevice.connect",
        side_effect=PermissionError("still bad"),
    ), patch(
        "custom_components.tago.config_flow.TagoDevice.disconnect",
        new=_raising_disconnect,
    ), caplog.at_level(logging.DEBUG):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIN: "still-wrong"},
        )

    # Auth failed → form re-renders, no reload triggered. The disconnect
    # exception in the finally-block was caught and logged.
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"
    assert any("Connection cleanup failed" in r.message for r in caplog.records)


# =====================================================================
# Reconfiguration flow — change hostname/PIN on an existing entry
# =====================================================================

@pytest.mark.asyncio
async def test_reconfigure_flow_updates_entry_and_reloads(
    hass, enable_custom_integrations, fake_server
):
    """User completes reconfigure with valid host + PIN → entry data is
    updated and the entry reloads against the new endpoint."""
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "old"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data=None,
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"

    # Submit with the same host (fake_server already running there) +
    # new PIN. Fake firmware accepts any PIN.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "new-pin"},
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PIN] == "new-pin"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reconfigure_flow_invalid_auth_shows_error(
    hass, enable_custom_integrations, fake_server, socket_enabled
):
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "old"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data=None,
    )

    with patch(
        "custom_components.tago.config_flow.TagoDevice.connect",
        side_effect=PermissionError("bad pin"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "wrong"},
        )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"
    # Original entry data not changed.
    assert entry.data[CONF_PIN] == "old"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reconfigure_flow_cannot_connect_shows_error(
    hass, enable_custom_integrations, fake_server, socket_enabled
):
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "p"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data=None,
    )

    with patch(
        "custom_components.tago.config_flow.TagoDevice.connect",
        side_effect=OSError("no route"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOSTSTR: "127.0.0.1:1", CONF_PIN: "p"},
        )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reconfigure_flow_wrong_device_aborts(
    hass, enable_custom_integrations, fake_server, monkeypatch
):
    """If the user points reconfigure at a different physical Tago (a
    different serial_num), abort — the entry's unique_id binds it to a
    specific device."""
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "p"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data=None,
    )

    # Patch TagoDevice so the test fake reports a different serial_num.
    from custom_components.tago.TagoNet import TagoDevice as _Real

    class _DifferentSerial(_Real):
        @property
        def serial_num(self):
            return "TAGO_DIFFERENT_001"

    monkeypatch.setattr(
        "custom_components.tago.config_flow.TagoDevice", _DifferentSerial,
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "p"},
    )

    assert result["type"] == "abort"
    assert result["reason"] == "wrong_device"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reconfigure_flow_swallows_disconnect_cleanup_exception(
    hass, enable_custom_integrations, fake_server, socket_enabled, caplog
):
    """An exception in the reconfigure finally-block (device.disconnect)
    must not propagate — log at debug and continue (config_flow.py:132-133)."""
    import logging

    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "p"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data=None,
    )

    async def _raising_disconnect(self, timeout: float = 0):
        raise RuntimeError("disconnect failed")

    # Combine with PermissionError on connect so the flow takes the
    # failure branch — that way no reload runs and we don't leak tasks.
    with patch(
        "custom_components.tago.config_flow.TagoDevice.connect",
        side_effect=PermissionError("bad"),
    ), patch(
        "custom_components.tago.config_flow.TagoDevice.disconnect",
        new=_raising_disconnect,
    ), caplog.at_level(logging.DEBUG):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "wrong"},
        )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"
    assert any("Connection cleanup failed" in r.message for r in caplog.records)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reconfigure_flow_aborts_if_entry_id_no_longer_exists(
    hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": "ghost-entry"},
        data=None,
    )
    assert result["type"] == "abort"
    assert result["reason"] == "unknown"


# =====================================================================
# Exception translations on async_setup_entry raises
# =====================================================================

@pytest.mark.asyncio
async def test_setup_entry_permission_error_has_translation_metadata(
    hass, enable_custom_integrations, fake_server, socket_enabled
):
    """async_setup_entry's PermissionError → ConfigEntryAuthFailed must
    carry translation_domain + translation_key per HA Gold-tier rule
    `exception-translations`."""
    from homeassistant.exceptions import ConfigEntryAuthFailed
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "stale"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    captured: list[Exception] = []

    from custom_components.tago import async_setup_entry as _setup_entry

    with patch(
        "custom_components.tago.TagoDevice.connect",
        side_effect=PermissionError("bad"),
    ):
        try:
            await _setup_entry(hass, entry)
        except ConfigEntryAuthFailed as err:
            captured.append(err)

    assert captured, "expected ConfigEntryAuthFailed"
    err = captured[0]
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "auth_failed"
    assert err.translation_placeholders == {"host": f"127.0.0.1:{fake_server.port}"}


@pytest.mark.asyncio
async def test_setup_entry_connection_error_has_translation_metadata(
    hass, enable_custom_integrations, fake_server, socket_enabled
):
    """async_setup_entry's OSError → ConfigEntryNotReady carries
    translation_key=cannot_connect."""
    from homeassistant.exceptions import ConfigEntryNotReady
    fake_server.seed({})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    captured: list[Exception] = []

    from custom_components.tago import async_setup_entry as _setup_entry

    with patch(
        "custom_components.tago.TagoDevice.connect",
        side_effect=OSError("network down"),
    ):
        try:
            await _setup_entry(hass, entry)
        except ConfigEntryNotReady as err:
            captured.append(err)

    assert captured
    err = captured[0]
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "cannot_connect"


@pytest.mark.asyncio
async def test_reauth_flow_cannot_connect_shows_error(
    hass, enable_custom_integrations, fake_server, socket_enabled
):
    """If the device is unreachable during reauth, the form re-renders
    with `cannot_connect`."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: "stale"},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )

    with patch(
        "custom_components.tago.config_flow.TagoDevice.connect",
        side_effect=OSError("no route to host"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIN: "doesnt-matter"},
        )

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"]["base"] == "cannot_connect"
