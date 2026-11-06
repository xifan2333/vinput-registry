# Receiver Resources

This directory contains standalone Receiver scripts (`resources/receivers/<folder>/<name>/`).

Receivers primarily **consume** the daemon's capture/ASR/LLM pipeline output
(subtitle HUDs, clipboard bridges, AI forwarders). The pipe protocol is symmetric:
a receiver may still emit `start` (e.g. to open its own background session with `"ime": false`).

## Conventions

- **Resource ID**: `receiver.<folder>.<name>` (registered in `registry/receivers.json`).
- **Entry**: `entry.py` (pure Python 3 standard library, zero pip dependencies) + `README.md`.
- **Environment Variables**: All variables must use the `VINPUT_RECEIVER_*` namespace.
- **Lifecycle**: Long-lived child processes spawned and supervised by `vinput-daemon` (`ProcessSession`).

## Wire Protocol

Clients control the daemon via JSONL on `stdout` (`start` / `stop` / `cancel` / `inject`),
receive status and results on `stdin` (`status` / `partial` / `committed`),
and emit desktop notifications on `stderr` (`NotificationBridge` contract).

See `.agents/skills/vinput-registry-dev/references/client-protocol.md` for the frozen protocol contract.
