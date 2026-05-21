from __future__ import annotations

from dataclasses import dataclass

import pytest

from custom_components.tago.config_flow import TagoConfigFlowHandler
from custom_components.tago.const import CONF_AUTHKEY, CONF_HOSTSTR


@dataclass
class _DiscoveryInfo:
    hostname: str
    port: int
    properties: dict[str, str]


@pytest.mark.asyncio
async def test_zeroconf_discovery_populates_host_and_name() -> None:
    flow = TagoConfigFlowHandler()
    flow.context = {"source": "zeroconf"}

    seen_unique_ids: list[str] = []

    async def _set_unique_id(value: str):
        seen_unique_ids.append(value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda: None  # type: ignore[method-assign]

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
    class FakeDevice:
        def __init__(self, host: str, authkey: str):
            self.host = host
            self.authkey = authkey

        async def connect(self, timeout: float = 0) -> None:
            raise OSError("bad host")

        async def disconnect(self, timeout: float = 0) -> None:
            return None

    monkeypatch.setattr("custom_components.tago.config_flow.TagoDevice", FakeDevice)

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}

    result = await flow.async_step_user(
        {CONF_HOSTSTR: "http://invalid-host", CONF_AUTHKEY: "abc"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_user_flow_invalid_auth_returns_invalid_auth(monkeypatch) -> None:
    class FakeDevice:
        def __init__(self, host: str, authkey: str):
            self.host = host
            self.authkey = authkey

        async def connect(self, timeout: float = 0) -> None:
            raise PermissionError("bad auth")

        async def disconnect(self, timeout: float = 0) -> None:
            return None

    monkeypatch.setattr("custom_components.tago.config_flow.TagoDevice", FakeDevice)

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}

    result = await flow.async_step_user({CONF_HOSTSTR: "dev.local:443"})

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_user_flow_success_creates_entry_and_uses_optional_auth(monkeypatch) -> None:
    class FakeDevice:
        def __init__(self, host: str, authkey: str):
            self.host = host
            self.authkey = authkey
            self.unique_id = "SN-1"
            self.serial_num = "SN-1"
            self.model_num = "M-1"

        async def connect(self, timeout: float = 0) -> None:
            return None

        async def disconnect(self, timeout: float = 0) -> None:
            return None

    monkeypatch.setattr("custom_components.tago.config_flow.TagoDevice", FakeDevice)

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}

    seen_unique_ids: list[str] = []

    async def _set_unique_id(value: str):
        seen_unique_ids.append(value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda: None  # type: ignore[method-assign]

    result = await flow.async_step_user({CONF_HOSTSTR: "dev.local:443"})

    assert seen_unique_ids == ["SN-1"]
    assert result["type"] == "create_entry"
    assert result["title"] == "M-1 SN-1"
    assert result["data"][CONF_HOSTSTR] == "dev.local:443"
    assert result["data"][CONF_AUTHKEY] == ""


@pytest.mark.asyncio
async def test_zeroconf_confirm_form_has_device_placeholder() -> None:
    flow = TagoConfigFlowHandler()
    flow.context = {"source": "zeroconf"}
    flow.device_name = "SN-ZC"

    result = await flow.async_step_zeroconf_confirm()

    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["device_name"] == "SN-ZC"


@pytest.mark.asyncio
async def test_connection_test_attempts_disconnect_after_connect_error(monkeypatch) -> None:
    calls: list[str] = []

    class FakeDevice:
        def __init__(self, host: str, authkey: str):
            self.host = host
            self.authkey = authkey

        async def connect(self, timeout: float = 0) -> None:
            calls.append("connect")
            raise OSError("boom")

        async def disconnect(self, timeout: float = 0) -> None:
            calls.append("disconnect")

    monkeypatch.setattr("custom_components.tago.config_flow.TagoDevice", FakeDevice)

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}
    result = await flow.async_step_user({CONF_HOSTSTR: "dev.local:443", CONF_AUTHKEY: "k"})

    assert result["errors"]["base"] == "cannot_connect"
    assert calls == ["connect", "disconnect"]


@pytest.mark.asyncio
async def test_connection_test_attempts_disconnect_after_success(monkeypatch) -> None:
    calls: list[str] = []

    class FakeDevice:
        def __init__(self, host: str, authkey: str):
            self.unique_id = "SN-2"
            self.serial_num = "SN-2"
            self.model_num = "M-2"

        async def connect(self, timeout: float = 0) -> None:
            calls.append("connect")

        async def disconnect(self, timeout: float = 0) -> None:
            calls.append("disconnect")

    monkeypatch.setattr("custom_components.tago.config_flow.TagoDevice", FakeDevice)

    flow = TagoConfigFlowHandler()
    flow.context = {"source": "user"}

    async def _set_unique_id(value: str):
        return None

    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda: None  # type: ignore[method-assign]

    result = await flow.async_step_user({CONF_HOSTSTR: "dev.local:443", CONF_AUTHKEY: "k"})

    assert result["type"] == "create_entry"
    assert calls == ["connect", "disconnect"]
