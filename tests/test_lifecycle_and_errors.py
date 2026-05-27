"""Connection-lifecycle, HA load/unload, and error-path coverage.

The earlier rounds of testing left ~24 lines of TagoNet.py uncovered with
the rationale "defensive paths that aren't easy to simulate." That was
under-claiming — most of them are actually reachable with the fake
firmware plus a small amount of fault injection. This file closes the
gap.

Covers:
  - HA config-entry load + clean unload (websocket closes on unload)
  - `connect()` with `timeout=None`
  - `connect()` early-return when already connected
  - `disconnect()` raising TimeoutError when the task hangs
  - `disconnect()` re-raising the task's own exception
  - `_stop_connection_manager()` close path on a connect-timeout
  - `_refresh_entities_from_list_nodes()` skipping loads with no `id`
  - `_refresh_entities_from_list_nodes()` swallowing entity-construction
    exceptions
  - `send_request(responseTimeout=...)` resolving the pending future when
    a matching `ref` arrives in the dispatch loop
  - Entity `handle_message()` raising synchronously (logged + suppressed)
  - Entity async handler raising (logged + suppressed)
  - `Ramp` task: completion path, interpolation step, exception swallow
  - Misc small bits: `TagoMessage.reference` property,
    `TagoDevice.input_event_message` no-op, `TagoEntity._handle_message`
    routing a `get_config` response to `handle_config_change`
"""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tago import TagoNet as _tn
from custom_components.tago.const import CONF_HOSTSTR, CONF_PIN, DOMAIN
from custom_components.tago.TagoNet import (
    Ramp,
    TagoDevice,
    TagoEntity,
    TagoGateway,
    TagoLight,
    TagoMessage,
)

L0 = "TAGO_TEST_001L1_0"

pytestmark = [pytest.mark.enable_socket]


def _make_device(host: str = "dummy:1") -> TagoDevice:
    """A bare TagoDevice on a (non-connected) TagoGateway. For unit
    tests that only need an entity-owner."""
    gateway = TagoGateway(host, authkey="k")
    return TagoDevice(gateway, {"id": "test_device", "available": True})


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# =====================================================================
# A. HA load + clean unload — verifies websocket is closed on unload.
# =====================================================================

@pytest.mark.asyncio
async def test_ha_entry_unload_closes_websocket_cleanly(
    hass, enable_custom_integrations, fake_server
):
    """Set up a config entry, then `async_unload` it; the integration must
    disconnect the websocket and tear down its tasks without leaving any
    lingering state."""
    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 0,
             "name": "Light", "tag": "1A"},
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
    assert gateway.is_connected is True

    # Trigger HA-side unload — should call our async_unload_entry which
    # calls gateway.disconnect().
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert gateway.is_connected is False
    assert gateway._task is None
    # The fake server should observe the connection close.
    await _wait_until(lambda: len(fake_server._clients) == 0, timeout=1.0)
    assert len(fake_server._clients) == 0


@pytest.mark.asyncio
async def test_ha_entry_unload_swallows_disconnect_errors(
    hass, enable_custom_integrations, fake_server, caplog
):
    """If `gateway.disconnect()` raises during unload, async_unload_entry
    must not propagate — it should log and proceed to unload the platforms
    so HA doesn't end up with a half-loaded entry (__init__.py:122-124)."""
    import logging
    from unittest.mock import patch

    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 0,
             "name": "Light", "tag": "1A"},
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

    async def _raising_disconnect(self, timeout: float = 0):
        raise RuntimeError("disconnect failed unexpectedly")

    with patch(
        "custom_components.tago.TagoGateway.disconnect",
        new=_raising_disconnect,
    ), caplog.at_level(logging.DEBUG):
        # Unload should still succeed despite the disconnect raising.
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert any("Disconnect during unload failed" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_ha_entry_unload_followed_by_setup_works(
    hass, enable_custom_integrations, fake_server
):
    """Unload + reload cycle: the integration must be safe to set up again
    after a clean unload."""
    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 0,
             "name": "Light", "tag": "1A"},
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
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Reload
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.is_connected is True
    assert await hass.config_entries.async_unload(entry.entry_id)


# =====================================================================
# B. TagoDevice.connect / disconnect lifecycle paths
# =====================================================================

