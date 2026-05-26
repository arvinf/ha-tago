"""Constants for the Tago integration."""

DOMAIN = "tago"

CONF_HOSTSTR = "hostname"

# Authentication credential storage key.
#
# Historically this stored the device's `api_key` and was labelled
# "Authentication Key" in the UI. The next major firmware release
# replaces api_key with a short device PIN, so:
#   * UI labels and translations refer to it as "PIN" from this version on
#   * the entry-data key is renamed `authkey` -> `pin` via a
#     ConfigEntry migration (see async_migrate_entry / VERSION 9)
#   * existing values are carried over verbatim during migration; if the
#     stored value no longer authenticates (because the firmware is now
#     PIN-based), HA triggers a reauth flow that prompts the user for the
#     real PIN.
CONF_PIN = "pin"

# Legacy key kept for migration only — do NOT use in new code paths.
CONF_AUTHKEY = "authkey"

CONF_DEVICENAME = "device_name"
ATTR_RATE = "rate"

# Minimum supported device firmware version. Devices reporting a lower
# version still load (the protocol is backward-compatible), but a Repair
# issue is raised so the user knows to update. Bumping this triggers a
# repair notification for every install on older firmware — change with
# care.
MIN_FIRMWARE_VERSION = "1.0.0"
