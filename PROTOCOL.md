# TagoESP WebSocket Protocol Specification

> **Source of truth.** Compiled from current firmware on branch `dm8c`:
> `main/tagonet.c`, `main/dimmer_pwm.c`, `main/webserver.c`, `main/network.c`,
> `main/device_config.c`, `main/load.h`, `main/tagonet_const.h`, plus
> `sdkconfig`.
>
> **Audience.** Host-side SDK authors, integration testers, and any tooling
> (including LLM-driven test generators) that needs an authoritative
> wire-format reference.
>
> **Convention.** Sections 1–17 describe **current shipping behavior** plus
> the small number of placeholder entity categories (keypad, presence sensor,
> cover) that are accepted as protocol territory but not yet implemented.
> Anything we'd like to **change** in already-implemented behavior is
> collected in section 18 ("Proposed Changes — not yet implemented") so a
> reader can never confuse the spec for the wish-list.
>
> **Authentication is intentionally omitted from this revision.** The current
> challenge/response mechanism will be redesigned; the connection
> establishment description in section 2 covers only the identity exchange.

---

## 1. Transport

| Property | Value |
|---|---|
| Endpoint (plaintext) | `ws://<ip>/api/v1/ws` |
| Endpoint (TLS) | `wss://<ip>/api/v1/ws` — only if `CONFIG_USE_SSL` is enabled at build time |
| Default HTTP port | 80 (or 443 when `CONFIG_USE_SSL`) |
| Frame type | WebSocket text frames, UTF-8, one JSON object per frame |
| Max simultaneous clients | 10 |
| HTTP header on every non-WS response | `X-Serialnum: <serial_number>` |
| Other HTTP endpoints (informational) | `GET /` → embedded `index.html`, `GET /bundle.js` → gzipped JS bundle, `POST /ota_update` → multipart firmware upload (not part of the JSON protocol; listed for discovery completeness) |
| mDNS service | `_tagodev._tcp`, instance `tago-<serial>`, TXT records: `model`, `serialnum`, `fwrev`, `ssl` |

There is **no message framing on top of WebSocket**: each WS text frame is
exactly one complete JSON object. There are no sequence numbers and no
chunked messages.

---

## 2. Connection lifecycle and authentication

### 2.1 Bearer-token authentication (current)

Authentication happens **before the first WebSocket frame is exchanged**.
The client supplies a bearer token that proves it knows the device's
pairing PIN — a 9-digit out-of-band secret (today hardcoded
`"123456789"`, future hardware reads it from eFUSE). The token is
self-contained: the device validates it locally with no additional
round-trip.

#### Binary layout

The token is **a flat concatenation of four binary fields**, then
base64url-encoded into one opaque ASCII string. No JSON, no nested
structures.

```
byte offset:  0      1                     17                    25                            41
              ┌──────┬─────────────────────┬─────────────────────┬─────────────────────────────┐
              │ ver  │       nonce         │     ts_ms_be        │     truncated HMAC          │
              │ 0x01 │      (16 bytes)     │     (8 bytes)       │       (16 bytes)            │
              └──────┴─────────────────────┴─────────────────────┴─────────────────────────────┘
              ↑                                                  ↑
              the MAC is computed over THIS region ──────────────┘
              (i.e., version + nonce + timestamp = 25 bytes)
```

| Field | Bytes | Purpose |
|---|---|---|
| `ver` | 1 | Format version + trust tier — `0x01` = User, `0x02` = Admin. See §2.1.1 for what changes between tiers. The MAC covers this byte so the tier can't be swapped without invalidating the token. |
| `nonce` | 16 | Cryptographically random per token. Visible in the clear; the server uses it as a replay key (it caches recent nonces) and as part of the MAC input. |
| `ts_ms_be` | 8 | Client wall-clock time in **milliseconds since Unix epoch**, encoded **big-endian**. Used for skew check (±60 s). Visible in the clear. |
| `MAC` | 16 | First 16 bytes of `HMAC-SHA256(key, ver \|\| nonce \|\| ts_ms_be)`, where `key = SHA256("tagoesp-pin-v1" \|\| pin)`. This is the proof-of-knowledge — only someone who knows the PIN can produce it. |

Total: **41 bytes** binary → **55 ASCII chars** of base64url (no `=`
padding, uses `-` and `_` instead of `+` and `/`). The base64url
alphabet is deliberately chosen so the same string passes through HTTP
headers, WebSocket subprotocol tokens, and URL query parameters
unmodified — no further escaping needed.

#### Token construction (client side)

```
# 1. Derive the PIN-bound HMAC key. Domain-separated so the same PIN
#    can be reused for a different purpose later without colliding.
key = SHA256("tagoesp-pin-v1" || pin_ascii)            # 32 bytes

# 2. Build the unsigned prefix (25 bytes).
bin[0]      = 0x01
bin[1..17]  = random(16)                                # nonce
bin[17..25] = htobe64(timestamp_ms)                     # big-endian uint64

# 3. Sign the prefix; keep only the first 16 bytes.
full_mac    = HMAC_SHA256(key, bin[0..25])              # 32 bytes
bin[25..41] = full_mac[0..16]                           # truncated to 16

# 4. Encode the 41-byte blob.
token       = base64url(bin)                            # 55 ASCII chars
```

#### Token validation (device side)

```
bin = base64url_decode(token)                           # back to 41 bytes
assert len(bin) == 41                                   # else INVALID_SIZE
assert bin[0] == 0x01                                   # else NOT_SUPPORTED

nonce      = bin[1..17]
ts_ms      = be64toh(bin[17..25])
recv_mac   = bin[25..41]

# Recompute the MAC over the same prefix.
key        = SHA256("tagoesp-pin-v1" || device_pin)
expected   = HMAC_SHA256(key, bin[0..25])[0..16]

if abs(now_ms - ts_ms) > 60_000:        reject INVALID_STATE   # clock skew
if not constant_time_eq(recv_mac, expected): reject FAIL       # wrong PIN / tampered
if nonce in recent_nonces:              reject NOT_FOUND       # replay

remember_nonce(nonce, ts_ms)
accept
```

Validation uses **constant-time MAC comparison** to prevent timing
attacks. The recent-nonce cache holds the last ~64 accepted nonces
seen within the skew window.

#### Concrete example

PIN = `"123456789"`, nonce = sixteen `0x42` bytes, ts = `1717000000000` ms:

```
hex layout of the 41-byte binary token:
  ver:   01
  nonce: 42 42 42 42  42 42 42 42  42 42 42 42  42 42 42 42
  ts:    00 00 01 8F  BD 8E 27 80                     (= 1717000000000 BE)
  mac:   <16 deterministic bytes from HMAC of the above with the PIN-derived key>

base64url: starts with "AUJCQkJCQkJCQkJCQkJCQkIAAAGPvY4ngP…"  (55 chars total)
```

To a client implementation the whole thing is one opaque string. To
HomeAssistant's Python code: `headers={"Authorization": f"Bearer {token}"}`.
To browser JS: `new WebSocket(url, ["tago-v1", "bearer." + token])`.
To Control4 Lua: `url .. "?token=" .. token`. None of them parse the
fields — they only compute the token from the PIN and ship it.

#### Signed, not encrypted

Worth being precise about what the token does and does not protect:

- **The nonce and timestamp are visible** to anyone who base64-decodes
  the token. They are not secrets. The MAC field is also visible.
- **What's protected is the ability to construct a valid token.** An
  attacker who lacks the PIN cannot create a token with a different
  nonce / timestamp and a valid MAC; the MAC binds them together.
- **The PIN itself never appears in the token.** It is the only true
  secret, and it stays local to client and device.
- **SSL adds transport confidentiality on top.** Over `wss://`, even
  the nonce and timestamp are encrypted on the wire. Without SSL, a
  passive observer can read them — but replay is bounded by the
  60-second window and the nonce cache, and forgery is bounded by
  needing the PIN.

#### Token delivery — three options, validated identically

| # | Channel | When to use |
|---|---|---|
| 1 | `Authorization: Bearer <token>` HTTP header on the WS upgrade | Native clients that can set arbitrary HTTP headers (HomeAssistant Python, curl, custom integrations). |
| 2 | `Sec-WebSocket-Protocol: tago-v1, bearer.<token>` on the WS upgrade | Browser-based clients. The standard WebSocket API in browsers blocks arbitrary HTTP headers but allows subprotocol values. The server responds with the `tago-v1` protocol; the `bearer.<token>` entry is consumed. |
| 3 | `?token=<token>` query parameter on the WS URL | Constrained clients that can't set headers OR subprotocols (Control4 / Lua, etc.). **Warning**: the token will appear in browser history, devtools, and any upstream HTTP access logs. Use only when 1 and 2 aren't available. Over SSL the token isn't visible on the wire, but it lingers in those client-side surfaces. |