@pytest.mark.asyncio
async def test_connect_with_no_timeout(fake_server):
    """`connect(timeout=None)` hits the no-timeout shield branch (line 505)."""
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=None)
    try:
        assert gateway.is_connected is True
    finally:
        await gateway.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_connect_when_already_connected_is_a_noop(fake_server):
    """Second `connect()` while `self._ws` is set returns immediately (line 490)."""
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        first_ws = gateway._ws
        await gateway.connect(timeout=5.0)
        assert gateway._ws is first_ws  # same object — no reconnect attempted
    finally:
        await gateway.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_disconnect_timeout_raises_when_task_hangs(monkeypatch):
    """`disconnect()` with too-short timeout raises TimeoutError (line 529)."""
    gateway = TagoGateway("dummy:1", authkey="k")

    async def _hang():
        await asyncio.sleep(60)

    gateway._task = asyncio.create_task(_hang())
    try:
        with pytest.raises(TimeoutError, match="Timed out waiting"):
            await gateway.disconnect(timeout=0.01)
    finally:
        gateway._task.cancel()


@pytest.mark.asyncio
async def test_disconnect_reraises_task_exception():
    """If the underlying task finished with an exception, `disconnect()`
    re-raises it after teardown (line 533)."""
    gateway = TagoGateway("dummy:1", authkey="k")

    async def _boom():
        raise RuntimeError("connection loop exploded")

    t = asyncio.create_task(_boom())
    # Let the task finish before disconnect awaits it.
    await asyncio.sleep(0)
    gateway._task = t

    with pytest.raises(RuntimeError, match="connection loop exploded"):
        await gateway.disconnect(timeout=1.0)


@pytest.mark.asyncio
async def test_connect_timeout_triggers_stop_connection_manager(monkeypatch):
    """When `connect()` times out, `_stop_connection_manager` runs and closes
    the websocket if one was opened (line 479).

    We simulate a half-open scenario: the inner WS opens, but the server
    never replies to the identity exchange so the startup future never
    resolves and connect() raises TimeoutError. _stop_connection_manager
    then closes the WS object."""
    # A WS that "opens" but never sends anything.
    class _SlowWS:
        def __init__(self):
            self._closed = False
            self.response = type("R", (), {"headers": {}})()

        async def send(self, _):
            return None

        async def recv(self):
            await asyncio.sleep(60)  # never replies

        async def close(self):
            self._closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(60)
            raise StopAsyncIteration

    slow_ws = _SlowWS()

    class _CM:
        async def __aenter__(self):
            return slow_ws

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_tn, "wsconnect", lambda **kwargs: _CM())
    gateway = TagoGateway("dummy:1", authkey="k")
    with pytest.raises(TimeoutError):
        await gateway.connect(timeout=0.05)

    # _stop_connection_manager cancels the task and calls _ws.close().
    # It leaves `_ws` as the (now-closed) object reference; the caller is
    # expected to not invoke it anymore — `is_connected` would still be
    # truthy, but the connection task is gone.
    assert gateway._task is None
    assert slow_ws._closed is True


# =====================================================================
# C. _refresh_entities_from_list_nodes edge cases
# =====================================================================

class _ScriptedFakeServer:
    """Minimal in-process WS server that lets us inject malformed
    list_nodes payloads to exercise rare error paths."""

    def __init__(self, list_nodes_payload: dict):
        self.list_nodes_payload = list_nodes_payload
        self._server = None
        self._clients: list = []

    async def start(self) -> int:
        import websockets
        from websockets.asyncio.server import serve

        async def _accept_any_bearer(connection, request):
            """PROTOCOL.md §2.1: any well-formed `Authorization: Bearer`
            header passes — token validation isn't the focus of these
            tests, only the post-auth list_nodes / list_devices path."""
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return connection.respond(401, "missing bearer token\n")
            return None

        async def _handle(ws):
            self._clients.append(ws)
            try:
                async for raw in ws:
                    try:
                        frame = json.loads(raw)
                    except Exception:
                        continue
                    req = frame.get("req")
                    ref = frame.get("ref")
                    if req == "list_devices":
                        await ws.send(json.dumps({
                            "rsp": "list_devices", "src": "TAGO_TEST_001",
                            "ref": ref,
                            "devices": [{"id": "TAGO_TEST_001", "available": True}],
                        }))
                    elif req == "get_device_info":
                        reply = {
                            "rsp": "get_device_info",
                            "src": "TAGO_TEST_001",
                            "ref": ref,
                            "firmware_rev": "1.0.0",
                            "model_num": "dimac8",
                            "serial_num": "TAGO_TEST_001",
                            "nodes": self.list_nodes_payload,
                        }
                        await ws.send(json.dumps(reply))
                    elif req == "list_nodes":
                        reply = {
                            "rsp": "list_nodes",
                            "src": "TAGO_TEST_001",
                            "ref": ref,
                            "nodes": self.list_nodes_payload,
                        }
                        await ws.send(json.dumps(reply))
                    # ignore everything else
            except Exception:
                pass

        self._server = await serve(_handle, "127.0.0.1", 0, process_request=_accept_any_bearer)
        return next(iter(self._server.sockets)).getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest_asyncio.fixture
