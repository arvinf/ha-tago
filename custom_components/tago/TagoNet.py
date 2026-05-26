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
    def content(self):
        return self.data

    @property
    def source(self):
        return self.src

    @property
    def reference(self):
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
    REQ_GET_CONFIG = "get_config"
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
        name = json.get(TagoEntity.PROP_NAME)
        if isinstance(name, str):
            name = name.strip() or None

        location = json.get(TagoEntity.PROP_LOCATION)
        if isinstance(location, str):
            location = location.strip() or None

        self._name: str | None = name
        self._location: str | None = location
        self._type: str = json.get(TagoEntity.PROP_TYPE, self.VALUE_UNUSED)
        self._fault: list[str] = list()
        self._tag = json.get(TagoEntity.PROP_TAG)
        rsi = json.get(TagoEntity.PROP_RSI)
        self._rsi: int | None = int(rsi) if isinstance(rsi, (int, float)) and not isinstance(rsi, bool) else None

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

    def _apply_rsi(self, payload: dict) -> None:
        rsi = payload.get(TagoEntity.PROP_RSI)
        if isinstance(rsi, (int, float)) and not isinstance(rsi, bool):
            self._rsi = int(rsi)

    def update_from_discovery_payload(self, payload: dict) -> None:
        name = payload.get(TagoEntity.PROP_NAME)
        if isinstance(name, str):
            name = name.strip() or None

        location = payload.get(TagoEntity.PROP_LOCATION)
        if isinstance(location, str):
            location = location.strip() or None

        self._name = name
        self._location = location
        self._type = payload.get(TagoEntity.PROP_TYPE, self._type)
        self._tag = payload.get(TagoEntity.PROP_TAG, self._tag)
        self._apply_rsi(payload)

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

    async def handle_event(self, msg: TagoMessage) -> None:
        if msg.is_event(self.EVT_STATE_CHANGED):
            await self.handle_state_change(msg)
        elif msg.is_event(self.EVT_CONFIG_CHANGED):
            await self.handle_config_change(msg)

    async def handle_state_change(self, msg: TagoMessage) -> None:
        # PROTOCOL_PROPOSALS §P6: rsi can land on any state_changed for
        # entities on a wireless link.
        if isinstance(msg.content, dict):
            self._apply_rsi(msg.content)
        self.update()

    async def handle_config_change(self, msg: TagoMessage) -> None:
        if isinstance(msg.content, dict):
            self._apply_rsi(msg.content)
        self.update()

    async def _handle_message(self, msg: TagoMessage) -> None:
        if msg.is_event():
            await self.handle_event(msg)
        elif msg.is_response(self.REQ_GET_STATE):
            await self.handle_state_change(msg)
        elif msg.is_response(self.REQ_GET_CONFIG):
            await self.handle_config_change(msg)

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


