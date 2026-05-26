"""Tests for the `repair-issues` and `discovery-update-info` Gold rules.

`repair-issues`: when the device's firmware is below `MIN_FIRMWARE_VERSION`,
the integration raises an issue in HA's Issue Registry (visible in
Settings → Repairs). The issue is cleared when firmware is updated and
the integration is reloaded, and when the entry is unloaded.

`discovery-update-info`: zeroconf rediscovery for an already-configured
device updates the stored hostname/port — so the entry heals if the
device moves networks.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tago import (
    _async_check_firmware_repair,
    _parse_version,
)
from custom_components.tago.const import (
    CONF_HOSTSTR,
    CONF_PIN,
    DOMAIN,
    MIN_FIRMWARE_VERSION,
)
from custom_components.tago.TagoNet import TagoDevice

L0 = "TAGO_TEST_001L1_0"


# =====================================================================
# _parse_version helper
# =====================================================================

def test_parse_version_valid_dotted_decimal():
    assert _parse_version("1.2.3") == (1, 2, 3)
    assert _parse_version("0.0.1") == (0, 0, 1)
    assert _parse_version("10") == (10,)


def test_parse_version_returns_none_on_garbage():
    assert _parse_version(None) is None
    assert _parse_version("") is None
    assert _parse_version("alpha-build") is None
    assert _parse_version("1.x.0") is None


# =====================================================================
# repair-issue: firmware too old
# =====================================================================

@pytest.mark.asyncio
async def test_repair_issue_created_when_firmware_below_minimum(
    hass, enable_custom_integrations, fake_server
):
    """Patch the fake firmware's get_config to report a low firmware
    version; integration setup should raise the issue."""
    # Patch fake_server's get_config response to return an old firmware.
    original_dispatch = fake_server._dispatch

    async def _patched_dispatch(ws, frame):
        if frame.get("req") == "get_config" and frame.get("dst") in (
            None, "TAGO_TEST_001",
        ):
            import json
            ref = frame.get("ref")
            await ws.send(json.dumps({
                "rsp": "get_config", "src": "TAGO_TEST_001",
                "firmware_rev": "0.9.0", "model_num": "dimac8",
                "serial_number": "TAGO_TEST_001",
                "api_key": "x" * 32, "loads": ["TAGO_TEST_001L1"],
                **({"ref": ref} if ref else {}),
            }))
            fake_server.sent.append({"firmware_rev": "0.9.0"})
            return
        await original_dispatch(ws, frame)

    fake_server._dispatch = _patched_dispatch

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

    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, f"firmware_too_old_{entry.entry_id}")
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.translation_placeholders["current"] == "0.9.0"
    assert issue.translation_placeholders["minimum"] == MIN_FIRMWARE_VERSION

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Unload should clear the issue.
    assert issues.async_get_issue(
        DOMAIN, f"firmware_too_old_{entry.entry_id}"
    ) is None


@pytest.mark.asyncio
async def test_repair_issue_not_created_when_firmware_meets_minimum(
    hass, enable_custom_integrations, fake_server
):
    """Default fake server returns firmware_rev='1.0.0' which equals
    MIN_FIRMWARE_VERSION ('1.0.0') — no issue should be raised."""
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

    issues = ir.async_get(hass)
    assert issues.async_get_issue(
        DOMAIN, f"firmware_too_old_{entry.entry_id}"
    ) is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_firmware_check_unit_clears_stale_issue_when_now_compliant(
    hass, enable_custom_integrations
):
    """Direct unit test on `_async_check_firmware_repair`: pre-create an
    issue, then run the check with a now-compliant version → issue is
    deleted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: "1.2.3.4", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)
    issues = ir.async_get(hass)

    # Pre-create a stale issue.
    ir.async_create_issue(
        hass, DOMAIN, f"firmware_too_old_{entry.entry_id}",
        is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key="firmware_too_old",
        translation_placeholders={"current": "0.9.0", "minimum": "1.0.0",
                                  "serial": "TAGO_TEST_001"},
    )
    assert issues.async_get_issue(
        DOMAIN, f"firmware_too_old_{entry.entry_id}"
    ) is not None

    # Build a device with a compliant firmware and run the check.
    device = TagoDevice("dummy:1", authkey="")
    device._firmware_rev = "1.5.0"
    device._serialnum = "TAGO_TEST_001"
    _async_check_firmware_repair(hass, entry, device)

    assert issues.async_get_issue(
        DOMAIN, f"firmware_too_old_{entry.entry_id}"
    ) is None


@pytest.mark.asyncio
async def test_firmware_check_skips_when_firmware_rev_is_none(
    hass, enable_custom_integrations
):
    """If firmware_rev is None (get_config response hasn't arrived) the
    check should NOT raise an issue — we can't compare what we don't know."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: "1.2.3.4", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    device = TagoDevice("dummy:1", authkey="")
    # firmware_rev is None by default.
    _async_check_firmware_repair(hass, entry, device)

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"firmware_too_old_{entry.entry_id}"
    ) is None


@pytest.mark.asyncio
async def test_firmware_check_skips_on_unparseable_version(
    hass, enable_custom_integrations
):
    """A weird firmware version string shouldn't crash the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: "1.2.3.4", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    device = TagoDevice("dummy:1", authkey="")
    device._firmware_rev = "build-abc-xyz"
    _async_check_firmware_repair(hass, entry, device)

    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"firmware_too_old_{entry.entry_id}"
    ) is None


# =====================================================================
# discovery-update-info: zeroconf updates an existing entry's host
# =====================================================================

@pytest.mark.asyncio
async def test_zeroconf_rediscovery_updates_existing_entry_hostname(
    hass, enable_custom_integrations
):
    """A zeroconf advertisement for an already-configured device must
    update the stored hostname (Gold rule `discovery-update-info`)."""
    from ipaddress import IPv4Address
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    # Pre-existing entry with an old hostname.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: "tago-old.local:80", CONF_PIN: ""},
        unique_id="SN-DISCOVER",
        version=9,
    )
    entry.add_to_hass(hass)

    # Build a zeroconf payload pointing at the same device but with a
    # new hostname/port (e.g., device moved to TLS on 443).
    info = ZeroconfServiceInfo(
        ip_address=IPv4Address("192.168.1.50"),
        ip_addresses=[IPv4Address("192.168.1.50")],
        hostname="tago-new.local.",
        port=443,
        type="_tagodev._tcp.local.",
        name="tago-SN-DISCOVER._tagodev._tcp.local.",
        properties={"serialnum": "SN-DISCOVER"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=info,
    )

    # Flow should abort because the unique_id is already configured —
    # the abort kwarg `updates=` updates the entry data.
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"

    # Entry's hostname should now reflect the new advertisement.
    assert entry.data[CONF_HOSTSTR] == "tago-new.local:443"


@pytest.mark.asyncio
async def test_zeroconf_first_discovery_proceeds_to_confirm(
    hass, enable_custom_integrations
):
    """When no entry exists yet, zeroconf flow continues to
    `zeroconf_confirm` (unchanged behaviour) — verifies the new
    `updates=` kwarg didn't break the new-device path."""
    from ipaddress import IPv4Address
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    info = ZeroconfServiceInfo(
        ip_address=IPv4Address("192.168.1.51"),
        ip_addresses=[IPv4Address("192.168.1.51")],
        hostname="tago-fresh.local.",
        port=443,
        type="_tagodev._tcp.local.",
        name="tago-SN-NEW._tagodev._tcp.local.",
        properties={"serialnum": "SN-NEW"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=info,
    )

    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"
