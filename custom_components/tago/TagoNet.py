from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
import hashlib
import json
import logging
import math
import random
import ssl
import string
import time
import uuid

from websockets.asyncio.client import ClientConnection, connect as wsconnect

class TagoMessage:
    PROP_DST = 'dst'
    PROP_RSP = 'rsp'
    PROP_SRC = 'src'
    PROP_REQ = 'req'
    PROP_REF = 'ref'
    PROP_EVT = 'evt'

    @staticmethod
    def create_random_str(n: int = 6) -> str:
        return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))

    def __init__(self):
        self.rsp = None
        self.data = None
        self.src = None
        self.ref = None
        self.evt = None
        self.dst = None
        self.req = None

    @classmethod
    def from_payload(cls, message: str):
        self = cls()
        #print('>> ' + str(message))
        data = json.loads(message)
        self.data = data
        self.rsp = data.get(TagoMessage.PROP_RSP)
        self.src = data.get(TagoMessage.PROP_SRC, '')
        self.ref = data.get(TagoMessage.PROP_REF)
        self.evt = data.get(TagoMessage.PROP_EVT)

        if TagoMessage.PROP_REF in data:
            del data[TagoMessage.PROP_REF]
        if TagoMessage.PROP_SRC in data:
            del data[TagoMessage.PROP_SRC]
        if TagoMessage.PROP_EVT in data:
            del data[TagoMessage.PROP_EVT]
        if TagoMessage.PROP_RSP in data:
            del data[TagoMessage.PROP_RSP]

        return self

    @classmethod
    def make_request(cls, req: str, data: dict, dst: str = None):
        self = cls()
        self.dst = dst
        self.req = req
        self.data = data
        self.ref = TagoMessage.create_random_str()
        return self

    def get_message(self) -> str:
        data = dict(self.data or {})
        if self.dst:
            data[TagoMessage.PROP_DST] = self.dst
        data[TagoMessage.PROP_REF] = self.ref
        data[TagoMessage.PROP_REQ] = self.req

        msg = json.dumps(data)
        return msg

    @property
    def content(self) -> dict:
        return self.data or dict()

    @property
    def source(self) -> str:
        return self.src

    @property
    def reference(self) -> str:
        return self.ref

    def refers_to(self, ref: str) -> bool:
        return (self.ref and self.ref == ref)

    def is_response(self, rsp: str = None) -> bool:
        if not rsp:
            return (self.rsp is not None)
        return (self.rsp and self.rsp in rsp)

    def is_event(self, evt: str = None) -> bool:
        if not evt:
            return (self.evt is not None)
        return (self.evt and self.evt in evt)

    def is_request(self, req: str = None) -> bool:
        if not req:
            return (self.req is not None)
        return (self.req and self.req in req)


class TagoBase:
    PROP_TYPE = "type"
    PROP_ID = "id"
    PROP_NAME = "name"
    PROP_LOCATION = "location"
    PROP_TAG = "tag"
    REQ_GET_STATE = "get_state"    
    EVT_STATE_CHANGED = "state_changed"
    EVT_CONFIG_CHANGED = "config_changed"
    EVT_KEYPAD = "keypad_evt"
    EVT_MOTION = "motion_evt"
    EVT_IO = "io_evt"

    STATE_ON = "ON"
    STATE_OFF = "OFF"

    def __init__(self, eid: str):
        self._eid: str = eid
        self._update_cbs: list = []

    def set_on_state_changed(self, callback):
        """Register a state-change listener.

        Multi-listener so a single wire entity can drive more than one
        HA entity — e.g., a TagoEntity may have both a primary platform
        entity (light/switch/etc.) and a companion signal-strength
        sensor (PROTOCOL_PROPOSALS §P6) wrapping the same TagoEntity."""
        if callback is None:
            return
        if callback not in self._update_cbs:
            self._update_cbs.append(callback)

    def remove_on_state_changed(self, callback) -> None:
        if callback in self._update_cbs:
            self._update_cbs.remove(callback)

    def update(self) -> None:
        for cb in list(self._update_cbs):
            cb()

    @property
    def unique_id(self):
        return self._eid


class TagoEntity(TagoBase):
    EVT_KEYPRESS = "key_pressed"
    EVT_KEYRELEASE = "key_released"
    VALUE_UNUSED = 'UNUSED'
    MAX_VALUE = 1000

    # PROTOCOL_PROPOSALS §P6: optional per-entity RSSI in dBm. Present
    # only for entities on a wireless link.
    PROP_RSI = "rsi"

    types = []

    def __init__(self, json: dict, device: TagoDevice):
        super().__init__(json[TagoEntity.PROP_ID])
        self._device: TagoDevice = device
        # `type` is the only true config field — it's what dispatch uses
        # to build the right subclass and never changes at runtime. Read
        # it here; everything else (name, location, tag, rsi, plus
        # subclass state) is read through handle_state_change so the
        # init-time and runtime parsers stay identical. Per D7 config is
        # frozen after initial connect, so the runtime path can't
        # overwrite the type either way.
        self._type: str = json.get(TagoEntity.PROP_TYPE, self.VALUE_UNUSED)
        self._fault: list[str] = list()
        self._name: str | None = None
        self._location: str | None = None
        self._tag: str | None = None
        self._rsi: int | None = None
        # Subclasses are expected to initialise their own state defaults
        # BEFORE calling super().__init__, so the handle_state_change
        # dispatch below sees a fully-constructed instance.
        self.handle_state_change(json)

        # if len(self._location.strip()):
        #     info = DeviceInfo(
        #         identifiers={
        #             (
        #                 DOMAIN,
        #                 self._location
        #             )
        #         },
        #         name=self._location,
        #         manufacturer='Tago',
        #         model="Virtual area device",
        #     )

        #     info[ATTR_SUGGESTED_AREA] = self._location
        #     self._attr_device_info = info

    @classmethod
    def is_of_type(cls, type: str):
        return (type in cls.types)

    @property
    def type(self) -> str:
        return self._type

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def location(self) -> str | None:
        return self._location

    @property
    def dashboard_uri(self):
        return f'{self._device.dashboard_uri}?find={self._eid}'

    @property
    def is_connected(self) -> bool:
        return self._device.is_connected

    @property
    def fault(self) -> list[str]:
        return self._fault

    @property
    def has_fault(self) -> bool:
        return len(self._fault) > 0

    @property
    def rsi(self) -> int | None:
        """PROTOCOL_PROPOSALS §P6: most recent RSSI for this entity, or
        None if it isn't on a wireless link / hasn't reported yet."""
        return self._rsi

    def is_unused(self) -> bool:
        return self.type == self.VALUE_UNUSED

    async def connection_state_changed(self, connected: bool) -> None:
        self.update()
        if connected:
            # request state refresh
            await self.send_request(req=self.REQ_GET_STATE)

    async def send_request(self, req: str, data: dict | None = None) -> None:
        if data is None:
            data = {}
        await self._device.send_request(req=req, dst=self._eid, data=data)

    def should_handle_message(self, msg: TagoMessage) -> bool:
        return msg.source == self._eid

    async def handle_event(self, msg: TagoMessage) -> bool:
        if msg.is_event(self.EVT_STATE_CHANGED):
            self.handle_state_change(msg.content)
            return True

        return False

    def handle_state_change(self, data: dict) -> None:
        """Read every state-ish field present in `data` and notify
        listeners. Called from three places with the same shape:
          - `__init__` (initial discovery from `list_nodes`)
          - `state_changed` event dispatch (runtime push)
          - `get_state` response dispatch (explicit refresh)
        Per D7 config is frozen, so name/location/tag/type are only
        materially set on the initial-discovery call — runtime events
        don't carry them. Anything they don't include is left alone."""
        name = data.get(TagoEntity.PROP_NAME)
        if isinstance(name, str):
            self._name = name.strip() or None
        location = data.get(TagoEntity.PROP_LOCATION)
        if isinstance(location, str):
            self._location = location.strip() or None
        if TagoEntity.PROP_TAG in data:
            self._tag = data[TagoEntity.PROP_TAG]
        # PROTOCOL_PROPOSALS §P6: rsi can land on any state_changed for
        # entities on a wireless link.
        rsi = data.get(TagoEntity.PROP_RSI)
        if isinstance(rsi, (int, float)) and not isinstance(rsi, bool):
            self._rsi = int(rsi)
        self.update()

    async def _handle_message(self, msg: TagoMessage) -> bool:
        if msg.is_event():
            return await self.handle_event(msg)
        elif msg.is_response(self.REQ_GET_STATE):
            self.handle_state_change(msg.content)
            return True

        return False

    def handle_message(self, msg: TagoMessage) -> Awaitable[None] | None:
        if not self.should_handle_message(msg):
            return None
        return self._handle_message(msg)

    @staticmethod
    def convert_value_to_float(value: int, max=1.0) -> float:
        return ((value * max) / TagoEntity.MAX_VALUE)

    @staticmethod
    def convert_value_from_float(value: float, max=1.0) -> int:
        return int(round(((value * TagoEntity.MAX_VALUE) / max), 0))


