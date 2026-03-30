# providers.doubaoime.streaming

Cloud ASR provider script for the unofficial `doubaoime-asr` protocol path used by Doubao IME.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: JSONL via stdin
- output: JSONL via stdout
- diagnostics: stderr only
- dependencies: system `libopus`

## Input Protocol

- `{"type":"audio","audio_base64":"...","commit":false}`
- `{"type":"audio","audio_base64":"...","commit":true}`
- `{"type":"finish"}`
- `{"type":"cancel"}`

`audio_base64` should contain mono `S16_LE` PCM at `16000 Hz`.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"..."}`
- `{"type":"final_timestamps","text":"...","words":[...]}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

## Environment Variables

- `VINPUT_ASR_TIMEOUT` optional
  Network timeout in seconds.
- `VINPUT_ASR_FINISH_GRACE_SECS` optional
  Extra wait time after local finish before closing the websocket.
- `VINPUT_ASR_URL` optional
  Full websocket URL. Defaults to the Doubao IME endpoint.
- `VINPUT_ASR_AID` optional
  App id query parameter. Defaults to the current upstream client value.
- `VINPUT_ASR_CREDENTIAL_PATH` optional
  Credential cache path. Defaults to `~/.cache/vinput/doubaoime-asr/credentials.json`.
- `VINPUT_ASR_DEVICE_ID` optional
  Overrides the cached or auto-registered device id.
- `VINPUT_ASR_TOKEN` optional
  Overrides the cached or auto-fetched ASR token.
- `VINPUT_ASR_ENABLE_PUNCTUATION` optional
  Enables punctuation in recognition output.
- `VINPUT_ASR_ENABLE_SPEECH_REJECTION` optional
  Enables speech rejection mode.
- `VINPUT_ASR_ENABLE_ASR_TWOPASS` optional
  Enables upstream two-pass decoding.
- `VINPUT_ASR_ENABLE_ASR_THREEPASS` optional
  Enables upstream three-pass decoding.
- `VINPUT_ASR_APP_NAME` optional
  App name forwarded in session config.

## Notes

- This path is based on a reverse-engineered, unofficial protocol and may break if upstream changes.
- The script auto-registers a virtual device and caches credentials locally when `VINPUT_ASR_DEVICE_ID` and `VINPUT_ASR_TOKEN` are not provided.
- This resource is intended to be materialized into local config and executed locally.
