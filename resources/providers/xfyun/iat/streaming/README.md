# provider.xfyun.iat.streaming

Cloud ASR provider script for the iFlytek **语音听写（流式版）** WebSocket
API (`wss://iat-api.xfyun.cn/v2/iat`). This is a separate service from
`provider.xfyun.streaming` (Spark dictation big model V2.0): credentials are
not interchangeable and the wire protocol is different.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- input: JSONL via stdin
- output: JSONL via stdout
- diagnostics: stderr only (JSONL notifications on errors)
- dependencies: Python standard library only

## Input Protocol

- `{"type":"audio","audio_base64":"..."}`
- `{"type":"finish"}`
- `{"type":"cancel"}`

`audio_base64` must contain mono `S16_LE` PCM at `16000 Hz`.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"..."}`
- `{"type":"closed"}`

Incremental upstream results are emitted as `partial`; one `final` is emitted
after the local `finish` event. This avoids committing text on intermediate
updates.

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
  Full WebSocket URL. Defaults to `wss://iat-api.xfyun.cn/v2/iat`.
- `VINPUT_ASR_LANGUAGE`
  Recognition language: `zh_cn` (default) or `en_us`.
- `VINPUT_ASR_DOMAIN`
  Recognition domain: `iat` (default). `xfime-mianqie` (dialect-free mode)
  requires separate console authorization.
- `VINPUT_ASR_ACCENT`
  Accent, defaults to `mandarin`. Other values require console authorization.
- `VINPUT_ASR_PTT`
  Punctuation switch: `1` (default) or `0`.
- `VINPUT_ASR_EOS`
  Back-end silence detection in milliseconds. Defaults to `2000`.
- `VINPUT_ASR_ENABLE_WPGS`
  Enable dynamic correction (`dwa=wpgs`). Defaults to `true`; only effective
  for `zh_cn` + `mandarin`. Set to `false` for English or dialect sessions.
- `VINPUT_ASR_TIMEOUT`
  WebSocket timeout in seconds. Defaults to `30`.
- `VINPUT_ASR_FINISH_GRACE_SECS`
  Time to wait for the final upstream result after the final frame, 0–8
  seconds. Defaults to `8`.

## Notes

- Authentication uses the iFlytek HMAC-SHA256 URL signature over `host`,
  `date` and `request-line`. The same AppID/APIKey/APISecret types are used as
  the Spark V2.0 service, but the credentials must belong to this service.
- Audio frames use `common` + `business` on the first frame and `data` on
  every frame. `data.format` is `audio/L16;rate=16000` and `encoding` is
  `raw`; the final frame has `status: 2` and empty audio.
- Results are plain JSON at `data.result`. The provider decodes `ws[].cw[].w`
  and applies `pgs`/`rg` replacement before emitting cumulative text. The
  session ends on `data.status == 2` or `result.ls == true`.
- A session is limited to 60 seconds and the service disconnects after about
  10 seconds without audio. Common errors are surfaced as localized desktop
  notifications: `10163` (timeout), `11200` (unauthorized language/domain),
  `10165` (audio format), `401`/`403` (auth or clock skew).
