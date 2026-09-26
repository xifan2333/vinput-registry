# providers.xfyun.streaming

Cloud ASR provider script for the iFlytek Spark speech dictation large model
V2.0 WebSocket API.

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
- `{"type":"final","text":"...","segment_final":true,"utterance_final":true}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

The provider keeps iFlytek's incremental results as `partial` events and emits
one final result after the local `finish` event. This prevents upstream clause
updates from prematurely committing the vinput utterance.

## Environment Variables

### Required

- `VINPUT_ASR_APP_ID`
  iFlytek application AppID.
- `VINPUT_ASR_API_KEY`
  iFlytek WebAPI APIKey.
- `VINPUT_ASR_API_SECRET`
  iFlytek WebAPI APISecret.

### Optional

- `VINPUT_ASR_URL`
  Full WebSocket URL. Defaults to
  `wss://iat.cn-huabei-1.xf-yun.com/v1`.
- `VINPUT_ASR_RES_ID`
  Application-level hotword resource id configured in the iFlytek console.
The V2.0 endpoint fixes the language to `zh_cn` and the accent to `mandarin`.

- `VINPUT_ASR_ENABLE_WPGS`
  Enable iFlytek dynamic correction (`dwa=wpgs`). Defaults to `true` for this
  Chinese-only endpoint. Set to `false` to disable dynamic correction; then
  results may arrive only after `finish`.
- `VINPUT_ASR_SVAD`
  iFlytek server-side VAD switch. Defaults to `0` (disabled); set to `1` to
  enable it. Only `0` and `1` are accepted. This is independent of vinput's
  local VAD.
- `VINPUT_ASR_TIMEOUT`
  WebSocket timeout in seconds. Defaults to `30`.
- `VINPUT_ASR_FINISH_GRACE_SECS`
  Time to wait for the final server result after sending the final audio frame.
  Defaults to `8.0` seconds and accepts values from `0` through `8` seconds.
  If no confirmed final result arrives before the timeout, the provider emits
  an error instead of promoting the last partial to final.

The script also accepts the advanced iFlytek `parameter.iat` settings through
`VINPUT_ASR_BOS`, `VINPUT_ASR_VGAP`, `VINPUT_ASR_EOS`, `VINPUT_ASR_VINFO`,
`VINPUT_ASR_FA_NBEST`, `VINPUT_ASR_EVL`, `VINPUT_ASR_OPT`, `VINPUT_ASR_LTC`,
`VINPUT_ASR_ETT`, `VINPUT_ASR_RES_LANGUAGE`,
`VINPUT_ASR_HOTWORDS`, and `VINPUT_ASR_CONTEXT`. These are intentionally not
part of the default registry environment list; use them only when needed.

## Notes

- Authentication is generated per connection using the iFlytek HMAC-SHA256
  URL signature (`host`, `date`, and `request-line`).
- Audio is sent as Base64 inside iFlytek JSON text frames. The first frame
  contains the `parameter.iat` configuration; subsequent frames contain audio
  only; the final frame has `status: 2` and an empty audio payload.
- iFlytek result text is Base64-encoded JSON. The provider decodes `ws[].cw[].w`
  and applies `pgs`/`rg` dynamic correction before emitting the cumulative text.
- iFlytek limits a recognition session to 60 seconds. The provider uses the
  vinput `finish` event to close a session and does not rely on server-side VAD.
