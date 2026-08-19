# providers.soniox.streaming

Cloud ASR provider script for Soniox Real-time Speech-to-Text.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: JSONL via stdin
- output: JSONL via stdout
- diagnostics: stderr only
- dependencies: Python standard library only

## Input Protocol

- `{"type":"audio","audio_base64":"...","commit":false}`
- `{"type":"audio","audio_base64":"...","commit":true}`
- `{"type":"finish"}`
- `{"type":"cancel"}`

`audio_base64` should contain mono `S16_LE` PCM at `16000 Hz`.
Other input sample rates are resampled to `VINPUT_ASR_TARGET_SAMPLE_RATE`.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"...","segment_final":true,"utterance_final":false}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

## Environment Variables

- `VINPUT_ASR_API_KEY` required
  Soniox API key, sent in the websocket start request.
- `VINPUT_ASR_URL` optional
  Full Soniox realtime websocket URL. Overrides the default endpoint.
- `VINPUT_ASR_MODEL` optional
  Soniox realtime model id. Defaults to `stt-rt-v5`.
- `VINPUT_ASR_LANGUAGE` optional
  Comma-separated language hints, for example `en,zh`.
- `VINPUT_ASR_LANGUAGE_HINTS` optional
  Alias of `VINPUT_ASR_LANGUAGE`, takes precedence when both are set.
- `VINPUT_ASR_LANGUAGE_HINTS_STRICT` optional
  Restricts recognition to the hinted languages.
- `VINPUT_ASR_PROMPT` optional
  Bias text forwarded as `context.text`.
- `VINPUT_ASR_CONTEXT_TERMS` optional
  Comma-separated custom vocabulary forwarded as `context.terms`.
- `VINPUT_ASR_ENABLE_ENDPOINT_DETECTION` optional
  Enables Soniox semantic endpoint detection.
- `VINPUT_ASR_ENDPOINT_SENSITIVITY` optional
  Endpoint likelihood between `-1.0` and `1.0`.
- `VINPUT_ASR_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL` optional
  Endpoint latency reduction level between `0` and `3`.
- `VINPUT_ASR_MAX_ENDPOINT_DELAY_MS` optional
  Upper bound in milliseconds before an endpoint is emitted.
- `VINPUT_ASR_ENABLE_SPEAKER_DIARIZATION` optional
  Enables speaker labels in the Soniox response.
- `VINPUT_ASR_ENABLE_LANGUAGE_IDENTIFICATION` optional
  Enables per-token language labels in the Soniox response.
- `VINPUT_ASR_TARGET_SAMPLE_RATE` optional
  Sample rate declared to Soniox. Input audio is resampled to it.
- `VINPUT_ASR_KEEPALIVE_SECS` optional
  Idle interval before a `keepalive` control message is sent.
- `VINPUT_ASR_CLIENT_REFERENCE_ID` optional
  Client-defined identifier recorded in Soniox usage logs.
- `VINPUT_ASR_TIMEOUT` optional
  Connection timeout in seconds.
- `VINPUT_ASR_FINISH_GRACE_SECS` optional
  Extra wait time after local `finish` before the script closes the socket.

## Notes

- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
- Audio is streamed as binary websocket frames, and `finish` sends an empty frame
  to close the stream, following the Soniox realtime protocol.
- Soniox returns a token stream instead of text deltas. Final tokens are
  accumulated, non-final tokens are replaced on every response, and the visible
  text is emitted as `partial`.
- `commit` sends a `{"type":"finalize"}` control message, and the resulting
  `<fin>` token is emitted as `final`.
- With `VINPUT_ASR_ENABLE_ENDPOINT_DETECTION`, the `<end>` token is emitted as
  `final` with `utterance_final`. Local VAD commits and server endpoint detection
  both close segments, so enabling both splits speech twice.
- `keepalive` control messages are sent while no audio is streamed, because the
  Soniox connection is closed after 20 seconds of inactivity.
