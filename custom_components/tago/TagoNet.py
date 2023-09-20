from __future__ import annotations
import logging

from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA512

import asyncio

from typing import Any, Optional, cast, Dict, Callable

import websockets
import json

_LOGGER = logging.getLogger(__name__)


class TagoEntity:
    MSG_STATE_CHANGED = "state_changed"
    MSG_CONFIG_CHANGED = "config_changed"
    MSG_MODBUS_KEYPRESS = "modbus_keypress"

    MSG_DIMMER_AC_ON = "dimmer_ac_on"
    MSG_DIMMER_AC_OFF = "dimmer_ac_off"
    MSG_DIMMER_AC_TOGGLE = "dimmer_ac_toggle"
    MSG_DIMMER_AC_FADE_TO = "dimmer_ac_fade_to"
    MSG_DIMMER_AC_SET_CONFIG = "dimmer_ac_set_config"
    MSG_DIMMER_AC_GET_STATE = "dimmer_ac_get_state"

    MSG_DEVICE_GET_INFO = "device_get_info"
    MSG_IDENTIFY = "identify"
    MSG_REBOOT = "reboot"
    MSG_ERROR = "error"

    TYPE_MODBUS = "modbus"
    TYPE_DIMMER_AC = "dimmer_ac"
    TYPE_DIMMER_0to10V = "dimmer_10v"
    TYPE_LED_DRIVER = "led_driver"

    def __init__(self, data: Dict[str, str]):
        self._uid = data["id"]
        self._name = data.get("name", "Unnamed")
        self._area = data.get("area", "unassigned")
        self._on_updated = None

    @property
    def uid(self) -> str:
        return self._uid

    @property
    def name(self) -> str:
        return self._name

    @property
    def type(self) -> str:
        p = self._uid.split(":")
        if len(p) > 2:
            return p[1]
        else:
            return p[0]

    def process_event(self, type: str, data: Dict[str, str]) -> bool:
        if type == self.MSG_CONFIG_CHANGED:
            self._name = data.get("name", self._name)
            return True

        return False

    def updated(self) -> None:
        if self._on_updated:
            self._on_updated()

    def set_update_handle(self, handler: Callable) -> None:
        self._on_updated = handler


class TagoPeripheral(TagoEntity):
    def __init__(self, data: Dict[str, str], device: TagoDevice):
        super().__init__(data)
        self._device = device
        self._channel = data["channel"]

    async def send_channel_message(
        self, msg: str, data: Dict[str, object] = {}
    ) -> None:
        data["channel"] = self._channel
        await self._device.send_message(msg=msg, data=data)


class TagoBridge(TagoPeripheral):
    def __init__(self, data: Dict[str, str], device: TagoDevice):
        super().__init__(data, device)
        self._message_handler = None

    def set_message_handler(
        self, handler: Callable[[TagoBridge, str, dict[str, str]]]
    ) -> None:
        self._message_handler = handler

    def forward_message(self, entity_id: str, message: dict[str, str]) -> None:
        if self._message_handler is None:
            return

        self._message_handler(self, entity_id=entity_id, message=message)