class TagoGateway(TagoBase):
    """The WebSocket endpoint to a Tago gateway. Owns the connection,
    authentication, and the list of TagoDevices reachable through it.

    The gateway is not addressable on the wire — `list_devices` returns
    the device set on the other side, and per-device commands target
    the device's id as `dst`. The gateway itself doesn't get an HA
    device-registry entry; only the TagoDevices do."""

    REQ_LIST_DEVICES = 'list_devices'
    EVT_DEVICE_AVAILABLE = 'device_available'
    EVT_DEVICE_UNAVAILABLE = 'device_unavailable'
    PROP_DEVICES = 'devices'
    PROP_DEVICE_ID = 'device_id'
    PROP_ID = 'id'
    PROP_AVAILABLE = 'available'
    AUTH_HEADER = 'x-tago-auth'
    AUTH_LEGACY = 'legacy'
    AUTH_HMAC_TLS_V2 = 'hmac_tls_v2'

    # How long initial discovery requests wait for a response before
    # giving up. Generous because a slow device can take a beat to
    # enumerate its nodes.
    _DISCOVERY_TIMEOUT_S = 10.0

    def __init__(self, hoststr: str, authkey: str = None, useSSL: bool = False):
        # Use the hoststr as the gateway's local identifier — it has no
        # wire-addressable id, but anything that wants a stable key (HA
        # entry.unique_id, log lines, etc.) needs one.
        super().__init__(hoststr)
        self._usessl = useSSL
        self._hoststr = hoststr
        self._authkey: str = authkey
        self._ca: str = None
        self._ws: ClientConnection = None
        self._task: asyncio.Task = None
        self._running: bool = False
        self._startup_future: asyncio.Future[None] | None = None
        self._devices: list[TagoDevice] = list()
        self._log_throttle_interval_s = 60.0
        self._log_throttle_last: dict[str, float] = {}
        self._log_throttle_suppressed: dict[str, int] = {}
        # Pending futures keyed by outgoing request `ref`. Populated by
        # send_request when a responseTimeout is set; resolved by the
        # message-dispatch loop when a frame with a matching `ref` lands.
        self._pending_responses: dict[str, asyncio.Future[TagoMessage]] = {}

    @property
    def hoststr(self) -> str:
        return self._hoststr

    @property
    def dashboard_uri(self) -> str:
        ssl = 's' if self._usessl else ''
        return f'http{ssl}://{self._hoststr}/'

    @property
    def uri(self) -> str:
        ssl = 's' if self._usessl else ''
        return f'ws{ssl}://{self._hoststr}/api/v1/ws'

    @property
    def manufacturer(self) -> str:
        return 'TAGO'

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    @property
    def devices(self) -> list["TagoDevice"]:
        return self._devices

    @property
    def entities(self) -> list[TagoEntity]:
        """Flat view of all entities across all devices. Convenience
        for HA platforms that don't care which device an entity sits on
        (each entity already knows its own device via `_device`)."""
        result: list[TagoEntity] = []
        for d in self._devices:
            result.extend(d._entities)
        return result

    def get_device(self, device_id: str) -> "TagoDevice | None":
        for d in self._devices:
            if d.unique_id == device_id:
                return d
        return None

    def input_event_message(self, msg: TagoMessage) -> None:
        pass

    def _get_server_handshake_headers(self, ws: ClientConnection) -> dict[str, str]:
        headers: dict[str, str] = {}
        response = getattr(ws, 'response', None)
        if response is None:
            return headers

        response_headers = getattr(response, 'headers', None)
        if response_headers is None:
            return headers

        try:
            for key, value in response_headers.items():
                headers[str(key).lower()] = str(value)
        except Exception as err:
            logging.debug('Unable to read handshake response headers: %s', err)

        return headers

    def _select_auth_strategy(self, headers: dict[str, str]) -> str:
        auth_header = headers.get(self.AUTH_HEADER, '')
        mode = auth_header.split(',')[0].strip().lower() if auth_header else ''

        if not mode:
            return self.AUTH_LEGACY

        if mode in (self.AUTH_LEGACY, self.AUTH_HMAC_TLS_V2):
            return mode

        logging.warning(
            'Unsupported auth mode "%s" from %s, falling back to legacy auth',
            mode,
            self._hoststr,
        )
        return self.AUTH_LEGACY

    async def _authenticate_legacy(self, ws: ClientConnection) -> None:
        """PIN-based handshake. The gateway has no addressable id of its
        own (PROTOCOL_PROPOSALS §P8) — auth just confirms the connection,
        then `list_devices` does the introduction. Older firmware may
        still echo serialnum/model/id in the envelope; we ignore them
        on the gateway side (per-device identity comes from `get_config`
        on each TagoDevice instead)."""
        await ws.send('{}')
        msg = json.loads(await ws.recv())

        status = msg.get('status', 0)
        if status == 200:
            return

        if msg.get('nonce') is None:
            raise PermissionError('No login message from server')

        server_nonce = msg.get('nonce')
        client_nonce = uuid.uuid4().hex
        sha256 = hashlib.sha256()
        sha256.update((client_nonce + self._authkey +
                       server_nonce).encode('utf-8'))
        authcode = sha256.hexdigest()

        await ws.send(json.dumps({
            'nonce': client_nonce,
            'auth': authcode,
        }))

        msg = json.loads(await ws.recv())
        if msg.get('status', 0) != 200:
            raise PermissionError('Legacy login failed')

    async def _authenticate_hmac_tls_v2(
        self, ws: ClientConnection, headers: dict[str, str],
    ) -> None:
        raise PermissionError(
            'Device requires auth mode hmac_tls_v2, which is not implemented in this integration version'
        )

    async def _authenticate_connection(self, ws: ClientConnection) -> None:
        headers = self._get_server_handshake_headers(ws)
        auth_mode = self._select_auth_strategy(headers)
        logging.debug('Selected auth mode "%s" for %s', auth_mode, self._hoststr)

        if auth_mode == self.AUTH_HMAC_TLS_V2:
            await self._authenticate_hmac_tls_v2(ws, headers)
            return

        await self._authenticate_legacy(ws)

    def _signal_startup_success(self) -> None:
        if self._startup_future and not self._startup_future.done():
            self._startup_future.set_result(None)

    def _signal_startup_error(self, err: Exception) -> None:
        if self._startup_future and not self._startup_future.done():
            self._startup_future.set_exception(err)

    def _log_exception_throttled(
        self, key: str, message: str, err: Exception
    ) -> None:
        now = time.monotonic()
        last_logged = self._log_throttle_last.get(key, 0.0)
        suppressed = self._log_throttle_suppressed.get(key, 0)

        if (now - last_logged) >= self._log_throttle_interval_s:
            suffix = f" (suppressed {suppressed} similar errors)" if suppressed else ""
            logging.exception("%s: %s%s", message, err, suffix)
            self._log_throttle_last[key] = now
            self._log_throttle_suppressed[key] = 0
            return

        self._log_throttle_suppressed[key] = suppressed + 1

    async def _stop_connection_manager(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self._startup_future = None

    async def connect(self, timeout: float | None = None) -> None:
        """Ensure the connection manager is running and wait for startup."""
        if self._ws is not None:
            return

        if self._startup_future is None or self._startup_future.done():
            self._startup_future = asyncio.get_running_loop().create_future()

        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self.connection_task())

        startup_future = self._startup_future
        if startup_future is None:
            raise RuntimeError("Connection manager did not initialize startup state")

        try:
            if timeout is None:
                await asyncio.shield(startup_future)
            else:
                await asyncio.wait_for(asyncio.shield(startup_future), timeout=timeout)
        except asyncio.TimeoutError as err:
            await self._stop_connection_manager()
            raise TimeoutError("Connection timed out") from err
        except Exception:
            await self._stop_connection_manager()
            raise

    async def disconnect(self, timeout: float | None = None) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

        if self._task is None:
            return

        # `await self._task` (or `wait_for`) re-raises whatever exception
        # the task held — including CancelledError and any uncaught
        # exception from `connection_task`. That's the intended propagation
        # path; callers are expected to handle it.
        if timeout is None:
            await self._task
        else:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError as err:
                raise TimeoutError(
                    "Timed out waiting for self._task to complete") from err

        self._task = None
        self._startup_future = None

    async def send_request(self, req: str, data: dict | None = None, dst: str = None, responseTimeout: float = None) -> None | TagoMessage:
        """Send a request frame. If `responseTimeout` is set, wait for a
        response with a matching `ref` and return it; raise TimeoutError if
        none arrives in time.

        Response correlation is performed via the `_pending_responses`
        registry: send_request registers a future keyed by the outgoing
        ref, and the connection_task message loop resolves the future when
        a frame with a matching `ref` arrives."""
        if self._ws is None:
            return None

        if data is None:
            data = {}

        msg = TagoMessage.make_request(req=req, dst=dst, data=data)
        future: asyncio.Future[TagoMessage] | None = None
        if responseTimeout:
            future = asyncio.get_running_loop().create_future()
            self._pending_responses[msg.ref] = future

        try:
            payload = msg.get_message()
            logging.debug(f"=== outgoing {payload}")
            await self._ws.send(payload)
            if future is None:
                return None
            return await asyncio.wait_for(asyncio.shield(future), timeout=responseTimeout)
        finally:
            if future is not None:
                self._pending_responses.pop(msg.ref, None)

    async def get_ssl_context(self) -> ssl.SSLContext:
        def _create_context(self) -> ssl.SSLContext:
            try:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_context.check_hostname = False
                ssl_context.set_ciphers('DEFAULT')
                if self._ca:
                    ssl_context.load_verify_locations(cadata=self._ca)
                    ssl_context.verify_mode = ssl.CERT_REQUIRED
                else:
                    ssl_context.verify_mode = ssl.CERT_NONE

                return ssl_context
            except Exception as e:
                logging.exception(e)

        return await asyncio.get_running_loop().run_in_executor(
            None, _create_context, self
        )

    async def _discover_devices(self) -> None:
        """Send `list_devices`. Per D7 the device set is frozen for the
        gateway's lifetime — only the per-device `available` flag is
        allowed to change across reconnects:
          - First connect: build TagoDevice instances from the response.
          - Subsequent reconnects: copy the new `available` value onto
            each existing TagoDevice. New ids in the response are
            ignored; missing ids keep their last-known availability."""
        response = await self.send_request(
            req=self.REQ_LIST_DEVICES,
            responseTimeout=self._DISCOVERY_TIMEOUT_S,
        )
        if response is None or not isinstance(response.data, dict):
            return
        devices_payload = response.data.get(self.PROP_DEVICES, list())

        if not self._devices:
            for item in devices_payload:
                device_id = item.get(self.PROP_ID)
                if not device_id:
                    continue
                self._devices.append(TagoDevice(self, item))
            return

        by_id = {
            item.get(self.PROP_ID): item
            for item in devices_payload
            if item.get(self.PROP_ID)
        }
        for device in self._devices:
            item = by_id.get(device.unique_id)
            if item is not None:
                device._available = bool(
                    item.get(self.PROP_AVAILABLE, device._available)
                )

    async def _dispatch_loop(self, ws: ClientConnection) -> None:
        """Main message-dispatch loop. Pulls frames off the websocket
        and routes them: (1) pending-response futures get resolved, (2)
        gateway-level events (device_available/unavailable) flip the
        right device's availability, (3) anything else is offered to
        every device for matching against its entities."""
        async for message in ws:
            msg = TagoMessage.from_payload(message)

            # Resolve any pending send_request(responseTimeout=) future
            # whose ref matches.
            if msg.ref and msg.ref in self._pending_responses:
                future = self._pending_responses.pop(msg.ref)
                if not future.done():
                    future.set_result(msg)

            # Gateway-level events: device_available / device_unavailable
            # (PROTOCOL_PROPOSALS §P8). `device_id` in the payload picks
            # which device the event is about — `src` is the gateway.
            if msg.evt == self.EVT_DEVICE_AVAILABLE:
                device_id = msg.content.get(self.PROP_DEVICE_ID)
                device = self.get_device(device_id)
                if device is not None:
                    try:
                        await device._on_available()
                    except Exception as e:
                        self._log_exception_throttled(
                            key='device_available_error',
                            message='device_available handling error',
                            err=e,
                        )
                continue
            if msg.evt == self.EVT_DEVICE_UNAVAILABLE:
                device_id = msg.content.get(self.PROP_DEVICE_ID)
                device = self.get_device(device_id)
                if device is not None:
                    try:
                        await device._on_unavailable()
                    except Exception as e:
                        self._log_exception_throttled(
                            key='device_unavailable_error',
                            message='device_unavailable handling error',
                            err=e,
                        )
                continue

            # Per-device dispatch.
            handlers: list[Awaitable[None]] = []
            for device in self._devices:
                try:
                    handler = device.handle_message(msg)
                    if handler is not None:
                        handlers.append(handler)
                except Exception as e:
                    self._log_exception_throttled(
                        key='device_message_prepare_error',
                        message='Device message scheduling error',
                        err=e,
                    )
            if handlers:
                results = await asyncio.gather(*handlers, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        self._log_exception_throttled(
                            key='device_message_error',
                            message='Device message handling error',
                            err=result,
                        )

    async def connection_task(self) -> None:
        self._running = True
        # Tracks whether we've logged the "gateway unavailable" message
        # for the *current* outage. Reset to False on every successful
        # connect and set to True the first time we fail to connect.
        # Implements the Silver-tier `log-when-unavailable` rule (log
        # once per transition).
        unavailable_logged = False
        while self._running:
            was_connected = False
            try:
                logging.debug(f"connecting to {self.uri}")
                ssl_context = await self.get_ssl_context() if self._usessl else None
                async with wsconnect(
                    uri=self.uri, ping_timeout=1, ping_interval=3,
                    close_timeout=5, ssl=ssl_context,
                ) as ws:
                    logging.debug(f"connected to {self.uri}")
                    self._ws = ws
                    try:
                        await self._authenticate_connection(ws)
                    except PermissionError as err:
                        self._running = False
                        auth_err = PermissionError('Auth failed')
                        self._signal_startup_error(auth_err)
                        raise auth_err from err

                    # Kick the dispatch loop off in the background. From
                    # here on, send_request(responseTimeout=...) works —
                    # the dispatch loop resolves the pending-response
                    # futures. Initial discovery uses that path so it
                    # doesn't have to consume the ws iterator itself.
                    dispatch_task = asyncio.create_task(self._dispatch_loop(ws))

                    try:
                        # list_devices — first connect builds the device
                        # list; reconnects just refresh `available` per
                        # device (the set itself is frozen for the
                        # gateway's lifetime per D7).
                        await self._discover_devices()

                        # Per available device, run initial discovery
                        # (list_nodes + get_config). _populate_from_gateway
                        # short-circuits on already-populated devices, so
                        # this is a no-op on reconnect — that path's job
                        # is just to fire `connection_state_changed(True)`
                        # below, which refreshes per-entity state.
                        for device in self._devices:
                            if device.available:
                                await device._populate_from_gateway()

                        was_connected = True
                        self._signal_startup_success()
                        if unavailable_logged:
                            logging.info(
                                "Tago gateway %s is available again",
                                self._hoststr,
                            )
                            unavailable_logged = False

                        # Notify entities on available devices that the
                        # transport is up.
                        for device in self._devices:
                            if device.available:
                                for entity in device._entities:
                                    await entity.connection_state_changed(True)
                        self.update()

                        # Wait for the dispatch loop to end — that
                        # happens when the websocket closes.
                        await dispatch_task
                    finally:
                        if not dispatch_task.done():
                            dispatch_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await dispatch_task

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._signal_startup_error(e)
                self._log_exception_throttled(
                    key='connection_loop_error',
                    message='Connection loop error',
                    err=e,
                )

            self._ws = None

            # Resolve any in-flight send_request futures so callers
            # don't hang waiting on responses that will never arrive.
            for future in list(self._pending_responses.values()):
                if not future.done():
                    future.set_exception(ConnectionError("WebSocket closed"))
            self._pending_responses.clear()

            # Log "gateway unavailable" once per outage.
            if not unavailable_logged:
                if was_connected:
                    logging.warning(
                        "Tago gateway %s became unavailable", self._hoststr,
                    )
                else:
                    logging.warning(
                        "Tago gateway %s is unavailable", self._hoststr,
                    )
                unavailable_logged = True

            # Notify all entities on all devices that the transport is
            # down. (Per-device `_available` stays as-is — that flag
            # tracks device-side state; the gateway's `is_connected`
            # going False makes entities unavailable through entity.is_connected.)
            if was_connected:
                for device in self._devices:
                    for entity in device._entities:
                        try:
                            await entity.connection_state_changed(False)
                        except Exception as e:
                            logging.exception(e)
            self.update()

            if self._running:
                await asyncio.sleep(3)


class TagoDevice(TagoBase):
    """A single physical device hanging off a TagoGateway. Owns the
    entities reported by its `list_nodes`, plus its own device-level
    config (firmware, name, location, model, serial). All wire traffic
    flows through the parent gateway's WebSocket; the device just
    contributes `dst` to outgoing requests and matches incoming `src`
    against its entities."""
    REQ_GET_DEVICE_INFO = 'get_device_info'
    REQ_DEVICE_REBOOT = 'reboot'
    REQ_DEVICE_IDENTIFY = 'identify'
    PROP_NODES = 'nodes'
    PROP_LOADS = 'loads'
    # PROTOCOL_PROPOSALS: additional collection keys sibling to `loads`
    # in each loads-group node.
    PROP_SCENES = 'scenes'
    PROP_KEYPADS = 'keypads'
    PROP_VIRTUAL_SWITCHES = 'virtual_switches'
    PROP_VIRTUAL_SENSORS = 'virtual_sensors'
    PROP_SENSORS = 'sensors'
    # PROTOCOL_PROPOSALS §P5 + §P7: extra fields on the device-level
    # `get_config` response.
    PROP_FIRMWARE_REV = 'firmware_rev'
    PROP_NAME = 'name'
    PROP_LOCATION = 'location'
    PROP_MODEL_NUM = 'model_num'
    PROP_SERIAL_NUM = 'serial_num'

    # PROTOCOL_PROPOSALS §P5.3: async event the device emits when a
    # firmware update becomes available. Carries the new revision in
    # `latest_firmware_rev`; the host stores it and flips the
    # firmware-update sensor on.
    EVT_FIRMWARE_UPDATE_AVAILABLE = 'firmware_update_available'
    PROP_LATEST_FIRMWARE_REV = 'latest_firmware_rev'

    _DISCOVERY_TIMEOUT_S = 10.0

    def __init__(self, gateway: TagoGateway, json: dict):
        super().__init__(json[TagoGateway.PROP_ID])
        self._gateway = gateway
        self._available: bool = bool(json.get(TagoGateway.PROP_AVAILABLE, True))
        # Identity + config — populated from the device's `get_config`
        # response during initial discovery. `list_devices` only carries
        # `id` and `available`.
        self._modelnum: str | None = None
        self._serialnum: str | None = None
        self._firmware_rev: str | None = None
        self._latest_firmware_rev: str | None = None
        self._name: str | None = None
        self._location: str | None = None
        self._entities: list[TagoEntity] = list()

    @property
    def gateway(self) -> TagoGateway:
        return self._gateway

    @property
    def available(self) -> bool:
        """Device-side availability flag. True if the device reported
        itself online in `list_devices` (or via a `device_available`
        event since). The HA-visible `is_connected` also factors in
        the gateway's transport state."""
        return self._available

    @property
    def is_connected(self) -> bool:
        return self._gateway.is_connected and self._available

    @property
    def dashboard_uri(self) -> str:
        # Single web UI per gateway; entities append their own `?find=`
        # off this root. Avoids stacking two `?find=` segments.
        return self._gateway.dashboard_uri

    @property
    def manufacturer(self) -> str:
        return self._gateway.manufacturer

    @property
    def model_num(self) -> str | None:
        return self._modelnum

    @property
    def serial_num(self) -> str | None:
        return self._serialnum

    @property
    def firmware_rev(self) -> str | None:
        return self._firmware_rev

    @property
    def latest_firmware_rev(self) -> str | None:
        """The new firmware revision reported by the most recent
        `firmware_update_available` event for this device, or None if
        no such event has fired since the last connect. Not part of
        any state/config response — see PROTOCOL_PROPOSALS §P5.3."""
        return self._latest_firmware_rev

    @property
    def firmware_update_available(self) -> bool:
        """True iff a `firmware_update_available` event has fired for
        this device since the last connect. Resets to False on each
        new gateway connection (and stays False until/unless the
        firmware re-fires the event)."""
        return self._latest_firmware_rev is not None

    @property
    def name(self) -> str:
        return self._name or f'Device {self.unique_id}'

    @property
    def location(self) -> str | None:
        """PROTOCOL_PROPOSALS §P7: optional user-set room/area for this
        device. Drives the `suggested_area` on its HA device-registry
        card. None when the device hasn't set one."""
        return self._location

    @property
    def entities(self) -> list[TagoEntity]:
        return self._entities

    async def send_request(
        self, req: str, data: dict | None = None,
        responseTimeout: float | None = None,
    ) -> "None | TagoMessage":
        return await self._gateway.send_request(
            req=req, dst=self._eid, data=data, responseTimeout=responseTimeout,
        )

    async def reboot(self) -> None:
        if not self.is_connected:
            return
        await self.send_request(req=self.REQ_DEVICE_REBOOT)

    async def identify(self) -> None:
        if not self.is_connected:
            return
        await self.send_request(req=self.REQ_DEVICE_IDENTIFY)

    async def _populate_from_gateway(self) -> None:
        """Initial-discovery helper: pull `list_nodes` + `get_config`
        for this device. Per D7 the entity list and the device-level
        config are frozen for the device's lifetime — calling this on
        a device that's already populated is a no-op, which makes it
        safe to invoke unconditionally on every (re)connect."""
        """Send `get_device_info` and build the entity list. Per D7 the
        entity set is frozen for this device's lifetime, so the caller
        (`_populate_from_gateway`) only runs this once."""
        response = await self.send_request(
            req=self.REQ_GET_DEVICE_INFO,
            responseTimeout=self._DISCOVERY_TIMEOUT_S,
        )
        if response is None or not isinstance(response.data, dict):
            return
        
        self._apply_device_config(response.data)
        
        if self._entities:
            return
        entities: list[TagoEntity] = []
        # PROTOCOL.md §9: `list_nodes` returns a `nodes` map keyed by
        # loads-group entity ID. Each group holds arrays under the
        # documented PROP_* keys (loads + the §P1–§P4 extensions).
        # Keypad LEDs live inside each keypad's `keys` array, not as
        # a top-level collection.
        for _, value in response.data.get(self.PROP_NODES, dict()).items():
            self._extract_entities_from_group(value, entities)
        self._entities = entities            

    async def _on_available(self) -> None:
        """Gateway delivered a `device_available` event for us. The
        very first time this device is seen as available (it was
        offline at initial connect), `_populate_from_gateway` runs the
        full list_nodes + get_config discovery. On every subsequent
        availability flip it's a no-op — per D7 the entity list is
        frozen, so we just notify entities they're back online and
        let them refresh state via their own `get_state`."""
        was_available = self._available
        self._available = True
        try:
            await self._populate_from_gateway()
        except Exception as e:
            logging.exception(
                "Failed to populate device %s on availability event: %s",
                self.unique_id, e,
            )
        for entity in self._entities:
            try:
                await entity.connection_state_changed(True)
            except Exception as e:
                logging.exception(e)
        if not was_available:
            self.update()

    async def _on_unavailable(self) -> None:
        """Gateway delivered a `device_unavailable` event. Mark down and
        notify entities — their `is_connected` now reads False, so HA
        will show them as unavailable."""
        if not self._available:
            return
        self._available = False
        for entity in self._entities:
            try:
                await entity.connection_state_changed(False)
            except Exception as e:
                logging.exception(e)
        self.update()

    def handle_message(self, msg: TagoMessage) -> Awaitable[None] | None:
        """Inspect a wire frame: apply device-level get_config or
        firmware-update event payload to this device, then offer the
        frame to every owned entity. Returns an awaitable to gather,
        or None if no entity wanted it."""
        if msg.src == self._eid:
            if msg.rsp == TagoDevice.REQ_GET_DEVICE_INFO:
                self._apply_device_config(msg.data)
            elif msg.evt == self.EVT_FIRMWARE_UPDATE_AVAILABLE:
                self._apply_firmware_update_event(msg.content)

        handlers: list[Awaitable[None]] = []
        for entity in self._entities:
            handler = entity.handle_message(msg)
            if handler is not None:
                handlers.append(handler)
        if not handlers:
            return None
        return asyncio.gather(*handlers, return_exceptions=True)

    def _apply_device_config(self, data: dict) -> None:
        """Pick up identity + firmware fields from a device-level
        `get_config` response. Per D7 this is materially applied only
        on the initial response — runtime `config_changed` events are
        ignored (host doesn't re-route from them)."""
        changed = False
        model = data.get(self.PROP_MODEL_NUM)
        if isinstance(model, str) and model != self._modelnum:
            self._modelnum = model
            changed = True
        serial = data.get(self.PROP_SERIAL_NUM)
        if isinstance(serial, str) and serial != self._serialnum:
            self._serialnum = serial
            changed = True
        fw = data.get(self.PROP_FIRMWARE_REV)
        if fw is not None and fw != self._firmware_rev:
            self._firmware_rev = fw
            changed = True
        name = data.get(self.PROP_NAME)
        if isinstance(name, str):
            name = name.strip() or None
            if name != self._name:
                self._name = name
                changed = True
        location = data.get(self.PROP_LOCATION)
        if isinstance(location, str):
            location = location.strip() or None
            if location != self._location:
                self._location = location
                changed = True
        if changed:
            self.update()

    def _apply_firmware_update_event(self, data: dict) -> None:
        """Apply a `firmware_update_available` event payload
        (PROTOCOL_PROPOSALS §P5.3). The event carries the new revision
        in `latest_firmware_rev`; receiving it flips the per-device
        firmware-update sensor on."""
        latest = data.get(self.PROP_LATEST_FIRMWARE_REV)
        if isinstance(latest, str) and latest != self._latest_firmware_rev:
            self._latest_firmware_rev = latest
            self.update()


    def _extract_entities_from_group(self, group: dict, entities: list) -> None:
        COLLECTIONS = (
            self.PROP_LOADS,
            self.PROP_SCENES,
            self.PROP_KEYPADS,
            self.PROP_VIRTUAL_SWITCHES,
            self.PROP_VIRTUAL_SENSORS,
            self.PROP_SENSORS,
        )
        for collection_key in COLLECTIONS:
            for item in group.get(collection_key, list()):
                try:
                    entity_id = item.get(TagoEntity.PROP_ID)
                    if not entity_id:
                        continue
                    entities.append(self._build_entity_from_payload(item))
                except Exception as e:
                    logging.exception(e)

    def _build_entity_from_payload(self, item: dict) -> TagoEntity:
        """Pick the right entity class for a discovery payload based on
        its `type`. Ordering matters because the type vocabularies don't
        overlap — first match wins."""
        t = item.get(TagoEntity.PROP_TYPE)
        if TagoLight.is_of_type(t):
            return TagoLight(item, self)
        if TagoSwitch.is_of_type(t):
            return TagoSwitch(item, self)
        if TagoCover.is_of_type(t):
            return TagoCover(item, self)
        if TagoFan.is_of_type(t):
            return TagoFan(item, self)
        if TagoScene.is_of_type(t):
            return TagoScene(item, self)
        if TagoKeypad.is_of_type(t):
            return TagoKeypad(item, self)
        if TagoVirtualSwitch.is_of_type(t):
            return TagoVirtualSwitch(item, self)
        if TagoVirtualSensor.is_of_type(t):
            return TagoVirtualSensor(item, self)
        if TagoSensor.is_of_type(t):
            return TagoSensor(item, self)
        # PROTOCOL_PROPOSALS §P4.2: any other `sensor_*` type is an
        # unknown sensor — still surface it (with no device class) so
        # users can automate against it.
        if isinstance(t, str) and t.startswith("sensor_"):
            return TagoSensor(item, self)
        # Unknown / UNUSED — placeholder entity.
        return TagoEntity(item, self)


class TagoSwitch(TagoEntity):
    OUTLET_ONOFF = "outlet_onoff"

    types = [OUTLET_ONOFF]

    REQ_TURN_ON = "turn_on"
    REQ_TURN_OFF = "turn_off"
    PROP_IS_ON = "is_on"

    def __init__(self, json: dict, device: TagoDevice):
        # Default goes before super().__init__ so the dispatch into
        # handle_state_change from the base init can safely read it.
        self._is_on: bool = False
        super().__init__(json, device)

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def turn_on(self):
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self):
        await self.send_request(req=self.REQ_TURN_OFF)

    def handle_state_change(self, data: dict) -> None:
        # PROTOCOL.md §12.3: on/off entities expose `is_on` (bool) only.
        if self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        super().handle_state_change(data)