The device checks the three sources in the order listed. The first
token it finds is the one it validates — if validation fails, the
connection is closed with no further opportunity to authenticate.

#### Security properties

- A token captured by a passive observer (no SSL) can be replayed only
  within the 60-second skew window, only until the original nonce
  evicts from the cache, and never if the nonce has already been
  consumed.
- Brute-forcing the 9-digit PIN takes ~10⁹ attempts. With per-connection
  rate limiting on the device (TBD — see §18 P4), this is
  computationally impractical over a LAN.
- Forward secrecy: **no** — PIN compromise compromises all past sessions.
  SSL provides session confidentiality but the PIN-derived auth has no
  PFS by design (PIN is the only secret).

#### 2.1.1 Trust tiers (User vs Admin)

> **Audience reminder.** This whole document is internal — consumed only
> by trusted integrators (firmware, in-house web dashboard, HomeAssistant
> integration, Control4 driver). The admin KDF below is documented here
> because everyone reading it is expected to be on the team.

The version byte `bin[0]` of the token doubles as a trust-tier tag:

| Version | Tier | KDF | Used by |
|---|---|---|---|
| `0x01` | **User** | `SHA256("tagoesp-pin-v1" \|\| pin)` | HomeAssistant, Control4, third-party tooling |
| `0x02` | **Admin** | `SHA256("tagoesp-admin-v1" \|\| pin)` | In-house web dashboard only |

Same PIN, different KDF prefix. Token format is otherwise identical.

**Admin-only commands.** The dispatcher rejects these with `status:500`
when the caller's token is User-tier:

- `list_nodes`
- `set_config` (load, group, or device level)
- `factory_reset`
- `reboot`
- `set_override`, `clear_override` (input override — testing path)
- `network_get_state`
- `wifi_scan`, `wifi_connect`, `wifi_disconnect`
- `zigbee_permit_join`, `zigbee_remove_device`, `zigbee_recreate_network`
- `get_config` (device, group, load, input, or keypad configuration)

Other public reads (`device_get_info`, `get_state`, `ping`), light
controls, identify, and batches of state-mutation commands work at User
tier. `list_nodes` and all `get_config` requests are Admin-only.

**Admin-only events.** Configuration, WiFi, Zigbee, and network events
are broadcast only to Admin-tier clients. Non-admin clients never see:

- `config_changed`, `eth_state_changed`
- `wifi_state_changed`, `wifi_scan_results`
- `zigbee_device_joined`, `zigbee_device_left`, `zigbee_permit_join_changed`

**Security model.** This is *security by undisclosed derivation*, not
cryptographic separation — anyone with the PIN AND the admin KDF prefix
can mint admin tokens. The point isn't to defend against attackers who
compromise the dashboard binary; it's to prevent HomeAssistant or
Control4 from being able to change configuration even by accident.
Future hardware will mix in an eFUSE-stored per-device secret to make
admin a real cryptographic tier (likely token version `0x03`).

### 2.2 Disconnect / reconnect

When the WebSocket closes (client- or server-initiated), the session
state is discarded. There is no long-lived reconnection token: clients
recompute and present a fresh token (new nonce, current timestamp) on
every reconnect.

---

## 3. Message envelope

Every message is one JSON object. There are four envelope shapes.

### 3a. Request (client → device)