class TagoDevice(TagoBase):
    REQ_LIST_NODES = 'list_nodes'
    REQ_DEVICE_REBOOT = 'reboot'
    REQ_DEVICE_IDENTIFY = 'identify'
    PROP_NODES = 'nodes'
    PROP_LOADS = 'loads'
    # Protocol extensions (PROTOCOL_PROPOSALS.md): additional collection
    # keys sibling to `loads` in each loads-group node.
    PROP_SCENES = 'scenes'
    PROP_KEYPADS = 'keypads'
    PROP_KEYPAD_LEDS = 'keypad_leds'
    PROP_VIRTUAL_SWITCHES = 'virtual_switches'
    PROP_VIRTUAL_SENSORS = 'virtual_sensors'
    # PROTOCOL_PROPOSALS §P4 + §P5: real sensors collection + firmware
    # update availability field on the device-level get_config response.
    PROP_SENSORS = 'sensors'
    PROP_FIRMWARE_REV = 'firmware_rev'
    PROP_LATEST_FIRMWARE_REV = 'latest_firmware_rev'
    AUTH_HEADER = 'x-tago-auth'
    AUTH_LEGACY = 'legacy'
    AUTH_HMAC_TLS_V2 = 'hmac_tls_v2'

    def __init__(self, hoststr: str, authkey: str = None, useSSL: bool = False):
        super().__init__(None)
        self._usessl = useSSL
        self._hoststr = hoststr
        self._authkey: str = authkey
        self._modelnum: str = None
        self._serialnum: str = None
        self._firmware_rev: str = None
        self._latest_firmware_rev: str | None = None
        self._name = None
        self._ca: str = None
        self._ws: ClientConnection = None
        self._task: asyncio.Task = None
        self._running: bool = False
        self._startup_future: asyncio.Future[None] | None = None
        self._entities: list[TagoEntity] = list()
        self._log_throttle_interval_s = 60.0
        self._log_throttle_last: dict[str, float] = {}
        self._log_throttle_suppressed: dict[str, int] = {}
        # Pending futures keyed by request `ref`. Populated by send_request
        # when a responseTimeout is set; resolved by the message dispatch
        # loop in connection_task when a matching response arrives.
        self._pending_responses: dict[str, asyncio.Future[TagoMessage]] = {}

    @property
    def dashboard_uri(self):
        ssl = 's' if self._usessl else ''
        return f'http{ssl}://{self._hoststr}/'

    @property
    def uri(self):
        ssl = 's' if self._usessl else ''
        return f'ws{ssl}://{self._hoststr}/api/v1/ws'

    @property
    def model_num(self):
        return self._modelnum

    @property
    def serial_num(self):
        return self._serialnum

    @property
    def firmware_rev(self):
        return self._firmware_rev

    @property
    def latest_firmware_rev(self) -> str | None:
        """PROTOCOL_PROPOSALS §P5: latest firmware revision the device is
        aware of. None when the device hasn't reported one (or it equals
        the running revision — see `firmware_update_available`)."""
        return self._latest_firmware_rev

    @property
    def firmware_update_available(self) -> bool:
        """True iff `latest_firmware_rev` is set, parseable, and strictly
        greater than `firmware_rev` (dotted-decimal compare). Equal or
        missing values mean "up to date" — see PROTOCOL_PROPOSALS §P5."""
        if not self._latest_firmware_rev or not self._firmware_rev:
            return False

        def _parse(v: str) -> tuple[int, ...] | None:
            try:
                return tuple(int(p) for p in v.split("."))
            except (ValueError, AttributeError):
                return None

        current = _parse(self._firmware_rev)
        latest = _parse(self._latest_firmware_rev)
        if current is None or latest is None:
            return False
        return latest > current

    @property
    def manufacturer(self):
        return 'TAGO'

    @property
    def entities(self):
        return self._entities

    @property
    def name(self):
        return self._name or f'Device {self.unique_id}'

    @property
    def is_connected(self):
        return self._ws is not None

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

    async def _authenticate_legacy(self, ws: ClientConnection) -> dict[str, str | None]:
        """Per PROTOCOL.md §2 the identity envelope carries
        `{status, nonce, serialnum, model, id}` — no `firmware` field. The
        firmware revision is read separately via device-level `get_config`
        (§9) once the connection is up."""
        await ws.send('{}')
        msg = json.loads(await ws.recv())

        status = msg.get('status', 0)
        serialnum = msg.get('serialnum')
        model_num = msg.get('model')
        entity_id = msg.get('id')

        if status != 200:
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
                'auth': authcode
            }))

            msg = json.loads(await ws.recv())
            if msg.get('status', 0) != 200:
                raise PermissionError('Legacy login failed')

            serialnum = serialnum or msg.get('serialnum')
            model_num = model_num or msg.get('model')
            entity_id = entity_id or msg.get('id')

        return {
            'serialnum': serialnum,
            'model': model_num,
            'id': entity_id,
        }

    async def _authenticate_hmac_tls_v2(
        self, ws: ClientConnection, headers: dict[str, str]
    ) -> dict[str, str | None]:
        raise PermissionError(
            'Device requires auth mode hmac_tls_v2, which is not implemented in this integration version'
        )

    async def _authenticate_connection(self, ws: ClientConnection) -> dict[str, str | None]:
        headers = self._get_server_handshake_headers(ws)
        auth_mode = self._select_auth_strategy(headers)
        logging.debug('Selected auth mode "%s" for %s', auth_mode, self._hoststr)

        if auth_mode == self.AUTH_HMAC_TLS_V2:
            return await self._authenticate_hmac_tls_v2(ws, headers)

        return await self._authenticate_legacy(ws)

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

    async def _refresh_entities_from_list_nodes(self, ws: ClientConnection) -> None:
        """Refresh entities from list_nodes response."""
        await self.send_request(req=TagoDevice.REQ_LIST_NODES)
        async for message in ws:
            logging.debug(f"=== incoming {message}")
            msg = TagoMessage.from_payload(message)
            if not msg.is_response([TagoDevice.REQ_LIST_NODES]):
                continue

            existing_entities = {entity.unique_id: entity for entity in self._entities}
            refreshed_entities: list[TagoEntity] = []

            # PROTOCOL.md §9 list_nodes returns a `nodes` map keyed by
            # loads-group entity ID. Each group's value is a dict
            # containing arrays of entities under documented keys:
            #   loads, scenes, keypads, keypad_leds, virtual_switches,
            #   virtual_sensors (the last five from PROTOCOL_PROPOSALS).
            for key, value in msg.data.get(TagoDevice.PROP_NODES, dict()).items():
                self._extract_entities_from_group(
                    value, existing_entities, refreshed_entities,
                )

            self._entities = refreshed_entities
            return

    def _extract_entities_from_group(
        self,
        group: dict,
        existing_entities: dict,
        refreshed_entities: list,
    ) -> None:
        """Walk every documented collection inside a loads-group and
        materialise/refresh entities."""
        COLLECTIONS = (
            TagoDevice.PROP_LOADS,
            TagoDevice.PROP_SCENES,
            TagoDevice.PROP_KEYPADS,
            TagoDevice.PROP_KEYPAD_LEDS,
            TagoDevice.PROP_VIRTUAL_SWITCHES,
            TagoDevice.PROP_VIRTUAL_SENSORS,
            TagoDevice.PROP_SENSORS,
        )
        for collection_key in COLLECTIONS:
            for item in group.get(collection_key, list()):
                try:
                    entity_id = item.get(TagoEntity.PROP_ID)
                    if not entity_id:
                        continue
                    created_entity = self._build_entity_from_payload(item)
                    existing = existing_entities.get(entity_id)
                    if existing is not None and type(existing) is type(created_entity):
                        existing.update_from_discovery_payload(item)
                        refreshed_entities.append(existing)
                    else:
                        refreshed_entities.append(created_entity)
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
        if TagoKeypadKey.is_of_type(t):
            return TagoKeypadKey(item, self)
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

    async def connection_task(self) -> None:
        self._running = True
        # Tracks whether we've logged the "device unavailable" message for
        # the *current* outage. Reset to False on every successful connect
        # and set to True the first time we fail to connect. Implements the
        # Silver-tier `log-when-unavailable` rule (log once per transition).
        unavailable_logged = False
        while self._running:
            was_connected = False
            try:
                logging.debug(f"connecting to {self.uri}")
                if self._usessl:
                    ssl_context = await self.get_ssl_context()
                else:
                    ssl_context = None
                async with wsconnect(uri=self.uri, ping_timeout=1, ping_interval=3, close_timeout=5, ssl=ssl_context) as ws:
                    logging.debug(f"connected to {self.uri}")
                    self._ws = ws
                    try:
                        login_data = await self._authenticate_connection(ws)
                        self._serialnum = login_data.get('serialnum')
                        self._modelnum = login_data.get('model')
                        # PROTOCOL.md §4: the device gateway entity ID is the
                        # value of `id` in the identity envelope (opaque).
                        # Fall back to serialnum only if `id` is missing
                        # (legacy / non-conformant fakes).
                        self._eid = login_data.get('id') or self._serialnum
                        self.update()

                    except PermissionError as err:
                        self._running = False
                        auth_err = PermissionError('Auth failed')
                        self._signal_startup_error(auth_err)
                        raise auth_err from err

                    # refresh entities list and types
                    await self._refresh_entities_from_list_nodes(ws)

                    # PROTOCOL.md §9 `get_config` carries firmware_rev. The
                    # identity envelope (§2) does not. Fire-and-forget;
                    # populate firmware_rev when the response arrives in the
                    # message loop below.
                    await self.send_request(req=TagoBase.REQ_GET_CONFIG, dst=self._eid)

                    # connected to device!
                    was_connected = True
                    self._signal_startup_success()
                    if unavailable_logged:
                        logging.info(
                            "Tago device %s is available again",
                            self._hoststr,
                        )
                        unavailable_logged = False
                    for entity in self._entities:
                        await entity.connection_state_changed(True)
                    self.update()

                    # process all messages from device
                    async for message in ws:
                        msg = TagoMessage.from_payload(message)

                        # Resolve any pending send_request(responseTimeout=)
                        # future whose ref matches.
                        if msg.ref and msg.ref in self._pending_responses:
                            future = self._pending_responses.pop(msg.ref)
                            if not future.done():
                                future.set_result(msg)

                        # Pick up firmware_rev from the device-level
                        # get_config response (PROTOCOL.md §9) and the
                        # latest-known firmware_rev (PROTOCOL_PROPOSALS §P5)
                        # from either the response or a config_changed event
                        # on the gateway entity.
                        if (msg.src == self._eid
                                and isinstance(msg.data, dict)
                                and (msg.rsp == TagoBase.REQ_GET_CONFIG
                                     or msg.evt == TagoBase.EVT_CONFIG_CHANGED)):
                            fw = msg.data.get(TagoDevice.PROP_FIRMWARE_REV)
                            latest = msg.data.get(TagoDevice.PROP_LATEST_FIRMWARE_REV)
                            changed = False
                            if fw is not None and fw != self._firmware_rev:
                                self._firmware_rev = fw
                                changed = True
                            if latest is not None and latest != self._latest_firmware_rev:
                                self._latest_firmware_rev = latest
                                changed = True
                            if changed:
                                self.update()

                        handlers: list[Awaitable[None]] = []
                        for entity in self._entities:
                            try:
                                handler = entity.handle_message(msg)
                                if handler is not None:
                                    handlers.append(handler)
                            except Exception as e:
                                self._log_exception_throttled(
                                    key='entity_message_prepare_error',
                                    message='Entity message scheduling error',
                                    err=e,
                                )

                        if handlers:
                            results = await asyncio.gather(*handlers, return_exceptions=True)
                            for result in results:
                                if isinstance(result, Exception):
                                    self._log_exception_throttled(
                                        key='entity_message_error',
                                        message='Entity message handling error',
                                        err=result,
                                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._signal_startup_error(e)
                self._log_exception_throttled(
                    key='connection_loop_error',
                    message='Connection loop error',
                    err=e,
                )
                pass

            self._ws = None

            # Log "device unavailable" once per outage. `was_connected`
            # distinguishes "we had a session and lost it" from "we've
            # never reached the device" — log accordingly.
            if not unavailable_logged:
                if was_connected:
                    logging.warning(
                        "Tago device %s became unavailable", self._hoststr,
                    )
                else:
                    logging.warning(
                        "Tago device %s is unavailable", self._hoststr,
                    )
                unavailable_logged = True

            # notify disconnection
            if was_connected:
                for entity in self._entities:
                    try:
                        await entity.connection_state_changed(False)
                    except Exception as e:
                        logging.exception(e)
            self.update()

            if self._running:
                await asyncio.sleep(3)

    async def reboot(self):
        if self.is_connected == False:
            return

        await self.send_request(req=TagoDevice.REQ_DEVICE_REBOOT, dst=self._eid)

    async def identify(self):
        if self.is_connected == False:
            return

        await self.send_request(req=TagoDevice.REQ_DEVICE_IDENTIFY, dst=self._eid)


class TagoSwitch(TagoEntity):
    OUTLET_ONOFF = "outlet_onoff"

    types = [OUTLET_ONOFF]

    REQ_TURN_ON = "turn_on"
    REQ_TURN_OFF = "turn_off"
    PROP_IS_ON = "is_on"

    def __init__(self, json: dict, device: TagoDevice):
        super().__init__(json, device)
        self._is_on: bool = bool(json.get(self.PROP_IS_ON, False))

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def turn_on(self):
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self):
        await self.send_request(req=self.REQ_TURN_OFF)

    async def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        # PROTOCOL.md §12.3: on/off entities expose `is_on` (bool) only.
        if isinstance(data, dict) and self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        await super().handle_state_change(msg)


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
        super().__init__(json, device)
        self._brightness: int = 0
        self._colour_x: float = 0.0
        self._colour_y: float = 0.0
        self._ct: float = 0.0
        self._ct_range_min: int = TagoLight.CT_MIN
        self._ct_range_max: int = TagoLight.CT_MAX
        self._ramp: Ramp = None
        self.parse_state_json(json)

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

    def parse_state_json(self, data: dict) -> None:
        # `is_on` carries on/off state for `light_onoff`; mirror it into
        # `_brightness` so callers that read `.brightness > 0` (e.g.
        # TagoLightHA.is_on) still work.
        if self.PROP_IS_ON in data:
            self._brightness = self.MAX_VALUE if bool(data[self.PROP_IS_ON]) else 0

        self._brightness = data.get(self.PROP_BRIGHTNESS, self._brightness)
        self._ct = data.get(self.PROP_CT, self._ct)
        # PROTOCOL.md §13.1: ct_range is a 2-element [warm_K, cool_K] list.
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

    async def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content

        # cancel any running ramps
        if self._ramp:
            self._ramp.cancel()
            self._ramp = None

        self.parse_state_json(msg.content)

        # if a ramp is active, 'animate' the value change by generating
        # periodic updates
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

        await super().handle_state_change(msg)

    async def handle_config_change(self, msg: TagoMessage) -> None:
        data = msg.content
        ct_range = data.get(self.PROP_CT_RANGE)
        if isinstance(ct_range, list) and len(ct_range) == 2:
            self._ct_range_min = max(int(ct_range[0]), TagoLight.CT_MIN)
            self._ct_range_max = min(int(ct_range[1]), TagoLight.CT_MAX)
        await super().handle_config_change(msg)


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
        super().__init__(json, device)
        self._position = int(json.get("position", 0))
        self._target = int(json.get("target", 0))

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

    async def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        self._position = data.get("position", self._position)
        self._target = data.get("target", self._target)
        await super().handle_state_change(msg)


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
        super().__init__(json, device)
        self._is_on: bool = bool(json.get(self.PROP_IS_ON, False))

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def turn_on(self):
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self):
        await self.send_request(req=self.REQ_TURN_OFF)

    async def toggle(self):
        await self.send_request(req=self.REQ_TOGGLE)

    async def handle_state_change(self, msg: TagoMessage) -> None:
        # PROTOCOL.md §12.3: on/off entities expose `is_on` (bool) only.
        data = msg.content
        if isinstance(data, dict) and self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        await super().handle_state_change(msg)


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
        super().__init__(json, device)
        self._last_activated_ts: int = int(json.get(self.PROP_LAST_ACTIVATED_TS, 0) or 0)
        self._scene_activated_cb = None

    @property
    def last_activated_ts(self) -> int:
        return self._last_activated_ts

    def set_on_scene_activated(self, callback) -> None:
        """Called whenever a `scene_activated` event fires for this scene.
        Used by the HA scene platform to fire a HA-bus event so automations
        can listen for scene activation across the whole device."""
        self._scene_activated_cb = callback

    async def activate(self) -> None:
        await self.send_request(req=self.REQ_ACTIVATE)

    async def handle_event(self, msg: TagoMessage) -> None:
        if msg.is_event(self.EVT_SCENE_ACTIVATED):
            data = msg.content
            if isinstance(data, dict):
                ts = data.get("ts")
                if isinstance(ts, int):
                    self._last_activated_ts = ts
            self.update()
            if self._scene_activated_cb is not None:
                await self._scene_activated_cb(msg)
            return
        await super().handle_event(msg)


