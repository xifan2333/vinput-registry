# providers.vinput.remote.streaming

Built-in VInput remote text provider.

This provider does not call an external ASR service. It connects to the
`fcitx5-vinput` daemon's built-in pseudo ASR realtime endpoint, while a phone or
another LAN browser sends text to the daemon's local-network web page.

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

Audio payloads are consumed for protocol compatibility, but the built-in remote
service ignores audio and waits for text from the LAN web client.

## Output Protocol

- `{"type":"session_started","session_id":"..."}`
- `{"type":"partial","text":"..."}`
- `{"type":"final","text":"..."}`
- `{"type":"error","message":"..."}`
- `{"type":"closed"}`

## Environment Variables

- `VINPUT_ASR_API_KEY` required
  Shared secret used as the local realtime Bearer token and as the LAN web page
  password.
- `VINPUT_ASR_PORT` optional
  Port used by the daemon's built-in remote service. Defaults to `8080`.
- `VINPUT_ASR_DEBOUNCE_MS` optional
  Quiet period in milliseconds before typed text is sent as a final transcript.
  Defaults to `1500`. This is read by the daemon service.

## Phone Setup

1. Activate this provider in VInput.
2. Make sure the daemon is running.
3. Open `http://<LAN-IP>:<port>/` on the phone.
4. Enter the same `VINPUT_ASR_API_KEY`, or open
   `http://<LAN-IP>:<port>/#key=<api-key>`.