```json
{
  "req": "<command>",
  "dst": "<entity_id>",
  "ref": "<optional opaque correlation tag>",
  ... command-specific fields merged at top level ...
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `req` | string | yes | Command name. See sections 9–14. |
| `dst` | string | no | Target entity. **Optional.** When omitted, the command targets the device gateway (the gateway entity ID, equivalent to sending `dst = <device_id>`). Per-load-entity commands (sections 12–14) must include `dst` because they need to identify which entity to act on; device-level and gateway commands (section 9) may omit it. |
| `ref` | string | no | Echoed back verbatim on the matching response. |
| (payload) | varies | no | Per-command fields, merged into the same object. |

### 3b. Response with data (device → client)

Sent when the command produces output (`get_*` reads, `ping`,
`list_nodes`, etc.):

```json
{
  "status": 200,
  "rsp": "<command>",
  "src": "<entity_id>",
  "ref": "<echoed if request had ref>",
  ... command-specific result fields ...
}
```

Every successful response carries `status: 200` regardless of whether
the handler also produced payload fields. This is a deliberate
simplification — clients always read the same shape.

### 3c. Status-only success (device → client)

Sent for successful commands that return no data and are not silent
state mutations, such as config writes and gateway actions:

```json
{ "status": 200, "rsp": "<command>", "src": "<entity_id>", "ref": "<echoed if present>" }
```

### 3d. State-mutation success — silent (device → client)

The commands `turn_on`, `turn_off`, `toggle`, `set_light`, `stop_ramp`,
and `set_output` **do not produce a response on success**. Load-state
commands emit `state_changed` when their state changes. `set_output` is a
diagnostic physical-output override: it emits no response and no event,
because it does not change load state.

Failures of these commands still emit `status:500` per 3e.

### 3e. Error response (device → client)

Every failure uses `status:500`, including invalid input, unknown
commands/targets, authorization failures, not-ready requests, and internal
errors.

```json
{ "status": 500, "rsp": "<command>", "src": "<entity_id>", "ref": "<echoed if present>" }
```

The specific failure reason is not exposed on the wire. Clients should
not infer it from the status; they have the request and entity type
locally.

**Every response — data, ack, or error — carries `rsp` and `src`.** `rsp`
echoes the original command name; `src` echoes the resolved `dst` (which
defaults to the device gateway ID when the request omitted `dst`). This
means a client can always correlate a response to its request even
without setting `ref`.

### 3f. Batch request — fire-and-forget (client → device)

Multiple state-mutation commands in one frame. Used by HA when a scene
changes many entities at once, so the device handles the parse and
dispatch as a single unit and the WebSocket sees one frame instead of
N:

```json
{
  "req": "batch",
  "ref": "<optional>",
  "cmds": [
    { "req": "set_light", "dst": "<load_entity_id>", "brightness": 500 },
    { "req": "turn_off",  "dst": "<other_entity_id>" },
    ...
  ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `req` | string | yes | Must be `"batch"`. |
| `cmds` | array | yes | One sub-request per element. Each must include its own `req` (and `dst` where applicable). |
| `ref` | string | no | Echoed on the error response if the batch envelope itself is malformed; not echoed otherwise. |

**Restrictions:**

- Sub-commands SHOULD only be state-mutation commands (`turn_on`,
  `turn_off`, `toggle`, `set_light`, `stop_ramp`, `set_output`). Other commands are
  not rejected — they just won't return a response (the batch is
  fire-and-forget), so issuing a `get_state` inside a batch is
  pointless. Reads should be sent as normal single-frame requests.
- The batch is **not atomic**. Per-command failures are silently
  dropped; clients reconcile state via the `state_changed` events.
- The whole batch returns nothing on success.
- The batch returns `status: 500` only when the envelope itself is
  malformed (`commands` not an array, missing entirely, etc.).

### 3d. Async event (device → client, unsolicited)

```json
{
  "evt": "<event_name>",
  "src": "<entity_id>",
  "eid": "01234567",
  ... event-specific fields ...
}
```

| Field | Notes |
|---|---|
| `evt` | Event name. See section 15 for the full vocabulary. |
| `src` | Entity ID the event is about. |
| `eid` | **Event ID**: 8-digit decimal string, randomly assigned per event by the device. Clients use it to deduplicate. Two events emitted back-to-back are guaranteed to have different `eid`s; across the device's lifetime a birthday collision is expected after ~10K events, which is well beyond any practical dedup window. |

Events are **broadcast to every authenticated client**, including the
client that triggered the underlying state change. Clients must
therefore expect to receive echoes of their own actions — the `eid`
field is the canonical way to recognize and drop duplicates of the
same broadcast received over multiple paths.

Admin-only events (WiFi, Zigbee, network state) are additionally
filtered at broadcast time and delivered only to admin-tier clients —
see §2.1.1.

---

## 4. Entity IDs

Entity IDs are **opaque, globally unique strings**. The client should never
parse or pattern-match an entity ID. ID generation is a firmware
implementation detail and is expected to evolve across firmware versions
and across future device types (in particular, devices that bridge
sub-devices over RS485 will surface those sub-device load entities under
the same gateway and may use a different ID format for them).

What a client can rely on:

- The set of entity IDs that exist on a gateway is discoverable through
  `device_get_info` (public, see section 9) and `list_nodes` (admin,
  see section 9).
- Each entity has a **type** (string, drawn from a fixed vocabulary — see
  section 7) and a **tag** (short user-facing display label).
- The device gateway entity ID equals the device's `serial_number`
  (returned by `device_get_info`, section 9) and is the `src` of
  replies to device-level commands.
- A `dst` round-trips intact: send a frame with `dst=X`, the matching reply
  carries `src=X`. Events related to entity `X` always carry `src=X`.

The currently-deployed firmware happens to derive IDs from the device
serial number (a 12-char uppercase hex MAC), but **host code must not
encode that assumption**.

---

## 5. Wire value scales

Everything that varies continuously on the wire is an **integer percentage
in units of 0.1%**:

| Value type | Range | Meaning |
|---|---|---|
| Intensity / brightness | `0` … `1000` | `0` = 0%, `5` = 0.5%, `500` = 50%, `1000` = 100%. |
| CT mix | `0` … `1000` | `0` = fully warm endpoint, `1000` = fully cool endpoint. See section 6. |
| `max_intensity` (config cap) | `0` … `1000` | Output ceiling. `1000` (= 100 %) is the "disabled" sentinel — the cap has no effect and the field is omitted from `get_config`. Legacy stored `0` is treated the same way (back-compat). |
| `min_intensity` (config floor) | `0` … `1000` | Output floor for any non-zero brightness. `0` is the "disabled" sentinel and the field is omitted from `get_config`. Brightness `0` always produces output `0` regardless of `min_intensity` — off is off. |

The constant 1000 (`CONFIG_LIGHT_VALUE_MAX`) is fixed at build time and is
not expected to change. Host code can hard-code `1000 = 100%`.

Ramp durations are **integer milliseconds**:

| Value type | Range | Meaning |
|---|---|---|
| Ramp duration | `300` … `10000` ms | Clamped to `[CONFIG_RAMP_DURATION_MIN, CONFIG_RAMP_DURATION_MAX]`. Anything below the minimum is treated as 0 — the change is applied instantly. |

CIE chromaticity coordinates (`x`, `y`) are floating-point in `[0.0, 1.0]`,
sent as JSON numbers with up to 4 decimal places. They round-trip through
the device's internal fixed-point representation, so expect small
quantization on read-back.

Counts (entity counts, channel counts in `list_nodes`) are integers.
Timestamps and elapsed times are integer milliseconds unless documented
otherwise.

---

## 6. Color temperature (CT) — semantics

CT is **always a percentage** on the wire: `ct ∈ [0, 1000]`. The firmware
does not deal in Kelvin internally and does not provide any command to set
an absolute Kelvin temperature.

- `ct = 0` → fully `cct_basis[0]` (the **warm** endpoint of the configured
  range).
- `ct = 1000` → fully `cct_basis[1]` (the **cool** endpoint).
- Intermediate values blend linearly between the two endpoints.

`ct_range` in `set_config` / `get_config` is **purely metadata for the host
UI** so it can display "2700 K — 6500 K" labels next to a slider. The
firmware never converts Kelvin; only the host does. Set `ct_range` once
during user setup and read it back on UI rebuild.

---

## 7. Entity categories and types

Every entity has a `type` string drawn from a fixed vocabulary. The host
may change a load entity's `type` via `set_config`, but the firmware
**validates** the new value against the per-category whitelist for the
entity slot — invalid values are rejected.

Today, every load entity is in the **light/outlet/fan family** (collectively
referred to as "load entities" in this doc). Other categories
(keypad, presence sensor, cover) are reserved in the protocol but not
implemented in the current firmware. See section 14 for the placeholders.

### 7a. Load entity types (lights, outlets, fans)

The table below is the protocol vocabulary. The current firmware
temporarily accepts changes to every type it recognizes in `set_config`,
including `"UNUSED"`; this does not guarantee that attached hardware can
drive each type. Unknown type strings are rejected with `status:500`.

| `type` value | Category | LED ch | CCT | Color | AC mode | Notes |
|---|---|:---:|:---:|:---:|:---:|---|
| `"UNUSED"` | — | 0 | – | – | – | Unconfigured slot. |
| `"light_onoff"` | on/off | 1 | – | – | – | Binary light. |
| `"outlet_onoff"` | on/off | 1 | – | – | – | Binary outlet. |
| `"fan_onoff"` | on/off | 1 | – | – | – | Binary fan. |
| `"light_dimmable"` | dimmable | 1 | – | – | yes | AC phase-cut dimmer. |
| `"light_mono"` | dimmable | 1 | – | – | – | Single-channel PWM. |
| `"light_ww"` | dimmable + CCT | 2 | yes | – | – | Warm/cool white. |
| `"light_rgb"` | dimmable + color | 3 | – | yes | – | RGB. |
| `"light_rgbw"` | dimmable + color | 4 | – | yes | – | RGB + white. |
| `"light_rgbww"` | dimmable + CCT + color | 5 | yes | yes | – | RGB + warm/cool. |
| `"cover_blind"` | cover | — | – | – | – | Placeholder — see §14.3. Not implemented today. |

The category (rightmost-implicit grouping above) determines **which
commands are available** to the entity:

- **on/off** category → `turn_on`, `turn_off`, `toggle` only. No ramping,
  no brightness setting, no CCT, no color.
- **dimmable** category → on/off commands **plus** `set_light` and
  `stop_ramp` for brightness ramping.
- **dimmable + CCT** category → dimmable commands **plus** `ct` / `ct+`
  fields in `set_light`, and `ct_range` in config.
- **dimmable + color** category → dimmable commands **plus** `x` / `y`
  fields in `set_light`, and `calibration` in config.
- **dimmable + CCT + color** category → all of the above.

### 7b. Reserved entity categories (not yet implemented)

These categories are part of the protocol surface but are not produced by
the current firmware. Host code should be prepared to **see** entities of
these types (especially when a future firmware bridges sub-devices over
RS485) but does not need to send commands to them yet.

| Category | Expected `type` values | Notes |
|---|---|---|
| Keypad | `"keypad_<n>btn"` (TBD) | Emits keypress events; minimal config. |
| Presence sensor | `"presence_pir"` (TBD), `"presence_mmwave"` (TBD) | Emits presence/motion events. |
| Cover | `"cover_blinds"` (TBD), `"cover_curtain"` (TBD) `"cover_shade"` (TBD) | `open` / `close` / `set_position` commands. |

Commands for these categories are stubbed in section 14.

### 7c. Properties shared across all entities

Regardless of category, every entity has at minimum:

| Property | Type | Notes |
|---|---|---|
| `id` | string | Opaque entity ID. |
| `type` | string | One of the values in 7a or 7b. Host-changeable via `set_config`. |
| `tag` | string | 2–3 char display label. **Read-only — firmware-generated, not user-editable.** Appears in `get_config` responses but is not accepted as an input field in `set_config`. |

`set_config` for any entity accepts `type` (validated against the
category's allowed values) plus any category-specific fields documented in
sections 12–14. **Tags are not accepted in `set_config`** — they are
generated by firmware (today as `1A` … `1H` for the eight load entities).

---

## 8. Dimming modes (AC dimmer subtype only)

`mode` in `set_config` / `get_config` for `light_dimmable`:

| Value | Meaning |
|---|---|
| `"leading"` | Leading-edge phase-cut (inductive loads). |
| `"trailing"` | Trailing-edge phase-cut (capacitive loads). |
| `"auto"` | Firmware picks based on detected load (placeholder — currently falls back to leading). |

Persisted to NVS on change.

---

## 9. Device-level commands

`dst = <device entity ID>` for everything in this section.

### `device_get_info`

The public discovery call. Replaces the older `get_config` on the
device entity; non-admin clients use this to identify the device and
get the trimmed public loads/sensors/keypads views.

**Request:**
```json
{"req":"device_get_info","dst":"<device_id>","ref":"<opt>"}
```

**Response:**
```json
{
  "status": 200,
  "rsp":"device_get_info",
  "src":"<device_id>",
  "firmware_rev":"<x.y.z>",
  "model_num":"<model>",
  "hardware_rev":<0..15>,
  "serial_number":"<serial>",
  "location":"<user-set string or empty>",
  "platform": { "output_count": <int>, "input_count": <int> },
  "loads":   [ <trimmed per-load shape, see §11.1>, ... ],
  "sensors": [ <see §14.1>, ... ],
  "keypads": [ <see §14.1>, ... ]
}
```

No `api_key` field — there is no per-device API key any more.
Authentication is bearer-token only (§2.1). No `name` field either —
device identity comes from `model_num` / `serial_number`; only
`location` is user-settable. `hardware_rev` comes from the custom eFuse
field and is 0 when unprogrammed.

### `get_state`

**Request:**
```json
{"req":"get_state","dst":"<device_id>"}
```

**Response:**
```json
{"status":200,"rsp":"get_state","src":"<device_id>","uptime":<seconds>}
```

`uptime` is integer seconds since boot.

### `set_config`

Persists device-level config. Currently accepts:

| Field | Type | Notes |
|---|---|---|
| `location` | string (≤ `CONFIG_LOCATION_MAX_LEN` UTF-8 bytes) | Free-text label, surfaced in `device_get_info`. |
| `eth`, `wifi`, `zigbee` | objects | Network configuration sub-objects (see "Static network configuration" below). |

Any unknown top-level fields (including `name`) are silently ignored.
The device has no user-settable name — identity comes from
`model_num` / `serial_number`.

**Response:** `{"status":200}` on success; `{"status":500}` on malformed `location` /
`eth` / `wifi` / `zigbee`.

After applying the request, the device emits an Admin-only
`config_changed` event containing a `device_get_info` snapshot. Persistence
is attempted, but the `config_commit()` result is not surfaced; therefore
`status:200` confirms the runtime apply, not guaranteed survival across a
reboot.

### `identify`

Triggers a visual indication on the device. `{"status":200}`.

### `reboot`

Reboots after ~1 s. Server sends `{"status":200}` then disconnects.

### `factory_reset`

Erases NVS and reboots. `{"status":200}` then disconnect.

### `ping`

Liveness check with no side effects.

**Request:**
```json
{"req":"ping"}
```

**Response:**
```json
{"status":200,"rsp":"ping","src":"<device_id>","ts":<ms_since_boot>}
```

`ts` is the device's monotonic uptime in milliseconds (from
`esp_timer_get_time()`). The value is useful for detecting reboots between
pings — a `ts` that goes backward means the device rebooted.

Like all device-level commands, `dst` may be omitted (it defaults to the
device gateway).

### `list_nodes`

Returns the full entity topology. `dst` may be omitted; if present, any
value is accepted (this command does not filter by `dst`).

**Request:**
```json
{"req":"list_nodes"}
```

**Response:**
```json
{
  "status": 200,
  "rsp":"list_nodes",
  "src":"<device_id>",
  "nodes": {
    "<loads_group_id>": {
      "type":"dimac",
      "ch": 8,
      "loads":[ <load_entity_info_0>, <load_entity_info_1>, ... ]
    }
  }
}
```

Each `<load_entity_info_i>` has the same shape as that entity's
`get_config` + `get_state` responses merged (see sections 11–14).

> **Note for bridged-device builds.** Future firmwares that bridge
> sub-devices over RS485 will surface those sub-device load groups as
> additional keys in `nodes`. The exact `type` value of the group
> (`"dimac"` above) will vary; host code should iterate over `nodes`
> rather than assume a single key.

### `network_get_state` — **Admin only**

Returns network interface state across all enabled interfaces. `dst`
may be omitted. Rejected with `status:500` if the caller's token is
User-tier (the response reveals BSSID / RSSI / Zigbee PAN ID, which
are mildly sensitive).

**Response:**
```json
{
  "status": 200,
  "rsp": "network_get_state",
  "src": "<device_id>",

  "eth": {
    "state": "connected",        // down | connecting | connected | error_*
    "mode":  "dhcp",             // dhcp | static
    "mac":   "aa:bb:cc:dd:ee:ff",
    "ip4": {
      "ip":         "192.168.1.100",
      "nm":         "255.255.255.0",
      "gw":         "192.168.1.1",
      "dns_main":   "8.8.8.8",
      "dns_backup": "1.1.1.1"    // omitted when not configured
    },
    "speed_mbps": 100,           // single-port devices only (see note)
    "duplex":     "full",        // single-port devices only

    "ports": [                   // OPTIONAL — emitted only on devices
      {                          // with a multi-port switch chip
        "index":      0,         // (compile-gated by CONFIG_HAS_ETH_SWITCH
        "link_up":    true,      // on the firmware side). UI clients
        "speed_mbps": 1000,      // MUST silently skip when the key is
        "duplex":     "full"     // absent — render single-port mode.
      },                         // speed_mbps + duplex omitted if !link_up.
      { "index": 1, "link_up": false }
    ]
  },

  "wifi": {                      // present only if CONFIG_ENA_WIFI
    "state":    "connected",     // down | connecting | connected |
                                 // error_auth | error_no_ap | error_dhcp | error
    "ssid":     "MyNetwork",
    "bssid":    "11:22:33:44:55:66",
    "channel":  6,
    "band":     "2.4GHz",        // "2.4GHz" | "5GHz"
    "security": "WPA2_PSK",      // OPEN | WPA_PSK | WPA2_PSK | WPA_WPA2_PSK | WPA3_PSK
    "rssi":     -55,
    "mac":      "aa:bb:cc:dd:ee:ff",
    "ip4":      { ...same shape as eth.ip4... }
  },

  "zigbee": {                    // present only if CONFIG_ENA_ZIGBEE
    "state":            "connected",  // down | connecting | connected | error
    "role":             "coordinator",
    "pan_id":           "0x1A2B",
    "extended_pan_id":  "A0A1A2A3A4A5A6A7",
    "channel":          15,
    "permit_join_remaining_s": 0,     // > 0 when join window is open
    "device_count":     3
  }
}
```

**Notes on the shape:**

- The per-interface `state` field carries error info — there is **no
  separate `error` key**. UIs render the state string directly.
- `eth.speed_mbps` / `eth.duplex` are emitted only on devices that
  surface a single Ethernet PHY. Multi-port devices emit the `ports`
  array instead (each port's link/speed/duplex live on its array
  entry). UIs that don't render port detail can ignore the array.
- `wifi.ssid`, `wifi.bssid`, etc. are emitted only when state is
  `connecting` or `connected` (i.e., a network is selected).
- `zigbee.pan_id` / `extended_pan_id` / `channel` are emitted only
  when state is not `down`.

### Static network configuration — via `set_config` (**Admin only**)

Network configuration changes are sent via the device-level `set_config`
command, with `eth` / `wifi` / `zigbee` sub-objects matching the shape
of their entries in `network_get_state`. Only the fields that change
need to be present; unspecified fields stay at their current values.

```json
{
  "req": "set_config",
  "dst": "<device_id>",
  "eth": {
    "mode": "static",
    "ip4": {
      "ip":       "192.168.1.100",
      "nm":       "255.255.255.0",
      "gw":       "192.168.1.1",
      "dns_main": "8.8.8.8"
    }
  }
}
```

Switching `eth.mode` triggers a brief network reconnect on the device.
The device emits an Admin-only `config_changed` snapshot after applying
the request. Ethernet changes also emit `eth_state_changed`. NVS commit is
attempted, but its result is not surfaced; the response/event do not
guarantee that the setting will survive a reboot.

### WiFi action commands (**Admin only**, gated by `CONFIG_ENA_WIFI`)

Each returns `status:200` immediately if accepted; the actual result
arrives as an event (see §15).

#### `wifi_scan`

```json
{ "req": "wifi_scan" }
```

Triggers an async WiFi scan. Results are emitted as a
`wifi_scan_results` admin event when complete. Rate-limited to one
scan per 10 seconds — repeated calls within that window re-emit the
last cached results without re-scanning.

#### `wifi_connect`

```json
{ "req": "wifi_connect", "ssid": "MyNetwork", "password": "..." }
```

Saves the credentials to NVS (one slot, overwrites the prior network)
and initiates the connection. Progress reports via `wifi_state_changed`
events. The `password` field may be omitted for `OPEN` networks.

#### `wifi_disconnect`

```json
{ "req": "wifi_disconnect" }
```

Disconnects from the current network. Emits `wifi_state_changed` with
state `down`.

### Zigbee action commands (**Admin only**, gated by `CONFIG_ENA_ZIGBEE`)

#### `zigbee_permit_join`

```json
{
  "req": "zigbee_permit_join",
  "duration_s": 60,
  "install_code": "0123456789ABCDEF0123456789ABCDEF"  // optional, 16-byte hex
}
```

Opens the network for new devices to join for `duration_s` seconds
(0..254; pass 0 to close an open window early). The optional
`install_code` is the OOB code printed on a Zigbee 3.0 device; when
present, the coordinator pre-loads it so the next device that joins
with that IEEE address derives the correct link key.

Progress: emits `zigbee_permit_join_changed` when the window opens
and closes, and `zigbee_device_joined` once per device that joins
during the window.

#### `zigbee_remove_device`

```json
{ "req": "zigbee_remove_device", "id": "ZB_00158D000178A3B2" }
```

Removes a paired device from the network. Emits `zigbee_device_left`
on completion. Zigbee device entity IDs are formatted as
`ZB_<16-hex-IEEE-MAC>` (uppercase).

#### `zigbee_recreate_network`

```json
{ "req": "zigbee_recreate_network" }
```

Wipes the existing Zigbee network and re-forms a new one with fresh
PAN ID, extended PAN ID, and network/link keys. All previously-paired
devices will be orphaned and must be re-joined.

---

## 10. Loads-group commands

`dst = <loads_group_id>` for everything in this section. These commands
operate on the **group of load entities** as a whole.

### `get_config` (group)

Admin-only.

Returns the configuration of every load entity in the group as an array,
keyed by the **group entity ID itself**:

**Response:**
```json
{
  "status": 200,
  "rsp":"get_config",
  "src":"<loads_group_id>",
  "<loads_group_id>": [
    { /* load 0 config — same shape as per-load get_config */ },
    { /* load 1 config */ },
    ...
  ]
}
```

> Note the unusual shape: the array is keyed by the entity ID string, not
> by a stable field name like `"loads"` or `"entities"`. Host parsers must
> look up the key dynamically.
>
> **Design decision (intentional, do not change).** Keying by the entity
> ID is deliberate — it makes the response self-describing about which
> group the array belongs to, and matches the shape used by other
> entity-collection endpoints in this protocol. Client code should
> resolve the key from the `src` field of the response (which is the
> same string). This is closed-system protocol and we control the
> clients; the cost of dynamic-key lookup is one line.

### `set_output` (diagnostic; do not use in production clients)

Directly overrides a physical output's drive level, bypassing all
load-entity logic. The override stays in effect until the next `set_light`
on a load that owns that output, which clears it.

**Request:**
```json
{"req":"set_output","dst":"<loads_group_id>","ch":<0..7>,"value":<0..1000>}
```

| Field | Type | Range | Meaning |
|---|---|---|---|
| `ch` | int | 0..7 | **Physical output** index (not a load-entity index). |
| `value` | int | 0..1000 | Output intensity in 0.1% units. |

**Success behavior:** no response and no event. This command directly
overrides a physical output without changing load state; treat it as
fire-and-forget.

---

## 11. Per-load-entity commands — common shape

`dst = <load_entity_id>`. The commands accepted by an entity depend on its
**category** (section 7). The next three sections describe each category's
specifics; this section covers the parts every load entity shares.

### 11.1 `get_config` (common)

Returns the static configuration for a single load entity.

`get_config` is Admin-only for device, group, load, input, and keypad
targets. User-tier clients use `device_get_info` for public discovery.

**Common fields in the response (always present):**
```json
{
  "status": 200,
  "rsp":"get_config",
  "src":"<load_entity_id>",
  "id":"<load_entity_id>",
  "type":"<type_string>",
  "tag":"<short_label>"
}
```

**Additional fields depend on the category** — see 12 / 13 / 14.

### 11.2 `set_config` (common)

All fields are optional; omitted fields are unchanged. Every load entity
accepts `type`. `tag` is **not** an accepted input field (see section 7c).

**Common request shape:**
```json
{
  "req":"set_config",
  "dst":"<load_entity_id>",
  "type":"<type_string>",
  ... category-specific fields ...
}
```

**Validation:**
- `type` must be a recognized value (section 7a). Unknown strings produce
  `{"status":500}`; hardware-specific type gating is currently disabled.
- Changing `type` **resets all category-specific config** (map, basis,
  calibration, mode, etc.) to defaults before applying any other fields in
  the same message.

**Response:** `{"status":200}` (plus echoed `rsp`/`src`/`ref` per section 3c).

**Side effect:** Emits a `config_changed` event for this entity (and any
other entities whose physical output assignments were affected).

**Persistence:** All changes are flushed to NVS before the response is
sent.

### 11.3 `get_state` (common)

```json
{
  "status": 200,
  "rsp":"get_state",
  "src":"<load_entity_id>",
  "id":"<load_entity_id>",
  "type":"<type_string>",
  ... category-specific state fields ...
}
```

Load state always reports `fault` as an array of active fault codes:
`[]` (none), `["oc"]` (overcurrent), `["ot"]` (overtemperature), or
`["oc","ot"]` (both). Codes appear in that order, but clients should
treat the array as a set. This is a complete state snapshot, not a delta.
No request can set or clear faults; firmware manages them internally.
Overtemperature monitoring is not yet implemented, so `"ot"` is not
currently set by the running firmware. There are no `oc_fault` or
`ot_fault` fields in JSON.

---

## 12. Per-load-entity commands — on/off category

Applies to: `light_onoff`, `outlet_onoff`, `fan_onoff`.

These entities have **no brightness, no CCT, no color, no ramping** — they
are strictly binary.

### 12.1 `get_config`
Admin-only. User-tier clients discover the public load list through
`device_get_info`.

```json
{
  "status": 200,
  "rsp":"get_config",
  "src":"<load_entity_id>",
  "id":"<load_entity_id>",
  "type":"light_onoff",
  "tag":"1A",
  "map":[<physical_output_index>]
}
```

| Field | Notes |
|---|---|
| `map` | Single-element array selecting the physical output (`-1` = unassigned). |

### 12.2 `set_config`

Accepted fields: `type`, `map`. (Section 7c: `tag` is not user-editable.)

```json
{
  "req":"set_config",
  "dst":"<load_entity_id>",
  "type":"light_onoff",
  "map":[<physical_output_index>]
}
```

`map` length must equal 1; a mismatched array (or any other malformed
field) causes the whole request to be rejected with `status:500` and no
state is changed (see section 11.2). If the chosen output is already
owned by another entity, the firmware steals it and emits a
`config_changed` for the other entity.

### 12.3 `get_state`

```json
{
  "status": 200,
  "rsp":"get_state",
  "src":"<load_entity_id>",
  "id":"<load_entity_id>",
  "type":"light_onoff",
  "is_on": true,
  "fault": []
}
```

`is_on` (bool) is the canonical state field for this category. **No other
category-specific state fields are defined.** The common firmware-owned
`fault` is always present. The firmware does not
emit `brightness`, `ct`, `x`, or `y` for on/off entities.

### 12.4 `turn_on`

**Request:** `{"req":"turn_on","dst":"<load_entity_id>"}`
**Success:** silent; emits `state_changed` when load state changes.
**Effect:** sets `is_on = true`.

### 12.5 `turn_off`

**Request:** `{"req":"turn_off","dst":"<load_entity_id>"}`
**Success:** silent; emits `state_changed` when load state changes.
**Effect:** sets `is_on = false`.

### 12.6 `toggle`

**Request:** `{"req":"toggle","dst":"<load_entity_id>"}`
**Success:** silent; emits `state_changed` when load state changes.
**Effect:** flips `is_on`.

### 12.7 Not supported

`set_light`, `stop_ramp` → `{"status":500}` (validation failure).

---

## 13. Per-load-entity commands — dimmable category

Applies to: `light_dimmable`, `light_mono`.
and (with extensions) `light_ww`, `light_rgb`, `light_rgbw`, `light_rgbww`.

### 13.1 `get_config`
Admin-only. User-tier clients discover public load state through
`device_get_info` and `get_state`.

```json
{
  "status": 200,
  "rsp":"get_config",
  "src":"<load_entity_id>",
  "id":"<load_entity_id>",
  "type":"light_dimmable",
  "tag":"1A",
  "map":[<output_index>],
  "max_intensity": 800,
  "min_intensity": 50,
  "l_curve": "linear"
}
```

| Field | Notes |
|---|---|
| `map` | Length equals the LED count of the type (section 7a). Each entry is a physical output index or `-1`. |
| `max_intensity` | 0..1000 output cap. **Omitted from the response when at the disabled sentinel** (`1000` = no cap; legacy stored `0` is treated the same). When present, the load's output never exceeds this value. |
| `min_intensity` | 0..1000 output floor for non-zero brightness. **Omitted from the response when 0** (the disabled sentinel). When present, any non-zero brightness produces an output of at least `min_intensity`. Brightness exactly `0` always produces `0` regardless of this field — off is off. |
| `mode` | **Only for `light_dimmable` (AC dimmer):** `"leading"` / `"trailing"` / `"auto"`. See section 8. |
| `l_curve` | Dimmer response curve: `"linear"` (default), `"s_curve"` (Hermite smoothstep — eases at both endpoints), `"dali"` (IEC 62386 / 60929 logarithmic, 0.1 % floor), `"log"` (gentler logarithmic, 1 % floor). Stored per-load and persisted to NVS; defaults to `"linear"` when unset. Applied inside the gamma pipeline: `out = (b == 0) ? 0 : min + (max-min) * curve(b) / 1000`. |
| `ct_range` | **Only for CCT-capable types** (`light_ww`, `light_rgbww`): `[warm_K, cool_K]`. UI metadata only — see section 6. |
| `calibration` | **Only for color-capable types** (`light_rgb`, `light_rgbw`, `light_rgbww`) and only when at least one entry is non-zero: `[[rx,ry],[gx,gy],[bx,by]]` CIE xy for the three primaries. |

### 13.2 `set_config`

Accepted fields: `type`, `map`, `max_intensity`, `min_intensity`,
`l_curve`, plus the subtype-conditional fields above (`mode`,
`ct_range`, `calibration`). (Section 7c: `tag` is not user-editable.)

Validation rules:

- The whole request is **pre-validated** before any state is changed. If
  any field is malformed, the request is rejected with `status:500` and
  the entity is left unchanged. Specifically, the firmware rejects:
  - `type` that is not a recognized string (one of section 7a, or
    `"UNUSED"`),
  - `map` that is not an array, or whose length does not equal the LED
    count of the (possibly newly-set) type,
  - `max_intensity` / `min_intensity` / `max_current` that is not a number,
  - `ct_range` that is not a 2-element array of numbers,
  - `calibration` that is not a 3-element array of `[number, number]`
    pairs,
  - `mode` that is not one of `"leading"` / `"trailing"` / `"auto"`,
  - `l_curve` that is not one of `"linear"` / `"s_curve"` / `"dali"` / `"log"`.
- `map` entries are clamped at apply-time: entries outside
  `[-1, CONFIG_OUTPUT_COUNT-1]` are coerced to `-1` (this is **not** a
  validation failure — only structurally-malformed maps are rejected).
- If a chosen output is already mapped to another entity, the firmware
  steals it and emits a `config_changed` for the displaced entity.
- `mode` is applied only for AC dimmer types (silently ignored for
  others).
- `ct_range` is applied only for CCT-capable types (silently ignored for
  others).
- `calibration` is applied only for color-capable types; values
  round-trip through Q16 fixed-point and may show small quantization on
  read-back.

### 13.3 `get_state`

```json
{
  "status": 200,
  "rsp":"get_state",
  "src":"<load_entity_id>",
  "id":"<load_entity_id>",
  "type":"light_dimmable",
  "brightness": 750,
  "fault": []
}
```

| Field | Present when | Notes |
|---|---|---|
| `brightness` | always | 0..1000, **live** value (interpolated mid-ramp). |
| `ct` | CCT-capable types | 0..1000, live value. |
| `x`, `y` | color-capable types | CIE xy, live value. |
| `fault` | always | Array of active fault codes (`"oc"`, `"ot"`). Empty when healthy; firmware-owned. Overtemperature monitoring is not yet implemented. |
| `ramp` | a ramp is in progress | See 13.7. |

### 13.4 `turn_on`

Sets brightness to 1000 (100%) **instantly** (no ramp).

In CCT-capable / color-capable subtypes, **preserves** the previous
`ct.end`, `x.end`, `y.end` targets. In subtypes without CCT/color, this is
moot.

**Success:** silent; emits `state_changed` when load state changes.

### 13.5 `turn_off`

Sets brightness to 0 instantly. In CCT-capable / color-capable subtypes,
**preserves** `ct.end` / `x.end` / `y.end` so a subsequent `turn_on` (or
`toggle`) restores the previous color/CT targets.

**Success:** silent; emits `state_changed` when load state changes.

### 13.6 `toggle`

If `brightness.value > 0` → off (preserves color/CT targets in
CCT/color-capable subtypes).
If `brightness.value == 0` → on at 100% (preserves color/CT targets).

**Success:** silent; emits `state_changed` when load state changes.

### 13.7 `set_light`

The primary control command. All payload fields are optional.

```json
{
  "req":"set_light",
  "dst":"<load_entity_id>",
  "brightness":<0..1000>,    // OR "brightness+":<delta>
  "ct":<0..1000>,            // OR "ct+":<delta>     -- CCT-capable subtypes only
  "x":<float>, "y":<float>,                          //  color-capable subtypes only
  "duration":<ms>,           // OR "rate":<ms_per_unit>
  "ref":"<opt>"
}
```

**Brightness** (mutually exclusive; `brightness+` wins if both present):

| Field | Effect |
|---|---|
| `brightness` | Absolute target, clamped to `[0, 1000]`. |
| `brightness+` | Delta applied to the **live** value `brightness.value` (not the ramp target). Result clamped to `[0, 1000]`. |

**CCT** (CCT-capable subtypes only; mutually exclusive with `x`/`y`):

| Field | Effect |
|---|---|
| `ct` | Absolute 0..1000. Clears `x`/`y` to 0 in the same command. |
| `ct+` | Delta applied to live `ct.value`. Clears `x`/`y` to 0. |

**Color** (color-capable subtypes only; mutually exclusive with `ct` / `ct+`):

| Field | Effect |
|---|---|
| `x` + `y` (pair) | CIE xy targets. Clears `ct` to 0. Both must be present; passing only one is ignored. |

Sending CCT and color in the same message is undefined — clients should not.

**Ramp** (mutually exclusive; `duration` wins if both present):

| Field | Effect |
|---|---|
| `duration` | Absolute ramp duration in ms, clamped to `[300, 10000]`. Below 300 → instant. |
| `rate` | ms-per-unit-change. Duration is computed: `duration = (max_delta / 1000) * rate` where `max_delta = max(|brightness_new − brightness_live|, |ct_new − ct_live|)`. Then clamped to `[300, 10000]`. Below 300 → instant. |

**Inapplicable fields are silently dropped.** Sending `ct` or `ct+` to a
load that doesn't have CCT, or `x`/`y` to a load that doesn't have
color, is **not** an error — the firmware reads the load's type
([loads.h `is_load_light` / has_cct / has_color](main/load_light.c)) and
simply ignores the field. Even a malformed value (`"ct":"warm"`) on a
non-CCT load is ignored, not rejected. This is by design: clients that
push a kitchen-sink frame to a mixed set of entity types don't have to
branch on type at the call site. Strictness is reserved for fields that
apply to the load — those still produce 500 on malformed values.

**No-op detection.** If the computed `(brightness, ct, x, y, duration)`
exactly equals the current `.end` targets and `ramp_duration`, and there
is no fault, the command produces neither a response nor a
`state_changed` event (state-mutation success is silent — §3d).
Clients must not depend on confirmation arriving; poll `get_state` if
needed.

**Fault state.** `set_light`, `turn_on`, `turn_off`, and `toggle` do not
set or clear fault flags. Fault state is managed internally by firmware.

**Output-override clearing.** Any `set_light` clears `set_output`
overrides on all physical outputs in this entity's `map`.

**Success:** silent; emits `state_changed` on the next PWM tick when the
load state changed (see 15).

#### The `ramp` object

Present in `get_state` and `state_changed` **only while a ramp is in
progress**:

```json
"ramp": {
  "duration": <ms_total>,
  "elapsed":  <ms_since_start>,
  "start": {
    "brightness": <0..1000>,
    "ct":         <0..1000>,    // if CCT-capable
    "x":          <float>,       // if color-capable
    "y":          <float>
  },
  "end": {
    "brightness": <0..1000>,
    "ct":         <0..1000>,
    "x":          <float>,
    "y":          <float>
  }
}
```

`elapsed` is computed at message-generation time, so the host can
interpolate locally for smooth UI.

### 13.8 `stop_ramp`

Cancels any active ramp:
1. `is_ramping = false`
2. `ramp_duration = 0`
3. `brightness.end = brightness.value` (freezes the brightness target at
   the currently-interpolated value)

> Note: only the brightness target is frozen. `ct.end` / `x.end` / `y.end`
> are **not** updated by `stop_ramp` — if the ramp was driving a color/CT
> change, those revert to whatever they were before the ramp started.
> Follow `stop_ramp` with `get_state` if you need a definitive read.

**Success:** silent; emits a `state_changed` on the next PWM tick
(~10 ms) without a `ramp` object when the ramp state changed.

---

## 14. Per-load-entity commands — reserved categories

These categories are reserved in the protocol surface but **not
implemented** in the current firmware. They are documented here so host
code (and test generators) can plan for them.

### 14.1 Digital inputs, sensors, and the per-device keypad

**Implemented in current firmware.** This section is the source of
truth — supersedes the keypad placeholder previously in this slot.

The device exposes `CONFIG_INPUT_COUNT` digital input lines (4 on
dm8c, hardware-variant-driven via the `#define` in
`main/load_config.h`). The count is surfaced to clients in the
`platform` sub-object of `device_get_info` so the UI doesn't need to
discover it empirically.

Inputs are **buttons / sensors** — they have no user-facing
`name` or `location` (unlike loads). Admin-side configuration is just
`type`, `polarity`, and — for momentary inputs — `press_held_ms`.
Each input is identified to clients by:

- An opaque `id` (string) used as `dst` for admin commands.
- A short `tag` that is exactly the **1-based slot number as a
  decimal string** (`"1"`, `"2"`, …, `"4"` on dm8c). The same string
  doubles as the `key_id` in keypad events and `press` commands —
  callers don't have to derive it from the slot index.

Inputs surface to clients in one of three ways based on `type`:

| `type` | Public surface | Notes |
|---|---|---|
| `"UNUSED"` | none | omitted from `sensors[]` / `keypads[]` |
| `"sensor_light"`, `"sensor_motion"`, `"sensor_occupancy"`, `"sensor_opening"`, `"sensor_presence"`, `"sensor_door"`, `"sensor_window"` | `device_get_info.sensors[]` | read-only binary entity; emits `state_changed` |
| `"momentary_switch"` | one key in `device_get_info.keypads[0].keys[]` | emits `key_pressed` / `key_released` / `key_single_press` / `key_press_held` |

Regardless of type, every input slot appears in the admin-only
`list_nodes` view under the loads-group's `inputs[]` array, with full
configuration + override state. That's how admins reconfigure inputs
from the UI.

#### Debouncing

The firmware samples each input at a 10 ms cadence
(`INPUT_TICK_MS`). A raw pin level must hold for at least
**`INPUT_DEBOUNCE_MS` = 20 ms** (two consecutive samples) before it
commits to the input's debounced `physical` state. Polarity is then
applied to derive the logical `is_on`; an edge fires only on a real
logical change.

This is a firmware-internal detail — clients never see the bouncy
raw level, only the debounced edges (`state_changed` for sensors,
`key_*` events for momentary inputs). `set_override` bypasses the
debouncer (override is a software command, not a physical edge), so
test code can drive instantaneous transitions.

#### Admin view — `nodes.<loads_group_id>.inputs[]`

```json
{
  "id": "<input_entity_id>",
  "type": "sensor_motion"|"momentary_switch"|"UNUSED"|...,
  "tag": "2",
  "polarity": false,
  "is_on": false,
  "override_active": false,
  "press_held_ms": 1000      // only when type == momentary_switch
}
```

All slots are emitted (including UNUSED) — matches today's admin
treatment of load slots. No `name` / `location` fields.

#### Public view — `device_get_info.sensors[]`

One entry per input whose `type` starts with `sensor_*`:

```json
{
  "id": "<input_entity_id>",
  "type": "sensor_door",
  "tag": "2",
  "is_on": true
}
```

UNUSED and `momentary_switch` inputs are omitted. No `name` /
`location`.

#### Public view — `device_get_info.keypads[]`

At most one keypad entry per device, emitted **only when at least one
input has `type == momentary_switch`**. The single keypad object
collects every momentary input as a key:

```json
{
  "id": "<keypad_entity_id>",
  "type": "keypad",
  "tag": "K1",
  "keys": [
    { "id": "1", "has_led": false },
    { "id": "4", "has_led": false }
  ]
}
```

`keys[].id` is the input's `tag` (1-based slot number as a string).
`has_led` is **always `false`** on this hardware variant; LED-related
fields (`is_on`, `brightness`, `rgb`) and the `set_led` command are
not exposed.

#### Per-input commands — admin (`dst = <input_entity_id>`)

| Request | Effect |
|---|---|
| `get_config` | Returns the admin shape above. |
| `get_state` | Returns `{is_on, override_active}`. |
| `set_config` | Accepts `type`, `polarity`, `press_held_ms`. Pre-validated all-or-nothing per §11.2. Emits `config_changed`. **No** `name` / `location`. |
| `set_override` | `{is_on: bool}` — forces the **logical** `is_on` (bypasses polarity and debouncing). Emits `state_changed` (sensor) or `key_*` events (momentary). Admin-only. |
| `clear_override` | Restores physical-pin control. Emits an edge event if the resulting logical value differs from what the override was holding. Admin-only. |

#### Per-sensor commands — public (`dst = <sensor_entity_id>`)

A sensor's entity_id is the same as its input's entity_id. Public
clients can call:

| Request | Effect |
|---|---|
| `get_state` | Returns `{is_on, override_active}` from the shared input state. |

Input sensors share the same entity ID as their configurable input, so
`get_state` includes the input-only `override_active` boolean as well as
`is_on`. Load entities never report `override_active`.

Writes (`turn_on` / `turn_off` / `toggle` / `set_light`) → `status:500`.

#### Per-keypad commands (`dst = <keypad_entity_id>`)

| Request | Effect |
|---|---|
| `get_config` | Admin-only. Returns the keypad shape above. |
| `get_state` | Same shape as `get_config` (no dynamic per-keypad state on this variant). |
| `press` | `{key_id, duration?}` — simulates a press of the named key. `key_id` must equal one of the keypad's `keys[].id` values (i.e., an input's `tag`). Emits the same `key_*` event sequence a real press produces. `duration` defaults to a short tap when omitted; clamped to `[1, 60000]` ms. |