class TagoKeypad(TagoEntity):
    """A physical keypad with N keys. PROTOCOL_PROPOSALS §P2.

    Not exposed as an HA entity — the integration registers each keypad as
    a `device_registry` entry (so users see it as a device card) and
    forwards key events to HA's bus. Per-key data lives in the `keys`
    list; the keypad's own LED light is a separate `TagoKeypadKey`
    entity referenced by `led_id`."""

    KEYPAD_MODULAR = "keypad_modular"
    types = [KEYPAD_MODULAR]

    PROP_MODEL_NUM = "model_num"
    PROP_KEYS = "keys"    
    PROP_KEYPAD_ID = "keypad_id"

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
    
    def __init__(self, json: dict, device: TagoDevice):
        super().__init__(json, device)
        self._model_num: str = json.get(self.PROP_MODEL_NUM, "") or ""
        self._leds: list[str] = [TagoKeypad.TagoKeypadKey(self, k) for k in json.get(self.PROP_KEYS, [])]
        self._key_event_cb = None

    @property
    def model_num(self) -> str:
        return self._model_num

    @property
    def keys(self) -> list[str]:
        return list(self._keys)
    
    def leds(self) -> list[TagoKeypad.TagoKeypadKey]:
        return list(self._leds)

    def set_on_key_event(self, callback) -> None:
        """Called for every key_* event arriving for this keypad. Used by
        the HA-side keypad device wiring to fan events out to hass.bus."""
        self._key_event_cb = callback

    async def handle_event(self, msg: TagoMessage) -> None:
        if msg.evt in self.KEY_EVENTS:
            if self._key_event_cb is not None:
                await self._key_event_cb(msg)
            return
        await super().handle_event(msg)

    class TagoKeypadKey:
        """The LED on a keypad — addressable via `set_led`. PROTOCOL_PROPOSALS
        §P2.5. RGB is the only color mode. Supports a `flash` effect with
        user-supplied duration."""

        REQ_SET_LED = "set_led"
        REQ_PRESS = "press"

        PROP_IS_ON = "is_on"
        PROP_BRIGHTNESS = "brightness"
        PROP_RGB = "rgb"
        PROP_EFFECT = "effect"
        PROP_DURATION = "duration"
        PROP_KEY_ID = "key_id"

        EFFECT_FLASH = "flash"

        BRIGHTNESS_MIN = 0
        BRIGHTNESS_MAX = 1000
        EFFECT_DURATION_MIN_MS = 100
        EFFECT_DURATION_MAX_MS = 60000

        def __init__(self, keypad: TagoKeypad, json: dict):
            self._keypad = keypad
            self._key_id = json.get(self.PROP_KEY_ID)
            self._is_on: bool = bool(json.get(self.PROP_IS_ON, False))
            self._brightness: int = int(json.get(self.PROP_BRIGHTNESS, 0) or 0)
            rgb = json.get(self.PROP_RGB) or {}
            self._rgb: tuple[int, int, int] = (
                int(rgb.get("r", 0)),
                int(rgb.get("g", 0)),
                int(rgb.get("b", 0)),
            )

        @property
        def keypad(self) -> str:
            return self._keypad
        
        @property
        def key_id(self) -> str:
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
        
        async def press (
            self,
            duration_ms: int | None = None,
        ) -> None:
            """Convenience: to trigger a virtual key press."""
            await self.set_led(req=self.REQ_SET_LED, duration_ms=duration_ms)

        async def set_led(
            self,
            *,
            is_on: bool | None = None,
            brightness: int | None = None,
            rgb: tuple[int, int, int] | None = None,
            effect: str | None = None,
            duration_ms: int | None = None,
        ) -> None:
            """Per PROTOCOL_PROPOSALS §P2.5. All args optional; absent fields
            leave the corresponding LED property unchanged. `effect` and
            `duration_ms` form a pair — either both or neither."""
            data: dict = {}
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
            if effect is not None or duration_ms is not None:
                # Pair must come together — let the firmware reject mismatched
                # halves rather than silently dropping one.
                if effect is not None:
                    data[self.PROP_EFFECT] = effect
                if duration_ms is not None:
                    data[self.PROP_DURATION] = max(
                        self.EFFECT_DURATION_MIN_MS,
                        min(self.EFFECT_DURATION_MAX_MS, int(duration_ms)),
                    )

            data[TagoKeypad.PROP_KEY_ID] = self.key_id
            await self.keypad.send_request(req=self.REQ_SET_LED, data=data)

        async def flash(self, duration_ms: int) -> None:
            """Convenience: trigger the flash effect for `duration_ms`."""
            await self.set_led(effect=self.EFFECT_FLASH, duration_ms=duration_ms)

        async def handle_state_change(self, msg: TagoMessage) -> None:
            data = msg.content
            if isinstance(data, dict):
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
            await super().handle_state_change(msg)


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
        super().__init__(json, device)
        self._is_on: bool = bool(json.get(self.PROP_IS_ON, False))
        self._index: int = int(json.get(self.PROP_INDEX, 0) or 0)

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

    async def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        if isinstance(data, dict) and self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        await super().handle_state_change(msg)


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
        super().__init__(json, device)
        self._is_on: bool = bool(json.get(self.PROP_IS_ON, False))

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        if isinstance(data, dict) and self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        await super().handle_state_change(msg)


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
        super().__init__(json, device)
        self._is_on: bool = bool(json.get(self.PROP_IS_ON, False))
        self._index: int = int(json.get(self.PROP_INDEX, 0) or 0)

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def index(self) -> int:
        return self._index

    async def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        if isinstance(data, dict) and self.PROP_IS_ON in data:
            self._is_on = bool(data[self.PROP_IS_ON])
        await super().handle_state_change(msg)
