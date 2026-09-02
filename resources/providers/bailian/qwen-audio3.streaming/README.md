# providers.bailian.qwen-audio3.streaming

Cloud ASR provider script for Bailian `qwen-audio-3.0-asr-flash-streaming`
over the DashScope duplex realtime speech recognition WebSocket protocol
(`run-task` / `finish-task` with binary audio frames).

The same protocol is shared by the Fun-ASR-Realtime and Paraformer realtime
models, so those can be selected with `VINPUT_ASR_MODEL` as well.

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

`audio_base64` should contain mono `S16_LE` PCM at `16000 Hz`. The script sends
raw PCM as binary WebSocket frames and re-encodes nothing.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"...","segment_final":true}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

`partial` events carry the full text visible so far. `final` events are emitted
for each server-side sentence end (`sentence_end: true`) and carry the
cumulative confirmed text.

## Environment Variables

- `VINPUT_ASR_API_KEY` required
  Bailian API key sent as `Authorization: Bearer <key>` during the WebSocket
  handshake. API keys are region-bound; pick the key that matches the endpoint
  below.
- `VINPUT_ASR_MODEL` optional
  Model id. Defaults to `qwen-audio-3.0-asr-flash-streaming`. Any model of the
  `qwen-audio-3.0-asr-flash-streaming`, `fun-asr-realtime`, or `paraformer`
  realtime families can be used here.
- `VINPUT_ASR_URL` optional
  Full WebSocket endpoint override. Needed when the default endpoint does not
  match your region or workspace.
- `VINPUT_ASR_WORKSPACE_ID` optional
  Bailian business space id. When set (and `VINPUT_ASR_URL` is not), the script
  connects to `wss://<workspace_id>.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`.
- `VINPUT_ASR_LANGUAGE` optional
  Language hint forwarded as `parameters.language_hints` (single value).
- `VINPUT_ASR_PROMPT` optional
  Bias text or domain vocabulary forwarded as recognition context
  (`input.context` with an `input_text` user message). Truncated to 400 chars.
- `VINPUT_ASR_ENABLE_PUNCTUATION` optional
  When enabled, sets `parameters.semantic_punctuation_enabled: true`, which
  switches the server from VAD sentence splitting to semantic punctuation.
  Defaults to disabled (low-latency VAD mode).
- `VINPUT_ASR_VAD_SILENCE_DURATION_MS` optional
  VAD sentence-splitting silence threshold in milliseconds
  (`parameters.max_sentence_silence`). Server default is `1300`; accepted range
  is `[200, 6000]`.
- `VINPUT_ASR_TIMEOUT` optional
  Network timeout in seconds.
- `VINPUT_ASR_FINISH_GRACE_SECS` optional
  Extra wait time after `finish-task` before the script closes the socket.

## Endpoint Notes

Default endpoint is `wss://dashscope.aliyuncs.com/api-ws/v1/inference`, which
still serves the model (legacy domain). Alibaba recommends the dedicated
business-space domains:

- Beijing: `wss://<workspace_id>.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`
- Singapore: `wss://<workspace_id>.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/inference`

Singapore users without a workspace should override `VINPUT_ASR_URL` with
`wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference` and use a Singapore
region API key.

## Notes

- The `commit` flag from the input protocol is ignored: sentence boundaries are
  decided server-side (VAD or semantic punctuation).
- The script buffers audio until the `task-started` event arrives, as required
  by the upstream API.
- Only standard-library modules are used; the WebSocket client, frame masking,
  and handshake are implemented in-process.
