# Trigger & Receiver Resources

This directory contains standalone Trigger/Receiver scripts (`resources/triggers/<folder>/<name>/`).

## Conventions

- **Resource ID**: `trigger.<folder>.<name>` (registered in `registry/triggers.json`).
- **Entry**: `entry.py` (pure Python 3 standard library, zero pip dependencies) + `README.md`.
- **Environment Variables**: All variables must use the `VINPUT_TRIGGER_*` namespace.
- **Lifecycle**: Long-lived child processes spawned and supervised by `vinput-daemon` (`ProcessSession`).

## Wire Protocol

Triggers control the daemon via JSONL on `stdout` (`start` / `stop` / `cancel` / `inject`),
receive status and results on `stdin` (`status` / `partial` / `committed`),
and emit desktop notifications on `stderr` (`NotificationBridge` contract).

See `.agents/skills/vinput-registry-dev/references/trigger-protocol.md` for the frozen protocol contract.
