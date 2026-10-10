import base64
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "wetype", Path(__file__).resolve().parents[1] / "resources/providers/wetype/streaming/entry.py"
)
assert spec and spec.loader
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class ProtocolTests(unittest.TestCase):
    def test_aes_known_vector_and_padding(self):
        aes = w.AES()
        key = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        encrypted = aes.crypt(key, plain)
        self.assertEqual(encrypted[:16].hex(), "f3eed1bdb5d2a03c064b5a7e3db181f8")
        self.assertEqual(aes.crypt(key, encrypted, False), plain)
        with self.assertRaises(ValueError):
            aes.crypt(key, encrypted[:-1], False)

    def test_ecdh_validation_and_agreement(self):
        self.assertIsNone(w.point_mul(w.N))
        a, b = 123456789, 987654321

        def public(k):
            x, y = w.point_mul(k)
            return f"04{x:032X}{y:032X}"

        self.assertEqual(w.shared_key(a, public(b)), w.shared_key(b, public(a)))
        with self.assertRaises(ValueError):
            w.shared_key(a, "04" + "0" * 64)

    def test_snappy_literals_copies_and_bounds(self):
        for data in (b"", b"hello", bytes(range(256)) * 10):
            self.assertEqual(w.decompress(w.compress(data)), data)
        for copy in (b"\x01\x02", b"\x0e\x02\x00", b"\x0f\x02\x00\x00\x00"):
            self.assertEqual(w.decompress(b"\x06\x04ab" + copy), b"ababab")
        for broken in (b"\x06\x01\x00", b"\x02\x08a", b"\x01\x04ab"):
            with self.assertRaises(ValueError):
                w.decompress(broken)

    def test_protobuf_repeated_fields_and_truncation(self):
        data = w.field(1, 300) + w.field(2, "甲") + w.field(2, "乙")
        self.assertEqual(w.fields(data), [(1, 300), (2, "甲".encode()), (2, "乙".encode())])
        for data in (b"\x80", b"\x12\x03a", b"\x0b"):
            with self.assertRaises(ValueError):
                w.fields(data)

    def test_compressed_voice_response_and_finish_request(self):
        client = w.Client.__new__(w.Client)
        client.aes = w.AES()
        client.key = b"0" * 32
        plain = w.field(1, w.field(4, "临时") + w.field(14, "定稿。"))
        encrypted = client.aes.crypt(client.key, w.compress(plain))
        client.roundtrip = MagicMock(return_value=(encrypted, {"Kb-CompressionType": "2"}))
        self.assertEqual(client.voice("voice-id", b"opus", 2, 100, True), ("临时", "定稿。"))
        args = client.roundtrip.call_args.args
        outer = dict(w.fields(w.decompress(client.aes.crypt(client.key, args[1], False))))
        packet = dict(w.fields(outer[1]))
        self.assertEqual((packet[2], packet[4], packet[6], packet[7], packet[11]), (b"voice-id", b"opus", 1, 2, 100))

    def test_http_response_preserves_repeated_headers(self):
        client = w.Client.__new__(w.Client)
        client.task = 0
        client.uin = "0"
        client.key = None
        client.ws = MagicMock()
        http = w.field(2, 200) + w.field(5, b"body")
        for key, value in [("Kb-CompressionType", "2"), ("Other", "value")]:
            http += w.field(4, w.field(1, key) + w.field(2, value))
        client.ws.recv_binary.return_value = w.field(4, http)
        body, headers = client.roundtrip("/timestamp")
        self.assertEqual(body, b"body")
        self.assertEqual(headers, {"Kb-CompressionType": "2", "Other": "value"})
        client.ws.recv_binary.return_value = w.field(4, w.field(2, 403))
        with self.assertRaisesRegex(RuntimeError, "403"):
            client.roundtrip("/timestamp")

    def test_identity_registration_and_cache_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "identity.json"
            path.write_text("[]")  # Invalid caches should recover, not crash.
            with patch.object(w, "WebSocketClient"), patch.dict(os.environ, {"VINPUT_ASR_CREDENTIAL_PATH": str(path)}):
                client = w.Client()
                client.key = b"0" * 32
                registration = client.aes.crypt(client.key, w.field(2, 123))
                client.roundtrip = MagicMock(side_effect=[(b"", {}), (registration, {}), (b"", {})])
                client.exchange = MagicMock(return_value=("public", "registration-token"))
                client.handshake()
                self.assertEqual(client.uin, "123")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text())["uin"], "123")
                client.roundtrip = MagicMock(return_value=(b"", {}))
                client.exchange.reset_mock()
                client.handshake()
                client.exchange.assert_called_once_with(False)

    def test_websocket_retains_bytes_after_http_upgrade(self):
        ws = w.WebSocketClient.__new__(w.WebSocketClient)
        sock = MagicMock()
        sock.recv.return_value = b"HTTP/1.1 101 Switching Protocols\r\n\r\n\x82\x00"
        ws._read_http_response(sock)
        self.assertEqual(ws._recv_buffer, b"\x82\x00")


