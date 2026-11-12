# providers.openai-compatible.streaming

Cloud ASR provider script for OpenAI-compatible realtime transcription APIs.

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

`audio_base64` should contain mono `S16_LE` PCM at `16000 Hz` unless the caller
explicitly sets `sample_rate`.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"..."}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

## Environment Variables

- `VINPUT_ASR_API_KEY` required
  Bearer token sent in the `Authorization` header.
- `VINPUT_ASR_URL` optional
  Full realtime websocket URL. Overrides the default endpoint.
- `VINPUT_ASR_MODEL` optional
  Transcription model id forwarded as `audio.input.transcription.model`.
- `VINPUT_ASR_SESSION_MODEL` optional
  Realtime websocket `model`. Defaults to `VINPUT_ASR_MODEL`. When both are
  `gpt-live-transcribe`, the connection uses `?intent=transcription` without a
  `model` query parameter.
- `VINPUT_ASR_BINARY_MODE` optional
  Set to `true` to stream raw PCM audio via WebSocket binary frames instead of
  Base64-encoded JSON `input_audio_buffer.append` text frames. Supported by
  OpenAI Realtime API. Defaults to `false`.
- `VINPUT_ASR_LANGUAGE` optional
  Single language hint for models other than `gpt-live-transcribe`.
- `VINPUT_ASR_LANGUAGES` optional
  JSON array of expected language codes for `gpt-live-transcribe`, for example
  `["en", "zh", "ja"]`. Omitted by default; not combined with the singular hint.
- `VINPUT_ASR_DELAY` optional
  Live transcription delay: `minimal`, `low`, `medium`, `high`, or `xhigh`.
  Defaults to `medium`. More delay allows more audio context before partial text.
- `VINPUT_ASR_PROMPT` optional
  Prompt or bias text forwarded to the upstream transcription session.
- `VINPUT_ASR_TIMEOUT` optional
  Network timeout in seconds.
- `VINPUT_ASR_FINISH_GRACE_SECS` optional
  Maximum wait for outstanding final transcripts after local `finish`. Defaults
  to `VINPUT_ASR_TIMEOUT`. The script exits as soon as all committed turns finish;
  this is a timeout, not an unconditional delay. A timeout or early disconnect
  reports an error instead of returning an incomplete transcript as successful.
- `VINPUT_ASR_BASE_URL` optional
  Base HTTP(S) URL used to derive the realtime websocket URL when `VINPUT_ASR_URL`
  is not provided.
- `VINPUT_ASR_TARGET_SAMPLE_RATE` optional
  Target sample rate sent to the upstream API after local resampling.
- `VINPUT_ASR_ENABLE_VAD` optional
  Enables upstream server-side VAD. Not supported by `gpt-live-transcribe`; leave
  disabled for that model and let the caller send `finish`.
- `VINPUT_ASR_VAD_THRESHOLD` optional
  VAD sensitivity threshold passed to the provider.
- `VINPUT_ASR_VAD_PREFIX_PADDING_MS` optional
  Milliseconds of audio padding preserved before speech start.
- `VINPUT_ASR_VAD_SILENCE_DURATION_MS` optional
  Milliseconds of silence required before the provider closes a speech turn.

## Notes

- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
- The script uses OpenAI-style realtime transcription events such as `session.update`,
  `input_audio_buffer.append`, `conversation.item.input_audio_transcription.delta`,
  and `conversation.item.input_audio_transcription.completed`.
- For OpenAI live transcription, set `VINPUT_ASR_MODEL=gpt-live-transcribe` and
  leave `VINPUT_ASR_SESSION_MODEL` unset. Optionally set `VINPUT_ASR_DELAY=medium`
  and `VINPUT_ASR_LANGUAGES='["en", "zh", "ja"]'`. The script resamples incoming
  audio to 24 kHz, emits partial text during speech, commits on `finish`, and
  waits for completion before emitting one final transcript.
- Other models retain their model-based WebSocket URL and singular language hint.
- Partial text is retained when a commit acknowledgement arrives after transcript
  deltas. Audio already committed by an `audio` event is not committed again on
  `finish` unless more audio was appended.
