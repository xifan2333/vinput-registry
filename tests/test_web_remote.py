"""Unit tests for trigger.vinput.web_remote virtual keys and WebSocket protocol."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / "resources/triggers/vinput/web_remote/entry.py"
spec = importlib.util.spec_from_file_location("web_remote_entry", ENTRY)
assert spec and spec.loader
web_remote = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = web_remote
spec.loader.exec_module(web_remote)


def _ws_send_masked(sock: socket.socket, payload_obj: dict[str, object]) -> None:
    payload = json.dumps(payload_obj).encode("utf-8")
    mask = b"\x12\x34\x56\x78"
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytes(payload[i] ^ mask[i % 4] for i in range(length))
    sock.sendall(bytes(header) + masked)


def _ws_recv_json(sock: socket.socket) -> dict[str, object]:
    def recv_exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("WebSocket closed")
            buf.extend(chunk)
        return bytes(buf)

    head = recv_exact(2)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(8))[0]
    payload = recv_exact(length)
    return json.loads(payload.decode("utf-8"))


class WebRemoteConfigTests(unittest.TestCase):
    def test_default_virtual_keys_include_menu_rightshift(self) -> None:
        env = {"VINPUT_TRIGGER_API_KEY": "secret", "VINPUT_TRIGGER_PORT": "8765"}
        with patch.dict(os.environ, env, clear=True):
            cfg = web_remote.load_config()

        self.assertEqual(
            [k.action_id for k in cfg.virtual_keys],
            ["menu", "esc", "up", "down", "left", "right", "backspace", "enter"],
        )
        self.assertEqual(cfg.virtual_keys[0].label, "Menu")
        self.assertEqual(cfg.virtual_keys[0].tokens, ("KEY_RIGHTSHIFT",))

    def test_override_and_disable_virtual_keys(self) -> None:
        env = {
            "VINPUT_TRIGGER_API_KEY": "secret",
            "VINPUT_TRIGGER_PORT": "8765",
            "VINPUT_TRIGGER_MENU_KEY": "KEY_LEFTCTRL+KEY_M",
            "VINPUT_TRIGGER_ESC_KEY": "NONE",
            "VINPUT_TRIGGER_ENTER_KEY": "0x1c",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = web_remote.load_config()

        by_id = {k.action_id: k for k in cfg.virtual_keys}
        self.assertNotIn("esc", by_id)
        self.assertEqual(by_id["menu"].tokens, ("KEY_LEFTCTRL", "KEY_M"))
        self.assertEqual(by_id["enter"].tokens, ("0x1c",))

    def test_invalid_key_spec_raises_config_error(self) -> None:
        env = {
            "VINPUT_TRIGGER_API_KEY": "secret",
            "VINPUT_TRIGGER_PORT": "8765",
            "VINPUT_TRIGGER_MENU_KEY": "KEY_LEFTCTRL+",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(web_remote.ConfigError):
                web_remote.load_config()


class LibEvdevAndUInputTests(unittest.TestCase):
    def setUp(self) -> None:
        web_remote.close_virtual_keyboard()
        web_remote._KEY_RESOLVER = None
        web_remote._RESOLVED_CHORDS.clear()

    def tearDown(self) -> None:
        web_remote.close_virtual_keyboard()
        web_remote._KEY_RESOLVER = None
        web_remote._RESOLVED_CHORDS.clear()

    def test_libevdev_distinguishes_left_and_right_modifiers(self) -> None:
        try:
            resolver = web_remote.LibEvdevResolver()
        except web_remote.VInputError:
            self.skipTest("libevdev shared library is not installed on this host")

        self.assertEqual(resolver.resolve_token("KEY_RIGHTSHIFT", "VINPUT_TRIGGER_MENU_KEY"), 54)
        self.assertEqual(resolver.resolve_token("KEY_LEFTSHIFT", "VINPUT_TRIGGER_MENU_KEY"), 42)
        self.assertEqual(resolver.resolve_token("KEY_LEFTCTRL", "VINPUT_TRIGGER_MENU_KEY"), 29)
        self.assertEqual(resolver.resolve_token("KEY_RIGHTCTRL", "VINPUT_TRIGGER_MENU_KEY"), 97)
        self.assertEqual(resolver.resolve_token("KEY_ESC", "VINPUT_TRIGGER_ESC_KEY"), 1)
        with self.assertRaises(web_remote.ConfigError):
            resolver.resolve_token("KEY_DEFINITELY_INVALID", "VINPUT_TRIGGER_MENU_KEY")

    def test_numeric_keycode_works_without_libevdev(self) -> None:
        binding = web_remote.VirtualKeyBinding(
            action_id="menu",
            label="Menu",
            env_name="VINPUT_TRIGGER_MENU_KEY",
            tokens=("54",),
        )
        with patch.dict(os.environ, {"VINPUT_TRIGGER_LIBEVDEV_PATH": "/nonexistent/libevdev.so.2"}):
            codes = web_remote._resolve_binding_codes(binding)
        self.assertEqual(codes, (54,))

    def test_missing_libevdev_path_notifies_gracefully(self) -> None:
        binding = web_remote.VirtualKeyBinding(
            action_id="menu",
            label="Menu",
            env_name="VINPUT_TRIGGER_MENU_KEY",
            tokens=("KEY_RIGHTSHIFT",),
        )
        web_remote._VIRTUAL_KEY_LIST = (binding,)
        web_remote._VIRTUAL_KEY_MAP = {"menu": binding}

        stderr = StringIO()
        with (
            patch.dict(os.environ, {"VINPUT_TRIGGER_LIBEVDEV_PATH": "/nonexistent/libevdev.so.2"}),
            patch.object(sys, "stderr", stderr),
        ):
            ok = web_remote.trigger_virtual_key("menu")

        self.assertFalse(ok)
        payload = json.loads(stderr.getvalue().strip())
        self.assertIn("libevdev", payload["body"])

    def test_uinput_keyboard_setup_and_chord_events(self) -> None:
        ioctl_calls: list[tuple[int, int, object]] = []
        written: list[bytes] = []

        def fake_ioctl(fd: int, req: int, arg: object = 0) -> int:
            ioctl_calls.append((fd, req, arg))
            return 0

        with (
            patch("os.open", return_value=42) as mock_open,
            patch("fcntl.ioctl", side_effect=fake_ioctl),
            patch("os.write", side_effect=lambda fd, data: written.append(data) or len(data)),
            patch("os.close") as mock_close,
            patch("time.sleep"),
        ):
            kbd = web_remote.UInputKeyboard(extra_codes={275})
            kbd.emit_chord((29, 46))
            kbd.close()

        mock_open.assert_called_once_with(web_remote.UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        self.assertIn((42, web_remote.UI_SET_EVBIT, web_remote.EV_KEY), ioctl_calls)
        self.assertIn((42, web_remote.UI_SET_KEYBIT, 1), ioctl_calls)
        self.assertIn((42, web_remote.UI_SET_KEYBIT, 248), ioctl_calls)
        self.assertIn((42, web_remote.UI_SET_KEYBIT, 275), ioctl_calls)
        self.assertTrue(any(req == web_remote.UI_DEV_SETUP for _, req, _ in ioctl_calls))
        self.assertIn((42, web_remote.UI_DEV_CREATE, 0), ioctl_calls)
        self.assertIn((42, web_remote.UI_DEV_DESTROY, 0), ioctl_calls)
        mock_close.assert_called_once_with(42)

        raw = written[0]
        step = web_remote.INPUT_EVENT_STRUCT.size
        events = [web_remote.INPUT_EVENT_STRUCT.unpack(raw[i : i + step])[2:] for i in range(0, len(raw), step)]
        self.assertEqual(
            events,
            [
                (web_remote.EV_KEY, 29, 1),
                (web_remote.EV_SYN, web_remote.SYN_REPORT, 0),
                (web_remote.EV_KEY, 46, 1),
                (web_remote.EV_SYN, web_remote.SYN_REPORT, 0),
                (web_remote.EV_KEY, 46, 0),
                (web_remote.EV_SYN, web_remote.SYN_REPORT, 0),
                (web_remote.EV_KEY, 29, 0),
                (web_remote.EV_SYN, web_remote.SYN_REPORT, 0),
            ],
        )

    def test_uinput_permission_error_notifies_without_crashing(self) -> None:
        binding = web_remote.VirtualKeyBinding(
            action_id="menu",
            label="Menu",
            env_name="VINPUT_TRIGGER_MENU_KEY",
            tokens=("54",),
        )
        web_remote._VIRTUAL_KEY_LIST = (binding,)
        web_remote._VIRTUAL_KEY_MAP = {"menu": binding}

        stderr = StringIO()
        with (
            patch("os.open", side_effect=PermissionError("denied")),
            patch.object(sys, "stderr", stderr),
        ):
            ok = web_remote.trigger_virtual_key("menu")

        self.assertFalse(ok)
        payload = json.loads(stderr.getvalue().strip())
        self.assertIn("/dev/uinput", payload["body"])


class WebSocketVirtualKeyFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        web_remote.close_virtual_keyboard()
        web_remote._RESOLVED_CHORDS.clear()

    def tearDown(self) -> None:
        web_remote.close_virtual_keyboard()
        web_remote._RESOLVED_CHORDS.clear()

    def test_virtual_key_flushes_pending_text_before_emitting_key(self) -> None:
        env = {
            "VINPUT_TRIGGER_API_KEY": "top-secret",
            "VINPUT_TRIGGER_PORT": "18080",
            "VINPUT_TRIGGER_ENTER_KEY": "28",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = web_remote.load_config()

        web_remote._API_KEY = cfg.api_key
        web_remote._DEBOUNCE_SEC = 60.0
        web_remote._VIRTUAL_KEY_LIST = cfg.virtual_keys
        web_remote._VIRTUAL_KEY_MAP = {b.action_id: b for b in cfg.virtual_keys}

        timeline: list[tuple[str, object]] = []

        class FakeKeyboard:
            def emit_chord(self, codes: tuple[int, ...]) -> None:
                timeline.append(("key", codes))

            def close(self) -> None:
                pass

        server = web_remote.ThreadedHttpServer(("127.0.0.1", 0), web_remote.RemoteHttpHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        stdout = StringIO()

        def capture_stdout_write(s: str) -> int:
            for line in s.splitlines():
                if line.strip():
                    timeline.append(("stdout", json.loads(line)))
            return stdout.write(s)

        fake_stdout = type("Stdout", (), {"write": staticmethod(capture_stdout_write), "flush": stdout.flush})()

        with (
            patch.object(web_remote, "_UINPUT_KEYBOARD", FakeKeyboard()),
            patch.object(sys, "stdout", fake_stdout),
            patch.object(sys, "stderr", StringIO()),
        ):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            self.addCleanup(sock.close)
            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(req.encode("ascii"))
            handshake = b""
            while b"\r\n\r\n" not in handshake:
                handshake += sock.recv(1024)
            expected_accept = base64.b64encode(
                hashlib.sha1((ws_key + web_remote.WS_MAGIC).encode("utf-8")).digest()
            ).decode("ascii")
            self.assertIn(f"Sec-WebSocket-Accept: {expected_accept}".encode("ascii"), handshake)

            _ws_send_masked(sock, {"type": "auth", "api_key": "top-secret"})
            self.assertEqual(_ws_recv_json(sock), {"type": "auth_ok"})
            init_msg = _ws_recv_json(sock)
            self.assertEqual(init_msg["output_status"], "ready")
            vkeys = init_msg["virtual_keys"]
            assert isinstance(vkeys, list)
            self.assertEqual(vkeys[0], {"id": "menu", "label": "Menu"})

            _ws_send_masked(sock, {"type": "text_update", "text": "ls -la"})
            _ws_send_masked(sock, {"type": "virtual_key", "id": "enter"})
            self.assertEqual(_ws_recv_json(sock), {"type": "committed"})

        self.assertEqual(
            timeline,
            [
                ("stdout", {"type": "inject", "stage": "partial", "text": "ls -la", "ime": True}),
                ("stdout", {"type": "inject", "stage": "final", "text": "ls -la", "ime": True}),
                ("key", (28,)),
            ],
        )

    def test_html_uses_compact_topbar_and_scheme_b_layout(self) -> None:
        html = web_remote.INDEX_HTML
        self.assertIn('<header class="topbar">', html)
        self.assertIn('". up ."', html)
        self.assertIn('"left down right"', html)
        self.assertIn('"menu esc"', html)
        self.assertIn('"backspace enter"', html)
        self.assertNotIn('.vk-btn[data-key-id="menu"]', html)
        self.assertIn(".vk-btn:not(:disabled).is-active", html)
        self.assertLess(html.index('<textarea id="editor"'), html.index('<section id="vkeys"'))


if __name__ == "__main__":
    unittest.main()
