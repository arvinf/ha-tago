#!/usr/bin/env bash
# Sync the wire_scenarios.json snapshot from the firmware repo.
# See tests/vectors/README.md for the rationale.
set -euo pipefail

TAGOESP_DIR="${TAGOESP_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)/tagoesp}"
SRC="$TAGOESP_DIR/tests/vectors/wire_scenarios.json"
DEST="$(cd "$(dirname "$0")" && pwd)/wire_scenarios.json"

if [[ ! -f "$SRC" ]]; then
  echo "ERROR: $SRC not found." >&2
  echo "Set TAGOESP_DIR to the firmware checkout root." >&2
  exit 1
fi

cp "$SRC" "$DEST"
echo "Synced $SRC -> $DEST"

if command -v git >/dev/null && git -C "$TAGOESP_DIR" rev-parse HEAD >/dev/null 2>&1; then
  sha="$(git -C "$TAGOESP_DIR" rev-parse --short HEAD)"
  echo "Firmware HEAD: $sha"
fi
