from __future__ import annotations

import asyncio
from collections.abc import Callable
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

        return self

    def get_message(self) -> str:
        data = self.data
        if self.dst:
            data[TagoMessage.PROP_DST] = self.dst
        data[TagoMessage.PROP_REF] = TagoMessage.create_random_str()
        data[TagoMessage.PROP_REQ] = self.req

        msg = json.dumps(data)
        #print('<< ' + str(msg))
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
    EVT_MODBUS = "modbus_evt"
    EVT_KEYPAD = "keypad_evt"    
    EVT_MOTION = "motion_evt"
    EVT_IO = "io_evt"
        
    STATE_ON = "ON"
    STATE_OFF = "OFF"

    def __init__(self, eid: str):
        self._eid: str = eid
        self._update_cb = None

    def set_on_state_changed(self, callback):
        self._update_cb = callback

    def update(self) -> None:
        if self._update_cb:
            self._update_cb()

    @property
    def unique_id(self):
        return self._eid


class TagoEntity(TagoBase):
    EVT_KEYPRESS = "key_pressed"
    EVT_KEYRELEASE = "key_released"
    VALUE_UNUSED = 'UNUSED'
    MAX_VALUE = 1000


    types = []

    def __init__(self, json: dict, device: TagoDevice):
        super().__init__(json[TagoEntity.PROP_ID])
        self._device: TagoDevice = device
        self._name: str = json.get(TagoEntity.PROP_NAME)
        self._location: str = json.get(TagoEntity.PROP_LOCATION)
        self._type: str = json.get(TagoEntity.PROP_TYPE, self.VALUE_UNUSED)
        self._fault: list[str] = list()
        self._tag = json.get(TagoEntity.PROP_TAG)

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

    def is_unused(self) -> bool:
        return self.type == self.VALUE_UNUSED

    async def connection_state_changed(self, connected: bool) -> None:
        self.update()
        if connected:
            # request state refresh
            await self.send_request(req=self.REQ_GET_STATE)        

    async def send_request(self, req: str, data: dict = {}) -> None:
        await self._device.send_request(req=req, dst=self._eid, data=data)

    def handle_event(self, msg: TagoMessage) -> None:
        if msg.is_event(self.EVT_STATE_CHANGED):
            self.handle_state_change(msg)
        elif msg.is_event(self.EVT_CONFIG_CHANGED):
            self.handle_config_change(msg)

    async def handle_message(self, msg: TagoMessage) -> None:
        if msg.source != self._eid:
            return

        if msg.is_event():
            self.handle_event(msg)
        elif msg.is_response(self.REQ_GET_STATE):
            self.handle_state_change(msg)
        elif msg.is_response(self.REQ_GET_CONFIG):
            self.handle_config_change(msg)

    def handle_state_change(self, msg: TagoMessage) -> None:
        self.update()

    def handle_config_change(self, msg: TagoMessage) -> None:
        self.update()

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

    def __init__(self, hoststr: str, authkey: str = None, useSSL: bool = False):
        super().__init__(None)
        self._usessl = useSSL
        self._hoststr = hoststr
        self._authkey: str = authkey
        self._modelnum: str = None
        self._serialnum: str = None
        self._firmware_rev: str = None
        self._name = None
        self._ca: str = None
        self._ws: ClientConnection = None
        self._task: asyncio.Task = None
        self._running: bool = False
        self._connected_flag = asyncio.Event()
        self._disconnected_flag = asyncio.Event()
        self._entities: list[TagoEntity] = list()

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

    async def connect(self, timeout: float | None = None) -> None:
        """Connect function that waits for connection or error with optional timeout."""
        # Create the two event flags
        connected = asyncio.Event()
        autherror = asyncio.Event()

        self._running = False
        if self._ws:
            self._ws.close()
        if self._task:
            self._task.cancel()

        self._task = asyncio.create_task(
            self.connection_task(connected, autherror))
        # try:
        # Wait for either connected or error to be set, with optional timeout
        done, pending = await asyncio.wait(
            [asyncio.create_task(connected.wait()),
             asyncio.create_task(autherror.wait())],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=timeout
        )

        # Check if timeout occurred
        if not done:
            self._task.cancel()
            raise TimeoutError("Connection timed out")

        # Check which event was set
        if connected.is_set():
            return  # Success case
        if autherror.is_set():
            self._task.cancel()
            raise PermissionError("Authentication failed")

    async def disconnect(self, timeout: float | None = None) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

        if timeout is None:
            await self._task
        else:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except TimeoutError:
                raise TimeoutError(
                    "Timed out waiting for self._task to complete")

        if self._task.exception() is not None:
            raise self._task.exception()
        self._task = None

    async def send_request(self, req: str, data: dict = dict(), dst: str = None, responseTimeout: float = None) -> None | TagoMessage:
        if self._ws is None:
            return None

        """ sends a message to peer, and optionally waits for a response to be received or a timeout to occur. """
        msg = TagoMessage.make_request(req=req, dst=dst, data=data)
        resp: TagoMessage = None
        flag = asyncio.Event()

        if responseTimeout:
            async def check_response(respmsg: TagoMessage):
                nonlocal resp
                if respmsg.refers_to(msg.reference):
                    resp = respmsg
                    flag.set()

        payload = msg.get_message()
        logging.debug(f"=== outgoing {payload}")
        await self._ws.send(payload)
        if responseTimeout:
            async with asyncio.timeout(responseTimeout):
                await flag.wait()
                return resp

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

    async def connection_task(self, connected: asyncio.Event, autherror: asyncio.Event) -> None:
        self._running = True
        while self._running:
            try:
                logging.debug(f"connecting to {self.uri}")
                if self._usessl:
                    ssl_context = await self.get_ssl_context()
                else:
                    ssl_context = None
                async with wsconnect(uri=self.uri, ping_timeout=1, ping_interval=3, close_timeout=5, ssl=ssl_context) as ws:
                    logging.debug(f"connected to {self.uri}")
                    self._ws = ws
                    # login
                    try:
                        await ws.send('{}')
                        msg = json.loads(await ws.recv())

                        status = msg.get('status', 0)
                        serialnum = msg.get('serialnum')
                        model_num = msg.get('model')
                        firmware_rev = msg.get('firmware')

                        if status != 200:
                            if msg.get('nonce') is None:
                                raise Exception('No login message from server')

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
                                raise Exception('login failed')

                            ca = msg.get('ca', None)
                            # if refresh_ca and ca:
                            #     ca_hash = msg.get('ca_hash', '')
                            #     sha256 = hashlib.sha256()
                            #     sha256.update(
                            #         (ca + client_nonce + self._authkey).encode('utf-8'))
                            #     hash = sha256.hexdigest()
                            #     if ca_hash == hash:
                            #         self._ca = ca
                            #     else:
                            #         logging.error('unexpected ca hash {ca_hash} {hash}')

                        self._serialnum = serialnum
                        self._modelnum = model_num
                        self._firmware_rev = firmware_rev
                        self._eid = serialnum
                        self.update()

                    except:
                        autherror.set()
                        self._running = False
                        raise PermissionError('Auth failed')
                                        
                    # refresh entities list and types
                    await self.send_request(req=TagoDevice.REQ_LIST_NODES)
                    async for message in ws:
                        logging.debug(f"=== incoming {message}")
                        msg = TagoMessage.from_payload(message)                        
                        if msg.is_response([TagoDevice.REQ_LIST_NODES]):                            
                            for key, value in msg.data.get(TagoDevice.PROP_NODES, dict()).items():                                
                                for item in value.get(TagoDevice.PROP_LOADS, list()):
                                    try: 
                                        entity = None
                                        if TagoLight.is_of_type(item.get(TagoEntity.PROP_TYPE)):
                                            entity = TagoLight(item, self)
                                        elif TagoSwitch.is_of_type(item.get(TagoEntity.PROP_TYPE)):
                                            entity = TagoSwitch(item, self)
                                        elif TagoCover.is_of_type(item.get(TagoEntity.PROP_TYPE)):
                                            entity = TagoCover(item, self)
                                        elif TagoFan.is_of_type(item.get(TagoEntity.PROP_TYPE)):
                                            entity = TagoFan(item, self)
                                        else: ## unused loads
                                            entity = TagoEntity(item, self)

                                        self._entities.append(entity)
                                    except Exception as e:
                                        logging.exception(e)

                            break
                    
                    # connected to device!
                    connected.set()
                    for entity in self._entities:
                        await entity.connection_state_changed(True)
                    self.update()

                    # process all messages from device
                    async for message in ws:
                        msg = TagoMessage.from_payload(message)
                        if msg.src == self._eid:
                            if msg.is_event([TagoDevice.EVT_CONFIG_CHANGED]):
                                pass
                            elif msg.is_event([TagoDevice.EVT_KEYPAD, TagoDevice.EVT_MOTION, TagoDevice.EVT_IO]):
                                self.input_event_message(msg)

                        for entity in self._entities:
                            try:
                                await entity.handle_message(msg)
                            except Exception as e:
                                logging.exception(str(e))

            except Exception as e:
                logging.exception(str(e))
                pass

            self._ws = None

            # notify disconnection
            if connected.is_set():
                for entity in self._entities:
                    try:
                        await entity.connection_state_changed(False)
                    except Exception as e:
                        logging.exception(e)
                connected.clear()
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
    OUTLET = "relay_outlet"
    SWITCH = "relay_switch"

    types = [OUTLET, SWITCH]

    REQ_TURN_ON = "turn_on"
    REQ_TURN_OFF = "turn_off"

    def __init__(self, json: dict, device: TagoDevice):
        super().__init__(json, device)
        self.state = self.STATE_OFF

    async def turn_on(self):
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self):
        await self.send_request(req=self.REQ_TURN_OFF)

    def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        self._state = data.get("state", self._state)
        super().handle_state_change(msg)


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

    def _brightness_param_parse(self, brightness: float, duration: float = None, rate: float = None) -> dict:
        data = {}
        if duration is not None:
            data[self.PROP_DURATION] = int(round((duration * 1000), 0))
        elif rate is not None:
            data[self.PROP_RATE] = int(round((rate * 1000), 0))

        if brightness is not None:
            data[self.PROP_BRIGHTNESS] = self.convert_value_from_float(
                brightness)

        return data

    async def set_light_flash(self, duration: int) -> None:
        """Flash all channels for a specified duration"""
        await self.send_request(req=self.REQ_LIGHT_EFFECT, data={self.PROP_EFFECT: self.VALUE_FLASH, self.PROP_DURATION: duration})

    async def set_brightness(self, brightness: float, duration: float = None, rate: float = None) -> None:
        """Set brightness to specified value between 0.0 and 1.0"""
        if brightness is None:
            raise ValueError('Brightness must be specified')

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
        self._brightness = data.get(self.PROP_BRIGHTNESS, self._brightness)
        self._ct = data.get(self.PROP_CT, self._ct)
        ct_basis = data.get(TagoLight.PROP_CT_RANGE, list())
        if len(ct_basis) > 2:
            self._ct_range_min = max(ct_basis[0], TagoLight.CT_MIN)
            self._ct_range_max = min(ct_basis[1], TagoLight.CT_MAX)
        self._colour_x = data.get(self.PROP_X, self._colour_x)
        self._colour_y = data.get(self.PROP_Y, self._colour_y)

        fault = data.get(self.PROP_FAULT)
        if fault:
            self._fault = fault.split(',')
        else:
            self._fault = list()

    def handle_state_change(self, msg: TagoMessage) -> None:
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

        super().handle_state_change(msg)

    def handle_config_change(self, msg: TagoMessage) -> None:
        data = msg.content
        self._ct_range_min = data.get(self.PROP_CT_RANGE, self._ct_range_min)
        self._ct_range_max = data.get(self.PROP_CT_RANGE, self._ct_range_max)
        super().handle_config_change(msg)