class FakeEncoder:
    def __init__(self, *args):
        pass

    def encode(self, pcm, samples):
        if len(pcm) != 640 or samples != 320:
            raise AssertionError("Expected one padded 20 ms audio frame")
        return b"opus"


class FakeClient:
    def __init__(self):
        self.ws = MagicMock()
        self.calls = []

    def handshake(self):
        pass

    def voice(self, voice_id, opus, seq, total, end):
        self.calls.append((opus, seq, total, end))
        return ("完整口述", "完整口述。") if end else ("临时结果", "不能提前提交")


def audio(pcm=b"\0" * 3840):
    return json.dumps({"type": "audio", "audio_base64": base64.b64encode(pcm).decode()}) + "\n"


class LifecycleTests(unittest.TestCase):
    def invoke(self, stream, client=None):
        client = client or FakeClient()
        output, error = io.StringIO(), io.StringIO()
        with (
            patch.object(w, "Client", return_value=client),
            patch.object(w, "OpusEncoder", FakeEncoder),
            patch.object(w.sys, "stdin", stream),
            patch.object(w.sys, "stdout", output),
            patch.object(w.sys, "stderr", error),
        ):
            status = w.main()
        return status, [json.loads(line) for line in output.getvalue().splitlines()], error.getvalue()

    def test_one_final_after_finish_and_native_revision(self):
        client = FakeClient()
        status, events, error = self.invoke(io.StringIO(audio() + audio(b"\0" * 10) + '{"type":"finish"}\n'), client)
        self.assertEqual(status, 0)
        self.assertEqual(error, "")
        self.assertEqual([e["type"] for e in events], ["session_started", "partial", "partial", "final", "closed"])
        self.assertEqual(events[-2]["text"], "完整口述。")
        self.assertTrue(events[-2]["utterance_final"])
        self.assertTrue(client.calls[0][0].startswith(b"#!OPUS_RAW_V1\x02\x01\x00"))
        self.assertEqual([call[3] for call in client.calls], [False, True])

    def test_empty_recording_does_not_upload_audio(self):
        client = FakeClient()
        status, events, _ = self.invoke(io.StringIO('{"type":"finish"}\n'), client)
        self.assertEqual(status, 0)
        self.assertEqual([e["text"] for e in events if e["type"] == "final"], [""])
        self.assertEqual(client.calls, [])

    def test_cancel_and_eof_never_finalize(self):
        for stream in ('{"type":"cancel"}\n', ""):
            status, events, error = self.invoke(io.StringIO(stream))
            self.assertEqual(status, 0)
            self.assertFalse(any(e["type"] == "final" for e in events))
            self.assertEqual(error, "")

    def test_cancel_interrupts_blocked_handshake(self):
        started, aborted, stopped = threading.Event(), threading.Event(), threading.Event()
        client = FakeClient()

        def handshake():
            started.set()
            aborted.wait(2)
            stopped.set()
            raise OSError("connection aborted")

        client.handshake = handshake
        client.ws.abort.side_effect = aborted.set

        def stream():
            self.assertTrue(started.wait(2))
            yield '{"type":"cancel"}\n'

        start = time.monotonic()
        status, events, error = self.invoke(stream(), client)
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(stopped.wait(1))
        self.assertEqual(status, 0)
        self.assertFalse(any(e["type"] == "final" for e in events))
        self.assertEqual(error, "")

    def test_finish_timeout_preserves_latest_partial(self):
        client = FakeClient()

        def voice(*args):
            if args[-1]:
                raise TimeoutError("final timeout")
            return "保留口述", ""

        client.voice = voice
        status, events, error = self.invoke(io.StringIO(audio() + '{"type":"finish"}\n'), client)
        self.assertEqual(status, 0)
        self.assertEqual([e["text"] for e in events if e["type"] == "final"], ["保留口述"])
        self.assertEqual(error, "")

    def test_network_failure_before_finish_reports_error_without_final(self):
        client = FakeClient()
        client.voice = MagicMock(side_effect=RuntimeError("service unavailable"))
        status, events, error = self.invoke(io.StringIO(audio() + '{"type":"finish"}\n'), client)
        self.assertEqual(status, 1)
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(any(e["type"] == "final" for e in events))
        self.assertIn("service unavailable", error)

    def test_malformed_input_reports_error(self):
        for payload in (
            "not-json\n",
            "[]\n",
            '{"type":"unknown"}\n',
            '{"type":"audio","audio_base64":"!"}\n{"type":"finish"}\n',
        ):
            status, events, error = self.invoke(io.StringIO(payload))
            self.assertEqual(status, 1)
            self.assertEqual(events[-1]["type"], "error")
            self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