`set_led` → `status:500` (no LEDs on this hardware variant).

#### Key event sequence

For a single tap on a momentary input:

```
key_pressed → key_released (with `duration` ms) → key_single_press
```

For a held press past `press_held_ms` (per-input config; default
`INPUT_PRESS_HELD_DEFAULT_MS` = 1000 ms):

```
key_pressed → key_press_held (cadence: INPUT_PRESS_HELD_REPEAT_MS = 500 ms) ... → key_released (duration) → key_single_press
```

The repeating `key_press_held` cadence is a **global** firmware
constant (not per-input). `duration` on `key_released` and
`key_press_held` is the monotonic-ms hold time since `key_pressed`.

All key events share the envelope:

```json
{
  "evt": "key_pressed"|"key_released"|"key_single_press"|"key_press_held",
  "src": "<keypad_entity_id>",
  "keypad_id": "<keypad_entity_id>",
  "key_id": "1",
  "duration": 250          // only on key_released / key_press_held
}
```

`key_id` is the input's `tag` (1-based decimal string).

### 14.2 Presence sensors — TBD

`type` values: TBD (e.g. `"presence_pir"`, `"presence_mmwave"`).

Expected shape:

- `get_config` returns `{id, type, tag, ...}` with sensitivity / hold-time
  config fields.
