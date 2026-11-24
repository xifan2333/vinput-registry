# trigger.vinput.web_remote

A local network mobile web input trigger. It hosts a responsive web page for phones on the same Wi-Fi / local network to type or use mobile voice input directly into desktop applications via text injection, along with a mobile virtual key bar for command palette and editing control over `/dev/uinput`.

## Runtime

- command: `python3`
- wire protocol: Three-stream client pipe model (`inject` commands on stdout, `committed` broadcasts on stdin, desktop notifications on stderr).
- virtual keys (optional / lazy): Uses `libevdev.so.2` via `ctypes` and Linux `/dev/uinput` when a virtual key button is tapped. If `/dev/uinput` lacks write permissions or `libevdev` is unavailable, text/voice injection continues to work and a desktop notification reports the virtual-key error.

Listens on all local interfaces (`0.0.0.0`) at the configured `VINPUT_TRIGGER_PORT`. Offers `GET /` (web app), `GET /favicon.svg`, `GET /health`, and `GET /ws` (WebSocket connection for mobile clients).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VINPUT_TRIGGER_API_KEY` | Required | Shared secret token for authentication. |
| `VINPUT_TRIGGER_PORT` | Required | Local listening port, an integer from 1 to 65535. |
| `VINPUT_TRIGGER_DEBOUNCE_MS` | `1500` | Quiet period in milliseconds before typed text is finalized. |
| `VINPUT_TRIGGER_MENU_KEY` | `KEY_RIGHTSHIFT` | Linux key binding for the `Menu` virtual button (matches Fcitx5 `MenuKey`, default Right Shift). |
| `VINPUT_TRIGGER_ESC_KEY` | `KEY_ESC` | Linux key binding for the `Esc` virtual button. |
| `VINPUT_TRIGGER_UP_KEY` | `KEY_UP` | Linux key binding for the `↑` virtual button. |
| `VINPUT_TRIGGER_DOWN_KEY` | `KEY_DOWN` | Linux key binding for the `↓` virtual button. |
| `VINPUT_TRIGGER_LEFT_KEY` | `KEY_LEFT` | Linux key binding for the `←` virtual button. |
| `VINPUT_TRIGGER_RIGHT_KEY` | `KEY_RIGHT` | Linux key binding for the `→` virtual button. |
| `VINPUT_TRIGGER_BACKSPACE_KEY` | `KEY_BACKSPACE` | Linux key binding for the `⌫` virtual button. |
| `VINPUT_TRIGGER_ENTER_KEY` | `KEY_ENTER` | Linux key binding for the `⏎` virtual button. |
| `VINPUT_TRIGGER_LIBEVDEV_PATH` | System lookup | Custom path to `libevdev.so.2` (useful on NixOS or custom setups). |

### Key Binding Format

Each `VINPUT_TRIGGER_*_KEY` variable accepts:
- A standard Linux `EV_KEY` name resolved via `libevdev` (e.g. `KEY_RIGHTSHIFT`, `KEY_LEFTCTRL`, `KEY_ESC`), preserving left/right modifier distinction.
- A `+`-separated chord (e.g. `KEY_LEFTCTRL+KEY_C`, `KEY_LEFTSHIFT+KEY_TAB`).
- A numeric evdev keycode in decimal or hex (e.g. `54`, `0x36`), which works even without `libevdev`.
- `none` to hide and disable that virtual button.

## Usage

1. Enable and start this trigger via `vinput trigger start trigger.vinput.web_remote` or set `"auto_start": true`.
2. Ensure your user has write permission on `/dev/uinput` (typically via `input` / `uinput` group and a udev rule) if you plan to use the virtual key bar.
3. Open `http://<desktop-ip>:<port>/` in the browser on your phone.
4. Enter the API key configured in `VINPUT_TRIGGER_API_KEY`, or open `http://<desktop-ip>:<port>/#key=<api-key>`.
5. Tap the text area on your phone and start speaking or typing. Text is injected in real-time into the active desktop application, and virtual keys finalize any pending text before emitting key events.
