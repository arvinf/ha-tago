from __future__ import annotations

from dataclasses import dataclass

import pytest

from custom_components.tago.config_flow import TagoConfigFlowHandler
from custom_components.tago.const import CONF_HOSTSTR, CONF_PIN


@dataclass
class _DiscoveryInfo:
    hostname: str
    port: int
    properties: dict[str, str]


# Helpers ---------------------------------------------------------------

def _install_fake_gateway(monkeypatch, *, raises=None, capture: dict | None = None):
    """Patch `config_flow.TagoGateway` with a fake whose
    `connect_and_auth` either succeeds (returns None) or raises the
    given exception. The real integration only calls `connect_and_auth`
    in the config flow now (PROTOCOL.md §2.1 bearer auth) — there's
    no follow-up `disconnect()` to drive."""

    class FakeGateway:
        def __init__(self, host: str, authkey: str):
            if capture is not None:
                capture["host"] = host
                capture["authkey"] = authkey

        async def connect_and_auth(self, timeout: float | None = None) -> None:
            if raises is not None:
                raise raises

    monkeypatch.setattr("custom_components.tago.config_flow.TagoGateway", FakeGateway)


# Tests -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_zeroconf_discovery_populates_host_and_name() -> None:
    flow = TagoConfigFlowHandler()
    flow.context = {"source": "zeroconf"}

    seen_unique_ids: list[str] = []

    async def _set_unique_id(value: str):
        seen_unique_ids.append(value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda **kw: None  # type: ignore[method-assign]

    result = await flow.async_step_zeroconf(
        _DiscoveryInfo(
            hostname="tago-device.local.",
            port=443,
            properties={"serialnum": "SN-ABC"},
        )
    )

    assert flow.hoststr == "tago-device.local:443"
    assert flow.device_name == "SN-ABC"
    assert seen_unique_ids == ["SN-ABC"]
    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"


@pytest.mark.asyncio
async def test_user_flow_connection_failure_returns_cannot_connect(monkeypatch) -> None:
    _install_fake_gateway(monkeypatch, raises=OSError("bad host"))

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}

    result = await flow.async_step_user(
        {CONF_HOSTSTR: "http://invalid-host", CONF_PIN: "abc"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_user_flow_invalid_auth_returns_invalid_auth(monkeypatch) -> None:
    _install_fake_gateway(monkeypatch, raises=PermissionError("bad auth"))

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}

    result = await flow.async_step_user({CONF_HOSTSTR: "dev.local:443"})

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_user_flow_success_creates_entry_with_hoststr_as_unique_id(monkeypatch) -> None:
    """The probe-only config flow can't enumerate devices, so the
    entry's `unique_id` is the hoststr and the title carries the
    gateway endpoint rather than per-device identity."""
    _install_fake_gateway(monkeypatch)

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}

    seen_unique_ids: list[str] = []

    async def _set_unique_id(value: str):
        seen_unique_ids.append(value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda **kw: None  # type: ignore[method-assign]

    result = await flow.async_step_user({CONF_HOSTSTR: "dev.local:443"})

    assert seen_unique_ids == ["dev.local:443"]
    assert result["type"] == "create_entry"
    assert "dev.local:443" in result["title"]
    assert result["data"][CONF_HOSTSTR] == "dev.local:443"
    assert result["data"][CONF_PIN] == ""


@pytest.mark.asyncio
async def test_zeroconf_confirm_form_has_device_placeholder() -> None:
    flow = TagoConfigFlowHandler()
    flow.context = {"source": "zeroconf"}
    flow.device_name = "SN-ZC"

    result = await flow.async_step_zeroconf_confirm()

    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["device_name"] == "SN-ZC"


# =====================================================================
# Zeroconf-flow branches
# =====================================================================

@pytest.mark.asyncio
async def test_zeroconf_confirm_submission_proceeds_to_connection_test(monkeypatch) -> None:
    """async_step_zeroconf_confirm with user_input dispatches to test_connection."""
    captured: dict = {}
    _install_fake_gateway(monkeypatch, capture=captured)

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "zeroconf"}
    flow.hoststr = "tago-zc.local:443"
    flow.device_name = "SN-Z"

    async def _set_unique_id(value: str):
        return None

    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda **kw: None  # type: ignore[method-assign]

    result = await flow.async_step_zeroconf_confirm({CONF_PIN: "key123"})

    assert result["type"] == "create_entry"
    assert captured["authkey"] == "key123"
    assert captured["host"] == "tago-zc.local:443"


@pytest.mark.asyncio
async def test_zeroconf_flow_invalid_auth_returns_to_zeroconf_confirm(monkeypatch) -> None:
    _install_fake_gateway(monkeypatch, raises=PermissionError("bad auth"))

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "zeroconf"}
    flow.hoststr = "tago-zc.local:443"
    flow.device_name = "SN-Z"

    result = await flow.async_step_zeroconf_confirm({CONF_PIN: "wrong"})

    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_zeroconf_flow_connection_failure_returns_to_zeroconf_confirm(monkeypatch) -> None:
    _install_fake_gateway(monkeypatch, raises=OSError("connect refused"))

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "zeroconf"}
    flow.hoststr = "tago-zc.local:443"
    flow.device_name = "SN-Z"

    result = await flow.async_step_zeroconf_confirm({CONF_PIN: "k"})

    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"
    assert result["errors"]["base"] == "cannot_connect"
