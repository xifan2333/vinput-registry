# Domain Glossary

Shared vocabulary for `vinput-registry`. Skills and agents must use these terms as defined here; when a concept is missing, that is a signal to add it here rather than to invent a synonym.

---

## Wire Protocols (Protocol Layer)

### Three-Stream Contract

The foundational standard I/O boundary every helper script follows. Each stream has exactly one job: `stdin` for input, `stdout` for business output, and `stderr` strictly for user notifications.

_Avoid_: Standard logging, mixed diagnostic output.

### Client Protocol

The symmetric bidirectional JSONL wire contract between `vinput-daemon` and supervised helper client processes (triggers and receivers). Commands flow on client `stdout`, broadcasts flow on client `stdin`.

_Avoid_: Trigger protocol, receiver protocol.

### Streaming ASR Protocol

The duplex JSONL wire contract between `vinput-daemon` and cloud ASR provider scripts during active recording. Receives audio chunk events on `stdin` and emits live `partial` text and a single `final` transcript on `stdout`.

_Avoid_: Real-time ASR, online ASR, pipe stream ASR.

### Batch ASR Protocol

The one-shot wire contract between `vinput-daemon` and cloud ASR provider scripts after recording completes. Consumes raw uncompressed PCM bytes on `stdin` and writes the final transcript string directly to `stdout`.

_Avoid_: Non-streaming ASR, offline ASR (in cloud provider context), one-shot ASR.

### Adapter Protocol

The one-shot Unix pipe filter contract between `vinput-daemon` and local scene text transformation scripts. Consumes raw transcript text or command JSON on `stdin` and writes rewritten candidate text directly to `stdout`.

_Avoid_: HTTP adapter, LLM proxy, loopback adapter, command adapter.

### Notification Channel

The universal desktop notification stream carried on helper `stderr`. Strictly formatted as single-line JSONL (`{"title": "...", "body": "..."}`), delivered to `NotificationBridge`, and completely silent during normal operation.

_Avoid_: Log channel, stderr diagnostics, console banner.

---

## Helpers and Resources (Registry Layer)

### ASR Provider

A standalone cloud speech-to-text script in `resources/providers/` executing either the Streaming ASR Protocol or Batch ASR Protocol.

_Avoid_: Recognition plugin, speech engine.

### Adapter

A standalone local text transformation script in `resources/adapters/` executing the Adapter Protocol to rewrite, clean, or format transcripts for an LLM scene.

_Avoid_: Scene proxy, LLM adapter service.

### Client

A supervised long-lived helper process executing the Client Protocol. Categorized in the registry by primary role as either a **Trigger** or a **Receiver**, though both share the identical symmetric wire protocol.

_Avoid_: Peripheral daemon, external plugin.

### Trigger

A Client whose primary role is initiating and controlling voice input sessions (e.g. mouse side buttons, Bluetooth remotes, web remote).

### Receiver

A Client whose primary role is consuming recognition and post-processing results (e.g. subtitle HUDs, clipboard bridges).

---

## Speech and Utterance Lifecycle

### Session

The complete interaction lifecycle from when the user presses the record key (or a client emits `start`) to when recording finishes or aborts.

### Utterance

A single spoken sentence or semantic unit within a session.

### Partial Text

Cumulative visible unconfirmed transcript emitted by streaming ASR providers for live candidate preedit display.

_Avoid_: Interim text, tentative result.

### Final Text

The confirmed final transcript for an utterance emitted by an ASR provider.

_Avoid_: Committed text (at provider layer).

### Single Final per Utterance

The mandatory contract requiring a streaming ASR provider to emit exactly one `final` event at session conclusion, buffering or accumulating any mid-utterance completions as `partial` text.

_Avoid_: Multi-final streaming, chunk finals.

### Committed Text

The final confirmed text delivered by the daemon to the active desktop input context or broadcast to connected clients via `committed`.
