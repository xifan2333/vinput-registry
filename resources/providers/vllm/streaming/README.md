# providers.vllm.streaming

Local/self-hosted realtime ASR provider over WebSocket, generic and model-agnostic.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: JSONL via stdin
- output: JSONL via stdout
- diagnostics: stderr only
- dependencies: Python standard library only (socket, struct, ssl, threading)

## Input Protocol

- `{"type":"audio","audio_base64":"...","commit":false}`
- `{"type":"audio","audio_base64":"...","commit":true}`
- `{"type":"finish"}`
- `{"type":"cancel"}`

`audio_base64` should contain mono `S16_LE` PCM at `16000 Hz`.

## Output Protocol

- `{"type":"session_started"}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"..."}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

## Environment Variables

- `VINPUT_ASR_URL` optional
  WebSocket endpoint, e.g. `ws://127.0.0.1:7000/v1/realtime`.
- `VINPUT_ASR_MODEL` optional
  Served model name forwarded as `session.update.model`.

Advanced tuning (chunk size, timeouts, debug logging) can still be supplied via
environment variables but is intentionally not advertised in the registry to
avoid polluting user configuration.

## Notes

- This provider is intentionally generic: it streams PCM to any service that exposes a realtime ASR WebSocket API (OpenAI Realtime style `session.update` / `input_audio_buffer.append` / `input_audio_buffer.commit` events).
- It has been tested against a locally deployed vLLM `Qwen3ASRRealtimeGeneration` endpoint (`/v1/realtime`).
- The script does not modify or patch the ASR server; it only speaks the standard WebSocket protocol.
- Prefix cleaning strips every `language {lang}<asr_text>` marker emitted by Qwen3-ASR and joins segment text.