class TagoLight(TagoPeripheral):
    LIGHT = "light"
    LIGHT_RGB = "light_rgb"
    LIGHT_RGBW = "light_rgbw"
    LIGHT_RGBWW = "light_rgbww"
    LIGHT_WW = "light_ww"

    types = [LIGHT, LIGHT_RGB, LIGHT_RGBW, LIGHT_RGBWW, LIGHT_WW]

    STATE_ON = "on"
    STATE_OFF = "off"

    def __init__(self, data: [str, str], device: TagoDevice):
        super().__init__(data, device)

        self._type = data.get("type", self.LIGHT)
        self._state = data.get("state", self.STATE_OFF)
        self._intensity = data.get("intensity", 0)
        self._edge = data.get("edge", "leading")

    @property
    def is_on(self):
        return self._state == self.STATE_ON

    @staticmethod
    def convert_intensity_to_device(
        intensity: float, srclimit: float = 255
    ) -> int:
        return int(round((intensity * 65535) / srclimit, 0))

    @staticmethod
    def convert_intensity_from_device(
        intensity: float, srclimit: float = 255
    ) -> int:
        return int(round((intensity * srclimit) / 65535, 0))

    @property
    def is_dimmable(self):
        return self.type in [
            self.TYPE_DIMMER_AC,
            self.TYPE_DIMMER_0to10V,
            self.TYPE_LED_DRIVER,
        ]

    async def turn_on(self) -> None:
        await self.send_channel_message(msg=self.MSG_DIMMER_AC_ON)

    async def turn_off(self) -> None:
        await self.send_channel_message(msg=self.MSG_DIMMER_AC_OFF)

    async def toggle(self) -> None:
        await self.send_channel_message(msg=self.MSG_DIMMER_AC_TOGGLE)

    async def fade_to(
        self, intensity: int, time: int = 0, relative: bool = False
    ) -> None:
        data = {
            "intensity": intensity,
        }

        if time is not None:
            data["speed"] = int((65535 * 6) / (int(time * 1000)))

        data["relative"] = relative
        await self.send_channel_message(
            msg=self.MSG_DIMMER_AC_FADE_TO, data=data
        )

    def process_event(self, type: str, data: Dict[str, str]) -> bool:
        super().process_event(type=type, data=data)
        if type == self.MSG_STATE_CHANGED:
            self._intensity = data.get("intensity", self._intensity)
            self._state = data.get("state", self._state)

            return True

        elif type == self.MSG_CONFIG_CHANGED:
            self._name = data.get("name", self._name)
            self._type = data.get("type", self._type)
            self._edge = data.get("edge", self._edge)

            return True

        return False


class TagoModbus(TagoBridge):
    def process_event(self, type: str, data: Dict[str, str]) -> bool:
        super().process_event(type=type, data=data)

        if type == self.MSG_MODBUS_KEYPRESS:
            self.forward_message(
                entity_id=data.get("id", ""),
                message={
                    "type": "keypress_single",
                    "address": data.get("addr", 0),
                    "key": data.get("key", -1),
                    "duration": data.get("duration", 1),
                },
            )

            return True

        return False


