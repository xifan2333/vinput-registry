"""Unit tests for trigger.vinput.mouse_button."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / "resources/triggers/vinput/mouse_button/entry.py"
spec = importlib.util.spec_from_file_location("mouse_button_entry", ENTRY)
assert spec and spec.loader
mouse_button = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mouse_button
spec.loader.exec_module(mouse_button)


class _FakeWatcher:
    """In-memory DeviceWatcher using an os.pipe() for hotplug notifications."""

    def __init__(self, initial: list[str] | None = None, check_pointer_props: bool = False) -> None:
        self._initial = list(initial or [])
        self._check_pointer_props = check_pointer_props
        self._r_fd, self._w_fd = os.pipe()
        os.set_blocking(self._r_fd, False)
        self._queue: list[tuple[list[str], list[str]]] = []

    @property
    def fd(self) -> int:
        return self._r_fd

    @property
    def check_pointer_props(self) -> bool:
        return self._check_pointer_props

    def scan_candidates(self) -> list[str]:
        return list(self._initial)

    def push_change(self, added: list[str] | None = None, removed: list[str] | None = None) -> None:
        self._queue.append((list(added or []), list(removed or [])))
        os.write(self._w_fd, b"x")

    def read_changes(self) -> tuple[list[str], list[str]]:
        try:
            os.read(self._r_fd, 64)
        except BlockingIOError:
            return [], []
        added: list[str] = []
        removed: list[str] = []
        while self._queue:
            a, r = self._queue.pop(0)
            added.extend(a)
            removed.extend(r)
        return added, removed

    def close(self) -> None:
        for fd in (self._r_fd, self._w_fd):
            if fd >= 0:
                os.close(fd)
        self._r_fd = -1
        self._w_fd = -1


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = mouse_button.load_config()
        self.assertEqual(cfg.button_codes, frozenset({0x113}))
        self.assertEqual(cfg.mode, "both")
        self.assertEqual(cfg.device_path, "auto")
        self.assertEqual(cfg.capture_device, "")

    def test_custom_buttons_and_mode(self) -> None:
        env = {
            "VINPUT_TRIGGER_BUTTON": "btn_side, 0x114, 274",
            "VINPUT_TRIGGER_MODE": "HOLD",
            "VINPUT_TRIGGER_DEVICE_PATH": "/dev/input/event6",
            "VINPUT_TRIGGER_CAPTURE_DEVICE": "alsa_input.usb",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = mouse_button.load_config()
        self.assertEqual(cfg.button_codes, frozenset({0x113, 0x114, 274}))
        self.assertEqual(cfg.mode, "hold")
        self.assertEqual(cfg.device_path, "/dev/input/event6")
        self.assertEqual(cfg.capture_device, "alsa_input.usb")

    def test_invalid_mode_raises(self) -> None:
        with patch.dict(os.environ, {"VINPUT_TRIGGER_MODE": "double"}, clear=True):
            with self.assertRaises(mouse_button.ConfigError) as ctx:
                mouse_button.load_config()
        self.assertEqual(ctx.exception.exit_code, mouse_button.EXIT_USAGE_ERROR)

    def test_unknown_button_raises(self) -> None:
        with patch.dict(os.environ, {"VINPUT_TRIGGER_BUTTON": "BTN_NONEXISTENT_XYZ"}, clear=True):
            with patch.object(mouse_button, "_load_libevdev", return_value=None):
                with self.assertRaises(mouse_button.ConfigError):
                    mouse_button.load_config()

    def test_out_of_range_numeric_button_raises(self) -> None:
        with patch.dict(os.environ, {"VINPUT_TRIGGER_BUTTON": "9999"}, clear=True):
            with self.assertRaises(mouse_button.ConfigError):
                mouse_button.load_config()


class StateMachineTests(unittest.TestCase):
    def test_hold_mode(self) -> None:
        sm = mouse_button.TriggerStateMachine(mode="hold", capture_device="usb-mic")
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 1, 1.0),
            [{"type": "start", "ime": True, "mode": "normal", "capture_device": "usb-mic"}],
        )
        self.assertEqual(sm.on_button_event("/dev/input/event6", 0x113, 2, 1.1), [])
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 0, 1.5),
            [{"type": "stop", "ime": True}],
        )

    def test_tap_mode(self) -> None:
        sm = mouse_button.TriggerStateMachine(mode="tap")
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 1, 1.0),
            [{"type": "start", "ime": True, "mode": "normal"}],
        )
        self.assertEqual(sm.on_button_event("/dev/input/event6", 0x113, 0, 1.05), [])
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 1, 2.0),
            [{"type": "stop", "ime": True}],
        )
        self.assertEqual(sm.on_button_event("/dev/input/event6", 0x113, 0, 2.05), [])

    def test_both_mode_short_tap_locks_then_second_tap_stops(self) -> None:
        sm = mouse_button.TriggerStateMachine(mode="both", tap_threshold_sec=0.2)
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 1, 10.0),
            [{"type": "start", "ime": True, "mode": "normal"}],
        )
        # Release after 50ms -> tap-locked, no stop emitted
        self.assertEqual(sm.on_button_event("/dev/input/event6", 0x113, 0, 10.05), [])
        self.assertTrue(sm.tap_locked)

        # Second press stops immediately and suppresses release
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 1, 11.0),
            [{"type": "stop", "ime": True}],
        )
        self.assertEqual(sm.on_button_event("/dev/input/event6", 0x113, 0, 11.05), [])
        self.assertFalse(sm.tap_locked)

    def test_both_mode_long_hold_stops_on_release(self) -> None:
        sm = mouse_button.TriggerStateMachine(mode="both", tap_threshold_sec=0.2)
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 1, 10.0),
            [{"type": "start", "ime": True, "mode": "normal"}],
        )
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 0, 10.35),
            [{"type": "stop", "ime": True}],
        )
        self.assertFalse(sm.tap_locked)

    def test_external_idle_status_resets_tap_lock(self) -> None:
        sm = mouse_button.TriggerStateMachine(mode="both")
        sm.on_button_event("/dev/input/event6", 0x113, 1, 1.0)
        sm.on_button_event("/dev/input/event6", 0x113, 0, 1.05)
        self.assertTrue(sm.tap_locked)

        sm.on_status("idle")
        self.assertFalse(sm.tap_locked)
        self.assertEqual(
            sm.on_button_event("/dev/input/event6", 0x113, 1, 2.0),
            [{"type": "start", "ime": True, "mode": "normal"}],
        )

    def test_disconnect_stops_active_hold_but_not_tap_lock(self) -> None:
        sm = mouse_button.TriggerStateMachine(mode="hold")
        sm.on_button_event("/dev/input/event6", 0x113, 1, 1.0)
        self.assertEqual(sm.on_device_removed("/dev/input/event6"), [{"type": "stop", "ime": True}])

        sm_both = mouse_button.TriggerStateMachine(mode="both")
        sm_both.on_button_event("/dev/input/event6", 0x113, 1, 1.0)
        sm_both.on_button_event("/dev/input/event6", 0x113, 0, 1.05)
        self.assertEqual(sm_both.on_device_removed("/dev/input/event6"), [])


class EvdevProbeAndWatcherTests(unittest.TestCase):
    def test_probe_evdev_fd_filters_virtual_and_touchpad(self) -> None:
        def make_fake_ioctl(bustype: int, prop_bits: int, key_bits: list[int]):
            def fake_ioctl(_fd: int, req: int, buf: bytearray, _mutate: bool = False) -> int:
                if req == mouse_button.EVIOCGID:
                    buf[:8] = (bustype).to_bytes(2, "little") + b"\x00" * 6
                elif req == mouse_button._eviocgprop(len(buf)):
                    buf[:4] = prop_bits.to_bytes(4, "little")
                elif req == mouse_button._eviocgbit(mouse_button.EV_KEY, len(buf)):
                    for code in key_bits:
                        buf[code // 8] |= 1 << (code % 8)
                return 0

            return fake_ioctl

        with patch.object(mouse_button.fcntl, "ioctl", side_effect=make_fake_ioctl(0x06, 0, [0x113])):
            self.assertFalse(mouse_button.probe_evdev_fd(0, frozenset({0x113}), check_pointer_props=False))

        with patch.object(
            mouse_button.fcntl,
            "ioctl",
            side_effect=make_fake_ioctl(0x03, 1 << mouse_button.INPUT_PROP_BUTTONPAD, [0x110]),
        ):
            self.assertFalse(mouse_button.probe_evdev_fd(0, frozenset({0x110}), check_pointer_props=True))
            self.assertTrue(mouse_button.probe_evdev_fd(0, frozenset({0x110}), check_pointer_props=False))

        with patch.object(mouse_button.fcntl, "ioctl", side_effect=make_fake_ioctl(0x03, 0, [0x113])):
            self.assertTrue(mouse_button.probe_evdev_fd(0, frozenset({0x113}), check_pointer_props=True))
            self.assertFalse(mouse_button.probe_evdev_fd(0, frozenset({0x114}), check_pointer_props=True))

    def test_create_watcher_falls_back_to_inotify(self) -> None:
        real_inotify_cls = mouse_button.InotifyDeviceWatcher
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(mouse_button, "UdevDeviceWatcher", side_effect=OSError("no libudev")),
                patch.object(
                    mouse_button,
                    "InotifyDeviceWatcher",
                    side_effect=lambda dp: real_inotify_cls(dp, input_dir=tmpdir),
                ),
            ):
                watcher = mouse_button.create_watcher("auto")
                try:
                    self.assertIsInstance(watcher, real_inotify_cls)
                    self.assertTrue(watcher.check_pointer_props)
                finally:
                    watcher.close()


class RunnerLoopTests(unittest.TestCase):
    def test_startup_permission_error_notifies_and_exits_usage_error(self) -> None:
        watcher = _FakeWatcher(initial=["/dev/input/event6"])
        r_stdin, w_stdin = os.pipe()
        os.close(w_stdin)

        stderr_buf = io.StringIO()
        try:
            with (
                patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True),
                patch.object(mouse_button, "create_watcher", return_value=watcher),
                patch.object(mouse_button, "open_matching_evdev", side_effect=PermissionError("denied")),
                patch.object(sys.stdin, "fileno", return_value=r_stdin),
                patch.object(sys, "stderr", stderr_buf),
            ):
                code = mouse_button.main()
        finally:
            os.close(r_stdin)

        self.assertEqual(code, mouse_button.EXIT_USAGE_ERROR)
        notif = json.loads(stderr_buf.getvalue().strip())
        self.assertIn("/dev/input/event6", notif["body"])

    def test_hotplug_and_button_events_end_to_end(self) -> None:
        import threading
        import time

        cfg = mouse_button.ClientConfig(
            button_codes=frozenset({0x113}),
            mode="hold",
            device_path="auto",
            capture_device="",
        )
        sm = mouse_button.TriggerStateMachine(mode="hold")
        watcher = _FakeWatcher(initial=[])
        r_stdin, w_stdin = os.pipe()
        r_ev, w_ev = os.pipe()
        os.set_blocking(r_ev, False)

        stdout_buf = io.StringIO()

        def fake_open(path: str, _codes: frozenset[int], check_pointer_props: bool) -> int | None:
            self.assertEqual(path, "/dev/input/event6")
            self.assertFalse(check_pointer_props)
            return r_ev

        runner = mouse_button.MouseTriggerRunner(cfg=cfg, watcher=watcher, state=sm)

        def feed_events() -> None:
            time.sleep(0.02)
            os.write(w_stdin, b'{"type":"status","status":"idle"}\n')
            watcher.push_change(added=["/dev/input/event6"])
            time.sleep(0.02)
            os.write(w_ev, mouse_button.INPUT_EVENT_STRUCT.pack(0, 0, mouse_button.EV_KEY, 0x113, 1))
            os.write(w_ev, mouse_button.INPUT_EVENT_STRUCT.pack(0, 0, mouse_button.EV_KEY, 0x113, 0))
            time.sleep(0.02)
            os.close(w_ev)
            os.close(w_stdin)

        feeder = threading.Thread(target=feed_events, daemon=True)
        feeder.start()

        try:
            with (
                patch.object(mouse_button, "open_matching_evdev", side_effect=fake_open),
                patch.object(sys, "stdout", stdout_buf),
            ):
                runner._stdin_fd = r_stdin
                exit_code = runner.run_loop()
        finally:
            feeder.join(timeout=1.0)
            os.close(r_stdin)

        self.assertEqual(exit_code, mouse_button.EXIT_SUCCESS)
        lines = [json.loads(line) for line in stdout_buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(
            lines,
            [
                {"type": "start", "ime": True, "mode": "normal"},
                {"type": "stop", "ime": True},
            ],
        )


if __name__ == "__main__":
    unittest.main()