- `get_state` returns `presence` (bool), `last_motion` (ms), etc.
- Emits `presence_changed` events.
- No `set_light` etc.

### 14.3 Covers (blinds / curtains) — TBD

`type` values: TBD (e.g. `"cover_blinds"`, `"cover_curtain"`).

Expected commands:

- `open`, `close`, `stop_motion`, `set_position` (0..1000).
- `get_state` returns `position` (0..1000), `moving` (bool), `direction`
  (`"opening"`/`"closing"`/`"stopped"`).
- Emits `state_changed` events.
- No `set_light`, `turn_on`, etc.

---

## 15. Async events

Events are broadcast to authenticated clients according to their scope.
Configuration, Ethernet, WiFi, and Zigbee events are Admin-only; load
state and input/key events go to authenticated clients. Clients must
dedupe against their own outgoing commands if they need to.

### 15.1 `state_changed`

Fires in three situations:

1. **Ramp start.** First PWM tick (~10 ms) after a `set_light` that
   initiated a ramp. **Includes** a `ramp` object.
2. **Ramp end.** When a ramp completes naturally or when `stop_ramp` is
   called. **Excludes** the `ramp` object.
3. **Immediate change.** After `turn_on`, `turn_off`, `toggle`, or a
  `set_light` with no ramp, or when a firmware-owned load fault changes.
  **Excludes** the `ramp` object.

