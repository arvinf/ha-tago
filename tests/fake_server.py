"""In-process fake firmware honoring CLIENT_TEST_GUIDE.md §3.

State machine that:
  - Accepts WS on a local port at /api/v1/ws.
  - Replies to any first frame with a fixed identity envelope.
  - Pattern-matches subsequent frames by `req` and applies §10–§15 effects
    to an in-memory entity-state dict seeded from scenario `setup` blocks.
  - Echoes `rsp`, `src` (=`dst` or DEVICE_ID), and `ref` per §3c.
  - Emits `state_changed` / `config_changed` per protocol.

Assumes a protocol-conformant firmware (CLIENT_TEST_GUIDE §5 F-deviations
are treated as already-fixed). Validation rules (set_light type checks,
x/y mutual requirement, set_config name/location length) match the
authoritative C source.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import websockets
from websockets.asyncio.server import serve

from scenarios import DEVICE_ID, GROUP_ID, MODEL


class FakeServer:
    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self._clients: set = set()
        self._server = None
        self._boot_time = time.monotonic()

    async def start(self) -> int:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets))
        return sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass

    def seed(self, setup: dict[str, dict[str, Any]] | None) -> None:
        for eid, fields in (setup or {}).items():
            self.state.setdefault(eid, {"id": eid}).update(fields)

    def reboot(self) -> None:
        self._boot_time = time.monotonic()

    async def broadcast_event(self, evt: dict[str, Any]) -> None:
        payload = json.dumps(evt)
        for ws in list(self._clients):
            try:
                await ws.send(payload)
                self.sent.append(evt)
            except Exception:
                pass

    async def _handle(self, ws) -> None:
        self._clients.add(ws)
        try:
            # Identity exchange — any first frame triggers reply per
            # PROTOCOL.md §2 (and webserver.c:182-185). No `firmware` field.
            first = await ws.recv()
            try:
                self.received.append(json.loads(first))
            except Exception:
                self.received.append({"_raw": first})
            await ws.send(
                json.dumps(
                    {
                        "status": 200,
                        "nonce": "Z" * 32,
                        "serialnum": DEVICE_ID,
                        "model": MODEL,
                        "id": DEVICE_ID,
                    }
                )
            )
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                self.received.append(frame)
                await self._dispatch(ws, frame)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def _dispatch(self, ws, frame: dict[str, Any]) -> None:
        req = frame.get("req")
        ref = frame.get("ref")
        dst_raw = frame.get("dst", DEVICE_ID)
        if not isinstance(dst_raw, str):
            await self._send(ws, {"rsp": req, "src": DEVICE_ID, "status": 500, "ref": ref})
            return
        dst = dst_raw

        if req == "list_nodes":
            await self._send(ws, {"rsp": req, "src": DEVICE_ID, "nodes": self._nodes(), "ref": ref})
        elif req == "get_config" and dst == DEVICE_ID:
            # Allow scenarios to seed `firmware_rev` and the optional
            # PROTOCOL_PROPOSALS §P5 `latest_firmware_rev` field on the
            # device entity. Defaults preserve historical behaviour.
            dev_state = self.state.get(DEVICE_ID, {})
            payload: dict[str, Any] = {
                "rsp": req, "src": DEVICE_ID, "ref": ref,
                "firmware_rev": dev_state.get("firmware_rev", "1.0.0"),
                "model_num": MODEL,
                "serial_number": DEVICE_ID, "api_key": "deadbeef" * 4,
                "loads": [GROUP_ID],
            }
            latest = dev_state.get("latest_firmware_rev")
            if latest is not None:
                payload["latest_firmware_rev"] = latest
            await self._send(ws, payload)
        elif req == "get_config":
            st = self.state.get(dst)
            if st is None:
                await self._send(ws, {"rsp": req, "src": dst, "status": 500, "ref": ref})
            else:
                await self._send(ws, {"rsp": req, "src": dst, "ref": ref, **st})
        elif req == "get_state":
            st = self.state.get(dst)
            if st is None:
                await self._send(ws, {"rsp": req, "src": dst, "status": 500, "ref": ref})
            else:
                await self._send(ws, {"rsp": req, "src": dst, "ref": ref, **self._state_view(st)})
        elif req == "ping":
            ts = int((time.monotonic() - self._boot_time) * 1000)
            await self._send(ws, {"rsp": req, "src": DEVICE_ID, "ts": ts, "ref": ref})
        elif req == "regen_api_key":
            # PROTOCOL.md §9: 32-char lowercase hex string.
            key = "".join("0123456789abcdef"[(i * 11) % 16] for i in range(32))
            await self._send(ws, {"rsp": req, "src": DEVICE_ID, "api_key": key, "ref": ref})
        elif req in ("turn_on", "turn_off", "toggle"):
            await self._apply_onoff(ws, req, dst, ref)
        elif req == "set_light":
            await self._apply_set_light(ws, dst, frame, ref)
        elif req == "stop_ramp":
            await self._apply_stop_ramp(ws, dst, ref)
        elif req == "set_config":
            await self._apply_set_config(ws, dst, frame, ref)
        elif req == "activate":
            # PROTOCOL_PROPOSALS §P1.3 scene activation.
            await self._apply_scene_activate(ws, dst, ref)
        elif req == "set_led":
            # PROTOCOL_PROPOSALS §P2.5 keypad LED control.
            await self._apply_set_led(ws, dst, frame, ref)
        else:
            await self._send(ws, {"rsp": req, "src": dst, "status": 500, "ref": ref})

    async def _send(self, ws, payload: dict[str, Any]) -> None:
        clean = {k: v for k, v in payload.items() if v is not None}
        self.sent.append(clean)
        await ws.send(json.dumps(clean))

    # Map entity type → which collection key it should appear under in
    # the list_nodes response (PROTOCOL_PROPOSALS extensions).
    _COLLECTION_BY_TYPE = {
        "scene": "scenes",
        "keypad_4btn": "keypads",
        "keypad_8btn": "keypads",
        "keypad_modular": "keypads",
        "keypad_led": "keypad_leds",
        "virtual_switch": "virtual_switches",
        "virtual_sensor": "virtual_sensors",
        # PROTOCOL_PROPOSALS §P4: real sensors.
        "sensor_light": "sensors",
        "sensor_motion": "sensors",
        "sensor_occupancy": "sensors",
        "sensor_opening": "sensors",
        "sensor_presence": "sensors",
        "sensor_door": "sensors",
        "sensor_window": "sensors",
    }

    def _nodes(self) -> dict[str, Any]:
        loads: list = []
        scenes: list = []
        keypads: list = []
        keypad_leds: list = []
        virtual_switches: list = []
        virtual_sensors: list = []
        sensors: list = []

        for eid, st in self.state.items():
            if eid in (DEVICE_ID, GROUP_ID):
                continue
            entry = {
                "id": eid,
                "name": st.get("name", ""),
                "location": st.get("location", ""),
                "tag": st.get("tag", ""),
            }
            entry.update(st)
            entry["id"] = eid

            type_str = st.get("type", "")
            collection_name = self._COLLECTION_BY_TYPE.get(type_str)
            # Any unknown `sensor_*` type lands in `sensors` too — the
            # firmware doesn't gate the type vocabulary.
            if collection_name is None and isinstance(type_str, str) and type_str.startswith("sensor_"):
                collection_name = "sensors"

            if collection_name == "scenes":
                scenes.append(entry)
            elif collection_name == "keypads":
                # `map` doesn't apply; remove the default.
                entry.pop("map", None)
                keypads.append(entry)
            elif collection_name == "keypad_leds":
                entry.pop("map", None)
                keypad_leds.append(entry)
            elif collection_name == "virtual_switches":
                entry.pop("map", None)
                virtual_switches.append(entry)
            elif collection_name == "virtual_sensors":
                entry.pop("map", None)
                virtual_sensors.append(entry)
            elif collection_name == "sensors":
                entry.pop("map", None)
                sensors.append(entry)
            else:
                # Regular load — has the C-style `map` array.
                entry.setdefault("map", st.get("map", [-1]))
                loads.append(entry)

        group: dict = {"type": "dimac", "ch": 8, "loads": loads}
        if scenes:
            group["scenes"] = scenes
        if keypads:
            group["keypads"] = keypads
        if keypad_leds:
            group["keypad_leds"] = keypad_leds
        if virtual_switches:
            group["virtual_switches"] = virtual_switches
        if virtual_sensors:
            group["virtual_sensors"] = virtual_sensors
        if sensors:
            group["sensors"] = sensors
        return {GROUP_ID: group}

    def _state_view(self, st: dict[str, Any]) -> dict[str, Any]:
        t = st.get("type", "")
        view = {"id": st.get("id"), "type": t}
        is_real_sensor = isinstance(t, str) and t.startswith("sensor_")
        if t in ("light_onoff", "outlet_onoff", "fan_onoff",
                 "virtual_switch", "virtual_sensor") or is_real_sensor:
            # PROTOCOL.md §12.3 + PROTOCOL_PROPOSALS §P3/§P4: on/off
            # entities (incl. virtual ones and real sensors) expose
            # canonical `is_on`.
            view["is_on"] = st.get("is_on", st.get("brightness", 0) > 0)
        elif t == "keypad_led":
            # PROTOCOL_PROPOSALS §P2.5: keypad LED state.
            view["is_on"] = st.get("is_on", False)
            view["brightness"] = st.get("brightness", 0)
            view["rgb"] = st.get("rgb", {"r": 0, "g": 0, "b": 0})
        elif t == "scene":
            view["last_activated_ts"] = st.get("last_activated_ts", 0)
        else:
            for k in ("brightness", "ct", "x", "y"):
                if k in st:
                    view[k] = st[k]
        if "ramp" in st:
            view["ramp"] = st["ramp"]
        # PROTOCOL_PROPOSALS §P6: per-entity RSSI is included in state
        # views whenever the seed has it set.
        if "rsi" in st:
            view["rsi"] = st["rsi"]
        return view

    async def _apply_onoff(self, ws, req: str, dst: str, ref: str | None) -> None:
        st = self.state.get(dst)
        if st is None:
            await self._send(ws, {"rsp": req, "src": dst, "status": 500, "ref": ref})
            return
        # PROTOCOL_PROPOSALS §P3.4 + §P4.4: virtual sensors and real
        # sensors are read-only; the firmware is the only writer.
        # turn_on/turn_off/toggle from a client must be rejected.
        sensor_type = st.get("type", "")
        if sensor_type == "virtual_sensor" or (
            isinstance(sensor_type, str) and sensor_type.startswith("sensor_")
        ):
            await self._send(ws, {"rsp": req, "src": dst, "status": 500, "ref": ref})
            return
        if req == "turn_on":
            st["is_on"] = True
        elif req == "turn_off":
            st["is_on"] = False
        else:
            st["is_on"] = not st.get("is_on", False)
        await self._send(ws, {"rsp": req, "src": dst, "status": 200, "ref": ref})
        # PROTOCOL.md §12.4–§12.6: turn_on/turn_off/toggle emit state_changed.
        await self.broadcast_event({"evt": "state_changed", "src": dst, **self._state_view(st)})

    async def _apply_set_light(self, ws, dst: str, frame: dict[str, Any], ref: str | None) -> None:
        st = self.state.get(dst)
        if st is None:
            await self._send(ws, {"rsp": "set_light", "src": dst, "status": 500, "ref": ref})
            return
        # PROTOCOL_PROPOSALS §P2.5: set_light against a keypad_led is
        # rejected; the LED's only command is `set_led`. PROTOCOL_PROPOSALS
        # §P4.4: real sensors don't take any write commands either.
        type_str = st.get("type", "")
        if type_str == "keypad_led" or (
            isinstance(type_str, str) and type_str.startswith("sensor_")
        ):
            await self._send(ws, {"rsp": "set_light", "src": dst, "status": 500, "ref": ref})
            return

        # Type validation matches load_light.c:set_light — any non-number
        # field is a 500 reject before any state change.
        def _is_num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        for k in ("brightness", "brightness+", "ct", "ct+", "x", "y", "duration", "rate"):
            if k in frame and not _is_num(frame[k]):
                await self._send(ws, {"rsp": "set_light", "src": dst, "status": 500, "ref": ref})
                return

        # x/y must come as a pair — passing only one is a 500
        # (load_light.c:697 `if ((item_x != NULL) != (item_y != NULL))`).
        if ("x" in frame) != ("y" in frame):
            await self._send(ws, {"rsp": "set_light", "src": dst, "status": 500, "ref": ref})
            return

        old = dict(st)
        if "brightness" in frame:
            st["brightness"] = max(0, min(1000, int(frame["brightness"])))
        if "brightness+" in frame:
            st["brightness"] = max(0, min(1000, st.get("brightness", 0) + int(frame["brightness+"])))
        if "ct" in frame:
            st["ct"] = max(0, min(1000, int(frame["ct"])))
            st["x"] = 0.0
            st["y"] = 0.0
        if "ct+" in frame:
            st["ct"] = max(0, min(1000, st.get("ct", 0) + int(frame["ct+"])))
            st["x"] = 0.0
            st["y"] = 0.0
        if "x" in frame and "y" in frame:
            st["x"] = float(frame["x"])
            st["y"] = float(frame["y"])
            st["ct"] = 0
        duration = frame.get("duration")
        if duration is not None:
            d = max(0, min(10000, int(duration)))
            duration = d if d >= 300 else 0

        await self._send(ws, {"rsp": "set_light", "src": dst, "status": 200, "ref": ref})

        # PROTOCOL.md §13.7 no-op detection compares all of
        # (brightness, ct, x, y, duration); event is dropped only if nothing
        # changed (and no ramp duration was requested).
        changed = any(st.get(k) != old.get(k) for k in ("brightness", "ct", "x", "y"))
        if not changed and not duration:
            return

        evt: dict[str, Any] = {"evt": "state_changed", "src": dst, **self._state_view(st)}
        if duration:
            evt["ramp"] = {"duration": duration, "end": {"brightness": st.get("brightness", 0)}}
        await self.broadcast_event(evt)

    async def _apply_stop_ramp(self, ws, dst: str, ref: str | None) -> None:
        st = self.state.get(dst)
        if st is None:
            await self._send(ws, {"rsp": "stop_ramp", "src": dst, "status": 500, "ref": ref})
            return
        st.pop("ramp", None)
        await self._send(ws, {"rsp": "stop_ramp", "src": dst, "status": 200, "ref": ref})
        # PROTOCOL.md §13.8: stop_ramp emits state_changed.
        await self.broadcast_event({"evt": "state_changed", "src": dst, **self._state_view(st)})

    async def _apply_set_config(self, ws, dst: str, frame: dict[str, Any], ref: str | None) -> None:
        st = self.state.get(dst)
        if st is None:
            await self._send(ws, {"rsp": "set_config", "src": dst, "status": 500, "ref": ref})
            return
        # PROTOCOL.md §7a — full set of valid load types.
        valid_types = {
            "UNUSED", "light_onoff", "outlet_onoff", "fan_onoff",
            "light_dimmable", "light_mono", "fan_adjustable",
            "light_ww", "light_rgb", "light_rgbw", "light_rgbww",
        }
        new_type = frame.get("type")
        if new_type is not None and new_type not in valid_types:
            await self._send(ws, {"rsp": "set_config", "src": dst, "status": 500, "ref": ref})
            return

        # name / location validation per loads.c:368-378 — must be string,
        # length <= LOAD_NAME_MAX_LEN (64). Any violation rejects the whole
        # request with 500 before any state changes.
        LOAD_NAME_MAX_LEN = 64
        for key in ("name", "location"):
            v = frame.get(key)
            if v is None:
                continue
            if not isinstance(v, str) or len(v) > LOAD_NAME_MAX_LEN:
                await self._send(ws, {"rsp": "set_config", "src": dst, "status": 500, "ref": ref})
                return

        emit_for: list[str] = []
        if "map" in frame and isinstance(frame["map"], list):
            for ch in frame["map"]:
                for other_id, other in self.state.items():
                    if other_id == dst or other_id in (DEVICE_ID, GROUP_ID):
                        continue
                    if ch in other.get("map", []):
                        other["map"] = [-1 if c == ch else c for c in other["map"]]
                        emit_for.append(other_id)
        for k, v in frame.items():
            if k in ("req", "dst", "ref"):
                continue
            st[k] = v
        await self._send(ws, {"rsp": "set_config", "src": dst, "status": 200, "ref": ref})
        await self.broadcast_event({"evt": "config_changed", "src": dst, **st})
        for other_id in emit_for:
            await self.broadcast_event({"evt": "config_changed", "src": other_id, **self.state[other_id]})

    # =================================================================
    # Protocol extensions (PROTOCOL_PROPOSALS.md)
    # =================================================================

    async def _apply_scene_activate(self, ws, dst: str, ref: str | None) -> None:
        """PROTOCOL_PROPOSALS §P1.3 `activate`. Emits a status 200 reply
        and broadcasts a `scene_activated` event with a synthetic ts."""
        st = self.state.get(dst)
        if st is None or st.get("type") != "scene":
            await self._send(ws, {"rsp": "activate", "src": dst, "status": 500, "ref": ref})
            return
        ts = int((time.monotonic() - self._boot_time) * 1000)
        st["last_activated_ts"] = ts
        await self._send(ws, {"rsp": "activate", "src": dst, "status": 200, "ref": ref})
        await self.broadcast_event({
            "evt": "scene_activated",
            "src": dst,
            "id": dst,
            "type": "scene",
            "name": st.get("name", ""),
            "ts": ts,
        })

    async def _apply_set_led(self, ws, dst: str, frame: dict[str, Any], ref: str | None) -> None:
        """PROTOCOL_PROPOSALS §P2.5 `set_led`. All fields optional;
        validates `effect`/`duration` pairing."""
        st = self.state.get(dst)
        if st is None or st.get("type") != "keypad_led":
            await self._send(ws, {"rsp": "set_led", "src": dst, "status": 500, "ref": ref})
            return

        def _is_num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        # Type validation.
        if "is_on" in frame and not isinstance(frame["is_on"], bool):
            await self._send(ws, {"rsp": "set_led", "src": dst, "status": 500, "ref": ref})
            return
        for k in ("brightness", "duration"):
            if k in frame and not _is_num(frame[k]):
                await self._send(ws, {"rsp": "set_led", "src": dst, "status": 500, "ref": ref})
                return
        rgb = frame.get("rgb")
        if rgb is not None:
            if not isinstance(rgb, dict) or not all(k in rgb for k in ("r", "g", "b")):
                await self._send(ws, {"rsp": "set_led", "src": dst, "status": 500, "ref": ref})
                return
            for c in ("r", "g", "b"):
                v = rgb[c]
                if not _is_num(v) or not 0 <= int(v) <= 255:
                    await self._send(ws, {"rsp": "set_led", "src": dst, "status": 500, "ref": ref})
                    return
        # effect + duration must come together.
        has_effect = "effect" in frame
        has_duration = "duration" in frame
        if has_effect != has_duration:
            await self._send(ws, {"rsp": "set_led", "src": dst, "status": 500, "ref": ref})
            return
        if has_effect and frame["effect"] != "flash":
            await self._send(ws, {"rsp": "set_led", "src": dst, "status": 500, "ref": ref})
            return

        # Apply (we don't actually simulate the flash effect's blink
        # cycle — we just acknowledge the request and broadcast a
        # state_changed reflecting any non-effect changes).
        if "is_on" in frame:
            st["is_on"] = bool(frame["is_on"])
        if "brightness" in frame:
            st["brightness"] = max(0, min(1000, int(frame["brightness"])))
        if rgb is not None:
            st["rgb"] = {"r": int(rgb["r"]), "g": int(rgb["g"]), "b": int(rgb["b"])}

        await self._send(ws, {"rsp": "set_led", "src": dst, "status": 200, "ref": ref})
        await self.broadcast_event({
            "evt": "state_changed", "src": dst, **self._state_view(st),
        })
