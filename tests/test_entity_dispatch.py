"""Direct coverage for `TagoDevice._build_entity_from_payload`.

The dispatcher in [TagoNet.py:_build_entity_from_payload] picks a
Python subclass per wire `type`. Every other test relies on side
effects (sub-card registration, light platform setup, etc.) to
implicitly verify dispatch, which means a typo in `is_of_type` or a
removed override (we shipped one such regression with `TagoKeypad`)
slips through unnoticed.

This file asserts the class-vs-type mapping directly. Add an entry
here for every type the firmware can emit.
"""
from __future__ import annotations

import pytest

from custom_components.tago.TagoNet import (
    TagoCover,
    TagoDevice,
    TagoEntity,
    TagoFan,
    TagoGateway,
    TagoKeypad,
    TagoLight,
    TagoScene,
    TagoSensor,
    TagoSwitch,
    TagoVirtualSensor,
    TagoVirtualSwitch,
)


def _make_device() -> TagoDevice:
    gateway = TagoGateway("dummy:1", authkey="")
    return TagoDevice(gateway, {"id": "test_device", "available": True})


# Each tuple: (wire `type` string, expected Python subclass). Every
# variant the firmware can ship lives here — a new wire type without
# a row in this table risks landing on `TagoEntity` (the unknown-type
# fallback) at runtime instead of its dedicated subclass.
_DISPATCH_TABLE = [
    # Switches (PROTOCOL.md §7a)
    ("outlet_onoff", TagoSwitch),
    # Lights — every member of TagoLight.types
    ("light_onoff", TagoLight),
    ("light_dimmable", TagoLight),
    ("light_mono", TagoLight),
    ("light_rgb", TagoLight),
    ("light_ww", TagoLight),      # CCT-tunable (wire = "light_ww")
    ("light_rgbw", TagoLight),
    ("light_rgbww", TagoLight),   # RGB + CCT (wire = "light_rgbww")
    # Covers (PROTOCOL_PROPOSALS §P-covers)
    ("cover_shade", TagoCover),
    ("cover_curtain", TagoCover),
    ("cover_blind", TagoCover),
    # Fans
    ("fan_onoff", TagoFan),
    # Scenes (PROTOCOL_PROPOSALS §P1)
    ("scene", TagoScene),
    # Keypad variants (PROTOCOL_PROPOSALS §P2) — TagoKeypad uses a
    # family-prefix `is_of_type`, so any `keypad_*` matches.
    ("keypad_4btn", TagoKeypad),
    ("keypad_8btn", TagoKeypad),
    ("keypad_modular", TagoKeypad),
    ("keypad_some_future_variant", TagoKeypad),
    # Virtual switches/sensors (PROTOCOL_PROPOSALS §P3)
    ("virtual_switch", TagoVirtualSwitch),
    ("virtual_sensor", TagoVirtualSensor),
    # Real sensors — every member of TagoSensor.types (PROTOCOL_PROPOSALS §P4)
    ("sensor_light", TagoSensor),
    ("sensor_motion", TagoSensor),
    ("sensor_occupancy", TagoSensor),
    ("sensor_opening", TagoSensor),
    ("sensor_presence", TagoSensor),
    ("sensor_door", TagoSensor),
    ("sensor_window", TagoSensor),
    # Future `sensor_*` types fall through to TagoSensor too (per
    # _build_entity_from_payload's prefix fallback).
    ("sensor_some_future_kind", TagoSensor),
]


@pytest.mark.parametrize("wire_type, expected_class", _DISPATCH_TABLE)
def test_build_entity_picks_correct_class(wire_type, expected_class):
    """Every documented wire type lands on its dedicated Python
    subclass — not on the `TagoEntity` unknown-type fallback."""
    device = _make_device()
    payload = {
        "id": f"E_{wire_type}",
        "type": wire_type,
        "name": "n", "location": "l", "tag": "1A",
        # Subclass-specific state defaults aren't strictly required
        # for dispatch, but a few subclasses read fields during
        # `handle_state_change` and tolerate absence. We pass a
        # broadly-compatible shape:
        "brightness": 0, "ct": 0, "x": 0, "y": 0,
        "is_on": False,
        "position": 0, "target": 0,
        "rgb": {"r": 0, "g": 0, "b": 0},
        "keys": [],
    }
    entity = device._build_entity_from_payload(payload)
    assert type(entity) is expected_class, (
        f"wire type {wire_type!r} dispatched to {type(entity).__name__}, "
        f"expected {expected_class.__name__}"
    )


def test_unknown_type_falls_through_to_tagoentity():
    """A wire type with no matching `is_of_type` lands on the generic
    `TagoEntity` placeholder. Keeps unknown firmware features
    surface-able without crashing."""
    device = _make_device()
    entity = device._build_entity_from_payload({
        "id": "E_unknown", "type": "totally_made_up",
        "name": "n", "location": "l", "tag": "U1",
    })
    assert type(entity) is TagoEntity


def test_unused_type_falls_through_to_tagoentity():
    """`UNUSED` entries (PROTOCOL.md §11) also land on `TagoEntity`."""
    device = _make_device()
    entity = device._build_entity_from_payload({
        "id": "E_unused", "type": "UNUSED",
        "name": "", "location": "", "tag": "",
    })
    assert type(entity) is TagoEntity
    assert entity.is_unused() is True