**Shape** mirrors that entity's `get_state` response (minus the `rsp`
wrapper):

```json
{
  "evt":"state_changed",
  "src":"<load_entity_id>",
  "id":"<load_entity_id>",
  "type":"<type_string>",
  ... state fields per category (12.3 / 13.3 / 14) ...
}
```

For on/off entities the category-specific field is `is_on`; for dimmable
entities it is `brightness` plus `ct` / `x` / `y` per subtype. Common
The `fault` array is always present and replaces the previous snapshot's
array; it is not a list of newly added faults. `ramp` appears only during
a ramp.

### 15.2 `config_changed`

Admin-only. Fires after a successful `set_config` for the modified entity
and for any load entities whose `map` was implicitly modified by output
reassignment.

For load and input entities, the payload mirrors that entity's complete
`get_config` response (minus the wrapper). Device-level configuration
changes carry a `device_get_info` snapshot. `src` identifies the changed
entity.

```json
{
  "evt":"config_changed",
  "src":"<load_entity_id>",
  ... config fields per category ...
}
```

### 15.3 `modbus_keypress` (`CONFIG_MODBUS_KEYPAD` builds only)

```json
{
  "evt":"modbus_keypress",
  "src":"<serial>:modbus:1",
  "addr":<int>,
  "key":<int>,
  "duration":<int>
}
```