class Ramp:
    def __init__(self, start: list[float], end: list[float], duration: int, elapsed: int, update_interval: int, callback: Callable):
        self.start = start
        self.end = end
        self.duration = duration
        self.elapsed = elapsed
        self.start_time = round(time.time() * 1000)
        self.update_interval = update_interval
        self.cb = callback
        self.task: asyncio.Task = asyncio.create_task(self.task())

    async def task(self) -> None:
        while True:
            try:
                elapsed = ((round(time.time() * 1000)) -
                           self.start_time) + self.elapsed
                # ramp finished?
                if elapsed > self.duration:
                    return

                progress = min(elapsed / self.duration, 1.0)

                values = self.start.copy()
                for i in range(len(values)):
                    if values[i] is None:
                        continue
                    values[i] = self.start[i] + \
                        (progress * (self.end[i] - self.start[i]))

                if self.cb:
                    self.cb(values)

                await asyncio.sleep(self.update_interval)
            except Exception as e:
                logging.exception(e)

    def cancel(self):
        if self.task:
            self.cb = None
            self.task.cancel()


class TagoLight(TagoEntity):
    LIGHT_ONOFF = "light_onoff"
    LIGHT_DIMMABLE = "light_dimmable"
    LIGHT_MONO = "light_mono"
    LIGHT_RGB = "light_rgb"
    LIGHT_RGBW = "light_rgbw"
    LIGHT_RGB_CCT = "light_rgbww"
    LIGHT_CCT = "light_ww"
    PROP_X = "x"
    PROP_Y = "y"
    PROP_CT = "ct"
    PROP_CT_PLUS = "ct+"
    PROP_CT_RANGE = "ct_range"
    PROP_DURATION = "duration"
    PROP_RATE = "rate"
    PROP_BRIGHTNESS = "brightness"
    PROP_BRIGHTNESS_PLUS = "brightness+"
    PROP_DURATION = "duration"
    PROP_MAX_INTENSITY = "max_intensity"
    PROP_RAMP = "ramp"
    PROP_ELAPSED = "elapsed"
    PROP_START = "start"
    PROP_END = "end"
    PROP_FAULT = "fault"
    PROP_EFFECT = "effect"
    VALUE_FLASH = "flash"
    REQ_SET_LIGHT = "set_light"
    REQ_STOP_RAMP = "stop_ramp"
    REQ_LIGHT_EFFECT = "light_effect"

    types = [LIGHT_ONOFF, LIGHT_DIMMABLE, LIGHT_MONO, LIGHT_RGB,
             LIGHT_RGBW, LIGHT_RGB_CCT, LIGHT_CCT]

    REQ_TURN_ON = "turn_on"
    REQ_TURN_OFF = "turn_off"
    REQ_TOGGLE = "toggle"
    PROP_IS_ON = "is_on"

    CT_MIN = 1400
    CT_MAX = 10000

    def __init__(self, json: dict, device: TagoDevice):
        # Defaults set before super().__init__ so the base class's
        # handle_state_change dispatch lands on a fully-initialised
        # instance.
        self._brightness: int = 0
        self._colour_x: float = 0.0
        self._colour_y: float = 0.0
        self._ct: float = 0.0
        self._ct_range_min: int = TagoLight.CT_MIN
        self._ct_range_max: int = TagoLight.CT_MAX
        self._ramp: Ramp = None
        super().__init__(json, device)

    # PROTOCOL.md §5: ramp duration is clamped to
    # [CONFIG_RAMP_DURATION_MIN, CONFIG_RAMP_DURATION_MAX] = [300, 10000] ms.
    # Below the minimum is treated as instant (no ramp).
    DURATION_MIN_MS = 300
    DURATION_MAX_MS = 10000

    def _brightness_param_parse(self, brightness: float, duration: float = None, rate: float = None) -> dict:
        data = {}
        if duration is not None:
            ms = int(round(duration * 1000, 0))
            if ms <= 0 or ms < self.DURATION_MIN_MS:
                # Instant change — omit `duration` rather than send a value
                # the device will silently treat as 0.
                pass
            elif ms > self.DURATION_MAX_MS:
                data[self.PROP_DURATION] = self.DURATION_MAX_MS
            else:
                data[self.PROP_DURATION] = ms
        elif rate is not None:
            data[self.PROP_RATE] = int(round((rate * 1000), 0))

        if brightness is not None:
            data[self.PROP_BRIGHTNESS] = self.convert_value_from_float(
                brightness)

        return data

    async def set_light_flash(self, duration: int) -> None:
        """Flash all channels for a specified duration"""
        await self.send_request(req=self.REQ_LIGHT_EFFECT, data={self.PROP_EFFECT: self.VALUE_FLASH, self.PROP_DURATION: duration})

    @property
    def is_onoff(self) -> bool:
        return self._type == self.LIGHT_ONOFF

    async def turn_on(self) -> None:
        """PROTOCOL.md §12.4 — on/off entities must use the `turn_on` request,
        not `set_light` (which is forbidden for the on/off category per §12.7)."""
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self) -> None:
        await self.send_request(req=self.REQ_TURN_OFF)

    async def toggle(self) -> None:
        await self.send_request(req=self.REQ_TOGGLE)

    async def set_brightness(self, brightness: float, duration: float = None, rate: float = None) -> None:
        """Set brightness to specified value between 0.0 and 1.0.

        For `light_onoff` entities (PROTOCOL.md §12) this routes to
        `turn_on`/`turn_off` per §12.7 — `set_light` is rejected by the
        firmware for the on/off category."""
        if brightness is None:
            raise ValueError('Brightness must be specified')

        if self.is_onoff:
            if brightness > 0:
                await self.turn_on()
            else:
                await self.turn_off()
            return

        data = self._brightness_param_parse(brightness, duration, rate)
        await self.send_request(req=self.REQ_SET_LIGHT, data=data)

    async def adjust_brightness(self, brightness: float, duration: float = None, rate: float = None) -> None:
        """Adjust brightness up or down between -1.0 and 1.0"""
        data = self._brightness_param_parse(brightness, duration, rate)
        await self.send_request(req=self.REQ_SET_LIGHT, data=data)

    async def set_ct(self, ct: float,  brightness: float = None, duration: float = None, rate: float = None) -> None:
        """Set colour temperature ratio and (optional) brightness to be between 0.0 and 1.0"""
        if ct is None:
            raise ValueError('Colour Temperature must be specified')

        data = self._brightness_param_parse(brightness, duration, rate)
        data[self.PROP_CT] = self.convert_value_from_float(ct)
        await self.send_request(req=self.REQ_SET_LIGHT, data=data)

    async def set_colour(self, colour: tuple[float, float],  brightness: float = None, duration: float = None) -> None:
        """Set colour XY points and (optional) brightness to be between 0.0 and 1.0"""
        if colour is None or len(colour) < 2:
            raise ValueError('Colour XY pair must be specified')

        data = self._brightness_param_parse(brightness, duration)
        data[self.PROP_X] = colour[0]
        data[self.PROP_Y] = colour[1]
        await self.send_request(req=self.REQ_SET_LIGHT, data=data)

    async def stop_ramp(self):
        """Stop any active ramps"""
        await self.send_request(req=self.REQ_STOP_RAMP)

    @property
    def brightness(self) -> int:
        return self.convert_value_to_float(self._brightness)

    @property
    def ct(self) -> int:
        return self.convert_value_to_float(self._ct)

    @property
    def colour_xy(self) -> tuple[float, float]:
        return (self._colour_x, self._colour_y)

    @property
    def colour_temp_range(self) -> tuple[int, int]:
        return (self._ct_range_min, self._ct_range_max)

    @property
    def is_ramp_active(self) -> bool:
        return self._ramp is not None

    def ramp_update(self, values):
        if values[0] is not None:
            self._brightness = values[0]
        if values[1] is not None:
            self._ct = values[1]
        if values[2] is not None:
            self._colour_x = values[2]
        if values[3] is not None:
            self._colour_y = values[3]

        self.update()

    def handle_state_change(self, data: dict) -> None:
        # Cancel any in-flight ramp; the new payload supersedes it.
        if self._ramp:
            self._ramp.cancel()
            self._ramp = None

        # `is_on` carries on/off state for `light_onoff`; mirror it into
        # `_brightness` so callers that read `.brightness > 0` (e.g.
        # TagoLightHA.is_on) still work.
        if self.PROP_IS_ON in data:
            self._brightness = self.MAX_VALUE if bool(data[self.PROP_IS_ON]) else 0

        self._brightness = data.get(self.PROP_BRIGHTNESS, self._brightness)
        self._ct = data.get(self.PROP_CT, self._ct)
        # PROTOCOL.md §13.1: ct_range is a 2-element [warm_K, cool_K] list.
        # Lands on the discovery payload only; per D7 runtime config_changed
        # is ignored, so this is effectively init-only.
        ct_basis = data.get(TagoLight.PROP_CT_RANGE, list())
        if isinstance(ct_basis, list) and len(ct_basis) == 2:
            self._ct_range_min = max(int(ct_basis[0]), TagoLight.CT_MIN)
            self._ct_range_max = min(int(ct_basis[1]), TagoLight.CT_MAX)
        self._colour_x = data.get(self.PROP_X, self._colour_x)
        self._colour_y = data.get(self.PROP_Y, self._colour_y)

        fault = data.get(self.PROP_FAULT)
        if fault:
            self._fault = fault.split(',')
        else:
            self._fault = list()

        # If a ramp is in progress on the device, animate the value
        # change locally so HA renders smoothly instead of snapping to
        # the end-state.
        ramp: dict = data.get(self.PROP_RAMP, dict())
        if ramp:
            start = ramp.get(self.PROP_START, dict())
            end = ramp.get(self.PROP_END, dict())
            duration = ramp.get(self.PROP_DURATION, 0)
            elapsed = ramp.get(self.PROP_ELAPSED, 0)

            def get_values(collection, map):
                values = list()
                for i in range(len(map)):
                    key = map[i]
                    value = collection.get(key)
                    if value:
                        values.append(value)
                    else:
                        values.append(None)
                return values

            props = [self.PROP_BRIGHTNESS,
                     self.PROP_CT, self.PROP_X, self.PROP_Y]
            start_values = get_values(start, props)
            end_values = get_values(end, props)
            self._ramp = Ramp(start_values, end_values,
                              duration, elapsed, 1/8, self.ramp_update)

        super().handle_state_change(data)


