# trigger.vinput.mouse_button

Mouse button voice trigger for `fcitx5-vinput` using `libudev` device discovery and Linux `evdev` events.

## Entry
- `entry.py`

## Runtime
- command: `python3`
- architecture: Long-lived Trigger client (`stdin`/`stdout` JSONL pipe protocol)
- dependencies: Python 3 standard library only (uses system `libudev.so.1` via `ctypes` when available, with `inotify` fallback; optional `libevdev.so.2` for resolving arbitrary non-mouse `KEY_*` names)

## Environment Variables

### Optional
- `VINPUT_TRIGGER_BUTTON` (optional, default: `BTN_SIDE`): Linux `EV_KEY` button name(s) or numeric keycode(s), comma-separated (for example: `BTN_SIDE`, `BTN_EXTRA`, `BTN_MIDDLE`, `BTN_SIDE,BTN_EXTRA`, or `0x113`).
- `VINPUT_TRIGGER_MODE` (optional, default: `both`): Trigger mode:
  - `both`: Short tap (`< 200 ms`) locks recording until next press; hold (`>= 200 ms`) stops recording on release.
  - `hold`: Push-to-talk (start on press, stop on release).
  - `tap`: Toggle recording on each press.
- `VINPUT_TRIGGER_DEVICE_PATH` (optional, default: `auto`): Use `auto` for automatic `libudev` (`ID_INPUT_MOUSE=1`) discovery and hotplug, or specify an explicit `/dev/input/event*` or `/dev/input/by-id/*` path.
- `VINPUT_TRIGGER_CAPTURE_DEVICE` (optional, default: `""`): Optional PipeWire audio source node passed as `capture_device` on `start`.
- `VINPUT_TRIGGER_LIBUDEV_PATH` (optional): Custom path to `libudev.so.1` (useful on NixOS or custom setups).
- `VINPUT_TRIGGER_LIBEVDEV_PATH` (optional): Custom path to `libevdev.so.2` when resolving non-mouse `KEY_*` button names (useful on NixOS or custom setups).