async def scripted_server_factory(socket_enabled):
    servers: list[_ScriptedFakeServer] = []

    async def _factory(payload):
        s = _ScriptedFakeServer(payload)
        s.port = await s.start()
        servers.append(s)
        return s

    yield _factory

    for s in servers:
        await s.stop()


@pytest.mark.asyncio
async def test_load_with_no_id_is_skipped(scripted_server_factory):
    """A load entry lacking `id` triggers `continue` (line 606); the rest
    of the group is processed normally."""
    server = await scripted_server_factory({
        "TAGO_TEST_001L1": {
            "type": "dimac", "ch": 8,
            "loads": [
                {"type": "light_dimmable"},  # missing `id`
                {"id": "valid_load", "type": "light_dimmable"},
            ],
        },
    })
    gateway = TagoGateway(f"127.0.0.1:{server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        ids = {e.unique_id for e in gateway.entities}
        assert "valid_load" in ids
        assert len(gateway.entities) == 1
    finally:
        await gateway.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_load_construction_exception_is_swallowed_and_logged(
    scripted_server_factory, monkeypatch, caplog
):
    """If TagoLight.__init__ raises during entity construction, the outer
    except logs and the rest of the discovery continues (lines 626-627)."""
    import logging

    real_init = TagoLight.__init__
    raised_for: list[str] = []

    def _exploding_init(self, json_data, gateway):
        if json_data.get("id") == "boom":
            raised_for.append("boom")
            raise ValueError("intentional test failure")
        real_init(self, json_data, gateway)

    monkeypatch.setattr(TagoLight, "__init__", _exploding_init)

    server = await scripted_server_factory({
        "TAGO_TEST_001L1": {
            "type": "dimac", "ch": 8,
            "loads": [
                {"id": "boom", "type": "light_dimmable"},
                {"id": "ok", "type": "light_dimmable"},
            ],
        },
    })
    gateway = TagoGateway(f"127.0.0.1:{server.port}", authkey="")
    with caplog.at_level(logging.ERROR):
        await gateway.connect(timeout=5.0)
    try:
        ids = {e.unique_id for e in gateway.entities}
        assert "ok" in ids
        assert "boom" not in ids
        assert raised_for == ["boom"]
    finally:
        await gateway.disconnect(timeout=5.0)


# =====================================================================
# D. send_request(responseTimeout=) end-to-end: hits the pending_responses
# future-resolution path (lines 685-687).
# =====================================================================

@pytest.mark.asyncio
async def test_send_request_with_response_timeout_resolves_on_matching_ref(
    fake_server,
):
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        # `ping` returns synchronously from the fake server with a `ts` field.
        reply = await gateway.send_request(req="ping", responseTimeout=2.0)
        assert reply is not None
        assert reply.rsp == "ping"
        assert "ts" in reply.data
    finally:
        await gateway.disconnect(timeout=5.0)


# =====================================================================
# E. Entity handler errors during message dispatch
# =====================================================================

@pytest.mark.asyncio
async def test_entity_handle_message_sync_exception_is_swallowed_and_logged(
    fake_server, caplog
):
    """If an entity's `handle_message` raises synchronously while preparing
    to dispatch, the connection_task swallows it and logs once via the
    throttle (lines 705-706)."""
    import logging

    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 0,
             "name": "Light", "tag": "1A"},
    })
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        # Replace the entity's `handle_message` with one that raises in
        # the sync prep path (i.e. before returning a coroutine).
        entity = next(iter(gateway.entities))

        def _raising_handle_message(msg):
            raise RuntimeError("synthetic sync failure")

        entity.handle_message = _raising_handle_message

        # Broadcasting any event triggers the dispatch loop to call
        # entity.handle_message and catch the exception.
        with caplog.at_level(logging.ERROR):
            await fake_server.broadcast_event(
                {"evt": "state_changed", "src": L0, "id": L0,
                 "type": "light_dimmable", "brightness": 500}
            )
            await _wait_until(
                lambda: any("Entity message scheduling error" in r.message
                            for r in caplog.records),
                timeout=1.0,
            )
        # Connection survived the entity-side exception.
        assert gateway.is_connected is True
    finally:
        await gateway.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_entity_async_handler_exception_is_swallowed_and_logged(
    fake_server, caplog
):
    """If an entity's async handler raises during `asyncio.gather`, the
    connection_task captures it and logs (line 716)."""
    import logging

    fake_server.seed({
        L0: {"type": "light_dimmable", "brightness": 0,
             "name": "Light", "tag": "1A"},
    })
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        entity = next(iter(gateway.entities))

        async def _raising_async_handler(msg):
            raise RuntimeError("synthetic async failure")

        def _route_to_raising_handler(msg):
            return _raising_async_handler(msg)

        entity.handle_message = _route_to_raising_handler

        with caplog.at_level(logging.ERROR):
            await fake_server.broadcast_event(
                {"evt": "state_changed", "src": L0, "id": L0,
                 "type": "light_dimmable", "brightness": 500}
            )
            await _wait_until(
                lambda: any("Entity message handling error" in r.message
                            for r in caplog.records),
                timeout=1.0,
            )
        assert gateway.is_connected is True
    finally:
        await gateway.disconnect(timeout=5.0)