class TagoCover(TagoEntity):
    # PROTOCOL.md §14.3 reserves `cover_blinds` and `cover_curtain` as the
    # eventual type strings for cover loads (commands TBD).
    COVER_SHADE = "cover_shade"
    COVER_BLIND = "cover_blind"
    COVER_CURTAIN = "cover_curtain"

    types = [COVER_SHADE, COVER_CURTAIN, COVER_BLIND]

    REQ_STOP = "stop_move"
    REQ_MOVE_TO = "move_to"

    def __init__(self, json: dict, device: TagoDevice):
        self._position = 0
        self._target = 0
        super().__init__(json, device)

    @property
    def position(self) -> int:
        return self._position

    @property
    def target(self) -> int:
        return self._target

    async def move_to(self, target: int):
        await self.send_request(req=self.REQ_MOVE_TO, data={"target": target})

    async def stop_move(self):
        await self.send_request(req=self.REQ_STOP)

    def handle_state_change(self, data: dict) -> None:
        self._position = data.get("position", self._position)
        self._target = data.get("target", self._target)
        super().handle_state_change(data)


class TagoFan(TagoEntity):
    # PROTOCOL.md §7a — `fan_onoff` is the only fan type today and it is
    # strictly on/off. There is no speed control on the wire (`set_fan` is
    # not a protocol command — the firmware has no dispatcher for it).
    FAN_ONOFF = "fan_onoff"

    types = [FAN_ONOFF]

    REQ_TURN_ON = "turn_on"
    REQ_TURN_OFF = "turn_off"
    REQ_TOGGLE = "toggle"
    PROP_IS_ON = "is_on"

    def __init__(self, json: dict, device: TagoDevice):
        self._is_on: bool = False
        super().__init__(json, device)

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def turn_on(self):
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self):
        await self.send_request(req=self.REQ_TURN_OFF)

    async def toggle(self):
        await self.send_request(req=self.REQ_TOGGLE)

    def handle_state_change(self, data: dict) -> None:
        # PROTOCOL.md §12.3: on/off entities expose `is_on` (bool) only.
        if self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        super().handle_state_change(data)


