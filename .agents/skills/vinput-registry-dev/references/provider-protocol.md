# Cloud ASR Provider Protocol Specification

This document details the communication protocol between `vinput-daemon` and Cloud ASR Provider scripts in `resources/providers/`.

---

## 1. Streaming Provider Protocol (Duplex JSONL)

Streaming providers are long-running child processes spawned by `vinput-daemon` when recording starts. They maintain a bidirectional stream over standard pipes.

### Trigger / Identifier
- Resource ID **must** end with `.streaming` (e.g. `provider.bailian.streaming`).
- In `registry/providers.json`, `"stream": true`.

### Standard Input (`stdin`): Daemon -> Provider
Daemon writes **one JSON object per line (JSONL)** terminated by `\n`:

1. **`audio` chunk event**:
   ```json
   {"type": "audio", "audio_base64": "<base64_encoded_pcm>", "commit": false}
   ```
   - **Audio Specs**: Raw PCM, `16000 Hz`, `1 channel` (mono), `S16_LE` (16-bit signed integer, little-endian).
   - `audio_base64`: Base64 string of the PCM chunk.
   - `commit`: Boolean flag from daemon. Provider scripts may use or ignore this depending on server-side VAD capabilities.
2. **`finish` event**:
   ```json
   {"type": "finish"}
   ```
   - Sent when user releases the recording key.
   - Daemon closes the provider's `stdin` immediately after sending `finish`.
   - Provider script must notify upstream server of completion, await the final transcription within a grace period, emit final events, and exit with code 0.
3. **`cancel` event**:
   ```json
   {"type": "cancel"}
   ```
   - Sent when recording is aborted. Provider must abort upstream connection and exit cleanly.

### Standard Output (`stdout`): Provider -> Daemon
Provider writes **strictly clean JSONL** to `stdout`. Every line must be a valid JSON object followed by `\n`. Every line must be flushed immediately (`sys.stdout.flush()`). **Never print debug text or logs to stdout.**

1. **`session_started`**:
   ```json
   {"type": "session_started", "session_id": "uuid-or-id", "config": {}}
   ```
   - Emitted once upstream WebSocket/session handshake succeeds.
2. **`partial`** (interim text):
   ```json
   {"type": "partial", "text": "cumulative visible text"}
   ```
   - Emitted when unconfirmed text updates. Triggers real-time input method candidate display.
3. **`final`** (confirmed sentence/segment):
   ```json
   {"type": "final", "text": "cumulative confirmed text", "segment_final": true}
   ```
   - Emitted when server VAD or semantic punctuation finalizes a sentence. Daemon commits this text.
4. **`error`**:
   ```json
   {"type": "error", "message": "Human readable error details"}
   ```
   - Emitted on unrecoverable upstream failures.
5. **`closed`**:
   ```json
   {"type": "closed"}
   ```
   - Emitted just before script exits after clean termination.

---

## 2. Batch Provider Protocol (One-Shot)

Batch providers are short-lived child processes spawned by `vinput-daemon` after user stops recording.

### Trigger / Identifier
- Resource ID does **not** end with `.streaming` (e.g. `provider.bailian.batch`).
- In `registry/providers.json`, `"stream": false`.

### Standard Input (`stdin`)
- Daemon pipes the **raw, uncompressed binary PCM bytes** (`S16_LE`, 16000Hz, mono) directly into `stdin`.
- Provider reads `sys.stdin.buffer.read()`.

### Standard Output (`stdout`)
- Provider transcribes audio and writes the **raw final text directly to `stdout`**:
  ```python
  sys.stdout.write(text.strip())
  ```
- No JSON wrapping required for batch output.

---

## 3. Standard Error (`stderr`) & Exit Codes

### `stderr` Role
- **Strictly for diagnostics**: Connection status, error traces, debug logs.
- When a provider exits with non-zero code or fails, `vinput-daemon` captures the last line of `stderr` (`stderr_tail_`) to display in user-facing error notifications.

### Exit Codes
- `0`: Success.
- `1` (`EXIT_RUNTIME_ERROR`): Network failure, API authentication failure, socket drop, server-side error.
- `2` (`EXIT_USAGE_ERROR`): Missing required environment variable, invalid parameter syntax.

---

## 4. Implementation Best Practices

1. **Zero External Dependencies**: Must use only Python 3 standard library (`socket`, `ssl`, `json`, `urllib.request`, `secrets`, `base64`, `struct`, `threading`).
2. **Audio Start Buffering**: If upstream requires a server acknowledgment (e.g. `task-started` in Bailian/DashScope) before receiving binary frames, buffer incoming `audio` chunks in memory (`pending_audio`) and flush them only after the handshake completes.
3. **Fallback Final on Termination**: In `finally` blocks, if the script has uncommitted `partial` text in memory and connection closes before an explicit `final`, emit a fallback final to prevent lost speech.
4. **Finish Grace Period**: After sending finish command to upstream, do not close socket immediately. Wait for upstream's final packet (typically 0.4s – 1.2s timeout).
5. **Text Concatenation (`combine_transcript`)**: When joining sentences, do not insert spaces between Chinese characters, but preserve single spaces between consecutive ASCII/English words.
