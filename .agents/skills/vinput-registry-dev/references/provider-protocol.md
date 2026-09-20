# Cloud ASR Provider Protocol Specification

This document details the communication protocol between `vinput-daemon` and Cloud ASR Provider scripts in `resources/providers/`.

---

## 1. Streaming Provider Protocol (Duplex JSONL)

Streaming providers are long-running child processes spawned by `vinput-daemon` when recording starts. They maintain a bidirectional stream over standard pipes.

### Trigger / Identifier
- Resource ID **must** end with `.streaming` (e.g. `provider.bailian.streaming`, `provider.openai-compatible.streaming`).
- In `registry/providers.json`, `"stream": true`.

### Standard Input (`stdin`): Daemon -> Provider
Daemon writes **one JSON object per line (JSONL)** terminated by `\n`:

1. **`audio` chunk event**:
   ```json
   {"type": "audio", "audio_base64": "<base64_encoded_pcm>", "commit": false}
   ```
   - **Audio Specs**: Raw PCM, `16000 Hz`, `1 channel` (mono), `S16_LE` (16-bit signed integer, little-endian).
   - `audio_base64`: Base64 string of the raw PCM chunk (~32 KB/s audio throughput).
   - `commit`: Boolean flag from daemon. Provider scripts may use or ignore this depending on server-side VAD capabilities.
2. **`finish` event**:
   ```json
   {"type": "finish"}
   ```
   - Sent when user releases the recording key.
   - Daemon closes the provider's `stdin` immediately after sending `finish`.
   - Provider script must notify upstream server of completion (e.g. commit audio buffer or send finish packet), await final transcription within a grace period, emit final events, and exit with code 0.
3. **`cancel` event**:
   ```json
   {"type": "cancel"}
   ```
   - Sent when recording is aborted. Provider must immediately abort the upstream connection and exit cleanly.

### Standard Output (`stdout`): Provider -> Daemon
Provider writes **strictly clean JSONL** to `stdout`. Every line must be a valid JSON object followed by `\n`. Every line must be flushed immediately (`sys.stdout.flush()`). **Never print debug text or operational logs to stdout.**

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
   - **Critical Rule**: Mid-stream segment completions from upstream must be surfaced as cumulative `partial` text rather than premature `final` events to prevent early cutoff.
3. **`final`** (confirmed sentence/segment):
   ```json
   {"type": "final", "text": "cumulative confirmed text", "segment_final": true, "utterance_final": true}
   ```
   - Emitted when user finishes speaking and the entire utterance is committed.
   - **Single Final per Utterance**: `vinput-daemon` commits and ends the current utterance upon receiving the first non-empty `final`. Providers must emit exactly **one** single final at the conclusion of recording (after `finish`), rather than multiple fragmented finals during recording.
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

## 2. Upstream Audio Streaming Modes: Raw PCM vs Base64

When bridging audio from `vinput-daemon` to upstream WebSocket servers (e.g. OpenAI Realtime, vLLM, or proprietary cloud engines), providers typically support two transmission modes:

| Mode | Mechanism | Bandwidth Overhead | Use Case & Configuration |
| :--- | :--- | :--- | :--- |
| **JSON Text Frames (Base64)** | Audio is base64-encoded and sent inside JSON text frames (WebSocket Opcode `0x1`).<br>`{"type": "input_audio_buffer.append", "audio": "<base64>"}` | +33% base64 expansion | Default compatibility mode for standard JSON-over-WebSocket endpoints. |
| **Raw PCM Binary Frames** | Raw PCM bytes are sent directly as WebSocket binary frames (Opcode `0x2`). | **0% overhead** (raw binary) | Supported by OpenAI Realtime and compatible engines. Opt-in via `VINPUT_ASR_BINARY_MODE=true`. Avoids base64 encoding/decoding CPU cycles on both ends. |

---

## 3. Batch Provider Protocol (One-Shot)

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

## 4. Standard Error (`stderr`) & Exit Codes

### `stderr` Role
- **Strictly for diagnostics**: Connection status, error traces, debug logs.
- When a provider exits with non-zero code or fails, `vinput-daemon` captures the last line of `stderr` (`stderr_tail_`) to display in user-facing error notifications.

### Exit Codes
- `0`: Success.
- `1` (`EXIT_RUNTIME_ERROR`): Network failure, API authentication failure, socket drop, server-side error.
- `2` (`EXIT_USAGE_ERROR`): Missing required environment variable, invalid parameter syntax.

---

## 5. Implementation Best Practices

### 1. Zero External Dependencies & No Precompiled Binaries
- All providers must be single-file Python 3 scripts (`entry.py`) relying **solely on the Python 3 standard library** (`socket`, `ssl`, `json`, `urllib.request`, `secrets`, `base64`, `struct`, `threading`).
- **Never include precompiled binary executables**: Binaries cannot be easily audited for security, break across CPU architectures (`x86_64`, `aarch64`, RISC-V), and cause `glibc`/`musl` incompatibility across distributions.
- **Lightweight RFC 6455 WebSocket Implementation**: The Python standard library can implement a full RFC 6455 client in under 80 lines using `socket`, `struct` (for frame headers and lengths), and `secrets` (for mask keys). Refer to `resources/providers/openai-compatible/streaming/entry.py` for a battle-tested template.

### 2. Single Final per Utterance (Preventing Premature Cutoff)
- `vinput-daemon` concludes candidate collection when a non-empty `final` is emitted.
- If an upstream engine reports mid-utterance completions (e.g. per-sentence VAD or incremental `transcription.completed`), accumulate the confirmed text in memory and emit it as an updated `partial`.
- Emit the sole `final` event only during teardown after user `finish`.

### 3. Audio Start Buffering
- If upstream requires an initial handshake acknowledgment (e.g. `task-started` in Bailian or `session.created` in OpenAI Realtime) before accepting audio, buffer incoming `audio` chunks in memory (`pending_audio`) and flush them immediately once the handshake succeeds.

### 4. Fallback Final on Termination
- In `finally` cleanup blocks, if the connection terminates or errors out while uncommitted text exists in memory (`confirmed_text` or non-empty `partial`), emit a fallback `final` to ensure spoken text is never lost.

### 5. Finish Grace Period
- After sending finish notification to upstream, do not close the socket immediately. Wait for upstream's final packet (typically bounded by a grace timeout of 0.4s – 1.2s, configurable via `VINPUT_ASR_FINISH_GRACE_SECS`).

### 6. Text Concatenation (`combine_transcript`)
- When joining sentence fragments, do not insert spaces between Chinese characters, but preserve single spaces between consecutive ASCII/English words:
  ```python
  def combine_transcript(committed_text: str, current_text: str) -> str:
      committed = " ".join(committed_text.split()).strip()
      current = " ".join(current_text.split()).strip()
      if not committed:
          return current
      if not current:
          return committed
      if current == committed or current.startswith(committed):
          return current
      if committed.endswith(current):
          return committed
      return committed + " " + current
  ```

### 7. Self-Hosted & Dedicated GPU Endpoints (vLLM / Realtime Guidelines)
- When integrating self-hosted ASR servers (such as vLLM or OpenAI-compatible realtime endpoints on local GPU workstations or LAN servers):
  - Target the endpoint explicitly (e.g. `provider.vllm.streaming`).
  - Strip model-specific artifact tokens during streaming (for example, Qwen3-ASR under vLLM outputs tokens like `language {lang}<asr_text>`; providers must clean these tokens before emitting `partial`/`final`).
  - Opt into raw PCM binary streaming (`VINPUT_ASR_BINARY_MODE=true`) if the server supports WebSocket Opcode `0x2`.