# =====================================================================
# Protocol extensions — see PROTOCOL_PROPOSALS.md in the firmware repo.
# These wire shapes are not yet implemented in shipping firmware; HA-side
# handling is in place so the integration is ready when the firmware
# ships.
# =====================================================================


class TagoScene(TagoEntity):
    """A user-curated scene on the device. PROTOCOL_PROPOSALS §P1."""

    SCENE = "scene"
    types = [SCENE]

    REQ_ACTIVATE = "activate"
    EVT_SCENE_ACTIVATED = "scene_activated"
    PROP_LAST_ACTIVATED_TS = "last_activated_ts"

    def __init__(self, json: dict, device: TagoDevice):
        self._last_activated_ts: int = 0
        self._scene_activated_cbs: list = []
        super().__init__(json, device)

    def handle_state_change(self, data: dict) -> None:
        # `last_activated_ts` rides on the discovery payload (§P1) and
        # any explicit get_state response. Runtime activation flows
        # through the `scene_activated` event in handle_event.
        if self.PROP_LAST_ACTIVATED_TS in data:
            ts = data[self.PROP_LAST_ACTIVATED_TS]
            if isinstance(ts, int):
                self._last_activated_ts = ts
        super().handle_state_change(data)

    @property
    def last_activated_ts(self) -> int:
        return self._last_activated_ts

    def set_on_scene_activated(self, callback) -> None:
        """Register a listener for `scene_activated` events. Multi-listener
        so both the HA scene-platform state-stamp and the hass.bus
        dispatcher can subscribe without overwriting each other."""
        if callback is None:
            return
        if callback not in self._scene_activated_cbs:
            self._scene_activated_cbs.append(callback)

    def remove_on_scene_activated(self, callback) -> None:
        if callback in self._scene_activated_cbs:
            self._scene_activated_cbs.remove(callback)

    async def activate(self) -> None:
        await self.send_request(req=self.REQ_ACTIVATE)

    async def handle_event(self, msg: TagoMessage) -> None:
        if msg.is_event(self.EVT_SCENE_ACTIVATED):
            ts = msg.content.get("ts")
            if isinstance(ts, int):
                self._last_activated_ts = ts
            if self._scene_activated_cbs:
                # Listeners own the HA state-stamp (via the scene platform's
                # `_async_record_activation`) and any bus fan-out. Skipping
                # `self.update()` avoids a redundant pre-stamp state write.
                for cb in list(self._scene_activated_cbs):
                    await cb(msg)
            else:
                self.update()
            return
        await super().handle_event(msg)


