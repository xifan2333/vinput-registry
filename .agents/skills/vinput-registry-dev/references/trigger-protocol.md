# Trigger & Receiver Protocol Specification

This document details the communication protocol between `vinput-daemon` and peripheral Triggers/Receivers in `resources/triggers/`.

---

## 1. Architectural Role & Positioning

In `fcitx5-vinput`, `vinput-daemon` functions as a **dispatching hub (dispatcher)** for voice and text processing pipelines:
- Audio capture (PipeWire), voice activity detection (VAD), speech recognition (ASR via sherpa-onnx or cloud providers), and LLM scene post-processing are encapsulated in the daemon.
- External components are **clients** of the daemon.
- A client can act as a **Trigger** (controller initiating/stopping sessions), a **Receiver** (consumer displaying/routing results), or **both simultaneously**.
- Communication is conducted over a persistent bidirectional child process pipeline (Three-stream Subprocess Model).

---

## 2. Unix Filter Perspective: Three-Stream Wire Contract

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

## 3. Client stdout -> Daemon Input: Command Stream

Clients write single-line JSON objects to their `stdout` to control sessions or inject text. Every command must contain `"type"`.

### 1. `start` (Initiate Recording Session)
Begins microphone audio capture and ASR processing in the daemon.
```json
{"type": "start", "ime": true, "mode": "normal", "selected_text": ""}
```
- `type` (string, required): `"start"`
- `ime` (boolean, optional, default `true`):
  - When `true`: Managed as an active input method session. Status bar shows recording, preedit candidate underlines appear at the text cursor, and final text commits directly to the active application.
  - When `false`: Silent background mode. The input method remains completely unaware (no status bar changes, no candidate popups, no text committed to cursor). Output is broadcast exclusively to connected receivers via `stdin`.
- `mode` (string, optional, default `"normal"`):
  - `"normal"`: Standard speech-to-text dictation.
  - `"command"`: Selection rewrite command mode.
- `selected_text` (string, optional): Required when `mode == "command"`, passing the highlighted surrounding text to LLM prompt templates.

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

## 4. Daemon Output -> Client stdin: Broadcast Stream

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

## 5. Client stderr -> User: Desktop Notifications

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

## 6. Hardware Keys & Peripheral Boundaries

1. **No Daemon Key Remapping**: The daemon does **not** emulate virtual keyboards, forward menu navigation keys (`Up`/`Down`/`Enter`), or implement virtual key injection.
2. **Peripheral-Local Handling**:
   - If a peripheral (such as a Bluetooth remote or foot pedal) generates standard Linux input events, the operating system and window manager process them natively.
   - If a custom script captures raw hardware events, it sends `start`/`stop` to `vinput-daemon` for speech, and directly emits standard key events via `/dev/uinput` or tools like `ydotool` if virtual keys are required.

---

## 7. Reference Implementations

### Example A: Peripheral Mouse Side-Button Trigger (`ime: true`)
A pure Python 3 standard library daemon that listens on `/dev/input/event*` for middle/side button presses:

```python
#!/usr/bin/env python3
"""Mouse side button push-to-talk trigger."""
import json
import struct
import sys

EVENT_FORMAT = "llHHI"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
BTN_SIDE = 0x113  # Linux input event code


def send_command(cmd: dict) -> None:
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

### Example B: Floating Subtitle Receiver (`ime: false`)
A lightweight receiver that displays live speech on a floating HUD without touching the desktop input method:

```python
#!/usr/bin/env python3
"""Floating subtitles receiver using wayhud and standard I/O."""
import json
import subprocess
import sys


def main() -> None:
    hud = subprocess.Popen(
        ["wayhud", "-n", "captions"],
        stdin=subprocess.PIPE,
        text=True,
    )
    assert hud.stdin is not None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        typ = event.get("type")
        text = event.get("text", "").strip()
        if typ in ("partial", "committed") and text:
            hud.stdin.write(text + "\n")
            hud.stdin.flush()


if __name__ == "__main__":
    main()
```
