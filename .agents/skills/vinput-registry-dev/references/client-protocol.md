# Client Pipe Protocol Specification

This document freezes the communication protocol between `vinput-daemon` and its managed client scripts in `resources/triggers/` and `resources/receivers/`.

---

## 1. Architectural Role & Positioning

In `fcitx5-vinput`, `vinput-daemon` functions as a **dispatching hub** for voice and text processing pipelines:
- Audio capture (PipeWire), voice activity detection (VAD), speech recognition (ASR via sherpa-onnx or cloud providers), and LLM scene post-processing are encapsulated in the daemon.
- External components are supervised child processes (**clients**) of the daemon.
- At the protocol layer every client is symmetric: any client may send commands, and every client receives broadcasts. A client can act as a trigger (initiating/controlling sessions), a receiver (consuming results), or both simultaneously.

---

## 2. Domain Layer: Triggers vs Receivers

The **protocol layer does not distinguish** client kinds — the wire format is identical for all. The **domain layer (registry) does distinguish** resource types by their primary role:

| Resource Type | Primary Role | Examples | Env Namespace |
| :--- | :--- | :--- | :--- |
| `trigger.<folder>.<name>` | Session control logic | Mouse side buttons, Bluetooth remotes, foot pedals, phone web remote | `VINPUT_TRIGGER_*` |
| `receiver.<folder>.<name>` | Consuming recognition results (reusing the daemon's capture/ASR/LLM pipeline) | Subtitle HUDs, clipboard bridges, AI forwarders | `VINPUT_RECEIVER_*` |

- The registry type expresses the resource's **primary purpose** for catalog display and UI presentation.
- The protocol does **not** restrict capabilities: a receiver may still emit `start` (e.g. to open its own background session with `"ime": false`), and a trigger may read `stdin` broadcasts.

---

## 3. Unix Filter Perspective: Three-Stream Wire Contract

To align with standard Unix filters (`producer | consumer`) and project terminology:
- **Daemon's input** is received from the Client's `stdout`.
- **Daemon's output** is written to the Client's `stdin`.
- **Desktop notifications** flow from the Client's `stderr` to the daemon's `NotificationBridge`.

All messages across `stdin` and `stdout` are strictly **JSON Lines (JSONL)**: single-line JSON objects encoded in UTF-8 and terminated by `\n`. Every line must be flushed immediately.

```
       ┌────────────────────────┐
       │     Client Process     │
       │  (Trigger / Receiver)  │
       └────┬──────▲──────┬─────┘
   stdout   │      │      │  stderr (NotificationBridge)
 (commands) │      │      │  {"title":"..","body":".."}
            ▼      │      ▼
       ┌───────────┴────────────┐
       │     vinput-daemon      │ ───> D-Bus (ime: true only)
       │    (Dispatcher Hub)    │ ───> Desktop Notifications
       └────────────────────────┘
                 stdin
          (broadcast results)
```

---

## 4. Client stdout -> Daemon Input: Command Stream

Clients write single-line JSON objects to their `stdout` to control sessions or inject text. Every command must contain `"type"`.

### 1. `start` (Initiate Recording Session)
Begins microphone audio capture and ASR processing in the daemon.
```json
{"type": "start", "ime": true, "mode": "normal", "selected_text": "", "capture_device": ""}
```
- `type` (string, required): `"start"`
- `ime` (boolean, optional, default `true`):
  - When `true`: Managed as an active input method session. Status bar shows recording, preedit candidate underlines appear at the text cursor, and final text commits directly to the active application.
  - When `false`: Silent background mode. The input method remains completely unaware (no status bar changes, no candidate popups, no text committed to cursor). Output is broadcast exclusively to connected receivers via `stdin`.
- `mode` (string, optional, default `"normal"`):
  - `"normal"`: Standard speech-to-text dictation.
  - `"command"`: Selection rewrite command mode.
- `selected_text` (string, optional): Required when `mode == "command"`, passing the highlighted surrounding text to LLM prompt templates.
- `capture_device` (string, optional, default `""`):
  - Per-session override of the globally configured capture device (`/global/capture_device`, i.e. `AudioCapture::SetTargetObject`).
  - When omitted or empty, the session uses the global configuration (which itself falls back to the PipeWire system default source when empty).
  - When set, the daemon connects this session's capture to the specified PipeWire source node. If the node cannot be matched, the daemon falls back to the global/default source and emits exactly one desktop notification (never fails silently).
  - Designed for near-field recording peripherals (Bluetooth remotes with built-in microphones): the trigger script typically forwards its own configured `VINPUT_TRIGGER_CAPTURE_DEVICE` value.

### 2. `stop` (Finish Recording & Finalize)
Concludes audio capture, flushes audio buffers to the ASR engine, and executes post-processing scenes.
```json
{"type": "stop", "ime": true, "scene": "translate_en"}
```
- `type` (string, required): `"stop"`
- `ime` (boolean, required): Must match the session's IME mode.
  - When `true`: If the post-processing scene produces multiple candidates, the input method opens the candidate selection menu.
  - When `false`: Suppresses all candidate menus; dispatches final candidates directly to the result broadcast.
- `scene` (string, optional): Specific scene ID to apply (e.g. `"translate_en"`, `"polish"`). Defaults to the active scene if omitted.

### 3. `cancel` (Abort or Settle Session)
Aborts an in-progress session with two-phase semantics based on daemon state.
```json
{"type": "cancel", "ime": true, "commit_raw": false}
```
- `type` (string, required): `"cancel"`
- `ime` (boolean, required): Determines where raw text is delivered if `commit_raw: true`.
- `commit_raw` (boolean, optional, default `false`):
  - **In `recording` phase**: Aborts capture and discards all buffers immediately. `commit_raw` is ignored because no transcription is available.
  - **In `postprocessing` phase**:
    - `commit_raw: false`: Discards the session completely (corresponds to keyboard `Escape`).
    - `commit_raw: true`: Bypasses slow LLM processing and commits the raw ASR text immediately (corresponds to keyboard `Return`).

### 4. `inject` (External Text Injection)
Injects text directly into the processing pipeline without capturing audio from PipeWire.
```json
{"type": "inject", "stage": "partial", "text": "transcribing text...", "ime": true}
{"type": "inject", "stage": "final", "text": "Complete sentence.", "ime": true, "scene": "default"}
```
- `type` (string, required): `"inject"`
- `stage` (string, required):
  - `"partial"`: Interim progress update (mirrors candidate preedit).
  - `"final"`: Complete utterance ready for scene transformation and commitment.
- `text` (string, required): Text payload.
- `ime` (boolean, required): Whether to target the desktop input method context.
- `scene` (string, optional): Applicable only when `stage == "final"`. Routes text through the specified LLM scene before dispatching.
- **Stateless Daemon Principle**: The daemon does **not** debounce or buffer partial injection. The trigger script owns debounce timers and WebSocket framing, emitting `stage: "final"` only when the sentence is definitive.

---

## 5. Daemon Output -> Client stdin: Broadcast Stream

The daemon broadcasts lifecycle state, interim partials, and final committed results to the `stdin` of all supervised clients.

### 1. `status` (Daemon State Update)
Emitted whenever the internal state machine transitions.
```json
{"type": "status", "status": "recording", "ime": true}
```
- `status` (string, required): One of `"idle"`, `"recording"`, `"inferring"`, `"postprocessing"`, `"error"`.
- `ime` (boolean, required): Identifies whether the active session belongs to the input method.

### 2. `partial` (Interim Transcription)
Emitted in real-time as the ASR engine accumulates text.
```json
{"type": "partial", "text": "recognizing audio", "ime": true}
```
- `text` (string, required): Cumulative unconfirmed transcript.
- `ime` (boolean, required): Target context flag.

### 3. `committed` (Final Result Delivery)
Emitted when recognition and post-processing finish successfully.
```json
{
  "type": "committed",
  "text": "Hello world.",
  "candidates": [
    {"text": "Hello world.", "source": "llm"},
    {"text": "Hello world", "source": "raw"}
  ],
  "ime": true
}
```
- `text` (string, required): Committed final string.
- `candidates` (array of objects, optional): Candidate list with `text` and `source` (`"llm"` | `"raw"` | `"asr"`).
- `ime` (boolean, required): `true` indicates text has been committed to the Fcitx5 cursor; `false` indicates silent background delivery.

---

## 6. Client stderr -> User: Desktop Notifications

Client `stderr` is strictly reserved for user-visible notifications routed through `NotificationBridge`. **Never log routine operational output, debug lines, or banners to `stderr`**.

- **Format A (Structured JSON)**:
  ```json
  {"title": "Mouse Trigger", "body": "Permission denied opening /dev/input/event3. Add user to 'input' group."}
  ```
- **Format B (Plain Text)**:
  ```text
  Bluetooth remote disconnected
  ```
  (The host wraps plain text lines with a standard title and displays a desktop notification).

---

## 7. Hardware Keys & Peripheral Boundaries

1. **No Daemon Key Remapping**: The daemon does **not** emulate virtual keyboards, forward menu navigation keys (`Up`/`Down`/`Enter`), or implement virtual key injection.
2. **Peripheral-Local Handling**:
   - If a peripheral (such as a Bluetooth remote or foot pedal) generates standard Linux input events, the operating system and window manager process them natively.
   - If a custom script captures raw hardware events, it sends `start`/`stop` to `vinput-daemon` for speech, and directly emits standard key events via `/dev/uinput` or tools like `ydotool` if virtual keys are required.

---

## 8. Reference Implementation: Mouse Side-Button Trigger

A pure trigger: it only emits commands on `stdout` and never reads `stdin`. It forwards its configured capture device so a mic-bearing remote records from its own near-field microphone:

```python
#!/usr/bin/env python3
"""Mouse side button push-to-talk trigger."""

import json
import os
import struct
import sys

EVENT_FORMAT = "llHHI"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
BTN_SIDE = 0x113  # Linux input event code

CAPTURE_DEVICE = os.environ.get("VINPUT_TRIGGER_CAPTURE_DEVICE", "")


def send_command(cmd: dict) -> None:
    if CAPTURE_DEVICE and cmd.get("type") == "start":
        cmd["capture_device"] = CAPTURE_DEVICE
    sys.stdout.write(json.dumps(cmd) + "\n")
    sys.stdout.flush()


def main() -> None:
    try:
        with open("/dev/input/by-id/my-mouse-event", "rb") as dev:
            while True:
                data = dev.read(EVENT_SIZE)
                if not data:
                    break
                _sec, _usec, ev_type, code, value = struct.unpack(EVENT_FORMAT, data)
                if ev_type == 1 and code == BTN_SIDE:  # EV_KEY
                    if value == 1:  # Key down
                        send_command({"type": "start", "ime": True})
                    elif value == 0:  # Key up
                        send_command({"type": "stop", "ime": True})
    except PermissionError:
        sys.stderr.write(
            json.dumps(
                {
                    "title": "Mouse Trigger",
                    "body": "Cannot read mouse device: permission denied.",
                }
            )
            + "\n"
        )
        sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
```