class TagoKeypad(TagoEntity):
    """A physical keypad with N keys, each with an addressable LED.
    PROTOCOL_PROPOSALS §P2.

    The keypad is the only addressable entity on the wire — each key
    is a nested `TagoKeypadKey` whose LED is controlled by sending
    `set_led` to the keypad with `key_id` in the payload. Not exposed
    as an HA entity; the integration registers a `device_registry`
    entry per keypad and a `light` entity per key LED."""

    PROP_KEYS = "keys"
    PROP_KEY_ID = "key_id"

    KEY_EVENT_PRESSED = "key_pressed"
    KEY_EVENT_RELEASED = "key_released"
    KEY_EVENT_SINGLE_PRESS = "key_single_press"
    KEY_EVENT_DOUBLE_PRESS = "key_double_press"
    KEY_EVENT_TRIPLE_PRESS = "key_triple_press"
    KEY_EVENT_PRESS_HELD = "key_press_held"

    KEY_EVENTS = [
        KEY_EVENT_PRESSED,
        KEY_EVENT_RELEASED,
        KEY_EVENT_SINGLE_PRESS,
        KEY_EVENT_DOUBLE_PRESS,
        KEY_EVENT_TRIPLE_PRESS,
        KEY_EVENT_PRESS_HELD,
    ]

    # PROTOCOL_PROPOSALS §P2.4: emitted when a key LED's state changes
    # without an accompanying key press (remote set_led, firmware-internal
    # automation). Key events already piggy-back the LED state, so this
    # fires only when a press didn't drive the change.
    EVT_KEYPAD_LED_CHANGED = "keypad_led_changed"

    @classmethod
    def is_of_type(cls, type: str) -> bool:
        # Match any `keypad_*` wire type so new variants (4btn, 8btn,
        # modular, ...) work without an explicit list update. There is
        # no `keypad_led` top-level type — LEDs live under keys.
        return isinstance(type, str) and type.startswith("keypad_")

    def __init__(self, json: dict, device: TagoDevice):
        # Per-key LED objects are built from the `keys[]` array now;
        # we initialise the list first so the base init's
        # handle_state_change dispatch (which would land on this class's
        # override if it ever needed to walk keys) sees a real list.
        self._keys: list[TagoKeypad.TagoKeypadKey] = [
            TagoKeypad.TagoKeypadKey(self, k)
            for k in (json.get(self.PROP_KEYS) or [])
        ]
        self._key_event_cbs: list = []
        super().__init__(json, device)

    @property
    def keys(self) -> list[TagoKeypad.TagoKeypadKey]:
        return list(self._keys)

    def get_key(self, key_id: str) -> TagoKeypad.TagoKeypadKey | None:
        for k in self._keys:
            if k.key_id == key_id:
                return k
        return None

    def set_on_key_event(self, callback) -> None:
        """Register a per-key-event listener. Multi-listener so the HA
        bus dispatcher and the per-LED state observer can coexist."""
        if callback is None:
            return
        if callback not in self._key_event_cbs:
            self._key_event_cbs.append(callback)

    def remove_on_key_event(self, callback) -> None:
        if callback in self._key_event_cbs:
            self._key_event_cbs.remove(callback)

    async def handle_event(self, msg: TagoMessage) -> None:
        # Every per-key event carries `key_id`; route it to the matching
        # key first so per-key LED state is current before listeners run.
        # Key press / gesture events additionally fan out via
        # `_key_event_cbs`. Events without `key_id` (keypad-level rsi
        # updates, config_changed) fall through to the base handler.
        kid = msg.content.get(self.PROP_KEY_ID)
        if kid is not None:
            key = self.get_key(kid)
            if key is not None:
                key.handle_state_change(msg.content)

        if msg.evt in self.KEY_EVENTS:
            for cb in list(self._key_event_cbs):
                await cb(msg)
            return

        if kid is None:
            await super().handle_event(msg)

    class TagoKeypadKey(TagoEntity):
        """One key of a keypad — primarily models the per-key LED.
        PROTOCOL_PROPOSALS §P2.

        Not a top-level wire entity. Addressed by sending commands
        (`set_led`) to the parent keypad with `key_id` in the payload.
        Exposes a `set_on_state_changed` listener so the HA-side light
        entity can re-render when LED state changes via either route
        (embedded in key events, or standalone `keypad_led_changed`)."""

        REQ_SET_LED = "set_led"
        REQ_PRESS = "press"

        PROP_ID = "id"
        PROP_IS_ON = "is_on"
        PROP_BRIGHTNESS = "brightness"
        PROP_RGB = "rgb"
        PROP_EFFECT = "effect"
        PROP_DURATION = "duration"

        EFFECT_FLASH = "flash"

        BRIGHTNESS_MIN = 0
        BRIGHTNESS_MAX = 1000
        EFFECT_DURATION_MIN_MS = 100
        EFFECT_DURATION_MAX_MS = 60000

        def __init__(self, keypad: TagoKeypad, json: dict):
            self._keypad = keypad
            self._key_id: str | None = json.get(self.PROP_ID)
            # State defaults — the canonical parser below populates them
            # from the discovery payload, the same way it does for
            # runtime state events.
            self._is_on: bool = False
            self._brightness: int = 0
            self._rgb: tuple[int, int, int] = (0, 0, 0)
            self._update_cbs: list = []
            self.handle_state_change(json)

        @property
        def keypad(self) -> TagoKeypad:
            return self._keypad

        @property
        def keypad_id(self) -> str:
            """The parent keypad's entity id — used by the HA-side light
            entity to nest under the keypad's device-registry card."""
            return self._keypad.unique_id

        @property
        def unique_id(self) -> str:
            return f"{self._keypad.unique_id}:{self._key_id}"

        @property
        def key_id(self) -> str | None:
            return self._key_id

        @property
        def is_on(self) -> bool:
            return self._is_on

        @property
        def brightness(self) -> int:
            """0..1000 wire scale (same as PROTOCOL.md §5)."""
            return self._brightness

        @property
        def rgb(self) -> tuple[int, int, int]:
            return self._rgb

        @property
        def is_connected(self) -> bool:
            return self._keypad.is_connected

        def handle_state_change(self, data: dict) -> None:
            """Apply any LED state in the payload, then notify
            listeners. Every wire shape that carries this key's LED
            state — `keys[]` entries in `list_nodes`, `keypad_led_changed`
            events, and key events that piggy-back LED fields — uses
            the same `(is_on, brightness, rgb)` triplet, so a single
            parse covers all three (PROTOCOL_PROPOSALS §P2)."""
            if self.PROP_IS_ON in data:
                self._is_on = bool(data[self.PROP_IS_ON])
            if self.PROP_BRIGHTNESS in data:
                self._brightness = int(data[self.PROP_BRIGHTNESS])
            rgb = data.get(self.PROP_RGB)
            if isinstance(rgb, dict):
                self._rgb = (
                    int(rgb.get("r", self._rgb[0])),
                    int(rgb.get("g", self._rgb[1])),
                    int(rgb.get("b", self._rgb[2])),
                )
            self.update()

        async def set_led(
            self,
            *,
            is_on: bool | None = None,
            brightness: int | None = None,
            rgb: tuple[int, int, int] | None = None,
            effect: str | None = None,
            duration_ms: int | None = None,
        ) -> None:
            """Per PROTOCOL_PROPOSALS §P2.5. All non-`key_id` fields
            optional; absent fields leave the corresponding LED
            property unchanged. `effect` requires `duration_ms`."""
            data: dict = {TagoKeypad.PROP_KEY_ID: self._key_id}
            if is_on is not None:
                data[self.PROP_IS_ON] = bool(is_on)
            if brightness is not None:
                data[self.PROP_BRIGHTNESS] = max(
                    self.BRIGHTNESS_MIN, min(self.BRIGHTNESS_MAX, int(brightness))
                )
            if rgb is not None:
                r, g, b = rgb
                data[self.PROP_RGB] = {
                    "r": max(0, min(255, int(r))),
                    "g": max(0, min(255, int(g))),
                    "b": max(0, min(255, int(b))),
                }
            if effect is not None:
                data[self.PROP_EFFECT] = effect
            if duration_ms is not None:
                data[self.PROP_DURATION] = max(
                    self.EFFECT_DURATION_MIN_MS,
                    min(self.EFFECT_DURATION_MAX_MS, int(duration_ms)),
                )
            await self._keypad.send_request(req=self.REQ_SET_LED, data=data)

        async def turn_on(self) -> None:
            """Light the LED at its current brightness/rgb."""
            await self.set_led(is_on=True)

        async def turn_off(self) -> None:
            await self.set_led(is_on=False)

        async def flash(self, duration_ms: int) -> None:
            await self.set_led(effect=self.EFFECT_FLASH, duration_ms=duration_ms)

        async def press(self, duration_ms: int | None = None) -> None:
            """PROTOCOL_PROPOSALS §P2.5. Trigger a virtual press of this
            key — firmware emits the normal key_pressed / key_released /
            gesture sequence and any bound automation fires.
            `duration_ms` overrides the firmware default tap length;
            clamped to [1, 60000]."""
            data: dict = {TagoKeypad.PROP_KEY_ID: self._key_id}
            if duration_ms is not None:
                data[self.PROP_DURATION] = max(1, min(60000, int(duration_ms)))
            await self._keypad.send_request(req=self.REQ_PRESS, data=data)


