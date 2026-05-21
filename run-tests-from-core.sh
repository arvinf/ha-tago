#!/usr/bin/env bash
set -euo pipefail

CORE_DIR="/home/arvinf/core"
INTEGRATION_DIR="/home/arvinf/ha-tago"
PYTHON_BIN="$CORE_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Python interpreter not found at $PYTHON_BIN"
  echo "Activate your HA venv manually and run pytest, or update this script path."
  exit 1
fi

export PYTHONPATH="$INTEGRATION_DIR:$CORE_DIR:$INTEGRATION_DIR/tests:${PYTHONPATH:-}"

exec "$PYTHON_BIN" -m pytest -q \
  -c "$INTEGRATION_DIR/pytest.ini" \
  "$INTEGRATION_DIR/tests" \
  "$@"
