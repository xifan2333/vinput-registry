# trigger.vinput.web_remote

A local network mobile web input trigger. It hosts a responsive web page for phones on the same Wi-Fi / local network to type or use mobile voice input directly into desktop applications via text injection.

## Runtime

- command: `python3`
- wire protocol: Three-stream client pipe model (`inject` commands on stdout, `committed` broadcasts on stdin, desktop notifications on stderr).

Listens on all local interfaces (`0.0.0.0`) at the configured `VINPUT_TRIGGER_PORT`. Offers `GET /` (web app), `GET /favicon.svg`, `GET /health`, and `GET /ws` (WebSocket connection for mobile clients).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VINPUT_TRIGGER_API_KEY` | Required | Shared secret token for authentication. |
| `VINPUT_TRIGGER_PORT` | Required | Local listening port, an integer from 1 to 65535. |
| `VINPUT_TRIGGER_DEBOUNCE_MS` | `1500` | Quiet period in milliseconds before typed text is finalized. |

## Usage

1. Enable and start this trigger via `vinput trigger start trigger.vinput.web_remote` or set `"auto_start": true`.
2. Open `http://<desktop-ip>:<port>/` in the browser on your phone.
3. Enter the API key configured in `VINPUT_TRIGGER_API_KEY`, or open `http://<desktop-ip>:<port>/#key=<api-key>`.
4. Tap the text area on your phone and start speaking or typing. Text is injected in real-time into the active desktop application.
