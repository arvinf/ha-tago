from __future__ import annotations

import json
from typing import Any

import pytest

from custom_components.tago import TagoNet
from fakes import FakeWSConnectCM, FakeWSConnection


@pytest.fixture
def patch_wsconnect(monkeypatch):
    """Patch TagoNet.wsconnect to return a scripted in-process websocket."""

    def _patch(ws: FakeWSConnection) -> FakeWSConnection:
        def _connect(**kwargs):
            return FakeWSConnectCM(ws)

        monkeypatch.setattr(TagoNet, "wsconnect", _connect)
        return ws

    return _patch


@pytest.fixture
def nodes_payload() -> str:
    return json.dumps(
        {
            "rsp": "list_nodes",
            "nodes": {
                "n1": {
                    "loads": [
                        {
                            "id": "light-1",
                            "type": "light_dimmable",
                            "name": "Kitchen Main",
                            "location": "Kitchen",
                            "tag": "L1",
                            "brightness": 500,
                        },
                        {
                            "id": "switch-1",
                            "type": "relay_switch",
                            "name": "Pump",
                            "location": "Plant",
                            "tag": "S1",
                            "state": "OFF",
                        },
                        {
                            "id": "cover-1",
                            "type": "cover_shades",
                            "name": "Blind",
                            "location": "Bedroom",
                            "tag": "C1",
                            "position": 50,
                            "target": 50,
                        },
                        {
                            "id": "fan-1",
                            "type": "fan_onoff",
                            "name": "Ceiling",
                            "location": "Bedroom",
                            "tag": "F1",
                            "value": 700,
                        },
                        {
                            "id": "unknown-1",
                            "type": "unknown_type",
                            "name": "Mystery",
                            "location": "Lab",
                            "tag": "U1",
                        },
                    ]
                }
            },
        }
    )


@pytest.fixture
def login_ok_payload() -> str:
    return json.dumps(
        {
            "status": 200,
            "serialnum": "SN-1234",
            "model": "TAGO-X",
            "firmware": "9.9.9",
        }
    )
