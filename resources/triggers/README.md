# Trigger Resources

This directory contains standalone Trigger scripts (`resources/triggers/<folder>/<name>/`).

Triggers primarily **control** daemon sessions (mouse buttons, Bluetooth remotes,
foot pedals, phone web remote). The pipe protocol is symmetric: a trigger may also
read `stdin` broadcasts.

## Conventions

- **Resource ID**: `trigger.<folder>.<name>` (registered in `registry/triggers.json`).
- **Entry**: `entry.py` (pure Python 3 standard library, zero pip dependencies) + `README.md`.
- **Environment Variables**: All variables must use the `VINPUT_TRIGGER_*` namespace.
- **Lifecycle**: Long-lived child processes spawned and supervised by `vinput-daemon` (`ProcessSession`).
- **Capture Device**: A mic-bearing trigger may forward `VINPUT_TRIGGER_CAPTURE_DEVICE`
  as the optional `capture_device` field of `start` to record from its own near-field source.

## Wire Protocol

Clients control the daemon via JSONL on `stdout` (`start` / `stop` / `cancel` / `inject`),
receive status and results on `stdin` (`status` / `partial` / `committed`),
and emit desktop notifications on `stderr` (`NotificationBridge` contract).

See `.agents/skills/vinput-registry-dev/references/client-protocol.md` for the frozen protocol contract.