class TagoVirtualSwitch(TagoEntity):
    """A virtual on/off switch — flipped by HA to signal something to the
    firmware's automation engine. PROTOCOL_PROPOSALS §P3.

    `name` is set by the device; `location` is fixed to `"VIRTUAL"`."""

    VIRTUAL_SWITCH = "virtual_switch"
    LOCATION_VIRTUAL = "VIRTUAL"
    types = [VIRTUAL_SWITCH]

    REQ_TURN_ON = "turn_on"
    REQ_TURN_OFF = "turn_off"
    REQ_TOGGLE = "toggle"
    PROP_IS_ON = "is_on"
    PROP_INDEX = "index"

    def __init__(self, json: dict, device: TagoDevice):
        self._is_on: bool = False
        self._index: int = int(json.get(self.PROP_INDEX, 0) or 0)
        super().__init__(json, device)

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def index(self) -> int:
        return self._index

    async def turn_on(self) -> None:
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self) -> None:
        await self.send_request(req=self.REQ_TURN_OFF)

    async def toggle(self) -> None:
        await self.send_request(req=self.REQ_TOGGLE)

    def handle_state_change(self, data: dict) -> None:
        if self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        super().handle_state_change(data)


class TagoSensor(TagoEntity):
    """A read-only physical sensor (motion, contact, occupancy, etc.).
    PROTOCOL_PROPOSALS §P4.

    The firmware reports an `is_on` boolean on `list_nodes`/`get_state`
    and emits `state_changed` whenever the underlying input flips. The
    wire `type` carries which kind of sensor it is (`sensor_motion`,
    `sensor_door`, ...); HA maps that to the right `BinarySensorDeviceClass`.

    Write commands (`turn_on`/`turn_off`/`toggle`/`set_light`) are
    rejected by the firmware with status 500 — sensors are the device's
    eyes, not its hands."""

    SENSOR_LIGHT = "sensor_light"
    SENSOR_MOTION = "sensor_motion"
    SENSOR_OCCUPANCY = "sensor_occupancy"
    SENSOR_OPENING = "sensor_opening"
    SENSOR_PRESENCE = "sensor_presence"
    SENSOR_DOOR = "sensor_door"
    SENSOR_WINDOW = "sensor_window"

    types = [
        SENSOR_LIGHT, SENSOR_MOTION, SENSOR_OCCUPANCY, SENSOR_OPENING,
        SENSOR_PRESENCE, SENSOR_DOOR, SENSOR_WINDOW,
    ]

    PROP_IS_ON = "is_on"

    def __init__(self, json: dict, device: TagoDevice):
        self._is_on: bool = False
        super().__init__(json, device)

    @property
    def is_on(self) -> bool:
        return self._is_on

    def handle_state_change(self, data: dict) -> None:
        if self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        super().handle_state_change(data)


class TagoVirtualSensor(TagoEntity):
    """A read-only virtual binary sensor — flipped by the firmware's
    automation engine to signal something to HA. PROTOCOL_PROPOSALS §P3.

    `name` is set by the device; `location` is fixed to `"VIRTUAL"`.
    Write commands (`turn_on`/`turn_off`/`toggle`/`set_config`) are
    rejected by the firmware with status 500."""

    VIRTUAL_SENSOR = "virtual_sensor"
    LOCATION_VIRTUAL = "VIRTUAL"
    types = [VIRTUAL_SENSOR]

    PROP_IS_ON = "is_on"
    PROP_INDEX = "index"

    def __init__(self, json: dict, device: TagoDevice):
        self._is_on: bool = False
        self._index: int = int(json.get(self.PROP_INDEX, 0) or 0)
        super().__init__(json, device)

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def index(self) -> int:
        return self._index

    def handle_state_change(self, data: dict) -> None:
        if self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        super().handle_state_change(data)