class TagoCover(TagoEntity):
    SHADE = "cover_shades"
    CURTAIN = "cover_curtains"

    types = [SHADE, CURTAIN]

    REQ_STOP = "stop_move"
    REQ_MOVE_TO = "move_to"

    def __init__(self, json: dict, device: TagoDevice):
        super().__init__(json, device)
        self._position = 0
        self._target = 0

    async def move_to(self, target: int):
        await self.send_request(req=self.REQ_MOVE_TO, target=target)

    async def stop_move(self):
        await self.send_request(req=self.REQ_STOP)

    def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        self._position = data.get("position", self._position)
        self._target = data.get("target", self._target)
        super().handle_state_change(msg)


class TagoFan(TagoEntity):
    ONOFF = "fan_onoff"
    DIMMABLE = "fan_adjustable"

    types = [ONOFF, DIMMABLE]

    REQ_SET_FAN = "set_fan"
    REQ_TURN_ON = "turn_on"
    REQ_TURN_OFF = "turn_off"

    def __init__(self, json: dict, device: TagoDevice):
        super().__init__(json, device)
        self._value = 0
        self.state = self.STATE_OFF

    async def turn_on(self):
        await self.send_request(req=self.REQ_TURN_ON)

    async def turn_off(self):
        await self.send_request(req=self.REQ_TURN_OFF)

    async def set_speed(self, percentage: int):
        if percentage == 0:
            await self.turn_off()
            return

        level = math.ceil(percentage_to_ranged_value(
            self.MAX_VALUE, percentage))
        await self.send_request(req=self.REQ_SET_FAN, data={"value": [level]})

    def handle_state_change(self, msg: TagoMessage) -> None:
        data = msg.content
        
        self._value = data.get('value', data.get('brightness', self._value))
        if data.get('is_on', self._value > 0):            
            self.state = self.STATE_ON
        else:
            self.state = self.STATE_OFF
            
        super().handle_state_change(msg)
