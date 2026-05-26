"""Loader for the shared wire scenario fixtures.

The canonical file lives in the firmware repo at
`tagoesp/tests/vectors/wire_scenarios.json` and is shared with the firmware
host-side suite. Override with `TAGO_WIRE_SCENARIOS=<path>` if it lives
elsewhere.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

DEVICE_ID = "TAGO_TEST_001"
GROUP_ID = "TAGO_TEST_001L1"
MODEL = "dimac8"


def scenarios_path() -> Path:
    env = os.environ.get("TAGO_WIRE_SCENARIOS")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent.parent / "tagoesp" / "tests" / "vectors" / "wire_scenarios.json",
        here / "vectors" / "wire_scenarios.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "wire_scenarios.json not found; set TAGO_WIRE_SCENARIOS or symlink "
        "tests/vectors/wire_scenarios.json"
    )


@lru_cache(maxsize=1)
def load_all() -> dict[str, Any]:
    raw = json.loads(scenarios_path().read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def get(scenario_id: str) -> dict[str, Any]:
    return load_all()[scenario_id]


def ids() -> list[str]:
    return sorted(load_all().keys())