| Field | Notes |
|---|---|
| `addr` | Modbus device address (1..N). |
| `key` | Key index pressed. |
| `duration` | Press duration; units defined by firmware build. |

The trailing `:1` in `src` identifies the modbus bus (always 1 today).
This event is the precedent for the future generic `keypress` event
(section 14.1).

### 15.4 Ethernet events (**Admin-only**)

#### `eth_state_changed`

Fires whenever any field of the Ethernet interface state changes:
link up/down, DHCP lease acquired/renewed/expired, IP/netmask/gw
change, DNS change, or (on devices with `CONFIG_HAS_ETH_SWITCH`)
per-port link state. The payload is the same `eth` object you'd see
in `network_get_state`.

```json
{
  "evt":   "eth_state_changed",
  "src":   "<device_id>",
  "eid":   "<8 digits>",
  "state": "connected",
  "mode":  "dhcp",
  "mac":   "...",
  "ip4":   { ... },
  "speed_mbps": 100,
  "duplex":     "full",
  "ports": [ ... ]    // if CONFIG_HAS_ETH_SWITCH
}
```

The device caches the most recent interface state locally; the next
`network_get_state` request returns the same shape the event carried.
Clients can rely on the event being a complete snapshot, not a delta.

### 15.5 WiFi events (**Admin-only**, `CONFIG_ENA_WIFI` builds)