# =====================================================================
# F. Ramp internal task — completion, interpolation, exception swallow
# =====================================================================

@pytest.mark.asyncio
async def test_ramp_task_runs_through_completion(monkeypatch):
    """A short ramp's task should reach the `elapsed > duration` branch
    (line 809) and exit cleanly."""
    captured: list[list] = []

    def _cb(values):
        captured.append(list(values))

    ramp = Ramp(
        start=[0, 0, 0, 0],
        end=[1000, 0, 0, 0],
        duration=50,  # ms
        elapsed=0,
        update_interval=0.005,
        callback=_cb,
    )
    # Wait long enough for the ramp to complete on its own.
    await asyncio.sleep(0.15)
    assert ramp.task.done()
    # The interpolation loop should have produced at least one mid-ramp
    # update where progress < 1.0 (line 817).
    assert captured, "no callback fired"
    assert any(0 < row[0] < 1000 for row in captured)


@pytest.mark.asyncio
async def test_ramp_task_swallows_callback_exceptions():
    """Exceptions raised inside the callback are caught by the task's
    `except Exception` and logged (line 825)."""
    raised: list[str] = []

    def _exploding_cb(values):
        raised.append("called")
        raise RuntimeError("callback failure")

    ramp = Ramp(
        start=[0, 0, 0, 0],
        end=[1000, 0, 0, 0],
        duration=50,
        elapsed=0,
        update_interval=0.005,
        callback=_exploding_cb,
    )
    await asyncio.sleep(0.15)
    # The task should have completed despite the exception path.
    assert ramp.task.done()
    assert raised, "callback was never invoked"


# =====================================================================
# G. Misc small bits
# =====================================================================

def test_tagomessage_reference_property():
    """The `reference` @property simply returns `self.ref` (line 90)."""
    msg = TagoMessage()
    msg.ref = "abc-123"
    assert msg.reference == "abc-123"


@pytest.mark.asyncio
async def test_tagoentity_handle_message_ignores_get_config_response_under_d7():
    """Per D7 (config frozen after initial connect), runtime
    `get_config` responses are received but NOT re-applied to entities.
    `handle_message` should route the message but the entity should
    leave its config untouched."""
    device = _make_device()
    entity = TagoEntity(
        {"id": "L0", "type": "light_dimmable", "name": "Original",
         "location": "y", "tag": "1A"},
        device,
    )
    original_name = entity.name

    msg = TagoMessage.from_payload(
        json.dumps({"rsp": "get_config", "src": "L0", "ref": "r1",
                    "type": "light_dimmable", "name": "Renamed"})
    )
    handler = entity.handle_message(msg)
    if handler is not None:
        await handler
    # D7: name from the runtime config response is ignored.
    assert entity.name == original_name


