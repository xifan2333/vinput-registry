# providers.doubao.streaming

Cloud ASR provider script for the official Volcengine OpenSpeech bigmodel
streaming ASR API.

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

`audio_base64` must contain mono `S16_LE` PCM at `16000 Hz`.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"..."}`
- `{"type":"final_timestamps","text":"...","utterances":[...],"words":[...]}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

## Environment Variables

- `VINPUT_ASR_APP_ID` required
  App key sent as `X-Api-App-Key`.
- `VINPUT_ASR_ACCESS_TOKEN` required
  Access key sent as `X-Api-Access-Key`.
- `VINPUT_ASR_API_KEY` optional
  Compatibility shortcut in `app_id:access_key` form. Used only when the two
  fields above are not both present.
- `VINPUT_ASR_URL` optional
  Full websocket URL. Defaults to
  `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel`.
- `VINPUT_ASR_RESOURCE_ID` optional
  Official OpenSpeech resource id. Defaults to `volc.bigasr.sauc.duration`.
  Common values:
  `volc.bigasr.sauc.duration`, `volc.bigasr.sauc.concurrent`,
  `volc.seedasr.sauc.duration`, `volc.seedasr.sauc.concurrent`.
  If this does not match the service you activated, websocket upgrade may fail
  with `403 Forbidden`.
- `VINPUT_ASR_MODEL` optional
  Upstream `request.model_name`. Defaults to `bigmodel`.
- `VINPUT_ASR_USER_ID` optional
  Forwarded as `user.uid`. Defaults to `VINPUT_ASR_APP_ID`.
- `VINPUT_ASR_LANGUAGE` optional
  Forwarded as `audio.language`. This is mainly useful when overriding the URL
  to the `bigmodel_nostream` path.
- `VINPUT_ASR_REQUEST_JSON` optional
  JSON object merged into the upstream `request` payload. Use this for advanced
  official parameters such as `corpus`, `enable_poi_fc`, or
  `enable_music_fc`.
- `VINPUT_ASR_ENABLE_NONSTREAM` optional
  Enables the official second-pass nonstream recognition mode.
- `VINPUT_ASR_ENABLE_ITN` optional
  Forwards `request.enable_itn`.
- `VINPUT_ASR_ENABLE_PUNC` optional
  Forwards `request.enable_punc`.
- `VINPUT_ASR_ENABLE_DDC` optional
  Forwards `request.enable_ddc`.
- `VINPUT_ASR_RESULT_TYPE` optional
  Forwards `request.result_type`. Accepts `full`, `single`, `0`, or `1`.
- `VINPUT_ASR_SHOW_UTTERANCES` optional
  Requests utterance timing data from the upstream API.
- `VINPUT_ASR_END_WINDOW_SIZE` optional
  Forwards `request.end_window_size`.
- `VINPUT_ASR_VAD_SEGMENT_DURATION` optional
  Forwards `request.vad_segment_duration`.
- `VINPUT_ASR_FORCE_TO_SPEECH_TIME` optional
  Forwards `request.force_to_speech_time`.
- `VINPUT_ASR_TIMEOUT` optional
  Network timeout in seconds.
- `VINPUT_ASR_FINISH_GRACE_SECS` optional
  Extra wait time after a local commit or finish before closing the websocket.

## Notes

- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
- The upstream connection uses the official OpenSpeech binary websocket protocol,
  not the edge AI Gateway realtime event API.