These are filtered at broadcast time and delivered only to clients
authenticated at Admin tier.

#### `wifi_state_changed`

Fires on every WiFi state transition (connect / disconnect / failure).
The payload mirrors the `wifi` object from `network_get_state`.

```json
{
  "evt":   "wifi_state_changed",
  "src":   "<device_id>",
  "state": "connected",      // or connecting | down | error_*
  "ssid":  "...",
  "bssid": "...",
  "channel": 6,
  "band":  "2.4GHz",
  "security": "WPA2_PSK",
  "rssi":  -52,
  "ip4":   { ... }
}
```

#### `wifi_scan_results`

Fires once after a `wifi_scan` completes (or immediately with cached
data if the rate-limit window suppressed an actual scan).

```json
{
  "evt": "wifi_scan_results",
  "src": "<device_id>",
  "ts":  <ms_since_boot>,
  "aps": [
    {
      "ssid":     "MyNetwork",
      "bssid":    "11:22:33:44:55:66",
      "channel":  6,
      "rssi":     -45,
      "security": "WPA2_PSK"
    },
    ...
  ]
}
```

### 15.6 Zigbee events (**Admin-only**, `CONFIG_ENA_ZIGBEE` builds)

#### `zigbee_permit_join_changed`

```json
{
  "evt":   "zigbee_permit_join_changed",
  "src":   "<device_id>",
  "open":  true,
  "remaining_s": 60
}
```

Fires when the join window opens (in response to `zigbee_permit_join`
with `duration_s > 0`) and when it closes (timeout or explicit close).

#### `zigbee_device_joined`

```json
{
  "evt":         "zigbee_device_joined",
  "src":         "<device_id>",
  "id":          "ZB_00158D000178A3B2",
  "ieee_addr":   "00:15:8D:00:01:78:A3:B2",
  "short_addr":  "0x1234"
}
```

Fires once per device that joins during a permit-join window. The `id`
is the Zigbee entity ID (used as `dst` for future per-device commands);
`ieee_addr` is the same MAC formatted human-readably.

The device-level firmware purposely keeps the join event lean — just
enough to identify the device. Friendly names and location strings
live on the load entity that gets associated with the Zigbee device
later, via the normal `set_config` flow on that entity. Device-class
metadata (clusters, endpoints, etc.) is not surfaced — the UI doesn't
need it for pairing.

#### `zigbee_device_left`

```json
{
  "evt": "zigbee_device_left",
  "src": "<device_id>",
  "id":  "ZB_00158D000178A3B2"
}
```

Fires when a device is removed (via `zigbee_remove_device`) or leaves
the network spontaneously.

---

## 16. Error handling

Successful requests that have responses use `status:200`. Silent
state-mutation commands produce no success response. Every failure uses
`status:500`, regardless of whether the cause is malformed input,
authorization, readiness, unsupported operation, or an internal error.
The firmware does **not** report the specific failure reason on the wire;
details belong in device logs.

esp_err_t → status mapping (firmware-side, for reference):

| `esp_err_t` | Status |
|---|---|
| Any non-`ESP_OK` result | 500 |

Error responses carry the standard `rsp` / `src` / `ref` echo per
section 3c.

---

## 17. Known quirks (current shipping behavior)

These describe the live firmware. Fixes are listed in section 18; until
those land, tests should match the behavior below.

1. **`set_light` no-op silently drops events** (this is the documented
   no-op detection; no `state_changed` fires because nothing changed).
   Combined with the silent-on-success rule for state-mutation commands
   (section 3d), a no-op `set_light` produces **no response and no
   event** — the client's UI should already reflect the requested state
   since nothing changed. If you absolutely need to confirm, follow
   with a `get_state`. See section 13.7.
2. **`stop_ramp` does not return current state.** Follow with `get_state`.
3. **`brightness+` uses the live value mid-ramp** — delta applied to the
   interpolated brightness, not the ramp target.
4. **CT is a percentage, not Kelvin.** See section 6 (this is by design,
   not a bug — listed here because clients commonly expect Kelvin).

---

## 18. Proposed changes — NOT YET IMPLEMENTED

Recommendations open for discussion. None of these are in shipping
firmware; host test code should be written against sections 1–17.

_(No proposed changes pending; previous P1 and P2 entries have been addressed in code — see `tests/CODE_REVIEW.md`.)_

---

## 19. Compile-time feature flags reference

| Flag | Effect on protocol |
|---|---|
| `CONFIG_USE_SSL` | Switches transport to `wss://` on port 443. Currently unset. |
| `CONFIG_LOAD_DIMMER_AC` | Selects AC phase-cut dimmer hardware and enables its `mode` config field. It does not currently restrict recognized types in `set_config` (see §7a). |
| `CONFIG_LOAD_DIMMER_PWM` | Selects PWM driver hardware. Sets factory default type to `light_mono`. (Mutually exclusive with the AC flag.) |
| `LOAD_SUPPORT_CC` | Enables `max_current` field. Not fully wired in current firmware. |
| `CONFIG_MODBUS_KEYPAD` | Enables `modbus_keypress` event. |
| `CONFIG_ENA_WIFI` | Adds a `wifi` object to `network_get_state`. |
| `CONFIG_ALLOW_CORSX` | Adds CORS headers to HTTP responses. Currently unset. |
| `CONFIG_SELF_HOSTED_PAGE` | Enables the embedded web UI at `GET /`. **Currently set.** |

`CONFIG_RAMP_DURATION_MIN` / `CONFIG_RAMP_DURATION_MAX` /
`CONFIG_LIGHT_VALUE_MAX` are numeric (300, 10000, 1000 in the shipping
build) and should be treated as fixed by host code.

---

## 20. For LLM / test-generator consumers

If you are an LLM generating tests against this protocol:

1. **Treat sections 1–17 as the contract.** Generate tests for those.
2. **Reserved categories (section 14) are placeholders only.** Do not
   generate tests against keypad / presence / cover commands until those
   sections are filled in with concrete shapes.
3. **Do not generate tests for section 18.** Those changes are
   unimplemented.
4. **Quirks in section 17 are facts, not bugs to test as "should be
   fixed".** Tests should reflect today's behavior.
5. **Compile-time feature flags (section 19) gate which fields exist.**
   Don't assert presence of CCT/color fields unconditionally — gate on
   the entity type as documented in section 7a.
6. **Authentication is intentionally out of scope** in this revision
   (section 2). Don't generate auth tests yet.
7. **Entity IDs are opaque.** Never split, parse, or pattern-match an
   entity ID — discover them via `get_config` / `list_nodes`.
8. **All percentage values are integers 0..1000 where 1000 = 100%.** No
   floats for brightness / CT / `max_intensity`. CIE xy are the only
   float values on the wire.
9. **Correlation is by `rsp` + `src` (always echoed) plus optional `ref`.**
   Every response — data, ack, or error — carries `rsp` (the request's
   command name) and `src` (the resolved target, defaulting to the device
   gateway ID when `dst` was omitted). `ref` is additionally echoed if
   the request set it. Tests can match on any combination.
10. **`dst` is optional for device-level and gateway commands.** When
    omitted, the device gateway is the implicit target and `src` in the
    response will be the device gateway ID. Per-load-entity commands
    (sections 12–14) must include `dst`.
11. **Tags are read-only.** `tag` appears in `get_config` responses but
    must not be set via `set_config` — the firmware ignores the field.
12. **Each command's full request/response shape is described in
    sections 9–15.** Treat field names and types as exact. The firmware
    identifiers in `tagonet_const.h` (`PROP_*`, `REQ_*`, `EVT_*`) are
    the wire strings.

---
