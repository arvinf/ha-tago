# Wire scenario fixtures

`wire_scenarios.json` is the shared scenario set defined and owned by the
[tagoesp firmware repo](../../../tagoesp/tests/vectors/wire_scenarios.json).
The integration's tests treat it as **read-only**.

This directory holds a **snapshot** so CI (and any contributor without the
firmware repo checked out alongside) can run the suite.

The loader at [`tests/scenarios.py`](../scenarios.py) prefers paths in this
order:

1. `$TAGO_WIRE_SCENARIOS` (env override) — for ad-hoc runs against a
   work-in-progress firmware branch.
2. `../tagoesp/tests/vectors/wire_scenarios.json` (sister repo) — used on
   developer machines where both repos are siblings.
3. `tests/vectors/wire_scenarios.json` (this file) — used in CI.

## Updating the snapshot

When the firmware team adds or changes a scenario, run:

```bash
./tests/vectors/sync.sh
```

This copies the file from `../tagoesp/tests/vectors/wire_scenarios.json`
(if present) into this directory. Commit the change with a message like
`tests: sync wire_scenarios.json with firmware <sha>`.

If the firmware repo lives somewhere other than `../tagoesp`, override:

```bash
TAGOESP_DIR=/path/to/tagoesp ./tests/vectors/sync.sh
```