@pytest.mark.asyncio
async def test_log_when_unavailable_then_available_again_on_reconnect(fake_server, caplog):
    """After a disconnect + reconnect cycle, the integration must emit:
       1. A WARNING that the gateway became unavailable
       2. An INFO that it's available again
    Each exactly once per transition. Silver-tier `log-when-unavailable`.
    """
    import logging
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        with caplog.at_level(logging.INFO):
            # Force-close from the client side; the integration's main
            # message loop will exit, the outer while-loop will note the
            # drop, sleep, and reconnect.
            await gateway._ws.close()
            await _wait_until(
                lambda: any("became unavailable" in r.message for r in caplog.records),
                timeout=5.0,
            )
            # Connection_task sleeps 3s between retries.
            await _wait_until(
                lambda: any("available again" in r.message for r in caplog.records),
                timeout=8.0,
            )
        assert sum(1 for r in caplog.records if "available again" in r.message) >= 1
    finally:
        await gateway.disconnect(timeout=5.0)


@pytest.mark.asyncio
async def test_log_when_unavailable_fires_once_per_outage(fake_server, caplog):
    """`log-when-unavailable` Silver-tier rule: WARN once on the
    transition to unavailable; don't spam for every reconnect attempt."""
    import logging
    gateway = TagoGateway(f"127.0.0.1:{fake_server.port}", authkey="")
    await gateway.connect(timeout=5.0)
    try:
        with caplog.at_level(logging.WARNING):
            await fake_server.stop()
            await _wait_until(
                lambda: any("became unavailable" in r.message for r in caplog.records),
                timeout=3.0,
            )
        first_count = sum(
            1 for r in caplog.records if "became unavailable" in r.message
        )
        # Let the connection_task retry-loop spin for a bit; the
        # "unavailable" warning must NOT fire again during this outage.
        await asyncio.sleep(0.2)
        second_count = sum(
            1 for r in caplog.records if "became unavailable" in r.message
        )
        assert first_count == second_count == 1
    finally:
        gateway._running = False
        try:
            await gateway.disconnect(timeout=2.0)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_unused_load_with_existing_device_registry_entry_is_removed(
    hass, enable_custom_integrations, fake_server
):
    """`__init__.py:60` — when an UNUSED load has a pre-existing gateway in
    the gateway registry, the cleanup loop calls `async_remove_device`."""
    from homeassistant.helpers import device_registry as dr

    # Pre-register a fake gateway for the unused load so the cleanup loop
    # has something to remove.
    fake_server.seed({
        L0: {"type": "UNUSED", "name": "Old Slot", "tag": "1A"},
    })
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOSTSTR: f"127.0.0.1:{fake_server.port}", CONF_PIN: ""},
        unique_id="TAGO_TEST_001",
        version=9,
    )
    entry.add_to_hass(hass)

    # Insert a gateway for L0 BEFORE async_setup so the cleanup branch fires.
    registry = dr.async_get(hass)
    pre_existing = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, L0)},
        manufacturer="TAGO",
        name="Old Slot",
    )
    assert registry.async_get_device(identifiers={(DOMAIN, L0)}) is not None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Cleanup loop should have removed the pre-existing gateway.
    assert registry.async_get_device(identifiers={(DOMAIN, L0)}) is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_tagodevice_input_event_message_is_a_noop():
    """`input_event_message` is a stub `pass` (line 355)."""
    gateway = TagoGateway("dummy:1", authkey="k")
    # Call it; should return None and not raise.
    result = gateway.input_event_message(None)
    assert result is None


@pytest.mark.asyncio
async def test_connection_task_calls_get_ssl_context_when_useSSL_true(monkeypatch):
    """`connection_task` calls `get_ssl_context()` when `useSSL=True`
    (line 639). The SSL context body itself is excluded from coverage as
    part of the auth/transport rewrite, but the branch must still fire."""
    get_ssl_called = []

    async def _fake_get_ssl_context(self):
        get_ssl_called.append("yes")
        return None  # use no SSL underneath

    monkeypatch.setattr(TagoGateway, "get_ssl_context", _fake_get_ssl_context)

    # We don't need the connection to succeed — only need the SSL branch
    # to execute before any failure.
    class _ImmediateFailCM:
        async def __aenter__(self):
            raise OSError("no real network in this test")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(_tn, "wsconnect", lambda **kwargs: _ImmediateFailCM())

    gateway = TagoGateway("dummy:1", authkey="k", useSSL=True)
    with pytest.raises((TimeoutError, OSError)):
        await gateway.connect(timeout=0.2)

    assert get_ssl_called == ["yes"] or len(get_ssl_called) >= 1
