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
- `VINPUT_ASR_URL` optional
- `VINPUT_ASR_MODEL` optional
- `VINPUT_ASR_LANGUAGE` optional
- `VINPUT_ASR_PROMPT` optional
- `VINPUT_ASR_TIMEOUT` optional
- `VINPUT_ASR_FINISH_GRACE_SECS` optional
- `VINPUT_ASR_BASE_URL` optional
- `VINPUT_ASR_TARGET_SAMPLE_RATE` optional
- `VINPUT_ASR_ENABLE_VAD` optional
- `VINPUT_ASR_VAD_THRESHOLD` optional
- `VINPUT_ASR_VAD_PREFIX_PADDING_MS` optional
- `VINPUT_ASR_VAD_SILENCE_DURATION_MS` optional

## Notes

- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
- The script uses OpenAI-style realtime transcription events such as `session.update`,
  `input_audio_buffer.append`, `conversation.item.input_audio_transcription.delta`,
  and `conversation.item.input_audio_transcription.completed`.