class TagoDevice(TagoEntity):
    def __init__(self, host: str):
        super().__init__({"id": host})
        self._host = host
        self._websocket = None
        self._enumerated = False
        self._task = None
        self._running = False

        self._lights: list = list()
        self._bridges: list = list()
        self._children: Dict[str, object] = {}

        self.fw_rev = None
        self.model_name = None
        self.model_desc = None
        self.serial_number = None
        self.manufacturer = "Tago"

    def connect(self) -> None:
        self._task = asyncio.create_task(self._device_task())

    async def close(self) -> None:
        self._running = False
        if self._websocket:
            await self._websocket.close()

        try:
            async with asyncio.timeout(2):
                while self.is_connected:
                    await asyncio.sleep(0.5)
        except TimeoutError:
            self._task.cancel()

    @property
    def is_connected(self) -> bool:
        return self._websocket is not None

    @property
    def lights(self) -> list:
        return self._lights

    @property
    def bridges(self) -> list:
        return self._bridges

    @property
    def is_enumerated(self) -> bool:
        return self._enumerated

    @property
    def host(self) -> str:
        return self._host

    async def send_message(self, msg: str, data: object = {}) -> None:
        data["msg"] = msg
        payload = json.dumps(data)
        _LOGGER.info(f"=== send_message {payload}")
        await self._websocket.send(payload)

    async def msg_device_get_info(self) -> None:
        await self.send_message(msg=self.MSG_DEVICE_GET_INFO)

    def process_device_msg(self, msg: object) -> None:
        _LOGGER.info(f"=== process_device_msg {msg}")
        match msg["msg"]:
            ## device enumeration
            case self.MSG_DEVICE_GET_INFO:
                self._id = msg["id"]
                self._name = msg.get("name")
                self.fw_rev = msg.get("firmware_rev")
                self.model_name = msg.get("model_name")
                self.model_desc = msg.get("model_desc")
                self.serial_number = msg.get("serial_number")

                # Create a child for every enabled output channel on the device
                for item in msg.get(self.TYPE_DIMMER_AC, []):
                    child = None

                    if item.get("type") in TagoLight.types:
                        child = TagoLight(item, self)
                        self._lights.append(child)
                    else:
                        _LOGGER.error(f"unsupported item {item}")

                    if child:
                        self._children[child.uid] = child

                for item in msg.get(self.TYPE_MODBUS, []):
                    child = TagoModbus(item, self)
                    self._bridges.append(child)
                    self._children[child.uid] = child

                self._enumerated = True
            ## If an event has been posted find the target,
            ##  and pass the message to it for processing
            case self.MSG_CONFIG_CHANGED | self.MSG_STATE_CHANGED | self.MSG_MODBUS_KEYPRESS:
                uid = msg.get("id", "")
                target = None
                if uid == self._uid:
                    target = self
                else:
                    target = self._children.get(uid, None)

                if target:
                    if target.process_event(type=msg["msg"], data=msg):
                        target.updated()
                else:
                    _LOGGER.info(f"could not find {uid} in children.")

            case self.MSG_DIMMER_AC_ON | self.MSG_DIMMER_AC_OFF | self.MSG_DIMMER_AC_FADE_TO | self.MSG_DIMMER_AC_TOGGLE | self.MSG_IDENTIFY | self.MSG_REBOOT:
                pass
            case self.MSG_ERROR:
                _LOGGER.error(f"command failed {msg}")
            case _:
                _LOGGER.error(f"unsupported message {msg}.")

    async def handle_ws_msgs(self) -> None:
        async for message in self._websocket:
            msg = json.loads(message)
            self.process_device_msg(msg)

    async def _device_task(self) -> None:
        uri = f"ws://{self._host}/api/v1/ws"
        self._running = True
        while self._running:
            _LOGGER.info(f"connecting to {self._host}")
            try:
                async with websockets.connect(
                    uri=uri, ping_timeout=5, ping_interval=5
                ) as websocket:
                    self._websocket = websocket
                    _LOGGER.info(f"connected to {self._host}")
                    try:
                        if not self._enumerated:
                            await self.msg_device_get_info()

                        await self.handle_ws_msgs()
                    except Exception as e:
                        self._websocket = None
                        _LOGGER.info("==== EXCEPT")
                        _LOGGER.exception(e)
            except Exception as e:
                self._websocket = None
                _LOGGER.exception(e)
            ## small delay between successive attempts to connect
            if self._running:
                await asyncio.sleep(0.5)

    async def identify(self) -> None:
        await self.send_message(msg=self.MSG_IDENTIFY)

    async def reboot(self) -> None:
        await self.send_message(msg=self.MSG_REBOOT)


class TagoNet:
    def __init__(self, network_key: str):
        self._devices: list = list()
        (self._nwk_id, self._nwk_secret) = TagoNet.generate_network_creds(
            network_key
        )

    @property
    def devices(self) -> list():
        return self._devices

    @property
    def zeroconf_services(cls) -> list:
        return ["_tagonet._tcp.local."]

    @classmethod
    def generate_network_creds(cls, key) -> (str, str):
        if len(key.strip()) < 10:
            raise ValidationError("Invalid network key")

        keys = PBKDF2(
            key, "tago-device-key", 64, count=1000000, hmac_hash_module=SHA512
        )
        return (keys[:8], keys[32:])

    @classmethod
    def generate_network_id(cls, key) -> bytes:
        (nid, nsec) = cls.generate_network_creds(key)
        return "".join(":{:02x}".format(x) for x in bytearray(nid))[1:]

    @property
    def network_id(self) -> bytes:
        return "".join(
            "{:02x}".format(x) for x in bytearray(self._nwk_id)
        ).encode()

    @property
    def enumeration_complete(self) -> bool:
        for device in self._devices:
            if device.is_enumerated == False:
                return False

        return True

    def add_host(self, host: str) -> None:
        if host in self._devices:
            return

        device = TagoDevice(host)
        self._devices.append(device)
        device.connect()

    async def close(self):
        for device in self.devices:
            await device.close()
